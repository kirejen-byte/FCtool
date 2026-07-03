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


import types as _types
import fc_gui as _fcg


class FakeAuth:
    def __init__(self, name, cid, *, online=True, has_online=True,
                 loc=None, ship=None):
        self.character_name = name
        self.character_id = cid
        self._online = online
        self._has_online = has_online
        self._loc = loc or {"solar_system_id": 30000142}
        self._ship = ship or {"ship_type_id": 11957, "ship_name": "cyno alt"}
    def has_scope(self, scope):
        return self._has_online
    def get_location(self):
        return dict(self._loc)
    def get_ship_type(self):
        return dict(self._ship)
    def esi_get(self, path):
        if path.endswith("/online/"):
            return {"online": self._online}
        return None


def _state_host(monkeypatch):
    host = _types.SimpleNamespace()
    host._overlay_states = {}
    # stub ship_classes lookups so the test is network-free + deterministic
    monkeypatch.setattr(_fcg.ship_classes, "get_group_name",
                        lambda tid: "Force Recon Ship" if tid == 11957 else "Shuttle")
    monkeypatch.setattr(_fcg.ship_classes, "is_capital", lambda tid: False)
    host._overlay_build_state = _types.MethodType(
        _fcg.FCToolGUI._overlay_build_state, host)
    return host


def test_build_state_locship(monkeypatch):
    host = _state_host(monkeypatch)
    auth = FakeAuth("Alpha", 1)
    # monkeypatch system name resolution used by the builder
    monkeypatch.setattr(_fcg, "get_system_info",
                        lambda sid: {"name": "Jita"}, raising=False)
    st = host._overlay_build_state(auth, do_online=False)
    assert st.name == "Alpha"
    assert st.ship_type_id == 11957
    assert st.ship_group == "Force Recon Ship"
    assert st.system_name == "Jita"
    assert st.docked is False
    assert st.online is None      # online not fetched this pass


def test_build_state_docked(monkeypatch):
    host = _state_host(monkeypatch)
    auth = FakeAuth("Beta", 2, loc={"solar_system_id": 30000142,
                                     "station_id": 60003760})
    monkeypatch.setattr(_fcg, "get_system_info",
                        lambda sid: {"name": "Jita"}, raising=False)
    st = host._overlay_build_state(auth, do_online=False)
    assert st.docked is True


def test_build_state_online(monkeypatch):
    host = _state_host(monkeypatch)
    auth = FakeAuth("Gamma", 3, online=False)
    monkeypatch.setattr(_fcg, "get_system_info",
                        lambda sid: {"name": "Jita"}, raising=False)
    st = host._overlay_build_state(auth, do_online=True)
    assert st.online is False
