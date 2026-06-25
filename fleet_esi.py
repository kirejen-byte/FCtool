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


class AuthEsiSession:
    """Adapts an ESIAuth into the `session` protocol `_call` expects.

    Each request applies the ESI rate limiter, attaches the bearer token, and
    calls through the auth's live requests.Session. Raises FleetESIError
    ("no_token") if the auth has no valid token (e.g. not the fleet boss yet)."""

    def __init__(self, auth):
        self._auth = auth

    def request(self, method, path, json=None):
        from rate_limiter import rate_limit
        from esi_constants import ESI_BASE
        token = self._auth.access_token
        if not token:
            raise FleetESIError("no_token")
        rate_limit("esi")
        return self._auth._session.request(
            method, f"{ESI_BASE}{path}",
            headers={"Authorization": f"Bearer {token}"},
            json=json, timeout=10)
