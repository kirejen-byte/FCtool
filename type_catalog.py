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

from app_path import app_dir, bundle_dir

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
                cache = {}
        for type_id, entry in entries.items():
            cache[str(type_id)] = entry

        tmp_path = f"{self._cache_path}.tmp"
        parent = os.path.dirname(self._cache_path)
        if parent and not os.path.isdir(parent):
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError:
                return
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(cache, f, separators=(",", ":"))
                f.flush()
                try:
                    os.fsync(f.fileno())
                except (OSError, AttributeError):
                    pass
            os.replace(tmp_path, self._cache_path)
        except OSError:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
