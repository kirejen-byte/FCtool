"""
EVE SSO OAuth2 Authentication Module
Handles login flow, token storage, and refresh for ESI API access.
"""

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlencode, urlparse, parse_qs
from datetime import datetime, timezone, timedelta

import requests

from app_path import app_dir

SSO_AUTH_URL = "https://login.eveonline.com/v2/oauth/authorize"
SSO_TOKEN_URL = "https://login.eveonline.com/v2/oauth/token"
SSO_JWKS_URL = "https://login.eveonline.com/oauth/jwks"
ESI_BASE = "https://esi.evetech.net/latest"
HEADERS = {"User-Agent": "FCTool/1.0 (EVE FC Assistant)"}

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
]


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

    def __init__(self, client_id: str, client_secret: str,
                 callback_url: str = "http://localhost:8834/callback",
                 token_file: str = TOKEN_FILE):
        self.client_id = client_id
        self.client_secret = client_secret
        self.callback_url = callback_url
        self.callback_port = int(urlparse(callback_url).port or 8834)
        self.token_file = token_file

        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at: datetime | None = None
        self._character_id: int | None = None
        self._character_name: str | None = None

        # Re-entrant lock guarding all token state mutation and refresh RPCs.
        # Re-entrant because _do_refresh() (which acquires) may be called from
        # within the access_token property (which also acquires).
        self._refresh_lock = threading.RLock()

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
        """Exchange authorization code for access + refresh tokens."""
        auth_header = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()

        try:
            resp = self._session.post(
                SSO_TOKEN_URL,
                headers={
                    "Authorization": f"Basic {auth_header}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                },
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
        """
        with self._refresh_lock:
            auth_header = base64.b64encode(
                f"{self.client_id}:{self.client_secret}".encode()
            ).decode()

            try:
                resp = self._session.post(
                    SSO_TOKEN_URL,
                    headers={
                        "Authorization": f"Basic {auth_header}",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": self._refresh_token,
                    },
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

    def _decode_character_info(self):
        """Extract character ID and name from the JWT access token."""
        try:
            # JWT is three base64 segments separated by dots
            payload_b64 = self._access_token.split(".")[1]
            # Add padding
            padding = 4 - len(payload_b64) % 4
            if padding != 4:
                payload_b64 += "=" * padding
            payload = json.loads(base64.urlsafe_b64decode(payload_b64))

            # Subject format: "CHARACTER:EVE:1234567890"
            sub = payload.get("sub", "")
            parts = sub.split(":")
            if len(parts) == 3:
                self._character_id = int(parts[2])

            self._character_name = payload.get("name", "Unknown")

        except Exception as e:
            print(f"[ESI Auth] Error decoding JWT: {e}")

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
        no rename side effects. Writes to {path}.tmp then os.replace()s
        into place so a crash mid-write cannot corrupt the token file."""
        data = {
            "refresh_token": self._refresh_token,
            "character_id": self._character_id,
            "character_name": self._character_name,
        }
        final_path = self.token_file
        tmp_path = f"{final_path}.tmp"
        # Ensure parent exists (tests use tmp_path which already does).
        parent = os.path.dirname(final_path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except (OSError, AttributeError):
                pass
        try:
            os.replace(tmp_path, final_path)
        except OSError:
            # Replace failed (disk full, perms, antivirus lock, etc.).
            # Don't leave the .tmp orphaned; swallow cleanup errors so we
            # can re-raise the original write failure to the caller.
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise

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

    def esi_get(self, path: str, params: dict = None) -> dict | list | None:
        """Make an authenticated GET request to ESI."""
        token = self.access_token
        if not token:
            return None
        try:
            resp = self._session.get(
                f"{ESI_BASE}{path}",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
                timeout=10,
            )
            if resp.ok:
                return resp.json()
            # Suppress noisy expected errors
            if resp.status_code == 404 and "/fleet/" in path:
                pass  # Not in fleet
            elif resp.status_code == 403 and "/structures/" in path:
                pass  # No access to structure (different corp/alliance)
            else:
                print(f"[ESI] {path} returned {resp.status_code}")
        except Exception as e:
            print(f"[ESI] Error: {e}")
        return None

    def esi_post(self, path: str, json_data: dict = None) -> dict | list | None:
        """Make an authenticated POST request to ESI."""
        token = self.access_token
        if not token:
            return None
        try:
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

    # ── Character Info ───────────────────────────────────────────────────────

    def get_location(self) -> dict | None:
        """Get the authenticated character's current location."""
        if not self._character_id:
            return None
        return self.esi_get(f"/characters/{self._character_id}/location/")

    def get_ship_type(self) -> dict | None:
        """Get the authenticated character's current ship."""
        if not self._character_id:
            return None
        return self.esi_get(f"/characters/{self._character_id}/ship/")

    def get_online_status(self) -> dict | None:
        """Check if the character is currently online."""
        if not self._character_id:
            return None
        return self.esi_get(f"/characters/{self._character_id}/online/")

    def set_waypoint(self, destination_id: int, clear_other: bool = False,
                     add_to_beginning: bool = False) -> bool:
        """Set an in-game waypoint."""
        token = self.access_token
        if not token:
            return False
        try:
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

    def get_fleet_id(self) -> int | None:
        """Get the fleet ID the authenticated character is in."""
        if not self._character_id:
            return None
        data = self.esi_get(f"/characters/{self._character_id}/fleet/")
        if data and "fleet_id" in data:
            return data["fleet_id"]
        return None

    def get_fleet_members(self) -> list[dict] | None:
        """Get all members of the character's current fleet.
        Returns list of dicts with character_id, solar_system_id, ship_type_id, etc.
        """
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
        """Resolve system info -> constellation -> region name."""
        try:
            constellation_id = sys_info.get("constellation_id")
            if not constellation_id:
                return ""
            const_info = self.esi_get(f"/universe/constellations/{constellation_id}/")
            if not const_info:
                return ""
            region_id = const_info.get("region_id")
            if not region_id:
                return ""
            region_info = self.esi_get(f"/universe/regions/{region_id}/")
            if region_info:
                return region_info.get("name", "")
        except Exception:
            pass
        return ""

    # ── Names / Affiliations / Contacts ──────────────────────────────────────

    def resolve_names_to_ids(self, names: list[str]) -> dict[str, int]:
        """Resolve a list of EVE names to character IDs. Batches of 1000."""
        out: dict[str, int] = {}
        if not names:
            return out
        for i in range(0, len(names), 1000):
            chunk = names[i:i + 1000]
            data = self.esi_post("/universe/ids/", chunk)
            if not isinstance(data, dict):
                continue
            for entry in data.get("characters", []) or []:
                n = entry.get("name")
                cid = entry.get("id")
                if n and cid:
                    out[n] = cid
        return out

    def get_affiliations(self, char_ids: list[int]) -> list[dict]:
        """Resolve characters to corp/alliance affiliations. Batches of 1000."""
        out: list[dict] = []
        if not char_ids:
            return out
        for i in range(0, len(char_ids), 1000):
            chunk = char_ids[i:i + 1000]
            data = self.esi_post("/characters/affiliation/", chunk)
            if isinstance(data, list):
                out.extend(data)
        return out

    def get_personal_contacts(self) -> list[dict]:
        """Get the authenticated character's personal contacts."""
        if not self._character_id:
            return []
        data = self.esi_get(f"/characters/{self._character_id}/contacts/")
        return data if isinstance(data, list) else []

    def get_corp_contacts(self) -> list[dict]:
        """Get the authenticated character's corporation contacts."""
        if not self._character_id:
            return []
        info = self.esi_get(f"/characters/{self._character_id}/")
        if not isinstance(info, dict):
            return []
        corp_id = info.get("corporation_id")
        if not corp_id:
            return []
        data = self.esi_get(f"/corporations/{corp_id}/contacts/")
        return data if isinstance(data, list) else []

    def get_alliance_contacts(self) -> list[dict]:
        """Get the authenticated character's alliance contacts."""
        if not self._character_id:
            return []
        info = self.esi_get(f"/characters/{self._character_id}/")
        if not isinstance(info, dict):
            return []
        alliance_id = info.get("alliance_id")
        if not alliance_id:
            return []
        data = self.esi_get(f"/alliances/{alliance_id}/contacts/")
        return data if isinstance(data, list) else []

    def is_fleet_boss(self) -> bool:
        """Return True if the authenticated character is fleet commander."""
        if not self._character_id:
            return False
        data = self.esi_get(f"/characters/{self._character_id}/fleet/")
        if not isinstance(data, dict):
            return False
        return data.get("role") == "fleet_commander"

    # ── Ansiblex Discovery ───────────────────────────────────────────────────

    def _get_character_corp_id(self) -> int | None:
        """Get the character's corporation ID."""
        if not self._character_id:
            return None
        info = self.esi_get(f"/characters/{self._character_id}/")
        if info:
            return info.get("corporation_id")
        return None

    def _get_character_alliance_id(self) -> int | None:
        """Get the character's alliance ID."""
        if not self._character_id:
            return None
        info = self.esi_get(f"/characters/{self._character_id}/")
        if info:
            return info.get("alliance_id")
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
        # ESI search requires minimum 3 characters, so we try " » " with spaces
        for search_term in ["»", " » "]:
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
                page = 1
                while True:
                    corp_structs = self.esi_get(
                        f"/corporations/{corp_id}/structures/",
                        params={"page": page},
                    )
                    if not corp_structs:
                        break
                    for s in corp_structs:
                        # Ansiblex Jump Gate type_id = 35841
                        if s.get("type_id") == 35841:
                            structure_ids.add(s["structure_id"])
                    if len(corp_structs) < 250:
                        break
                    page += 1
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
                with open(new_path, "w") as f:
                    json.dump(data, f, indent=2)
                print(f"[ESI Auth] Migrated legacy tokens to {new_path}")
            os.remove(legacy)
            print("[ESI Auth] Removed legacy esi_tokens.json")
    except Exception as e:
        print(f"[ESI Auth] Legacy migration error: {e}")


def load_all_tokens(client_id: str, client_secret: str,
                    callback_url: str = "http://localhost:8834/callback") -> list[ESIAuth]:
    """Load all per-character ESI token files and return authenticated ESIAuth instances."""
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
