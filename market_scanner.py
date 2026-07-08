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

import os
import statistics
import threading
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

    def get(self, type_id: int) -> TypeDepth | None:
        return self.depth.get(type_id)

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
        return MarketSnapshot(
            source_id=int(d.get("source_id", 0)),
            source_kind=str(d.get("source_kind", "")),
            region_id=int(d.get("region_id", 0)),
            system_id=d.get("system_id"),
            taken_at=str(d.get("taken_at", "")),
            depth=depth,
            etag=d.get("etag"),
            degraded=degraded,
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

    def contract_items(self, contract_id: int) -> list[dict]:
        ...

    def resolve_location_system(self, location_id: int) -> int | None:
        ...

    # Optional (corp/alliance contracts). The scanner duck-types their presence.
    def corp_contracts(self, corporation_id: int) -> list[dict]:
        ...

    def corp_contract_items(self, corporation_id: int, contract_id: int) -> list[dict]:
        ...


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

    # ── Market depth scan ─────────────────────────────────────────────────────

    def scan_market(self, source: MarketSource | None) -> MarketSnapshot:
        """Pull the staging market and return a depth snapshot (sell-side).

        ``None`` source (unconfigured) → an empty snapshot, no network. A
        structure source pulls the whole authed order book; a station source
        pulls region sell orders and filters to the station's ``location_id``.
        Adapter errors are surfaced as an empty snapshot (never raised) so a
        single bad pull degrades gracefully instead of crashing the worker.
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

        # Clear the adapter's per-pass forbidden record so a stale 403 from an
        # earlier scan can't leak into this snapshot (duck-typed: fake adapters
        # without ``last_forbidden`` are unaffected).
        fb = getattr(self._adapter, "last_forbidden", None)
        if isinstance(fb, set):
            fb.clear()

        try:
            if source.kind == "structure":
                raw = self._adapter.structure_orders(source.source_id)
            else:
                raw = self._adapter.region_orders(source.region_id, order_type="sell")
        except Exception:
            log.exception("[market] order pull failed for source %s", source)
            raw = []

        depth = self._build_depth(raw or [], source)
        snap = MarketSnapshot(
            source_id=source.source_id,
            source_kind=source.kind,
            region_id=source.region_id,
            system_id=source.system_id,
            taken_at=_now_iso(),
            depth=depth,
            degraded=self._degraded_reasons(source),
        )
        self._store_snapshot(snap)
        return snap

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
        self, orders: list[dict], source: MarketSource
    ) -> dict[int, TypeDepth]:
        """Aggregate raw ESI order dicts into per-type sell ladders.

        * Drops buy orders (``is_buy_order`` truthy) — sell-side only.
        * For the STATION path, keeps only orders at ``source.source_id``
          (``location_id`` filter) since the region pull returns the whole
          region. For the STRUCTURE path every order is already structure-local.
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
    ) -> ContractScan:
        """Scan public (+ optional corp) item-exchange contracts in ``region_id``
        for ones that match any of ``fits`` (same hull, >=95% over modules +
        subsystems; charges/drones/cargo excluded).

        System filter: when ``system_id`` is given, a contract is kept only if it
        resolves to that system — UNLESS ``region_wide`` is True (then unknown /
        other-system contracts are kept too). Contracts whose location can't be
        resolved are treated as unknown.

        Uses the persistent contract-items cache (ETag replay) so a re-scan only
        fetches new/changed contracts. Every adapter failure is swallowed per
        contract (a bad item pull just skips that contract) so a firehose region
        never crashes the pass.
        """
        matches: dict[str, list[ContractMatch]] = {f.id: [] for f in fits}
        scanned = 0
        from_cache = 0

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
            public = self._adapter.public_contracts(region_id) or []
        except Exception:
            log.exception("[market] public contracts pull failed for region %s", region_id)
            public = []

        contract_source: list[tuple[dict, bool]] = [(c, False) for c in public]

        # Optional corp/alliance contracts (duck-typed adapter method).
        if corp_id and hasattr(self._adapter, "corp_contracts"):
            try:
                corp = self._adapter.corp_contracts(corp_id) or []
                contract_source.extend((c, True) for c in corp)
            except Exception:
                log.exception("[market] corp contracts pull failed for corp %s", corp_id)

        for contract, is_alliance in contract_source:
            if not isinstance(contract, dict):
                continue
            if str(contract.get("type", "")) != "item_exchange":
                continue
            cid = contract.get("contract_id")
            if cid is None:
                continue

            loc_id = contract.get("start_location_id", 0) or 0
            # Only resolve the contract's system when it is actually needed: a
            # system filter is active (system_id given). With no filter the
            # match is region-wide and csys is never used for filtering, and the
            # stamped value would be informational only — skip the per-contract
            # location resolve entirely (it's a network/cache hit each time).
            if system_id is not None:
                csys = self._resolve_system_cached(loc_id)
                if not region_wide:
                    # Strict system filter: drop known-other-system contracts.
                    if csys is not None and csys != system_id:
                        continue
                    # Unknown-location contracts are dropped in strict mode too
                    # (they can't be confirmed in-system).
                    if csys is None:
                        continue
            else:
                csys = None

            items, was_cached = self._fetch_contract_items(cid, is_alliance, corp_id)
            scanned += 1
            if was_cached:
                from_cache += 1
            if not items:
                continue

            included = _included_for_match(items, self._catalog)

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
                    matches[fit.id].append(m)

        for fid in matches:
            matches[fid].sort(key=lambda mm: mm.price)

        return ContractScan(
            region_id=region_id,
            taken_at=_now_iso(),
            matches=matches,
            scanned_contracts=scanned,
            from_cache=from_cache,
        )

    def _fetch_contract_items(
        self, contract_id, is_alliance: bool, corp_id: int | None
    ) -> tuple[list[dict], bool]:
        """Return (items, served_from_cache) for a contract.

        Reads the persistent contract-items cache first; a cache hit within the
        1h TTL replays from disk (no network). Otherwise fetches via the adapter
        (public route, or the corp route for an alliance contract) and stores the
        result. Any adapter error yields ``([], False)`` — the caller skips it.
        """
        cached = self._read_contract_items_cache(int(contract_id))
        if cached is not None:
            return cached, True

        try:
            if is_alliance and corp_id and hasattr(self._adapter, "corp_contract_items"):
                items = self._adapter.corp_contract_items(corp_id, int(contract_id))
            else:
                items = self._adapter.contract_items(int(contract_id))
        except Exception:
            log.exception("[market] contract items pull failed for %s", contract_id)
            return [], False

        items = items or []
        self._write_contract_items_cache(int(contract_id), items)
        return items, False

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
    ) -> GapList:
        """Build a shopping list to seed ``target_fits`` of every fit in the
        doctrine, using the local market as on-hand stock.

        For each fit, ``needed`` per type = ``per_fit_qty * target_fits`` summed
        across fits; ``available`` = current market sell qty; ``short`` =
        ``max(0, needed - available)``. Only short items appear.

        By owner decision #4 the gap builder accepts whichever component subset
        it is handed: the ``components`` predicate/iterable selects roles to
        include. Default (``None``) = the scored set — hull + modules +
        subsystems (charges/drones/cargo excluded), matching the completable-fits
        math. Pass ``components="full"`` to include the whole BoM
        (charges/drones too).
        """
        # needed[type_id] -> (name, total needed units)
        needed: dict[int, list] = {}  # type_id -> [name, needed]
        role_filter = _resolve_gap_role_filter(components)

        for member in getattr(doctrine, "members", []):
            fit = store.get_fit(member.fit_id) if store is not None else None
            if fit is None:
                continue
            for c in fit_bom(fit.parsed, self._catalog):
                if not role_filter(c.role):
                    continue
                units = c.per_fit_qty * max(0, target_fits)
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

        target_desc = f"{target_fits}x {getattr(doctrine, 'name', '') or 'doctrine'}"
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
        """Return cached items for a contract if present and within the 1h TTL,
        else None. Uses ``fetched_at`` age against ``CACHE_TTL_CONTRACT_ITEMS``."""
        data = self._read_cache()
        entry = (data.get("contract_items") or {}).get(str(contract_id))
        if not isinstance(entry, dict):
            return None
        fetched_at = entry.get("fetched_at")
        if _age_seconds(fetched_at) > CACHE_TTL_CONTRACT_ITEMS:
            return None
        items = entry.get("items")
        return items if isinstance(items, list) else None

    def _write_contract_items_cache(
        self, contract_id: int, items: list[dict], etag: str | None = None
    ) -> None:
        def _mut(data: dict) -> None:
            data.setdefault("contract_items", {})[str(contract_id)] = {
                "items": items,
                "etag": etag,
                "fetched_at": _now_iso(),
            }

        self._write_cache(_mut)

    def _read_location_cache(self, location_id: int) -> int | None:
        data = self._read_cache()
        val = (data.get("locations") or {}).get(str(location_id))
        return int(val) if isinstance(val, int) else None

    def _write_location_cache(self, location_id: int, system_id: int) -> None:
        def _mut(data: dict) -> None:
            data.setdefault("locations", {})[str(location_id)] = int(system_id)

        self._write_cache(_mut)


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
    agree on the fit's contents. (Unlike ``to_dna``, module identity here is
    ``type_id`` only — online/offline copies of the same module are one BoM line,
    because for buying purposes they are the same item.)

    Cargo note: this intentionally DIVERGES from ``to_dna``'s cargo-safety drop.
    ``to_dna`` omits/limits cargo (a DNA link shouldn't force-load a hold); the
    BoM keeps every cargo + loaded-charge line (role ``"cargo"``/``"charge"``) so
    a full component table is available, but the two scored numbers
    (``completable_fits`` / >=95% similarity) and the default gap list exclude
    cargo/charges (see ``_SCORING_MODULE_ROLES``), so keeping them here is
    harmless to seeding math while still surfacing them for display.
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

    * ``None`` (default) → hull + modules + subsystems (the scored subset;
      charges/drones/cargo excluded).
    * ``"full"`` → every role (hull, module, subsystem, charge, cargo, drone).
    * an iterable of role strings → exactly those roles.
    * a callable → used directly.
    """
    if components is None:
        allowed = {"hull"} | set(_SCORING_MODULE_ROLES)
        return lambda role: role in allowed
    if components == "full":
        return lambda role: True
    if callable(components):
        return components
    allowed = set(components)
    return lambda role: role in allowed
