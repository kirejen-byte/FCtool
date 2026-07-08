"""Market Scanner backend — staging-market depth/breadth + contract scan + gaps.

Pure logic, Tk-free. Network access happens ONLY through an injected adapter
(the ``MarketEsiAdapter`` protocol), exactly like ``type_catalog.TypeCatalog``
and ``fittings_store.FittingsStore``. A concrete ``AuthMarketAdapter`` lives in
``market_esi.py``; unit tests drive a ``FakeMarketAdapter`` so the whole
computation is proven offline.

What it computes (see docs/superpowers/specs/2026-07-06-market-scanner-design.md):

* **Market depth + breadth** for every component of every doctrine fit in the
  user's staging market — is each hull/module/charge/drone on the market
  (breadth), how many units at what prices (depth, full sell ladder), and how
  many complete fits can be assembled from local stock (the binding-constraint
  minimum across the fit's components).
* **Contract scan** — public (+ optional corp/alliance) item-exchange contracts
  in the staging region that are meaningfully similar (same hull, >=95% match)
  to a doctrine fit, with a price ladder and an "includes extras" flag.
* **Gap export** — a shopping list of short items formatted for Janice and EVE
  Multibuy (clipboard strings; no Janice API integration in v1).

Owner-decision interpretations baked in here (these RESOLVE the design doc's
open questions):

* **Sell-side only.** Depth captures sell orders; buy orders are excluded.
* **Full price ladder** kept per type (total qty + sorted price levels +
  best/median helpers).
* **Seed target is a FIXED number per fit** (``config["market"]["seed_target"]``,
  default 20), NOT ``DoctrineMember.ideal_*``. The gap builder takes it as
  ``target_fits``.
* **Modules + subsystems scores (decision #4, as amended by the owner).** BOTH
  the >=95% contract-similarity score AND the completable-fits
  binding-constraint computation use HULL + FITTED MODULES + T3 SUBSYSTEMS —
  charges/drones/cargo are EXCLUDED from those two numbers. (A T3 without its
  subsystems isn't the doctrine ship.) HOWEVER the per-component depth/breadth
  DATA (``ComponentAvailability`` rows on ``DoctrineAvailability``) still covers
  the FULL fit bill-of-materials (hull, modules, subsystems, charges, drones —
  each row carries its ``role`` so a later UI can show complete tables), and the
  gap/shopping-list exporters accept whatever component subset they are handed
  (default: the scored set — hull + modules + subsystems). See
  ``_SCORING_MODULE_ROLES`` and ``fit_bom``.

Thread-safety: a scan holds no mutable shared state except the JSON cache, which
is guarded by a ``threading.Lock`` around every read-modify-write (the same
discipline ``TypeCatalog._write_cache_entries`` uses: re-read before writing so
concurrent scanners don't clobber). All returned dataclasses are fresh per call,
safe to hand to the Tk thread via the app's ``after()`` marshalling.
"""

from __future__ import annotations

import concurrent.futures
import os
import statistics
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from app_io import atomic_write_json
from app_log import get_logger
from app_path import app_dir

log = get_logger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

# ESI cache timers (seconds) — do NOT refetch within these windows. Mirrors the
# per-endpoint cache the design cites (§8): 300s market orders, 1800s contracts
# list, 3600s contract items. The adapter/worker is expected to honour these;
# the scanner exposes them so a future poller can pace off them.
CACHE_TTL_ORDERS = 300
CACHE_TTL_CONTRACTS = 1800
CACHE_TTL_CONTRACT_ITEMS = 3600

# An "ok but genuinely EMPTY" contract-items result (a 2xx with a bare body — a
# want-to-buy request, a courier, or a since-emptied stock contract) is cached on
# a much longer timer than a populated one. WHY: on a busy staging book the large
# majority of surviving contracts fetch back empty, and with the 1h items TTL they
# ALL re-fetched live every hour — a recurring serial-latency storm of pointless
# GETs against contracts that were empty last hour and are still empty. They gain
# nothing from hourly freshness, so an empty result rides a 12h TTL instead. A
# populated contract keeps the tight 1h TTL (its stock can sell out fast). Stored
# with an explicit ``"empty": True`` flag inside the SAME cache-entry shape; an
# older entry that lacks the flag simply falls back to the 1h timer (old
# behaviour), so existing cache files load unchanged.
CACHE_TTL_CONTRACT_ITEMS_EMPTY = 12 * 3600

# Pass 2 of the contract scan fetches each surviving contract's items over ESI.
# Each fetch is one blocking ~0.4-1.0s round-trip, so fetching 300+ survivors
# SERIALLY took minutes (the user-reported "stuck at 309/316" plateau — the
# newest + corp contracts, uncached, cluster at the tail). We fetch survivors
# CONCURRENTLY on this small pool to overlap that latency. The pool size is NOT
# the request-rate control: the shared global ``rate_limit("esi")`` token bucket
# (15/s, burst 30) inside every adapter GET remains the real cadence gate, and a
# serial loop used only ~1/s of it — 15x of headroom. Six workers overlap the
# per-request latency (turning 316x~1s ≈ 5min into roughly a sixth of that, then
# bounded by the 15/s limiter) without ever exceeding the budget the fleet /
# overlay / zkill pollers also share.
CONTRACT_FETCH_WORKERS = 6

# During a contract scan the item cache is held in memory (one disk read at pass
# start) and flushed back to disk every N *fetched* contracts (plus a final flush
# at pass end). This does two things at once: it eliminates the O(n²) whole-file
# re-read/re-write the naïve read-modify-write-per-contract cache incurred (the
# real "3-minute scan" multiplier), AND it lets an interrupted first scan resume
# from what it already fetched instead of restarting. See ``_ContractCacheSession``.
_ITEM_FLUSH_EVERY = 25

# Pass-1 (cheap pre-filter) progress is emitted at most once every N *listed*
# contracts so the tight in-memory filter loop stays cheap on a huge region
# contract book (a busy trade hub region can list thousands of contracts).
_FILTER_PROGRESS_STRIDE = 25

# The contract-similarity threshold (design §5): a contract matches a doctrine
# fit iff same hull AND similarity >= this.
SIMILARITY_THRESHOLD = 0.95

# Roles that count toward the TWO scored numbers (similarity + completable_fits)
# under owner decision #4 (as amended): hull + fitted modules + T3 subsystems.
# The hull is handled separately as the similarity GATE (condition (a)); for
# completable_fits the hull IS one of the scored components (a fit needs its
# hull). Charges, drones and cargo are EXCLUDED from these two scores but still
# appear in the full-BoM component data.
#
# Subsystems count by explicit owner ruling: a T3 without its subsystems isn't
# the doctrine ship, so a contract missing a subsystem must fall below the 95%
# match and a market short on subsystems must show them as the seeding
# bottleneck.
_SCORING_MODULE_ROLES = frozenset({"module", "subsystem"})


def _now_iso() -> str:
    """An ISO-8601 UTC timestamp for ``taken_at`` stamps."""
    return datetime.now(timezone.utc).isoformat()


def _emit_progress(progress, payload: dict) -> None:
    """Invoke an optional progress callback, swallowing ANY exception it raises.

    Progress callbacks are advisory (the GUI marshals them onto the Tk thread via
    ``after()``); a raising — or merely slow — callback must never break or crash
    a scan. Cheap no-op when ``progress`` is None, so callers can pass it through
    unconditionally. Called synchronously from the scanning thread."""
    if progress is None:
        return
    try:
        progress(payload)
    except Exception:
        # A broken callback must not kill the scan, and it must not spam the log
        # (it could fire per page / per contract) — a single debug line is enough.
        log.debug("[market] progress callback raised (ignored)", exc_info=True)


def _volume_weighted_median(ladder: "list[PriceLevel]") -> float | None:
    """Volume-weighted median price of a sorted (ascending) sell ladder.

    "If I had to buy roughly half the available units, what per-unit price would
    I be paying at the midpoint?" Expands each price level by its volume and
    takes the median unit price. Returns ``None`` for an empty ladder. Robust to
    a huge total volume (does not materialize the expansion — walks cumulative
    volume to the midpoint instead)."""
    if not ladder:
        return None
    total = sum(max(0, lvl.volume) for lvl in ladder)
    if total <= 0:
        # Every level has zero/negative volume; fall back to the plain median of
        # the distinct prices so we still return something sensible.
        return statistics.median(lvl.price for lvl in ladder)
    midpoint = total / 2.0
    cumulative = 0
    for lvl in ladder:
        cumulative += max(0, lvl.volume)
        if cumulative >= midpoint:
            return lvl.price
    return ladder[-1].price


# ── Data model (§3) ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PriceLevel:
    """One sell order's price and remaining volume."""

    price: float  # ISK per unit
    volume: int  # units available at this order

    def to_dict(self) -> dict:
        return {"price": self.price, "volume": self.volume}

    @staticmethod
    def from_dict(d: dict) -> "PriceLevel":
        return PriceLevel(price=float(d.get("price", 0.0)), volume=int(d.get("volume", 0)))


@dataclass
class TypeDepth:
    """Sell-side depth for one type on one market."""

    type_id: int
    sell_qty: int  # total units on sell orders (this market only)
    sell_ladder: list[PriceLevel]  # ascending price; [] if none on market

    @property
    def on_market(self) -> bool:
        return self.sell_qty > 0

    @property
    def best_sell(self) -> float | None:
        """Lowest sell price, or None if not on market."""
        return self.sell_ladder[0].price if self.sell_ladder else None

    @property
    def median_sell(self) -> float | None:
        """Volume-weighted median sell price ("realistic seed cost/unit")."""
        return _volume_weighted_median(self.sell_ladder)

    def to_dict(self) -> dict:
        return {
            "type_id": self.type_id,
            "sell_qty": self.sell_qty,
            "sell_ladder": [lvl.to_dict() for lvl in self.sell_ladder],
        }

    @staticmethod
    def from_dict(d: dict) -> "TypeDepth":
        return TypeDepth(
            type_id=int(d["type_id"]),
            sell_qty=int(d.get("sell_qty", 0)),
            sell_ladder=[PriceLevel.from_dict(x) for x in d.get("sell_ladder", [])],
        )


@dataclass
class MarketSnapshot:
    """One market (structure or station) at one point in time."""

    source_id: int  # structure_id OR station_id
    source_kind: str  # "structure" | "station"
    region_id: int
    system_id: int | None  # resolved staging system (for display/filter)
    taken_at: str  # ISO-8601 UTC
    depth: dict[int, TypeDepth]  # type_id -> depth (only types with any order)
    etag: str | None = None  # last ETag for the orders pull (region path)
    # Degradation tags for THIS snapshot (forbidden != empty). A structure pull
    # that 403s for a missing scope / no docking access records
    # ``"structure_market"`` here so the UI can show a "re-authenticate" banner
    # instead of rendering an empty (forbidden) order book as a bare market.
    # JSON round-tripped through the cache so a stale cached load remembers it.
    degraded: list[str] = field(default_factory=list)
    # ── Scan scope (honest cached rendering) ──────────────────────────────────
    # "full"     → the whole market was pulled; ``scanned_type_ids`` is None
    #              (covers EVERY type). This is the historical behaviour.
    # "doctrine" → only a doctrine's bill-of-materials type_ids were scanned;
    #              ``scanned_type_ids`` holds EXACTLY those ids. Such a snapshot
    #              must NOT masquerade as coverage for a DIFFERENT doctrine/fit
    #              whose types it never scanned — consumers gate on ``covers()``
    #              so an unscanned type reads "not scanned" (unknown), never
    #              "not on the market". ``scope_label`` is the scanned doctrine's
    #              display name (for an honest "scanned for X only" UI message);
    #              "" for a full scan. All three round-trip through the cache so a
    #              disk-loaded doctrine-scoped snapshot keeps its honesty.
    scope: str = "full"
    scanned_type_ids: set[int] | None = None
    scope_label: str = ""

    def get(self, type_id: int) -> TypeDepth | None:
        return self.depth.get(type_id)

    def covers(self, type_ids) -> bool:
        """True when this snapshot's scope covers ALL of ``type_ids`` — i.e. every
        one of those types was actually scanned, so availability/marks/gaps built
        from this snapshot are HONEST for them.

        A full snapshot covers everything; a doctrine-scoped one covers only the
        ids it scanned. An out-of-scope type means "not scanned" (unknown), never
        "not on the market" — that distinction is the whole point of the scope
        tag, so a doctrine-scoped cache can't silently paint a DIFFERENT
        doctrine's items as absent."""
        if self.scope == "full" or self.scanned_type_ids is None:
            return True
        try:
            needed = {int(t) for t in type_ids}
        except (TypeError, ValueError):
            return False
        return needed <= self.scanned_type_ids

    def mark_degraded(self, reason: str) -> None:
        """Record a degradation reason (deduped, order-preserving). Used by the
        GUI worker's pre-check belt when a structure source's auth lacks the
        market scope even before the pull records a 403."""
        if reason and reason not in self.degraded:
            self.degraded.append(reason)

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "source_kind": self.source_kind,
            "region_id": self.region_id,
            "system_id": self.system_id,
            "taken_at": self.taken_at,
            "etag": self.etag,
            "degraded": list(self.degraded),
            "scope": self.scope,
            # A set isn't JSON-serializable — store a sorted list (None for full).
            "scanned_type_ids": (
                sorted(self.scanned_type_ids)
                if self.scanned_type_ids is not None else None),
            "scope_label": self.scope_label,
            # type_id keys become strings in JSON, ints in memory.
            "depth": {str(tid): d.to_dict() for tid, d in self.depth.items()},
        }

    @staticmethod
    def from_dict(d: dict) -> "MarketSnapshot":
        depth: dict[int, TypeDepth] = {}
        for raw_tid, raw in (d.get("depth") or {}).items():
            try:
                tid = int(raw_tid)
            except (TypeError, ValueError):
                continue
            depth[tid] = TypeDepth.from_dict(raw)
        raw_degraded = d.get("degraded")
        degraded = (
            [str(x) for x in raw_degraded] if isinstance(raw_degraded, list) else []
        )
        # Scope round-trip. A legacy cache dict with none of these keys loads back
        # as a FULL snapshot (scope "full", scanned None) — exactly the old
        # behaviour, so pre-scope caches keep rendering as full coverage.
        raw_scanned = d.get("scanned_type_ids")
        scanned: set[int] | None = None
        if isinstance(raw_scanned, list):
            scanned = set()
            for x in raw_scanned:
                try:
                    scanned.add(int(x))
                except (TypeError, ValueError):
                    continue
        return MarketSnapshot(
            source_id=int(d.get("source_id", 0)),
            source_kind=str(d.get("source_kind", "")),
            region_id=int(d.get("region_id", 0)),
            system_id=d.get("system_id"),
            taken_at=str(d.get("taken_at", "")),
            depth=depth,
            etag=d.get("etag"),
            degraded=degraded,
            scope=str(d.get("scope", "full") or "full"),
            scanned_type_ids=scanned,
            scope_label=str(d.get("scope_label", "") or ""),
        )


@dataclass
class Component:
    """One line of a fit's bill-of-materials."""

    type_id: int
    name: str
    per_fit_qty: int  # units this fit needs (stacked; e.g. 8x charge)
    role: str  # "hull" | "module" | "charge" | "drone" | "cargo" | "subsystem"


@dataclass
class ComponentAvailability:
    component: Component
    market_qty: int  # units on market (from MarketSnapshot)
    best_price: float | None
    completable: int  # market_qty // per_fit_qty (fits this component alone supports)
    on_market: bool


@dataclass
class DoctrineAvailability:
    fit_id: str
    hull_type_id: int
    hull_name: str
    components: list[ComponentAvailability]  # FULL BoM (hull+modules+charges+drones)
    completable_fits: int  # min(completable) over SCORED components = binding constraint
    binding_type_id: int | None  # the component that limits completable_fits
    binding_name: str | None
    breadth_pct: float  # % of SCORED components with on_market == True
    # Depth split for the birds-eye view:
    market_hulls: int  # hull units on the local market
    contract_matches: int  # count of similar contracts (from ContractScan)
    total_available_hulls: int  # market_hulls + contract_matches (informational)


@dataclass
class ContractMatch:
    contract_id: int
    fit_id: str  # the doctrine fit it matched
    similarity: float  # 0.0..1.0 (see §5)
    price: float  # contract price (ISK)
    title: str
    has_extras: bool  # contract carries items beyond the fit's scored BoM
    extra_type_ids: list[int]  # the extra item type_ids (for "bundle includes…")
    issuer_id: int
    is_alliance: bool  # True if visible via corp/alliance route (vs public)
    location_id: int
    system_id: int | None


@dataclass
class ContractScan:
    region_id: int
    taken_at: str
    matches: dict[str, list[ContractMatch]]  # fit_id -> matches (sorted by price asc)
    scanned_contracts: int  # how many contracts we fetched items for this pass
    from_cache: int  # how many were served from the item cache (unchanged)
    # Reasons the pass is degraded (mirrors ``MarketSnapshot.degraded``): e.g.
    # ``"contract_error_limit"`` when the ESI error budget forced an early abort
    # so the returned matches are PARTIAL (the contracts fetched before the floor
    # was hit are kept). Empty ``[]`` => a clean, complete pass. Defaulted so
    # every existing construction (and any older caller) stays valid.
    degraded: list[str] = field(default_factory=list)


@dataclass
class GapItem:
    type_id: int
    name: str
    needed: int  # units required to reach the seed target
    available: int  # units already on the staging market
    short: int  # max(0, needed - available)


@dataclass
class GapList:
    items: list[GapItem]  # only items with short > 0 (the shopping list)
    target_desc: str  # e.g. "20x Onyx doctrine"

    def janice(self) -> str:
        """``name<TAB>short`` lines (only shortfalls). TAB is unambiguous with
        multi-word item names, which Janice's paste parser handles."""
        return "\n".join(f"{it.name}\t{it.short}" for it in self.items)

    def multibuy(self) -> str:
        """``name<space>short`` lines (only shortfalls). EVE Multibuy's native
        clipboard form is ``<Item Name> <qty>`` with any-whitespace delimiter."""
        return "\n".join(f"{it.name} {it.short}" for it in self.items)


# ── Market source value object (§4.2) ────────────────────────────────────────


@dataclass(frozen=True)
class MarketSource:
    """Which market to scan.

    ``kind == "structure"`` → authed structure pull of ``source_id`` (a citadel).
    ``kind == "station"``  → region sell orders filtered to ``source_id`` (an NPC
    station); ``region_id`` is then required. ``system_id`` is the resolved
    staging system (optional, for display/filter)."""

    kind: str  # "structure" | "station"
    source_id: int  # structure_id OR station_id
    region_id: int
    system_id: int | None = None

    @staticmethod
    def from_config(market_cfg: dict) -> "MarketSource | None":
        """Build a MarketSource from a ``config["market"]`` block, applying the
        design's resolution rules (§6.2):

        * ``staging_structure_id > 0``  → structure pull (citadel; the primary).
        * else ``staging_station_id > 0`` and ``staging_region_id > 0`` → NPC
          station region-filter path.
        * else → ``None`` (unconfigured; caller returns an empty snapshot with a
          "configure staging market" status, no network).
        """
        cfg = market_cfg or {}
        structure_id = int(cfg.get("staging_structure_id", 0) or 0)
        station_id = int(cfg.get("staging_station_id", 0) or 0)
        region_id = int(cfg.get("staging_region_id", 0) or 0)
        system_id = cfg.get("staging_system_id")
        system_id = int(system_id) if system_id else None
        if structure_id > 0:
            return MarketSource("structure", structure_id, region_id, system_id)
        if station_id > 0 and region_id > 0:
            return MarketSource("station", station_id, region_id, system_id)
        return None


# ── Injectable ESI adapter (§4.1) ────────────────────────────────────────────


class MarketEsiAdapter(Protocol):
    """The network seam. The scanner never imports ``requests``; it calls these.

    Every method returns plain dicts/lists (ESI JSON shapes). The concrete
    ``market_esi.AuthMarketAdapter`` implements this over a live ESIAuth; tests
    pass a ``FakeMarketAdapter`` returning canned payloads.
    """

    def region_orders(
        self, region_id: int, *, type_id: int | None = None, order_type: str = "sell"
    ) -> list[dict]:
        ...

    def structure_orders(self, structure_id: int) -> list[dict]:  # authed
        ...

    def public_contracts(self, region_id: int) -> list[dict]:
        ...

    def contract_items(self, contract_id: int) -> "tuple[list[dict], str]":
        # Returns ``(items, status)``; ``status`` is the per-call negative-cache
        # disposition ("ok"/"dead"/"transient"). Returned per call (not stashed on
        # the adapter) so the parallel Pass-2 attributes each contract's outcome to
        # its OWN fetch.
        ...

    def resolve_location_system(self, location_id: int) -> int | None:
        ...

    # Optional (corp/alliance contracts). The scanner duck-types their presence.
    def corp_contracts(self, corporation_id: int) -> list[dict]:
        ...

    def corp_contract_items(self, corporation_id: int, contract_id: int) -> "tuple[list[dict], str]":
        # Returns ``(items, status)`` — see ``contract_items``.
        ...


@dataclass
class _FetchResult:
    """One survivor's Pass-2 outcome, produced by a POOL WORKER and consumed by
    the coordinating (scan) thread. Workers are pure w.r.t. shared scanner state:
    they do the network item fetch + the (pure) match and hand back this record;
    the coordinating thread then does all the shared-state work (cache write,
    counters, ``matches`` accumulation, progress) as the future completes, so no
    lock is needed anywhere. ``match_list`` is ``[(fit_id, ContractMatch), ...]``.
    """
    idx: int  # position in ``to_fetch`` — matches are applied in this order (det.)
    contract_id: int
    items: list  # the fetched item rows ([] when empty/dead/transient/raised)
    status: str  # adapter disposition: "ok" / "dead" / "transient" / "raise"
    error_limited: bool  # the adapter reported error-budget floor after this call
    raised: bool  # the adapter call raised (→ transient, do not cache)
    match_list: list  # [(fit_id, ContractMatch), ...] for this contract


# ── The scanner (§4.2) ───────────────────────────────────────────────────────


class MarketScanner:
    """Compute staging-market availability + contract matches + gap lists.

    Construction mirrors the house style (``adapter, catalog, cache_path``). The
    catalog is a duck-typed ``TypeCatalog`` (``resolve_name`` / ``category_of``).
    All heavy computation is pure; the only shared mutable state is the JSON
    cache, guarded by ``self._lock``.
    """

    def __init__(self, adapter, catalog, cache_path: str | None = None) -> None:
        if cache_path is None:
            cache_path = os.path.join(app_dir(), "market_scanner_cache.json")
        self._adapter = adapter
        self._catalog = catalog
        self._cache_path = cache_path
        self._lock = threading.Lock()
        # In-memory cache view for the duration of ONE ``scan_contracts`` pass
        # (set/cleared there). None outside a scan → the cache helpers fall back
        # to their direct per-call disk read-modify-write. See _ContractCacheSession.
        self._session: "_ContractCacheSession | None" = None

    # ── Market depth scan ─────────────────────────────────────────────────────

    def scan_market(
        self,
        source: MarketSource | None,
        *,
        progress=None,
        type_ids: "set[int] | None" = None,
        scope_label: str = "",
    ) -> MarketSnapshot:
        """Pull the staging market and return a depth snapshot (sell-side).

        ``None`` source (unconfigured) → an empty snapshot, no network. A
        structure source pulls the whole authed order book; a station source
        pulls region sell orders and filters to the station's ``location_id``.
        Adapter errors are surfaced as an empty snapshot (never raised) so a
        single bad pull degrades gracefully instead of crashing the worker.

        **Scope.** ``type_ids=None`` (default) is a FULL scan — every market type,
        exactly the historical behaviour. A ``type_ids`` set requests a
        doctrine-TARGETED scan of only those types:

        * **Region / NPC-station path** — one server-side ``type_id``-filtered
          query PER type (see ``_pull_region_by_type``): a doctrine's ~30-120
          types cost that many small one-page requests instead of paging the
          whole busy region's order book (each per-type query is ETag-cached
          independently, so re-scans are cheap 304s).
        * **Citadel / structure path** — ESI offers NO per-type structure-market
          filter, so the whole order book is STILL fetched (ETag makes repeats
          cheap); the snapshot then RETAINS only ``type_ids`` (a smaller depth
          build + a leaner cache). This is an ESI limitation, not a choice.

        The resulting snapshot records its ``scope`` ("full"/"doctrine"), the
        ``scanned_type_ids`` set, and a ``scope_label`` (the doctrine name, for an
        honest UI message) so a doctrine-scoped cached snapshot never masquerades
        as full coverage for a different doctrine (consumers gate on
        ``MarketSnapshot.covers``).

        ``progress`` (optional) is a cheap callback the scan feeds coarse stage
        dicts as it works, so the GUI can move off a dead "Scanning" label:

        * ``{"stage": "market", "status": "orders_start"}`` — before the pull.
        * ``{"stage": "orders", "page": i, "pages": n}`` — per page of the order
          book, ONLY when the adapter reports pages (``supports_progress``); the
          big first-scan cost is a many-page structure book, so this is where the
          per-page bar comes from.
        * ``{"stage": "market", "status": "orders_done", "orders": N}`` — pull done.
        * ``{"stage": "market", "status": "depth_done", "types": M}`` — depth built.
        * ``{"stage": "market", "status": "orders_ready", "snapshot": snap}`` —
          the finished, analysis-ready ``MarketSnapshot`` (emitted AFTER it is
          built + cached, so it is the same object this method returns). A live
          consumer can render order-derived doctrine/fit availability from it
          immediately — before the separate, far slower contract scan runs — with
          contract-derived figures shown as pending. The snapshot is not mutated
          after this point, so it is safe to read from another (Tk) thread.

        Callbacks are exception-swallowed (a raising callback can't break the
        scan) and fire on the scanning thread — the GUI marshals them.
        """
        if source is None:
            return MarketSnapshot(
                source_id=0,
                source_kind="",
                region_id=0,
                system_id=None,
                taken_at=_now_iso(),
                depth={},
            )

        t0 = time.monotonic()

        # None → full pull (historical). A set → a doctrine-targeted scan of
        # exactly these types (normalized to ints once, up front).
        targeted = type_ids is not None
        tid_set = {int(t) for t in type_ids} if targeted else None

        # Clear the adapter's per-pass forbidden record so a stale 403 from an
        # earlier scan can't leak into this snapshot (duck-typed: fake adapters
        # without ``last_forbidden`` are unaffected).
        fb = getattr(self._adapter, "last_forbidden", None)
        if isinstance(fb, set):
            fb.clear()

        _emit_progress(progress, {"stage": "market", "status": "orders_start"})
        try:
            raw = self._pull_orders(source, progress, tid_set)
        except Exception:
            log.exception("[market] order pull failed for source %s", source)
            raw = []
        raw = raw or []
        _emit_progress(
            progress, {"stage": "market", "status": "orders_done", "orders": len(raw)}
        )

        depth = self._build_depth(raw, source, tid_set)
        _emit_progress(
            progress, {"stage": "market", "status": "depth_done", "types": len(depth)}
        )
        snap = MarketSnapshot(
            source_id=source.source_id,
            source_kind=source.kind,
            region_id=source.region_id,
            system_id=source.system_id,
            taken_at=_now_iso(),
            depth=depth,
            degraded=self._degraded_reasons(source),
            scope=("doctrine" if targeted else "full"),
            scanned_type_ids=(tid_set if targeted else None),
            scope_label=((scope_label or "") if targeted else ""),
        )
        self._store_snapshot(snap)
        # Analysis-ready hand-off: the depth snapshot is fully built and cached,
        # so hand the finished object to any live consumer NOW. This is the seam
        # that lets the GUI paint doctrine/fit availability from ORDER data before
        # the (separate, ~100x slower) contract scan even starts — the caller
        # renders contract-derived figures as pending until scan_contracts lands.
        # Carries the snap itself (not just counts) so no second fetch is needed;
        # emitted after store so a consumer and the disk cache never disagree.
        _emit_progress(
            progress, {"stage": "market", "status": "orders_ready", "snapshot": snap}
        )
        log.info(
            "[market] order scan: source=%s/%s scope=%s orders=%d types=%d elapsed=%.2fs",
            source.kind, source.source_id, ("doctrine" if targeted else "full"),
            len(raw), len(depth), time.monotonic() - t0,
        )
        return snap

    def _pull_orders(self, source: "MarketSource", progress, type_ids=None) -> list[dict]:
        """Call the adapter's order endpoint for ``source``, wiring the optional
        per-page ``progress`` callback only when the adapter advertises support
        (``supports_progress``). Fake/older adapters — which take no ``progress``
        kwarg — are called the classic way, so this stays backward-compatible.

        ``type_ids`` (a targeted scan) changes ONLY the region/station path: it
        issues one server-side ``type_id`` query per type instead of paging the
        whole region. The structure path has no per-type ESI filter, so it still
        pulls the whole book — the caller trims it to ``type_ids`` in
        ``_build_depth``."""
        prog_ok = progress is not None and getattr(self._adapter, "supports_progress", False)
        if source.kind == "structure":
            # No server-side structure-market type filter (ESI limitation): pull
            # the whole book whether targeted or not; _build_depth trims it.
            if prog_ok:
                return self._adapter.structure_orders(source.source_id, progress=progress)
            return self._adapter.structure_orders(source.source_id)
        # Region / NPC-station path.
        if type_ids:
            return self._pull_region_by_type(source.region_id, type_ids, progress)
        if prog_ok:
            return self._adapter.region_orders(
                source.region_id, order_type="sell", progress=progress
            )
        return self._adapter.region_orders(source.region_id, order_type="sell")

    def _pull_region_by_type(self, region_id: int, type_ids, progress) -> list[dict]:
        """Targeted region pull: one server-side ``type_id``-filtered sell-order
        query per type, concatenated.

        This is the whole point of a targeted scan — a doctrine's ~30-120 types
        cost that many small (usually one-page) requests instead of paging an
        entire busy region's order book. Each per-type query is ETag-cached
        independently by the adapter (one ETag per ``(region, type)``), so a
        re-scan replays cheap 304s. A per-type failure is swallowed (that type
        simply shows as absent) so one bad type can't abort the whole pass.

        Progress ticks once per type as ``{"stage": "orders", "page": i,
        "pages": N}`` — reusing the existing per-page status slot so the GUI shows
        'orders page i/N' advancing over the doctrine's types. ``progress`` is
        emitted directly (not delegated to the adapter) so it counts types, not
        the single page each per-type query returns."""
        tids = sorted({int(t) for t in type_ids})
        total = len(tids)
        orders: list[dict] = []
        for i, tid in enumerate(tids, start=1):
            try:
                chunk = self._adapter.region_orders(
                    region_id, type_id=tid, order_type="sell")
            except Exception:
                log.exception(
                    "[market] region per-type order pull failed (type %s)", tid)
                chunk = []
            if chunk:
                orders.extend(chunk)
            _emit_progress(progress, {"stage": "orders", "page": i, "pages": total})
        return orders

    def _degraded_reasons(self, source: "MarketSource") -> list[str]:
        """Reasons the just-pulled snapshot is degraded (forbidden != empty).

        Combines two duck-typed signals so a fake adapter without either is
        simply treated as healthy:

        * REACTIVE — the adapter's ``last_forbidden`` set (a 403 the structure /
          corp route recorded during this pass, e.g. ``"structure_market"``).
        * PROACTIVE pre-check belt — a structure source whose auth already
          reports ``has_market_structure_scope() == False`` is degraded even
          before/without a network 403 (missing scope means the pull can only
          come back forbidden-empty). Reached via the adapter's ``_auth``.
        """
        reasons: set[str] = set()
        fb = getattr(self._adapter, "last_forbidden", None)
        if isinstance(fb, (set, frozenset, list, tuple)):
            reasons.update(str(x) for x in fb)
        if source is not None and source.kind == "structure":
            auth = getattr(self._adapter, "_auth", None)
            checker = getattr(auth, "has_market_structure_scope", None)
            if callable(checker):
                try:
                    if not checker():
                        reasons.add("structure_market")
                except Exception:
                    pass
        return sorted(reasons)

    def _build_depth(
        self, orders: list[dict], source: MarketSource, type_ids=None
    ) -> dict[int, TypeDepth]:
        """Aggregate raw ESI order dicts into per-type sell ladders.

        * Drops buy orders (``is_buy_order`` truthy) — sell-side only.
        * For the STATION path, keeps only orders at ``source.source_id``
          (``location_id`` filter) since the region pull returns the whole
          region. For the STRUCTURE path every order is already structure-local.
        * When ``type_ids`` is given (a targeted scan) only those types are
          retained — this trims the structure path's whole-book pull down to the
          doctrine's items. The region path is already type-filtered server-side,
          so the guard is a cheap no-op there.
        * Builds an ascending-price ladder per type and the total sell volume.
        """
        buckets: dict[int, list[PriceLevel]] = {}
        for o in orders:
            if not isinstance(o, dict):
                continue
            if o.get("is_buy_order"):
                continue  # sell-side only
            if source.kind == "station":
                if o.get("location_id") != source.source_id:
                    continue
            try:
                tid = int(o["type_id"])
                price = float(o["price"])
                vol = int(o.get("volume_remain", o.get("volume_total", 0)))
            except (KeyError, TypeError, ValueError):
                continue
            if type_ids is not None and tid not in type_ids:
                continue  # targeted scan: retain only the requested types
            if vol <= 0:
                continue
            buckets.setdefault(tid, []).append(PriceLevel(price=price, volume=vol))

        depth: dict[int, TypeDepth] = {}
        for tid, levels in buckets.items():
            levels.sort(key=lambda lvl: lvl.price)  # ascending: best sell first
            total = sum(lvl.volume for lvl in levels)
            depth[tid] = TypeDepth(type_id=tid, sell_qty=total, sell_ladder=levels)
        return depth

    # ── Doctrine availability (seeding) ───────────────────────────────────────

    def doctrine_availability(
        self,
        doctrine,
        store,
        snap: MarketSnapshot,
        contracts: "ContractScan | None" = None,
    ) -> list[DoctrineAvailability]:
        """For every fit in ``doctrine``, compute per-component depth/breadth and
        the binding-constraint ``completable_fits``.

        ``components`` on each result carries the FULL bill-of-materials (hull +
        modules + subsystems + charges + drones), each row flagged by ``role``,
        so a later UI can render complete tables. ``completable_fits`` and
        ``breadth_pct`` are computed over the SCORED subset only (hull + modules
        + subsystems, per owner decision #4 as amended; charges/drones/cargo
        excluded).
        """
        results: list[DoctrineAvailability] = []
        for member in getattr(doctrine, "members", []):
            fit = store.get_fit(member.fit_id) if store is not None else None
            if fit is None:
                continue
            results.append(self._fit_availability(fit, snap, contracts))
        return results

    def _fit_availability(
        self, fit, snap: MarketSnapshot, contracts: "ContractScan | None"
    ) -> DoctrineAvailability:
        bom = fit_bom(fit.parsed, self._catalog)
        comps: list[ComponentAvailability] = []
        for c in bom:
            td = snap.get(c.type_id)
            market_qty = td.sell_qty if td else 0
            best = td.best_sell if td else None
            completable = market_qty // c.per_fit_qty if c.per_fit_qty > 0 else 0
            comps.append(
                ComponentAvailability(
                    component=c,
                    market_qty=market_qty,
                    best_price=best,
                    completable=completable,
                    on_market=(market_qty > 0),
                )
            )

        # Scored subset (hull + modules + subsystems) for completable_fits + breadth.
        scored = [
            ca
            for ca in comps
            if ca.component.role == "hull" or ca.component.role in _SCORING_MODULE_ROLES
        ]
        if scored:
            binding = min(scored, key=lambda ca: ca.completable)
            completable_fits = binding.completable
            binding_type_id = binding.component.type_id
            binding_name = binding.component.name
            on_market_count = sum(1 for ca in scored if ca.on_market)
            breadth_pct = 100.0 * on_market_count / len(scored)
        else:
            completable_fits = 0
            binding_type_id = None
            binding_name = None
            breadth_pct = 0.0

        hull_td = snap.get(fit.hull_type_id)
        market_hulls = hull_td.sell_qty if hull_td else 0
        contract_matches = 0
        if contracts is not None:
            contract_matches = len(contracts.matches.get(fit.id, []))

        return DoctrineAvailability(
            fit_id=fit.id,
            hull_type_id=fit.hull_type_id,
            hull_name=fit.hull_name,
            components=comps,
            completable_fits=completable_fits,
            binding_type_id=binding_type_id,
            binding_name=binding_name,
            breadth_pct=breadth_pct,
            market_hulls=market_hulls,
            contract_matches=contract_matches,
            total_available_hulls=market_hulls + contract_matches,
        )

    # ── Contract scan ─────────────────────────────────────────────────────────

    def scan_contracts(
        self,
        region_id: int,
        fits: list,
        *,
        system_id: int | None = None,
        corp_id: int | None = None,
        region_wide: bool = False,
        progress=None,
        cancel=None,
    ) -> ContractScan:
        """Scan public (+ optional corp) item-exchange contracts in ``region_id``
        for ones that match any of ``fits`` (same hull, >=95% over modules +
        subsystems; charges/drones/cargo excluded).

        System filter: when ``system_id`` is given, a contract is kept only if it
        resolves to that system — UNLESS ``region_wide`` is True (then unknown /
        other-system contracts are kept too). Contracts whose location can't be
        resolved are treated as unknown.

        **Order of operations (perf-critical).** The region contract *list* is
        cheap (paginated, 1800s cache); the expensive part is the PER-CONTRACT
        item fetch. So the scan runs in two passes, and NO item is fetched for a
        contract that a cheap filter would drop:

        * **Pass 1 — cheap pre-filters only, zero item fetches.** Drop non
          ``item_exchange`` types and missing ids, then (when a ``system_id``
          filter is active) resolve each survivor's ``start_location_id`` → system
          via the *cached* resolver and drop out-of-staging contracts. Location
          resolves are memoised per ``location_id``, so the hundreds of contracts
          that overwhelmingly sit at the ONE staging citadel cost a single resolve.
        * **Pass 2 — fetch items ONLY for the survivors, then match.** Cache hits
          replay inline (instant, no network); the remaining live fetches run
          CONCURRENTLY on a ``CONTRACT_FETCH_WORKERS``-wide pool to overlap the
          per-request latency (the global 15/s ESI limiter is still the true
          cadence gate). Pool WORKERS are pure — they fetch + match and return a
          result; THIS thread does every shared-state mutation (cache write,
          counters, ``matches``, progress) as each future completes, so results
          are deterministic (matches applied in ``to_fetch`` order, then sorted by
          price, exactly as the old serial loop) and no lock is needed.

        The persistent contract-items cache (immutable per contract_id) is held in
        memory for the pass and flushed back to disk every ``_ITEM_FLUSH_EVERY``
        fetched contracts + at pass end, so a re-scan replays unchanged contracts
        with no network AND an interrupted first scan resumes from what it already
        fetched. Every adapter failure is swallowed per contract (a bad item pull
        just skips that contract) so a firehose region never crashes the pass.

        **Error-limit + cancel.** Before submitting each fetch AND on each
        completion the loop checks the adapter's ``error_limited`` flag; once the
        ESI error budget drops below the floor it STOPS submitting, drains the
        in-flight fetches, records ``"contract_error_limit"`` in the returned
        scan's ``degraded`` list, and keeps the matches gathered so far (partial,
        never a crash). ``cancel`` (optional) is a zero-arg predicate polled the
        same way: a truthy return stops new submissions, drains what is running,
        and returns the partial result promptly (bounded by the in-flight fetches'
        own timeouts) — the executor is always shut down cleanly, so a cancel can
        never hang the caller. ``cancel=None`` (the default) never cancels, so the
        existing GUI call site is unchanged.

        ``progress`` (optional) is a cheap, exception-swallowed callback fed:

        * ``{"stage": "contracts_list", "page": i, "pages": n}`` — per contract-list
          page (only when the adapter reports pages via ``supports_progress``).
        * ``{"stage": "filter", "done": i, "total": n, "kept": k}`` — throttled,
          during pass 1.
        * ``{"stage": "contracts", "done": i, "total": n, "from_cache": k}`` — per
          item fetch in pass 2 (the main progress bar; ``total`` is the exact count
          of contracts that survived the filters).
        * ``{"stage": "contracts", "status": "complete", "scanned": s,
          "from_cache": k, "matches": m}`` — at the end.
        """
        t0 = time.monotonic()
        matches: dict[str, list[ContractMatch]] = {f.id: [] for f in fits}
        scanned = 0
        from_cache = 0
        degraded: list[str] = []

        if not fits:
            return ContractScan(
                region_id=region_id,
                taken_at=_now_iso(),
                matches=matches,
                scanned_contracts=0,
                from_cache=0,
            )

        # Pre-compute each fit's scored BoM (hull gate + module multiset) once.
        fit_specs = [(f, _contract_scoring_spec(f, self._catalog)) for f in fits]

        try:
            public = self._contract_list(self._adapter.public_contracts, region_id, progress) or []
        except Exception:
            log.exception("[market] public contracts pull failed for region %s", region_id)
            public = []

        contract_source: list[tuple[dict, bool]] = [(c, False) for c in public]

        # Optional corp/alliance contracts (duck-typed adapter method). Corp lists
        # can be huge (couriers etc.) — they go through the SAME two-pass filter,
        # so a corp courier at the wrong end of the region is dropped before any
        # item fetch, exactly like a public one.
        n_corp = 0
        if corp_id and hasattr(self._adapter, "corp_contracts"):
            try:
                corp = self._contract_list(self._adapter.corp_contracts, corp_id, progress) or []
                n_corp = len(corp)
                contract_source.extend((c, True) for c in corp)
            except Exception:
                log.exception("[market] corp contracts pull failed for corp %s", corp_id)

        n_listed = len(contract_source)
        n_item_exchange = 0
        n_status_dropped = 0

        # Hold the cache in memory for the whole pass (one disk read up front) and
        # flush it back periodically + at the end (see the finally). This kills the
        # O(n²) whole-file re-read/re-write the per-contract cache otherwise did.
        self._session = _ContractCacheSession(self, flush_every=_ITEM_FLUSH_EVERY)
        try:
            # ── PASS 1 — cheap pre-filters ONLY (no item fetches). ──────────────
            # (contract, is_alliance, csys, loc_id) for each survivor.
            to_fetch: list[tuple[dict, bool, int | None, int]] = []
            for idx, (contract, is_alliance) in enumerate(contract_source):
                if not isinstance(contract, dict):
                    continue
                if str(contract.get("type", "")) != "item_exchange":
                    continue
                if contract.get("contract_id") is None:
                    continue
                n_item_exchange += 1

                # ── Status guard (the corp-history firehose fix). ──────────────
                # The corp/character contracts endpoint returns up to ~30 days of
                # contract HISTORY of ALL statuses (finished / finished_issuer /
                # finished_contractor / cancelled / rejected / failed / deleted /
                # reversed / in_progress), so a busy staging corp lists a
                # thousand-plus DEAD contracts. Only "outstanding" contracts are
                # still acceptable — seedable — stock. Drop any row whose status is
                # present AND not "outstanding" BEFORE the (cached) location resolve
                # and the expensive per-contract item fetch. The PUBLIC region
                # endpoint omits the status field and already lists only
                # outstanding contracts, so a MISSING status is kept — which also
                # defensively guards the public path if it ever grows the field.
                status = contract.get("status")
                if status is not None and str(status) != "outstanding":
                    n_status_dropped += 1
                    continue

                loc_id = int(contract.get("start_location_id", 0) or 0)
                # Only resolve the contract's system when it is actually needed: a
                # system filter is active. With no filter the match is region-wide
                # and csys is informational only — skip the resolve entirely.
                if system_id is not None:
                    csys = self._resolve_system_cached(loc_id)
                    # Strict system filter keeps ONLY confirmed in-staging
                    # contracts (csys == system_id): other-system AND
                    # unknown-location (csys is None) are both dropped BEFORE any
                    # item fetch. region_wide keeps everything (unknown included).
                    if not region_wide and csys != system_id:
                        continue
                else:
                    csys = None

                to_fetch.append((contract, is_alliance, csys, loc_id))
                if progress is not None and idx % _FILTER_PROGRESS_STRIDE == 0:
                    _emit_progress(
                        progress,
                        {"stage": "filter", "done": idx + 1, "total": n_listed,
                         "kept": len(to_fetch)},
                    )
            _emit_progress(
                progress,
                {"stage": "filter", "done": n_listed, "total": n_listed,
                 "kept": len(to_fetch)},
            )

            # ── PASS 2 — fetch survivors' items (parallel), then match. ─────────
            # Cache reads + writes, the counters, the ``matches`` accumulator, the
            # ``degraded`` list and progress emission are ALL confined to THIS (the
            # coordinating) thread; the pool workers only fetch + match and RETURN a
            # _FetchResult. That confinement is why nothing here needs a lock even
            # though the fetches overlap, and it preserves the cache session's
            # batched-flush cadence — ``put_items`` still runs only here, one call
            # per completed future, exactly as the old serial loop did.
            total_fetch = len(to_fetch)
            # Per-survivor matches keyed by to_fetch index so they are APPLIED in
            # to_fetch order regardless of which future finishes first → the result
            # is byte-identical to the serial pass (then a final per-fit price sort,
            # as before).
            match_by_idx: dict[int, list] = {}
            aborted = False       # ESI error-limit floor forced an early stop
            cancelled = False     # the caller's cancel predicate fired

            def _stop_requested() -> bool:
                if not callable(cancel):
                    return False
                try:
                    return bool(cancel())
                except Exception:
                    return False

            # 2a. Cache reads (instant, no network): a hit replays and counts as
            #     from_cache exactly as before; misses queue for the parallel fetch.
            misses: list[tuple[int, dict, bool, "int | None", int]] = []
            for idx, (contract, is_alliance, csys, loc_id) in enumerate(to_fetch):
                cid = contract.get("contract_id")
                cached = self._read_contract_items_cache(int(cid))
                if cached is not None:
                    scanned += 1
                    from_cache += 1
                    match_by_idx[idx] = self._match_contract_items(
                        cached, contract, is_alliance, loc_id, csys, fit_specs)
                    _emit_progress(
                        progress,
                        {"stage": "contracts", "done": scanned, "total": total_fetch,
                         "from_cache": from_cache},
                    )
                else:
                    misses.append((idx, contract, is_alliance, csys, loc_id))

            # 2b. Live fetches in parallel with a BOUNDED in-flight window, so the
            #     pass can stop submitting the instant an error-limit or a cancel is
            #     observed and merely drain what is already running.
            if misses and not _stop_requested():
                workers = min(CONTRACT_FETCH_WORKERS, len(misses))
                ex = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
                miss_iter = iter(misses)
                inflight: set = set()

                def _submit_next() -> bool:
                    nxt = next(miss_iter, None)
                    if nxt is None:
                        return False
                    m_idx, m_contract, m_ally, m_csys, m_loc = nxt
                    inflight.add(ex.submit(
                        self._fetch_and_match_contract, m_idx, m_contract,
                        m_ally, m_csys, m_loc, corp_id, fit_specs))
                    return True

                try:
                    # Poll the error-limit floor BEFORE the first submissions (4a):
                    # an earlier phase may already have driven the budget down.
                    if bool(getattr(self._adapter, "error_limited", False)):
                        aborted = True
                    else:
                        for _ in range(workers):
                            if not _submit_next():
                                break
                    while inflight:
                        done = concurrent.futures.wait(
                            inflight,
                            return_when=concurrent.futures.FIRST_COMPLETED).done
                        for fut in done:
                            inflight.discard(fut)
                            res = fut.result()  # worker catches all → never raises
                            scanned += 1
                            self._cache_live_result(res)
                            match_by_idx[res.idx] = res.match_list
                            _emit_progress(
                                progress,
                                {"stage": "contracts", "done": scanned,
                                 "total": total_fetch, "from_cache": from_cache},
                            )
                            if res.error_limited:
                                aborted = True
                        # Poll cancel + the live error-limit flag on EACH completion
                        # (4b); once either fires, stop feeding and let the in-flight
                        # fetches drain (the while loop keeps consuming them).
                        if _stop_requested():
                            cancelled = True
                        if not aborted and not cancelled and bool(
                                getattr(self._adapter, "error_limited", False)):
                            aborted = True
                        if aborted or cancelled:
                            continue
                        for _ in range(len(done)):
                            if not _submit_next():
                                break
                finally:
                    # Drain in-flight fetches + release the pool. wait=True is
                    # bounded — at most ``workers`` requests, each capped by the
                    # adapter's own GET timeout — so a cancel can never hang here.
                    ex.shutdown(wait=True)
            elif misses:
                cancelled = True  # cancelled before any live fetch started

            if aborted and "contract_error_limit" not in degraded:
                degraded.append("contract_error_limit")
                log.warning(
                    "[market] contract scan hit the ESI error-limit floor after "
                    "%d/%d fetched — stopped early, partial matches kept",
                    scanned, total_fetch,
                )

            # Apply per-survivor matches in to_fetch order (deterministic); the
            # final per-fit price sort below then reproduces the serial output.
            for idx in range(total_fetch):
                for fid, m in match_by_idx.get(idx, ()):
                    matches[fid].append(m)
        finally:
            # Final flush of anything not yet persisted (even on an unexpected
            # error mid-pass) so an interrupted scan keeps its fetched items, then
            # drop the in-memory view so the cache helpers go back to direct disk.
            try:
                if self._session is not None:
                    self._session.flush()
            except Exception:
                log.exception("[market] final contract-items cache flush failed")
            self._session = None

        for fid in matches:
            matches[fid].sort(key=lambda mm: mm.price)

        total_matches = sum(len(v) for v in matches.values())
        _emit_progress(
            progress,
            {"stage": "contracts", "status": "complete", "scanned": scanned,
             "from_cache": from_cache, "matches": total_matches},
        )
        log.info(
            "[market] contract scan: region=%s listed=%d (corp=%d) item_exchange=%d "
            "status_dropped=%d outstanding=%d fetched=%d (live=%d cache=%d) "
            "matches=%d elapsed=%.1fs",
            region_id, n_listed, n_corp, n_item_exchange, n_status_dropped,
            n_item_exchange - n_status_dropped, scanned,
            scanned - from_cache, from_cache, total_matches, time.monotonic() - t0,
        )

        return ContractScan(
            region_id=region_id,
            taken_at=_now_iso(),
            matches=matches,
            scanned_contracts=scanned,
            from_cache=from_cache,
            degraded=degraded,
        )

    def _contract_list(self, fn, arg, progress) -> list[dict]:
        """Call an adapter contract-*list* endpoint (``public_contracts`` /
        ``corp_contracts``), wiring the optional per-page ``progress`` callback
        only when the adapter advertises support (``supports_progress``). Fake /
        older adapters that take no ``progress`` kwarg are called the classic way,
        keeping this backward-compatible."""
        if progress is not None and getattr(self._adapter, "supports_progress", False):
            return fn(arg, progress=progress)
        return fn(arg)

    def _fetch_and_match_contract(
        self, idx: int, contract: dict, is_alliance: bool,
        csys: "int | None", loc_id: int, corp_id: int | None, fit_specs: list,
    ) -> "_FetchResult":
        """POOL-WORKER unit for one survivor: fetch its items over ESI and match
        them against every fit, returning a ``_FetchResult``. PURE with respect to
        shared scanner state — it touches NO cache, NO counter, NO ``matches``
        accumulator; the coordinating thread applies all of that as the future
        completes (see ``scan_contracts`` Pass 2 / ``_cache_live_result``). Never
        raises: an adapter failure is captured as ``status="raise"`` /
        ``raised=True`` so the coordinator simply skips + does not cache it (the
        same transient treatment the old serial path gave a raising fetch).

        The adapter RETURNS the per-call disposition as ``(items, status)`` — it is
        NOT read back off a shared adapter attribute, so under concurrency each
        contract's status belongs to its OWN fetch (reading a shared attribute on a
        later thread could misattribute a genuinely-dead contract's "dead" to a
        contract that merely hit a transient blip, tombstoning it forever). The
        error-budget flag (``error_limited``) is a MONOTONIC GLOBAL signal, so it is
        still polled off the adapter and rides back on the result."""
        cid = int(contract.get("contract_id") or 0)
        try:
            if is_alliance and corp_id and hasattr(self._adapter, "corp_contract_items"):
                items, status = self._adapter.corp_contract_items(corp_id, cid)
            else:
                items, status = self._adapter.contract_items(cid)
        except Exception:
            log.exception("[market] contract items pull failed for %s", cid)
            return _FetchResult(idx, cid, [], "raise", False, True, [])
        # ``error_limited`` is a monotonic global budget signal (not per-call), so
        # reading it off the adapter here is safe under concurrency.
        err = bool(getattr(self._adapter, "error_limited", False))
        items = items or []
        match_list = (
            self._match_contract_items(items, contract, is_alliance, loc_id, csys, fit_specs)
            if items else []
        )
        return _FetchResult(idx, cid, items, status, err, False, match_list)

    def _match_contract_items(
        self, items: list, contract: dict, is_alliance: bool,
        loc_id: int, csys: "int | None", fit_specs: list,
    ) -> list:
        """Pure per-contract match: reduce ``items`` to the included multiset and
        score it against every fit, returning ``[(fit_id, ContractMatch), ...]``
        (empty when nothing matches). No shared state — safe to run on a worker OR
        inline on the coordinating thread (a cache hit)."""
        if not items:
            return []
        included = _included_for_match(items, self._catalog)
        out: list = []
        for fit, spec in fit_specs:
            m = _match_contract(
                contract=contract,
                included=included,
                fit=fit,
                spec=spec,
                catalog=self._catalog,
                is_alliance=is_alliance,
                location_id=int(loc_id),
                system_id=csys,
            )
            if m is not None:
                out.append((fit.id, m))
        return out

    def _cache_live_result(self, res: "_FetchResult") -> None:
        """COORDINATING-THREAD cache write for one live fetch, mirroring the old
        ``_fetch_contract_items`` tail's negative-caching (the every-refresh
        dead-contract crawl fix). Runs only on the scan thread, so the in-memory
        cache session stays single-writer and its batched-flush cadence is intact.

        Classifies an EMPTY result by the worker-captured disposition:

        * ``"dead"`` — a definitively gone contract (404/410, or a 403 on the
          PUBLIC route = permanently restricted). Written as an immutable-forever
          TOMBSTONE (``dead=True``, no TTL) so it is NEVER re-fetched — it counts
          as ``from_cache`` on every later scan.
        * ``"transient"``/``"raise"`` — a timeout / 5xx / error-limit / corp-route
          scope 403 / raising adapter. NOT cached, so the next scan re-fetches (a
          live contract must never be hidden behind a transient blip).
        * ``"ok"`` — a genuinely empty 2xx. Cached as an ok-empty entry on the
          LONGER ``CACHE_TTL_CONTRACT_ITEMS_EMPTY`` timer (the empties-storm fix).

        A populated result is cached on the normal 1h TTL (disposition
        irrelevant)."""
        if res.raised:
            return  # adapter raised → transient, do not cache
        if res.items:
            # Live contract with items → normal-TTL cache (disposition irrelevant).
            self._write_contract_items_cache(res.contract_id, res.items)
            return
        if res.status == "dead":
            # Immutable-forever tombstone: a gone contract stays gone.
            self._write_contract_items_cache(res.contract_id, [], dead=True)
        elif res.status == "transient":
            # Timeout / 5xx / error-limit — do NOT cache; retry next scan.
            return
        else:
            # "ok" (a genuinely empty 2xx) — longer-TTL empty entry so the many
            # empties on a staging book stop re-fetching hourly.
            self._write_contract_items_cache(res.contract_id, [], empty=True)

    def _resolve_system_cached(self, location_id: int) -> int | None:
        """Resolve a contract ``location_id`` to a system id, memoized in the
        cache. 0 / falsy → None. Adapter errors → None (treated as unknown)."""
        if not location_id:
            return None
        cached = self._read_location_cache(int(location_id))
        if cached is not None:
            # Sentinel -1 means "resolved to unknown" — don't re-hit ESI for it.
            return None if cached == -1 else cached
        try:
            sysid = self._adapter.resolve_location_system(int(location_id))
        except Exception:
            log.exception("[market] location resolve failed for %s", location_id)
            sysid = None
        self._write_location_cache(int(location_id), sysid if sysid is not None else -1)
        return sysid

    # ── Gap list ──────────────────────────────────────────────────────────────

    def gap_list(
        self,
        doctrine,
        store,
        snap: MarketSnapshot,
        *,
        target_fits: int,
        components=None,
        per_fit_targets: "dict[str, int] | None" = None,
    ) -> GapList:
        """Build a shopping list to seed the doctrine, using the local market as
        on-hand stock.

        For each fit, ``needed`` per type = ``per_fit_qty * <fit's target>`` summed
        across fits; ``available`` = current market sell qty; ``short`` =
        ``max(0, needed - available)``. Only short items appear.

        **Per-fit seed targets.** ``target_fits`` is the doctrine-wide fallback.
        ``per_fit_targets`` (optional) is a ``fit_id -> target`` mapping consulted
        first for each member, falling back to ``target_fits`` for any fit not
        present — so a doctrine can seed e.g. 50 stabbers, 20 scythes and 10
        bifrosts instead of a flat N of every hull. When the applied targets vary
        across fits, ``target_desc`` reads ``"per-fit seed targets · <doctrine>"``
        (honest summary) instead of ``"Nx <doctrine>"``. Omitting the mapping
        (``None``) reproduces the prior single-target behaviour exactly.

        The gap builder accepts whichever component subset it is handed: the
        ``components`` predicate/iterable selects roles to include. Default
        (``None``) = the FULL BoM — hull + modules + subsystems + charges + cargo
        + drones — so the seeding shopping list covers modules, implants,
        boosters, ammo, drones and everything else the doctrine's fits carry
        (owner requirement). This is intentionally WIDER than the completable-fits
        / breadth scoring (which stays modules + subsystems + the hull gate): a
        fit is flyable without its cargo, but a seeding list is not complete
        without it. Pass an explicit ``components`` iterable to narrow — e.g.
        ``{"module", "subsystem"}`` for the flyable-only subset, or ``["hull"]``
        for hulls alone. ``components="full"`` is a synonym for the default.
        """
        # needed[type_id] -> (name, total needed units)
        needed: dict[int, list] = {}  # type_id -> [name, needed]
        role_filter = _resolve_gap_role_filter(components)
        overrides = per_fit_targets or {}
        # Distinct targets actually applied to fits that resolved (drives the
        # honest "varies" summary): a single value → "Nx"; two+ → per-fit.
        applied_targets: set[int] = set()

        for member in getattr(doctrine, "members", []):
            fit = store.get_fit(member.fit_id) if store is not None else None
            if fit is None:
                continue
            fit_target = overrides.get(getattr(member, "fit_id", None), target_fits)
            try:
                fit_target = int(fit_target)
            except (TypeError, ValueError):
                fit_target = target_fits
            fit_target = max(0, fit_target)
            applied_targets.add(fit_target)
            for c in fit_bom(fit.parsed, self._catalog):
                if not role_filter(c.role):
                    continue
                units = c.per_fit_qty * fit_target
                if units <= 0:
                    continue
                entry = needed.setdefault(c.type_id, [c.name, 0])
                entry[1] += units

        items: list[GapItem] = []
        for tid, (name, need_units) in needed.items():
            td = snap.get(tid)
            available = td.sell_qty if td else 0
            short = max(0, need_units - available)
            if short <= 0:
                continue
            items.append(
                GapItem(
                    type_id=tid,
                    name=name,
                    needed=need_units,
                    available=available,
                    short=short,
                )
            )
        # Stable, useful ordering: biggest shortfall first, then by name.
        items.sort(key=lambda it: (-it.short, it.name.lower()))

        doctrine_name = getattr(doctrine, "name", "") or "doctrine"
        if len(applied_targets) > 1:
            # Targets genuinely differ across fits — a single "Nx" would lie.
            target_desc = f"per-fit seed targets · {doctrine_name}"
        else:
            # Uniform: the one applied value if any fit resolved, else the fallback.
            uniform = next(iter(applied_targets)) if applied_targets else target_fits
            target_desc = f"{uniform}x {doctrine_name}"
        return GapList(items=items, target_desc=target_desc)

    # ── Cache (JSON, atomic, lock-guarded) ────────────────────────────────────
    #
    # File shape:
    #   {
    #     "snapshots": {"<source_id>": <MarketSnapshot.to_dict>},
    #     "contract_items": {"<contract_id>": {"items": [...], "etag": str|null,
    #                                          "fetched_at": iso}},
    #     "locations": {"<location_id>": system_id_int_or_-1},
    #     "etags": {"<key>": etag}     # region-orders / contracts-list ETags
    #   }

    def _read_cache(self) -> dict:
        """Read the whole cache file (defensive; corrupt/missing → empty)."""
        path = self._cache_path
        if not path or not os.path.exists(path):
            return {}
        try:
            import json

            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            log.warning("Discarding unreadable market scanner cache: %s", path)
            return {}
        return data if isinstance(data, dict) else {}

    def _write_cache(self, mutate) -> None:
        """Read-modify-write the cache under the lock (re-read inside the lock so
        concurrent scanners don't clobber), then atomically persist. ``mutate``
        takes the loaded dict and edits it in place. All failures are logged and
        swallowed — a cache write must never crash a scan."""
        with self._lock:
            data = self._read_cache()
            try:
                mutate(data)
            except Exception:
                log.exception("[market] cache mutate failed")
                return
            parent = os.path.dirname(self._cache_path)
            if parent and not os.path.isdir(parent):
                try:
                    os.makedirs(parent, exist_ok=True)
                except OSError:
                    return
            try:
                atomic_write_json(self._cache_path, data, indent=None)
            except Exception:
                log.exception("[market] failed to write cache: %s", self._cache_path)

    def _store_snapshot(self, snap: MarketSnapshot) -> None:
        if not snap.source_id:
            return

        def _mut(data: dict) -> None:
            data.setdefault("snapshots", {})[str(snap.source_id)] = snap.to_dict()

        self._write_cache(_mut)

    def load_snapshot(self, source_id: int) -> MarketSnapshot | None:
        """Load the last-cached snapshot for a source, or None."""
        data = self._read_cache()
        raw = (data.get("snapshots") or {}).get(str(source_id))
        if not isinstance(raw, dict):
            return None
        try:
            return MarketSnapshot.from_dict(raw)
        except Exception:
            return None

    def _read_contract_items_cache(self, contract_id: int) -> list[dict] | None:
        """Return cached items for a contract if present and still within its TTL,
        else None. A populated (or older, un-flagged) entry uses the 1h
        ``CACHE_TTL_CONTRACT_ITEMS``; an ok-EMPTY entry (``"empty": True``) uses
        the longer ``CACHE_TTL_CONTRACT_ITEMS_EMPTY`` so genuinely-empty contracts
        stop re-fetching hourly. A DEAD tombstone is served forever (no TTL).

        During a scan (``self._session`` set) this reads the in-memory session
        view — one disk read served the whole pass — instead of re-reading and
        re-parsing the whole cache file per contract."""
        if self._session is not None:
            return self._session.get_items(int(contract_id))
        data = self._read_cache()
        entry = (data.get("contract_items") or {}).get(str(contract_id))
        if not isinstance(entry, dict):
            return None
        # A permanent DEAD tombstone is served forever (no TTL) — a gone contract
        # stays gone, so we never re-fetch it.
        if entry.get("dead"):
            items = entry.get("items")
            return items if isinstance(items, list) else []
        # An ok-empty entry rides the longer empty TTL; a missing/absent "empty"
        # flag (older cache files, populated entries) falls back to the 1h timer.
        ttl = CACHE_TTL_CONTRACT_ITEMS_EMPTY if entry.get("empty") else CACHE_TTL_CONTRACT_ITEMS
        if _age_seconds(entry.get("fetched_at")) > ttl:
            return None
        items = entry.get("items")
        return items if isinstance(items, list) else None

    def _write_contract_items_cache(
        self, contract_id: int, items: list[dict], etag: str | None = None,
        *, dead: bool = False, empty: bool = False,
    ) -> None:
        # During a scan the write lands in the in-memory session (flushed to disk
        # in merged batches) instead of a full read-modify-write per contract.
        # ``dead`` marks a permanent tombstone (a gone contract): stored with no
        # TTL so it is served forever and never re-fetched. ``empty`` marks an
        # ok-empty result so the read path uses the longer empty TTL; it is an
        # OPTIONAL field — an entry without it reads exactly as before.
        if self._session is not None:
            self._session.put_items(int(contract_id), items, etag, dead=dead, empty=empty)
            return

        def _mut(data: dict) -> None:
            data.setdefault("contract_items", {})[str(contract_id)] = {
                "items": items,
                "etag": etag,
                "fetched_at": _now_iso(),
                "dead": dead,
                "empty": empty,
            }

        self._write_cache(_mut)

    def _read_location_cache(self, location_id: int) -> int | None:
        # During a scan, served from the in-memory session (so hundreds of
        # contracts at one citadel share a single resolve AND a single disk read).
        if self._session is not None:
            return self._session.get_location(int(location_id))
        data = self._read_cache()
        val = (data.get("locations") or {}).get(str(location_id))
        return int(val) if isinstance(val, int) else None

    def _write_location_cache(self, location_id: int, system_id: int) -> None:
        if self._session is not None:
            self._session.put_location(int(location_id), int(system_id))
            return

        def _mut(data: dict) -> None:
            data.setdefault("locations", {})[str(location_id)] = int(system_id)

        self._write_cache(_mut)


# ── In-memory cache session for one contract scan (§8, perf) ──────────────────


class _ContractCacheSession:
    """The persistent cache held in memory for the duration of ONE contract scan.

    WHY: the contract-items + location caches are immutable-per-key, but the
    naïve helper did a full ``_read_cache()`` (whole-file read + JSON parse) on
    every lookup and a full read-modify-write on every store. As the
    ``contract_items`` map fills during a first scan of a big staging citadel,
    that is O(n²) disk churn — the dominant, avoidable share of a "stuck on
    Scanning for minutes" pass. This session reads the cache ONCE, serves every
    lookup from memory, accumulates new entries, and flushes them back to disk in
    merged batches (every ``flush_every`` fetched contracts, plus a final flush),
    which also means an interrupted first scan resumes from what it already
    fetched instead of restarting.

    Concurrency: flushes go through ``MarketScanner._write_cache``, which re-reads
    the file under the scanner's lock and MERGES these pending entries in — so a
    concurrent writer (another snapshot store, say) is never clobbered; the whole
    in-memory view is never written back wholesale.
    """

    def __init__(self, scanner: "MarketScanner", *, flush_every: int = _ITEM_FLUSH_EVERY) -> None:
        self._scanner = scanner
        self._flush_every = max(1, int(flush_every))
        data = scanner._read_cache()  # the ONE disk read for the whole pass
        raw_items = data.get("contract_items")
        raw_locs = data.get("locations")
        # Full in-memory view (loaded ∪ fetched-this-pass), for serving lookups.
        self._items: dict[str, dict] = dict(raw_items) if isinstance(raw_items, dict) else {}
        self._locations: dict[str, int] = dict(raw_locs) if isinstance(raw_locs, dict) else {}
        # Delta accumulated since the last flush (only these are written back, so
        # a concurrent writer's other keys survive the merge).
        self._pending_items: dict[str, dict] = {}
        self._pending_locs: dict[str, int] = {}
        self._fetched_since_flush = 0

    # ── contract items ────────────────────────────────────────────────────────

    def get_items(self, contract_id: int) -> list[dict] | None:
        """Cached items for a contract within its TTL, else None — the SAME rule as
        the direct disk path: a populated (or older, un-flagged) entry uses the 1h
        ``CACHE_TTL_CONTRACT_ITEMS``, an ok-EMPTY entry (``"empty": True``) uses the
        longer ``CACHE_TTL_CONTRACT_ITEMS_EMPTY``, and a permanent DEAD tombstone
        bypasses the TTL — a gone contract stays gone, served forever so it is never
        re-fetched."""
        entry = self._items.get(str(contract_id))
        if not isinstance(entry, dict):
            return None
        if entry.get("dead"):
            items = entry.get("items")
            return items if isinstance(items, list) else []
        ttl = CACHE_TTL_CONTRACT_ITEMS_EMPTY if entry.get("empty") else CACHE_TTL_CONTRACT_ITEMS
        if _age_seconds(entry.get("fetched_at")) > ttl:
            return None
        items = entry.get("items")
        return items if isinstance(items, list) else None

    def put_items(
        self, contract_id: int, items: list[dict], etag: str | None = None,
        *, dead: bool = False, empty: bool = False,
    ) -> None:
        entry = {
            "items": items, "etag": etag, "fetched_at": _now_iso(),
            "dead": dead, "empty": empty,
        }
        key = str(contract_id)
        self._items[key] = entry
        self._pending_items[key] = entry
        # Only *fetched contracts* pace the flush cadence (per the task); location
        # resolves piggyback on the next flush + the final one.
        self._fetched_since_flush += 1
        if self._fetched_since_flush >= self._flush_every:
            self.flush()

    # ── locations ─────────────────────────────────────────────────────────────

    def get_location(self, location_id: int) -> int | None:
        """Cached resolved system id (or the -1 unknown sentinel), else None. The
        caller (`_resolve_system_cached`) interprets the sentinel."""
        val = self._locations.get(str(location_id))
        return int(val) if isinstance(val, int) else None

    def put_location(self, location_id: int, system_id: int) -> None:
        key = str(location_id)
        self._locations[key] = int(system_id)
        self._pending_locs[key] = int(system_id)

    # ── flush ─────────────────────────────────────────────────────────────────

    def flush(self) -> None:
        """Persist the pending delta (merged into a fresh disk read) and reset the
        counter. No-op when nothing is pending."""
        if not self._pending_items and not self._pending_locs:
            self._fetched_since_flush = 0
            return
        pend_items = self._pending_items
        pend_locs = self._pending_locs

        def _mut(data: dict) -> None:
            if pend_items:
                data.setdefault("contract_items", {}).update(pend_items)
            if pend_locs:
                data.setdefault("locations", {}).update(pend_locs)

        self._scanner._write_cache(_mut)
        self._pending_items = {}
        self._pending_locs = {}
        self._fetched_since_flush = 0


# ── Bill-of-materials helper (§4.3) ──────────────────────────────────────────


def fit_bom(parsed, catalog=None) -> list[Component]:
    """Reproduce ``fit_dna.to_dna``'s stacking as a bill-of-materials.

    Emits one ``Component`` per distinct line:

    * hull → 1x (role ``"hull"``)
    * each subsystem → 1x (role ``"subsystem"``)
    * modules grouped by ``type_id`` (summed across slots + online/offline;
      offline modules still count — they're still bought) → role ``"module"``
    * drones by type → summed qty (role ``"drone"``)
    * cargo + loaded charges merged by type_id → summed qty (role ``"charge"``
      if the catalog classifies the type as a charge, else ``"cargo"``)

    First-seen order is preserved within each section (hull, subsystems, modules,
    drones, cargo/charges) so output is deterministic. Names resolve via
    ``catalog.resolve_name`` when available, else fall back to any name already on
    the parsed line, else ``str(type_id)``.

    This is the SINGLE definition of "what a fit needs" for seeding; it is kept
    deliberately parallel to ``to_dna`` so the seeding math and the MOTD DNA link
    agree on the fit's contents. Like ``to_dna`` (which since the cargo-grammar
    rework in commit 210a874 also collapses the online/offline copies of a type
    into one token), module identity here is ``type_id`` only — an online and an
    offline copy of the same module are ONE BoM line, because for buying purposes
    they are the same item.

    Cargo note: ``fit_bom`` remains a SUPERSET of ``to_dna``'s items, and since
    commit 210a874 the two largely agree on cargo. ``to_dna`` no longer drops
    module-category cargo — it now EMITS it as an unfitted ``typeID_;qty`` token
    (a spare module/subsystem carried in the hold, marked unfitted so it lands in
    cargo instead of ghost-fitting into a slot) rather than omitting it; only a
    ship carried in cargo is still skipped (no sanctioned cargo-ship token in the
    grammar), which ``fit_bom`` keeps — hence "superset". ``fit_bom`` likewise
    carries every cargo + loaded-charge line (role ``"cargo"``/``"charge"``) so a
    full component table is available. Only the two scored numbers
    (``completable_fits`` / >=95% similarity) still exclude cargo/charges (see
    ``_SCORING_MODULE_ROLES``). The default gap list, by contrast, INCLUDES them:
    since commit 2ca9f42 ``gap_list(components=None)`` shops the full BoM — cargo,
    charges and drones included — because a seeding list is not complete without
    the ammo/boosters/drones the fits carry (narrow it with an explicit
    ``components`` set for the flyable-only subset).
    """

    def _name(type_id: int, fallback: str = "") -> str:
        if catalog is not None:
            resolver = getattr(catalog, "resolve_name", None)
            if callable(resolver):
                try:
                    n = resolver(type_id)
                except Exception:
                    n = None
                if isinstance(n, str) and n:
                    return n
        return fallback or str(type_id)

    def _is_charge(type_id: int) -> bool:
        if catalog is None:
            return True  # no classifier → treat merged cargo/charges as charges
        cat_of = getattr(catalog, "category_of", None)
        if not callable(cat_of):
            return True
        try:
            return cat_of(type_id) == "charge"
        except Exception:
            return False

    out: list[Component] = []

    # Hull.
    out.append(
        Component(
            type_id=parsed.ship_type_id,
            name=_name(parsed.ship_type_id, getattr(parsed, "ship_name", "")),
            per_fit_qty=1,
            role="hull",
        )
    )

    # Subsystems (one each, first-seen order).
    for sub_tid in getattr(parsed, "subsystems", []):
        out.append(
            Component(type_id=sub_tid, name=_name(sub_tid), per_fit_qty=1, role="subsystem")
        )

    # Modules grouped by type_id, summed across slots + online/offline state.
    module_counts: dict[int, int] = {}
    module_names: dict[int, str] = {}
    for m in getattr(parsed, "modules", []):
        module_counts[m.type_id] = module_counts.get(m.type_id, 0) + 1
        module_names.setdefault(m.type_id, getattr(m, "name", "") or "")
    for tid, qty in module_counts.items():
        out.append(
            Component(
                type_id=tid, name=_name(tid, module_names.get(tid, "")), per_fit_qty=qty,
                role="module",
            )
        )

    # Drones by type, summed.
    drone_counts: dict[int, int] = {}
    drone_names: dict[int, str] = {}
    for d in getattr(parsed, "drones", []):
        drone_counts[d.type_id] = drone_counts.get(d.type_id, 0) + d.quantity
        drone_names.setdefault(d.type_id, getattr(d, "name", "") or "")
    for tid, qty in drone_counts.items():
        out.append(
            Component(
                type_id=tid, name=_name(tid, drone_names.get(tid, "")), per_fit_qty=qty,
                role="drone",
            )
        )

    # Cargo + loaded charges merged by type_id (mirrors to_dna's merge). Loaded
    # charges from modules are folded in first-seen after cargo, matching to_dna.
    cargo_counts: dict[int, int] = {}
    cargo_names: dict[int, str] = {}
    for c in getattr(parsed, "cargo", []):
        cargo_counts[c.type_id] = cargo_counts.get(c.type_id, 0) + c.quantity
        cargo_names.setdefault(c.type_id, getattr(c, "name", "") or "")
    for m in getattr(parsed, "modules", []):
        ctid = getattr(m, "charge_type_id", None)
        if ctid is not None:
            cargo_counts[ctid] = cargo_counts.get(ctid, 0) + 1
            cargo_names.setdefault(ctid, getattr(m, "charge_name", "") or "")
    for tid, qty in cargo_counts.items():
        role = "charge" if _is_charge(tid) else "cargo"
        out.append(
            Component(
                type_id=tid, name=_name(tid, cargo_names.get(tid, "")), per_fit_qty=qty,
                role=role,
            )
        )

    return out


# ── Similarity scoring (§5) ──────────────────────────────────────────────────


@dataclass(frozen=True)
class _ScoringSpec:
    """Pre-computed per-fit data for contract matching: the hull gate + the
    scored module multiset + its total unit count."""

    hull_type_id: int
    # type_id -> qty (MODULES + SUBSYSTEMS, per decision #4 as amended;
    # charges/drones/cargo excluded)
    module_multiset: dict[int, int]
    total_units: int


def _contract_scoring_spec(fit, catalog) -> _ScoringSpec:
    """Build the ``_ScoringSpec`` for a fit: hull id + the scored multiset
    (modules + subsystems; owner decision #4 as amended).

    The >=95% similarity ratio uses HULL (as the gate) + FITTED MODULES + T3
    SUBSYSTEMS — a T3 without its subsystems isn't the doctrine ship.
    Charges/drones/cargo are excluded from the ratio's denominator (unlike the
    design doc's full-BoM ratio). The hull is the gate, so it is NOT in the
    multiset (matching the design's "hull excluded from the ratio" rule)."""
    multiset: dict[int, int] = {}
    total = 0
    for c in fit_bom(fit.parsed, catalog):
        if c.role in _SCORING_MODULE_ROLES:
            multiset[c.type_id] = multiset.get(c.type_id, 0) + c.per_fit_qty
            total += c.per_fit_qty
    return _ScoringSpec(
        hull_type_id=fit.hull_type_id, module_multiset=multiset, total_units=total
    )


def _included_for_match(items: list[dict], catalog) -> "dict[int, int]":
    """Reduce a contract's item list to the multiset ``C`` used for matching.

    Applies the assembled-ship detection rules (§5.1):

    * keep only ``is_included == true`` items (drop want-to-buy requests);
    * drop blueprint copies/originals (``is_blueprint_copy`` true OR category
      "blueprint" OR ``runs`` present and != -1 for a copy) — a blueprint of the
      hull is not the hull;
    * sum quantities by ``type_id``.

    Returns ``type_id -> qty`` over all included non-blueprint items (ships +
    modules + charges + drones alike; the caller decides which count)."""
    out: dict[int, int] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        if not it.get("is_included", False):
            continue
        if it.get("is_blueprint_copy"):
            continue
        # A blueprint COPY has a positive run count; a blueprint ORIGINAL reports
        # runs == -1 (design §11, verified). A blueprint of a type is not that
        # type, so drop any row with a positive `runs` (a copy) — but do NOT drop
        # on runs == -1 alone, because some ESI serializations report runs == -1
        # for ordinary (non-blueprint) items too; those are caught by the
        # category guard below instead. This avoids dropping legitimate modules.
        runs = it.get("runs")
        if isinstance(runs, int) and runs > 0:
            continue
        try:
            tid = int(it["type_id"])
            qty = int(it.get("quantity", 0))
        except (KeyError, TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        # Blueprint category guard (when catalog can classify) — catches BPOs
        # (runs == -1) and any blueprint the flags above missed.
        if catalog is not None:
            cat_of = getattr(catalog, "category_of", None)
            if callable(cat_of):
                try:
                    if cat_of(tid) == "blueprint":
                        continue
                except Exception:
                    pass
        out[tid] = out.get(tid, 0) + qty
    return out


def contract_similarity(spec: _ScoringSpec, included: "dict[int, int]") -> float:
    """The similarity ratio (§5): fraction of the fit's scored (modules +
    subsystems) units present in the contract's included items.

    ``matched = Σ min(D[t], C[t])``, ``total = Σ D[t]``, ``similarity =
    matched/total``. A fit with no scored components (``total_units == 0``) is
    treated as a perfect structural match (1.0) once the hull gate passes — there
    is nothing to be short of. Extras in ``C`` never lower the score."""
    if spec.total_units <= 0:
        return 1.0
    matched = 0
    for tid, need in spec.module_multiset.items():
        matched += min(need, included.get(tid, 0))
    return matched / spec.total_units


def _hull_present(spec: _ScoringSpec, included: "dict[int, int]", catalog) -> bool:
    """Condition (a): the contract contains exactly the fit's hull as a ship.

    We require the fit's ``hull_type_id`` to be among the included items with a
    ship category. Per §5.1, zero or >=2 DISTINCT ship-category hulls other than a
    single match is treated as "not a fit match"; here we implement the core
    gate: the fit's hull type must be present as a ship. Multiple copies of the
    SAME hull are allowed (a 2-pack of the doctrine hull still matches). A
    different ship alongside it does not by itself disqualify (it may be a bundle
    → recorded as an extra)."""
    if spec.hull_type_id not in included:
        return False
    if catalog is None:
        return True
    cat_of = getattr(catalog, "category_of", None)
    if callable(cat_of):
        try:
            return cat_of(spec.hull_type_id) == "ship"
        except Exception:
            return True
    return True


def _match_contract(
    *,
    contract: dict,
    included: "dict[int, int]",
    fit,
    spec: _ScoringSpec,
    catalog=None,
    is_alliance: bool = False,
    location_id: int | None = None,
    system_id: int | None = None,
) -> ContractMatch | None:
    """Evaluate one contract against one fit; return a ``ContractMatch`` iff it
    passes the hull gate AND scores >= the similarity threshold, else ``None``.

    ``contract`` is the raw ESI contract dict (for ``price`` / ``title`` /
    ``issuer_id`` / ``start_location_id``). ``included`` is the pre-reduced
    included-item multiset. ``catalog`` (optional) enables the hull ship-category
    check; without one the gate accepts any type equal to the hull id (tolerated
    for standalone tests). Records ``has_extras`` / ``extra_type_ids`` for any
    included item beyond the fit's scored BoM (hull + scored
    modules/subsystems), even when
    they don't affect the score — so the UI can surface "bundle includes …"."""
    if not _hull_present(spec, included, catalog):
        return None
    sim = contract_similarity(spec, included)
    if sim < SIMILARITY_THRESHOLD:
        return None

    # Extras = included items whose qty exceeds what the fit's scored BoM (hull +
    # scored modules/subsystems) needs. The hull's own count above 1 is an extra too.
    scored_need: dict[int, int] = dict(spec.module_multiset)
    scored_need[spec.hull_type_id] = scored_need.get(spec.hull_type_id, 0) + 1
    extras: list[int] = []
    for tid, have in included.items():
        if have > scored_need.get(tid, 0):
            extras.append(tid)
    extras.sort()

    if location_id is None:
        location_id = int(contract.get("start_location_id", 0) or 0)

    return ContractMatch(
        contract_id=int(contract.get("contract_id", 0) or 0),
        fit_id=fit.id,
        similarity=sim,
        price=float(contract.get("price", 0.0) or 0.0),
        title=str(contract.get("title", "") or ""),
        has_extras=bool(extras),
        extra_type_ids=extras,
        issuer_id=int(contract.get("issuer_id", 0) or 0),
        is_alliance=is_alliance,
        location_id=location_id,
        system_id=system_id,
    )


# ── Small helpers ────────────────────────────────────────────────────────────


def _age_seconds(iso_ts) -> float:
    """Seconds since an ISO-8601 timestamp; ``inf`` if unparseable/missing so a
    bad stamp is always treated as stale."""
    if not iso_ts or not isinstance(iso_ts, str):
        return float("inf")
    try:
        dt = datetime.fromisoformat(iso_ts)
    except ValueError:
        return float("inf")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds()


def _resolve_gap_role_filter(components):
    """Return a predicate ``role -> bool`` selecting which BoM roles the gap list
    includes.

    * ``None`` (default) → the FULL bill-of-materials: hull + modules +
      subsystems + charges + cargo + drones. The seeding export must cover
      modules, implants, boosters, ammo, drones, and anything else a doctrine's
      shopping-list fit carries in cargo — so the default is the whole BoM, not
      the flyable-only scored subset. (This is deliberately wider than the
      completable-fits / breadth scoring, which stays modules + subsystems + the
      hull gate — a fit is flyable without its cargo, but a seeding list is not
      complete without it.)
    * ``"full"`` → identical to the default (every role); kept as an explicit
      keyword for call sites that want to state the intent.
    * an iterable of role strings → exactly those roles (e.g. ``["hull"]`` or
      ``{"module", "subsystem"}`` to reproduce the old flyable-only export).
    * a callable → used directly.
    """
    if components is None or components == "full":
        return lambda role: True
    if callable(components):
        return components
    allowed = set(components)
    return lambda role: role in allowed
