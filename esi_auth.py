"""
EVE SSO OAuth2 Authentication Module
Handles login flow, token storage, and refresh for ESI API access.
"""

import base64
import hashlib
import json
import os
import secrets
import sys
import threading
import time
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlencode, urlparse, parse_qs
from datetime import datetime, timezone, timedelta

import requests

from app_path import app_dir
from app_io import atomic_write_json
from app_log import get_logger
from esi_constants import ESI_BASE, ESI_HEADERS as HEADERS
from rate_limiter import rate_limit

log = get_logger(__name__)

SSO_AUTH_URL = "https://login.eveonline.com/v2/oauth/authorize"
SSO_TOKEN_URL = "https://login.eveonline.com/v2/oauth/token"

TOKEN_FILE = os.path.join(app_dir(), "esi_tokens.json")  # Legacy single-char file
TOKEN_DIR = app_dir()  # Directory for per-character token files

SCOPES = [
    "publicData",
    "esi-location.read_location.v1",
    "esi-location.read_ship_type.v1",
    "esi-search.search_structures.v1",
    "esi-universe.read_structures.v1",
    "esi-fleets.read_fleet.v1",
    "esi-fleets.write_fleet.v1",
    "esi-ui.open_window.v1",
    "esi-ui.write_waypoint.v1",
    "esi-location.read_online.v1",
    "esi-characters.read_contacts.v1",
    "esi-characters.write_contacts.v1",
    "esi-corporations.read_contacts.v1",
    "esi-alliances.read_contacts.v1",
    "esi-characters.read_loyalty.v1",
    "esi-characters.read_chat_channels.v1",
    "esi-characters.read_medals.v1",
    "esi-characters.read_standings.v1",
    "esi-characters.read_agents_research.v1",
    "esi-characters.read_blueprints.v1",
    "esi-characters.read_corporation_roles.v1",
    "esi-characters.read_fatigue.v1",
    "esi-characters.read_notifications.v1",
    "esi-characters.read_titles.v1",
    "esi-characters.read_fw_stats.v1",
    "esi-characters.read_freelance_jobs.v1",
    "esi-structures.read_corporation.v1",
    "esi-structures.read_character.v1",
    "esi-assets.read_assets.v1",
    "esi-fittings.read_fittings.v1",
    "esi-fittings.write_fittings.v1",
    # Market Scanner (design 2026-07-06-market-scanner-design.md §6.3). Adding
    # these forces a RE-AUTH: SSO grants scopes only at login, so every existing
    # character token was minted without them and must be re-consented per
    # character before the citadel-market / corp-contract pulls work. The
    # existing ⚠ missing-scope indicator communicates this (has_scope gating);
    # public region orders + public contracts need NO scope, so the degraded
    # mode works without re-auth.
    "esi-markets.structure_markets.v1",  # citadel market pull
    "esi-contracts.read_character_contracts.v1",  # personal/alliance contract visibility
    "esi-contracts.read_corporation_contracts.v1",  # corp/alliance contracts
]

# Market Scanner scope groups, for has_scope gating helpers (design §6.3). Kept
# as module constants so the GUI and any worker gate features on the same names.
MARKET_STRUCTURE_SCOPE = "esi-markets.structure_markets.v1"
MARKET_CONTRACT_SCOPES = (
    "esi-contracts.read_character_contracts.v1",
    "esi-contracts.read_corporation_contracts.v1",
)

# Static-SDE memo backing ESIAuth._get_region_name(): constellation->region
# and region->name are immutable game data, never re-derived once seen, so
# they are cached permanently at module scope (shared across every ESIAuth
# instance/character for the process lifetime — see B1 in
# OPTIMIZATION_REVIEW.md, ~5,000 wasted calls/hr for a 10-man fleet before
# this). Thread-safety: plain dict get/set with no lock — concurrent racing
# writers can only ever write the SAME value for a given key (idempotent
# lookups), so the benign race needs no synchronization.
_CONSTELLATION_REGION_MEMO: dict[int, int] = {}
_REGION_NAME_MEMO: dict[int, str] = {}


class _CallbackHandler(BaseHTTPRequestHandler):
    """HTTP handler that captures the OAuth callback."""

    auth_code = None
    auth_state = None
    error = None

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if "code" in params:
            _CallbackHandler.auth_code = params["code"][0]
            _CallbackHandler.auth_state = params.get("state", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body style='background:#1a1a2e;color:#00d4ff;"
                b"font-family:monospace;text-align:center;padding-top:100px'>"
                b"<h1>Authentication Successful</h1>"
                b"<p>You can close this window and return to FCTool.</p>"
                b"</body></html>"
            )
        else:
            _CallbackHandler.error = params.get("error", ["unknown"])[0]
            self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body style='background:#1a1a2e;color:#ff4444;"
                b"font-family:monospace;text-align:center;padding-top:100px'>"
                b"<h1>Authentication Failed</h1>"
                b"<p>Please try again from FCTool.</p>"
                b"</body></html>"
            )

    def log_message(self, format, *args):
        pass  # Suppress HTTP log output


class ESIAuth:
    """Manages EVE SSO authentication and token lifecycle."""

    def __init__(self, client_id: str, client_secret: str = "",
                 callback_url: str = "http://localhost:8834/callback",
                 token_file: str = TOKEN_FILE):
        self.client_id = client_id
        # client_secret is optional. A non-empty secret selects the
        # confidential flow (HTTP Basic on token/refresh); a missing or
        # blank secret selects the PKCE public/native flow (no secret).
        self.client_secret = client_secret or ""
        self.callback_url = callback_url
        self.callback_port = int(urlparse(callback_url).port or 8834)
        self.token_file = token_file

        # Dual-mode detection point: PKCE iff no usable client_secret.
        self._use_pkce = not self.client_secret.strip()

        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at: datetime | None = None
        self._character_id: int | None = None
        self._character_name: str | None = None
        # Per-login PKCE code_verifier (set in _login_flow_inner when using
        # the PKCE flow, consumed by _exchange_code, then cleared).
        self._code_verifier: str | None = None

        # Re-entrant lock guarding all token state mutation and refresh RPCs.
        # Re-entrant because _do_refresh() (which acquires) may be called from
        # within the access_token property (which also acquires).
        self._refresh_lock = threading.RLock()

        # Cache for the sibling ESIAuth delegate resolved by
        # _any_authenticated_auth() when *this* instance is unauthenticated.
        # Constructing a sibling ESIAuth fires a live SSO refresh that rotates
        # that character's refresh token, so we must not rebuild one per call
        # (search_entities is driven by debounced type-ahead keystrokes on
        # worker threads). Guarded by _refresh_lock. _delegate_last_fail_ts is a
        # monotonic timestamp used to throttle re-resolution when no sibling
        # authenticated, so a keystroke burst can't trigger a glob+construct
        # storm; a later attempt (after the cooldown) can still pick up a
        # character the user logs in.
        self._delegate_auth: "ESIAuth | None" = None
        self._delegate_last_fail_ts: float = 0.0

        # Short-TTL per-instance memo for get_location()/get_ship_type() (B3
        # in OPTIMIZATION_REVIEW.md). Three independent pollers (preview
        # overlay ~10s, map-chars ~60s, char tab) hit the same character's
        # location/ship on overlapping cadences; ESI's own server-side cache
        # is ~5s, so a short client-side memo collapses duplicate calls
        # inside that window. Each is (monotonic_ts_of_fetch, value); only a
        # successful (non-None) result is ever stored, so a transient error
        # is retried on the very next call instead of being cached.
        self._loc_memo: "tuple[float, dict] | None" = None
        self._ship_memo: "tuple[float, dict] | None" = None

        # Per-instance ETag cache for esi_get_ex (B2): key (path + folded params)
        # -> (etag, parsed_body). On a repeat GET we send If-None-Match; a 304
        # replays the cached body, so the hot location/ship/online/fleet pollers
        # pay ~1 rate-token instead of a full 200's 2 when data is unchanged.
        # Bounded by _ETAG_CACHE_MAX; sits BELOW the get_location/get_ship_type
        # TTL memos. Lock-free by design (GIL-atomic dict ops + idempotent
        # eviction), mirroring fleet_esi/market_esi's ETag caches.
        self._etag_cache: "dict[str, tuple]" = {}

        self._session = requests.Session()
        self._session.headers.update(HEADERS)

        self._load_tokens()

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def is_authenticated(self) -> bool:
        return self._refresh_token is not None

    @property
    def character_id(self) -> int | None:
        return self._character_id

    @property
    def character_name(self) -> str | None:
        return self._character_name

    @property
    def access_token(self) -> str | None:
        # Double-checked locking: fast path first (no lock contention for
        # the common case of a still-valid token), then re-check under lock
        # before actually refreshing.
        margin = timedelta(seconds=5)
        now = datetime.now(timezone.utc)
        if self._access_token and self._expires_at and now + margin < self._expires_at:
            return self._access_token
        # Expired or near-expiry: take the lock and re-check.
        with self._refresh_lock:
            now = datetime.now(timezone.utc)
            if self._access_token and self._expires_at and now + margin < self._expires_at:
                # Another thread refreshed while we waited.
                return self._access_token
            if self._refresh_token:
                self._do_refresh()
                return self._access_token
            return None

    # ── PKCE Helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _generate_code_verifier() -> str:
        """Generate a PKCE code_verifier: base64url of 32 random bytes with
        trailing '=' padding stripped (RFC 7636 / CCP native SSO)."""
        return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")

    @staticmethod
    def _code_challenge_for(verifier: str) -> str:
        """Derive the S256 PKCE code_challenge: base64url of the SHA-256 of
        the verifier, padding stripped. The hash is taken over the ASCII
        bytes of the verifier string per RFC 7636."""
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    # ── Login Flow ────────────────────────────────────────────────────────────

    def login(self, on_complete: callable = None):
        """Start the SSO login flow in a background thread."""
        thread = threading.Thread(
            target=self._login_flow, args=(on_complete,), daemon=True
        )
        thread.start()

    def _login_flow(self, on_complete: callable = None):
        """Run the full OAuth2 authorization code flow."""
        try:
            self._login_flow_inner(on_complete)
        except Exception as e:
            print(f"[ESI Auth] Login flow crashed: {e}")
            # Log to file for debugging frozen builds
            try:
                log_path = os.path.join(app_dir(), "esi_auth_error.log")
                with open(log_path, "a") as f:
                    import traceback
                    f.write(f"{datetime.now()} Login flow error:\n")
                    traceback.print_exc(file=f)
                    f.write("\n")
            except Exception:
                pass
            if on_complete:
                on_complete(False, f"Login error: {e}")

    def _login_flow_inner(self, on_complete: callable = None):
        """Run the full OAuth2 authorization code flow (inner)."""
        state = secrets.token_urlsafe(32)

        # PKCE: generate a fresh verifier/challenge for this login attempt.
        # Stored on the instance so _exchange_code can present the verifier.
        # In confidential mode these stay None and no PKCE params are sent.
        self._code_verifier = None
        code_challenge = None
        if self._use_pkce:
            self._code_verifier = self._generate_code_verifier()
            code_challenge = self._code_challenge_for(self._code_verifier)

        # Reset handler state
        _CallbackHandler.auth_code = None
        _CallbackHandler.auth_state = None
        _CallbackHandler.error = None

        # Start local HTTP server
        try:
            server = HTTPServer(("127.0.0.1", self.callback_port), _CallbackHandler)
        except OSError as e:
            print(f"[ESI Auth] Could not start callback server: {e}")
            if on_complete:
                on_complete(False, f"Port {self.callback_port} in use")
            return

        # Build auth URL and open browser
        params = {
            "response_type": "code",
            "redirect_uri": self.callback_url,
            "client_id": self.client_id,
            "scope": " ".join(SCOPES),
            "state": state,
        }
        if self._use_pkce:
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"
        auth_url = f"{SSO_AUTH_URL}?{urlencode(params)}"
        webbrowser.open(auth_url)
        print("[ESI Auth] Opened browser for EVE login...")

        # Wait for callback (timeout after 120s)
        server.timeout = 120
        server.handle_request()
        server.server_close()

        if _CallbackHandler.error:
            print(f"[ESI Auth] Error: {_CallbackHandler.error}")
            if on_complete:
                on_complete(False, _CallbackHandler.error)
            return

        if not _CallbackHandler.auth_code:
            print("[ESI Auth] No auth code received (timeout?)")
            if on_complete:
                on_complete(False, "Timed out waiting for login")
            return

        if _CallbackHandler.auth_state != state:
            print("[ESI Auth] State mismatch — possible CSRF attack")
            if on_complete:
                on_complete(False, "State mismatch")
            return

        # Exchange code for tokens
        success = self._exchange_code(_CallbackHandler.auth_code)
        if success:
            # character_id is known now; move token_file from the temp/legacy
            # path to the per-character canonical path once, then save.
            self._migrate_to_per_character_path()
            self._save_tokens()
            print(f"[ESI Auth] Logged in as {self._character_name} "
                  f"(ID: {self._character_id})")

        if on_complete:
            on_complete(success, self._character_name if success else "Token exchange failed")

    def _exchange_code(self, code: str) -> bool:
        """Exchange authorization code for access + refresh tokens.

        PKCE (public) flow: form body carries grant_type, code, client_id and
        the code_verifier — no Authorization header, no client_secret.
        Confidential flow: HTTP Basic client_id:client_secret, body carries
        only grant_type and code (unchanged legacy behavior)."""
        if self._use_pkce:
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            data = {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": self.client_id,
                "code_verifier": self._code_verifier or "",
            }
        else:
            auth_header = base64.b64encode(
                f"{self.client_id}:{self.client_secret}".encode()
            ).decode()
            headers = {
                "Authorization": f"Basic {auth_header}",
                "Content-Type": "application/x-www-form-urlencoded",
            }
            data = {
                "grant_type": "authorization_code",
                "code": code,
            }

        try:
            resp = self._session.post(
                SSO_TOKEN_URL,
                headers=headers,
                data=data,
            )
            if not resp.ok:
                print(f"[ESI Auth] Token exchange failed: {resp.status_code} {resp.text}")
                return False

            data = resp.json()
            self._access_token = data["access_token"]
            self._refresh_token = data["refresh_token"]
            self._expires_at = datetime.now(timezone.utc) + timedelta(
                seconds=data.get("expires_in", 1199)
            )

            # Verifier is single-use; drop it once the code is redeemed.
            self._code_verifier = None

            # Decode JWT to get character info
            self._decode_character_info()
            return True

        except Exception as e:
            print(f"[ESI Auth] Token exchange error: {e}")
            return False

    def _do_refresh(self) -> bool:
        """Refresh the access token using the refresh token.

        Returns True on success. Returns False on transient failure
        (network error, timeout, 5xx) — the caller may retry later with
        the existing refresh token still intact. On terminal failure
        (HTTP 400 invalid_grant — EVE SSO revoked the refresh token),
        clears all token state and persists an empty token file so the
        class cannot linger in a zombie-authenticated state.

        RLock guards the body so concurrent callers (access_token property,
        _load_tokens) don't race on the refresh-token rotation that SSO
        performs on every successful refresh.

        PKCE (public) flow sends grant_type, refresh_token and client_id form
        fields with no Authorization header / no client_secret. Confidential
        flow keeps HTTP Basic client_id:client_secret (unchanged). A 400
        invalid_grant (e.g. tokens minted under the old confidential app) is
        treated as terminal for both flows: state is cleared and an empty
        token file persisted so the user is dropped to the re-auth path
        instead of crashing.
        """
        with self._refresh_lock:
            if self._use_pkce:
                headers = {"Content-Type": "application/x-www-form-urlencoded"}
                data = {
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                    "client_id": self.client_id,
                }
            else:
                auth_header = base64.b64encode(
                    f"{self.client_id}:{self.client_secret}".encode()
                ).decode()
                headers = {
                    "Authorization": f"Basic {auth_header}",
                    "Content-Type": "application/x-www-form-urlencoded",
                }
                data = {
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                }

            try:
                resp = self._session.post(
                    SSO_TOKEN_URL,
                    headers=headers,
                    data=data,
                    timeout=15,
                )
            except (requests.Timeout, requests.ConnectionError) as e:
                # Transient: network didn't reach SSO. Keep refresh token.
                print(f"[ESI Auth] Token refresh transient network error: {e}")
                self._access_token = None
                return False
            except Exception as e:
                print(f"[ESI Auth] Refresh error: {e}")
                self._access_token = None
                return False

            if not resp.ok:
                # Distinguish terminal (invalid_grant = SSO revoked) from
                # transient (5xx, rate-limit, etc). 400 with invalid_grant
                # means the refresh token is dead — no point keeping it.
                terminal = False
                if resp.status_code == 400:
                    try:
                        err = resp.json().get("error", "")
                    except Exception:
                        err = ""
                    if err == "invalid_grant":
                        terminal = True
                if terminal:
                    print(
                        f"[ESI Auth] Refresh token revoked by SSO "
                        f"(invalid_grant) for {self._character_name}; "
                        f"clearing local credentials"
                    )
                    self._access_token = None
                    self._refresh_token = None
                    self._expires_at = None
                    # Persist an empty token file so on next start we don't
                    # silently "restore" a dead session.
                    try:
                        self._save_tokens()
                    except Exception as e:
                        print(f"[ESI Auth] Could not persist empty tokens: {e}")
                        log.exception(
                            "[ESI Auth] Could not persist empty tokens after "
                            "invalid_grant for %s", self._character_name
                        )
                    return False
                # Transient server-side failure — preserve refresh_token so
                # we can retry on the next expiry tick.
                print(
                    f"[ESI Auth] Token refresh transient failure: "
                    f"{resp.status_code}"
                )
                self._access_token = None
                return False

            try:
                data = resp.json()
                self._access_token = data["access_token"]
                self._refresh_token = data["refresh_token"]
                self._expires_at = datetime.now(timezone.utc) + timedelta(
                    seconds=data.get("expires_in", 1199)
                )
                self._save_tokens()
                return True
            except Exception as e:
                print(f"[ESI Auth] Refresh response parse error: {e}")
                self._access_token = None
                return False

    @staticmethod
    def _decode_jwt_payload(token: str) -> dict:
        """Decode the (unverified) payload segment of a JWT into a dict.

        Returns {} on any malformed/empty input. The token signature is NOT
        verified — we only read claims SSO already issued to us (sub, name,
        scp), so this is purely a local decode, not a trust boundary."""
        try:
            payload_b64 = token.split(".")[1]
            padding = 4 - len(payload_b64) % 4
            if padding != 4:
                payload_b64 += "=" * padding
            return json.loads(base64.urlsafe_b64decode(payload_b64))
        except Exception:
            return {}

    def _decode_character_info(self):
        """Extract character ID and name from the JWT access token."""
        try:
            payload = self._decode_jwt_payload(self._access_token or "")
            if not payload:
                # Undecodable/absent token — leave any previously-loaded
                # character_id/name intact (matches prior behavior where a
                # decode failure was swallowed without overwriting state).
                return

            # Subject format: "CHARACTER:EVE:1234567890"
            sub = payload.get("sub", "")
            parts = sub.split(":")
            if len(parts) == 3:
                self._character_id = int(parts[2])

            self._character_name = payload.get("name", "Unknown")

        except Exception as e:
            print(f"[ESI Auth] Error decoding JWT: {e}")

    def granted_scopes(self) -> set[str]:
        """Return the set of ESI scopes this character's token was granted.

        Reads the `scp` claim from the access-token JWT (SSO grants scopes
        only at login, so a token minted before a new scope was added will
        not carry it). The claim may be a list of scope strings or a single
        space-separated string — both are handled. Triggers a refresh via the
        access_token property if no decoded token is in memory yet. Returns an
        empty set on any error (missing/expired token, malformed JWT), so
        callers can treat 'unknown' as 'not granted' and route to re-auth."""
        token = self._access_token
        if not token:
            # Lazily obtain a valid access token (may refresh) so a freshly
            # loaded account still reports its scopes. Never raises.
            try:
                token = self.access_token
            except Exception:
                token = None
        if not token:
            return set()
        scp = self._decode_jwt_payload(token).get("scp")
        if isinstance(scp, str):
            return {s for s in scp.split(" ") if s}
        if isinstance(scp, (list, tuple)):
            return {s for s in scp if isinstance(s, str) and s}
        return set()

    def has_scope(self, scope: str) -> bool:
        """True iff this character's token was granted `scope`. Defensive:
        returns False on any decode error or missing token."""
        return scope in self.granted_scopes()

    def missing_scopes(self) -> set[str]:
        """The scopes in the current ``SCOPES`` list this token was NOT granted.

        Empty set = fully scoped for every feature. A token minted before scopes
        were added (e.g. the Market Scanner's structure-market + contract scopes,
        or the fittings scopes) reports those as missing so the UI can flag ⚠ +
        offer Re-authorize for ANY missing scope — not just fittings. Defensive:
        an unreadable/expired token (no granted scopes) reports every scope
        missing, routing the user to re-auth rather than silently degrading."""
        granted = self.granted_scopes()
        return {s for s in SCOPES if s not in granted}

    def has_market_structure_scope(self) -> bool:
        """True iff this token can pull a citadel/structure market (design §6.3).

        Gates the authed structure-market path. When False the Market Scanner
        falls back to public region orders / public contracts (degraded mode) and
        the UI shows the existing ⚠ re-auth indicator for this character."""
        return self.has_scope(MARKET_STRUCTURE_SCOPE)

    def has_market_contract_scope(self) -> bool:
        """True iff this token can read corp/alliance contracts (design §6.3).

        Gates the corp-contract path only; public region contracts need no scope,
        so the contract scan still runs (public-only) when this is False."""
        return any(self.has_scope(s) for s in MARKET_CONTRACT_SCOPES)

    def logout(self):
        """Clear stored tokens."""
        with self._refresh_lock:
            self._access_token = None
            self._refresh_token = None
            self._expires_at = None
            self._character_id = None
            self._character_name = None
            if os.path.exists(self.token_file):
                os.remove(self.token_file)
        print("[ESI Auth] Logged out")

    # ── Token Persistence ────────────────────────────────────────────────────

    def _resolve_token_path(self) -> str:
        """Return the canonical per-character token path if character_id
        is known, else the currently configured token_file. Pure function
        — does NOT mutate state or touch the filesystem."""
        if self._character_id:
            return os.path.join(
                TOKEN_DIR, f"esi_tokens_{self._character_id}.json"
            )
        return self.token_file

    def _migrate_to_per_character_path(self):
        """One-shot transition: after first successful login, move the
        token file from a temp/legacy path to the per-character canonical
        path. Idempotent. Called only when character_id just became known
        (i.e. right after _exchange_code succeeds), not on every save."""
        if not self._character_id:
            return
        target = self._resolve_token_path()
        if self.token_file == target:
            return
        old_path = self.token_file
        self.token_file = target
        if old_path and os.path.exists(old_path):
            try:
                os.remove(old_path)
            except OSError:
                pass

    def _save_tokens(self):
        """Atomically write tokens to self.token_file. No path resolution,
        no rename side effects. Delegates to app_io.atomic_write_json, which
        writes to {path}.tmp then os.replace()s into place so a crash
        mid-write cannot corrupt the token file (and cleans up the .tmp on
        failure before re-raising)."""
        data = {
            "refresh_token": self._refresh_token,
            "character_id": self._character_id,
            "character_name": self._character_name,
        }
        final_path = self.token_file
        # Ensure parent exists (tests use tmp_path which already does).
        parent = os.path.dirname(final_path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        atomic_write_json(final_path, data, indent=2)

    def _load_tokens(self):
        """Load tokens from disk and refresh if available."""
        if not os.path.exists(self.token_file):
            return
        try:
            with open(self.token_file) as f:
                data = json.load(f)
            self._refresh_token = data.get("refresh_token")
            self._character_id = data.get("character_id")
            self._character_name = data.get("character_name")

            if self._refresh_token:
                # Refresh to get a valid access token
                if self._do_refresh():
                    self._decode_character_info()
                    print(f"[ESI Auth] Restored session: {self._character_name}")
                else:
                    print("[ESI Auth] Stored token expired, re-login needed")
                    self._refresh_token = None
        except Exception as e:
            print(f"[ESI Auth] Error loading tokens: {e}")

    # ── ESI API Helpers ──────────────────────────────────────────────────────

    # Upper bound on the per-instance ETag cache (B2). The hot pollers touch a
    # small fixed set of paths; a long multi-region infra scan adds one entry per
    # resolved structure, so cap growth and evict oldest-first when full.
    _ETAG_CACHE_MAX = 512

    @staticmethod
    def _etag_key(path: str, params: "dict | None") -> str:
        """Key for the ETag cache: the path with any query params folded in
        deterministically, so two GETs to the same path with DIFFERENT params
        (e.g. per-system structure searches) never collide on one ETag entry.
        Param-less calls (the location/ship/online/fleet pollers) key on the bare
        path. Mirrors market_esi's per-(region, type) cache_key folding."""
        if not params:
            return path
        return f"{path}?{urlencode(sorted(params.items()))}"

    def _etag_store(self, key: str, etag: str, body) -> None:
        """Record ``(etag, body)`` under ``key``, bounding the cache to
        ``_ETAG_CACHE_MAX`` by evicting the oldest inserted entry (dicts preserve
        insertion order). Eviction is best-effort/idempotent — safe lock-free."""
        cache = self._etag_cache
        if key not in cache and len(cache) >= self._ETAG_CACHE_MAX:
            cache.pop(next(iter(cache)), None)
        cache[key] = (etag, body)

    def esi_get_ex(self, path: str, params: dict = None) -> tuple[object | None, int, dict]:
        """Authenticated GET returning (parsed_json_or_None, http_status, headers).

        Same auth/refresh/rate-limit/logging behaviour as ``esi_get`` but also
        surfaces the HTTP status and response headers, so bulk callers (the infra
        region scanner) can read the ``X-ESI-Error-Limit-Remain``/``-Reset``
        budget headers that ``esi_get`` discards. ``esi_get`` delegates here and
        returns element ``[0]``, so every existing caller is untouched.

        Returns ``(None, 0, {})`` on the no-token and exception paths (keeping the
        delegation None-returning) and ``(None, status, headers)`` on a non-OK
        response — with the SAME quiet-error suppression as before. Token refresh
        happens in the ``access_token`` property before the request, so there is
        no post-401 retry to preserve.

        ETag/conditional GET (B2): a per-instance cache (see ``_etag_cache`` in
        ``__init__``) maps the path(+params) key to the last ``(ETag, body)``.
        When an ETag is known it is replayed as ``If-None-Match``; a **304** then
        returns the cached body AS A 200 — transparent to every caller (``esi_get``
        still gets the body; the infra scanner still sees a success + the real
        budget headers off the 304). Only true 200s that carry an ``ETag`` header
        are cached; error/304 bodies are never stored. This layer sits BELOW the
        get_location/get_ship_type TTL memos (they memoize whatever body this
        returns), so nothing goes stale."""
        token = self.access_token
        if not token:
            return None, 0, {}
        key = self._etag_key(path, params)
        cached = self._etag_cache.get(key)
        req_headers = {"Authorization": f"Bearer {token}"}
        if cached is not None:
            req_headers["If-None-Match"] = cached[0]
        try:
            rate_limit('esi')
            resp = self._session.get(
                f"{ESI_BASE}{path}",
                headers=req_headers,
                params=params,
                timeout=10,
            )
            raw = getattr(resp, "headers", {}) or {}
            headers = dict(raw)
            if resp.status_code == 304:
                # A 304 only comes back because we sent If-None-Match from the
                # cache: replay the stored body AS a 200 so esi_get and the infra
                # scanner see an ordinary success (the real budget headers off the
                # 304 are still surfaced). Never call .json() on a 304 (empty body).
                if cached is not None:
                    return cached[1], 200, headers
                return None, 304, headers
            if resp.ok:
                body = resp.json()
                if resp.status_code == 200:
                    etag = raw.get("ETag")
                    if etag:
                        self._etag_store(key, etag, body)
                return body, resp.status_code, headers
            # Suppress noisy expected errors
            if resp.status_code == 404 and "/fleet/" in path:
                pass  # Not in fleet
            elif resp.status_code == 403 and "/structures/" in path:
                pass  # No access to structure (different corp/alliance)
            else:
                print(f"[ESI] {path} returned {resp.status_code}")
            return None, resp.status_code, headers
        except Exception as e:
            print(f"[ESI] Error: {e}")
        return None, 0, {}

    def esi_get(self, path: str, params: dict = None) -> dict | list | None:
        """Make an authenticated GET request to ESI."""
        return self.esi_get_ex(path, params)[0]

    def esi_post(self, path: str, json_data: dict = None) -> dict | list | None:
        """Make an authenticated POST request to ESI."""
        token = self.access_token
        if not token:
            return None
        try:
            rate_limit('esi')
            resp = self._session.post(
                f"{ESI_BASE}{path}",
                headers={"Authorization": f"Bearer {token}"},
                json=json_data,
                timeout=10,
            )
            if resp.ok:
                return resp.json() if resp.text else True
            print(f"[ESI] POST {path} returned {resp.status_code}")
        except Exception as e:
            print(f"[ESI] Error: {e}")
        return None

    def esi_put(self, path: str, json_data: dict = None) -> bool:
        """Make an authenticated PUT request to ESI.

        Returns True when ESI accepts the write (any 2xx, e.g. the 204 that
        PUT /fleets/{id}/ returns); returns False and logs on a non-2xx
        status, a missing token, or a network error — mirroring the
        non-raising error convention of esi_post so failures don't surface
        into the GUI as exceptions."""
        token = self.access_token
        if not token:
            return False
        try:
            rate_limit('esi')
            resp = self._session.put(
                f"{ESI_BASE}{path}",
                headers={"Authorization": f"Bearer {token}"},
                json=json_data,
                timeout=10,
            )
            if resp.ok:
                return True
            print(f"[ESI] PUT {path} returned {resp.status_code}")
        except Exception as e:
            print(f"[ESI] Error: {e}")
        return False

    def esi_delete(self, path: str) -> bool:
        """Make an authenticated DELETE request to ESI.

        Returns True when ESI accepts the delete (any 2xx, e.g. the 204 that
        DELETE /characters/{id}/fittings/{fid}/ returns); returns False and
        logs on a non-2xx status, a missing token, or a network error
        (same non-raising convention as esi_post/esi_put)."""
        token = self.access_token
        if not token:
            return False
        try:
            rate_limit('esi')
            resp = self._session.delete(
                f"{ESI_BASE}{path}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            if resp.ok:
                return True
            print(f"[ESI] DELETE {path} returned {resp.status_code}")
        except Exception as e:
            print(f"[ESI] Error: {e}")
        return False

    @staticmethod
    def esi_post_public(path: str, body) -> dict | list | None:
        """Public ESI POST — no auth, no token. For endpoints that don't require scopes
        (e.g. /universe/ids/, /characters/affiliation/). Returns None on failure."""
        url = ESI_BASE + path
        headers = {**HEADERS, "Content-Type": "application/json"}
        try:
            rate_limit('esi')
            resp = requests.post(url, json=body, headers=headers, timeout=10)
            if resp.ok:
                return resp.json()
        except Exception:
            log.exception("[ESI] public POST %s failed", path)
        return None

    # ── Character Info ───────────────────────────────────────────────────────

    # TTL for the get_location()/get_ship_type() per-instance memo (B3). ESI's
    # own server-side cache on these endpoints is ~5s; 4s keeps us just under
    # that so a memoized read is never staler than a fresh one would be.
    _POLL_MEMO_TTL_S = 4.0

    def get_location(self) -> dict | None:
        """Get the authenticated character's current location.

        Memoized for _POLL_MEMO_TTL_S seconds — see _loc_memo in __init__."""
        if not self._character_id:
            return None
        now = time.monotonic()
        if self._loc_memo is not None:
            ts, value = self._loc_memo
            if now - ts < self._POLL_MEMO_TTL_S:
                return value
        value = self.esi_get(f"/characters/{self._character_id}/location/")
        if value is not None:
            self._loc_memo = (now, value)
        return value

    def get_ship_type(self) -> dict | None:
        """Get the authenticated character's current ship.

        Memoized for _POLL_MEMO_TTL_S seconds — see _ship_memo in __init__."""
        if not self._character_id:
            return None
        now = time.monotonic()
        if self._ship_memo is not None:
            ts, value = self._ship_memo
            if now - ts < self._POLL_MEMO_TTL_S:
                return value
        value = self.esi_get(f"/characters/{self._character_id}/ship/")
        if value is not None:
            self._ship_memo = (now, value)
        return value

    def set_waypoint(self, destination_id: int, clear_other: bool = False,
                     add_to_beginning: bool = False) -> bool:
        """Set an in-game waypoint."""
        token = self.access_token
        if not token:
            return False
        try:
            rate_limit('esi')
            resp = self._session.post(
                f"{ESI_BASE}/ui/autopilot/waypoint/",
                headers={"Authorization": f"Bearer {token}"},
                params={
                    "destination_id": destination_id,
                    "clear_other_waypoints": str(clear_other).lower(),
                    "add_to_beginning": str(add_to_beginning).lower(),
                },
                timeout=10,
            )
            return resp.status_code == 204
        except Exception as e:
            print(f"[ESI] Waypoint error: {e}")
            return False

    # ── Assets ───────────────────────────────────────────────────────────────

    def get_assets(self) -> list[dict]:
        """Get all character assets (auto-paginated).
        Returns flat list of asset items with type_id, location_id, etc.
        Returns empty list if the esi-assets scope is not granted."""
        if not self._character_id:
            return []
        all_assets = []
        page = 1
        while True:
            token = self.access_token
            if not token:
                break
            try:
                rate_limit('esi')
                resp = self._session.get(
                    f"{ESI_BASE}/characters/{self._character_id}/assets/",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"page": page},
                    timeout=15,
                )
                if resp.status_code == 403:
                    # Scope not granted — silently return empty
                    print(f"[ESI] Assets scope not granted for {self._character_name}")
                    return []
                if not resp.ok:
                    break
                items = resp.json()
                all_assets.extend(items)
                total_pages = int(resp.headers.get("x-pages", 1))
                if page >= total_pages:
                    break
                page += 1
            except Exception as e:
                print(f"[ESI] Assets page {page} error: {e}")
                break
        return all_assets

    # ── Fleet ────────────────────────────────────────────────────────────────

    def get_fleet_info(self) -> dict | None:
        """Return the authed character's current fleet info as
        {'fleet_id', 'fleet_boss_id', 'role'}, or None if not in a fleet."""
        if not self._character_id:
            return None
        data = self.esi_get(f"/characters/{self._character_id}/fleet/")
        if isinstance(data, dict) and "fleet_id" in data:
            return {
                "fleet_id": data["fleet_id"],
                "fleet_boss_id": data.get("fleet_boss_id"),
                "role": data.get("role"),
            }
        return None

    @staticmethod
    def is_boss(info, character_id) -> bool:
        """True iff `info` (from get_fleet_info) shows `character_id` is the fleet
        boss — the only member allowed to read /fleets/{id}/members/."""
        return bool(info and info.get("fleet_boss_id") == character_id)

    def get_fleet_id(self) -> int | None:
        """The authed character's current fleet_id, or None if not in a fleet."""
        info = self.get_fleet_info()
        return info["fleet_id"] if info else None

    def get_fleet_members(self, fleet_id=None) -> list[dict] | None:
        """Fleet member roster. Pass a known fleet_id to avoid a redundant
        /characters/{id}/fleet/ lookup. ESI returns 403 unless the authed char
        is the fleet boss.
        Returns list of dicts with character_id, solar_system_id, ship_type_id, etc.
        """
        if fleet_id is None:
            fleet_id = self.get_fleet_id()
        if not fleet_id:
            return None
        return self.esi_get(f"/fleets/{fleet_id}/members/")

    def get_fleet_member_locations(self, members=None) -> dict[str, tuple[str, str, str]]:
        """Get a map of character_name -> (system_name, region_name, ship_name).
        Accepts optional pre-fetched members list to avoid duplicate ESI calls."""
        if members is None:
            members = self.get_fleet_members()
        if not members:
            return {}

        from jump_range import get_system_info
        from zkill_monitor import resolve_name

        result = {}
        for m in members:
            char_id = m.get("character_id")
            sys_id = m.get("solar_system_id")
            ship_type_id = m.get("ship_type_id")
            if char_id and sys_id:
                char_name = resolve_name(char_id, "character")
                sys_info = get_system_info(sys_id)
                sys_name = sys_info.get("name", "???") if sys_info else "???"
                region_name = self._get_region_name(sys_info) if sys_info else ""
                ship_name = resolve_name(ship_type_id, "type") if ship_type_id else ""
                result[char_name] = (sys_name, region_name, ship_name)
        return result

    def _get_region_name(self, sys_info: dict) -> str:
        """Resolve system info -> constellation -> region name.

        Both hops are memoized in module-level dicts (_CONSTELLATION_REGION_MEMO,
        _REGION_NAME_MEMO) since constellation->region and region->name are
        static SDE data that never changes — a permanent memo is correct. Only
        successful lookups are cached; a failed/empty response is retried on
        the next call instead of being pinned as a permanent miss."""
        try:
            constellation_id = sys_info.get("constellation_id")
            if not constellation_id:
                return ""

            region_id = _CONSTELLATION_REGION_MEMO.get(constellation_id)
            if region_id is None:
                const_info = self.esi_get(f"/universe/constellations/{constellation_id}/")
                if not const_info:
                    return ""
                region_id = const_info.get("region_id")
                if not region_id:
                    return ""
                _CONSTELLATION_REGION_MEMO[constellation_id] = region_id

            region_name = _REGION_NAME_MEMO.get(region_id)
            if region_name is None:
                region_info = self.esi_get(f"/universe/regions/{region_id}/")
                if not region_info:
                    return ""
                region_name = region_info.get("name", "")
                if not region_name:
                    return ""
                _REGION_NAME_MEMO[region_id] = region_name
            return region_name
        except Exception:
            pass
        return ""

    def get_fleet(self, fleet_id: int) -> dict | None:
        """Read a fleet's settings, including its current MOTD.

        GET /fleets/{fleet_id}/ -> {motd, is_free_move, is_registered}.
        Boss-only on ESI's side; returns None on any error (esi_get convention).
        """
        return self.esi_get(f"/fleets/{fleet_id}/")

    def set_fleet_motd(self, fleet_id: int, motd: str) -> bool:
        """Write a fleet's MOTD via PUT /fleets/{fleet_id}/ (204 on success).

        Sends a motd-only body. Boss-only — ESI returns 403 for non-boss
        callers; the GUI gates on fleet_boss_id before calling. Returns True
        on success, False otherwise (esi_put convention)."""
        return self.esi_put(f"/fleets/{fleet_id}/", {"motd": motd})

    # ── Character Fittings ───────────────────────────────────────────────────

    # Per-fitting field limits enforced before POST (ESI rejects out-of-range).
    _FITTING_NAME_MAX = 50
    _FITTING_DESC_MAX = 500
    _FITTING_ITEMS_MIN = 1
    _FITTING_ITEMS_MAX = 512

    def get_fittings(self, character_id: int) -> list[dict]:
        """List a character's in-game saved fittings.

        GET /characters/{character_id}/fittings/ -> array of
        {fitting_id, name, description, ship_type_id, items[]}. Each item is
        {type_id, flag(string), quantity}. Returns [] on any error."""
        data = self.esi_get(f"/characters/{character_id}/fittings/")
        return data if isinstance(data, list) else []

    def create_fitting(self, character_id: int, body: dict) -> int | None:
        """Save a fitting to a character's in-game Fittings.

        POST /characters/{character_id}/fittings/ -> {fitting_id}. Applies the
        client-side guards ESI enforces (name <= 50, description <= 500,
        1..512 items, drop any Invalid-flagged items) so a borderline fit is
        trimmed rather than rejected. Returns the new fitting_id, or None on
        failure / a rejected body."""
        body = self._sanitize_fitting_body(body)
        if body is None:
            return None
        resp = self.esi_post(f"/characters/{character_id}/fittings/", body)
        if isinstance(resp, dict):
            return resp.get("fitting_id")
        return None

    def delete_fitting(self, character_id: int, fitting_id: int) -> bool:
        """Delete one of a character's in-game saved fittings.

        DELETE /characters/{character_id}/fittings/{fitting_id}/ (204). There
        is no ESI update endpoint, so editing is delete + recreate. Returns
        True on success, False otherwise."""
        return self.esi_delete(
            f"/characters/{character_id}/fittings/{fitting_id}/"
        )

    def _sanitize_fitting_body(self, body: dict) -> dict | None:
        """Clamp/validate a fitting body to ESI's limits before POST.

        Truncates name/description, drops Invalid-flagged items, and rejects
        (returns None) a body whose item count falls outside 1..512 after
        filtering — the only case ESI cannot satisfy. The input dict is not
        mutated; a shallow copy with a filtered items list is returned."""
        if not isinstance(body, dict):
            return None
        clean = dict(body)
        name = clean.get("name") or ""
        clean["name"] = name[: self._FITTING_NAME_MAX]
        desc = clean.get("description") or ""
        clean["description"] = desc[: self._FITTING_DESC_MAX]
        items = [
            it for it in (clean.get("items") or [])
            if it.get("flag") != "Invalid"
        ]
        if not (self._FITTING_ITEMS_MIN <= len(items) <= self._FITTING_ITEMS_MAX):
            return None
        clean["items"] = items
        return clean

    # ── Names / Affiliations / Contacts ──────────────────────────────────────

    def resolve_names_to_ids(self, names: list[str]) -> dict[str, int]:
        """Resolve a list of EVE names to character IDs.

        Tries the public ESI endpoint first (no auth required), falls back to the
        authenticated POST. Names are deduplicated and length-filtered (3-37 chars
        per EVE's character-name rules) before being sent. On 400/failure, the
        chunk is recursively split in half and retried — this handles ESI batch
        limits, duplicate-rejection, and isolated bad names automatically."""
        out: dict[str, int] = {}
        if not names:
            return out

        # Deduplicate (preserve insertion order) and length-filter
        seen: set[str] = set()
        cleaned: list[str] = []
        for n in names:
            if n in seen:
                continue
            seen.add(n)
            if 3 <= len(n) <= 37:
                cleaned.append(n)

        self._resolve_chunk_recursive(cleaned, out)
        return out

    def _resolve_chunk_recursive(self, chunk: list[str], out: dict[str, int]) -> None:
        """Resolve a chunk, splitting in half on failure until each chunk is size 1."""
        if not chunk:
            return
        INITIAL_CHUNK = 500
        if len(chunk) > INITIAL_CHUNK:
            for i in range(0, len(chunk), INITIAL_CHUNK):
                self._resolve_chunk_recursive(chunk[i:i + INITIAL_CHUNK], out)
            return

        data = ESIAuth.esi_post_public("/universe/ids/", chunk)
        if not isinstance(data, dict):
            data = self.esi_post("/universe/ids/", chunk)

        # A True sentinel is a successful 2xx with an empty body (esi_post's
        # empty-body signal) — the batch resolved to nothing, so stop here
        # instead of treating it as a failure and needlessly splitting.
        if data is True:
            return

        if isinstance(data, dict):
            for entry in data.get("characters", []) or []:
                n = entry.get("name")
                cid = entry.get("id")
                if n and cid:
                    out[n] = cid
            return

        # Failure — split chunk in half and retry
        if len(chunk) <= 1:
            # Single name failure: log and drop
            print(
                f"[esi_auth] /universe/ids/ rejected single name: "
                f"{chunk[0] if chunk else '<empty>'}",
                file=sys.stderr,
            )
            return
        mid = len(chunk) // 2
        self._resolve_chunk_recursive(chunk[:mid], out)
        self._resolve_chunk_recursive(chunk[mid:], out)

    # ── Categorized ID resolution (/universe/ids/) ───────────────────────────

    # Categories returned by POST /universe/ids/ that this tool cares about.
    _ID_CATEGORIES = (
        "alliances",
        "corporations",
        "regions",
        "systems",
        "characters",
    )

    def resolve_ids(self, names: list[str]) -> dict[str, list[dict]]:
        """Resolve a list of EVE names to categorized {id, name} entries.

        Built on the public POST /universe/ids/ endpoint (exact, case-insensitive
        name match) with the same split-on-failure robustness used by
        resolve_names_to_ids — one bad name in a batch does not sink the rest.

        Returns a dict with exactly these keys, each a list of {"id", "name"}:
            {"alliances": [...], "corporations": [...], "regions": [...],
             "systems": [...], "characters": [...]}
        Unknown names simply don't appear. Returns all-empty lists for empty
        input. Resilient to network failure (empty lists, never raises)."""
        out: dict[str, list[dict]] = {cat: [] for cat in self._ID_CATEGORIES}
        if not names:
            return out

        # Deduplicate (preserve insertion order) and length-filter. The 3-37
        # bound matches resolve_names_to_ids; it is permissive enough for
        # alliance/corp/region/system names too (all >= 3 chars in EVE).
        seen: set[str] = set()
        cleaned: list[str] = []
        for n in names:
            if not isinstance(n, str):
                continue
            if n in seen:
                continue
            seen.add(n)
            if 3 <= len(n) <= 37:
                cleaned.append(n)

        self._resolve_ids_chunk_recursive(cleaned, out)
        return out

    def _resolve_ids_chunk_recursive(
        self, chunk: list[str], out: dict[str, list[dict]]
    ) -> None:
        """Resolve a chunk into categorized buckets, splitting in half on
        failure until each chunk is size 1 (mirrors _resolve_chunk_recursive)."""
        if not chunk:
            return
        INITIAL_CHUNK = 500
        if len(chunk) > INITIAL_CHUNK:
            for i in range(0, len(chunk), INITIAL_CHUNK):
                self._resolve_ids_chunk_recursive(chunk[i:i + INITIAL_CHUNK], out)
            return

        data = ESIAuth.esi_post_public("/universe/ids/", chunk)
        if not isinstance(data, dict):
            data = self.esi_post("/universe/ids/", chunk)

        # A True sentinel is a successful 2xx with an empty body (esi_post's
        # empty-body signal) — the batch resolved to nothing, so stop here
        # instead of treating it as a failure and needlessly splitting.
        if data is True:
            return

        if isinstance(data, dict):
            for cat in self._ID_CATEGORIES:
                for entry in data.get(cat, []) or []:
                    cid = entry.get("id")
                    name = entry.get("name")
                    if cid and name:
                        out[cat].append({"id": cid, "name": name})
            return

        # Failure — split chunk in half and retry.
        if len(chunk) <= 1:
            print(
                f"[esi_auth] /universe/ids/ rejected single name: "
                f"{chunk[0] if chunk else '<empty>'}",
                file=sys.stderr,
            )
            return
        mid = len(chunk) // 2
        self._resolve_ids_chunk_recursive(chunk[:mid], out)
        self._resolve_ids_chunk_recursive(chunk[mid:], out)

    @staticmethod
    def _first_exact(entries: list[dict], name: str) -> dict | None:
        """Return the first {id, name} whose name matches `name`
        case-insensitively, else None. /universe/ids/ already does an exact
        (case-insensitive) match, but a single query can in principle return
        multiple categories/entries — this pins the intended one."""
        target = name.strip().casefold()
        for entry in entries:
            eid = entry.get("id")
            ename = entry.get("name")
            if eid and isinstance(ename, str) and ename.casefold() == target:
                return {"id": eid, "name": ename}
        # Fall back to the first well-formed entry (ESI matched something).
        for entry in entries:
            eid = entry.get("id")
            ename = entry.get("name")
            if eid and isinstance(ename, str):
                return {"id": eid, "name": ename}
        return None

    def resolve_alliance(self, name: str) -> dict | None:
        """Resolve a single alliance name -> {"id", "name"} (exact,
        case-insensitive; first match) or None if not found."""
        if not name or not name.strip():
            return None
        res = self.resolve_ids([name])
        return self._first_exact(res.get("alliances", []), name)

    def resolve_corporation(self, name: str) -> dict | None:
        """Resolve a single corporation name -> {"id", "name"} (exact,
        case-insensitive; first match) or None if not found."""
        if not name or not name.strip():
            return None
        res = self.resolve_ids([name])
        return self._first_exact(res.get("corporations", []), name)

    def resolve_region(self, name: str) -> dict | None:
        """Resolve a single region name -> {"id", "name"} (exact,
        case-insensitive; first match) or None if not found."""
        if not name or not name.strip():
            return None
        res = self.resolve_ids([name])
        return self._first_exact(res.get("regions", []), name)

    # ── Live type-ahead search (/characters/{id}/search/) ────────────────────

    # Minimum seconds between failed sibling-delegate resolutions. A resolution
    # that finds no authenticated sibling globs the token dir and constructs an
    # ESIAuth per file (each a live SSO refresh), so we throttle retries to keep
    # a keystroke burst from re-running that scan; the cooldown is short enough
    # that a character logged in mid-session is picked up on the next attempt.
    _DELEGATE_FAIL_COOLDOWN = 7.0

    def _any_authenticated_auth(self) -> "ESIAuth | None":
        """Return an authenticated ESIAuth usable for endpoints that require a
        character context (e.g. /search/). Prefers self; otherwise loads any
        other stored per-character token file and caches it on the instance so
        subsequent calls reuse it instead of reconstructing (which would fire a
        live SSO refresh and rotate that sibling's token every keystroke).
        Returns None if none authenticate.

        We reuse self.client_id/secret/callback so no extra credentials are
        needed. Errors loading sibling tokens are swallowed (best-effort)."""
        # Fast path: this instance is authenticated -> always prefer self, and
        # drop any stale sibling delegate so it can't shadow us. No lock needed
        # (is_authenticated/access_token are internally synchronized).
        if self.is_authenticated and self.access_token:
            if self._delegate_auth is not None:
                self._delegate_auth = None
            return self

        # Fast path: reuse a still-authenticated cached delegate without taking
        # the lock (mirrors the access_token double-checked-locking idiom).
        cached = self._delegate_auth
        if cached is not None and cached.is_authenticated and cached.access_token:
            return cached

        # Slow path: (re)resolve under the lock so two concurrent worker-thread
        # callers can't each glob + construct the same sibling (double SSO
        # refresh -> possible invalid_grant race).
        with self._refresh_lock:
            # Re-check self: another thread may have authenticated us.
            if self.is_authenticated and self.access_token:
                self._delegate_auth = None
                return self
            # Re-check the cache: another thread may have resolved it while we
            # waited on the lock.
            cached = self._delegate_auth
            if cached is not None and cached.is_authenticated and cached.access_token:
                return cached
            # Stale (or never set): clear before re-resolving.
            self._delegate_auth = None

            # Throttle repeated failed resolutions so a keystroke burst doesn't
            # re-glob + reconstruct siblings every call. A prior failure within
            # the cooldown short-circuits; the window is short enough that a
            # newly logged-in character is still picked up soon after.
            now = time.monotonic()
            if (self._delegate_last_fail_ts
                    and now - self._delegate_last_fail_ts < self._DELEGATE_FAIL_COOLDOWN):
                return None

            import glob
            pattern = os.path.join(TOKEN_DIR, "esi_tokens_*.json")
            for token_file in glob.glob(pattern):
                try:
                    other = ESIAuth(
                        client_id=self.client_id,
                        client_secret=self.client_secret,
                        callback_url=self.callback_url,
                        token_file=token_file,
                    )
                    if other.is_authenticated and other.access_token:
                        self._delegate_auth = other
                        self._delegate_last_fail_ts = 0.0
                        return other
                except Exception:
                    continue
            # No sibling authenticated: record the failure time for the cooldown.
            self._delegate_last_fail_ts = now
            return None

    def search_entities(
        self, query: str,
        categories: list[str] = ("alliance", "corporation"),
    ) -> list[dict]:
        """Live type-ahead search via the authenticated
        GET /characters/{character_id}/search/ endpoint (non-strict, so it
        matches substrings/prefixes), then resolve the returned IDs to names
        via the public POST /universe/names/.

        Returns up to ~20 results as a list of
            {"id": int, "name": str, "category": str}
        ordered by category (in the requested order) then by ESI's id order.

        Returns [] gracefully when: query is empty, no authenticated character
        is available, the esi-search scope is missing, or any network error
        occurs. Never raises."""
        MAX_RESULTS = 20
        if not query or not query.strip():
            return []
        cats = [c for c in (categories or ()) if c]
        if not cats:
            return []

        auth = self._any_authenticated_auth()
        if auth is None or not auth.character_id:
            return []

        try:
            results = auth.esi_get(
                f"/characters/{auth.character_id}/search/",
                params={
                    "categories": ",".join(cats),
                    "search": query.strip(),
                    "strict": "false",
                },
            )
        except Exception:
            return []
        if not isinstance(results, dict):
            return []

        # Collect (id, category) preserving requested-category order, capped.
        # The /search/ response keys its id arrays by the SAME category names
        # we requested (alliance, corporation, region, ...), so we read each
        # bucket by the requested category key directly.
        id_to_category: dict[int, str] = {}
        ordered_ids: list[int] = []
        for cat in cats:
            ids = results.get(cat) or []
            if not isinstance(ids, list):
                continue
            for cid in ids:
                if not isinstance(cid, int):
                    continue
                if cid not in id_to_category:
                    id_to_category[cid] = cat
                    ordered_ids.append(cid)
                if len(ordered_ids) >= MAX_RESULTS:
                    break
            if len(ordered_ids) >= MAX_RESULTS:
                break

        if not ordered_ids:
            return []

        # Resolve ids -> names via public /universe/names/ (auth fallback).
        names_data = ESIAuth.esi_post_public("/universe/names/", ordered_ids)
        if not isinstance(names_data, list):
            names_data = auth.esi_post("/universe/names/", ordered_ids)
        if not isinstance(names_data, list):
            return []

        name_by_id: dict[int, str] = {}
        for entry in names_data:
            if not isinstance(entry, dict):
                continue
            eid = entry.get("id")
            ename = entry.get("name")
            if isinstance(eid, int) and isinstance(ename, str):
                name_by_id[eid] = ename

        out: list[dict] = []
        for cid in ordered_ids:
            name = name_by_id.get(cid)
            if not name:
                continue
            out.append({
                "id": cid,
                "name": name,
                "category": id_to_category.get(cid, ""),
            })
        return out

    def search_structures(self, term: str) -> list[dict]:
        """Live structure search via the authenticated
        GET /characters/{id}/search/?categories=structure endpoint (non-strict,
        so it matches substrings/prefixes), resolving each hit's name +
        solar_system_id via the authenticated GET /universe/structures/{sid}/.

        Returns up to ~20 results as a list of
            {"id": int, "name": str, "solar_system_id": int|None,
             "type_id": int|None}
        in ESI's id order. Uses the ``esi-search.search_structures`` scope — the
        SAME scope Ansiblex discovery already relies on, so no new grant is
        needed.

        NOTE: /search/ and /universe/structures/ only see structures the
        character has docking access to; inaccessible citadels simply don't
        appear (and their name GET 403s, which is skipped). Returns [] gracefully
        when: term is empty, no authenticated character is available, the search
        scope is missing, or any network error occurs. Never raises."""
        MAX_RESULTS = 20
        if not term or not term.strip():
            return []

        auth = self._any_authenticated_auth()
        if auth is None or not auth.character_id:
            return []

        try:
            results = auth.esi_get(
                f"/characters/{auth.character_id}/search/",
                params={
                    "categories": "structure",
                    "search": term.strip(),
                    "strict": "false",
                },
            )
        except Exception:
            return []
        if not isinstance(results, dict):
            return []

        ids = results.get("structure")
        if not isinstance(ids, list):
            return []

        out: list[dict] = []
        for sid in ids[:MAX_RESULTS]:
            if not isinstance(sid, int):
                continue
            try:
                info = auth.esi_get(f"/universe/structures/{sid}/")
            except Exception:
                info = None
            if not isinstance(info, dict):
                continue
            name = info.get("name")
            if not isinstance(name, str) or not name:
                continue
            out.append({
                "id": sid,
                "name": name,
                "solar_system_id": info.get("solar_system_id"),
                "type_id": info.get("type_id"),
            })
        return out

    def get_affiliations(self, char_ids: list[int]) -> list[dict]:
        """Resolve characters to corp/alliance affiliations. Public endpoint with
        auth fallback; chunks of 1000 with split-on-failure recovery."""
        out: list[dict] = []
        if not char_ids:
            return out
        # Dedupe (preserve order)
        seen: set[int] = set()
        cleaned: list[int] = []
        for cid in char_ids:
            if cid not in seen:
                seen.add(cid)
                cleaned.append(cid)
        self._affiliations_chunk_recursive(cleaned, out)
        return out

    def _affiliations_chunk_recursive(self, chunk: list[int], out: list[dict]) -> None:
        if not chunk:
            return
        if len(chunk) > 1000:
            for i in range(0, len(chunk), 1000):
                self._affiliations_chunk_recursive(chunk[i:i + 1000], out)
            return

        data = ESIAuth.esi_post_public("/characters/affiliation/", chunk)
        if not isinstance(data, list):
            data = self.esi_post("/characters/affiliation/", chunk)

        # A True sentinel is a successful 2xx with an empty body (esi_post's
        # empty-body signal) — the batch resolved to nothing, so stop here
        # instead of treating it as a failure and needlessly splitting.
        if data is True:
            return

        if isinstance(data, list):
            out.extend(data)
            return

        if len(chunk) <= 1:
            print(f"[esi_auth] /characters/affiliation/ rejected id: {chunk[0] if chunk else '<empty>'}",
                  file=sys.stderr)
            return
        mid = len(chunk) // 2
        self._affiliations_chunk_recursive(chunk[:mid], out)
        self._affiliations_chunk_recursive(chunk[mid:], out)

    def get_personal_contacts(self) -> list[dict]:
        """Get the authenticated character's personal contacts."""
        if not self._character_id:
            return []
        data = self.esi_get(f"/characters/{self._character_id}/contacts/")
        return data if isinstance(data, list) else []

    def get_corp_contacts(self, corp_id: int | None = None) -> list[dict]:
        """Get the authenticated character's corporation contacts.

        When ``corp_id`` is supplied, the internal ``/characters/{id}/`` lookup
        used to rediscover the corp id is skipped -- callers that already know
        the id (e.g. StandingsCache.refresh) can pass it to avoid a redundant
        GET. When ``corp_id`` is None the behaviour is unchanged: the corp id is
        self-resolved from the character sheet.
        """
        if not self._character_id:
            return []
        if corp_id is None:
            info = self.esi_get(f"/characters/{self._character_id}/")
            if not isinstance(info, dict):
                return []
            corp_id = info.get("corporation_id")
        if not corp_id:
            return []
        data = self.esi_get(f"/corporations/{corp_id}/contacts/")
        return data if isinstance(data, list) else []

    def get_alliance_contacts(self, alliance_id: int | None = None) -> list[dict]:
        """Get the authenticated character's alliance contacts.

        When ``alliance_id`` is supplied, the internal ``/characters/{id}/``
        lookup used to rediscover the alliance id is skipped -- callers that
        already know the id (e.g. StandingsCache.refresh) can pass it to avoid a
        redundant GET. When ``alliance_id`` is None the behaviour is unchanged:
        the alliance id is self-resolved from the character sheet, and a
        character with no alliance yields an empty list.
        """
        if not self._character_id:
            return []
        if alliance_id is None:
            info = self.esi_get(f"/characters/{self._character_id}/")
            if not isinstance(info, dict):
                return []
            alliance_id = info.get("alliance_id")
        if not alliance_id:
            return []
        data = self.esi_get(f"/alliances/{alliance_id}/contacts/")
        return data if isinstance(data, list) else []

    def is_fleet_boss(self) -> bool:
        """True iff the authed character is the fleet boss (can read members)."""
        return self.is_boss(self.get_fleet_info(), self._character_id)

    # ── Ansiblex Discovery ───────────────────────────────────────────────────

    def _get_character_corp_id(self) -> int | None:
        """Get the character's corporation ID."""
        if not self._character_id:
            return None
        info = self.esi_get(f"/characters/{self._character_id}/")
        if info:
            return info.get("corporation_id")
        return None

    def _parse_gate_name(self, name: str) -> list[str] | None:
        """
        Parse a gate name into [sys_a, sys_b].
        Common formats:
          "SystemA » SystemB"
          "SystemA » SystemB - Some Description"
        """
        if "»" not in name:
            return None
        parts = name.split("»")
        if len(parts) != 2:
            return None
        sys_a = parts[0].strip()
        sys_b = parts[1].strip()
        # The second part often has " - Description" suffix, strip it
        # System names are the first token before " - "
        if " - " in sys_b:
            sys_b = sys_b.split(" - ")[0].strip()
        if sys_a and sys_b:
            return [sys_a, sys_b]
        return None

    def discover_ansiblex_gates(self) -> list[list[str]]:
        """
        Discover Ansiblex jump gates accessible to the character.
        Returns list of [system_a, system_b] pairs parsed from gate names.

        Tries multiple approaches:
        1. ESI structure search with "»" character
        2. Corporation structures endpoint (if scope available)
        """
        if not self._character_id:
            print("[ESI Auth] No character ID — cannot discover gates")
            return []

        print("[ESI Auth] Discovering Ansiblex gates...")
        structure_ids = set()

        # Approach 1: Character search for structures containing "»"
        # ESI search requires minimum 3 characters, so we use " » " with spaces
        # (the bare "»" is a guaranteed 400 — one char, below the 3-char floor —
        # and every 4xx eats the shared error budget).
        for search_term in [" » "]:
            results = self.esi_get(
                f"/characters/{self._character_id}/search/",
                params={
                    "categories": "structure",
                    "search": search_term,
                    "strict": "false",
                },
            )
            if results and "structure" in results:
                found = results["structure"]
                print(f"[ESI Auth] Search '{search_term}' found {len(found)} structures")
                structure_ids.update(found)
                break
            else:
                print(f"[ESI Auth] Search '{search_term}' returned no results")

        # Approach 2: Corporation structures endpoint
        if not structure_ids:
            corp_id = self._get_character_corp_id()
            if corp_id:
                print(f"[ESI Auth] Trying corporation {corp_id} structures...")
                # X-Pages-driven pagination (mirrors get_assets): a page-size
                # heuristic like `len(page) < 250` ends discovery early
                # whenever a full page happens to come back under 250 items
                # while more pages remain. Bypasses esi_get/esi_get_ex (whose
                # header dict loses the original casing) for the same reason
                # get_assets does — resp.headers on the raw Session response
                # is case-insensitive.
                page = 1
                while True:
                    token = self.access_token
                    if not token:
                        break
                    try:
                        rate_limit('esi')
                        resp = self._session.get(
                            f"{ESI_BASE}/corporations/{corp_id}/structures/",
                            headers={"Authorization": f"Bearer {token}"},
                            params={"page": page},
                            timeout=10,
                        )
                        if resp.status_code == 403:
                            # No access (scope not granted / wrong corp)
                            break
                        if not resp.ok:
                            break
                        corp_structs = resp.json()
                        if not corp_structs:
                            break
                        for s in corp_structs:
                            # Ansiblex Jump Gate type_id = 35841
                            if s.get("type_id") == 35841:
                                structure_ids.add(s["structure_id"])
                        total_pages = int(resp.headers.get("x-pages", 1))
                        if page >= total_pages:
                            break
                        page += 1
                    except Exception as e:
                        print(f"[ESI Auth] Corp structures page {page} error: {e}")
                        break
                if structure_ids:
                    print(f"[ESI Auth] Found {len(structure_ids)} Ansiblex gates from corp structures")

        if not structure_ids:
            print("[ESI Auth] No Ansiblex structures found via any method")
            return []

        print(f"[ESI Auth] Resolving {len(structure_ids)} structure names...")

        # Resolve structure names and parse gate pairs
        gates = []
        seen = set()  # Avoid duplicate pairs
        for sid in structure_ids:
            info = self.esi_get(f"/universe/structures/{sid}/")
            if not info:
                print(f"[ESI Auth] Could not resolve structure {sid} (no access?)")
                continue

            name = info.get("name", "")
            type_id = info.get("type_id", 0)

            # Only process Ansiblex gates (type_id 35841) or names with »
            if type_id != 35841 and "»" not in name:
                continue

            pair = self._parse_gate_name(name)
            if pair:
                # Deduplicate (A->B is same as B->A)
                key = tuple(sorted(pair))
                if key not in seen:
                    seen.add(key)
                    gates.append(pair)
                    print(f"[ESI Auth] Gate: {pair[0]} <-> {pair[1]}")

        print(f"[ESI Auth] Discovered {len(gates)} unique Ansiblex gate(s)")
        return gates


# ── Multi-character helpers ──────────────────────────────────────────────────

def _migrate_legacy_tokens():
    """Migrate old single esi_tokens.json to per-character format."""
    legacy = os.path.join(TOKEN_DIR, "esi_tokens.json")
    if not os.path.exists(legacy):
        return
    try:
        with open(legacy) as f:
            data = json.load(f)
        char_id = data.get("character_id")
        if char_id:
            new_path = os.path.join(TOKEN_DIR, f"esi_tokens_{char_id}.json")
            if not os.path.exists(new_path):
                atomic_write_json(new_path, data, indent=2)
                print(f"[ESI Auth] Migrated legacy tokens to {new_path}")
            os.remove(legacy)
            print("[ESI Auth] Removed legacy esi_tokens.json")
    except Exception as e:
        print(f"[ESI Auth] Legacy migration error: {e}")


def load_all_tokens(client_id: str, client_secret: str = "",
                    callback_url: str = "http://localhost:8834/callback") -> list[ESIAuth]:
    """Load all per-character ESI token files and return authenticated ESIAuth instances.

    client_secret is optional: pass an empty string (the distribution default)
    to use the PKCE public-client flow, or a real secret to use the legacy
    confidential flow. The mode is auto-detected per ESIAuth instance."""
    _migrate_legacy_tokens()
    accounts = []
    import glob
    pattern = os.path.join(TOKEN_DIR, "esi_tokens_*.json")
    for token_file in glob.glob(pattern):
        try:
            auth = ESIAuth(
                client_id=client_id,
                client_secret=client_secret,
                callback_url=callback_url,
                token_file=token_file,
            )
            if auth.is_authenticated:
                accounts.append(auth)
                print(f"[ESI Auth] Loaded: {auth.character_name} (ID: {auth.character_id})")
            else:
                print(f"[ESI Auth] Token expired for {token_file}")
        except Exception as e:
            print(f"[ESI Auth] Error loading {token_file}: {e}")
    return accounts
