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

TOKEN_FILE = os.path.join(app_dir(), "esi_tokens.json")

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
        if self._access_token and self._expires_at:
            if datetime.now(timezone.utc) < self._expires_at:
                return self._access_token
            # Token expired, try refresh
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
        """Refresh the access token using the refresh token."""
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
            )
            if not resp.ok:
                print(f"[ESI Auth] Token refresh failed: {resp.status_code}")
                self._access_token = None
                return False

            data = resp.json()
            self._access_token = data["access_token"]
            self._refresh_token = data["refresh_token"]
            self._expires_at = datetime.now(timezone.utc) + timedelta(
                seconds=data.get("expires_in", 1199)
            )
            self._save_tokens()
            return True

        except Exception as e:
            print(f"[ESI Auth] Refresh error: {e}")
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
        self._access_token = None
        self._refresh_token = None
        self._expires_at = None
        self._character_id = None
        self._character_name = None
        if os.path.exists(self.token_file):
            os.remove(self.token_file)
        print("[ESI Auth] Logged out")

    # ── Token Persistence ────────────────────────────────────────────────────

    def _save_tokens(self):
        """Save tokens to disk."""
        data = {
            "refresh_token": self._refresh_token,
            "character_id": self._character_id,
            "character_name": self._character_name,
        }
        with open(self.token_file, "w") as f:
            json.dump(data, f, indent=2)

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
