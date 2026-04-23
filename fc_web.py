"""
FCTool Web Interface
Flask-based web dashboard for EVE Online fleet intel.
Runs alongside fc_headless.py on the VPS.

Usage:
    python fc_web.py                  # Run with config.json defaults
    python fc_web.py --config my.json # Custom config
    python fc_web.py --port 9090      # Override port
"""

import argparse
import base64
import json
import os
import queue
import secrets
import signal
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode

from flask import Flask, Response, jsonify, redirect, render_template, request, session
from flask_httpauth import HTTPBasicAuth

from app_path import app_dir
from jump_range import (
    JumpRangeChecker, calculate_ly_distance, get_stargate_route,
    get_system_info, save_route_cache, search_system,
)
from system_cache import get_sorted_names
from wh_route import fetch_connections, find_wh_route
from zkill_monitor import KillAlert, ZKillMonitor
from chat_monitor import ChatMonitor, ChatMessage
from intel_monitor import (
    IntelReport, parse_intel_message, scan_available_channels,
    INTEL_CHANNELS, DSCAN_URL_PATTERN, parse_dscan_text,
    make_dscan_summary, resolve_characters, coalesce_report,
    load_standings_whitelist,
)

import requests as http_requests

# ── Constants ────────────────────────────────────────────────────────────────

# Hard cap on response body size when fetching dscan URLs. A typical dscan
# paste is well under 200 KB; 1 MB gives generous headroom while preventing
# a compromised/malicious allowed host from exhausting memory.
DSCAN_MAX_FETCH_BYTES = 1024 * 1024

# Hosts from which we are willing to fetch dscan pastes. Used both by the
# /api/dscan/parse route guard and threaded through _fetch_with_size_cap so
# that redirect targets are re-validated against the same allowlist.
DSCAN_ALLOWED_HOSTS = frozenset({
    "dscan.info", "dscan.me", "adashboard.info",
    "zero.the-initiative.rocks",
})


class FetchSizeCapExceeded(Exception):
    """Raised when a fetched response exceeds the configured size cap."""


def _fetch_with_size_cap(
    url,
    *,
    max_bytes,
    connect_timeout=5,
    read_timeout=10,
    allowed_schemes=("https",),
    allowed_hosts=None,
    max_redirects=3,
):
    """Fetch a URL with a hard cap on response body size.

    Streams the response and aborts if the body exceeds max_bytes. Rejects
    URLs whose scheme is not in allowed_schemes (and whose hostname is not
    in allowed_hosts, if provided).

    Redirects are followed manually, up to max_redirects hops. Each hop's
    target is re-validated against allowed_schemes and allowed_hosts before
    being followed; an off-allowlist Location aborts with ok=False so a
    compromised allowed host cannot exfiltrate via a 3xx redirect.

    Returns a tuple (ok, status_code, text) where:
      - ok is True on a successful, within-cap fetch;
      - status_code is the HTTP status (or None if request failed before a response);
      - text is the decoded body (str) on success, or None on any failure.

    On cap exceeded: logs a warning, closes the response, returns ok=False.
    On non-HTTPS / disallowed scheme / off-allowlist host: returns ok=False.
    On too many redirects / redirect without Location: returns ok=False with
    the status from the last 3xx response.
    On connection/read/HTTP errors: returns ok=False.
    """
    from urllib.parse import urlparse, urljoin

    def _validate(target_url):
        try:
            parsed = urlparse(target_url)
        except Exception:
            return False, None
        scheme = (parsed.scheme or "").lower()
        if scheme not in allowed_schemes:
            return False, None
        host = (parsed.hostname or "").lower()
        if allowed_hosts is not None and host not in allowed_hosts:
            return False, None
        return True, parsed

    ok_initial, _parsed = _validate(url)
    if not ok_initial:
        print(f"[dscan] Rejecting URL (scheme/host): {url!r}")
        return (False, None, None)

    current_url = url
    for hop in range(max_redirects + 1):
        resp = None
        try:
            resp = http_requests.get(
                current_url,
                stream=True,
                timeout=(connect_timeout, read_timeout),
                allow_redirects=False,
            )
            status_code = resp.status_code

            # Handle redirects manually with per-hop allowlist re-validation.
            if 300 <= status_code < 400:
                location = resp.headers.get("Location")
                if not location:
                    print(f"[dscan] Redirect {status_code} without Location (url={current_url!r})")
                    return (False, status_code, None)
                if hop >= max_redirects:
                    print(
                        f"[dscan] Redirect chain exceeded {max_redirects} hops "
                        f"(url={current_url!r})"
                    )
                    return (False, status_code, None)
                next_url = urljoin(current_url, location)
                ok_next, _np = _validate(next_url)
                if not ok_next:
                    print(
                        f"[dscan] Rejecting redirect target (scheme/host): "
                        f"{next_url!r} from {current_url!r}"
                    )
                    return (False, status_code, None)
                current_url = next_url
                continue

            if not resp.ok:
                return (False, status_code, None)

            # Check declared Content-Length up front
            content_length = resp.headers.get("Content-Length")
            if content_length is not None:
                try:
                    declared = int(content_length)
                except (TypeError, ValueError):
                    declared = None
                if declared is not None and declared > max_bytes:
                    print(
                        f"[dscan] Refusing fetch: Content-Length {declared} exceeds cap "
                        f"{max_bytes} (url={current_url!r})"
                    )
                    return (False, status_code, None)

            buf = bytearray()
            for chunk in resp.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    print(
                        f"[dscan] Aborting fetch: body exceeded cap {max_bytes} "
                        f"(url={current_url!r})"
                    )
                    return (False, status_code, None)

            encoding = resp.encoding or resp.apparent_encoding or "utf-8"
            try:
                text = bytes(buf).decode(encoding, errors="replace")
            except (LookupError, TypeError):
                text = bytes(buf).decode("utf-8", errors="replace")
            return (True, status_code, text)
        except http_requests.RequestException as e:
            print(f"[dscan] Fetch error for {current_url!r}: {e}")
            return (False, None, None)
        finally:
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass

    # Loop exhausted without return (shouldn't happen — redirect branch handles cap).
    return (False, None, None)


# ── Flask App ────────────────────────────────────────────────────────────────

app = Flask(__name__, template_folder=os.path.join(app_dir(), "templates"))
app.secret_key = secrets.token_hex(32)
auth = HTTPBasicAuth()

# ── Global State ─────────────────────────────────────────────────────────────

config: dict = {}
zkill_monitor: ZKillMonitor | None = None
_ansiblex_connections: list[str] = []
_ansiblex_id_pairs: dict[tuple[int, int], tuple[str, str]] = {}
_system_names: list[str] = []
_start_time: float = 0
_alert_count: int = 0

# SSE fan-out
_alert_subscribers: list[queue.Queue] = []
_subscribers_lock = threading.Lock()
_recent_alerts: list[dict] = []
_recent_lock = threading.Lock()
MAX_RECENT = 50

SECONDARY_BRIDGE_SYSTEMS = [
    "6RCQ-V", "F7C-H0", "CL6-ZG", "HPS5-C",
    "Korasen", "Y-2ANO", "NOL-M9",
]

# Intel Fusion state
_intel_monitor: ChatMonitor | None = None
_intel_enabled: bool = False
_intel_channels_enabled: set[str] = set()
_intel_lock = threading.Lock()
_intel_thread: threading.Thread | None = None

# ESI SSO state
_esi_tokens: dict = {}  # {access_token, refresh_token, expires_at, character_id, character_name}
_esi_lock = threading.Lock()
_sso_states: dict[str, float] = {}  # state -> timestamp (CSRF protection)

SSO_AUTH_URL = "https://login.eveonline.com/v2/oauth/authorize"
SSO_TOKEN_URL = "https://login.eveonline.com/v2/oauth/token"
ESI_BASE = "https://esi.evetech.net/latest"
ESI_HEADERS = {"User-Agent": "FCTool/1.0 (EVE FC Assistant)"}

ESI_SCOPES = [
    "publicData",
    "esi-location.read_location.v1",
    "esi-ui.write_waypoint.v1",
    "esi-search.search_structures.v1",
    "esi-universe.read_structures.v1",
]


# ── Auth ─────────────────────────────────────────────────────────────────────

@auth.verify_password
def verify_password(username, password):
    web_cfg = config.get("web", {})
    if (username == web_cfg.get("username", "")
            and password == web_cfg.get("password", "")):
        return username
    return None


# ── ESI SSO ──────────────────────────────────────────────────────────────────

def _get_esi_callback_url():
    """Get the ESI callback URL for the web server."""
    web_cfg = config.get("web", {})
    # Use configured callback or auto-build from host/port
    cb = config.get("esi", {}).get("web_callback_url", "")
    if cb:
        return cb
    port = web_cfg.get("port", 8080)
    return f"http://localhost:{port}/sso/callback"


def _esi_auth_header():
    esi_cfg = config.get("esi", {})
    creds = f"{esi_cfg['client_id']}:{esi_cfg.get('client_secret', '')}"
    return base64.b64encode(creds.encode()).decode()


def _esi_refresh_token():
    """Refresh ESI access token."""
    global _esi_tokens
    with _esi_lock:
        rt = _esi_tokens.get("refresh_token")
    if not rt:
        return False
    try:
        resp = http_requests.post(
            SSO_TOKEN_URL,
            headers={
                "Authorization": f"Basic {_esi_auth_header()}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "refresh_token", "refresh_token": rt},
            timeout=10,
        )
        if resp.ok:
            data = resp.json()
            with _esi_lock:
                _esi_tokens["access_token"] = data["access_token"]
                _esi_tokens["refresh_token"] = data["refresh_token"]
                _esi_tokens["expires_at"] = (
                    datetime.now(timezone.utc) + timedelta(seconds=data.get("expires_in", 1199))
                ).isoformat()
                _decode_jwt(data["access_token"])
            _save_esi_tokens()
            return True
        print(f"[ESI] Refresh failed: {resp.status_code}")
    except Exception as e:
        print(f"[ESI] Refresh error: {e}")
    return False


def _decode_jwt(token: str):
    """Extract character info from JWT. Must hold _esi_lock."""
    try:
        payload_b64 = token.split(".")[1]
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        sub = payload.get("sub", "")
        parts = sub.split(":")
        if len(parts) == 3:
            _esi_tokens["character_id"] = int(parts[2])
        _esi_tokens["character_name"] = payload.get("name", "Unknown")
    except Exception as e:
        print(f"[ESI] JWT decode error: {e}")


def _get_esi_access_token() -> str | None:
    """Get a valid access token, refreshing if needed."""
    with _esi_lock:
        exp = _esi_tokens.get("expires_at")
        token = _esi_tokens.get("access_token")
    if token and exp:
        try:
            exp_dt = datetime.fromisoformat(exp)
            if datetime.now(timezone.utc) < exp_dt:
                return token
        except Exception:
            pass
    if _esi_refresh_token():
        with _esi_lock:
            return _esi_tokens.get("access_token")
    return None


def _esi_get(path: str, params: dict = None) -> dict | list | None:
    """Make authenticated ESI GET request."""
    token = _get_esi_access_token()
    if not token:
        return None
    try:
        resp = http_requests.get(
            f"{ESI_BASE}{path}",
            headers={**ESI_HEADERS, "Authorization": f"Bearer {token}"},
            params=params, timeout=10,
        )
        if resp.ok:
            return resp.json()
    except Exception:
        pass
    return None


def _save_esi_tokens():
    path = os.path.join(app_dir(), "esi_tokens_web.json")
    with _esi_lock:
        data = {k: v for k, v in _esi_tokens.items() if k != "access_token"}
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def _load_esi_tokens():
    global _esi_tokens
    path = os.path.join(app_dir(), "esi_tokens_web.json")
    if not os.path.exists(path):
        return
    try:
        with open(path) as f:
            data = json.load(f)
        with _esi_lock:
            _esi_tokens = data
        if data.get("refresh_token"):
            if _esi_refresh_token():
                with _esi_lock:
                    name = _esi_tokens.get("character_name", "?")
                print(f"[ESI] Restored session: {name}")
            else:
                print("[ESI] Stored token expired, re-login needed")
    except Exception as e:
        print(f"[ESI] Error loading tokens: {e}")


# ── SSO Routes ───────────────────────────────────────────────────────────────

@app.route("/sso/login")
@auth.login_required
def sso_login():
    """Initiate EVE SSO login."""
    esi_cfg = config.get("esi", {})
    if not esi_cfg.get("client_id"):
        return jsonify({"error": "ESI not configured"}), 400

    state = secrets.token_urlsafe(32)
    _sso_states[state] = time.time()
    # Clean old states
    cutoff = time.time() - 300
    for s in list(_sso_states):
        if _sso_states[s] < cutoff:
            del _sso_states[s]

    params = {
        "response_type": "code",
        "redirect_uri": _get_esi_callback_url(),
        "client_id": esi_cfg["client_id"],
        "scope": " ".join(ESI_SCOPES),
        "state": state,
    }
    return redirect(f"{SSO_AUTH_URL}?{urlencode(params)}")


@app.route("/sso/callback")
def sso_callback():
    """Handle EVE SSO callback."""
    code = request.args.get("code")
    state = request.args.get("state")

    if not code or not state or state not in _sso_states:
        return "Authentication failed: invalid state", 400
    del _sso_states[state]

    esi_cfg = config.get("esi", {})
    try:
        resp = http_requests.post(
            SSO_TOKEN_URL,
            headers={
                "Authorization": f"Basic {_esi_auth_header()}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "authorization_code",
                "code": code,
            },
            timeout=10,
        )
        if not resp.ok:
            return f"Token exchange failed: {resp.status_code}", 400

        data = resp.json()
        with _esi_lock:
            _esi_tokens["access_token"] = data["access_token"]
            _esi_tokens["refresh_token"] = data["refresh_token"]
            _esi_tokens["expires_at"] = (
                datetime.now(timezone.utc) + timedelta(seconds=data.get("expires_in", 1199))
            ).isoformat()
            _decode_jwt(data["access_token"])
            name = _esi_tokens.get("character_name", "Unknown")

        _save_esi_tokens()
        print(f"[ESI] Logged in as {name}")

        # HTML response that closes the window or redirects
        return (
            f'<html><body style="background:#1a1a2e;color:#00ff88;font-family:monospace;'
            f'text-align:center;padding-top:100px">'
            f'<h1>Logged in as {name}</h1>'
            f'<p>You can close this window.</p>'
            f'<script>setTimeout(()=>window.location="/",2000)</script>'
            f'</body></html>'
        )
    except Exception as e:
        return f"Error: {e}", 500


@app.route("/sso/logout")
@auth.login_required
def sso_logout():
    global _esi_tokens
    with _esi_lock:
        _esi_tokens = {}
    path = os.path.join(app_dir(), "esi_tokens_web.json")
    if os.path.exists(path):
        os.remove(path)
    return jsonify({"ok": True})


@app.route("/api/sso/status")
@auth.login_required
def sso_status():
    with _esi_lock:
        name = _esi_tokens.get("character_name")
        cid = _esi_tokens.get("character_id")
    authenticated = name is not None
    result = {"authenticated": authenticated}
    if authenticated:
        result["character_name"] = name
        result["character_id"] = cid
    return jsonify(result)


@app.route("/api/sso/location")
@auth.login_required
def sso_location():
    """Get character's current location."""
    with _esi_lock:
        cid = _esi_tokens.get("character_id")
    if not cid:
        return jsonify({"error": "Not logged in"}), 401
    loc = _esi_get(f"/characters/{cid}/location/")
    if loc:
        sys_id = loc.get("solar_system_id")
        if sys_id:
            info = get_system_info(sys_id)
            loc["system_name"] = info.get("name", "") if info else ""
    return jsonify(loc or {})


@app.route("/api/sso/set_waypoint", methods=["POST"])
@auth.login_required
def sso_set_waypoint():
    """Set in-game waypoint."""
    token = _get_esi_access_token()
    if not token:
        return jsonify({"error": "Not logged in"}), 401
    data = request.get_json(silent=True) or {}
    system_name = data.get("system_name", "")
    sys_id = search_system(system_name) if system_name else data.get("system_id")
    if not sys_id:
        return jsonify({"error": "System not found"}), 400
    try:
        resp = http_requests.post(
            f"{ESI_BASE}/ui/autopilot/waypoint/",
            headers={**ESI_HEADERS, "Authorization": f"Bearer {token}"},
            params={
                "destination_id": sys_id,
                "clear_other_waypoints": str(data.get("clear", False)).lower(),
                "add_to_beginning": "false",
            },
            timeout=10,
        )
        return jsonify({"ok": resp.status_code == 204})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Alert Serialization ─────────────────────────────────────────────────────

def serialize_alert(alert: KillAlert) -> dict:
    return {
        "type": "zkill",
        "system_id": alert.system_id,
        "system_name": alert.system_name,
        "region_id": alert.region_id,
        "region_name": alert.region_name,
        "kill_count": alert.kill_count,
        "total_value_millions": round(alert.total_value_millions, 1),
        "pilots_on_field": alert.pilots_on_field,
        "capitals_involved": alert.capitals_involved,
        "capital_breakdown": alert.capital_breakdown,
        "timestamp": alert.timestamp.isoformat() if alert.timestamp else "",
        "zkill_url": alert.zkill_url,
        "dotlan_url": alert.dotlan_url,
        "zkill_related_url": alert.zkill_related_url,
        "warbeacon_url": alert.warbeacon_url,
        "top_alliances": alert.top_alliances,
        "route_from_staging": alert.route_from_staging,
        "is_update": alert.is_update,
    }


def on_zkill_alert(alert: KillAlert):
    global _alert_count
    _alert_count += 1

    staging = config.get("zkillboard", {}).get("staging_system", "")
    if staging:
        try:
            origin_id = search_system(staging)
            dest_id = search_system(alert.system_name)
            if origin_id and dest_id:
                conns = _ansiblex_connections or None
                route = get_stargate_route(origin_id, dest_id, connections=conns)
                if route:
                    alert.route_from_staging = f"{staging} -> {alert.system_name}: {len(route) - 1} jumps"
        except Exception:
            pass

    data = serialize_alert(alert)

    with _recent_lock:
        _recent_alerts.insert(0, data)
        while len(_recent_alerts) > MAX_RECENT:
            _recent_alerts.pop()

    with _subscribers_lock:
        dead = []
        for q in _alert_subscribers:
            try:
                q.put_nowait(data)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _alert_subscribers.remove(q)


# ── Ansiblex Resolution ─────────────────────────────────────────────────────

def _resolve_ansiblex():
    global _ansiblex_connections, _ansiblex_id_pairs
    pairs = config.get("ansiblex_connections", [])
    resolved = []
    id_pairs = {}
    for pair in pairs:
        if len(pair) == 2:
            id1 = search_system(pair[0])
            id2 = search_system(pair[1])
            if id1 and id2:
                resolved.append(f"{id1}|{id2}")
                id_pairs[(id1, id2)] = (pair[0], pair[1])
                id_pairs[(id2, id1)] = (pair[1], pair[0])
    _ansiblex_connections = resolved
    _ansiblex_id_pairs = id_pairs
    print(f"[Web] Resolved {len(resolved)} Ansiblex gate(s)")
    save_route_cache()


def _get_conns() -> list[str] | None:
    global _ansiblex_connections
    if _ansiblex_connections:
        return _ansiblex_connections
    pairs = config.get("ansiblex_connections", [])
    if not pairs:
        return None
    _resolve_ansiblex()
    return _ansiblex_connections or None


def _prewarm_cache():
    """Pre-resolve commonly used systems."""
    systems = list(SECONDARY_BRIDGE_SYSTEMS)
    staging = config.get("zkillboard", {}).get("staging_system", "")
    if staging:
        systems.insert(0, staging)
    for name in systems:
        sid = search_system(name)
        if sid:
            get_system_info(sid)
    save_route_cache()
    print(f"[Web] Pre-warmed {len(systems)} system(s)")


# ── System Name Loading ──────────────────────────────────────────────────────

def _load_system_names():
    global _system_names
    try:
        _system_names = get_sorted_names()
        print(f"[Web] Loaded {len(_system_names)} system names")
    except Exception as e:
        print(f"[Web] Error loading system names: {e}")


# ── Page Routes ──────────────────────────────────────────────────────────────

@app.route("/")
@auth.login_required
def index():
    staging = config.get("zkillboard", {}).get("staging_system", "")
    has_esi = bool(config.get("esi", {}).get("client_id"))
    return render_template("index.html", staging=staging, has_esi=has_esi)


# ── API Routes ───────────────────────────────────────────────────────────────

@app.route("/api/status")
@auth.login_required
def api_status():
    uptime = int(time.time() - _start_time)
    hours, rem = divmod(uptime, 3600)
    mins, secs = divmod(rem, 60)
    with _esi_lock:
        esi_name = _esi_tokens.get("character_name")
    return jsonify({
        "uptime": f"{hours}h {mins}m {secs}s",
        "alerts": _alert_count,
        "ansiblex_gates": len(_ansiblex_connections),
        "systems_cached": len(_system_names),
        "esi_character": esi_name,
    })


@app.route("/api/alerts/stream")
@auth.login_required
def alert_stream():
    def generate():
        q = queue.Queue(maxsize=100)
        with _subscribers_lock:
            _alert_subscribers.append(q)
        try:
            with _recent_lock:
                for alert_data in reversed(_recent_alerts):
                    yield f"data: {json.dumps(alert_data)}\n\n"
            while True:
                try:
                    data = q.get(timeout=25)
                    yield f"data: {json.dumps(data)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            with _subscribers_lock:
                if q in _alert_subscribers:
                    _alert_subscribers.remove(q)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/alerts/recent")
@auth.login_required
def recent_alerts():
    with _recent_lock:
        return jsonify(_recent_alerts)


@app.route("/api/systems/search")
@auth.login_required
def systems_search():
    q = request.args.get("q", "").strip().lower()
    if len(q) < 2:
        return jsonify([])
    matches = [n for n in _system_names if n.lower().startswith(q)][:10]
    return jsonify(matches)


@app.route("/api/route/find", methods=["POST"])
@auth.login_required
def route_find():
    data = request.get_json(silent=True) or {}
    origin = data.get("origin", "").strip()
    destination = data.get("destination", "").strip()
    ship_size = data.get("ship_size", "any")

    if not origin or not destination:
        return jsonify({"error": "Origin and destination required"}), 400

    conns = _get_conns()
    result = find_wh_route(origin, destination, ship_size, connections=conns)
    save_route_cache()

    if result is None:
        return jsonify({"error": "Could not find systems"})

    leg_ansiblex = {}
    if result.legs:
        for idx, leg in enumerate(result.legs):
            if leg["type"] == "gate" and conns:
                gates = _find_ansiblex_in_leg(leg["from"], leg["to"], conns)
                if gates:
                    leg_ansiblex[str(idx)] = gates

    return jsonify({
        "origin": result.origin,
        "destination": result.destination,
        "gate_jumps_direct": result.gate_jumps_direct,
        "total_jumps_via_wh": result.total_jumps_via_wh,
        "jumps_saved": result.jumps_saved,
        "hub_name": result.hub_name,
        "legs": result.legs,
        "leg_ansiblex": leg_ansiblex,
        "entry_connection": _serialize_wh_conn(result.entry_connection),
        "exit_connection": _serialize_wh_conn(result.exit_connection),
        "ansiblex_count": len(_ansiblex_connections),
    })


def _serialize_wh_conn(conn) -> dict | None:
    if conn is None:
        return None
    return {
        "hub_name": conn.hub_name,
        "dest_system_name": conn.dest_system_name,
        "dest_region_name": conn.dest_region_name,
        "dest_signature": conn.dest_signature,
        "hub_signature": conn.hub_signature,
        "max_ship_size": conn.max_ship_size,
        "remaining_hours": conn.remaining_hours,
        "wh_type": conn.wh_type,
    }


def _find_ansiblex_in_leg(from_name, to_name, conns):
    gates_used = []
    from_id = search_system(from_name)
    to_id = search_system(to_name)
    if from_id and to_id:
        raw_route = get_stargate_route(from_id, to_id, connections=conns)
        if raw_route:
            for i in range(len(raw_route) - 1):
                pair = (raw_route[i], raw_route[i + 1])
                if pair in _ansiblex_id_pairs:
                    names = _ansiblex_id_pairs[pair]
                    gates_used.append(list(names))
    return gates_used


@app.route("/api/jump/check", methods=["POST"])
@auth.login_required
def jump_check():
    data = request.get_json(silent=True) or {}
    origin = data.get("origin", "").strip()
    destination = data.get("destination", "").strip()
    ship_type = data.get("ship_type", "Dreadnought")

    if not origin or not destination:
        return jsonify({"error": "Origin and destination required"}), 400

    conns = _get_conns()
    checker = JumpRangeChecker(ship_type, jdc_level=5)
    result = checker.check_range(origin, destination, connections=conns)

    if "error" in result:
        return jsonify(result), 400

    if not result.get("in_range"):
        dest_id = result.get("destination_id")
        if dest_id:
            titan_checker = JumpRangeChecker("Titan", jdc_level=5)
            titan_range = titan_checker.jump_range
            sec_ids = {}
            for sys_name in SECONDARY_BRIDGE_SYSTEMS:
                sid = search_system(sys_name)
                if sid:
                    sec_ids[sys_name] = sid
                    get_system_info(sid)
            get_system_info(dest_id)
            save_route_cache()

            secondary = []
            for sys_name in SECONDARY_BRIDGE_SYSTEMS:
                sid = sec_ids.get(sys_name)
                if sid:
                    dist = calculate_ly_distance(sid, dest_id)
                    if dist is not None:
                        secondary.append({
                            "system": sys_name,
                            "in_range": dist <= titan_range,
                            "distance_ly": round(dist, 2),
                            "range_ly": round(titan_range, 2),
                        })
            result["secondary"] = secondary

    save_route_cache()
    return jsonify(result)


@app.route("/api/connections")
@auth.login_required
def wh_connections():
    conns = fetch_connections()
    result = []
    for c in conns:
        result.append({
            "hub_name": c.hub_name,
            "dest_system_name": c.dest_system_name,
            "dest_region_name": c.dest_region_name,
            "dest_security_class": c.dest_security_class,
            "dest_signature": c.dest_signature,
            "hub_signature": c.hub_signature,
            "max_ship_size": c.max_ship_size,
            "remaining_hours": c.remaining_hours,
            "wh_type": c.wh_type,
        })
    return jsonify(result)


# ── Intel Fusion ─────────────────────────────────────────────────────────────

def _check_cyno_beacon(system_id: int) -> bool:
    """Check if a Pharolux Cynosural Beacon exists in the given system via ESI."""
    with _esi_lock:
        char_id = _esi_tokens.get("character_id")
    if not char_id:
        return False
    try:
        data = _esi_get(
            f"/characters/{char_id}/search/",
            params={"categories": "structure", "search": "Pharolux", "strict": "false"},
        )
        if not data or "structure" not in data:
            return False
        for struct_id in data["structure"][:20]:
            info = _esi_get(f"/universe/structures/{struct_id}/")
            if info and info.get("solar_system_id") == system_id:
                return True
    except Exception:
        pass
    return False


def _on_intel_message(msg: ChatMessage):
    """Callback for ChatMonitor — processes intel channel messages."""
    with _intel_lock:
        enabled_channels = set(_intel_channels_enabled)
    if msg.channel not in enabled_channels:
        return

    report = parse_intel_message(msg, search_system)
    if report is None:
        return

    # Skip clear reports entirely
    if report.report_type == "clear":
        return

    # Skip pure info messages with no system
    if report.report_type == "info" and not report.system_name:
        return

    # Coalesce rapid-fire posts from same reporter
    report, is_new = coalesce_report(report)
    if not is_new:
        # This was merged into an existing report — push update
        data = report.serialize()
        data["is_update"] = True
        with _subscribers_lock:
            dead = []
            for q in _alert_subscribers:
                try:
                    q.put_nowait(data)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                _alert_subscribers.remove(q)
        return

    # Calculate route from staging
    staging = config.get("zkillboard", {}).get("staging_system", "")
    if staging and report.system_id:
        try:
            origin_id = search_system(staging)
            if origin_id:
                conns = _ansiblex_connections or None
                route = get_stargate_route(origin_id, report.system_id, connections=conns)
                if route:
                    report.route_from_staging = f"{staging} -> {report.system_name}: {len(route) - 1} jumps"
        except Exception:
            pass

    # Resolve region name
    if report.system_id and not report.region_name:
        try:
            info = get_system_info(report.system_id)
            if info:
                region_id = info.get("constellation", {}).get("region_id") or info.get("region_id")
                # Try to get region from the system info cache
                report.region_name = info.get("region_name", "")
        except Exception:
            pass

    # Check for Pharolux cyno beacon via ESI (no failure if not authenticated)
    if report.system_id:
        try:
            report.has_cyno_beacon = _check_cyno_beacon(report.system_id)
        except Exception:
            pass

    # Resolve character names (public ESI, no auth needed)
    try:
        chars = resolve_characters(report.raw_message, report.system_name, search_system)
        if chars:
            report.characters = chars
    except Exception:
        pass

    data = report.serialize()

    # Push to SSE subscribers (same fan-out as zKill alerts)
    with _recent_lock:
        _recent_alerts.insert(0, data)
        while len(_recent_alerts) > MAX_RECENT:
            _recent_alerts.pop()

    with _subscribers_lock:
        dead = []
        for q in _alert_subscribers:
            try:
                q.put_nowait(data)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _alert_subscribers.remove(q)

    # If there's a dscan URL, try to parse it in background
    if report.dscan_url:
        threading.Thread(
            target=_fetch_and_update_dscan, args=(report, data), daemon=True
        ).start()


def _fetch_and_update_dscan(report: IntelReport, original_data: dict):
    """Fetch dscan URL and push an updated intel report with ship data."""
    try:
        ok, _status, text = _fetch_with_size_cap(
            report.dscan_url,
            max_bytes=DSCAN_MAX_FETCH_BYTES,
            allowed_hosts=DSCAN_ALLOWED_HOSTS,
        )
        if not ok or text is None:
            return
        dscan_result = parse_dscan_text(text)
        if dscan_result["total"] == 0:
            return

        report.dscan_ships = dscan_result["ships"]
        report.dscan_total = dscan_result["total"]
        report.dscan_summary = make_dscan_summary(dscan_result["ships"], dscan_result["total"])

        updated = report.serialize()
        updated["is_update"] = True

        with _subscribers_lock:
            dead = []
            for q in _alert_subscribers:
                try:
                    q.put_nowait(updated)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                _alert_subscribers.remove(q)
    except Exception:
        pass


def _start_intel_monitor():
    """Start the ChatMonitor for intel channels."""
    global _intel_monitor, _intel_thread
    logs_path = config.get("eve_logs_path", "")
    if not logs_path or not os.path.isdir(logs_path):
        print(f"[Intel] Chat logs path not found: {logs_path}")
        return False

    tracked_char = config.get("tracked_character", "")
    _intel_monitor = ChatMonitor(
        logs_path=logs_path,
        poll_interval=config.get("poll_interval_seconds", 1.0),
        listener_filter=tracked_char or None,
        channel_filters=sorted(INTEL_CHANNELS),
    )
    _intel_monitor.on_message(_on_intel_message)
    _intel_thread = threading.Thread(target=_intel_monitor.run, daemon=True)
    _intel_thread.start()
    print(f"[Intel] Monitor started (path={logs_path}, char={tracked_char})")
    return True


def _stop_intel_monitor():
    """Stop the ChatMonitor for intel channels."""
    global _intel_monitor, _intel_thread
    if _intel_monitor:
        _intel_monitor.stop()
        _intel_monitor = None
    _intel_thread = None
    print("[Intel] Monitor stopped")


@app.route("/api/intel/channels")
@auth.login_required
def intel_channels():
    """Return list of intel channels with their availability status."""
    logs_path = config.get("eve_logs_path", "")
    tracked_char = config.get("tracked_character", "")
    channels = scan_available_channels(logs_path, tracked_char)
    with _intel_lock:
        enabled = set(_intel_channels_enabled)
        fusion_on = _intel_enabled
    for ch in channels:
        ch["enabled"] = ch["name"] in enabled
        del ch["file_path"]  # Don't expose file paths to frontend
    return jsonify({"enabled": fusion_on, "channels": channels})


@app.route("/api/intel/toggle", methods=["POST"])
@auth.login_required
def intel_toggle():
    """Enable or disable intel fusion."""
    global _intel_enabled
    data = request.get_json(silent=True) or {}
    enable = data.get("enabled", not _intel_enabled)

    if enable and not _intel_enabled:
        success = _start_intel_monitor()
        if not success:
            return jsonify({"error": "Could not start intel monitor (check eve_logs_path in config)"}), 400
        _intel_enabled = True
        # Auto-enable all active channels
        logs_path = config.get("eve_logs_path", "")
        tracked_char = config.get("tracked_character", "")
        channels = scan_available_channels(logs_path, tracked_char)
        with _intel_lock:
            _intel_channels_enabled.clear()
            for ch in channels:
                if ch["active"]:
                    _intel_channels_enabled.add(ch["name"])
    elif not enable and _intel_enabled:
        _stop_intel_monitor()
        _intel_enabled = False
        with _intel_lock:
            _intel_channels_enabled.clear()

    return jsonify({"enabled": _intel_enabled})


@app.route("/api/intel/channels/update", methods=["POST"])
@auth.login_required
def intel_channels_update():
    """Update which intel channels are enabled."""
    data = request.get_json(silent=True) or {}
    channels = data.get("channels", [])
    with _intel_lock:
        _intel_channels_enabled.clear()
        for name in channels:
            if name in INTEL_CHANNELS:
                _intel_channels_enabled.add(name)
    return jsonify({"channels": sorted(_intel_channels_enabled)})


@app.route("/api/dscan/parse")
@auth.login_required
def dscan_parse():
    """Fetch and parse a d-scan URL."""
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL required"}), 400

    # Validate domain
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.hostname not in DSCAN_ALLOWED_HOSTS:
            return jsonify({"error": f"Domain not allowed: {parsed.hostname}"}), 400
    except Exception:
        return jsonify({"error": "Invalid URL"}), 400

    try:
        ok, status_code, text = _fetch_with_size_cap(
            url,
            max_bytes=DSCAN_MAX_FETCH_BYTES,
            allowed_hosts=DSCAN_ALLOWED_HOSTS,
        )
        if not ok or text is None:
            if status_code is not None and not (200 <= status_code < 300):
                return jsonify({"error": f"Fetch failed: {status_code}"}), 502
            return jsonify({"error": "Fetch failed"}), 502
        result = parse_dscan_text(text)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    global config, zkill_monitor, _start_time

    parser = argparse.ArgumentParser(description="FCTool Web Interface")
    parser.add_argument("--config", default=os.path.join(app_dir(), "config.json"))
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    if not os.path.exists(args.config):
        print(f"[ERROR] Config not found: {args.config}")
        sys.exit(1)
    with open(args.config, "r") as f:
        config = json.load(f)

    web_cfg = config.get("web", {})
    if not web_cfg.get("username") or not web_cfg.get("password"):
        print("[ERROR] Set web.username and web.password in config.json")
        sys.exit(1)

    port = args.port or web_cfg.get("port", 8080)
    host = web_cfg.get("host", "0.0.0.0")

    _start_time = time.time()

    # Load ESI tokens if available
    _load_esi_tokens()

    # Background init
    threading.Thread(target=_resolve_ansiblex, daemon=True).start()
    threading.Thread(target=_load_system_names, daemon=True).start()
    threading.Thread(target=_prewarm_cache, daemon=True).start()

    # Start zKill monitor
    zk = config.get("zkillboard", {})
    zkill_monitor = ZKillMonitor(
        watch_regions=zk.get("watch_regions", []),
        watch_alliances=zk.get("watch_alliances", []),
        watch_systems=zk.get("watch_systems", []),
        min_kill_value_millions=zk.get("min_kill_value_millions", 0),
        min_pilots_involved=1,
        alert_window_seconds=zk.get("alert_window_seconds", 300),
        on_alert=on_zkill_alert,
        watch_all=True,
    )
    zkill_monitor.start()

    print(f"[Web] FCTool Web Interface starting on {host}:{port}")
    print(f"[Web] zKill monitor active (watch_all mode)")

    def shutdown(sig, frame):
        print(f"\n[Web] Shutting down...")
        if zkill_monitor:
            zkill_monitor.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
