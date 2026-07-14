"""
zKillboard Real-Time Monitor
Uses the R2Z2 polling API to stream kills in near-real-time.
Filters by region, alliance, system, and engagement size.

R2Z2 docs: https://github.com/zKillboard/zKillboard/wiki/API-(R2Z2)
"""

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

import requests
from rate_limiter import rate_limit

from app_log import get_logger
from esi_constants import ESI_BASE, ESI_HEADERS as HEADERS

log = get_logger(__name__)

R2Z2_BASE = "https://r2z2.zkillboard.com/ephemeral"

# Capital ship type IDs grouped by class for breakdown display
CAPITAL_CLASSES: dict[str, set[int]] = {
    "Dreads": {19720, 19722, 19724, 19726, 42241, 42243, 45647, 52907},
    "Carriers": {23757, 23911, 23915, 24483, 42245, 42246},
    "FAX": {37604, 37605, 37606, 37607},
    "Supers": {3514, 3628, 22852, 23913, 23917, 23919, 42125, 42126},
    "Titans": {671, 3764, 11567, 23773, 42242, 45649},
    "Rorquals": {28352},
}
# Flat set for quick membership checks
CAPITAL_TYPE_IDS: set[int] = set()
for _ids in CAPITAL_CLASSES.values():
    CAPITAL_TYPE_IDS |= _ids


def classify_capital(type_id: int) -> str | None:
    """Return the capital class name for a ship type ID, or None."""
    for cls_name, ids in CAPITAL_CLASSES.items():
        if type_id in ids:
            return cls_name
    return None

# Cache for ESI lookups.
# _name_cache is keyed by (category, entity_id) so different entity kinds that
# happen to share an id (e.g. a character and a system) never collide. It holds
# PERMANENT entries only: a resolved name, or a str(id) fallback for a
# definitive 404 (an identity that returned 404 will not start resolving later).
_name_cache: dict[tuple[str, int], str] = {}
# Negative cache for TRANSIENT failures (network/timeout errors and non-404
# HTTP failures such as an ESI 420 error-limit or a 5xx blip), keyed the same
# way. Maps key -> (fallback_str, expiry_monotonic); entries heal after the TTL
# so a temporary outage does not mute a name forever.
_name_neg_cache: dict[tuple[str, int], tuple[str, float]] = {}
_NAME_NEG_TTL = 300  # seconds a transient-failure fallback is trusted
# system_id -> region_id. The system->region mapping is static, so a successful
# hit is permanently valid; failures are never cached (they stay retryable).
_region_cache: dict[int, int] = {}


def resolve_name(entity_id: int, category: str = "solar_system") -> str:
    """Resolve an EVE entity ID to a name via ESI."""
    key = (category, entity_id)
    # Permanent cache (resolved name or definitive-404 fallback) wins outright.
    if key in _name_cache:
        return _name_cache[key]
    # Transient-failure fallback, honoured only until its TTL expires.
    neg = _name_neg_cache.get(key)
    if neg is not None:
        fallback, expiry = neg
        if time.monotonic() < expiry:
            return fallback
        # Expired — drop it and fall through to retry the HTTP request.
        del _name_neg_cache[key]
    try:
        endpoints = {
            "solar_system": f"{ESI_BASE}/universe/systems/{entity_id}/",
            "region": f"{ESI_BASE}/universe/regions/{entity_id}/",
            "alliance": f"{ESI_BASE}/alliances/{entity_id}/",
            "corporation": f"{ESI_BASE}/corporations/{entity_id}/",
            "character": f"{ESI_BASE}/characters/{entity_id}/",
            "type": f"{ESI_BASE}/universe/types/{entity_id}/",
        }
        url = endpoints.get(category, endpoints["solar_system"])
        rate_limit("esi")
        resp = requests.get(url, timeout=5, headers=HEADERS)
        if resp.ok:
            name = resp.json().get("name", str(entity_id))
            _name_cache[key] = name
            return name
        fallback = str(entity_id)
        if resp.status_code == 404:
            # Definitive 404: the id conclusively doesn't resolve (and won't
            # start to later), so cache the fallback PERMANENTLY — zero further
            # requests for this entity, and never a stale neg-cache entry.
            _name_cache[key] = fallback
        else:
            # Non-definitive HTTP failure (ESI 420 error-limit, 5xx blip):
            # trust the fallback only for the TTL so the name heals once the
            # server recovers instead of staying muted until restart.
            _name_neg_cache[key] = (fallback, time.monotonic() + _NAME_NEG_TTL)
        return fallback
    except Exception:
        # Network/timeout failure: cache the fallback with a TTL so it heals.
        fallback = str(entity_id)
        _name_neg_cache[key] = (fallback, time.monotonic() + _NAME_NEG_TTL)
        return fallback


def get_region_for_system(system_id: int) -> int | None:
    """Get the region ID for a solar system via ESI."""
    # The system->region mapping is static; a cached hit is permanently valid
    # and lets us skip both ESI round-trips entirely.
    cached = _region_cache.get(system_id)
    if cached is not None:
        return cached
    try:
        rate_limit("esi")
        resp = requests.get(
            f"{ESI_BASE}/universe/systems/{system_id}/",
            timeout=5, headers=HEADERS
        )
        if resp.ok:
            constellation_id = resp.json().get("constellation_id")
            if constellation_id:
                rate_limit("esi")
                resp2 = requests.get(
                    f"{ESI_BASE}/universe/constellations/{constellation_id}/",
                    timeout=5, headers=HEADERS
                )
                if resp2.ok:
                    region_id = resp2.json().get("region_id")
                    if region_id is not None:
                        # Cache ONLY a successful, non-None region id.
                        _region_cache[system_id] = region_id
                    return region_id
    except Exception:
        pass
    return None


@dataclass
class KillAlert:
    """A notable kill or engagement detected by the monitor."""
    system_id: int
    system_name: str
    region_id: int | None
    region_name: str
    kill_count: int
    total_value_millions: float
    alliances_involved: set[int]
    timestamp: datetime
    zkill_url: str
    pilots_on_field: int = 0
    capitals_involved: bool = False
    capital_breakdown: dict[str, int] | None = None  # e.g. {"Dreads": 3, "Titans": 1}
    dotlan_url: str = ""
    zkill_related_url: str = ""  # zKillboard related kills page
    warbeacon_url: str = ""      # WarBeacon battle report
    top_alliances: list[tuple[str, int]] | None = None  # Top 3 alliances by pilot count [(name, count)]
    route_from_staging: str = ""
    is_update: bool = False  # True if this is an update to an existing fight
    corps_involved: set[int] = field(default_factory=set)  # Corporation IDs in the fight


class EngagementTracker:
    """
    Tracks kills in a sliding window to detect engagements.
    Groups kills by system and checks if they meet alert thresholds.
    """

    def __init__(self, window_seconds: int = 300, min_pilots: int = 10,
                 friendly_ids: set[int] | None = None):
        self.window = timedelta(seconds=window_seconds)
        self.min_pilots = min_pilots
        # Corp/alliance IDs considered "friendly" (blue/own) for capital filtering.
        # Supplied by the GUI from standings; hostile = not in this set.
        self.friendly_ids = set(friendly_ids or [])
        # system_id -> list of (timestamp, kill_data)
        self._kills: dict[int, list[tuple[datetime, dict]]] = {}

    def set_friendly_ids(self, ids):
        """Replace the friendly corp/alliance id set (e.g. on standings refresh)."""
        self.friendly_ids = set(ids or [])

    def add_kill(self, kill_data: dict) -> KillAlert | None:
        """Add a kill and check if it triggers an engagement alert."""
        km = kill_data.get("killmail", kill_data)
        zkb = kill_data.get("zkb", {})

        system_id = km.get("solar_system_id", 0)
        if not system_id:
            return None

        # Cheap, ESI-free staleness gate FIRST — mirrors the 30-minute cutoff in
        # ZKillMonitor._matches_filters. A stale kill does zero aggregation and
        # zero name/region resolution (the only calls that touch ESI). This is
        # behaviour-preserving in production because _process_kill already ran
        # _matches_filters (which rejects >30-minute-old kills) before reaching
        # here. If killmail_time is absent or unparseable we treat the kill as
        # NOT stale and proceed (test killmails carry no killmail_time).
        kill_time_str = km.get("killmail_time", "")
        if kill_time_str:
            try:
                kill_time = datetime.fromisoformat(
                    kill_time_str.replace("Z", "+00:00"))
                if datetime.now(timezone.utc) - kill_time > timedelta(minutes=30):
                    return None
            except Exception:
                pass  # Unparseable time -> treat as fresh, let it through.

        now = datetime.now(timezone.utc)

        if system_id not in self._kills:
            self._kills[system_id] = []

        self._kills[system_id].append((now, kill_data))

        # Prune old kills outside window
        cutoff = now - self.window
        self._kills[system_id] = [
            (ts, kd) for ts, kd in self._kills[system_id] if ts > cutoff
        ]

        # Count unique pilots involved across all kills in this system
        pilots = set()
        alliances = set()
        corps = set()
        alliance_pilots: dict[int, set[int]] = {}  # alliance_id -> set of character_ids
        total_value = 0.0
        total_caps = 0
        friendly_caps = 0  # Caps whose corp/alliance is in the friendly (blue/own) set
        cap_counts: dict[str, int] = {}  # class_name -> HOSTILE-only count
        # Track capital char IDs per class to avoid double-counting
        cap_chars: dict[str, set[int]] = {}
        # Snapshot the current friendly set so the closure reads it consistently.
        friendly_ids = self.friendly_ids

        def _count_capital(ship_type_id: int, char_id: int, corp_id: int, alliance_id: int):
            nonlocal total_caps, friendly_caps
            cls = classify_capital(ship_type_id)
            if not cls:
                return
            cap_chars.setdefault(cls, set())
            if char_id and char_id in cap_chars[cls]:
                return  # Already counted
            if char_id:
                cap_chars[cls].add(char_id)
            total_caps += 1
            # Friendly if EITHER its corp or its alliance is in the friendly set.
            friendly = (corp_id and corp_id in friendly_ids) or \
                       (alliance_id and alliance_id in friendly_ids)
            if friendly:
                friendly_caps += 1
            else:
                # Hostile-only breakdown: count toward the threat tally only.
                cap_counts[cls] = cap_counts.get(cls, 0) + 1

        for _, kd in self._kills[system_id]:
            inner_km = kd.get("killmail", kd)
            inner_zkb = kd.get("zkb", {})
            # Victim
            victim = inner_km.get("victim", {})
            if victim.get("character_id"):
                pilots.add(victim["character_id"])
                if victim.get("alliance_id"):
                    alliance_pilots.setdefault(victim["alliance_id"], set()).add(victim["character_id"])
            if victim.get("alliance_id"):
                alliances.add(victim["alliance_id"])
            if victim.get("corporation_id"):
                corps.add(victim["corporation_id"])
            _count_capital(
                victim.get("ship_type_id", 0),
                victim.get("character_id", 0),
                victim.get("corporation_id", 0),
                victim.get("alliance_id", 0),
            )
            # Attackers
            for attacker in inner_km.get("attackers", []):
                if attacker.get("character_id"):
                    pilots.add(attacker["character_id"])
                    if attacker.get("alliance_id"):
                        alliance_pilots.setdefault(attacker["alliance_id"], set()).add(attacker["character_id"])
                if attacker.get("alliance_id"):
                    alliances.add(attacker["alliance_id"])
                if attacker.get("corporation_id"):
                    corps.add(attacker["corporation_id"])
                _count_capital(
                    attacker.get("ship_type_id", 0),
                    attacker.get("character_id", 0),
                    attacker.get("corporation_id", 0),
                    attacker.get("alliance_id", 0),
                )
            total_value += inner_zkb.get("totalValue", 0) / 1_000_000

        # Capital fight = at least one HOSTILE capital present.
        # Blue/own caps never self-trigger a capital alert.
        non_friendly_caps = total_caps - friendly_caps
        has_capitals = non_friendly_caps > 0

        # Alert if enough pilots, OR if capitals are involved
        if len(pilots) >= self.min_pilots or has_capitals:
            system_name = resolve_name(system_id, "solar_system")
            region_id = get_region_for_system(system_id)
            region_name = resolve_name(region_id, "region") if region_id else "Unknown"

            # Build dotlan URL (uses system name with spaces replaced)
            dotlan_system = system_name.replace(" ", "_")
            dotlan_url = f"https://evemaps.dotlan.net/system/{dotlan_system}"

            # Build battle report URLs (zKillboard related + WarBeacon)
            br_ts = now.strftime("%Y%m%d%H%M")
            zkill_related_url = f"https://zkillboard.com/related/{system_id}/{br_ts}/"
            warbeacon_url = f"https://warbeacon.net/br/related/{system_id}/{br_ts}/"

            # Resolve top 3 alliances by pilot count
            top_3_ids = sorted(alliance_pilots.items(),
                               key=lambda x: len(x[1]), reverse=True)[:3]
            top_alliances = [
                (resolve_name(aid, "alliance"), len(chars))
                for aid, chars in top_3_ids
            ]

            return KillAlert(
                system_id=system_id,
                system_name=system_name,
                region_id=region_id,
                region_name=region_name,
                kill_count=len(self._kills[system_id]),
                total_value_millions=round(total_value, 1),
                alliances_involved=alliances,
                corps_involved=corps,
                timestamp=now,
                zkill_url=f"https://zkillboard.com/system/{system_id}/",
                pilots_on_field=len(pilots),
                capitals_involved=has_capitals,
                capital_breakdown=cap_counts if cap_counts else None,
                dotlan_url=dotlan_url,
                zkill_related_url=zkill_related_url,
                warbeacon_url=warbeacon_url,
                top_alliances=top_alliances if top_alliances else None,
            )
        return None


# How long _fetch_kill waits before its single retry on a transient network
# failure (requests.RequestException — e.g. an SSLError CDN blip). Module-level
# so tests can patch zkill_monitor.time.sleep instead of waiting for real.
_RETRY_DELAY = 2.0


class ZKillMonitor:
    """
    Real-time zKillboard monitor using the R2Z2 polling API.
    Polls for new killmails and detects engagements matching configured criteria.
    """

    def __init__(self, watch_regions: list[int] = None,
                 watch_alliances: list[int] = None,
                 watch_systems: list[int] = None,
                 min_kill_value_millions: float = 0,
                 min_pilots_involved: int = 10,
                 alert_window_seconds: int = 300,
                 on_alert: Callable[[KillAlert], None] | None = None,
                 watch_all: bool = False,
                 friendly_ids: set[int] | None = None):
        self.watch_regions = set(watch_regions or [])
        self.watch_alliances = set(watch_alliances or [])
        self.watch_systems = set(watch_systems or [])
        self.watch_all = watch_all
        self.min_kill_value = min_kill_value_millions
        self.min_pilots = min_pilots_involved
        self.on_alert = on_alert
        # Friendly (blue/own) corp/alliance ids for capital friend/foe filtering.
        self._friendly_ids = set(friendly_ids or [])
        self._tracker = EngagementTracker(alert_window_seconds, min_pilots_involved,
                                          friendly_ids=self._friendly_ids)
        self._thread: threading.Thread | None = None
        self._running = False
        self._sequence: int = 0
        self._kills_processed: int = 0
        # Track which system alerts we've already fired (to avoid spam)
        # Maps system_id -> {"time": datetime, "pilots": int}
        self._alerted_systems: dict[int, dict] = {}
        self._pilot_growth_threshold = 100  # Re-ping Discord if fight grows by this many
        # How long a fired alert keeps muting the same system. A later fight past
        # this horizon counts as new and re-alerts. Scales with a custom window
        # (~3x the engagement window, i.e. ~900s by default).
        self._alert_expiry_seconds = alert_window_seconds * 3

    def set_friendly_ids(self, ids):
        """Update the friendly (blue/own) corp/alliance id set without rebuilding
        the monitor. Forwards to the live tracker so standings refreshes take
        effect immediately."""
        self._friendly_ids = set(ids or [])
        self._tracker.set_friendly_ids(self._friendly_ids)

    def _matches_filters(self, kill_data: dict) -> bool:
        """Check if a kill matches any of our watch filters."""
        km = kill_data.get("killmail", kill_data)

        # Reject stale kills (older than 30 minutes — allows catchup after restarts)
        kill_time_str = km.get("killmail_time", "")
        if kill_time_str:
            try:
                kill_time = datetime.fromisoformat(kill_time_str.replace("Z", "+00:00"))
                age = datetime.now(timezone.utc) - kill_time
                if age > timedelta(minutes=30):
                    return False
            except Exception:
                pass  # If we can't parse the time, let it through

        # Watch-all mode: accept every kill in K-space
        if self.watch_all:
            sid = km.get("solar_system_id", 0)
            return 30000000 <= sid <= 30999999

        # If no filters set, match everything
        if not self.watch_regions and not self.watch_alliances and not self.watch_systems:
            return True

        system_id = km.get("solar_system_id", 0)

        # System filter — always passes independently
        if self.watch_systems and system_id in self.watch_systems:
            return True

        # When both alliances AND regions are set, require BOTH to match
        # (only flag watched alliance kills that happen in watched regions)
        if self.watch_alliances and self.watch_regions:
            alliance_match = False
            victim = km.get("victim", {})
            if victim.get("alliance_id") in self.watch_alliances:
                alliance_match = True
            if not alliance_match:
                for attacker in km.get("attackers", []):
                    if attacker.get("alliance_id") in self.watch_alliances:
                        alliance_match = True
                        break
            if not alliance_match:
                return False
            # Alliance matched — now check region
            if system_id:
                region_id = get_region_for_system(system_id)
                return region_id in self.watch_regions
            return False

        # Only alliances set (no regions) — match any watched alliance
        if self.watch_alliances:
            victim = km.get("victim", {})
            if victim.get("alliance_id") in self.watch_alliances:
                return True
            for attacker in km.get("attackers", []):
                if attacker.get("alliance_id") in self.watch_alliances:
                    return True

        # Only regions set (no alliances) — match any watched region
        if self.watch_regions and system_id:
            region_id = get_region_for_system(system_id)
            if region_id in self.watch_regions:
                return True

        return False

    def _normalize_kill(self, raw: dict) -> dict:
        """
        Normalize R2Z2 format to the internal format used by filters/tracker.
        R2Z2 puts killmail data under 'esi', old WebSocket used 'killmail'.
        """
        if "esi" in raw and "killmail" not in raw:
            return {"killmail": raw["esi"], "zkb": raw.get("zkb", {})}
        return raw

    def _process_kill(self, kill_data: dict):
        """Process a single killmail through filters and engagement tracking."""
        kill_data = self._normalize_kill(kill_data)
        if not self._matches_filters(kill_data):
            return

        alert = self._tracker.add_kill(kill_data)
        if alert and self.on_alert:
            # Opportunistically prune stale entries (no background timer). Use
            # the alert timestamp as the clock so expiry is deterministic and
            # test-drivable. Anything older than the expiry horizon is a fight
            # that has since ended and should no longer mute its system.
            expiry = timedelta(seconds=self._alert_expiry_seconds)
            self._alerted_systems = {
                sid: rec for sid, rec in self._alerted_systems.items()
                if alert.timestamp - rec["time"] <= expiry
            }

            prev = self._alerted_systems.get(alert.system_id)
            # An expired entry was already dropped above, so a surviving `prev`
            # is a genuinely active fight; its absence means "new fight".
            if not prev:
                # New (or newly re-armed) fight — first alert.
                self._alerted_systems[alert.system_id] = {
                    "time": alert.timestamp,
                    "pilots": alert.pilots_on_field,
                }
                self.on_alert(alert)
            else:
                # Existing fight — send update if pilots grew by 100+
                growth = alert.pilots_on_field - prev["pilots"]
                if growth >= self._pilot_growth_threshold:
                    alert.is_update = True
                    self._alerted_systems[alert.system_id] = {
                        "time": alert.timestamp,
                        "pilots": alert.pilots_on_field,
                    }
                    self.on_alert(alert)

    def _get_current_sequence(self) -> int | None:
        """Fetch the current sequence number from R2Z2."""
        try:
            resp = requests.get(
                f"{R2Z2_BASE}/sequence.json",
                headers=HEADERS, timeout=10
            )
            if resp.ok:
                return resp.json().get("sequence", None)
        except Exception as e:
            print(f"[zKill] Error fetching sequence: {e}")
        return None

    def _fetch_kill(self, sequence_id: int) -> dict | None:
        """Fetch a single killmail by sequence ID from R2Z2.

        Returns the kill dict on success, or None for the legitimate idle case
        (a 404 means "no kill at this sequence yet"). Any other outcome — a
        non-404 HTTP error or a request exception — signals an outage rather
        than idleness, so it is logged (with context) while still returning
        None to preserve the caller's polling behavior.

        A requests.RequestException (SSLError/ConnectionError/Timeout/etc. —
        e.g. the SSL "UNEXPECTED_EOF_WHILE_READING" blips seen from
        r2z2.zkillboard.com) is treated as a routine, self-healing CDN hiccup:
        it gets ONE retry after a short backoff before giving up. A failure
        that survives the retry logs a single WARNING line (seq id + exception
        class + reason, no traceback) instead of ERROR, so routine network
        noise no longer buries real errors; the full traceback is still
        available at DEBUG via exc_info. Any other exception (an unexpected
        programming error, e.g. AttributeError/TypeError) is not a network
        blip, so it keeps the original ERROR + traceback behavior with no
        retry.
        """
        for attempt in range(2):
            try:
                resp = requests.get(
                    f"{R2Z2_BASE}/{sequence_id}.json",
                    headers=HEADERS, timeout=10
                )
                if resp.ok:
                    return resp.json()
                elif resp.status_code == 404:
                    return None  # No kill at this sequence yet (idle, not an outage)
                # Any other status is an unexpected R2Z2 response (possible outage).
                log.warning(
                    "R2Z2 _fetch_kill seq=%s returned HTTP %s (not OK / not 404)",
                    sequence_id, resp.status_code,
                )
                return None
            except requests.RequestException as e:
                if attempt == 0:
                    # First failure: almost always a transient CDN blip — wait
                    # briefly and retry once before treating it as an outage.
                    time.sleep(_RETRY_DELAY)
                    continue
                # Retry also failed. Routine + self-healing -> WARNING, not
                # ERROR; the traceback is demoted to DEBUG instead of dropped.
                log.warning(
                    "R2Z2 _fetch_kill seq=%s skipped after retry: %s: %s",
                    sequence_id, type(e).__name__, e,
                )
                log.debug(
                    "R2Z2 _fetch_kill seq=%s retry traceback", sequence_id,
                    exc_info=True,
                )
                return None
            except Exception:
                # Unexpected programming error (e.g. AttributeError/TypeError)
                # — not a routine blip, so no retry; keep full ERROR + traceback.
                log.exception("R2Z2 _fetch_kill seq=%s failed (request error)", sequence_id)
                return None
        return None  # Unreachable: every branch above returns.

    def _poll_loop(self):
        """Main polling loop using R2Z2 API."""
        print("[zKill] Starting R2Z2 poll loop...")

        # Get current sequence to start from. Retry iteratively (not by
        # recursing into _poll_loop) so a prolonged outage cannot grow the
        # stack until it hits Python's recursion limit and silently kills the
        # daemon thread.
        seq = None
        while self._running:
            seq = self._get_current_sequence()
            if seq is not None:
                break
            print("[zKill] ERROR: Could not fetch initial sequence. Retrying in 10s...")
            time.sleep(10)
        if seq is None:
            # Only reached when we were asked to stop before acquiring a sequence.
            return

        # Look back to catch recent kills we may have missed (e.g. during restart)
        # At ~6 kills/min global, 200 sequences ≈ ~30 minutes of catchup
        lookback = 200
        self._sequence = max(1, seq - lookback)
        print(f"[zKill] Connected to R2Z2. Head={seq}, starting at {self._sequence} "
              f"(lookback {lookback})")

        consecutive_404s = 0
        catching_up = True

        while self._running:
            try:
                kill_data = self._fetch_kill(self._sequence)

                if kill_data is None:
                    if catching_up:
                        # During catchup, skip missing sequences quickly
                        self._sequence += 1
                        if self._sequence >= seq:
                            catching_up = False
                            print(f"[zKill] Catchup complete at seq {self._sequence}")
                        continue
                    # No new kill at this sequence — wait before retrying
                    consecutive_404s += 1
                    # R2Z2 docs say minimum 6 seconds on 404
                    wait = min(6 + consecutive_404s, 15)  # Cap at 15s
                    time.sleep(wait)
                    continue

                # Got a kill — process it
                consecutive_404s = 0
                self._kills_processed += 1
                self._sequence += 1

                if catching_up and self._sequence >= seq:
                    catching_up = False
                    print(f"[zKill] Catchup complete at seq {self._sequence}")

                # Wrap in the format our tracker expects
                # R2Z2 returns the kill directly with killmail + zkb fields
                self._process_kill(kill_data)

                # During catchup, poll faster; normal mode: 0.1s between fetches
                if catching_up:
                    time.sleep(0.05)
                else:
                    time.sleep(0.1)

            except Exception as e:
                print(f"[zKill] Poll error: {e}")
                time.sleep(5)

        print("[zKill] Poll loop stopped.")

    def start(self):
        """Start the zKillboard monitor in a background thread."""
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
