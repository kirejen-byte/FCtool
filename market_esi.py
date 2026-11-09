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
  ESI ETags are PER PAGE (measured: page 1 can stay 304 while page 2 changes),
  so every page is cached and revalidated with its OWN ETag - a page-1 304
  never vouches for the rest of the book.
* Order-pull honesty: the order pulls (``structure_orders`` / ``region_orders``)
  retry a failed page ONCE (transport error / 5xx / bad body - never a 4xx,
  which would spend error budget) and, if the walk still ends short, record
  ``"orders_incomplete"`` in ``last_incomplete`` so the scanner marks the
  snapshot degraded instead of passing a partial book off as complete.
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


def _emit_progress(progress, payload: dict) -> None:
    """Invoke an optional progress callback, swallowing ANY exception it raises.

    Kept local (not imported from ``market_scanner``) so the adapter stays
    decoupled from the pure scanner. A raising or slow callback must never break a
    paginated pull. No-op when ``progress`` is None."""
    if progress is None:
        return
    try:
        progress(payload)
    except Exception:
        log.debug("[market-esi] progress callback raised (ignored)", exc_info=True)


class AuthMarketAdapter:
    """Live ESI adapter. Construct with an ``ESIAuth`` (for its ``requests``
    session + bearer token) and optionally a ``requests``-style public session
    for unauthenticated GETs (defaults to the auth's session).

    All list pulls auto-paginate on ``X-Pages``. Region-orders and
    contracts-list responses are ETag-cached in memory, per cache key AND per
    page (each page keeps its own ETag + body). Contract-item ETags are handled
    by the scanner's persistent cache; this adapter simply fetches when asked.
    """

    # Advertises that the list methods accept an optional ``progress`` callback
    # and emit ``{"stage": ..., "page": i, "pages": n}`` per page. The scanner
    # feature-detects this before passing a callback, so fake/older adapters
    # (which lack the kwarg) are still called the classic way.
    supports_progress = True

    def __init__(self, auth, *, public_session=None, timeout: int = 15) -> None:
        self._auth = auth
        self._timeout = timeout
        # A dedicated public session avoids leaking the Authorization header to
        # unauthenticated endpoints; fall back to the auth's session if none.
        self._public = public_session or getattr(auth, "_session", None)
        self.last_headers: dict = {}
        # cache key -> {"pages": {page_no: (etag, page_body)}, "total": n}.
        # Only pages that landed with an ETag are stored; a failed page never is.
        self._etags: dict[str, dict] = {}
        # Scope/access degradation record. A 403 on an AUTHED route adds a tag
        # here (e.g. "structure_market" / "corp_contracts") IN ADDITION to the
        # degrade-by-design empty return, so the scanner can mark the resulting
        # snapshot degraded (forbidden != empty) instead of it looking like a
        # genuinely bare market. The scanner clears this at the start of every
        # ``scan_market`` pass (duck-typed) so it only reflects the current scan.
        self.last_forbidden: set[str] = set()
        # Incomplete-pull record. An ORDER pull (``structure_orders`` /
        # ``region_orders``) whose page walk ended short - a page still failing
        # after its one retry, the error-limit floor, a 404/410/420 mid-walk -
        # adds ``"orders_incomplete"`` here, so the scanner can mark the snapshot
        # degraded (a partial book != the whole market) instead of items on the
        # lost pages reading as absent. Cleared by the scanner at the start of
        # every ``scan_market`` pass (duck-typed), like ``last_forbidden``.
        self.last_incomplete: set[str] = set()
        # Contract-items disposition (for the scanner's negative cache) is NOT
        # stashed on the adapter: ``contract_items`` / ``corp_contract_items``
        # RETURN it as ``(items, status)``. Returning it per call — rather than
        # recording it on a shared attribute — is what lets the scanner's PARALLEL
        # Pass-2 attribute each contract's disposition to its OWN fetch. (The shared
        # attribute was read on the wrong thread under 6-way concurrency and could
        # tombstone a live contract forever.) ``status`` is one of:
        #   "ok"        — a response (even an empty 2xx body),
        #   "dead"      — 404/410 (gone), or a 403 on the PUBLIC route (a
        #                 permanently restricted contract) → tombstone forever,
        #   "transient" — transport error / 5xx / error-limit, or a 403 on the
        #                 CORP route (a scope/access shortfall re-auth may fix) →
        #                 do NOT cache, retry next scan.

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

    # Per-page failure kinds that earn ONE immediate retry on a tagged (order)
    # pull. Everything else is never retried: a 420/429/other 4xx retry spends
    # ESI error budget, and a 403/404/410 will not change on a second ask.
    _RETRYABLE_KINDS = frozenset({"transport", "server", "bad_body"})

    def _page_attempt(self, url, *, authed: bool, params, headers=None,
                      conditional: bool = False):
        """One rate-limited page GET, classified. Returns ``(kind, resp, body)``:

        "ok" (``body`` is the page's list), "not_modified" (a 304 - only when
        ``conditional``, i.e. an If-None-Match was possible), "transport" (no
        response), "forbidden" (403), "dead" (404/410), "server" (5xx),
        "client" (420 / 429 / any other non-OK), "bad_body" (unparseable JSON or
        a non-list body). The check order mirrors the historical inline code, so
        an untagged caller classifies every response exactly as before."""
        resp = self._get_page(url, authed=authed, params=params, headers=headers)
        if resp is None:
            return "transport", None, None
        code = resp.status_code
        if code == 403:
            return "forbidden", resp, None
        if conditional and code == 304:
            return "not_modified", resp, None
        if code in (404, 410):
            return "dead", resp, None
        if not getattr(resp, "ok", False):
            kind = "server" if isinstance(code, int) and code >= 500 else "client"
            return kind, resp, None
        try:
            body = resp.json()
        except ValueError:
            return "bad_body", resp, None
        if not isinstance(body, list):
            return "bad_body", resp, None
        return "ok", resp, body

    def _fetch_page(self, url, *, authed: bool, params, headers=None,
                    conditional: bool = False, retry: bool = False):
        """``_page_attempt`` plus, when ``retry``, ONE immediate retry (through
        the same rate-limited GET) for a ``_RETRYABLE_KINDS`` failure."""
        kind, resp, body = self._page_attempt(
            url, authed=authed, params=params, headers=headers,
            conditional=conditional)
        if retry and kind in self._RETRYABLE_KINDS:
            log.info("[market-esi] page %s of %s failed (%s), retrying once",
                     (params or {}).get("page"), url, kind)
            kind, resp, body = self._page_attempt(
                url, authed=authed, params=params, headers=headers,
                conditional=conditional)
        return kind, resp, body

    def _mark_incomplete(self, tag: str, path: str, page: int, why: str) -> None:
        """Record an order pull that ended short (see ``last_incomplete``)."""
        log.warning("[market-esi] %s ended short at page %d (%s); marked %s",
                    path, page, why, tag)
        self.last_incomplete.add(tag)

    @staticmethod
    def _x_pages(resp, fallback: int) -> int:
        """The response's ``X-Pages`` total, or ``fallback`` when absent/garbage."""
        try:
            return int((getattr(resp, "headers", {}) or {}).get("x-pages", fallback))
        except (TypeError, ValueError):
            return fallback

    def _paginate(
        self, path, *, authed: bool, params=None, forbidden_tag: str | None = None,
        progress=None, stage: str = "page", incomplete_tag: str | None = None,
    ) -> list[dict]:
        """Auto-paginate a list endpoint on ``X-Pages``, returning the concatenated
        items. Thin wrapper over ``_paginate_status`` for the callers that don't
        need the per-pull disposition — the single-threaded ORDER path
        (``structure_orders`` / ``corp_contracts``). The concurrently-fetched
        contract-items path calls ``_paginate_status`` directly and RETURNS the
        status to its caller, so a parallel fetch never reads another thread's
        disposition off a shared attribute."""
        items, _status = self._paginate_status(
            path, authed=authed, params=params, forbidden_tag=forbidden_tag,
            progress=progress, stage=stage, incomplete_tag=incomplete_tag,
        )
        return items

    def _paginate_status(
        self, path, *, authed: bool, params=None, forbidden_tag: str | None = None,
        progress=None, stage: str = "page", incomplete_tag: str | None = None,
    ) -> "tuple[list[dict], str]":
        """Pagination core. Returns ``(items, status)`` where ``status`` classifies
        the pull for the contract-items negative cache: "ok" (a response, even an
        empty 2xx body), "transient" (transport error / 5xx / 420 error-limit),
        "forbidden" (403), or "dead" (404/410 gone). ``status`` is a LOCAL return
        value — NEVER stored on the instance — so concurrent contract-item fetches
        each get their OWN disposition with no cross-thread attribution race.

        A 403 (missing scope / no access) → ``([], "forbidden")``. Stops on the
        first non-OK page or transport error. Honours the error-limit floor between
        pages (aborts early, returning what it has).

        ``forbidden_tag`` (when given) is recorded in ``last_forbidden`` on a 403
        so the caller can distinguish "forbidden" (scope/access) from "genuinely
        empty" — the 403 is still degraded to ``[]`` (no raise).

        ``incomplete_tag`` (ORDER pulls only) turns on order-pull honesty: a page
        failing with a transport error / 5xx / bad body is retried ONCE, and a
        walk that still stops before the last page for any reason but a 403
        (failed page, error-limit floor, 404/410/420 mid-walk) records the tag in
        ``last_incomplete`` - it returns the pages it got. Without a tag (the
        parallel contract-items path) there is no retry and no shared-state write:
        behaviour is exactly the historical one.

        ``progress`` (optional) is fed ``{"stage": stage, "page": i, "pages": n}``
        after each successful page so the GUI can show a real per-page bar during
        a many-page pull (e.g. a large structure order book)."""
        url = f"{ESI_BASE}{path}"
        out: list[dict] = []
        page = 1
        # Outcome classification for the caller (contract-items negative cache):
        # optimistic "ok", downgraded on the terminal error branch reached below.
        # A LOCAL variable (not a shared attribute) — see the class docstring.
        status = "ok"
        short = None  # why the walk stopped before the last page (None = it didn't)
        while True:
            page_params = dict(params or {})
            page_params["page"] = page
            kind, resp, items = self._fetch_page(
                url, authed=authed, params=page_params, retry=bool(incomplete_tag))
            if kind == "forbidden":
                log.info("[market-esi] 403 (scope/access) for %s", path)
                if forbidden_tag:
                    self.last_forbidden.add(forbidden_tag)
                return out, "forbidden"
            if kind != "ok":
                if kind == "dead":
                    # Gone: a deleted/expired contract (or missing resource).
                    # Permanent - the contract-items caller tombstones it.
                    status = "dead"
                elif kind != "bad_body":
                    # Transport error / 5xx / 420 error-limit / other non-OK -
                    # transient, retry next scan. (A bad body keeps "ok".)
                    status = "transient"
                short = kind
                break
            out.extend(items)
            total = self._x_pages(resp, 1)
            _emit_progress(progress, {"stage": stage, "page": page, "pages": total})
            if page >= total:
                break
            if self.error_limited:
                log.warning("[market-esi] error-limit floor hit paginating %s", path)
                short = "error_limit"
                page += 1
                break
            page += 1
        if short and incomplete_tag:
            self._mark_incomplete(incomplete_tag, path, page, short)
        return out, status

    def _paginate_etag(
        self, path, *, authed: bool, params=None, progress=None, stage: str = "page",
        cache_key: str | None = None, incomplete_tag: str | None = None,
    ) -> list[dict]:
        """Like ``_paginate`` but conditional PER PAGE: every page is requested
        with ``If-None-Match`` set to THAT page's cached ETag (when one is
        cached); a 304 replays that page's cached body, a 200 replaces it. ESI
        ETags are per page - measured: across a cache refresh page 1 stayed 304
        while page 2 changed - so a page-1 304 never vouches for later pages.
        Used for the region-orders and contracts-list pulls where a 304 is common
        and cheap.

        The page total comes from page 1's ``X-Pages`` (a 304 carries it too),
        falling back to the cached total, then 1. After the pull, cached pages
        above the new total are dropped. Only a page that LANDED with an ETag is
        cached - a failed page never is, so a partial pull is refetched next
        time instead of being replayed.

        A 403 on page 1 -> ``[]``. A failed or unfetched page falls back to its
        previously cached body when one exists (stale beats missing).
        ``incomplete_tag`` (ORDER pulls only) adds the one-retry rule and the
        ``last_incomplete`` record exactly as in ``_paginate_status``; without it
        there is no retry and nothing is recorded.

        ``cache_key`` (optional) is the key under which the per-page ETags +
        bodies are stored; it defaults to ``path``. A per-``type_id``
        region-orders query passes a key that folds in the type so the many
        targeted queries against ONE region each get their OWN entry (one per
        ``(region, type)``) instead of clobbering a single region-wide entry.

        ``progress`` (optional) is fed ``{"stage": stage, "page": i, "pages": n}``
        once per page obtained (fetched or replayed from a 304), so a fully
        cached one-page pull still ticks page 1 of 1."""
        url = f"{ESI_BASE}{path}"
        key = cache_key or path
        entry = self._etags.get(key) or {}
        cached_pages: dict = dict(entry.get("pages") or {})
        cached_total = entry.get("total")
        pages = dict(cached_pages)  # the per-page cache this pull leaves behind
        out: list[dict] = []
        total = None  # known once page 1 lands
        page = 1
        short = None  # why the walk stopped before the last page (None = it didn't)
        while True:
            if page > 1 and self.error_limited:
                log.warning("[market-esi] error-limit floor hit paginating %s", path)
                short = "error_limit"
                break
            prev = cached_pages.get(page)
            headers = {"If-None-Match": prev[0]} if prev else None
            kind, resp, body = self._fetch_page(
                url, authed=authed, params={**(params or {}), "page": page},
                headers=headers, conditional=True, retry=bool(incomplete_tag),
            )
            if kind == "not_modified" and prev is None:
                kind = "client"  # a 304 for a page we never cached: nothing to replay
            if kind == "forbidden" and page == 1:
                return []
            if kind not in ("ok", "not_modified"):
                short = kind
                break
            if page == 1:
                total = self._x_pages(resp, cached_total or 1)
            if kind == "not_modified":
                body = prev[1]
            else:
                etag = (getattr(resp, "headers", {}) or {}).get("ETag")
                if etag:
                    pages[page] = (etag, body)
                else:
                    pages.pop(page, None)  # no validator: never replay an old body
            out.extend(body)
            _emit_progress(progress, {"stage": stage, "page": page, "pages": total})
            if page >= total:
                break
            page += 1
        if total is None:  # page 1 never landed: the cached shape is all we know
            total = cached_total or 0
        if short:
            # Stale beats missing: pad every page this walk did not obtain with its
            # last good cached body. The tag (below) marks the result either way.
            for p in range(page, total + 1):
                if p in cached_pages:
                    out.extend(cached_pages[p][1])
            if incomplete_tag and short != "forbidden":
                self._mark_incomplete(incomplete_tag, path, page, short)
        pages = {p: v for p, v in pages.items() if p <= total}
        if pages:
            self._etags[key] = {"pages": pages, "total": total}
        else:
            self._etags.pop(key, None)
        return out

    # ── MarketEsiAdapter protocol ─────────────────────────────────────────────

    def region_orders(
        self, region_id: int, *, type_id: int | None = None, order_type: str = "sell",
        progress=None,
    ) -> list[dict]:
        """Public region orders (NPC-station + ranged), ETag-cached. Optional
        single ``type_id`` filter; ``order_type`` default "sell". ``progress``
        (optional) receives per-page ``{"stage": "orders", ...}`` dicts.

        The ETag cache key folds in ``order_type`` and (when set) ``type_id`` so a
        targeted scan's many per-type queries against one region each keep their
        OWN per-page conditional-request ETags (one entry per ``(region, type)``)
        rather than clobbering a single region-wide entry. A walk that ends
        short records ``"orders_incomplete"`` in ``last_incomplete``."""
        params: dict = {"order_type": order_type}
        cache_key = f"/markets/{region_id}/orders/|{order_type}"
        if type_id is not None:
            params["type_id"] = type_id
            cache_key = f"{cache_key}|type={type_id}"
        return self._paginate_etag(
            f"/markets/{region_id}/orders/", authed=False, params=params,
            progress=progress, stage="orders", cache_key=cache_key,
            incomplete_tag="orders_incomplete",
        )

    def structure_orders(self, structure_id: int, *, progress=None) -> list[dict]:
        """Authed structure market — the whole order book, no filter (scope
        ``esi-markets.structure_markets.v1`` + docking access). 403 → [] and a
        ``"structure_market"`` entry in ``last_forbidden`` (forbidden != empty).
        ``progress`` (optional) receives per-page ``{"stage": "orders", ...}``
        dicts - the structure book is the many-page first-scan cost. A walk that
        ends short records ``"orders_incomplete"`` in ``last_incomplete``."""
        return self._paginate(
            f"/markets/structures/{structure_id}/", authed=True,
            forbidden_tag="structure_market", progress=progress, stage="orders",
            incomplete_tag="orders_incomplete",
        )

    def public_contracts(self, region_id: int, *, progress=None) -> list[dict]:
        """Public region contracts (no auth), ETag-cached. ``progress`` (optional)
        receives per-page ``{"stage": "contracts_list", ...}`` dicts."""
        return self._paginate_etag(
            f"/contracts/public/{region_id}/", authed=False,
            progress=progress, stage="contracts_list",
        )

    def contract_items(self, contract_id: int) -> "tuple[list[dict], str]":
        """Public contract items (no auth; 403 only for restricted contracts).

        Returns ``(items, status)`` — the per-call disposition for the scanner's
        negative cache. Returning it (instead of stashing it on the adapter) lets
        the scanner's PARALLEL contract pass attribute each contract's disposition
        to its OWN fetch. On the PUBLIC route a 403 means the contract is RESTRICTED
        (permanently unreadable without membership), so it maps to "dead"
        (tombstone) alongside 404/410 — re-fetching a restricted/gone contract every
        scan is pointless."""
        items, st = self._paginate_status(
            f"/contracts/public/items/{contract_id}/", authed=False)
        status = "dead" if st in ("dead", "forbidden") else st
        return items, status

    def corp_contracts(self, corporation_id: int, *, progress=None) -> list[dict]:
        """Corp/alliance contracts (scope
        ``esi-contracts.read_corporation_contracts.v1`` + a rolled character).
        403 → [] and a ``"corp_contracts"`` entry in ``last_forbidden``.
        ``progress`` (optional) receives per-page ``{"stage": "contracts_list",
        ...}`` dicts — corp books can be large (couriers etc.)."""
        return self._paginate(
            f"/corporations/{corporation_id}/contracts/", authed=True,
            forbidden_tag="corp_contracts", progress=progress, stage="contracts_list",
        )

    def corp_contract_items(self, corporation_id: int, contract_id: int) -> "tuple[list[dict], str]":
        """Items for a corp/alliance contract (authed). 403 → ``([], "transient")``
        and a ``"corp_contracts"`` entry in ``last_forbidden``.

        Returns ``(items, status)`` — the per-call disposition for the scanner's
        negative cache (returned, not stashed, so the parallel contract pass
        attributes it per call). Only 404/410 (the contract is gone) maps to "dead"
        here — a 403 on the CORP route is a SCOPE/access shortfall (re-auth may
        restore it), so it maps to "transient" (retry next scan), NOT a permanent
        tombstone."""
        items, st = self._paginate_status(
            f"/corporations/{corporation_id}/contracts/{contract_id}/items/",
            authed=True, forbidden_tag="corp_contracts",
        )
        status = "transient" if st == "forbidden" else st
        return items, status

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
