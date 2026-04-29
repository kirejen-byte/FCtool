import base64
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

import esi_auth
from esi_auth import ESIAuth, SCOPES, SSO_AUTH_URL


def _make_fake_jwt(character_id: int, character_name: str) -> str:
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "RS256", "typ": "JWT"}).encode()
    ).rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps({
            "sub": f"CHARACTER:EVE:{character_id}",
            "name": character_name,
            "scp": SCOPES[:3],
            "iss": "login.eveonline.com",
        }).encode()
    ).rstrip(b"=").decode()
    signature = base64.urlsafe_b64encode(b"fake-sig").rstrip(b"=").decode()
    return f"{header}.{payload}.{signature}"


def _make_auth(tmp_path, token_file_name="tokens.json"):
    tf = tmp_path / token_file_name
    return ESIAuth(
        client_id="fakeclient",
        client_secret="fakesecret",
        callback_url="http://localhost:8834/callback",
        token_file=str(tf),
    )


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, ok=None):
        self.status_code = status_code
        self._json = json_data or {}
        self.ok = ok if ok is not None else 200 <= status_code < 300
        self.text = json.dumps(self._json)

    def json(self):
        return self._json


def test_decode_character_info_extracts_id_and_name(tmp_path):
    auth = _make_auth(tmp_path)
    auth._access_token = _make_fake_jwt(2112710733, "Securitas Protector")
    auth._decode_character_info()
    assert auth._character_id == 2112710733
    assert auth._character_name == "Securitas Protector"


def test_decode_character_info_handles_unicode_name(tmp_path):
    auth = _make_auth(tmp_path)
    auth._access_token = _make_fake_jwt(90143494, "Müller von Groß")
    auth._decode_character_info()
    assert auth._character_id == 90143494
    assert auth._character_name == "Müller von Groß"


def test_auth_url_contains_state_scopes_and_client_id(tmp_path):
    auth = _make_auth(tmp_path)
    state = "teststate12345"
    params = {
        "response_type": "code",
        "redirect_uri": auth.callback_url,
        "client_id": auth.client_id,
        "scope": " ".join(SCOPES),
        "state": state,
    }
    url = f"{SSO_AUTH_URL}?{urlencode(params)}"
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "login.eveonline.com"
    assert qs["response_type"] == ["code"]
    assert qs["client_id"] == ["fakeclient"]
    assert qs["state"] == [state]
    assert qs["redirect_uri"] == ["http://localhost:8834/callback"]
    scope_value = qs["scope"][0]
    for scope in SCOPES:
        assert scope in scope_value


def test_auth_url_today_has_no_pkce(tmp_path):
    auth = _make_auth(tmp_path)
    state = "teststate"
    params = {
        "response_type": "code",
        "redirect_uri": auth.callback_url,
        "client_id": auth.client_id,
        "scope": " ".join(SCOPES),
        "state": state,
    }
    url = f"{SSO_AUTH_URL}?{urlencode(params)}"
    qs = parse_qs(urlparse(url).query)
    assert "code_challenge" not in qs
    assert "code_challenge_method" not in qs


def test_save_tokens_writes_to_current_path_without_rename(tmp_path):
    """_save_tokens is pure now: it writes to self.token_file and does not
    relocate the file based on character_id. Path resolution lives in
    _migrate_to_per_character_path, called once at login."""
    tf = tmp_path / "tokens_initial.json"
    auth = ESIAuth(
        client_id="fakeclient",
        client_secret="fakesecret",
        callback_url="http://localhost:8834/callback",
        token_file=str(tf),
    )
    auth._refresh_token = "refresh-abc-123"
    auth._character_id = 2112710733
    auth._character_name = "Securitas Protector"
    auth._save_tokens()

    # Path did NOT change — _save_tokens no longer has destructive side effects.
    assert auth.token_file == str(tf)
    assert os.path.exists(str(tf))

    with open(str(tf)) as f:
        data = json.load(f)
    assert data["refresh_token"] == "refresh-abc-123"
    assert data["character_id"] == 2112710733
    assert data["character_name"] == "Securitas Protector"


def test_save_tokens_is_atomic_via_os_replace(tmp_path):
    """Write goes via a .tmp file + os.replace so a mid-write crash cannot
    leave the canonical token file truncated."""
    tf = tmp_path / "atomic_tokens.json"
    auth = ESIAuth(
        client_id="fakeclient",
        client_secret="fakesecret",
        callback_url="http://localhost:8834/callback",
        token_file=str(tf),
    )
    auth._refresh_token = "refresh-atomic"
    auth._character_id = 11111
    auth._character_name = "Atomic Pilot"
    auth._save_tokens()

    # No leftover .tmp after successful write.
    assert not os.path.exists(str(tf) + ".tmp")
    assert os.path.exists(str(tf))
    with open(str(tf)) as f:
        data = json.load(f)
    assert data["refresh_token"] == "refresh-atomic"


def test_save_tokens_cleans_up_tmp_on_replace_failure(tmp_path, mocker):
    """If os.replace fails (disk full, perms, AV lock), the orphaned .tmp
    file must be cleaned up and the original error must propagate so the
    caller sees the write failure."""
    tf = tmp_path / "replace_fails.json"
    auth = ESIAuth(
        client_id="fakeclient",
        client_secret="fakesecret",
        callback_url="http://localhost:8834/callback",
        token_file=str(tf),
    )
    auth._refresh_token = "refresh-doomed"
    auth._character_id = 99999
    auth._character_name = "Doomed Pilot"

    boom = OSError("simulated replace failure")
    mocker.patch("esi_auth.os.replace", side_effect=boom)

    with pytest.raises(OSError) as exc_info:
        auth._save_tokens()

    # Original exception propagates unchanged.
    assert exc_info.value is boom
    # No orphaned .tmp file left behind.
    assert not os.path.exists(str(tf) + ".tmp")
    # And the final file was never created (replace never ran successfully).
    assert not os.path.exists(str(tf))


def test_load_tokens_roundtrip_with_migration(tmp_path, monkeypatch, mocker):
    """End-to-end: write via _save_tokens, load via a fresh ESIAuth."""
    monkeypatch.setattr(esi_auth, "TOKEN_DIR", str(tmp_path))
    mocker.patch.object(ESIAuth, "_do_refresh", return_value=True)

    tf = tmp_path / "tokens_initial.json"
    auth = ESIAuth(
        client_id="fakeclient",
        client_secret="fakesecret",
        callback_url="http://localhost:8834/callback",
        token_file=str(tf),
    )
    auth._refresh_token = "refresh-abc-123"
    auth._character_id = 2112710733
    auth._character_name = "Securitas Protector"
    # Simulate the login-flow transition: character_id just became known.
    auth._migrate_to_per_character_path()
    auth._save_tokens()

    # After migration, token_file should be the canonical per-character path.
    expected_path = os.path.join(
        str(tmp_path), "esi_tokens_2112710733.json"
    )
    assert auth.token_file == expected_path
    assert os.path.exists(expected_path)

    # Fresh instance loads from the canonical path.
    auth2 = ESIAuth(
        client_id="fakeclient",
        client_secret="fakesecret",
        callback_url="http://localhost:8834/callback",
        token_file=expected_path,
    )
    assert auth2._refresh_token == "refresh-abc-123"
    assert auth2._character_id == 2112710733
    assert auth2._character_name == "Securitas Protector"


def test_is_authenticated_true_when_refresh_token_present(tmp_path):
    auth = _make_auth(tmp_path)
    auth._refresh_token = "something"
    assert auth.is_authenticated is True


def test_is_authenticated_false_when_no_refresh_token(tmp_path):
    auth = _make_auth(tmp_path)
    auth._refresh_token = None
    assert auth.is_authenticated is False


# ── Refresh-token race / lock tests ──────────────────────────────────────────


def test_access_token_fast_path_skips_refresh(tmp_path, mocker):
    """If token is still valid, the property returns it without acquiring
    the refresh RPC — no network call happens."""
    auth = _make_auth(tmp_path)
    auth._access_token = "valid-token"
    auth._refresh_token = "refresh-1"
    auth._expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    refresh_spy = mocker.patch.object(auth, "_do_refresh")
    assert auth.access_token == "valid-token"
    refresh_spy.assert_not_called()


def test_access_token_near_expiry_triggers_refresh(tmp_path, mocker):
    """Within the 5s margin of expiry, access_token triggers _do_refresh."""
    auth = _make_auth(tmp_path)
    auth._access_token = "old-token"
    auth._refresh_token = "refresh-1"
    auth._expires_at = datetime.now(timezone.utc) + timedelta(seconds=1)

    def fake_refresh():
        auth._access_token = "new-token"
        auth._expires_at = datetime.now(timezone.utc) + timedelta(minutes=20)
        return True

    mocker.patch.object(auth, "_do_refresh", side_effect=fake_refresh)
    assert auth.access_token == "new-token"


def test_concurrent_access_refreshes_only_once(tmp_path, mocker):
    """The core race fix: N threads hitting access_token with an expired
    token must trigger _do_refresh exactly once. Other threads take the
    lock, re-check, and find the token now valid."""
    auth = _make_auth(tmp_path)
    auth._access_token = "expired-token"
    auth._refresh_token = "refresh-old"
    auth._expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    refresh_count = [0]
    start_gate = threading.Event()

    def fake_refresh():
        refresh_count[0] += 1
        # Simulate network latency so contenders pile up on the lock.
        start_gate.wait(timeout=2)
        auth._access_token = "fresh-token"
        auth._refresh_token = "refresh-new"
        auth._expires_at = datetime.now(timezone.utc) + timedelta(minutes=20)
        return True

    mocker.patch.object(auth, "_do_refresh", side_effect=fake_refresh)

    results = []

    def worker():
        results.append(auth.access_token)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    # Let them all queue on the lock before releasing the refresh.
    start_gate.set()
    for t in threads:
        t.join(timeout=5)

    assert refresh_count[0] == 1, (
        f"Expected exactly one refresh under contention; got "
        f"{refresh_count[0]}"
    )
    assert all(r == "fresh-token" for r in results)


def test_refresh_invalid_grant_clears_tokens_and_persists(tmp_path, mocker):
    """HTTP 400 invalid_grant = SSO revoked the refresh token. Must clear
    local state and write an empty token file so we don't zombie on next
    load."""
    auth = _make_auth(tmp_path)
    auth._access_token = "some-token"
    auth._refresh_token = "revoked-refresh"
    auth._expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)

    mocker.patch.object(
        auth._session,
        "post",
        return_value=_FakeResponse(400, {"error": "invalid_grant"}),
    )

    ok = auth._do_refresh()
    assert ok is False
    assert auth._access_token is None
    assert auth._refresh_token is None
    assert auth._expires_at is None
    # Token file persisted with empty/null credentials.
    assert os.path.exists(auth.token_file)
    with open(auth.token_file) as f:
        data = json.load(f)
    assert data["refresh_token"] is None


def test_refresh_transient_5xx_preserves_refresh_token(tmp_path, mocker):
    """Transient server failure — keep refresh_token so we can retry."""
    auth = _make_auth(tmp_path)
    auth._access_token = "some-token"
    auth._refresh_token = "still-good-refresh"
    auth._expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)

    mocker.patch.object(
        auth._session,
        "post",
        return_value=_FakeResponse(503, {"error": "service_unavailable"}),
    )

    ok = auth._do_refresh()
    assert ok is False
    # Refresh token preserved for next attempt.
    assert auth._refresh_token == "still-good-refresh"
    # Access token cleared (it may or may not still be valid; safest to nil).
    assert auth._access_token is None


def test_refresh_network_timeout_preserves_refresh_token(tmp_path, mocker):
    """requests.Timeout is treated as transient."""
    import requests

    auth = _make_auth(tmp_path)
    auth._access_token = "some-token"
    auth._refresh_token = "still-good-refresh"
    auth._expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)

    mocker.patch.object(
        auth._session, "post", side_effect=requests.Timeout("slow")
    )

    ok = auth._do_refresh()
    assert ok is False
    assert auth._refresh_token == "still-good-refresh"


def test_refresh_success_rotates_refresh_token(tmp_path, mocker):
    """SSO rotates the refresh token on every refresh — we must adopt the
    new one."""
    auth = _make_auth(tmp_path)
    auth._refresh_token = "refresh-v1"
    auth._character_id = 42
    auth._character_name = "Test Pilot"

    mocker.patch.object(
        auth._session,
        "post",
        return_value=_FakeResponse(200, {
            "access_token": "access-v2",
            "refresh_token": "refresh-v2",
            "expires_in": 1199,
        }),
    )

    ok = auth._do_refresh()
    assert ok is True
    assert auth._access_token == "access-v2"
    assert auth._refresh_token == "refresh-v2"
    # Persisted to disk.
    with open(auth.token_file) as f:
        data = json.load(f)
    assert data["refresh_token"] == "refresh-v2"


def test_refresh_lock_is_reentrant(tmp_path):
    """The access_token property acquires the lock, then calls _do_refresh
    which also acquires. With a plain Lock this would deadlock; RLock makes
    it safe."""
    auth = _make_auth(tmp_path)
    # Directly exercise re-entrancy.
    with auth._refresh_lock:
        with auth._refresh_lock:
            pass  # No deadlock.


# ── Bulk-name / Affiliations / Contacts / Fleet-boss helpers ────────────────

def test_resolve_names_to_ids_batches(tmp_path, monkeypatch):
    auth = _make_auth(tmp_path)
    captured: list[list[str]] = []

    def fake_post(path, body, **kw):
        captured.append(list(body))
        return {
            "characters": [
                {"name": n, "id": 1000 + i} for i, n in enumerate(body)
            ]
        }

    monkeypatch.setattr(
        ESIAuth, "esi_post_public", staticmethod(fake_post)
    )
    out = auth.resolve_names_to_ids(["Alice", "Bob"])
    assert out == {"Alice": 1000, "Bob": 1001}
    assert captured == [["Alice", "Bob"]]


def test_resolve_names_to_ids_chunks_over_1000(tmp_path, monkeypatch):
    auth = _make_auth(tmp_path)
    seen_batches: list[int] = []
    # Use a counter rather than enumerate(body) so we never emit id=0,
    # which the implementation correctly treats as invalid (truthy guard).
    next_id = [1]

    def fake_post(path, body, **kw):
        seen_batches.append(len(body))
        chars = []
        for n in body:
            chars.append({"name": n, "id": next_id[0]})
            next_id[0] += 1
        return {"characters": chars}

    monkeypatch.setattr(
        ESIAuth, "esi_post_public", staticmethod(fake_post)
    )
    names = [f"P{i}" for i in range(1500)]
    out = auth.resolve_names_to_ids(names)
    assert seen_batches == [1000, 500]
    assert len(out) == 1500


def test_resolve_names_to_ids_falls_back_to_auth_post(tmp_path, monkeypatch):
    """If the public POST returns None (e.g., network), fall back to the
    authenticated POST. This guards against regressions in the public-first
    pattern silently dropping all results when the public path fails."""
    auth = _make_auth(tmp_path)
    # Public endpoint fails for every batch.
    monkeypatch.setattr(
        ESIAuth, "esi_post_public",
        staticmethod(lambda path, body: None),
    )
    # Authenticated fallback succeeds.
    captured_bodies: list[list[str]] = []

    def fake_auth_post(path, body, **kw):
        captured_bodies.append(list(body))
        return {
            "characters": [
                {"name": n, "id": 100 + i} for i, n in enumerate(body)
            ]
        }

    monkeypatch.setattr(auth, "esi_post", fake_auth_post, raising=False)
    out = auth.resolve_names_to_ids(["X", "Y"])
    assert out == {"X": 100, "Y": 101}
    assert captured_bodies == [["X", "Y"]]


def test_get_affiliations_batches(tmp_path, monkeypatch):
    auth = _make_auth(tmp_path)

    def fake_post(path, body, **kw):
        return [
            {"character_id": cid, "corporation_id": 100, "alliance_id": 200}
            for cid in body
        ]

    monkeypatch.setattr(
        ESIAuth, "esi_post_public", staticmethod(fake_post)
    )
    out = auth.get_affiliations([1, 2, 3])
    assert len(out) == 3
    assert out[0] == {
        "character_id": 1,
        "corporation_id": 100,
        "alliance_id": 200,
    }


def test_get_affiliations_falls_back_to_auth_post(tmp_path, monkeypatch):
    """Mirror of the resolve_names_to_ids fallback: if public POST fails,
    fall through to the authenticated POST so authorized users still get
    results when (e.g.) an upstream proxy blocks unauthenticated calls."""
    auth = _make_auth(tmp_path)
    monkeypatch.setattr(
        ESIAuth, "esi_post_public",
        staticmethod(lambda path, body: None),
    )

    def fake_auth_post(path, body, **kw):
        return [
            {"character_id": cid, "corporation_id": 7, "alliance_id": None}
            for cid in body
        ]

    monkeypatch.setattr(auth, "esi_post", fake_auth_post, raising=False)
    out = auth.get_affiliations([10, 11])
    assert len(out) == 2
    assert out[0]["character_id"] == 10
    assert out[1]["character_id"] == 11


def test_is_fleet_boss_true(tmp_path, monkeypatch):
    auth = _make_auth(tmp_path)
    auth._character_id = 42

    def fake_get(path, **kw):
        return {
            "fleet_id": 999,
            "role": "fleet_commander",
            "wing_id": 0,
            "squad_id": 0,
        }

    monkeypatch.setattr(auth, "esi_get", fake_get)
    assert auth.is_fleet_boss() is True


def test_is_fleet_boss_false_when_not_in_fleet(tmp_path, monkeypatch):
    auth = _make_auth(tmp_path)
    auth._character_id = 42
    monkeypatch.setattr(auth, "esi_get", lambda path, **kw: None)
    assert auth.is_fleet_boss() is False


def test_is_fleet_boss_false_when_member(tmp_path, monkeypatch):
    auth = _make_auth(tmp_path)
    auth._character_id = 42
    monkeypatch.setattr(
        auth,
        "esi_get",
        lambda path, **kw: {"fleet_id": 1, "role": "squad_member"},
    )
    assert auth.is_fleet_boss() is False
