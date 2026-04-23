"""Tests for the size-capped dscan fetch helper in fc_web."""
from unittest.mock import patch

import pytest

import fc_web


class _FakeResponse:
    """Minimal stand-in for a requests.Response used with stream=True."""

    def __init__(self, *, status_code=200, headers=None, chunks=(), encoding="utf-8"):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = list(chunks)
        self.encoding = encoding
        self.apparent_encoding = encoding
        self.closed = False

    @property
    def ok(self):
        return 200 <= self.status_code < 400

    def iter_content(self, chunk_size=8192):
        for chunk in self._chunks:
            yield chunk

    def close(self):
        self.closed = True


def _make_get(fake_response):
    """Return a mock that records args and returns the given fake response."""
    calls = {}

    def _get(url, **kwargs):
        calls["url"] = url
        calls["kwargs"] = kwargs
        return fake_response

    return _get, calls


# ── Normal response under cap ───────────────────────────────────────────────

def test_fetch_normal_response_returns_body():
    body = b"ship list: small, readable"
    fake = _FakeResponse(
        headers={"Content-Length": str(len(body))},
        chunks=[body],
    )
    get_fn, _calls = _make_get(fake)

    with patch.object(fc_web.http_requests, "get", side_effect=get_fn):
        ok, status, text = fc_web._fetch_with_size_cap(
            "https://dscan.info/v/abc123", max_bytes=1024
        )

    assert ok is True
    assert status == 200
    assert text == body.decode("utf-8")
    assert fake.closed is True


def test_fetch_passes_streaming_and_no_redirect_and_timeout_tuple():
    fake = _FakeResponse(chunks=[b"ok"])
    get_fn, calls = _make_get(fake)

    with patch.object(fc_web.http_requests, "get", side_effect=get_fn):
        fc_web._fetch_with_size_cap("https://dscan.info/v/abc", max_bytes=1024)

    kwargs = calls["kwargs"]
    assert kwargs["stream"] is True
    assert kwargs["allow_redirects"] is False
    assert kwargs["timeout"] == (5, 10)


# ── Content-Length declares way over cap ────────────────────────────────────

def test_fetch_refuses_when_content_length_exceeds_cap():
    # Chunks should never be iterated in this path. If they are, the test
    # will still assert ok=False, but we place a huge payload here to prove
    # we did not consume it.
    huge_payload = b"x" * (10 * 1024 * 1024)
    fake = _FakeResponse(
        headers={"Content-Length": str(10 * 1024 * 1024)},
        chunks=[huge_payload],
    )

    iter_calls = {"count": 0}
    original_iter = fake.iter_content

    def _counting_iter(chunk_size=8192):
        iter_calls["count"] += 1
        yield from original_iter(chunk_size=chunk_size)

    fake.iter_content = _counting_iter  # type: ignore[assignment]

    get_fn, _calls = _make_get(fake)

    with patch.object(fc_web.http_requests, "get", side_effect=get_fn):
        ok, status, text = fc_web._fetch_with_size_cap(
            "https://dscan.info/v/huge", max_bytes=1024
        )

    assert ok is False
    assert text is None
    assert status == 200
    assert iter_calls["count"] == 0, "should not stream body when header declares oversize"
    assert fake.closed is True


# ── Content-Length missing/small but body streams over cap ──────────────────

def test_fetch_aborts_midstream_when_body_exceeds_cap():
    # No Content-Length at all; chunks add up to > cap.
    cap = 1024
    chunks = [b"a" * 512, b"b" * 512, b"c" * 512]  # total 1536 > 1024
    fake = _FakeResponse(headers={}, chunks=chunks)

    get_fn, _calls = _make_get(fake)

    with patch.object(fc_web.http_requests, "get", side_effect=get_fn):
        ok, status, text = fc_web._fetch_with_size_cap(
            "https://dscan.info/v/sneaky", max_bytes=cap
        )

    assert ok is False
    assert text is None
    assert status == 200
    assert fake.closed is True


def test_fetch_aborts_midstream_when_content_length_lies_small():
    # Content-Length lies: reports tiny size, actual stream is large.
    cap = 1024
    chunks = [b"x" * 4096]
    fake = _FakeResponse(
        headers={"Content-Length": "10"},  # lie — well under cap
        chunks=chunks,
    )
    get_fn, _calls = _make_get(fake)

    with patch.object(fc_web.http_requests, "get", side_effect=get_fn):
        ok, status, text = fc_web._fetch_with_size_cap(
            "https://dscan.info/v/liar", max_bytes=cap
        )

    assert ok is False
    assert text is None
    assert fake.closed is True


# ── Non-HTTPS rejected ──────────────────────────────────────────────────────

def test_fetch_rejects_http_scheme():
    # If our guard works, requests.get is never called.
    sentinel = {"called": False}

    def _should_not_be_called(*args, **kwargs):
        sentinel["called"] = True
        raise AssertionError("requests.get should not be called for http://")

    with patch.object(fc_web.http_requests, "get", side_effect=_should_not_be_called):
        ok, status, text = fc_web._fetch_with_size_cap(
            "http://dscan.info/v/abc", max_bytes=1024
        )

    assert ok is False
    assert status is None
    assert text is None
    assert sentinel["called"] is False


def test_fetch_rejects_other_schemes():
    with patch.object(fc_web.http_requests, "get") as mock_get:
        ok, _status, _text = fc_web._fetch_with_size_cap(
            "ftp://dscan.info/v/abc", max_bytes=1024
        )
    assert ok is False
    mock_get.assert_not_called()


# ── HTTP error from allowed host ────────────────────────────────────────────

def test_fetch_returns_not_ok_on_http_error_status():
    fake = _FakeResponse(status_code=502, chunks=[])
    get_fn, _calls = _make_get(fake)

    with patch.object(fc_web.http_requests, "get", side_effect=get_fn):
        ok, status, text = fc_web._fetch_with_size_cap(
            "https://dscan.info/v/missing", max_bytes=1024
        )

    assert ok is False
    assert status == 502
    assert text is None
    assert fake.closed is True


# ── Request exception ───────────────────────────────────────────────────────

def test_fetch_returns_failure_on_request_exception():
    def _raise(*args, **kwargs):
        raise fc_web.http_requests.ConnectionError("boom")

    with patch.object(fc_web.http_requests, "get", side_effect=_raise):
        ok, status, text = fc_web._fetch_with_size_cap(
            "https://dscan.info/v/unreachable", max_bytes=1024
        )

    assert ok is False
    assert status is None
    assert text is None


# ── Redirect handling with per-hop allowlist re-validation ──────────────────

def _make_sequential_get(responses_by_url):
    """Return a mock that dispatches to a per-URL fake response and records order."""
    calls = []

    def _get(url, **kwargs):
        calls.append({"url": url, "kwargs": kwargs})
        if url not in responses_by_url:
            raise AssertionError(f"Unexpected URL requested: {url!r}")
        return responses_by_url[url]

    return _get, calls


def test_fetch_follows_302_to_allowed_host_and_returns_final_body():
    # 302 from /X to /X/ on the same allowlisted host, then the final body.
    redirect = _FakeResponse(
        status_code=302,
        headers={"Location": "https://dscan.info/v/abc/"},
        chunks=[],
    )
    final_body = b"final dscan payload"
    final = _FakeResponse(
        status_code=200,
        headers={"Content-Length": str(len(final_body))},
        chunks=[final_body],
    )
    get_fn, calls = _make_sequential_get({
        "https://dscan.info/v/abc": redirect,
        "https://dscan.info/v/abc/": final,
    })

    with patch.object(fc_web.http_requests, "get", side_effect=get_fn):
        ok, status, text = fc_web._fetch_with_size_cap(
            "https://dscan.info/v/abc",
            max_bytes=1024,
            allowed_hosts={"dscan.info"},
        )

    assert ok is True
    assert status == 200
    assert text == final_body.decode("utf-8")
    assert [c["url"] for c in calls] == [
        "https://dscan.info/v/abc",
        "https://dscan.info/v/abc/",
    ]
    # Every hop must keep allow_redirects=False — manual validation is the point.
    for c in calls:
        assert c["kwargs"]["allow_redirects"] is False
    assert redirect.closed is True
    assert final.closed is True


def test_fetch_rejects_302_to_non_allowlisted_host():
    evil = _FakeResponse(
        status_code=302,
        headers={"Location": "https://evil.example/steal"},
        chunks=[],
    )
    # If the guard works, the evil URL is never requested.
    responses = {"https://dscan.info/v/abc": evil}
    get_fn, calls = _make_sequential_get(responses)

    with patch.object(fc_web.http_requests, "get", side_effect=get_fn):
        ok, status, text = fc_web._fetch_with_size_cap(
            "https://dscan.info/v/abc",
            max_bytes=1024,
            allowed_hosts={"dscan.info"},
        )

    assert ok is False
    assert status == 302
    assert text is None
    assert [c["url"] for c in calls] == ["https://dscan.info/v/abc"]
    assert evil.closed is True


def test_fetch_rejects_302_with_http_scheme_target():
    # Allowlisted host, but downgraded to http:// — must be rejected.
    downgrade = _FakeResponse(
        status_code=302,
        headers={"Location": "http://dscan.info/v/abc/"},
        chunks=[],
    )
    get_fn, calls = _make_sequential_get({"https://dscan.info/v/abc": downgrade})

    with patch.object(fc_web.http_requests, "get", side_effect=get_fn):
        ok, status, text = fc_web._fetch_with_size_cap(
            "https://dscan.info/v/abc",
            max_bytes=1024,
            allowed_hosts={"dscan.info"},
        )

    assert ok is False
    assert status == 302
    assert text is None
    assert [c["url"] for c in calls] == ["https://dscan.info/v/abc"]
    assert downgrade.closed is True


def test_fetch_aborts_redirect_chain_longer_than_three_hops():
    # Build a chain: a -> b -> c -> d -> e. Four redirects in a row exceeds
    # the 3-hop cap, so the fourth Location must not be followed.
    def _redir(to):
        return _FakeResponse(status_code=302, headers={"Location": to}, chunks=[])

    responses = {
        "https://dscan.info/a": _redir("https://dscan.info/b"),
        "https://dscan.info/b": _redir("https://dscan.info/c"),
        "https://dscan.info/c": _redir("https://dscan.info/d"),
        "https://dscan.info/d": _redir("https://dscan.info/e"),
        # /e is intentionally not in responses — if we reached it, the mock raises.
    }
    get_fn, calls = _make_sequential_get(responses)

    with patch.object(fc_web.http_requests, "get", side_effect=get_fn):
        ok, status, text = fc_web._fetch_with_size_cap(
            "https://dscan.info/a",
            max_bytes=1024,
            allowed_hosts={"dscan.info"},
            max_redirects=3,
        )

    assert ok is False
    assert status == 302
    assert text is None
    # Exactly 4 GETs: original + 3 hops; the 4th redirect target must NOT be fetched.
    assert [c["url"] for c in calls] == [
        "https://dscan.info/a",
        "https://dscan.info/b",
        "https://dscan.info/c",
        "https://dscan.info/d",
    ]


# ── Module-level cap sanity check ───────────────────────────────────────────

def test_module_cap_is_one_megabyte():
    assert fc_web.DSCAN_MAX_FETCH_BYTES == 1024 * 1024
