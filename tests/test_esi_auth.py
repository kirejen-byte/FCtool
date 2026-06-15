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
    # Use 6+ char names so they pass the 3-37 length filter.
    names = [f"Pilot{i}" for i in range(1500)]
    out = auth.resolve_names_to_ids(names)
    # New chunk size is 500.
    assert seen_batches == [500, 500, 500]
    assert len(out) == 1500


def test_resolve_names_to_ids_filters_short_names(tmp_path, monkeypatch):
    """Names < 3 chars are dropped before sending to ESI."""
    auth = _make_auth(tmp_path)
    captured: list[list[str]] = []

    def fake_post(path, body, **kw):
        captured.append(list(body))
        return {
            "characters": [
                {"name": n, "id": i} for i, n in enumerate(body, start=1)
            ]
        }

    monkeypatch.setattr(
        ESIAuth, "esi_post_public", staticmethod(fake_post)
    )
    out = auth.resolve_names_to_ids(["Bo", "Alice", "X", "Bobby"])
    # Bo (2) and X (1) dropped; only Alice and Bobby reach ESI.
    assert captured == [["Alice", "Bobby"]]
    assert "Alice" in out and "Bobby" in out
    assert "Bo" not in out and "X" not in out


def test_resolve_names_to_ids_dedupes(tmp_path, monkeypatch):
    """Duplicates are dropped before sending."""
    auth = _make_auth(tmp_path)
    captured: list[list[str]] = []

    def fake_post(path, body, **kw):
        captured.append(list(body))
        return {"characters": [{"name": n, "id": 1} for n in body]}

    monkeypatch.setattr(
        ESIAuth, "esi_post_public", staticmethod(fake_post)
    )
    out = auth.resolve_names_to_ids(
        ["Alice", "Bob", "Alice", "Bob", "Alice"]
    )
    assert captured == [["Alice", "Bob"]]
    assert "Alice" in out and "Bob" in out


def test_resolve_names_to_ids_recovers_via_split(tmp_path, monkeypatch):
    """If a batch returns None (simulating 400), it splits and retries."""
    auth = _make_auth(tmp_path)
    call_log: list[list[str]] = []

    def fake_post(path, body, **kw):
        call_log.append(list(body))
        # Fail on a specific size threshold to force a split.
        if len(body) >= 4:
            return None  # simulate 400
        return {
            "characters": [
                {"name": n, "id": 100 + i} for i, n in enumerate(body)
            ]
        }

    monkeypatch.setattr(
        ESIAuth, "esi_post_public", staticmethod(fake_post)
    )
    # Auth fallback also fails so we exercise the split path.
    monkeypatch.setattr(
        auth, "esi_post", lambda p, b, **kw: None, raising=False
    )
    names = ["Alice", "Bob", "Carol", "Dave"]  # 4 names → split to 2+2.
    out = auth.resolve_names_to_ids(names)
    # All 4 should resolve via split-and-retry.
    assert len(out) == 4
    # One initial 4-name attempt that fails, then two 2-name retries.
    assert [len(c) for c in call_log] == [4, 2, 2]


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
    # Use 3+ char names so they pass the new length filter (3-37 chars).
    out = auth.resolve_names_to_ids(["Xen", "Yon"])
    assert out == {"Xen": 100, "Yon": 101}
    assert captured_bodies == [["Xen", "Yon"]]


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


# ── Categorized ID resolution (resolve_ids / resolve_* convenience) ──────────


def _make_authed_for_search(tmp_path, character_id=42):
    """Build an ESIAuth that reports as authenticated with a valid (non-expired)
    access token so _any_authenticated_auth() returns self via the fast path."""
    auth = _make_auth(tmp_path)
    auth._character_id = character_id
    auth._character_name = "Search Pilot"
    auth._refresh_token = "refresh-token"
    auth._access_token = "valid-access-token"
    auth._expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)
    return auth


def test_resolve_ids_categorizes_all_buckets(tmp_path, monkeypatch):
    """/universe/ids/ response is split into the five requested categories,
    each a list of {id, name}."""
    auth = _make_auth(tmp_path)
    captured: list[list[str]] = []

    def fake_post(path, body, **kw):
        captured.append(list(body))
        return {
            "alliances": [{"id": 99005338, "name": "Pandemic Horde"}],
            "corporations": [{"id": 98388312, "name": "Horde Vanguard"}],
            "regions": [{"id": 10000002, "name": "The Forge"}],
            "systems": [{"id": 30000142, "name": "Jita"}],
            "characters": [{"id": 2112710733, "name": "Securitas Protector"}],
            # An extra category ESI may include but we don't surface:
            "factions": [{"id": 500001, "name": "Caldari State"}],
        }

    monkeypatch.setattr(ESIAuth, "esi_post_public", staticmethod(fake_post))
    out = auth.resolve_ids(
        ["Pandemic Horde", "Horde Vanguard", "The Forge", "Jita",
         "Securitas Protector"]
    )
    assert set(out.keys()) == {
        "alliances", "corporations", "regions", "systems", "characters"
    }
    assert out["alliances"] == [{"id": 99005338, "name": "Pandemic Horde"}]
    assert out["corporations"] == [{"id": 98388312, "name": "Horde Vanguard"}]
    assert out["regions"] == [{"id": 10000002, "name": "The Forge"}]
    assert out["systems"] == [{"id": 30000142, "name": "Jita"}]
    assert out["characters"] == [
        {"id": 2112710733, "name": "Securitas Protector"}
    ]
    # "factions" must NOT leak into the result.
    assert "factions" not in out
    assert captured == [
        ["Pandemic Horde", "Horde Vanguard", "The Forge", "Jita",
         "Securitas Protector"]
    ]


def test_resolve_ids_empty_input_returns_empty_buckets(tmp_path):
    auth = _make_auth(tmp_path)
    out = auth.resolve_ids([])
    assert out == {
        "alliances": [], "corporations": [], "regions": [],
        "systems": [], "characters": [],
    }


def test_resolve_ids_recovers_via_split(tmp_path, monkeypatch):
    """A failing batch (None, simulating a 400) splits in half and retries,
    so one bad name doesn't sink the categorization."""
    auth = _make_auth(tmp_path)
    call_log: list[list[str]] = []

    def fake_post(path, body, **kw):
        call_log.append(list(body))
        if len(body) >= 4:
            return None  # simulate 400 on the big batch
        return {
            "systems": [
                {"id": 30000000 + i, "name": n} for i, n in enumerate(body)
            ]
        }

    monkeypatch.setattr(ESIAuth, "esi_post_public", staticmethod(fake_post))
    monkeypatch.setattr(auth, "esi_post", lambda p, b, **kw: None, raising=False)
    out = auth.resolve_ids(["Jita", "Amarr", "Dodixie", "Rens"])
    assert len(out["systems"]) == 4
    assert [len(c) for c in call_log] == [4, 2, 2]


def test_resolve_ids_falls_back_to_auth_post(tmp_path, monkeypatch):
    """When the public POST returns None, fall back to the authenticated POST."""
    auth = _make_auth(tmp_path)
    monkeypatch.setattr(
        ESIAuth, "esi_post_public", staticmethod(lambda path, body: None)
    )

    def fake_auth_post(path, body, **kw):
        return {"regions": [{"id": 10000002, "name": "The Forge"}]}

    monkeypatch.setattr(auth, "esi_post", fake_auth_post, raising=False)
    out = auth.resolve_ids(["The Forge"])
    assert out["regions"] == [{"id": 10000002, "name": "The Forge"}]


def test_resolve_alliance_exact_match(tmp_path, monkeypatch):
    auth = _make_auth(tmp_path)
    monkeypatch.setattr(
        ESIAuth, "esi_post_public",
        staticmethod(lambda path, body: {
            "alliances": [{"id": 99005338, "name": "Pandemic Horde"}]
        }),
    )
    assert auth.resolve_alliance("Pandemic Horde") == {
        "id": 99005338, "name": "Pandemic Horde"
    }


def test_resolve_alliance_case_insensitive(tmp_path, monkeypatch):
    """ESI matches case-insensitively; we return its canonical-cased name."""
    auth = _make_auth(tmp_path)
    monkeypatch.setattr(
        ESIAuth, "esi_post_public",
        staticmethod(lambda path, body: {
            "alliances": [{"id": 99005338, "name": "Pandemic Horde"}]
        }),
    )
    res = auth.resolve_alliance("pandemic horde")
    assert res == {"id": 99005338, "name": "Pandemic Horde"}


def test_resolve_alliance_miss_returns_none(tmp_path, monkeypatch):
    """No alliance bucket / no match -> None."""
    auth = _make_auth(tmp_path)
    monkeypatch.setattr(
        ESIAuth, "esi_post_public",
        staticmethod(lambda path, body: {"corporations": [
            {"id": 1, "name": "Some Corp"}
        ]}),
    )
    assert auth.resolve_alliance("Nonexistent Alliance") is None


def test_resolve_alliance_empty_name_returns_none(tmp_path, monkeypatch):
    auth = _make_auth(tmp_path)
    # Should never hit the network for empty input.
    monkeypatch.setattr(
        ESIAuth, "esi_post_public",
        staticmethod(lambda path, body: pytest.fail("should not be called")),
    )
    assert auth.resolve_alliance("") is None
    assert auth.resolve_alliance("   ") is None


def test_resolve_corporation_exact_match(tmp_path, monkeypatch):
    auth = _make_auth(tmp_path)
    monkeypatch.setattr(
        ESIAuth, "esi_post_public",
        staticmethod(lambda path, body: {
            "corporations": [{"id": 98388312, "name": "Horde Vanguard"}]
        }),
    )
    assert auth.resolve_corporation("Horde Vanguard") == {
        "id": 98388312, "name": "Horde Vanguard"
    }


def test_resolve_corporation_miss_returns_none(tmp_path, monkeypatch):
    auth = _make_auth(tmp_path)
    monkeypatch.setattr(
        ESIAuth, "esi_post_public",
        staticmethod(lambda path, body: {}),
    )
    assert auth.resolve_corporation("No Such Corp") is None


def test_resolve_region_exact_match(tmp_path, monkeypatch):
    auth = _make_auth(tmp_path)
    monkeypatch.setattr(
        ESIAuth, "esi_post_public",
        staticmethod(lambda path, body: {
            "regions": [{"id": 10000002, "name": "The Forge"}]
        }),
    )
    assert auth.resolve_region("The Forge") == {
        "id": 10000002, "name": "The Forge"
    }


def test_resolve_region_miss_returns_none(tmp_path, monkeypatch):
    auth = _make_auth(tmp_path)
    monkeypatch.setattr(
        ESIAuth, "esi_post_public",
        staticmethod(lambda path, body: {"systems": [
            {"id": 30000142, "name": "Jita"}
        ]}),
    )
    assert auth.resolve_region("Nowhere") is None


# ── search_entities (authed /search/ + public /universe/names/) ──────────────


def test_search_entities_parses_ids_and_names(tmp_path, monkeypatch):
    """Authed /search/ returns category->ids; we resolve names and return
    {id, name, category} ordered by requested category."""
    auth = _make_authed_for_search(tmp_path)

    def fake_get(path, params=None, **kw):
        assert path == f"/characters/{auth._character_id}/search/"
        assert params["categories"] == "alliance,corporation"
        assert params["search"] == "horde"
        assert params["strict"] == "false"
        return {
            "alliance": [99005338],
            "corporation": [98388312, 98388313],
        }

    monkeypatch.setattr(auth, "esi_get", fake_get)

    def fake_names(path, body, **kw):
        assert path == "/universe/names/"
        mapping = {
            99005338: "Pandemic Horde",
            98388312: "Horde Vanguard",
            98388313: "Horde Reloaded",
        }
        return [{"id": i, "name": mapping[i], "category": "x"} for i in body]

    monkeypatch.setattr(ESIAuth, "esi_post_public", staticmethod(fake_names))

    out = auth.search_entities("horde", categories=["alliance", "corporation"])
    assert out == [
        {"id": 99005338, "name": "Pandemic Horde", "category": "alliance"},
        {"id": 98388312, "name": "Horde Vanguard", "category": "corporation"},
        {"id": 98388313, "name": "Horde Reloaded", "category": "corporation"},
    ]


def test_search_entities_default_categories(tmp_path, monkeypatch):
    """Default categories are alliance + corporation."""
    auth = _make_authed_for_search(tmp_path)
    seen_params = {}

    def fake_get(path, params=None, **kw):
        seen_params.update(params)
        return {"alliance": [1]}

    monkeypatch.setattr(auth, "esi_get", fake_get)
    monkeypatch.setattr(
        ESIAuth, "esi_post_public",
        staticmethod(lambda path, body: [{"id": 1, "name": "Alpha"}]),
    )
    out = auth.search_entities("alph")
    assert seen_params["categories"] == "alliance,corporation"
    assert out == [{"id": 1, "name": "Alpha", "category": "alliance"}]


def test_search_entities_caps_at_20(tmp_path, monkeypatch):
    """No more than ~20 results are returned/resolved."""
    auth = _make_authed_for_search(tmp_path)
    monkeypatch.setattr(
        auth, "esi_get",
        lambda path, params=None, **kw: {"corporation": list(range(1, 101))},
    )

    captured_bodies = []

    def fake_names(path, body, **kw):
        captured_bodies.append(list(body))
        return [{"id": i, "name": f"Corp{i}"} for i in body]

    monkeypatch.setattr(ESIAuth, "esi_post_public", staticmethod(fake_names))
    out = auth.search_entities("corp", categories=["corporation"])
    assert len(out) == 20
    # Only the first 20 ids were sent to /universe/names/.
    assert captured_bodies == [list(range(1, 21))]


def test_search_entities_empty_query_returns_empty(tmp_path, monkeypatch):
    auth = _make_authed_for_search(tmp_path)
    monkeypatch.setattr(
        auth, "esi_get",
        lambda *a, **k: pytest.fail("should not call ESI for empty query"),
    )
    assert auth.search_entities("") == []
    assert auth.search_entities("   ") == []


def test_search_entities_no_auth_returns_empty(tmp_path, monkeypatch):
    """No authenticated character available -> [] gracefully (no crash)."""
    import glob as _glob

    auth = _make_auth(tmp_path)  # not authenticated
    auth._refresh_token = None
    # Ensure the sibling-token scan (which does a local `import glob`) finds
    # nothing — patch the real glob module's glob function.
    monkeypatch.setattr(_glob, "glob", lambda pattern: [])
    # Also guard: even if it somehow proceeds, esi_get must not be reached.
    monkeypatch.setattr(
        auth, "esi_get",
        lambda *a, **k: pytest.fail("must not query search without auth"),
    )
    assert auth.search_entities("horde") == []


def test_search_entities_search_network_error_returns_empty(tmp_path, monkeypatch):
    """If the authed search returns None (network/scope error), return []."""
    auth = _make_authed_for_search(tmp_path)
    monkeypatch.setattr(auth, "esi_get", lambda path, params=None, **kw: None)
    assert auth.search_entities("horde") == []


def test_search_entities_names_failure_returns_empty(tmp_path, monkeypatch):
    """If /universe/names/ fails on both public and auth paths, return []."""
    auth = _make_authed_for_search(tmp_path)
    monkeypatch.setattr(
        auth, "esi_get",
        lambda path, params=None, **kw: {"alliance": [99005338]},
    )
    monkeypatch.setattr(
        ESIAuth, "esi_post_public", staticmethod(lambda path, body: None)
    )
    monkeypatch.setattr(auth, "esi_post", lambda path, body, **kw: None, raising=False)
    assert auth.search_entities("horde") == []


def test_search_entities_drops_ids_without_names(tmp_path, monkeypatch):
    """Ids that /universe/names/ doesn't resolve are dropped, not emitted with
    blank names."""
    auth = _make_authed_for_search(tmp_path)
    monkeypatch.setattr(
        auth, "esi_get",
        lambda path, params=None, **kw: {"alliance": [1, 2]},
    )
    # Only id=1 resolves.
    monkeypatch.setattr(
        ESIAuth, "esi_post_public",
        staticmethod(lambda path, body: [{"id": 1, "name": "Alpha"}]),
    )
    out = auth.search_entities("a", categories=["alliance"])
    assert out == [{"id": 1, "name": "Alpha", "category": "alliance"}]


# ── _first_exact defensive hardening (malformed entry dicts) ─────────────────


def test_first_exact_exact_case_insensitive_match():
    """Case-insensitive name match returns the canonical {id, name}."""
    entries = [
        {"id": 1, "name": "Some Corp"},
        {"id": 99005338, "name": "Pandemic Horde"},
    ]
    assert ESIAuth._first_exact(entries, "pandemic horde") == {
        "id": 99005338, "name": "Pandemic Horde"
    }


def test_first_exact_fallback_returns_first_valid_entry():
    """No exact match -> first well-formed entry (ESI matched something)."""
    entries = [
        {"id": 10000002, "name": "The Forge"},
        {"id": 30000142, "name": "Jita"},
    ]
    assert ESIAuth._first_exact(entries, "Nonexistent") == {
        "id": 10000002, "name": "The Forge"
    }


def test_first_exact_empty_list_returns_none():
    assert ESIAuth._first_exact([], "Anything") is None


def test_first_exact_entry_missing_id_is_skipped():
    """An entry without an 'id' must not raise; it's skipped. As the only
    entry, the result is None (no valid fallback)."""
    entries = [{"name": "Pandemic Horde"}]
    assert ESIAuth._first_exact(entries, "Pandemic Horde") is None


def test_first_exact_entry_missing_name_is_skipped():
    """An entry without a string 'name' is skipped in both the exact scan and
    the fallback."""
    entries = [{"id": 99005338}]
    assert ESIAuth._first_exact(entries, "Pandemic Horde") is None


def test_first_exact_skips_malformed_to_reach_valid_match():
    """Malformed entries (missing id / missing name) are skipped so a later
    valid exact match still wins."""
    entries = [
        {"name": "Pandemic Horde"},          # missing id
        {"id": 98388312},                    # missing name
        {"id": 99005338, "name": "Pandemic Horde"},  # valid exact match
    ]
    assert ESIAuth._first_exact(entries, "pandemic horde") == {
        "id": 99005338, "name": "Pandemic Horde"
    }


def test_first_exact_fallback_skips_malformed_entries():
    """With no exact match, the fallback skips malformed leading entries and
    returns the first VALID one."""
    entries = [
        {"name": "Broken"},                  # missing id
        {"id": 0, "name": "Zero Id"},        # falsy id -> not valid
        {"id": 30000142, "name": "Jita"},    # first valid entry
    ]
    assert ESIAuth._first_exact(entries, "Nonexistent") == {
        "id": 30000142, "name": "Jita"
    }
