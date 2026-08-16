"""
Standings cache for the Intelligence tab.

Builds a flat friendly/hostile entity-id set from the main character's
personal, corp, and alliance contact lists, persists it to disk, and
exposes is_friendly() for the analyzers.

Schema v2 additionally records the owner's OWN corp/alliance ids so a
consumer can split the flat blue set three ways -- ours / allies / enemies
(``own_ids``, ``ally_ids``, ``bucket_of()``). The split is PURELY ADDITIVE:
the own ids stay inside ``friendly_ids`` because every existing consumer
(the zKill hostile-capital filter and the intel local-scan analyzer) would
otherwise start counting the owner's own alliance-mates as hostile.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

from app_io import atomic_write_json
from app_log import get_logger

log = get_logger(__name__)

# On-disk schema version. v1 (no "version" key) carried only friendly/hostile;
# v2 adds own_corp_id/own_alliance_id. is_stale() treats anything below this as
# stale so the existing unattended background refresh heals an old file -- see
# the migration note on is_stale().
SCHEMA_VERSION = 2

# Ownership buckets. These three strings are deliberately DUPLICATED from
# loss_reconciler.BUCKET_* rather than imported: standings_cache is a leaf
# module (intel_analyzer imports it) and must not pull in the loss-tracking
# engine. tests/test_standings_cache.py carries the drift guard that asserts
# the two definitions stay identical.
BUCKET_OURS = "ours"
BUCKET_ALLIES = "allies"
BUCKET_ENEMIES = "enemies"


class StandingsRefreshError(RuntimeError):
    """refresh() could not establish the owner's own corp/alliance affiliations."""


def _coerce_id(value) -> int | None:
    """Best-effort int for an entity id read off disk; None when unusable.

    A hand-edited (or otherwise odd) cache file can carry a string, a float or
    junk where an id belongs. Anything that will not survive int() -- and 0,
    which is not a real EVE entity id -- becomes None, i.e. "unknown", which
    every own-id consumer already treats as "cannot be ours".
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        ivalue = int(value)
    except (TypeError, ValueError):
        return None
    return ivalue or None


def is_friendly(
    char_id: int | None,
    corp_id: int | None,
    alliance_id: int | None,
    friendly_ids: set[int],
    own_character_ids: set[int],
) -> bool:
    if char_id is not None and char_id in own_character_ids:
        return True
    for entity in (char_id, corp_id, alliance_id):
        if entity is not None and entity in friendly_ids:
            return True
    return False


class StandingsCache:
    def __init__(self, path: str):
        self.path = path
        self.friendly_ids: set[int] = set()
        self.hostile_ids: set[int] = set()
        self.fetched_at: datetime | None = None
        self.source_character_id: int | None = None
        # v2 fields: the owner's OWN affiliations, also present in friendly_ids.
        self.own_corp_id: int | None = None
        self.own_alliance_id: int | None = None
        # Schema version of the data currently held. 0 = nothing loaded yet.
        self.version: int = 0

    def load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            log.warning(
                "standings cache at %s unreadable/corrupt; ignoring",
                self.path,
                exc_info=True,
            )
            return
        self.friendly_ids = set(data.get("friendly_ids") or [])
        self.hostile_ids = set(data.get("hostile_ids") or [])
        self.own_corp_id = _coerce_id(data.get("own_corp_id"))
        self.own_alliance_id = _coerce_id(data.get("own_alliance_id"))
        # A v1 file carries no "version" key at all. Anything unparseable is
        # treated as v1 too -- the conservative answer, since v1 only means
        # "refresh me" (is_stale), never data loss.
        raw_version = data.get("version")
        self.version = 1 if raw_version is None else (_coerce_id(raw_version) or 1)
        if self.version >= SCHEMA_VERSION and self.own_corp_id is None:
            # A file claiming v2 without the owner's own corp is not v2: every
            # alliance-mate would silently bucket as an ALLY rather than OURS,
            # forever, because is_stale() would call it fresh. refresh() can no
            # longer write such a file (it raises before saving when identity is
            # unresolved), but a hand-edited or older-build file can exist.
            # Demote it to v1 so is_stale() drives a heal.
            log.warning(
                "standings cache at %s claims v%s but has no own_corp_id; "
                "treating as v1 (stale) so the next refresh heals it",
                self.path, self.version,
            )
            self.version = 1
        ts = data.get("fetched_at")
        if ts:
            try:
                parsed = datetime.fromisoformat(ts)
            except ValueError:
                parsed = None
            if parsed is not None and parsed.tzinfo is None:
                # Rehydrated naive timestamp (e.g. an older file written before
                # this field was always UTC-aware): assume UTC rather than let
                # is_stale()/age_string() raise a naive-vs-aware TypeError when
                # they diff it against datetime.now(timezone.utc).
                parsed = parsed.replace(tzinfo=timezone.utc)
            self.fetched_at = parsed
        self.source_character_id = data.get("source_character_id")

    def save(self) -> None:
        payload = {
            "fetched_at": (self.fetched_at or datetime.now(timezone.utc)).isoformat(),
            "source_character_id": self.source_character_id,
            "friendly_ids": sorted(self.friendly_ids),
            "hostile_ids": sorted(self.hostile_ids),
        }
        # Stamp v2 ONLY when the owner's own corp is actually known. Writing
        # "version": 2 with a null own_corp_id would be worse than writing v1:
        # is_stale() would call that file fresh for 24h while ally_ids ==
        # friendly_ids, permanently misfiling every alliance-mate as an Ally.
        # Falling back to the v1 shape (no version key, no own ids -- byte
        # identical to what this method wrote before v2 existed) keeps
        # is_stale() driving the retry instead.
        if self.own_corp_id:
            payload["version"] = SCHEMA_VERSION
            payload["own_corp_id"] = int(self.own_corp_id)
            payload["own_alliance_id"] = (
                int(self.own_alliance_id) if self.own_alliance_id else None
            )
            self.version = SCHEMA_VERSION
        else:
            self.version = 1
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # Preserve the original compact (no-indent) format and ascii escaping.
        atomic_write_json(self.path, payload, indent=None, ensure_ascii=True)

    # ── Ours / allies split (schema v2) ─────────────────────────────────────
    # The owner's own ids stay INSIDE friendly_ids; these accessors derive the
    # split without moving anything, so is_friendly() and every existing
    # friendly_ids consumer are untouched.

    @property
    def own_identity_known(self) -> bool:
        """True once the owner's own corp has been resolved (v2 data).

        False for a v1 file (or a never-refreshed cache): own_ids is empty and
        ``ally_ids``/``bucket_of()`` cannot tell an alliance-mate from a blue,
        so they report them as ALLIES. is_stale() returns True in that state,
        so the condition is transient -- the background refresh heals it. A
        consumer that must not show a wrong number should check this flag.
        """
        return self.own_corp_id is not None

    @property
    def own_ids(self) -> set[int]:
        """The owner's OWN corporation + alliance ids (never character ids).

        Shaped to drop straight into
        ``loss_reconciler.classify_victim(km, own_ids=…, ally_ids=…)``.
        Empty when the identity is unknown -- an unknown own id is never a
        match, so nothing is misclassified as ours by accident.
        """
        ids: set[int] = set()
        if self.own_corp_id:
            ids.add(int(self.own_corp_id))
        if self.own_alliance_id:
            ids.add(int(self.own_alliance_id))
        return ids

    @property
    def ally_ids(self) -> set[int]:
        """Blue entity ids that are NOT the owner's own corp/alliance.

        Recomputed on every access (a set difference over friendly_ids) so it
        can never go stale behind a refresh(); snapshot it once if you are
        classifying in a hot loop.
        """
        return set(self.friendly_ids) - self.own_ids

    def bucket_of(self, char_id, corp_id, alliance_id,
                  *, fleet_char_ids=frozenset()) -> str:
        """Bucket one entity as "ours" / "allies" / "enemies".

        Precedence is fixed and matches ``battle_ledger.classify_victim``:
        fleet roster (character ids) -> own corp/alliance -> blues -> everyone
        else. An id the cache has never heard of is "enemies" -- this is a
        three-way partition of everything, not a lookup that can miss.

        The blues tier matches a blue CHARACTER id as well as a blue corp or
        alliance (2026-08-16). ``loss_reconciler.classify_victim`` still tests
        corp/alliance only, so the two answer differently for a pilot who is
        personally blue in a corp the cache does not know -- a KNOWN gap, and
        the reason the agreement test in tests/test_standings_cache.py carries
        no blue-character case.

        ``hostile_ids`` deliberately plays no part: "enemies" here means "not
        ours and not blue", which is the question the tally asks.
        """
        # Snapshot the mutable state up front: refresh() runs on a background
        # thread and rebinds these, so read each exactly once (the same
        # discipline zkill_monitor uses around its friendly-set snapshot).
        if char_id and char_id in fleet_char_ids:
            return BUCKET_OURS
        own_corp = self.own_corp_id
        own_alliance = self.own_alliance_id
        friendly_ids = self.friendly_ids
        for entity in (corp_id, alliance_id):
            if not entity:
                continue
            if own_corp and entity == own_corp:
                return BUCKET_OURS
            if own_alliance and entity == own_alliance:
                return BUCKET_OURS
        # Blue-but-not-own == ally. Testing friendly_ids directly (rather than
        # building ally_ids) is O(1) instead of O(len(friendly_ids)) per call
        # and is equivalent: every own id was already answered above.
        #
        # char_id belongs in this test because a blue list is not only corps
        # and alliances: PERSONAL contacts are individual pilots. On the
        # owner's live cache (2026-08-16) roughly 14 of 56 blue ids look like
        # character ids by id-range inference, so a corp/alliance-only test
        # files about a quarter of a real blue list under ENEMIES -- and every
        # one of them silently. is_friendly() above has always tested char_id --
        # the two disagreed until 2026-08-16 -- and battle_ledger.
        # classify_victim was brought into step in the same change.
        for entity in (corp_id, alliance_id, char_id):
            if entity and entity in friendly_ids:
                return BUCKET_ALLIES
        return BUCKET_ENEMIES

    def is_stale(self, max_age_hours: float = 24.0) -> bool:
        """True when the cache needs a refresh.

        Schema migration rides on this: data older than SCHEMA_VERSION is
        ALWAYS stale, whatever its age, so the unattended background refresh
        that already runs at startup (fc_gui._schedule_background_standings_
        refresh) repopulates the v2 fields with no bespoke migration code. A v1
        file therefore keeps behaving exactly as before -- same friendly/hostile
        sets, same is_friendly() answers -- it is just due for a refresh.
        """
        if self.version < SCHEMA_VERSION:
            return True
        if self.fetched_at is None:
            return True
        age = datetime.now(timezone.utc) - self.fetched_at
        return age > timedelta(hours=max_age_hours)

    def refresh(self, auth) -> None:
        """Pull contacts from ESI and rebuild the cache.

        Also marks the active character's own corp and alliance as friendly,
        because EVE's contact endpoints don't list "yourself" -- your own
        affiliations aren't entities you have standings toward, so without this
        explicit handling alliance-mates would silently bucket as hostile.

        Raises:
            StandingsRefreshError: the owner's own corp/alliance could not be
                established (no character id, the /characters/{id}/ GET failed
                or returned something malformed). Nothing is mutated and
                nothing is saved in that case -- see the guard below.
        """
        friendly: set[int] = set()
        hostile: set[int] = set()

        own_char_id = (
            getattr(auth, "character_id", None)
            or getattr(auth, "_character_id", None)
        )
        own_corp_id = None
        own_alliance_id = None
        if own_char_id:
            try:
                info = auth.esi_get(f"/characters/{own_char_id}/")
            except Exception:
                info = None
            if isinstance(info, dict):
                own_corp_id = info.get("corporation_id")
                own_alliance_id = info.get("alliance_id")

        # Bail out rather than build a half-cache. This single guard covers all
        # three degraded cases -- no character id, the GET raising / returning a
        # non-dict, and a dict that came back without a corporation_id (every
        # EVE character belongs to a corporation, NPC starter corps included, so
        # a sheet missing it is malformed and must not be trusted). All three
        # leave the owner's OWN corp/alliance out of the friendly set, and
        # saving that would stamp fetched_at = now, making is_stale() report an
        # affiliation-less cache as fresh for 24h -- during which is_friendly()
        # returns False for the owner's own alliance-mates (own-fleet capitals
        # counted as hostile). Bailing BEFORE the contact GETs also avoids
        # spending ESI calls (and error budget) on a result we would discard.
        # Leaving friendly_ids / hostile_ids / fetched_at / source_character_id
        # untouched means a previously-good cache survives in memory and on
        # disk, and is_stale() keeps driving the retry (next launch).
        if not own_corp_id:
            log.warning(
                "standings refresh aborted: could not resolve own corporation "
                "for character_id=%r; cache left untouched",
                own_char_id,
            )
            raise StandingsRefreshError(
                "could not resolve the owner's own corporation "
                f"(character_id={own_char_id!r})"
            )

        friendly.add(int(own_corp_id))
        if own_alliance_id:
            friendly.add(int(own_alliance_id))

        # Build the contact getters. The corp/alliance ids are known by now, so
        # pass them through to skip the redundant /characters/{id}/ GETs that
        # get_corp_contacts / get_alliance_contacts would otherwise perform to
        # rediscover the same ids -- one /characters/{id}/ GET per refresh
        # instead of three. A character with no alliance simply omits the
        # alliance getter (today's get_alliance_contacts returns an empty list
        # for that case, so the contribution is identical).
        getters: list = [("get_personal_contacts", auth.get_personal_contacts)]
        getters.append(
            ("get_corp_contacts",
             lambda cid=own_corp_id: auth.get_corp_contacts(cid))
        )
        if own_alliance_id:
            getters.append(
                ("get_alliance_contacts",
                 lambda aid=own_alliance_id: auth.get_alliance_contacts(aid))
            )
        # else: character has no alliance -> skip the call entirely.

        for name, getter in getters:
            try:
                rows = getter() or []
            except (OSError, ValueError, RuntimeError) as exc:
                print(f"[standings_cache] {name} failed: {exc}", file=sys.stderr)
                rows = []
            for row in rows:
                cid = row.get("contact_id")
                standing = row.get("standing", 0)
                if cid is None:
                    continue
                if standing > 0:
                    friendly.add(int(cid))
                elif standing < 0:
                    hostile.add(int(cid))

        self.friendly_ids = friendly
        self.hostile_ids = hostile
        # Record the owner's own affiliations SEPARATELY as well as inside
        # friendly_ids (schema v2). Only reachable past the guard above, so
        # these are never stamped from a failed identity fetch -- save() then
        # writes version 2. A character with no alliance is a legitimate v2
        # state, not a degraded one: own_alliance_id stays None, own_ids is
        # just the corp.
        self.own_corp_id = int(own_corp_id)
        self.own_alliance_id = int(own_alliance_id) if own_alliance_id else None
        self.fetched_at = datetime.now(timezone.utc)
        self.source_character_id = own_char_id
        self.save()

    def age_string(self) -> str:
        if self.fetched_at is None:
            return "never"
        delta = datetime.now(timezone.utc) - self.fetched_at
        if delta.total_seconds() < 0:
            return "just now"
        hours = delta.total_seconds() / 3600
        if hours < 1:
            return f"{int(delta.total_seconds() / 60)}m old"
        if hours < 24:
            return f"{int(hours)}h old"
        return f"{int(hours / 24)}d old"
