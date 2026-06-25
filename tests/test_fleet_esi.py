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
