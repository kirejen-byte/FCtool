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

ESI_BASE = "https://esi.evetech.net/latest"
R2Z2_BASE = "https://r2z2.zkillboard.com/ephemeral"
HEADERS = {"User-Agent": "FCTool/1.0 (EVE FC Assistant)"}

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

# Cache for ESI lookups
_name_cache: dict[int, str] = {}


def resolve_name(entity_id: int, category: str = "solar_system") -> str:
    """Resolve an EVE entity ID to a name via ESI."""
    if entity_id in _name_cache:
        return _name_cache[entity_id]
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
            _name_cache[entity_id] = name
            return name
    except Exception:
        pass
    return str(entity_id)


def get_region_for_system(system_id: int) -> int | None:
    """Get the region ID for a solar system via ESI."""
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
                    return resp2.json().get("region_id")
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
        # Status callback for GUI
        self.on_status: Callable[[str], None] | None = None

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
            prev = self._alerted_systems.get(alert.system_id)
            if not prev:
                # New fight — first alert
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
        """Fetch a single killmail by sequence ID from R2Z2."""
        try:
            resp = requests.get(
                f"{R2Z2_BASE}/{sequence_id}.json",
                headers=HEADERS, timeout=10
            )
            if resp.ok:
                return resp.json()
            elif resp.status_code == 404:
                return None  # No kill at this sequence yet
        except Exception:
            pass
        return None

    def _poll_loop(self):
        """Main polling loop using R2Z2 API."""
        print("[zKill] Starting R2Z2 poll loop...")

        # Get current sequence to start from
        seq = self._get_current_sequence()
        if seq is None:
            print("[zKill] ERROR: Could not fetch initial sequence. Retrying in 10s...")
            time.sleep(10)
            if self._running:
                self._poll_loop()
            return

        # Look back to catch recent kills we may have missed (e.g. during restart)
        # At ~6 kills/min global, 200 sequences ≈ ~30 minutes of catchup
        lookback = 200
        self._sequence = max(1, seq - lookback)
        print(f"[zKill] Connected to R2Z2. Head={seq}, starting at {self._sequence} "
              f"(lookback {lookback})")
        if self.on_status:
            self.on_status(f"R2Z2 connected (seq {seq})")

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

    @property
    def kills_processed(self) -> int:
        return self._kills_processed

    @property
    def current_sequence(self) -> int:
        return self._sequence
