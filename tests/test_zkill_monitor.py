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
import requests

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


# ── standings-based capital friend/foe ──────────────────────────────────────
#
# These exercise the STANDINGS-BASED capital filter that replaced the old
# hard-coded FRIENDLY_ALLIANCE_IDS constant. A capital is FRIENDLY when its
# corp OR alliance is in the tracker's `friendly_ids` set; otherwise it is
# HOSTILE. Only hostile caps drive `capitals_involved` and the
# `capital_breakdown` (hostile-only count per class).
#
# In every test below `min_pilots` is set high enough that pilot count alone
# can NOT trip the alert, so the capital path is what fires it. Capital ship
# type ids are pulled from zkill_monitor.CAPITAL_CLASSES so they classify.

# Concrete capital type ids (each in exactly one class).
DREAD_TYPE_ID = 19720    # -> "Dreads"
CARRIER_TYPE_ID = 23757  # -> "Carriers"
TITAN_TYPE_ID = 671      # -> "Titans"
FAX_TYPE_ID = 37604      # -> "FAX"


def _cap_tracker(friendly_ids=None, min_pilots=50, window_seconds=300):
    """Tracker with a high pilot floor so only the capital path can alert."""
    return EngagementTracker(window_seconds=window_seconds, min_pilots=min_pilots,
                             friendly_ids=friendly_ids)


def test_hostile_capital_triggers_and_counts_by_class():
    """A cap whose corp AND alliance are NOT friendly is hostile: it sets
    capitals_involved and is counted by class in the breakdown."""
    tracker = _cap_tracker(friendly_ids={99000})  # friendly set does NOT include this cap
    alert = tracker.add_kill(_killmail(
        30000142,
        victim={"character_id": 1, "ship_type_id": DREAD_TYPE_ID,
                "corporation_id": 2000, "alliance_id": 88000},
        attackers=[],
    ))
    assert isinstance(alert, KillAlert)
    assert alert.capitals_involved is True
    assert alert.capital_breakdown == {"Dreads": 1}


def test_friendly_by_alliance_not_counted_hostile():
    """A cap whose ALLIANCE is friendly is excluded from the hostile tally;
    if ALL caps are friendly, no capital alert fires."""
    tracker = _cap_tracker(friendly_ids={88000})  # alliance 88000 is blue
    alert = tracker.add_kill(_killmail(
        30000142,
        victim={"character_id": 1, "ship_type_id": DREAD_TYPE_ID,
                "corporation_id": 2000, "alliance_id": 88000},
        attackers=[],
    ))
    # Only a friendly cap and not enough pilots -> no alert at all.
    assert alert is None


def test_friendly_by_corp_not_counted_hostile():
    """A cap whose CORP is friendly (alliance NOT) is treated friendly."""
    tracker = _cap_tracker(friendly_ids={2000})  # corp 2000 is blue, alliance 88000 is not
    alert = tracker.add_kill(_killmail(
        30000142,
        victim={"character_id": 1, "ship_type_id": DREAD_TYPE_ID,
                "corporation_id": 2000, "alliance_id": 88000},
        attackers=[],
    ))
    assert alert is None


def test_two_friendly_caps_do_not_trigger():
    """The old `friendly_caps > 1` self-trigger is gone: 2+ friendly caps with
    zero hostiles must NOT set capitals_involved (and not alert)."""
    tracker = _cap_tracker(friendly_ids={88000})  # both caps' alliance is blue
    alert = tracker.add_kill(_killmail(
        30000142,
        victim={"character_id": 1, "ship_type_id": DREAD_TYPE_ID,
                "corporation_id": 2000, "alliance_id": 88000},
        attackers=[
            {"character_id": 2, "ship_type_id": CARRIER_TYPE_ID,
             "corporation_id": 3000, "alliance_id": 88000},
        ],
    ))
    # Two friendly caps, no hostile cap, pilots below floor -> nothing.
    assert alert is None


def test_mixed_caps_breakdown_is_hostile_only():
    """1 hostile + 2 friendly caps -> capitals_involved True, and the breakdown
    shows ONLY the single hostile cap."""
    tracker = _cap_tracker(friendly_ids={88000})  # alliance 88000 is blue
    alert = tracker.add_kill(_killmail(
        30000142,
        # Hostile dread: corp/alliance not in friendly set.
        victim={"character_id": 1, "ship_type_id": DREAD_TYPE_ID,
                "corporation_id": 2000, "alliance_id": 77000},
        attackers=[
            # Friendly carrier (alliance blue).
            {"character_id": 2, "ship_type_id": CARRIER_TYPE_ID,
             "corporation_id": 3000, "alliance_id": 88000},
            # Friendly titan (alliance blue).
            {"character_id": 3, "ship_type_id": TITAN_TYPE_ID,
             "corporation_id": 4000, "alliance_id": 88000},
        ],
    ))
    assert isinstance(alert, KillAlert)
    assert alert.capitals_involved is True
    # Only the hostile dread is in the breakdown; friendly carrier/titan absent.
    assert alert.capital_breakdown == {"Dreads": 1}


def test_set_friendly_ids_flips_hostile_to_friendly():
    """A cap hostile under an empty set becomes friendly after set_friendly_ids
    adds its alliance — proving the closure reads the *current* set each call."""
    # First: empty friendly set -> the cap is hostile and alerts.
    tracker = _cap_tracker(friendly_ids=set())
    alert = tracker.add_kill(_killmail(
        30000142,
        victim={"character_id": 1, "ship_type_id": DREAD_TYPE_ID,
                "corporation_id": 2000, "alliance_id": 88000},
        attackers=[],
    ))
    assert isinstance(alert, KillAlert)
    assert alert.capitals_involved is True
    assert alert.capital_breakdown == {"Dreads": 1}

    # Now mark its alliance friendly and re-run on a FRESH tracker/window so the
    # only difference is the friendly set.
    tracker2 = _cap_tracker(friendly_ids=set())
    tracker2.set_friendly_ids({88000})
    alert2 = tracker2.add_kill(_killmail(
        30000142,
        victim={"character_id": 1, "ship_type_id": DREAD_TYPE_ID,
                "corporation_id": 2000, "alliance_id": 88000},
        attackers=[],
    ))
    # Same cap, now friendly -> no capital alert.
    assert alert2 is None


def test_empty_friendly_set_all_caps_hostile():
    """With an empty friendly set, every cap is hostile and trips the alert."""
    tracker = _cap_tracker(friendly_ids=set())
    alert = tracker.add_kill(_killmail(
        30000142,
        victim={"character_id": 1, "ship_type_id": TITAN_TYPE_ID,
                "corporation_id": 2000, "alliance_id": 88000},
        attackers=[],
    ))
    assert isinstance(alert, KillAlert)
    assert alert.capitals_involved is True
    assert alert.capital_breakdown == {"Titans": 1}


def test_friendly_by_corp_only_alliance_absent():
    """corp_id threading: a cap friendly ONLY by corp (alliance absent/None) is
    still treated friendly — proves corp_id is passed through to the filter."""
    tracker = _cap_tracker(friendly_ids={2000})  # blue by corp only
    alert = tracker.add_kill(_killmail(
        30000142,
        # No alliance_id key at all; corp 2000 is the only id and it's friendly.
        victim={"character_id": 1, "ship_type_id": FAX_TYPE_ID,
                "corporation_id": 2000},
        attackers=[],
    ))
    # Friendly by corp -> no hostile cap -> no alert.
    assert alert is None


def test_hostile_when_only_corp_present_and_not_friendly():
    """Control for the corp-threading test: a cap with ONLY a corp id that is
    NOT friendly is hostile (so the previous test isn't passing by accident)."""
    tracker = _cap_tracker(friendly_ids={9999})  # cap's corp 2000 is not in it
    alert = tracker.add_kill(_killmail(
        30000142,
        victim={"character_id": 1, "ship_type_id": FAX_TYPE_ID,
                "corporation_id": 2000},
        attackers=[],
    ))
    assert isinstance(alert, KillAlert)
    assert alert.capitals_involved is True
    assert alert.capital_breakdown == {"FAX": 1}


def test_monitor_set_friendly_ids_forwards_to_tracker():
    """ZKillMonitor.set_friendly_ids updates its own set AND forwards to the
    live tracker (no rebuild)."""
    from zkill_monitor import ZKillMonitor

    monitor = ZKillMonitor(friendly_ids={1})
    assert monitor._friendly_ids == {1}
    assert monitor._tracker.friendly_ids == {1}

    monitor.set_friendly_ids({2, 3})
    assert monitor._friendly_ids == {2, 3}
    # Forwarded to the same tracker instance, not a rebuilt one.
    assert monitor._tracker.friendly_ids == {2, 3}

    # None clears it.
    monitor.set_friendly_ids(None)
    assert monitor._friendly_ids == set()
    assert monitor._tracker.friendly_ids == set()


# ── _fetch_kill: outage vs idle logging ─────────────────────────────────────
#
# _fetch_kill must distinguish an OUTAGE (HTTP error / request exception ->
# logged) from IDLE (200 with a body, or a legitimate 404 -> silent). All four
# branches still return None or the body without raising, preserving the
# polling loop's behavior. We patch zkill_monitor.requests.get directly (the
# autouse `_no_network` fixture also patches it, but we override per-test).

from zkill_monitor import ZKillMonitor


class _FakeResp:
    """Minimal stand-in for requests.Response covering _fetch_kill's branches."""

    def __init__(self, ok, status_code, body=None):
        self.ok = ok
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


def test_fetch_kill_success_returns_body_no_log(mocker):
    """A 200 with a body returns it and logs nothing (idle/normal path)."""
    body = {"killmail": {"solar_system_id": 30000142}, "zkb": {}}
    mocker.patch.object(zkill_monitor.requests, "get",
                        return_value=_FakeResp(ok=True, status_code=200, body=body))
    log_warn = mocker.patch.object(zkill_monitor.log, "warning")
    log_exc = mocker.patch.object(zkill_monitor.log, "exception")

    monitor = ZKillMonitor()
    assert monitor._fetch_kill(5) == body
    log_warn.assert_not_called()
    log_exc.assert_not_called()


def test_fetch_kill_404_returns_none_no_log(mocker):
    """A 404 is the legitimate idle case: returns None and logs nothing."""
    mocker.patch.object(zkill_monitor.requests, "get",
                        return_value=_FakeResp(ok=False, status_code=404))
    log_warn = mocker.patch.object(zkill_monitor.log, "warning")
    log_exc = mocker.patch.object(zkill_monitor.log, "exception")

    monitor = ZKillMonitor()
    assert monitor._fetch_kill(5) is None
    log_warn.assert_not_called()
    log_exc.assert_not_called()


def test_fetch_kill_non_404_error_warns(mocker):
    """A non-404 HTTP error (e.g. 503) is an outage: returns None and warns."""
    mocker.patch.object(zkill_monitor.requests, "get",
                        return_value=_FakeResp(ok=False, status_code=503))
    log_warn = mocker.patch.object(zkill_monitor.log, "warning")
    log_exc = mocker.patch.object(zkill_monitor.log, "exception")

    monitor = ZKillMonitor()
    assert monitor._fetch_kill(7) is None
    assert log_warn.call_count == 1
    log_exc.assert_not_called()


def test_fetch_kill_exception_logs_and_returns_none(mocker):
    """A request exception is an outage: returns None and log.exception fires."""
    mocker.patch.object(zkill_monitor.requests, "get",
                        side_effect=requests.exceptions.ConnectionError("boom"))
    log_warn = mocker.patch.object(zkill_monitor.log, "warning")
    log_exc = mocker.patch.object(zkill_monitor.log, "exception")

    monitor = ZKillMonitor()
    assert monitor._fetch_kill(9) is None
    log_exc.assert_called_once()
    log_warn.assert_not_called()
