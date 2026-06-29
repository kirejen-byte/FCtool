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


import threading
import time

from intel_monitor import Resolution
from intel_resolver import IntelResolver


def _mk(name, standing="neutral"):
    return Resolution(name=name, character_id=hash(name) & 0xffff,
                      corporation_id=None, corporation="",
                      alliance_id=None, alliance="", standing=standing)


def test_lookup_cached_returns_none_then_value():
    def fake_resolve(names, friendly, hostile):
        return [_mk(n, "hostile") for n in names]

    r = IntelResolver(resolve_fn=fake_resolve, friendly=set(), hostile=set())
    assert r.lookup_cached("Bob") is None
    done = threading.Event()
    results = {}

    def cb(d):
        results.update(d)
        done.set()

    r.start()
    try:
        r.request(["Bob"], cb)
        assert done.wait(2.0)
        assert results["Bob"].standing == "hostile"
        # now cached and case-insensitive
        assert r.lookup_cached("bob") is not None
    finally:
        r.stop()


def test_cached_answered_immediately_without_worker():
    r = IntelResolver(resolve_fn=lambda n, f, h: [],
                      friendly=set(), hostile=set())
    r._cache["bob"] = _mk("Bob", "friendly")
    got = {}
    r.request(["Bob"], lambda d: got.update(d))
    assert got["Bob"].standing == "friendly"


def test_in_flight_dedupe():
    calls = []
    gate = threading.Event()

    def slow_resolve(names, friendly, hostile):
        calls.append(list(names))
        gate.wait(1.0)
        return [_mk(n) for n in names]

    r = IntelResolver(resolve_fn=slow_resolve, friendly=set(), hostile=set())
    r.start()
    try:
        r.request(["Bob"], lambda d: None)
        r.request(["Bob"], lambda d: None)  # already in-flight -> not re-queued
        time.sleep(0.2)
        gate.set()
        time.sleep(0.3)
        # "Bob" requested twice but only resolved once
        flat = [n for batch in calls for n in batch]
        assert flat.count("Bob") == 1
    finally:
        r.stop()


def test_cache_evicts_oldest_over_cap():
    r = IntelResolver(resolve_fn=lambda n, f, h: [], friendly=set(),
                      hostile=set(), cache_cap=3)
    for i in range(5):
        r._cache_put(_mk(f"P{i}"))
    assert len(r._cache) == 3
    # oldest two evicted
    assert "p0" not in r._cache and "p1" not in r._cache
    assert "p4" in r._cache
