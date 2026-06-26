# tests/test_fleet_esi.py
import pytest
from fleet_esi import FleetESIError, _call


class FakeResp:
    def __init__(self, status_code, body=None, text=""):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._body = body or {}
        self.text = text

    def json(self):
        return self._body


class FakeSession:
    """Returns/raises queued items in order on each .request() call."""
    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    def request(self, method, path, json=None):
        self.calls.append((method, path, json))
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_call_returns_response_on_2xx():
    sess = FakeSession([FakeResp(204)])
    resp = _call(sess, "PUT", "/x/", json={"a": 1}, expect=(204,))
    assert resp.status_code == 204
    assert sess.calls == [("PUT", "/x/", {"a": 1})]


def test_call_retries_once_on_5xx_then_succeeds():
    sess = FakeSession([FakeResp(500), FakeResp(201, {"wing_id": 7})])
    resp = _call(sess, "POST", "/w/", expect=(201,))
    assert resp.json()["wing_id"] == 7
    assert len(sess.calls) == 2


def test_call_raises_boss_lost_on_403():
    sess = FakeSession([FakeResp(403)])
    with pytest.raises(FleetESIError) as ei:
        _call(sess, "PUT", "/x/", expect=(204,))
    assert ei.value.reason == "boss_lost"


def test_call_raises_not_found_on_404():
    sess = FakeSession([FakeResp(404)])
    with pytest.raises(FleetESIError) as ei:
        _call(sess, "PUT", "/x/", expect=(204,))
    assert ei.value.reason == "not_found"


def test_call_raises_after_second_5xx_failure():
    sess = FakeSession([FakeResp(502), FakeResp(503)])
    with pytest.raises(FleetESIError) as ei:
        _call(sess, "POST", "/w/", expect=(201,))
    assert ei.value.reason == "http_error"
    assert ei.value.status == 503


def test_call_retries_once_on_network_exception():
    sess = FakeSession([RuntimeError("conn reset"), FakeResp(204)])
    resp = _call(sess, "PUT", "/x/", expect=(204,))
    assert resp.status_code == 204


import fleet_esi


def test_create_wing_returns_id_and_renames_when_named():
    sess = FakeSession([FakeResp(201, {"wing_id": 5}), FakeResp(204)])
    wid = fleet_esi.create_wing(sess, 999, "Alpha Wing")
    assert wid == 5
    # second call renames the new wing, name clamped to 10 chars
    assert sess.calls[1] == ("PUT", "/fleets/999/wings/5/", {"name": "Alpha Wing"})


def test_create_wing_skips_rename_for_blank_name():
    sess = FakeSession([FakeResp(201, {"wing_id": 5})])
    wid = fleet_esi.create_wing(sess, 999, "")
    assert wid == 5
    assert len(sess.calls) == 1


def test_create_squad_clamps_long_name_to_ten_chars():
    sess = FakeSession([FakeResp(201, {"squad_id": 8}), FakeResp(204)])
    sid = fleet_esi.create_squad(sess, 999, 5, "Logistics Wing Squad")
    assert sid == 8
    assert sess.calls[1] == ("PUT", "/fleets/999/squads/8/", {"name": "Logistics "})


def test_move_member_squad_member_sends_wing_and_squad():
    sess = FakeSession([FakeResp(204)])
    fleet_esi.move_member(sess, 999, 42, wing_id=5, squad_id=8, role="squad_member")
    assert sess.calls[0] == ("PUT", "/fleets/999/members/42/",
                             {"role": "squad_member", "wing_id": 5, "squad_id": 8})


def test_move_member_fleet_commander_sends_role_only():
    sess = FakeSession([FakeResp(204)])
    fleet_esi.move_member(sess, 999, 42, wing_id=None, squad_id=None,
                          role="fleet_commander")
    assert sess.calls[0] == ("PUT", "/fleets/999/members/42/",
                             {"role": "fleet_commander"})


def test_move_member_wing_commander_sends_wing_only():
    sess = FakeSession([FakeResp(204)])
    fleet_esi.move_member(sess, 999, 42, wing_id=5, squad_id=None,
                          role="wing_commander")
    assert sess.calls[0] == ("PUT", "/fleets/999/members/42/",
                             {"role": "wing_commander", "wing_id": 5})


def test_get_wings_returns_parsed_list():
    payload = [{"id": 1, "name": "W", "squads": [{"id": 2, "name": "S"}]}]
    sess = FakeSession([FakeResp(200, payload)])
    assert fleet_esi.get_wings(sess, 999) == payload


def test_delete_wing_and_squad():
    sess = FakeSession([FakeResp(204), FakeResp(204)])
    fleet_esi.delete_wing(sess, 999, 5)
    fleet_esi.delete_squad(sess, 999, 8)
    assert sess.calls == [("DELETE", "/fleets/999/wings/5/", None),
                          ("DELETE", "/fleets/999/squads/8/", None)]


class _FakeRequestsSession:
    def __init__(self):
        self.last = None

    def request(self, method, url, headers=None, json=None, timeout=None):
        self.last = {"method": method, "url": url, "headers": headers,
                     "json": json, "timeout": timeout}
        return FakeResp(204)


class _FakeAuth:
    def __init__(self, token="tok"):
        self.access_token = token
        self._session = _FakeRequestsSession()


def test_auth_session_builds_authorized_request(monkeypatch):
    # Don't actually sleep in the rate limiter during tests.
    monkeypatch.setattr("rate_limiter.rate_limit", lambda *a, **k: None)
    auth = _FakeAuth()
    sess = fleet_esi.AuthEsiSession(auth)
    resp = sess.request("PUT", "/fleets/1/members/2/", json={"role": "squad_member"})
    assert resp.status_code == 204
    call = auth._session.last
    assert call["method"] == "PUT"
    assert call["url"].endswith("/fleets/1/members/2/")
    assert call["headers"]["Authorization"] == "Bearer tok"
    assert call["json"] == {"role": "squad_member"}


def test_auth_session_raises_no_token_when_unauthenticated(monkeypatch):
    monkeypatch.setattr("rate_limiter.rate_limit", lambda *a, **k: None)
    auth = _FakeAuth(token=None)
    sess = fleet_esi.AuthEsiSession(auth)
    with pytest.raises(FleetESIError) as ei:
        sess.request("GET", "/fleets/1/wings/")
    assert ei.value.reason == "no_token"


class StatefulFleet:
    """In-memory fleet that models the ESI endpoints fleet_esi uses, including
    EVE auto-creating a default 'Squad 1' when a wing is created."""
    def __init__(self, wings=None):
        self.wings = wings or []          # [{id,name,squads:[{id,name}]}]
        self._next = 1000

    def _new_id(self):
        self._next += 1
        return self._next

    def request(self, method, path, json=None):
        parts = [p for p in path.split("/") if p]   # e.g. fleets,1,wings
        # GET /fleets/{id}/wings/
        if method == "GET" and parts[-1] == "wings":
            return FakeResp(200, [dict(w, squads=[dict(s) for s in w["squads"]])
                                  for w in self.wings])
        # POST /fleets/{id}/wings/  → new wing + AUTO squad (models EVE)
        if method == "POST" and parts[-1] == "wings":
            wid = self._new_id()
            self.wings.append({"id": wid, "name": "New Wing",
                               "squads": [{"id": self._new_id(), "name": "Squad 1"}]})
            return FakeResp(201, {"wing_id": wid})
        # POST /fleets/{id}/wings/{wid}/squads/  → new squad
        if method == "POST" and parts[-1] == "squads":
            wid = int(parts[-2])
            w = next(w for w in self.wings if w["id"] == wid)
            sid = self._new_id()
            w["squads"].append({"id": sid, "name": "Squad 2"})
            return FakeResp(201, {"squad_id": sid})
        # PUT /fleets/{id}/wings/{wid}/  rename
        if method == "PUT" and parts[-2] == "wings":
            wid = int(parts[-1])
            w = next(w for w in self.wings if w["id"] == wid)
            w["name"] = json["name"]
            return FakeResp(204)
        # PUT /fleets/{id}/squads/{sid}/  rename
        if method == "PUT" and parts[-2] == "squads":
            sid = int(parts[-1])
            for w in self.wings:
                for s in w["squads"]:
                    if s["id"] == sid:
                        s["name"] = json["name"]
            return FakeResp(204)
        if method == "DELETE":
            return FakeResp(204)
        raise AssertionError(f"unexpected {method} {path}")


def _squad_names(fleet):
    return {w["name"]: [s["name"] for s in w["squads"]] for w in fleet.wings}


def test_ensure_structure_creates_wing_and_reuses_auto_squad_no_stray():
    # Live fleet has Alpha/[Logi]; template wants Alpha/[Logi] + Bravo/[DPS].
    fleet = StatefulFleet([{"id": 1, "name": "Alpha",
                            "squads": [{"id": 2, "name": "Logi"}]}])
    wanted = [("Alpha", ["Logi"]), ("Bravo", ["DPS"])]
    wmap, smap = fleet_esi.ensure_structure(fleet, 99, wanted, fleet.wings)
    names = _squad_names(fleet)
    assert set(names) == {"Alpha", "Bravo"}        # Bravo wing was created
    assert names["Bravo"] == ["DPS"]               # auto "Squad 1" reused → no stray
    assert names["Alpha"] == ["Logi"]
    # returned maps are keyed by clamped name
    assert wmap["Alpha"] == 1 and "Bravo" in wmap
    assert ("Bravo", "DPS") in smap


def test_ensure_structure_creates_shortfall_squads_after_reusing_auto():
    fleet = StatefulFleet([])   # empty fleet
    wanted = [("Wing 1", ["A", "B", "C"])]
    fleet_esi.ensure_structure(fleet, 99, wanted, fleet.wings)
    names = _squad_names(fleet)
    assert names["Wing 1"] == ["A", "B", "C"]   # auto squad → A, then B and C created


def test_ensure_structure_idempotent_when_already_matches():
    fleet = StatefulFleet([{"id": 1, "name": "Alpha",
                            "squads": [{"id": 2, "name": "Logi"}]}])
    before = _squad_names(fleet)
    fleet_esi.ensure_structure(fleet, 99, [("Alpha", ["Logi"])], fleet.wings)
    assert _squad_names(fleet) == before   # nothing created/renamed


def test_ensure_structure_matches_clamped_names():
    # Live squad name is clamped to 10 chars; template uses the full name.
    fleet = StatefulFleet([{"id": 1, "name": "Logistics ",   # 10 chars (clamped)
                            "squads": [{"id": 2, "name": "Guardians "}]}])
    wanted = [("Logistics Wing", ["Guardians Squad"])]   # full (>10) names
    fleet_esi.ensure_structure(fleet, 99, wanted, fleet.wings)
    # Must NOT create a duplicate wing/squad — clamped names already match.
    assert len(fleet.wings) == 1
    assert len(fleet.wings[0]["squads"]) == 1
