"""
Standings cache for the Intelligence tab.

Builds a flat friendly/hostile entity-id set from the main character's
personal, corp, and alliance contact lists, persists it to disk, and
exposes is_friendly() for the analyzers.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

from app_io import atomic_write_json
from app_log import get_logger

log = get_logger(__name__)


class StandingsRefreshError(RuntimeError):
    """refresh() could not establish the owner's own corp/alliance affiliations."""


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
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # Preserve the original compact (no-indent) format and ascii escaping.
        atomic_write_json(self.path, payload, indent=None, ensure_ascii=True)

    def is_stale(self, max_age_hours: float = 24.0) -> bool:
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
