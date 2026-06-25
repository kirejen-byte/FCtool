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
