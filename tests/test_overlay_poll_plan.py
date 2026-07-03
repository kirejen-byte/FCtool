"""Pure scheduling tests for the overlay ESI poller round-robin."""
from fc_gui import FCToolGUI

plan = FCToolGUI._overlay_poll_plan
LOC_EVERY = 10.0
ONLINE_EVERY = 60.0


def test_first_pass_polls_everything():
    names = ["alpha", "bravo"]
    due = plan(names, last={}, now=1000.0,
               online_ok={"alpha": True, "bravo": False})
    # location+ship due for both; online due only for alpha (has scope)
    assert ("alpha", "locship") in due
    assert ("bravo", "locship") in due
    assert ("alpha", "online") in due
    assert ("bravo", "online") not in due    # no scope


def test_locship_respects_10s():
    names = ["alpha"]
    last = {("alpha", "locship"): 1000.0}
    assert plan(names, last, now=1005.0, online_ok={"alpha": False}) == []
    due = plan(names, last, now=1011.0, online_ok={"alpha": False})
    assert ("alpha", "locship") in due


def test_online_respects_60s():
    names = ["alpha"]
    last = {("alpha", "locship"): 1000.0, ("alpha", "online"): 1000.0}
    due = plan(names, last, now=1015.0, online_ok={"alpha": True})
    # locship due again (>10s) but online not yet (<60s)
    assert ("alpha", "locship") in due
    assert ("alpha", "online") not in due
    due2 = plan(names, last, now=1061.0, online_ok={"alpha": True})
    assert ("alpha", "online") in due2


def test_no_scope_never_schedules_online():
    names = ["alpha"]
    due = plan(names, last={}, now=9999.0, online_ok={"alpha": False})
    assert all(kind != "online" for _, kind in due)


def test_empty_names():
    assert plan([], last={}, now=1.0, online_ok={}) == []
