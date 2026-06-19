"""
CynoCheck — find a character's recent cyno-ship LOSSES.

Given an EVE ``character_id``, look at the character's killboard LOSSES over the
past 6 months and report the ones where the victim hull was a cyno-capable ship
(HIC / Force Recon / Strategic Cruiser / Stealth Bomber / Covert Ops) AND a
Cynosural Field Generator (normal or covert) was fitted in a high slot. These
are "lightswitch" / cyno-alt losses — useful intel on who a hostile uses to
bridge capitals into your space.

The feature is *losses only* and works WITHOUT login: zKillboard's REST API and
ESI killmail GETs are public. All HTTP goes through a small injectable fetcher
(:class:`_HttpFetcher`) so tests can run fully offline.

Data sources
------------
* zKillboard losses list (per cyno group):
  ``https://zkillboard.com/api/characterID/{cid}/losses/groupID/{gid}/page/{n}/``
  (TRAILING SLASH REQUIRED). Each entry is ``{"killmail_id", "zkb": {...}}``;
  ``killmail_time`` is taken from the ESI killmail, not this list.
* ESI killmail (public, immutable):
  ``{ESI_BASE}/killmails/{killmail_id}/{hash}/`` — gives ``victim.ship_type_id``,
  ``victim.items[]`` and ``killmail_time``. Cached forever.
* zKillboard "related" battle: ``https://zkillboard.com/api/related/{system_id}/{YYYYMMDDHH00}/``
  — the cluster of killmails around a fight (the ~1h battle report for a system
  at a given HOUR). zKill's related endpoint only accepts an HOUR-ALIGNED
  timestamp (``…HH00``); a non-aligned minute returns HTTP 500, so we floor the
  loss time to the hour (and, near hour boundaries, also try the previous hour).
  The response is a team-summary object, NOT a bare list of killmail stubs:
  ``{"summary": {"teamA": {...}, "teamB": {...}}}`` where each team carries a
  ``kills`` DICT keyed by killID. Each kill object holds the ``victim`` party and
  an ``involved`` list (ALL participants, the victim included with
  ``isVictim: true``) INLINE — camelCase fields (``characterID``,
  ``corporationID``, ``allianceID``, ``shipTypeID``) plus ``allianceName`` /
  ``corporationName``. This is the PRIMARY association signal: for each recent
  cyno LOSS we read who killed the pilot (the loss's attackers = the "enemy")
  and then scan the surrounding battle to find who fought ON THE PILOT'S SIDE
  (i.e. the pilot's blues — the bridge's friends), all from the inline data — no
  per-killmail ESI re-fetch. Cached FOREVER (immutable history), keyed by
  ``(system_id, YYYYMMDDHH00)``.
* zKillboard character stats: ``https://zkillboard.com/api/stats/characterID/{cid}/``
  — the FALLBACK association signal (an all-time aggregate of who the character
  most flies with). zKill's ``topLists`` is frequently empty in practice, so we
  fall back to the populated ``topAllTime`` section (confirmed via live API
  inspection).
* Public ESI affiliation: ``POST {ESI_BASE}/characters/affiliation/`` with
  ``[character_id]`` — to learn the character's OWN corp/alliance so we can
  exclude them from the association.

Association
-----------
The reported ``association`` carries a ``basis`` field:
  * ``"battles"`` — inferred from the battle around recent cyno losses (primary),
    read entirely from the related endpoint's inline team-summary data.
  * ``"stats"``   — the all-time zKill stats aggregate (hybrid fallback, used
    when the battle scan yields no usable friendly entity or is unavailable).
A degraded/unavailable battle scan never crashes the run: it marks the result
partial and falls back to stats; if that too is empty, ``{"kind": "unknown"}``.

Caching
-------
``cyno_cache.json`` (in :func:`app_path.app_dir`, cloned from the StandingsCache
atomic-write pattern) stores the per-character analysis result + a timestamp and
exposes ``is_stale(max_age_hours)``. Individual killmails (immutable) are cached
forever in the same file under ``"killmails"``; "related" battle clusters
(immutable history) are cached forever under ``"related"``, keyed by
``<system_id>:<YYYYMMDDHH00>`` (the hour-floored battle key).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

from app_io import atomic_write_json
from app_log import get_logger
from app_path import app_dir
from esi_constants import ESI_BASE, ESI_HEADERS_JSON as HEADERS
from rate_limiter import rate_limit
from ship_classes import (
    CYNO_LOSS_GROUPS,
    CYNO_MODULE_IDS,
    cyno_loss_hull_class,
)

log = get_logger(__name__)

ZKILL_API = "https://zkillboard.com/api"

# zKillboard REST returns up to 1000 mails per page (the documented hard cap).
# We stop a group early on the first page whose mails are entirely older than
# the cutoff, so the exact value only bounds the worst case.
ZKILL_PAGE_SIZE = 1000
MAX_PAGES_PER_GROUP = 10  # safety bound: 10 * 1000 = 10000 mails/group max

LOOKBACK_DAYS = 182  # ~6 months

# Battle-inferred association bound. We sample at most the MAX_BATTLES most
# recent qualifying cyno losses. Each battle is ONE related-endpoint fetch whose
# response already carries every kill's victim + attackers inline, so there is no
# per-killmail fan-out to bound — we scan all of a battle's kills in memory.
MAX_BATTLES = 8

# High-slot inventory flags (EVE SDE). A cyno generator only counts if it is
# actually FITTED in a high slot, not sitting in cargo/hold. HiSlot0..HiSlot7.
HIGH_SLOT_FLAGS = set(range(27, 35))  # 27..34 inclusive

# Module slot "flag" string variants seen on ESI killmails. ESI returns an int
# `flag`; we accept the int range above. (Some historic exports used strings
# like "HiSlot0"; we tolerate those defensively.)
_HIGH_SLOT_FLAG_NAMES = {f"HiSlot{i}" for i in range(8)}


def six_month_cutoff(now: datetime | None = None) -> datetime:
    """Return the timezone-aware UTC cutoff: now - ~6 months."""
    base = now or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return base - timedelta(days=LOOKBACK_DAYS)


def _parse_km_time(value) -> datetime | None:
    """Parse an ESI killmail_time ISO-8601 string to an aware UTC datetime."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _is_high_slot(flag) -> bool:
    """True if an item `flag` denotes a high (fitted) slot."""
    if isinstance(flag, bool):  # bool is a subclass of int — reject explicitly
        return False
    if isinstance(flag, int):
        return flag in HIGH_SLOT_FLAGS
    if isinstance(flag, str):
        return flag in _HIGH_SLOT_FLAG_NAMES
    return False


def _iter_items(items):
    """Yield every item dict in a victim.items[] list, recursing into nested
    container contents (`items` on a container module). Defensive against
    non-list/non-dict entries."""
    if not isinstance(items, list):
        return
    for it in items:
        if not isinstance(it, dict):
            continue
        yield it
        nested = it.get("items")
        if isinstance(nested, list):
            yield from _iter_items(nested)


def victim_has_high_slot_cyno(items) -> bool:
    """Return True if victim.items[] contains a cyno module in a HIGH slot.

    Recurses into nested container items. A cyno generator in cargo, a hold, or
    a low/med slot does NOT qualify — only a fitted high-slot generator marks
    the hull as an actual cyno ship.
    """
    for it in _iter_items(items):
        type_id = it.get("item_type_id")
        if type_id in CYNO_MODULE_IDS and _is_high_slot(it.get("flag")):
            return True
    return False


class _HttpFetcher:
    """Thin, injectable HTTP layer.

    All network access in CynoChecker routes through this object so tests can
    substitute a fake. Each method applies the correct rate-limit bucket and
    returns parsed JSON (or None on any failure). A shared ``requests.Session``
    is used for connection reuse; ``status`` of the last response is exposed so
    callers can back off on 403/429.
    """

    def __init__(self, session: requests.Session | None = None, timeout: float = 15.0):
        self._session = session or requests.Session()
        self._session.headers.update(HEADERS)
        self.timeout = timeout
        self.last_status: int | None = None
        # Set True once we see a 403/429 from zKill so the checker can stop hammering.
        self.zkill_blocked = False

    # -- low-level --------------------------------------------------------
    def _get(self, url: str, bucket: str, extra_headers: dict | None = None):
        rate_limit(bucket)
        try:
            resp = self._session.get(
                url, timeout=self.timeout, headers=extra_headers or None
            )
        except requests.RequestException as exc:
            print(f"[cyno_check] GET {url} failed: {exc}", file=sys.stderr)
            self.last_status = None
            return None
        self.last_status = resp.status_code
        if resp.status_code in (403, 429):
            if bucket == "zkill_api":
                self.zkill_blocked = True
            print(f"[cyno_check] GET {url} -> {resp.status_code} (backing off)",
                  file=sys.stderr)
            return None
        if not resp.ok:
            print(f"[cyno_check] GET {url} -> {resp.status_code}", file=sys.stderr)
            return None
        try:
            return resp.json()
        except ValueError:
            print(f"[cyno_check] GET {url} returned non-JSON body", file=sys.stderr)
            return None

    # -- zKillboard -------------------------------------------------------
    def zkill_losses_page(self, character_id: int, group_id: int, page: int):
        """GET one page of a character's losses for a single hull group.

        Returns a list of mail entries ({"killmail_id", "zkb": {...}}), or None
        on failure. TRAILING SLASH is required by zKill's REST router.
        """
        url = (f"{ZKILL_API}/characterID/{character_id}"
               f"/losses/groupID/{group_id}/page/{page}/")
        # Accept gzip — zKill encourages compressed responses for the REST API.
        data = self._get(url, "zkill_api", extra_headers={"Accept-Encoding": "gzip"})
        if data is None:
            return None
        # zKill returns a JSON list; an error object would be a dict.
        if isinstance(data, list):
            return data
        return []

    def zkill_stats(self, character_id: int):
        """GET zKillboard stats for a character (dict) or None."""
        url = f"{ZKILL_API}/stats/characterID/{character_id}/"
        data = self._get(url, "zkill_api", extra_headers={"Accept-Encoding": "gzip"})
        return data if isinstance(data, dict) else None

    def zkill_related(self, solar_system_id: int, ts_hour: str):
        """GET the zKillboard "related" battle for a system at a given HOUR.

        ``ts_hour`` is the killmail time floored to the hour as ``YYYYMMDDHH00``
        (UTC). zKill's related endpoint returns HTTP 500 for any non-hour-aligned
        timestamp, so the caller MUST pass an ``…HH00`` value. Returns the parsed
        team-summary object (a dict ``{"summary": {"teamA": {...},
        "teamB": {...}}}``), or None on failure. TRAILING SLASH is required by
        zKill's REST router. The result is an immutable slice of history, so the
        caller caches it forever.
        """
        url = f"{ZKILL_API}/related/{solar_system_id}/{ts_hour}/"
        data = self._get(url, "zkill_api", extra_headers={"Accept-Encoding": "gzip"})
        if data is None:
            return None
        # zKill returns a team-summary dict; tolerate anything else by returning
        # it as-is and letting the parser degrade (it never crashes on odd shapes).
        return data

    # -- ESI (public) -----------------------------------------------------
    def esi_killmail(self, killmail_id: int, killmail_hash: str):
        """GET a public, immutable ESI killmail (dict) or None."""
        url = f"{ESI_BASE}/killmails/{killmail_id}/{killmail_hash}/"
        data = self._get(url, "esi")
        return data if isinstance(data, dict) else None

    def esi_affiliation(self, character_id: int):
        """POST public /characters/affiliation/ for one character.

        Returns the single affiliation dict ({character_id, corporation_id,
        alliance_id, ...}) or None. No auth required.
        """
        rate_limit("esi")
        url = f"{ESI_BASE}/characters/affiliation/"
        try:
            resp = self._session.post(
                url,
                json=[character_id],
                headers={"Content-Type": "application/json"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            print(f"[cyno_check] affiliation POST failed: {exc}", file=sys.stderr)
            return None
        self.last_status = resp.status_code
        if not resp.ok:
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data[0]
        return None

    def esi_names(self, ids):
        """POST public /universe/names/ to resolve a list of ids to names.

        Returns a list of {id, name, category} dicts (possibly empty)."""
        ids = [i for i in ids if i]
        if not ids:
            return []
        rate_limit("esi")
        url = f"{ESI_BASE}/universe/names/"
        try:
            resp = self._session.post(
                url,
                json=ids,
                headers={"Content-Type": "application/json"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            print(f"[cyno_check] names POST failed: {exc}", file=sys.stderr)
            return []
        self.last_status = resp.status_code
        if not resp.ok:
            return []
        try:
            data = resp.json()
        except ValueError:
            return []
        return data if isinstance(data, list) else []


class CynoCache:
    """Per-character CynoCheck result cache.

    Clones the StandingsCache atomic-write (tmp + os.replace) and is_stale()
    pattern. Stores:
      * ``results``  : {str(character_id): {"result": <analysis>, "fetched_at": iso}}
      * ``killmails``: {str(killmail_id): <esi killmail dict>}  (immutable, forever)
      * ``related``  : {"<system_id>:<YYYYMMDDHH00>": <team-summary dict>}
                       (immutable battle history, forever; keyed by the
                       hour-floored battle timestamp)
    """

    def __init__(self, path: str):
        self.path = path
        self.results: dict[str, dict] = {}
        self.killmails: dict[str, dict] = {}
        self.related: dict[str, dict] = {}
        self._loaded = False

    def load(self) -> None:
        self._loaded = True
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            # A corrupt/unreadable cache is silently discarded (we just start with
            # empty caches). Log it so the discard isn't invisible.
            log.warning("cyno cache load failed; starting empty", exc_info=True)
            return
        if isinstance(data, dict):
            res = data.get("results")
            kms = data.get("killmails")
            rel = data.get("related")
            if isinstance(res, dict):
                self.results = res
            if isinstance(kms, dict):
                self.killmails = kms
            if isinstance(rel, dict):
                self.related = rel

    def save(self) -> None:
        payload = {
            "results": self.results,
            "killmails": self.killmails,
            "related": self.related,
        }
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # The original hand-rolled tmp+os.replace used json.dump(payload, f) with
        # no indent kwarg (compact) and the default ensure_ascii=True. Preserve
        # both: indent=None keeps the file compact, ensure_ascii=True matches.
        atomic_write_json(self.path, payload, indent=None, ensure_ascii=True)

    # -- result cache -----------------------------------------------------
    def get_result(self, character_id: int):
        if not self._loaded:
            self.load()
        return self.results.get(str(character_id))

    def is_stale(self, character_id: int, max_age_hours: float = 2.0) -> bool:
        entry = self.get_result(character_id)
        if not entry:
            return True
        ts = entry.get("fetched_at")
        fetched_at = _parse_km_time(ts) if isinstance(ts, str) else None
        if fetched_at is None:
            return True
        return (datetime.now(timezone.utc) - fetched_at) > timedelta(hours=max_age_hours)

    def put_result(self, character_id: int, result: dict) -> None:
        if not self._loaded:
            self.load()
        self.results[str(character_id)] = {
            "result": result,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

    # -- killmail cache (immutable) ---------------------------------------
    def get_killmail(self, killmail_id: int):
        if not self._loaded:
            self.load()
        return self.killmails.get(str(killmail_id))

    def put_killmail(self, killmail_id: int, killmail: dict) -> None:
        if not self._loaded:
            self.load()
        self.killmails[str(killmail_id)] = killmail

    # -- related-battle cache (immutable history) -------------------------
    @staticmethod
    def _related_key(system_id, ts_hour) -> str:
        return f"{system_id}:{ts_hour}"

    def get_related(self, system_id, ts_hour):
        if not self._loaded:
            self.load()
        return self.related.get(self._related_key(system_id, ts_hour))

    def put_related(self, system_id, ts_hour, battle) -> None:
        if not self._loaded:
            self.load()
        self.related[self._related_key(system_id, ts_hour)] = battle


def default_cache_path() -> str:
    return os.path.join(app_dir(), "cyno_cache.json")


class CynoChecker:
    """Analyze a character's recent cyno-ship losses.

    Construct with optional ``fetcher`` (HTTP layer) and ``cache`` overrides for
    testing; both default to live implementations. The public entry point is
    :meth:`analyze_character`.
    """

    def __init__(self, fetcher: _HttpFetcher | None = None,
                 cache: CynoCache | None = None,
                 lookback_days: int = LOOKBACK_DAYS):
        self.fetcher = fetcher or _HttpFetcher()
        self.cache = cache if cache is not None else CynoCache(default_cache_path())
        self.lookback_days = lookback_days

    # -- public API -------------------------------------------------------
    def analyze_character(self, character_id: int, progress=None,
                          use_cache: bool = True,
                          max_age_hours: float = 2.0) -> dict:
        """Analyze ``character_id``'s cyno-ship losses over the past ~6 months.

        Returns a result dict with fields:
          * ``total``       : int — number of qualifying cyno losses
          * ``breakdown``   : dict[str, int] — count per hull class
          * ``latest``      : {"killmail_id", "url", "time"} | None — most recent
          * ``association`` : dict — {name, id, kind, count, sample_total, basis,
                              battle_count, confident, runners_up} where basis is
                              "battles" (primary) or "stats" (fallback); or
                              {"kind": "unknown"}. For basis "battles", count ==
                              battle_count (the top entity's distinct-battle
                              presence), confident is True when the top entity is
                              present in a strict majority of scanned battles, and
                              runners_up lists up to the next 2 entities
                              ({name, id, kind, battle_count}). For basis "stats"
                              battle_count is None, confident is True, runners_up
                              is []
          * ``losses``      : list[dict] — per-loss display rows (newest first)
          * ``status``      : str — human-readable status / partial-result note

        ``progress`` is an optional ``callable(str)`` for streaming status lines
        to a GUI. ``character_id`` invalid / falsy returns an empty result with a
        status. Never raises on network/parse errors — returns partial results.
        """
        def _emit(msg: str) -> None:
            if progress:
                try:
                    progress(msg)
                except Exception:
                    pass

        if not character_id or not isinstance(character_id, int) or character_id <= 0:
            return self._empty_result("invalid character_id")

        if use_cache and self.cache is not None:
            try:
                if not self.cache.is_stale(character_id, max_age_hours):
                    cached = self.cache.get_result(character_id)
                    if cached and isinstance(cached.get("result"), dict):
                        _emit("Using cached CynoCheck result")
                        return cached["result"]
            except Exception:
                pass  # cache problems must never block a live analysis

        cutoff = six_month_cutoff() if self.lookback_days == LOOKBACK_DAYS else (
            datetime.now(timezone.utc) - timedelta(days=self.lookback_days)
        )

        _emit(f"Scanning losses for character {character_id} ...")
        qualifying: list[dict] = []
        partial = False

        for gid, label in CYNO_LOSS_GROUPS.items():
            _emit(f"Checking {label} losses ...")
            try:
                group_quals, group_partial = self._scan_group(
                    character_id, gid, label, cutoff, _emit
                )
            except Exception as exc:  # defensive: a group failure can't sink the run
                print(f"[cyno_check] group {gid} scan crashed: {exc}", file=sys.stderr)
                group_quals, group_partial = [], True
            qualifying.extend(group_quals)
            partial = partial or group_partial
            if self.fetcher.zkill_blocked:
                _emit("zKillboard rate-limited; returning partial results")
                partial = True
                break

        # Aggregate
        breakdown: dict[str, int] = {}
        for row in qualifying:
            breakdown[row["hull_class"]] = breakdown.get(row["hull_class"], 0) + 1

        qualifying.sort(key=lambda r: r.get("time") or "", reverse=True)
        latest = None
        if qualifying:
            top = qualifying[0]
            latest = {
                "killmail_id": top["killmail_id"],
                "url": top["url"],
                "time": top["time"],
            }

        _emit("Resolving association ...")
        association = {"kind": "unknown"}
        # PRIMARY: infer the pilot's blues from the battle around recent losses.
        try:
            own_corp_id, own_alliance_id = self._own_affiliation(character_id)
            battle_assoc, battle_partial = self._associate_from_battles(
                qualifying, own_corp_id, own_alliance_id, _emit
            )
            partial = partial or battle_partial
        except Exception as exc:
            print(f"[cyno_check] battle association failed: {exc}", file=sys.stderr)
            battle_assoc, own_corp_id, own_alliance_id = None, None, None
            partial = True

        if battle_assoc is not None:
            association = battle_assoc
        else:
            # HYBRID FALLBACK: the all-time zKill stats aggregate.
            try:
                stats_assoc = self._compute_association(
                    character_id, own_corp_id, own_alliance_id, _emit
                )
            except Exception as exc:
                print(f"[cyno_check] stats association failed: {exc}", file=sys.stderr)
                stats_assoc = {"kind": "unknown"}
            association = stats_assoc

        status = self._build_status(len(qualifying), partial,
                                    association.get("kind") == "unknown")

        result = {
            "total": len(qualifying),
            "breakdown": breakdown,
            "latest": latest,
            "association": association,
            "losses": qualifying,
            "status": status,
        }

        # Persist (best-effort; never fatal). Don't cache obviously-partial runs
        # so a transient outage doesn't get pinned for the whole TTL.
        if self.cache is not None and not partial:
            try:
                self.cache.put_result(character_id, result)
                self.cache.save()
            except Exception as exc:
                print(f"[cyno_check] cache save failed: {exc}", file=sys.stderr)

        _emit("Done")
        return result

    # -- internals --------------------------------------------------------
    def _scan_group(self, character_id, group_id, label, cutoff, emit):
        """Scan one hull group's loss pages newest-first until older than cutoff.

        Returns (qualifying_rows, partial_flag). ``partial_flag`` is True if a
        page fetch failed (so the count may be incomplete).
        """
        quals: list[dict] = []
        partial = False
        for page in range(1, MAX_PAGES_PER_GROUP + 1):
            entries = self.fetcher.zkill_losses_page(character_id, group_id, page)
            if entries is None:
                partial = True
                break  # network/HTTP failure for this group page
            if not entries:
                break  # no more mails in this group
            stop = False
            for entry in entries:
                km_id = entry.get("killmail_id")
                zkb = entry.get("zkb") or {}
                km_hash = zkb.get("hash")
                if not km_id or not km_hash:
                    continue
                killmail = self._get_killmail(km_id, km_hash)
                if killmail is None:
                    partial = True
                    continue
                kt = _parse_km_time(killmail.get("killmail_time"))
                if kt is not None and kt < cutoff:
                    # Mails are newest-first; once we cross the cutoff we can
                    # stop scanning this group entirely.
                    stop = True
                    break
                row = self._qualify(km_id, killmail, kt)
                if row is not None:
                    row["killmail_hash"] = km_hash
                    quals.append(row)
            if stop:
                break
            if len(entries) < ZKILL_PAGE_SIZE:
                break  # last page (short page) — no need to request another
        return quals, partial

    def _qualify(self, killmail_id, killmail, kt):
        """Return a per-loss row if the killmail qualifies, else None.

        Qualifies when: victim hull is a cyno-capable group AND a cyno module is
        fitted in a high slot (including nested container contents).
        """
        victim = killmail.get("victim")
        if not isinstance(victim, dict):
            return None
        ship_type_id = victim.get("ship_type_id")
        hull_class = cyno_loss_hull_class(ship_type_id)
        if hull_class is None:
            return None
        if not victim_has_high_slot_cyno(victim.get("items")):
            return None
        time_str = killmail.get("killmail_time")
        return {
            "killmail_id": killmail_id,
            "hull_class": hull_class,
            "ship_type_id": ship_type_id,
            "time": time_str,
            "url": f"https://zkillboard.com/kill/{killmail_id}/",
            "solar_system_id": killmail.get("solar_system_id"),
        }

    def _get_killmail(self, killmail_id, killmail_hash):
        """Fetch an ESI killmail with a forever-cache (immutable)."""
        if self.cache is not None:
            cached = self.cache.get_killmail(killmail_id)
            if cached is not None:
                return cached
        km = self.fetcher.esi_killmail(killmail_id, killmail_hash)
        if km is not None and self.cache is not None:
            try:
                self.cache.put_killmail(killmail_id, km)
            except Exception:
                pass
        return km

    # -- association: battle-inferred (primary) ---------------------------
    @staticmethod
    def _entity_of(party) -> int | None:
        """Reduce an ESI attacker/victim dict to its association entity id.

        Used for the pilot's OWN loss killmail (fetched from ESI, snake_case).
        Prefer alliance, else corporation. Returns None for NPCs (no
        ``character_id``) and faction-only / structure parties (no corp or
        alliance), so they never pollute the friendly/enemy tallies.
        """
        if not isinstance(party, dict):
            return None
        if not party.get("character_id"):
            return None  # NPC / structure / faction-only — not a real pilot
        return party.get("alliance_id") or party.get("corporation_id")

    @staticmethod
    def _entity_of_inline(party) -> int | None:
        """Reduce a zKill related-battle party to its association entity id.

        The related endpoint's inline ``victim``/``involved`` parties use
        **camelCase** keys (``characterID``, ``allianceID``, ``corporationID``)
        — NOT the snake_case of the ESI killmail format. Prefer alliance, else
        corporation. Returns None for NPCs/structures (no ``characterID``), so
        they never pollute the tallies.
        """
        if not isinstance(party, dict):
            return None
        if not party.get("characterID"):
            return None  # NPC / structure — not a real pilot
        return party.get("allianceID") or party.get("corporationID")

    @staticmethod
    def _iter_battle_kills(battle):
        """Yield each kill object from a zKill related team-summary response.

        The real shape is ``{"summary": {"teamA": {...}, "teamB": {...}}}`` where
        each team carries a ``kills`` DICT keyed by killID; each value is a kill
        object holding ``victim`` and ``involved`` (the full participant list,
        victim included with ``isVictim: true``) INLINE in camelCase. Both teams
        can report the same killID (one per side), so we DEDUPE by killID.

        Degrades silently on any missing/odd shape (yields nothing) so the caller
        falls back to stats and never crashes.
        """
        if not isinstance(battle, dict):
            return
        summary = battle.get("summary")
        if not isinstance(summary, dict):
            return
        seen: set = set()
        for team_key in ("teamA", "teamB"):
            team = summary.get(team_key)
            if not isinstance(team, dict):
                continue
            kills = team.get("kills")
            if not isinstance(kills, dict):
                continue
            for kid, kill in kills.items():
                if not isinstance(kill, dict):
                    continue
                key = kill.get("killID") or kid
                if key in seen:
                    continue
                seen.add(key)
                yield kill

    @classmethod
    def _enemy_set(cls, loss_killmail) -> set[int]:
        """The set of enemy entity ids = the association entity of each ATTACKER
        on the pilot's loss killmail (these are who killed the pilot)."""
        enemies: set[int] = set()
        if not isinstance(loss_killmail, dict):
            return enemies
        for atk in loss_killmail.get("attackers") or []:
            eid = cls._entity_of(atk)
            if eid:
                enemies.add(eid)
        return enemies

    @staticmethod
    def _hour_keys(kt) -> list[str]:
        """Return the related-battle hour keys to try for a loss time ``kt``.

        zKill's related endpoint only accepts an HOUR-ALIGNED timestamp; the
        loss minute (~never ``:00``) would 500. We floor to the hour and, for
        robustness near hour boundaries (a fight that straddles ``:59``/``:00``),
        ALSO offer the previous hour as a fallback. Each is ``YYYYMMDDHH00``.
        """
        floored = kt.astimezone(timezone.utc).replace(
            minute=0, second=0, microsecond=0)
        prev = floored - timedelta(hours=1)
        return [floored.strftime("%Y%m%d%H00"), prev.strftime("%Y%m%d%H00")]

    def _associate_from_battles(self, qualifying, own_corp_id, own_alliance_id,
                                emit):
        """Infer the pilot's blues from the battles around recent cyno losses.

        Returns ``(association | None, partial)``. ``association`` is None when
        the scan found no usable friendly entity (so the caller falls back to
        stats); ``partial`` is True if any related-battle fetch failed/errored
        (so the run is marked partial). Never raises.

        For each of the MAX_BATTLES newest losses (pilot = victim):
          * enemy set E = entities of the loss's attackers (who killed us), read
            from the ESI loss killmail (snake_case);
          * fetch the hour-floored battle (cached forever); if that hour yields no
            usable battle, also try the PREVIOUS hour. The related response
            carries every kill's victim + attackers INLINE (camelCase), so we scan
            ALL of a battle's kills in memory — no per-killmail ESI re-fetch.
            Score who fought ON OUR SIDE:
              - K.victim in E  -> K's (non-enemy) attackers are friendly;
              - K.attackers hit E (and K.victim not in E) -> K.victim is friendly.
          * skip NPCs, and skip the pilot's own corp/alliance.

        Counting is PER-BATTLE PRESENCE, not raw observations: each battle
        contributes a SET of friendly entities, and an entity's ``battle_count``
        increments by ONE per distinct battle it is present in. Raw observations
        (summed over kills) are kept as ``obs`` and used only as a tiebreak. This
        stops a single huge-fleet battle from out-tallying a group the pilot flies
        with consistently across MORE battles.

        The top friendly entity (most battles present, alliance-over-corp and raw
        obs as tiebreaks) is the association; its display name comes from the
        inline ``allianceName`` / ``corporationName``.
        """
        own_ids = {i for i in (own_corp_id, own_alliance_id) if i}
        # Distinct battles each friendly entity was PRESENT in (the primary rank).
        battle_count: dict[int, int] = {}
        # Raw friendly observations summed over kills across all battles (tiebreak).
        obs: dict[int, int] = {}
        # Track which friendly ids came in as an alliance vs only-corporation so
        # we can prefer alliances when picking the top (mirrors the stats path).
        kinds: dict[int, str] = {}
        # Collected inline display names: {entity_id: name}.
        names: dict[int, str] = {}
        partial = False
        battles_scanned = 0

        recent = [r for r in qualifying[:MAX_BATTLES]
                  if r.get("solar_system_id") and r.get("time")]
        n = len(recent)
        for idx, row in enumerate(recent, start=1):
            emit(f"Reading battle {idx}/{n} ...")
            kt = _parse_km_time(row.get("time"))
            if kt is None:
                continue
            system_id = row.get("solar_system_id")

            loss_km = self._get_killmail(row["killmail_id"], row.get("killmail_hash"))
            enemies = self._enemy_set(loss_km)
            if not enemies:
                continue  # solo gank / unknown attackers — nothing to anchor on

            # Try the floored hour first; if it has no usable battle, fall back to
            # the previous hour (boundary robustness). A None fetch -> partial.
            battle = None
            for ts_hour in self._hour_keys(kt):
                fetched = self._get_related(system_id, ts_hour)
                if fetched is None:
                    partial = True
                    continue
                if self.fetcher.zkill_blocked:
                    break
                if self._has_kills(fetched):
                    battle = fetched
                    break  # usable battle found — don't query the other hour
            if self.fetcher.zkill_blocked:
                partial = True
                break
            if battle is None:
                continue
            battles_scanned += 1
            # Per-battle fold: PRESENCE bumps battle_count once; obs accumulates.
            b_present, b_obs, b_kinds, b_names = self._score_battle(
                battle, enemies, own_ids)
            for eid in b_present:
                battle_count[eid] = battle_count.get(eid, 0) + 1
            for eid, c in b_obs.items():
                obs[eid] = obs.get(eid, 0) + c
            for eid, k in b_kinds.items():
                # Alliance evidence (from any battle) wins over a bare corp.
                if k == "alliance":
                    kinds[eid] = "alliance"
                else:
                    kinds.setdefault(eid, "corporation")
            for eid, nm in b_names.items():
                if eid not in names:
                    names[eid] = nm

        ranked = self._rank_entities(battle_count, obs, kinds)
        if not ranked:
            return None, partial

        top_eid = ranked[0]
        top_battle_count = battle_count.get(top_eid, 0)
        kind = "alliance" if kinds.get(top_eid) == "alliance" else "corporation"
        name = names.get(top_eid) or str(top_eid)
        # Confident when the top entity is present in a STRICT majority of the
        # scanned battles. False when no battles were scanned.
        confident = top_battle_count * 2 > battles_scanned
        runners_up = []
        for eid in ranked[1:3]:
            runners_up.append({
                "name": names.get(eid) or str(eid),
                "id": eid,
                "kind": "alliance" if kinds.get(eid) == "alliance" else "corporation",
                "battle_count": battle_count.get(eid, 0),
            })
        return {
            "name": name,
            "id": top_eid,
            "kind": kind,
            # count mirrors battle_count (per-battle presence of the top entity).
            "count": top_battle_count,
            "battle_count": top_battle_count,
            "sample_total": battles_scanned,
            "confident": confident,
            "runners_up": runners_up,
            "basis": "battles",
        }, partial

    @classmethod
    def _has_kills(cls, battle) -> bool:
        """True if a related team-summary response holds at least one kill."""
        for _ in cls._iter_battle_kills(battle):
            return True
        return False

    def _score_battle(self, battle, enemies, own_ids):
        """Reduce ONE battle's INLINE kills to its per-battle friendly tallies.

        ``battle`` is the zKill related team-summary dict. Every kill's victim and
        attackers (``involved`` minus the victim) are read inline in camelCase —
        no killmail fetch, no per-battle cap (one in-memory scan of all kills).

        Returns ``(present, obs, kinds, names)`` for THIS battle alone:
          * ``present``: set[int] — every friendly entity that the two-branch
            side-inference bumps at least once in this battle (the per-battle
            PRESENCE set);
          * ``obs``: dict[int, int] — raw friendly observation count per entity
            (sum over this battle's kills), used downstream as a TIEBREAK;
          * ``kinds``: dict[int, str] — "alliance" vs "corporation" per entity;
          * ``names``: dict[int, str] — inline ``allianceName`` /
            ``corporationName`` per entity, so the association can be labelled
            without an ESI /universe/names/ call.

        The own-corp/alliance + enemy exclusion and BOTH side-inference branches
        are unchanged from the previous in-place version — only the counting
        granularity (per-battle, folded by the caller) differs.
        """
        present: set[int] = set()
        obs: dict[int, int] = {}
        kinds: dict[int, str] = {}
        names: dict[int, str] = {}

        def _bump(eid, party):
            if not eid or eid in own_ids or eid in enemies:
                return
            present.add(eid)
            obs[eid] = obs.get(eid, 0) + 1
            # An alliance id (party carried allianceID) outranks a bare corp.
            if isinstance(party, dict) and party.get("allianceID") == eid:
                kinds[eid] = "alliance"
                nm = party.get("allianceName")
            else:
                kinds.setdefault(eid, "corporation")
                nm = party.get("corporationName") if isinstance(party, dict) else None
            if nm and eid not in names:
                names[eid] = nm

        for kill in self._iter_battle_kills(battle):
            victim = kill.get("victim") if isinstance(kill.get("victim"), dict) else {}
            victim_e = self._entity_of_inline(victim)
            # Attackers = every `involved` party that is not the victim.
            attackers = [p for p in (kill.get("involved") or [])
                         if isinstance(p, dict) and not p.get("isVictim")]
            attacker_es = {self._entity_of_inline(a) for a in attackers}
            attacker_es.discard(None)

            if victim_e in enemies:
                # K's attackers fought the enemy -> they're on our side.
                for atk in attackers:
                    _bump(self._entity_of_inline(atk), atk)
            elif (attacker_es & enemies) and victim_e not in enemies:
                # K's victim was killed BY the enemy -> the victim is on our side.
                _bump(victim_e, victim)

        return present, obs, kinds, names

    def _get_related(self, system_id, ts_hour):
        """Fetch a related battle with a forever-cache (immutable history).

        Cached per hour key so the floored hour and the previous-hour fallback
        are stored independently.
        """
        if self.cache is not None:
            cached = self.cache.get_related(system_id, ts_hour)
            if cached is not None:
                return cached
        battle = self.fetcher.zkill_related(system_id, ts_hour)
        if battle is not None and self.cache is not None:
            try:
                self.cache.put_related(system_id, ts_hour, battle)
            except Exception:
                pass
        return battle

    @staticmethod
    def _rank_entities(battle_count: dict[int, int], obs: dict[int, int],
                       kinds: dict[int, str]) -> list[int]:
        """Return friendly entity ids ranked best-first.

        Ranking key (each successive field is a tiebreak):
          1. ``battle_count`` DESC — present-as-friendly in the MOST distinct
             battles wins (the inflation fix: distinct-battle presence, not raw
             kill-observation tallies);
          2. is-alliance DESC — an alliance outranks a bare corp on a tie;
          3. ``obs`` DESC — raw friendly observations break a further tie;
          4. ``eid`` ASC — final deterministic tiebreak.

        Returns ``[]`` when there is no friendly entity.
        """
        eids = [e for e in battle_count if e]
        eids.sort(key=lambda e: (
            -battle_count.get(e, 0),
            0 if kinds.get(e) == "alliance" else 1,
            -obs.get(e, 0),
            e,
        ))
        return eids

    # -- association: stats aggregate (fallback) --------------------------
    def _compute_association(self, character_id, own_corp_id, own_alliance_id,
                             emit):
        """Propose the group the character most often flies with, from zKill's
        all-time stats aggregate. This is the FALLBACK signal (the battle-
        inferred path is primary). Degrades gracefully to {"kind": "unknown"}.

          1. ``own_corp_id``/``own_alliance_id`` (already fetched by the caller
             via public ESI affiliation) let us exclude self.
          2. Fetch zKill stats. Prefer ``topLists`` (per spec) but it is often
             empty; fall back to the populated ``topAllTime`` section.
          3. Pick the alliance with the most shared kills (excluding own
             alliance); fall back to the top corporation (excluding own corp).
          4. Resolve the chosen entity's name via public ESI /universe/names/.
        """
        stats = self.fetcher.zkill_stats(character_id)
        if not isinstance(stats, dict):
            return {"kind": "unknown"}

        alliance_counts = self._extract_counts(stats, "alliance", "allianceID")
        corp_counts = self._extract_counts(stats, "corporation", "corporationID")
        sample_total = self._sample_total(stats)

        # Prefer an alliance (excluding own), else a corporation (excluding own).
        chosen = self._pick_top(alliance_counts, exclude=own_alliance_id)
        kind = "alliance"
        if chosen is None:
            chosen = self._pick_top(corp_counts, exclude=own_corp_id)
            kind = "corporation"
        if chosen is None:
            return {"kind": "unknown"}

        entity_id, count = chosen
        name = self._resolve_name(entity_id)
        return {
            "name": name,
            "id": entity_id,
            "kind": kind,
            "count": count,
            # The stats path has no per-battle notion; expose the new fields
            # defensively so a GUI that reads them won't break. battle_count is
            # None (not a per-battle presence), the all-time aggregate is treated
            # as confident, and there are no battle runners-up.
            "battle_count": None,
            "sample_total": sample_total,
            "confident": True,
            "runners_up": [],
            "basis": "stats",
        }

    def _own_affiliation(self, character_id):
        """Return (own_corp_id, own_alliance_id), each int or None."""
        aff = self.fetcher.esi_affiliation(character_id)
        if not isinstance(aff, dict):
            return None, None
        return aff.get("corporation_id"), aff.get("alliance_id")

    @staticmethod
    def _extract_counts(stats: dict, list_type: str, id_field: str) -> dict[int, int]:
        """Build {entity_id: kills} for a given association type from zKill stats.

        Tries ``topLists`` first (array of {type, title, values}); if that
        element is missing or empty, falls back to ``topAllTime`` (array of
        {type, data}). Confirmed via live API: ``topAllTime`` data entries look
        like {"kills": N, "allianceID": X} or {"kills": N, "corporationID": X};
        ``topLists`` values (when present) carry id/kills under similar keys.
        """
        counts: dict[int, int] = {}

        def _accumulate(rows):
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                eid = row.get(id_field) or row.get("id")
                if not eid:
                    continue
                kills = row.get("kills")
                if not isinstance(kills, (int, float)):
                    kills = 1  # presence without an explicit count still ranks
                counts[int(eid)] = counts.get(int(eid), 0) + int(kills)

        # 1) topLists (preferred per spec; frequently empty in practice)
        top_lists = stats.get("topLists")
        if isinstance(top_lists, list):
            for element in top_lists:
                if not isinstance(element, dict):
                    continue
                if element.get("type") == list_type:
                    _accumulate(element.get("values"))

        if counts:
            return counts

        # 2) topAllTime fallback (the populated section on real responses)
        top_all = stats.get("topAllTime")
        if isinstance(top_all, list):
            for element in top_all:
                if not isinstance(element, dict):
                    continue
                if element.get("type") == list_type:
                    _accumulate(element.get("data"))
        return counts

    @staticmethod
    def _pick_top(counts: dict[int, int], exclude=None):
        """Return (entity_id, count) with the highest count, skipping `exclude`
        and any falsy id, or None if nothing qualifies."""
        best = None
        for eid, cnt in counts.items():
            if not eid or (exclude is not None and eid == exclude):
                continue
            if best is None or cnt > best[1]:
                best = (eid, cnt)
        return best

    @staticmethod
    def _sample_total(stats: dict):
        """A rough denominator for the association count, for display context.

        Uses zKill's total ships destroyed when available."""
        for key in ("shipsDestroyed", "allTimeSum"):
            v = stats.get(key)
            if isinstance(v, (int, float)):
                return int(v)
        return None

    def _resolve_name(self, entity_id):
        names = self.fetcher.esi_names([entity_id])
        for entry in names or []:
            if isinstance(entry, dict) and entry.get("id") == entity_id:
                nm = entry.get("name")
                if nm:
                    return nm
        return str(entity_id)

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _build_status(total: int, partial: bool, assoc_unknown: bool) -> str:
        parts = []
        if total == 0:
            parts.append("No cyno-ship losses in the past 6 months")
        else:
            parts.append(f"{total} cyno-ship loss(es) in the past 6 months")
        if partial:
            parts.append("partial results (some data unavailable)")
        if assoc_unknown:
            parts.append("association unknown")
        return "; ".join(parts)

    @staticmethod
    def _empty_result(status: str) -> dict:
        return {
            "total": 0,
            "breakdown": {},
            "latest": None,
            "association": {"kind": "unknown"},
            "losses": [],
            "status": status,
        }


def analyze_character(character_id: int, progress=None, **kwargs) -> dict:
    """Module-level convenience wrapper: build a default CynoChecker and run it.

    The GUI can call this directly; tests typically construct ``CynoChecker``
    with an injected fetcher instead.
    """
    return CynoChecker().analyze_character(character_id, progress=progress, **kwargs)
