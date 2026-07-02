# fleet_esi.py
"""ESI fleet-structure writes (move members, create/rename/delete wings & squads).

Every call goes through an injectable `session` whose `.request(method, path,
json=None)` returns a requests-style response (`.status_code`, `.ok`, `.json()`,
`.text`). `AuthEsiSession` adapts an `ESIAuth` for production; tests pass a fake.

Error policy (spec §3): retry ONCE on a 5xx or a network exception; raise
`FleetESIError("boss_lost")` on 403, `FleetESIError("not_found")` on 404, and
`FleetESIError("http_error", status=...)` on any other non-expected status or a
second failure. The caller (`fleet_template_window`) handles `FleetESIError`.

ESI wing/squad names are capped at 10 characters; every name is clamped.
"""
from __future__ import annotations

_NAME_MAX = 10


class FleetESIError(Exception):
    def __init__(self, reason: str, status: int | None = None, detail: str = ""):
        super().__init__(f"{reason} (status={status}) {detail}".strip())
        self.reason = reason      # "boss_lost" | "not_found" | "http_error" | "no_token" | "network"
        self.status = status
        self.detail = detail


def _call(session, method: str, path: str, *, json=None, expect=(200, 201, 204)):
    """Single ESI call with retry-once-on-5xx/network. Returns the response."""
    last_exc = None
    for attempt in (1, 2):
        try:
            resp = session.request(method, path, json=json)
        except FleetESIError:
            raise
        except Exception as e:   # network/transport error
            last_exc = e
            if attempt == 2:
                raise FleetESIError("network", detail=str(e))
            continue
        if resp.status_code in expect:
            return resp
        if resp.status_code == 403:
            raise FleetESIError("boss_lost", status=403)
        if resp.status_code == 404:
            raise FleetESIError("not_found", status=404)
        if 500 <= resp.status_code < 600 and attempt == 1:
            continue   # retry once on 5xx
        raise FleetESIError("http_error", status=resp.status_code,
                            detail=getattr(resp, "text", ""))
    # Unreachable: loop either returns or raises.
    raise FleetESIError("network", detail=str(last_exc))


def get_wings(session, fleet_id: int) -> list:
    """GET /fleets/{id}/wings/ → list of {id, name, squads:[{id, name}]}."""
    resp = _call(session, "GET", f"/fleets/{fleet_id}/wings/", expect=(200,))
    data = resp.json()
    return data if isinstance(data, list) else []


def create_wing(session, fleet_id: int, name: str | None = None) -> int:
    """POST a new wing; rename it if `name` is given. Returns the new wing_id."""
    resp = _call(session, "POST", f"/fleets/{fleet_id}/wings/", expect=(201, 200))
    wing_id = resp.json().get("wing_id")
    if name and name.strip():
        rename_wing(session, fleet_id, wing_id, name)
    return wing_id


def create_squad(session, fleet_id: int, wing_id: int, name: str | None = None) -> int:
    """POST a new squad into a wing; rename it if `name` is given. Returns squad_id."""
    resp = _call(session, "POST", f"/fleets/{fleet_id}/wings/{wing_id}/squads/",
                 expect=(201, 200))
    squad_id = resp.json().get("squad_id")
    if name and name.strip():
        rename_squad(session, fleet_id, squad_id, name)
    return squad_id


def rename_wing(session, fleet_id: int, wing_id: int, name: str) -> None:
    _call(session, "PUT", f"/fleets/{fleet_id}/wings/{wing_id}/",
          json={"name": name[:_NAME_MAX]}, expect=(204,))


def rename_squad(session, fleet_id: int, squad_id: int, name: str) -> None:
    _call(session, "PUT", f"/fleets/{fleet_id}/squads/{squad_id}/",
          json={"name": name[:_NAME_MAX]}, expect=(204,))


def delete_wing(session, fleet_id: int, wing_id: int) -> None:
    _call(session, "DELETE", f"/fleets/{fleet_id}/wings/{wing_id}/", expect=(204,))


def delete_squad(session, fleet_id: int, squad_id: int) -> None:
    _call(session, "DELETE", f"/fleets/{fleet_id}/squads/{squad_id}/", expect=(204,))


def move_member(session, fleet_id: int, member_id: int, *, wing_id, squad_id,
                role: str) -> None:
    """PUT /fleets/{id}/members/{member_id}/ — move a pilot to a role/position.

    Body shape by role: fleet_commander → role only; wing_commander → +wing_id;
    squad_commander / squad_member → +wing_id +squad_id."""
    body: dict = {"role": role}
    if wing_id is not None:
        body["wing_id"] = wing_id
    if squad_id is not None:
        body["squad_id"] = squad_id
    _call(session, "PUT", f"/fleets/{fleet_id}/members/{member_id}/",
          json=body, expect=(204,))


def clamp_name(name) -> str:
    """ESI wing/squad names are capped at 10 chars; this is the canonical join
    key between full template names and the (already-clamped) live names."""
    return (name or "")[:_NAME_MAX]


def ensure_structure(session, fleet_id, wanted, live_wings):
    """Materialize every wing/squad in `wanted` in the live fleet, reconciling
    EVE's auto-created default squad (reuse/rename instead of leaving a stray).

    wanted: list of (wing_name, [squad_name, ...]) in template order — FULL names.
    live_wings: current get_wings() result (live names are already <=10 chars).
    Returns (wing_id_by_key, squad_id_by_key): wing key = clamp_name(wing);
    squad key = (clamp_name(wing), clamp_name(squad)).

    Matching is by clamped name because ESI stores names clamped to 10 chars.
    Re-reads get_wings once after any wing creation to capture auto-created squads.
    """
    def _index(wings):
        wmap = {clamp_name(w["name"]): w["id"] for w in wings}
        squads = {w["id"]: [dict(s) for s in w.get("squads", [])] for w in wings}
        return wmap, squads

    wmap, squads_by_wing = _index(live_wings)

    created = False
    for wing_name, _squads in wanted:
        if clamp_name(wing_name) not in wmap:
            wmap[clamp_name(wing_name)] = create_wing(session, fleet_id, wing_name)
            created = True
    if created:
        # Re-read to capture EVE's auto-created squads + the new wing ids.
        wmap, squads_by_wing = _index(get_wings(session, fleet_id))

    squad_ids: dict = {}
    for wing_name, squad_names in wanted:
        wkey = clamp_name(wing_name)
        wing_id = wmap[wkey]
        existing = squads_by_wing.get(wing_id, [])
        by_key = {clamp_name(s["name"]): s for s in existing}
        wanted_keys = {clamp_name(sn) for sn in squad_names}
        claimed: set = set()
        for sn in squad_names:
            skey = clamp_name(sn)
            hit = by_key.get(skey)
            if hit is not None and hit["id"] not in claimed:
                squad_ids[(wkey, skey)] = hit["id"]
                claimed.add(hit["id"])
                continue
            # Reuse a stray squad (exists live but isn't a wanted name) by renaming —
            # this consumes EVE's auto-created "Squad 1" instead of duplicating.
            stray = next((s for s in existing
                          if s["id"] not in claimed
                          and clamp_name(s["name"]) not in wanted_keys), None)
            if stray is not None:
                rename_squad(session, fleet_id, stray["id"], sn)
                stray["name"] = clamp_name(sn)   # reflect so it isn't reused twice
                squad_ids[(wkey, skey)] = stray["id"]
                claimed.add(stray["id"])
            else:
                new_sid = create_squad(session, fleet_id, wing_id, sn)
                squad_ids[(wkey, skey)] = new_sid
                claimed.add(new_sid)
    return wmap, squad_ids


# ETag-cacheable hot GETs (spec §4: members + wings only, 5s cache).
_ETAG_CACHEABLE = ("/members/", "/wings/")

_CAPTURED_HEADERS = (
    "X-Ratelimit-Remaining", "X-ESI-Error-Limit-Remain",
    "X-ESI-Error-Limit-Reset", "Retry-After",
)


class _CachedBody:
    """A 200-shaped response replayed from the ETag cache on a 304."""
    def __init__(self, body, headers):
        self.status_code = 200
        self.ok = True
        self.headers = dict(headers)
        self._body = body
        self.text = ""

    def json(self):
        return self._body


class AuthEsiSession:
    """Adapts an ESIAuth into the `session` protocol `_call` expects.

    Each request applies the ESI rate limiter, attaches the bearer token, and
    calls through the auth's live requests.Session. Raises FleetESIError
    ("no_token") if the auth has no valid token (e.g. not the fleet boss yet).

    Phase B additions:
      * `last_headers` — the rate-limit / error-limit / Retry-After headers off
        the most recent response, for the executor's ledger + freeze logic.
      * ETag cache for the two hot GETs (members, wings): the first 200 stores
        (etag, body); subsequent GETs send `If-None-Match`; a 304 is rewritten
        into a 200-shaped response carrying the cached body (costs 1 token).
    """

    def __init__(self, auth):
        self._auth = auth
        self.last_headers: dict = {}
        self._etags: dict[str, tuple] = {}   # path -> (etag, body)

    @staticmethod
    def _etag_cacheable(method, path):
        return method.upper() == "GET" and path.endswith(_ETAG_CACHEABLE)

    def request(self, method, path, json=None):
        from rate_limiter import rate_limit
        from esi_constants import ESI_BASE
        token = self._auth.access_token
        if not token:
            raise FleetESIError("no_token")
        headers = {"Authorization": f"Bearer {token}"}
        cacheable = self._etag_cacheable(method, path)
        cached = self._etags.get(path) if cacheable else None
        if cached is not None:
            headers["If-None-Match"] = cached[0]
        rate_limit("esi")
        resp = self._auth._session.request(
            method, f"{ESI_BASE}{path}", headers=headers, json=json, timeout=10)
        rh = getattr(resp, "headers", {}) or {}
        self.last_headers = {k: rh[k] for k in _CAPTURED_HEADERS if k in rh}
        if cacheable:
            if resp.status_code == 304 and cached is not None:
                return _CachedBody(cached[1], self.last_headers)
            if resp.status_code == 200:
                etag = rh.get("ETag")
                if etag:
                    self._etags[path] = (etag, resp.json())
        return resp
