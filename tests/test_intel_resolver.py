import intel_monitor
from intel_monitor import Resolution, resolve_names


class FakeResp:
    def __init__(self, ok, payload):
        self.ok = ok
        self._payload = payload

    def json(self):
        return self._payload


def _install_fake_requests(monkeypatch, ids_payload, char_detail):
    def fake_post(url, json=None, headers=None, timeout=None):
        assert url.endswith("/universe/ids/")
        return FakeResp(True, ids_payload)

    def fake_get(url, headers=None, timeout=None):
        if "/characters/" in url:
            cid = int(url.rstrip("/").rsplit("/", 1)[-1])
            return FakeResp(True, char_detail[cid]["char"])
        if "/corporations/" in url:
            cid = int(url.rstrip("/").rsplit("/", 1)[-1])
            return FakeResp(True, {"name": f"Corp{cid}"})
        if "/alliances/" in url:
            aid = int(url.rstrip("/").rsplit("/", 1)[-1])
            return FakeResp(True, {"name": f"Alliance{aid}"})
        return FakeResp(False, {})

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "get", fake_get)


def test_resolve_names_buckets_by_standing(monkeypatch):
    ids_payload = {"characters": [{"name": "Bob", "id": 1001}]}
    char_detail = {1001: {"char": {"corporation_id": 5001, "alliance_id": 7001}}}
    _install_fake_requests(monkeypatch, ids_payload, char_detail)
    res = resolve_names(["Bob"], friendly={7001}, hostile=set())
    assert len(res) == 1
    r = res[0]
    assert isinstance(r, Resolution)
    assert r.name == "Bob"
    assert r.character_id == 1001
    assert r.corporation_id == 5001
    assert r.alliance_id == 7001
    assert r.corporation == "Corp5001"
    assert r.alliance == "Alliance7001"
    assert r.standing == "friendly"


def test_resolve_names_hostile_and_neutral(monkeypatch):
    ids_payload = {"characters": [
        {"name": "Red", "id": 2001},
        {"name": "Grey", "id": 2002},
    ]}
    char_detail = {
        2001: {"char": {"corporation_id": 6001}},
        2002: {"char": {"corporation_id": 6002}},
    }
    _install_fake_requests(monkeypatch, ids_payload, char_detail)
    res = {r.name: r for r in resolve_names(["Red", "Grey"],
                                            friendly=set(), hostile={6001})}
    assert res["Red"].standing == "hostile"
    assert res["Grey"].standing == "neutral"


def test_resolve_names_empty_returns_empty(monkeypatch):
    assert resolve_names([], set(), set()) == []
