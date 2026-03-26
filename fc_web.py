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

import requests as http_requests

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
