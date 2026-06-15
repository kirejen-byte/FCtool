"""
Tests for zkill_monitor.EngagementTracker corporation-id aggregation.

These exercise the additive `KillAlert.corps_involved` set: every fight should
carry the union of involved corporation IDs (victim + attackers), exactly
parallel to the existing `alliances_involved` set, while alliance behavior
stays correct.

All tests are network-free: `resolve_name` and `get_region_for_system` (the
only functions in the alert-build path that touch ESI) are stubbed, and we also
assert `requests.get` / `requests.post` are never called. Killmail dicts are
constructed directly in the internal normalized format
``{"killmail": {...}, "zkb": {...}}``.
"""

import pytest

import zkill_monitor
from zkill_monitor import EngagementTracker, KillAlert


@pytest.fixture(autouse=True)
def _no_network(mocker):
    """Stub the two ESI-touching helpers on the alert path and guard the wire.

    `resolve_name`/`get_region_for_system` swallow exceptions internally, but we
    stub them so a triggered alert never attempts a real HTTP request and the
    returned names/region are deterministic. We also patch `requests.get`/
    `requests.post` so any accidental network call fails the test loudly.
    """
    mocker.patch.object(zkill_monitor, "resolve_name",
                        side_effect=lambda eid, category="solar_system": f"Name-{eid}")
    mocker.patch.object(zkill_monitor, "get_region_for_system", return_value=10000002)
    get_patch = mocker.patch("requests.get")
    post_patch = mocker.patch("requests.post")
    return get_patch, post_patch


def _killmail(system_id, victim, attackers, total_value=0):
    """Build one normalized killmail dict the tracker understands.

    `victim`/`attackers` are partial dicts; only the keys the caller sets are
    present, mirroring sparse real killmails (e.g. NPC attackers with no
    alliance/corp, or structures with no character).
    """
    km = {"solar_system_id": system_id, "victim": victim, "attackers": attackers}
    return {"killmail": km, "zkb": {"totalValue": total_value}}


def _make_tracker(min_pilots=1, window_seconds=300):
    # min_pilots=1 makes any kill with >=1 pilot trip the alert threshold,
    # so tests can avoid the capital-classification path entirely.
    return EngagementTracker(window_seconds=window_seconds, min_pilots=min_pilots)


# ── corps_involved population ───────────────────────────────────────────────

def test_victim_corp_only():
    """A single kill yields a fight whose corp set includes the victim corp."""
    tracker = _make_tracker(min_pilots=1)
    alert = tracker.add_kill(_killmail(
        30000142,
        victim={"character_id": 1, "corporation_id": 1000, "alliance_id": 99000},
        attackers=[],
    ))
    assert isinstance(alert, KillAlert)
    assert alert.corps_involved == {1000}
    assert alert.alliances_involved == {99000}


def test_multiple_attacker_corps():
    """Victim corp + every distinct attacker corp accumulate into one set."""
    tracker = _make_tracker(min_pilots=1)
    alert = tracker.add_kill(_killmail(
        30000142,
        victim={"character_id": 1, "corporation_id": 1000, "alliance_id": 99000},
        attackers=[
            {"character_id": 2, "corporation_id": 2000, "alliance_id": 88000},
            {"character_id": 3, "corporation_id": 3000, "alliance_id": 88000},
            {"character_id": 4, "corporation_id": 2000, "alliance_id": 88000},  # dup corp
        ],
    ))
    assert isinstance(alert, KillAlert)
    # Set semantics: duplicate corp 2000 collapses.
    assert alert.corps_involved == {1000, 2000, 3000}
    # Alliances unaffected and correct (victim 99000 + attackers 88000).
    assert alert.alliances_involved == {99000, 88000}


def test_two_kills_same_window_merge_corp_sets():
    """Two kills in the same system+window union their corp sets on the latest alert."""
    tracker = _make_tracker(min_pilots=1, window_seconds=300)
    first = tracker.add_kill(_killmail(
        30000142,
        victim={"character_id": 1, "corporation_id": 1000, "alliance_id": 99000},
        attackers=[{"character_id": 2, "corporation_id": 2000, "alliance_id": 88000}],
    ))
    assert first.corps_involved == {1000, 2000}

    second = tracker.add_kill(_killmail(
        30000142,
        victim={"character_id": 3, "corporation_id": 3000, "alliance_id": 77000},
        attackers=[{"character_id": 4, "corporation_id": 4000, "alliance_id": 88000}],
    ))
    # Second alert reflects the cumulative fight: all four corps.
    assert second.corps_involved == {1000, 2000, 3000, 4000}
    assert second.alliances_involved == {99000, 88000, 77000}


def test_none_and_zero_corp_ids_skipped():
    """Falsy corp ids (None / 0 / missing) are excluded from the set."""
    tracker = _make_tracker(min_pilots=1)
    alert = tracker.add_kill(_killmail(
        30000142,
        # Victim corp is 0 -> skipped.
        victim={"character_id": 1, "corporation_id": 0, "alliance_id": 99000},
        attackers=[
            {"character_id": 2, "corporation_id": None, "alliance_id": 88000},  # None
            {"character_id": 3, "alliance_id": 88000},                          # missing key
            {"character_id": 4, "corporation_id": 5000, "alliance_id": 88000},  # valid
        ],
    ))
    assert isinstance(alert, KillAlert)
    # Only the one valid corp survives.
    assert alert.corps_involved == {5000}
    # Alliance set still derived normally from the same killmail.
    assert alert.alliances_involved == {99000, 88000}


# ── backward-compat / no-network guards ─────────────────────────────────────

def test_corps_involved_default_is_empty_set():
    """The dataclass field is additive: a KillAlert built with no corps arg
    still has an (independent) empty set, not a shared/None default."""
    from datetime import datetime, timezone

    a = KillAlert(
        system_id=1, system_name="x", region_id=None, region_name="?",
        kill_count=0, total_value_millions=0.0, alliances_involved=set(),
        timestamp=datetime.now(timezone.utc), zkill_url="",
    )
    b = KillAlert(
        system_id=2, system_name="y", region_id=None, region_name="?",
        kill_count=0, total_value_millions=0.0, alliances_involved=set(),
        timestamp=datetime.now(timezone.utc), zkill_url="",
    )
    assert a.corps_involved == set()
    a.corps_involved.add(123)
    # default_factory must give each instance its own set (no shared mutable default).
    assert b.corps_involved == set()


def test_alert_path_makes_no_network_call(_no_network):
    """Triggering an alert must not hit the wire (helpers are stubbed)."""
    get_patch, post_patch = _no_network
    tracker = _make_tracker(min_pilots=1)
    alert = tracker.add_kill(_killmail(
        30000142,
        victim={"character_id": 1, "corporation_id": 1000, "alliance_id": 99000},
        attackers=[{"character_id": 2, "corporation_id": 2000, "alliance_id": 88000}],
    ))
    assert alert is not None
    get_patch.assert_not_called()
    post_patch.assert_not_called()


def test_below_threshold_returns_none():
    """Sanity: with no pilots and no capitals, no alert is produced (so the
    corps_involved assertions above are genuinely exercising the alert path)."""
    tracker = _make_tracker(min_pilots=10)
    # Single kill, one pilot — under the 10-pilot threshold, no capitals.
    alert = tracker.add_kill(_killmail(
        30000142,
        victim={"character_id": 1, "corporation_id": 1000, "alliance_id": 99000},
        attackers=[{"character_id": 2, "corporation_id": 2000, "alliance_id": 88000}],
    ))
    assert alert is None
