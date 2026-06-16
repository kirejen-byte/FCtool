import esi_auth


def test_scopes_include_fittings():
    assert "esi-fittings.read_fittings.v1" in esi_auth.SCOPES
    assert "esi-fittings.write_fittings.v1" in esi_auth.SCOPES


def _auth(monkeypatch, responses):
    a = esi_auth.ESIAuth.__new__(esi_auth.ESIAuth)   # bypass __init__/network
    a._character_id = 100
    # stub the low-level verbs the methods call:
    monkeypatch.setattr(a, "esi_get", lambda path, params=None: responses.get(("GET", path)))
    monkeypatch.setattr(a, "esi_post", lambda path, json_data=None: responses.get(("POST", path)))
    monkeypatch.setattr(a, "esi_put", lambda path, json_data=None: responses.get(("PUT", path)))
    return a


def test_get_fittings_returns_list(monkeypatch):
    a = _auth(monkeypatch, {("GET", "/characters/100/fittings/"):
                            [{"fitting_id": 1, "name": "X", "ship_type_id": 12015, "items": []}]})
    assert a.get_fittings(100)[0]["fitting_id"] == 1


def test_create_fitting_returns_id(monkeypatch):
    a = _auth(monkeypatch, {("POST", "/characters/100/fittings/"): {"fitting_id": 42}})
    body = {"name": "X", "description": "", "ship_type_id": 12015,
            "items": [{"type_id": 2048, "flag": "LoSlot0", "quantity": 1}]}
    assert a.create_fitting(100, body) == 42


def test_delete_fitting_true_on_204(monkeypatch):
    a = _auth(monkeypatch, {("DELETE", "/characters/100/fittings/42/"): True})
    monkeypatch.setattr(a, "esi_delete", lambda path: True, raising=False)
    assert a.delete_fitting(100, 42) is True


def test_get_fleet_returns_motd(monkeypatch):
    a = _auth(monkeypatch, {("GET", "/fleets/777/"):
                            {"motd": "form up", "is_free_move": True, "is_registered": True}})
    assert a.get_fleet(777)["motd"] == "form up"


def test_set_fleet_motd_puts_and_returns_true(monkeypatch):
    captured = {}
    a = esi_auth.ESIAuth.__new__(esi_auth.ESIAuth)
    def fake_put(path, json_data=None):
        captured["path"] = path; captured["body"] = json_data; return True
    monkeypatch.setattr(a, "esi_put", fake_put)
    assert a.set_fleet_motd(777, "<b>form up</b>") is True
    assert captured["path"] == "/fleets/777/"
    assert captured["body"] == {"motd": "<b>form up</b>"}      # motd-only body
