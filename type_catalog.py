"""
TypeCatalog — resolve EVE type IDs to names, categories, and fitting slots.

Loads a bundled, SDE-derived ``fit_types.json`` (typeID -> name/category/slot)
for fully offline resolution, and falls back to an injected ESI id->name
adapter for unknown / newly-released type IDs. ESI-resolved entries are cached
to ``fit_types_cache.json`` in ``app_dir()`` (atomic temp+replace, mirroring
``esi_cache.json``) so subsequent lookups — even in a fresh process — never hit
the network again.

Pure logic, Tk-free. Network access only ever happens through the injected
``esi`` adapter, never directly from this module.

Bundled schema: ``{"<type_id>": {"n": name, "c": category_id, "g": group_id,
"s": slot_or_null}}``. The cache file uses the same shape so cached entries are
indistinguishable from bundled ones at read time.

The injected ``esi`` adapter must expose
``resolve_names(type_ids: list[int]) -> {type_id: {"name": str, "category": str}}``.
Slot/category for an ESI-only entry stay ``None`` / ``"other"`` — the bundled SDE
is the source of truth for slot data.
"""

from __future__ import annotations

import json
import os

from app_io import atomic_write_json
from app_log import get_logger
from app_path import app_dir, bundle_dir

log = get_logger(__name__)

# Numeric SDE categoryID -> our string category. categoryID 87 here is the
# Fighter inventory category (a different namespace from the SDE flagID 87 =
# DroneBay); do not conflate the two.
CATEGORY_NAMES: dict[int, str] = {
    6: "ship",
    7: "module",
    8: "charge",
    18: "drone",
    87: "fighter",
    32: "subsystem",
}

SHIP_CATEGORY_ID = 6

# SDE ship groupID -> display name, for every ship group present in the bundled
# fit_types.json. The names are the authoritative ESI /universe/groups/{id}/
# names, so a rule value chosen from ship_group_names() matches exactly what
# ship_classes.get_group_name() (same ESI endpoint) reports for a live hull.
# Used to populate the overlay Label-rules "ship_group" value autocomplete
# (which otherwise had no source of group names — the bundle carries group ids
# but no group names). "Shuttle" (group 31) is included so shuttles are
# reachable by group as well as by type.
SHIP_GROUP_NAMES: dict[int, str] = {
    25: "Frigate",
    26: "Cruiser",
    27: "Battleship",
    28: "Hauler",
    29: "Capsule",
    30: "Titan",
    31: "Shuttle",
    237: "Corvette",
    324: "Assault Frigate",
    358: "Heavy Assault Cruiser",
    380: "Deep Space Transport",
    419: "Combat Battlecruiser",
    420: "Destroyer",
    463: "Mining Barge",
    485: "Dreadnought",
    513: "Freighter",
    540: "Command Ship",
    541: "Interdictor",
    543: "Exhumer",
    547: "Carrier",
    659: "Supercarrier",
    830: "Covert Ops",
    831: "Interceptor",
    832: "Logistics",
    833: "Force Recon Ship",
    834: "Stealth Bomber",
    883: "Capital Industrial Ship",
    893: "Electronic Attack Ship",
    894: "Heavy Interdiction Cruiser",
    898: "Black Ops",
    900: "Marauder",
    902: "Jump Freighter",
    906: "Combat Recon Ship",
    941: "Industrial Command Ship",
    963: "Strategic Cruiser",
    1022: "Prototype Exploration Ship",
    1201: "Attack Battlecruiser",
    1202: "Blockade Runner",
    1283: "Expedition Frigate",
    1305: "Tactical Destroyer",
    1527: "Logistics Frigate",
    1534: "Command Destroyer",
    1538: "Force Auxiliary",
    1972: "Flag Cruiser",
    4594: "Lancer Dreadnought",
    4902: "Expedition Command Ship",
    5087: "Special Edition Yachts",
    5120: "Command Carrier",
}


class TypeCatalog:
    """Resolve type IDs to names/categories/slots from bundled SDE + ESI fallback."""

    def __init__(
        self,
        bundled_path: str | None = None,
        cache_path: str | None = None,
        esi=None,
    ) -> None:
        if bundled_path is None:
            bundled_path = os.path.join(bundle_dir(), "fit_types.json")
        if cache_path is None:
            cache_path = os.path.join(app_dir(), "fit_types_cache.json")
        self._bundled_path = bundled_path
        self._cache_path = cache_path
        self._esi = esi

        self._by_id: dict[int, dict] = {}
        self._by_name: dict[str, int] = {}

        self._load_bundled()
        self._load_cache()

    # ── Loading ──────────────────────────────────────────────────────────────

    def _load_bundled(self) -> None:
        """Load the bundled SDE table. A missing/corrupt file is non-fatal:
        the catalog then relies entirely on the ESI fallback."""
        self._ingest_file(self._bundled_path)

    def _load_cache(self) -> None:
        """Load previously ESI-resolved entries so they survive process
        restarts without another network call."""
        self._ingest_file(self._cache_path)

    def _ingest_file(self, path: str) -> None:
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            log.warning("Discarding unreadable/corrupt type catalog file: %s", path)
            return
        if not isinstance(data, dict):
            return
        for raw_id, entry in data.items():
            try:
                tid = int(raw_id)
            except (TypeError, ValueError):
                continue
            if not isinstance(entry, dict):
                continue
            self._register(tid, entry)

    def _register(self, type_id: int, entry: dict) -> None:
        self._by_id[type_id] = entry
        name = entry.get("n")
        if isinstance(name, str) and name:
            self._by_name[name.strip().lower()] = type_id

    # ── Lookups ──────────────────────────────────────────────────────────────

    def resolve_name(self, type_id: int) -> str | None:
        """Return the display name for ``type_id``, or ``None`` if unresolvable."""
        entry = self._by_id.get(type_id)
        if entry is None:
            entry = self._resolve_unknown(type_id)
        if entry is None:
            return None
        name = entry.get("n")
        return name if isinstance(name, str) else None

    def resolve_id(self, name: str) -> int | None:
        """Return the type ID for a name (case-insensitive), or ``None``."""
        if not name:
            return None
        return self._by_name.get(name.strip().lower())

    def search_prefix(self, prefix: str, limit: int = 20) -> list[str]:
        """Display names whose lowercased name starts with `prefix` (case-
        insensitive), sorted, capped at `limit`. Prefixes shorter than 2 chars
        return [] so a keystroke never dumps the whole catalog."""
        p = (prefix or "").strip().lower()
        if len(p) < 2:
            return []
        out: list[str] = []
        for low, tid in self._by_name.items():
            if low.startswith(p):
                entry = self._by_id.get(tid)
                name = entry.get("n") if isinstance(entry, dict) else None
                if isinstance(name, str) and name:
                    out.append(name)
        out.sort()
        return out[:limit]

    def ship_type_names(self, prefix: str = "", limit: int = 1000) -> list[str]:
        """Ship (categoryID 6) type names only, sorted, capped at ``limit``.

        With no prefix, returns the whole ship roster (used to seed the overlay
        rule "ship_type" value autocomplete, which filters client-side as the
        user types). With a prefix, returns ship names whose lowercased name
        contains it (substring, so "shuttle" finds "Amarr Shuttle"). Modules,
        charges, drones etc. are excluded — this is the source that guarantees
        shuttles are reachable under ship_type.
        """
        p = (prefix or "").strip().lower()
        out: list[str] = []
        for tid, entry in self._by_id.items():
            if not isinstance(entry, dict) or entry.get("c") != SHIP_CATEGORY_ID:
                continue
            name = entry.get("n")
            if not isinstance(name, str) or not name:
                continue
            if p and p not in name.lower():
                continue
            out.append(name)
        out.sort()
        return out[:limit]

    def ship_group_names(self) -> list[str]:
        """Distinct ship group NAMES for the overlay rule "ship_group" value
        autocomplete, sorted.

        Catalog-aligned: only groups that actually appear among the bundled
        ship types are offered (so the list tracks the shipped SDE), resolved
        to authoritative ESI names via SHIP_GROUP_NAMES. Any ship group id
        missing from the map is skipped rather than shown as a bare number. If
        no ship types are loaded at all (corrupt/missing bundle), falls back to
        the full SHIP_GROUP_NAMES value set so the field is never empty.
        """
        present: set[int] = set()
        for entry in self._by_id.values():
            if isinstance(entry, dict) and entry.get("c") == SHIP_CATEGORY_ID:
                gid = entry.get("g")
                if isinstance(gid, int):
                    present.add(gid)
        if present:
            names = {SHIP_GROUP_NAMES[g] for g in present if g in SHIP_GROUP_NAMES}
        else:
            names = set(SHIP_GROUP_NAMES.values())
        return sorted(names)

    def category_of(self, type_id: int) -> str | None:
        """Return one of ship/module/charge/drone/fighter/subsystem/other, or
        ``None`` if the type cannot be resolved at all."""
        entry = self._by_id.get(type_id)
        if entry is None:
            entry = self._resolve_unknown(type_id)
        if entry is None:
            return None
        return CATEGORY_NAMES.get(entry.get("c"), "other")

    def group_of(self, type_id: int) -> int | None:
        """Return the SDE groupID for ``type_id``, or ``None`` if unknown.

        Used to order Tech III subsystems by subsystem slot when serializing a
        fit to DNA/ESI. ESI-only entries carry no group, so they return ``None``.
        """
        entry = self._by_id.get(type_id)
        if entry is None:
            entry = self._resolve_unknown(type_id)
        if entry is None:
            return None
        group = entry.get("g")
        return group if isinstance(group, int) else None

    def slot_of(self, type_id: int) -> str | None:
        """Return the fitting slot (high/med/low/rig/subsystem/service) or
        ``None`` — for non-modules and for ESI-only entries with no slot data."""
        entry = self._by_id.get(type_id)
        if entry is None:
            entry = self._resolve_unknown(type_id)
        if entry is None:
            return None
        slot = entry.get("s")
        return slot if isinstance(slot, str) else None

    # ── Batch priming ────────────────────────────────────────────────────────

    def prime(self, type_ids) -> None:
        """Resolve every currently-unknown id in ``type_ids`` in ONE ESI batch.

        Deduplicates the input, keeps only ids not already known (neither in the
        bundled table nor the cache), and — if any remain and an ESI adapter is
        set — resolves them all with a single ``esi.resolve_names`` call (the
        adapter batches via POST /universe/names/). Each resolved entry is
        registered in-memory and persisted to the same ``fit_types_cache.json``
        that ``resolve_name``/``category_of`` read from, in a single disk write.

        Fully defensive: any failure (bad input, ESI error, malformed response,
        disk error) is swallowed — priming is an optimization, never a hard
        dependency, so this method never raises. After ``prime(ids)``,
        ``resolve_name(id)`` for those ids returns the primed name without a
        further ESI call."""
        if self._esi is None:
            return
        try:
            unknown: list[int] = []
            seen: set[int] = set()
            for raw in type_ids or ():
                try:
                    tid = int(raw)
                except (TypeError, ValueError):
                    continue
                if tid in seen:
                    continue
                seen.add(tid)
                if tid not in self._by_id:
                    unknown.append(tid)
            if not unknown:
                return
            try:
                resolved = self._esi.resolve_names(unknown)
            except Exception:
                return
            if not isinstance(resolved, dict):
                return
            new_entries: dict[int, dict] = {}
            for tid in unknown:
                info = resolved.get(tid)
                if not isinstance(info, dict):
                    continue
                name = info.get("name")
                if not isinstance(name, str) or not name:
                    continue
                entry = {"n": name, "c": None, "g": None, "s": None}
                self._register(tid, entry)
                new_entries[tid] = entry
            if new_entries:
                self._write_cache_entries(new_entries)
        except Exception:
            # Priming is best-effort; never let it propagate.
            return

    # ── ESI fallback + cache ─────────────────────────────────────────────────

    def _resolve_unknown(self, type_id: int) -> dict | None:
        """Resolve a type ID missing from bundled+cache data via the injected
        ESI adapter, persist it to the cache, and return the new entry.

        Returns ``None`` if there is no adapter or it can't resolve the id.
        Slot stays ``None`` and category falls back to ``"other"`` because the
        public ``/universe/names/`` resolver carries no slot data and only a
        coarse ESI category."""
        if self._esi is None:
            return None
        try:
            resolved = self._esi.resolve_names([type_id])
        except Exception:
            return None
        if not isinstance(resolved, dict):
            return None
        info = resolved.get(type_id)
        if info is None:
            return None
        name = info.get("name")
        if not isinstance(name, str) or not name:
            return None
        entry = {"n": name, "c": None, "g": None, "s": None}
        self._register(type_id, entry)
        self._write_cache_entry(type_id, entry)
        return entry

    def _write_cache_entry(self, type_id: int, entry: dict) -> None:
        """Merge one resolved entry into the on-disk cache (atomic temp+replace)."""
        self._write_cache_entries({type_id: entry})

    def _write_cache_entries(self, entries: dict[int, dict]) -> None:
        """Merge one or more resolved entries into the on-disk cache in a SINGLE
        atomic temp+replace write.

        Re-reads the current cache so concurrent catalogs don't clobber each
        other's additions, then writes the union back."""
        if not entries:
            return
        cache: dict[str, dict] = {}
        if os.path.exists(self._cache_path):
            try:
                with open(self._cache_path, encoding="utf-8") as f:
                    existing = json.load(f)
                if isinstance(existing, dict):
                    cache = existing
            except (OSError, ValueError):
                log.warning(
                    "Discarding unreadable/corrupt type catalog cache: %s",
                    self._cache_path,
                )
                cache = {}
        for type_id, entry in entries.items():
            cache[str(type_id)] = entry

        parent = os.path.dirname(self._cache_path)
        if parent and not os.path.isdir(parent):
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError:
                return
        try:
            atomic_write_json(self._cache_path, cache, indent=None)
        except Exception:
            log.exception(
                "Failed to write type catalog cache: %s", self._cache_path
            )
