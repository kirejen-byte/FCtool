"""Concrete ESI adapter for the Market Scanner (the network side of the seam).

``AuthMarketAdapter`` implements ``market_scanner.MarketEsiAdapter`` over a live
``ESIAuth``. It is the ONLY place in the market subsystem that touches HTTP; the
scanner stays pure and is driven by a ``FakeMarketAdapter`` in tests. Live wiring
of this adapter into the GUI is Phase B — Phase A ships it written + unit-shaped
but never exercised against the network by the test suite.

Reuses the verified repo infrastructure (design §4.1, §8):

* Base URL + User-Agent — ``esi_constants``.
* Global token bucket ``rate_limit("esi")`` (15/s, burst 30) on EVERY call —
  ``rate_limiter``, the same limiter the fleet/overlay/zkill pollers share, so
  the scanner competes fairly and can't exceed the global rate.
* ``X-Pages`` auto-pagination (like ``ESIAuth.get_assets``).
* ``If-None-Match`` / 304 ETag replay for the region-orders and contracts-list
  pulls (a 304 costs a token but no error budget and returns the cached body).
* ``X-ESI-Error-Limit-Remain`` / ``Retry-After`` capture (the ``_CAPTURED_HEADERS``
  set mirrored from ``fleet_esi``) so the worker can back off before tripping the
  error budget. ``error_limited`` exposes whether the last response left the
  remaining error budget below ``ERROR_LIMIT_FLOOR``; the scanner's contract-item
  loop can poll it between calls and abort the pass (§8.2).

Auth model:

* Public endpoints (region orders, public contracts, public contract items,
  station resolve) send NO Authorization header — they work with no token and no
  new scope, enabling the design's degraded mode.
* Structure orders + structure resolve + corp contracts attach the bearer token
  and require the corresponding scope; a 403 is surfaced as an empty result
  (missing scope / no docking access → the caller degrades, the UI shows the
  existing ⚠ re-auth indicator).
"""

from __future__ import annotations

from app_log import get_logger
from esi_constants import ESI_BASE, ESI_HEADERS

log = get_logger(__name__)

# Below this remaining error-budget the scanner should stop the pass (§8.2).
ERROR_LIMIT_FLOOR = 20

# NPC station id range (design §7) — resolve via the public /universe/stations/
# endpoint. Upwell structures (id >= 1e12) resolve via the authed
# /universe/structures/ endpoint (scope esi-universe.read_structures.v1, already
# granted).
_STATION_ID_MIN = 60_000_000
_STATION_ID_MAX = 64_000_000
_STRUCTURE_ID_MIN = 1_000_000_000_000

_CAPTURED_HEADERS = (
    "X-Ratelimit-Remaining",
    "X-ESI-Error-Limit-Remain",
    "X-ESI-Error-Limit-Reset",
    "Retry-After",
)


class AuthMarketAdapter:
    """Live ESI adapter. Construct with an ``ESIAuth`` (for its ``requests``
    session + bearer token) and optionally a ``requests``-style public session
    for unauthenticated GETs (defaults to the auth's session).

    All list pulls auto-paginate on ``X-Pages``. Region-orders and
    contracts-list responses are ETag-cached in memory (per-path). Contract-item
    ETags are handled by the scanner's persistent cache; this adapter simply
    fetches when asked.
    """

    def __init__(self, auth, *, public_session=None, timeout: int = 15) -> None:
        self._auth = auth
        self._timeout = timeout
        # A dedicated public session avoids leaking the Authorization header to
        # unauthenticated endpoints; fall back to the auth's session if none.
        self._public = public_session or getattr(auth, "_session", None)
        self.last_headers: dict = {}
        self._etags: dict[str, tuple] = {}  # path -> (etag, list-of-pages-body)

    # ── Error-limit awareness (§8.2) ──────────────────────────────────────────

    @property
    def error_limited(self) -> bool:
        """True if the most recent response left the remaining ESI error budget
        below ``ERROR_LIMIT_FLOOR`` — the contract-item loop polls this and
        aborts the pass rather than hammering into a 420."""
        remain = self.last_headers.get("X-ESI-Error-Limit-Remain")
        if remain is None:
            return False
        try:
            return int(remain) < ERROR_LIMIT_FLOOR
        except (TypeError, ValueError):
            return False

    def _capture(self, resp) -> None:
        rh = getattr(resp, "headers", {}) or {}
        self.last_headers = {k: rh[k] for k in _CAPTURED_HEADERS if k in rh}

    # ── Low-level GET helpers ─────────────────────────────────────────────────

    def _headers(self, *, authed: bool) -> dict:
        h = dict(ESI_HEADERS)
        if authed:
            token = getattr(self._auth, "access_token", None)
            if token:
                h["Authorization"] = f"Bearer {token}"
        return h

    def _get_page(self, url, *, authed: bool, params=None, headers=None):
        """One rate-limited GET. Returns the ``requests`` response (or None on a
        transport error). Captures the rate/error-limit headers."""
        from rate_limiter import rate_limit

        session = self._auth._session if authed else self._public
        if session is None:
            return None
        req_headers = self._headers(authed=authed)
        if headers:
            req_headers.update(headers)
        rate_limit("esi")
        try:
            resp = session.get(
                url, headers=req_headers, params=params or {}, timeout=self._timeout
            )
        except Exception as e:
            log.warning("[market-esi] GET %s failed: %s", url, e)
            return None
        self._capture(resp)
        return resp

    def _paginate(self, path, *, authed: bool, params=None) -> list[dict]:
        """Auto-paginate a list endpoint on ``X-Pages``. Returns the concatenated
        items. A 403 (missing scope / no access) → ``[]``. Stops on the first
        non-OK page or transport error. Honours the error-limit floor between
        pages (aborts early, returning what it has)."""
        url = f"{ESI_BASE}{path}"
        out: list[dict] = []
        page = 1
        while True:
            page_params = dict(params or {})
            page_params["page"] = page
            resp = self._get_page(url, authed=authed, params=page_params)
            if resp is None:
                break
            if resp.status_code == 403:
                log.info("[market-esi] 403 (scope/access) for %s", path)
                return []
            if not getattr(resp, "ok", False):
                break
            try:
                items = resp.json()
            except ValueError:
                break
            if isinstance(items, list):
                out.extend(items)
            else:
                break
            try:
                total = int(resp.headers.get("x-pages", 1))
            except (TypeError, ValueError):
                total = 1
            if page >= total:
                break
            if self.error_limited:
                log.warning("[market-esi] error-limit floor hit paginating %s", path)
                break
            page += 1
        return out

    def _paginate_etag(self, path, *, authed: bool, params=None) -> list[dict]:
        """Like ``_paginate`` but sends ``If-None-Match`` from the in-memory ETag
        cache and replays the cached body on a 304 (first page's ETag gates the
        whole result). Used for the region-orders and contracts-list pulls where
        a 304 is common and cheap."""
        url = f"{ESI_BASE}{path}"
        cached = self._etags.get(path)
        headers = {"If-None-Match": cached[0]} if cached else None
        # Probe page 1 with the conditional header.
        first = self._get_page(
            url, authed=authed, params={**(params or {}), "page": 1}, headers=headers
        )
        if first is None:
            return cached[1] if cached else []
        if first.status_code == 304 and cached is not None:
            return cached[1]
        if first.status_code == 403:
            return []
        if not getattr(first, "ok", False):
            return cached[1] if cached else []
        try:
            body = first.json()
        except ValueError:
            return cached[1] if cached else []
        out: list[dict] = list(body) if isinstance(body, list) else []
        etag = (getattr(first, "headers", {}) or {}).get("ETag")
        try:
            total = int(first.headers.get("x-pages", 1))
        except (TypeError, ValueError):
            total = 1
        # Remaining pages fetched unconditionally (ETag is for the first page).
        for page in range(2, total + 1):
            if self.error_limited:
                break
            resp = self._get_page(url, authed=authed, params={**(params or {}), "page": page})
            if resp is None or not getattr(resp, "ok", False):
                break
            try:
                more = resp.json()
            except ValueError:
                break
            if isinstance(more, list):
                out.extend(more)
        if etag:
            self._etags[path] = (etag, out)
        return out

    # ── MarketEsiAdapter protocol ─────────────────────────────────────────────

    def region_orders(
        self, region_id: int, *, type_id: int | None = None, order_type: str = "sell"
    ) -> list[dict]:
        """Public region orders (NPC-station + ranged), ETag-cached. Optional
        single ``type_id`` filter; ``order_type`` default "sell"."""
        params: dict = {"order_type": order_type}
        if type_id is not None:
            params["type_id"] = type_id
        return self._paginate_etag(
            f"/markets/{region_id}/orders/", authed=False, params=params
        )

    def structure_orders(self, structure_id: int) -> list[dict]:
        """Authed structure market — the whole order book, no filter (scope
        ``esi-markets.structure_markets.v1`` + docking access). 403 → []."""
        return self._paginate(
            f"/markets/structures/{structure_id}/", authed=True
        )

    def public_contracts(self, region_id: int) -> list[dict]:
        """Public region contracts (no auth), ETag-cached."""
        return self._paginate_etag(
            f"/contracts/public/{region_id}/", authed=False
        )

    def contract_items(self, contract_id: int) -> list[dict]:
        """Public contract items (no auth; 403 only for restricted contracts)."""
        return self._paginate(
            f"/contracts/public/items/{contract_id}/", authed=False
        )

    def corp_contracts(self, corporation_id: int) -> list[dict]:
        """Corp/alliance contracts (scope
        ``esi-contracts.read_corporation_contracts.v1`` + a rolled character).
        403 → []."""
        return self._paginate(
            f"/corporations/{corporation_id}/contracts/", authed=True
        )

    def corp_contract_items(self, corporation_id: int, contract_id: int) -> list[dict]:
        """Items for a corp/alliance contract (authed)."""
        return self._paginate(
            f"/corporations/{corporation_id}/contracts/{contract_id}/items/",
            authed=True,
        )

    def resolve_location_system(self, location_id: int) -> int | None:
        """Resolve a station/structure id to its solar-system id (design §7).

        NPC station (id in the station range) → public ``/universe/stations/``
        (``system_id``). Upwell structure (id >= 1e12) → authed
        ``/universe/structures/`` (``solar_system_id``; scope already granted, 403
        on no docking access → None). Ids outside both ranges → None."""
        if _STATION_ID_MIN <= location_id < _STATION_ID_MAX:
            resp = self._get_page(
                f"{ESI_BASE}/universe/stations/{location_id}/", authed=False
            )
            return _extract_system(resp, "system_id")
        if location_id >= _STRUCTURE_ID_MIN:
            resp = self._get_page(
                f"{ESI_BASE}/universe/structures/{location_id}/", authed=True
            )
            return _extract_system(resp, "solar_system_id")
        return None


def _extract_system(resp, key: str) -> int | None:
    """Pull an int system id off a universe resolve response, or None."""
    if resp is None or not getattr(resp, "ok", False):
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    val = data.get(key)
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None
