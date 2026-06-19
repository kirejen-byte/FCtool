"""
Fleet Loss Tracker
Detects likely fleet member deaths by observing ship->capsule transitions
across ESI fleet polls. Fires threshold notifications at 10%, 25%, 50% loss.
"""

from dataclasses import dataclass
from datetime import datetime

# EVE capsule ship type IDs
CAPSULE_TYPE_IDS = {
    670,    # Capsule
    33328,  # Capsule - Genolution 'Auroral' 197-variant
}

# Loss thresholds (as fractions of initial fleet size)
LOSS_THRESHOLDS = (0.10, 0.25, 0.50)


@dataclass
class DeathEvent:
    """A detected fleet member death."""
    character_id: int
    character_name: str
    ship_type_id: int
    ship_name: str
    system_id: int
    system_name: str
    timestamp: datetime


@dataclass
class _MemberState:
    """Last known state of a fleet member."""
    ship_type_id: int
    system_id: int
    is_docked: bool
    is_tackle: bool = False  # Was previously in a tackle-class ship
    # Snapshot for death events
    ship_name: str = ""
    character_name: str = ""
    system_name: str = ""
    # Pending death: snapshot of the last in-space ship when we first saw
    # this pilot in a pod, used to require two consecutive in-pod
    # observations before counting a death (avoids single-poll re-ship
    # false positives where a dock -> undock-in-pod was missed by sparse
    # ESI polling). None means "no unconfirmed pod transition".
    pending_death: "_MemberState | None" = None


class FleetLossTracker:
    """Tracks fleet member states to detect ship->capsule transitions.

    Baseline fleet size dynamically grows to the max count of non-capsule
    members in the FC's solar system, so reinforcements prevent false alarms.
    """

    def __init__(self):
        self._states: dict[int, _MemberState] = {}
        self._deaths: list[DeathEvent] = []          # ALL deaths (major + tackle)
        self._peak_major_in_system: int = 0          # Peak non-capsule, non-tackle in FC system
        self._peak_tackle_in_system: int = 0         # Peak tackle in FC system
        self._baseline_size: int = 0                 # Effective baseline for current mode
        # One-way latch: once fleet is observed as majority-major in any single
        # poll, mode locks to "main" permanently (until fleet reset).
        self._main_fleet_latched: bool = False
        self._thresholds_fired: set[float] = set()
        self._current_fleet_id: int | None = None

    def reset(self, new_fleet_id: int | None = None):
        """Reset tracker for a new fleet."""
        self._states.clear()
        self._deaths.clear()
        self._peak_major_in_system = 0
        self._peak_tackle_in_system = 0
        self._baseline_size = 0
        self._main_fleet_latched = False
        self._thresholds_fired.clear()
        self._current_fleet_id = new_fleet_id

    def set_fleet_id(self, fleet_id: int | None):
        """Check if fleet changed and reset if so."""
        if fleet_id != self._current_fleet_id:
            self.reset(fleet_id)

    def update(self, members: list[dict]) -> tuple[list[DeathEvent], float | None, bool]:
        """
        Observe the current fleet roster.

        Each member dict should have keys:
            character_id, character_name (optional), ship_type_id, ship_name (optional),
            solar_system_id, system_name (optional), station_id (optional),
            structure_id (optional), role (optional: "fleet_commander", etc.),
            is_tackle (bool, REQUIRED — pre-computed in caller to avoid ESI calls here)

        Returns:
            (new_deaths, highest_threshold_crossed_or_None, fc_docked)
            new_deaths includes ALL detected deaths (both tackle and major);
            the caller can differentiate for display. Threshold crossing uses
            only mode-relevant deaths.
        """
        new_deaths: list[DeathEvent] = []

        if not members:
            return new_deaths, None, False

        # Find the FC's solar system and docked status
        fc_system_id = None
        fc_docked = False
        for m in members:
            if m.get("role") == "fleet_commander":
                fc_system_id = m.get("solar_system_id")
                fc_docked = bool(m.get("station_id")) or bool(m.get("structure_id"))
                break
        if not fc_system_id:
            sys_counts: dict[int, int] = {}
            for m in members:
                sid = m.get("solar_system_id", 0)
                if sid:
                    sys_counts[sid] = sys_counts.get(sid, 0) + 1
            if sys_counts:
                fc_system_id = max(sys_counts.items(), key=lambda kv: kv[1])[0]

        # Count major vs tackle non-capsule pilots in FC's system
        current_major = 0
        current_tackle = 0
        if fc_system_id:
            for m in members:
                if m.get("solar_system_id") != fc_system_id:
                    continue
                if m.get("ship_type_id", 0) in CAPSULE_TYPE_IDS:
                    continue
                if m.get("is_tackle", False):
                    current_tackle += 1
                else:
                    current_major += 1

        # Peaks only grow
        self._peak_major_in_system = max(self._peak_major_in_system, current_major)
        self._peak_tackle_in_system = max(self._peak_tackle_in_system, current_tackle)

        # One-way latch: if the fleet is ever observed majority-major in a
        # single observation, switch to main mode permanently. Tie favors main
        # (1 tackle + 1 major = main) since any major presence is significant.
        if current_major > 0 and current_major >= current_tackle:
            self._main_fleet_latched = True

        # Compute effective baseline based on mode
        # Clamp by current fleet membership so baseline shrinks when pilots leave.
        if self._main_fleet_latched:
            # Count current major-ship members (including docked ones in fleet)
            current_major_members = sum(
                1 for m in members if not m.get("is_tackle", False)
                and m.get("ship_type_id", 0) not in CAPSULE_TYPE_IDS
            )
            # Also include podded majors (they might be members en route to clone)
            # — but for simplicity, use len(members-tackle) bounded by peak
            self._baseline_size = min(
                self._peak_major_in_system,
                max(current_major_members, 1),
            )
        else:
            # Tackle mode: baseline = all non-pods, clamped by current fleet size
            peak_total = self._peak_major_in_system + self._peak_tackle_in_system
            self._baseline_size = min(peak_total, len(members))

        # Detect deaths
        for m in members:
            char_id = m.get("character_id")
            if not char_id:
                continue

            ship_id = m.get("ship_type_id", 0)
            sys_id = m.get("solar_system_id", 0)
            is_docked = bool(m.get("station_id")) or bool(m.get("structure_id"))
            is_tackle_now = bool(m.get("is_tackle", False))

            prev = self._states.get(char_id)

            now_in_pod = ship_id in CAPSULE_TYPE_IDS
            pending_next: _MemberState | None = None

            if prev is not None:
                was_in_ship = (
                    prev.ship_type_id not in CAPSULE_TYPE_IDS
                    and not prev.is_docked
                )
                # Confirm a pending death: pilot was in-space in a ship,
                # then seen in pod, and is STILL in pod (same system, not
                # docked). Two consecutive in-pod observations => real loss.
                if (
                    prev.pending_death is not None
                    and now_in_pod
                    and not is_docked
                    and sys_id == prev.system_id
                ):
                    snap = prev.pending_death
                    death = DeathEvent(
                        character_id=char_id,
                        character_name=m.get("character_name", snap.character_name),
                        ship_type_id=snap.ship_type_id,
                        ship_name=snap.ship_name,
                        system_id=snap.system_id,
                        system_name=snap.system_name,
                        timestamp=datetime.now(),
                    )
                    setattr(death, "_was_tackle", snap.is_tackle)
                    new_deaths.append(death)
                    self._deaths.append(death)
                    # pending_next stays None -> cleared after confirmation
                elif was_in_ship and now_in_pod:
                    # First pod observation after being in-space in a ship.
                    # Require a second confirming observation before counting.
                    # Suppress entirely if system changed (jump-clone): the
                    # pilot can't have been podded in a different system
                    # than where we last saw their ship.
                    if sys_id == prev.system_id:
                        pending_next = prev  # snapshot of last in-space ship
                    # else: jump-clone => silently drop, no pending
                elif prev.pending_death is not None:
                    # Pending exists but new state is docked or back in a
                    # real ship => it was a re-ship, not a death. Drop.
                    pending_next = None

            self._states[char_id] = _MemberState(
                ship_type_id=ship_id,
                system_id=sys_id,
                is_docked=is_docked,
                is_tackle=is_tackle_now,
                ship_name=m.get("ship_name", ""),
                character_name=m.get("character_name", ""),
                system_name=m.get("system_name", ""),
                pending_death=pending_next,
            )

        # Count "relevant" deaths for the current mode
        #  - Main mode: only deaths of non-tackle ships count
        #  - Tackle mode: all deaths count
        if self._main_fleet_latched:
            relevant_deaths = sum(
                1 for d in self._deaths if not getattr(d, "_was_tackle", False)
            )
        else:
            relevant_deaths = len(self._deaths)

        # Check thresholds
        highest_crossed: float | None = None
        if self._baseline_size > 0:
            loss_ratio = relevant_deaths / self._baseline_size
            for threshold in LOSS_THRESHOLDS:
                if loss_ratio >= threshold and threshold not in self._thresholds_fired:
                    self._thresholds_fired.add(threshold)
                    if highest_crossed is None or threshold > highest_crossed:
                        highest_crossed = threshold

        return new_deaths, highest_crossed, fc_docked

    @property
    def deaths_count(self) -> int:
        """Total deaths (major + tackle)."""
        return len(self._deaths)

    @property
    def relevant_deaths_count(self) -> int:
        """Deaths that count toward thresholds in the current mode."""
        if self._main_fleet_latched:
            return sum(1 for d in self._deaths if not getattr(d, "_was_tackle", False))
        return len(self._deaths)

    @property
    def initial_size(self) -> int:
        """Baseline fleet size for the current mode."""
        return self._baseline_size

    @property
    def loss_percentage(self) -> float:
        if self._baseline_size == 0:
            return 0.0
        return 100.0 * self.relevant_deaths_count / self._baseline_size

    @property
    def mode(self) -> str:
        """Either 'main' or 'tackle'. One-way latch — once main, stays main."""
        return "main" if self._main_fleet_latched else "tackle"

    @property
    def deaths(self) -> list[DeathEvent]:
        return list(self._deaths)
