"""Persistent friendly-infrastructure store (citadels, gates, refineries…).

`InfraStore` owns ``infrastructure.json`` in ``app_path.app_dir()``: a keyed map
of structure records fed by clipboard paste (`infra_parser`) and rate-gated ESI
region scans (`infra_scan`), plus the configured scan-region list and per-system
scan timestamps. Writes are atomic (`app_io.atomic_write_json`).

Records are keyed by ``str(structure_id)`` when an id is known, else by a
synthetic ``name::{system_name}::{name}`` key. When an id-bearing sighting later
matches a name-keyed record (same ``system_name`` + ``name``, case-folded) the
store *upgrades*: it merges the two into the id-keyed record and drops the name
key. Scan absence NEVER deletes a record — ESI structure search is incomplete by
design, so disappearance is not destruction; ``stale`` is a read-time display
derivation, not stored state.

The store resolves ``system_id``/``region_id`` (and an Ansiblex's
``gate_to_system_id``) from an injected ``system_lookup`` (casefolded
system_name -> (system_id, region_id)); with no lookup those stay ``None``.

Thread-safe (`threading.RLock`); every mutator persists — unless the calling
thread has an open `deferred_save()` batch (see below), which collapses a
multi-mutation run (e.g. a region scan's per-system loop) into one on-disk
flush instead of one whole-file rewrite per mutator call. No Tkinter, no
network.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

import app_io
import app_path

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# The canonical field set of a store record (§3.1). Blank records are built from
# these defaults and incoming dicts overlay onto them.
_ENTRY_DEFAULTS: dict = {
    "structure_id": None,
    "type_id": None,
    "category": "unknown",
    "name": "",
    "system_id": None,
    "system_name": "",
    "region_id": None,
    "gate_to_system_name": None,
    "gate_to_system_id": None,
    "owner_id": None,
    "position": None,
    "source": "manual",
    "first_seen": "",
    "last_seen": "",
    "status": "alive",
    "notes": "",
    # Manual "reinforced/offline" flag (separate from status): a reinforced
    # Ansiblex STILL exists in space (stays a chip / in the by-system breakdown,
    # unlike a "dead" record which hides entirely) but its bridge line + jump-
    # range hop are suppressed while flagged. Absent in older files -> False.
    "reinforced": False,
}


def _as_int(value):
    """Coerce to int, or None. Bools are rejected (bool is an int subclass)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class InfraStore:
    """In-memory structure database backed by an atomic JSON file.

    All public methods are thread-safe; every mutator writes the file.
    """

    STALE_DAYS = 14                             # derived "stale" display threshold

    def __init__(self, path: str | None = None,
                 system_lookup: dict[str, tuple[int, int]] | None = None):
        """path defaults to ``os.path.join(app_path.app_dir(), 'infrastructure.json')``.

        system_lookup: casefolded system_name -> (system_id, region_id); injected
        by the composer (T7) from map_data; None => ids stay None (tests).
        """
        if path is None:
            path = os.path.join(app_path.app_dir(), "infrastructure.json")
        self.path = path
        self._lookup: dict[str, tuple[int, int]] = {}
        for key, val in (system_lookup or {}).items():
            try:
                self._lookup[str(key).casefold()] = (val[0], val[1])
            except (TypeError, IndexError, KeyError):
                continue
        self._lock = threading.RLock()
        self._entries: dict[str, dict] = {}
        self._regions: list[int] = []
        self._scan_state: dict = {"regions": {}, "systems": {}}
        # Per-thread deferred-save bookkeeping (see deferred_save()). Thread-
        # local so a batch opened on one thread (the scan worker) never
        # suppresses a mutator call made from a DIFFERENT thread (e.g. a
        # dialog edit on the Tk thread) — those keep saving immediately.
        self._defer_local = threading.local()

    # ── Timestamp / key helpers ───────────────────────────────────────────────

    @staticmethod
    def _now_iso() -> str:
        """Aware-UTC ISO timestamp (house rule: never naive)."""
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _key_for(structure_id, system_name: str, name: str) -> str:
        if structure_id is not None:
            return str(structure_id)
        return f"name::{system_name}::{name}"

    @staticmethod
    def store_key(entry: dict) -> str:
        """The canonical store key for an entry dict (§3.1). Deterministic, so
        callers holding a copy from :meth:`entries` can address it for
        remove/set_status/set_notes without the store exposing internal keys."""
        return InfraStore._key_for(
            entry.get("structure_id"),
            entry.get("system_name") or "",
            entry.get("name") or "",
        )

    # ── Persistence ───────────────────────────────────────────────────────────

    def load(self) -> None:
        """Load from disk. A missing OR corrupt file yields an empty store
        (logged) — never raises."""
        with self._lock:
            self._entries = {}
            self._regions = []
            self._scan_state = {"regions": {}, "systems": {}}
            if not os.path.exists(self.path):
                return
            try:
                with open(self.path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError, UnicodeDecodeError):
                # ValueError covers json.JSONDecodeError.
                log.warning("Infrastructure store at %s is unreadable; "
                            "starting from an empty store.", self.path)
                return
            if not isinstance(data, dict):
                log.warning("Infrastructure store at %s has an unexpected shape; "
                            "starting from an empty store.", self.path)
                return
            raw_entries = data.get("entries")
            if isinstance(raw_entries, dict):
                for key, raw in raw_entries.items():
                    if isinstance(raw, dict):
                        self._entries[str(key)] = self._coerce_entry(raw)
            for region in (data.get("regions") or []):
                ri = _as_int(region)
                if ri is not None and ri not in self._regions:
                    self._regions.append(ri)
            ss = data.get("scan_state")
            if isinstance(ss, dict):
                self._scan_state = {
                    "regions": dict(ss.get("regions") or {}),
                    "systems": dict(ss.get("systems") or {}),
                }

    def _coerce_entry(self, raw: dict) -> dict:
        """Fill any missing canonical fields so downstream code never KeyErrors
        on a schema-drifted or hand-edited file."""
        entry = dict(_ENTRY_DEFAULTS)
        for field in entry:
            if field in raw:
                entry[field] = raw[field]
        entry["category"] = entry["category"] or "unknown"
        entry["name"] = entry["name"] or ""
        entry["system_name"] = entry["system_name"] or ""
        entry["source"] = entry["source"] or "manual"
        entry["status"] = entry["status"] or "alive"
        entry["notes"] = entry["notes"] or ""
        entry["reinforced"] = bool(entry["reinforced"])   # tolerate any JSON shape
        if not entry["first_seen"]:
            entry["first_seen"] = self._now_iso()
        if not entry["last_seen"]:
            entry["last_seen"] = entry["first_seen"]
        return entry

    def save(self) -> None:
        """Atomically persist the whole store. Held under the lock so concurrent
        mutators cannot race on ``atomic_write_json``'s shared temp file."""
        with self._lock:
            payload = {
                "version": SCHEMA_VERSION,
                "entries": {k: dict(v) for k, v in self._entries.items()},
                "regions": list(self._regions),
                "scan_state": {
                    "regions": dict(self._scan_state.get("regions", {})),
                    "systems": dict(self._scan_state.get("systems", {})),
                },
            }
            parent = os.path.dirname(self.path)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            try:
                app_io.atomic_write_json(self.path, payload, indent=2)
            except Exception:
                log.exception("Failed to save infrastructure store to %s", self.path)
                raise

    # ── Batch mode (deferred save) ───────────────────────────────────────────

    def _defer_state(self) -> dict:
        """This thread's deferred-save bookkeeping — ``{"depth": int, "dirty":
        bool}`` — created lazily on first use. Thread-local by design; see
        ``deferred_save``."""
        state = getattr(self._defer_local, "state", None)
        if state is None:
            state = {"depth": 0, "dirty": False}
            self._defer_local.state = state
        return state

    def _save_or_defer(self) -> None:
        """The single choke point every mutator calls instead of ``save()``
        directly. Persists immediately UNLESS *this thread* currently has a
        ``deferred_save()`` batch open, in which case the pending mutation is
        marked dirty and left for that batch's own exit-flush. A mutator call
        made from a different thread (no open batch on its own thread) is
        completely unaffected and still saves immediately."""
        state = self._defer_state()
        if state["depth"] > 0:
            state["dirty"] = True
            return
        self.save()

    @contextmanager
    def deferred_save(self):
        """Batch this thread's mutations into a single on-disk flush.

        WHY: a region scan calls ``upsert_many`` + ``mark_system_scanned``
        once per system, and each is a whole-file ``save()`` — two full
        ~300 KB rewrites per scanned system (perf finding C2). Wrapping a
        multi-system loop in ``with store.deferred_save():`` keeps every
        mutator's in-memory update immediate (``entries()``/``scan_state()``
        reflect each change exactly as before) but skips the mutator's own
        disk write; exactly ONE ``save()`` happens when the outermost
        ``with`` exits.

        Flushes on the happy path AND when an exception propagates out of
        the block, matching ``InfraScanner``'s own partial-failure handling
        (a cancelled or 403-aborted scan already keeps its partial merge
        report) — so a batch that dies partway through still persists
        everything it completed before that point. No-op flush when nothing
        was actually mutated (e.g. an empty queue — every system already
        within the rescan gate). Nestable: only the outermost level flushes.

        Thread-scoped, not global: the depth/dirty bookkeeping is
        thread-local, so this batch can only ever suppress saves triggered
        by calls made on the SAME thread that opened it. A concurrent
        mutator call from another thread (e.g. a dialog edit on the Tk
        thread while a background scan batches on its own worker thread)
        keeps saving immediately — no behavior change for non-scan callers.
        """
        state = self._defer_state()
        state["depth"] += 1
        try:
            yield self
        finally:
            state["depth"] -= 1
            if state["depth"] == 0:
                dirty = state["dirty"]
                state["dirty"] = False
                if dirty:
                    self.save()

    # ── Merge / upsert ────────────────────────────────────────────────────────

    def _normalize_incoming(self, raw: dict) -> dict:
        """Coerce a parsed/entry dict into resolved incoming values. Resolves
        system_id/region_id from system_name (and gate endpoint id from
        gate_to_system_name) via the injected lookup when not already supplied.
        ``status``/``notes`` stay None when the caller did not provide them, so
        the merge can distinguish "absent" from an explicit value."""
        gate_to = raw.get("gate_to_system_name")
        inc = {
            "structure_id": _as_int(raw.get("structure_id")),
            "type_id": _as_int(raw.get("type_id")),
            "name": str(raw.get("name") or ""),
            "category": str(raw.get("category") or "unknown"),
            "system_name": str(raw.get("system_name") or ""),
            "system_id": _as_int(raw.get("system_id")),
            "region_id": _as_int(raw.get("region_id")),
            "gate_to_system_name": str(gate_to) if gate_to else None,
            "gate_to_system_id": _as_int(raw.get("gate_to_system_id")),
            "owner_id": _as_int(raw.get("owner_id")),
            "position": raw.get("position"),
            "status": raw.get("status") or None,
            "notes": raw.get("notes") or None,
        }
        if inc["system_name"] and (inc["system_id"] is None or inc["region_id"] is None):
            hit = self._lookup.get(inc["system_name"].casefold())
            if hit:
                if inc["system_id"] is None:
                    inc["system_id"] = hit[0]
                if inc["region_id"] is None:
                    inc["region_id"] = hit[1]
        if inc["gate_to_system_name"] and inc["gate_to_system_id"] is None:
            hit = self._lookup.get(inc["gate_to_system_name"].casefold())
            if hit:
                inc["gate_to_system_id"] = hit[0]
        return inc

    @staticmethod
    def _set(entry: dict, field: str, value) -> bool:
        if entry.get(field) != value:
            entry[field] = value
            return True
        return False

    def _apply_incoming(self, entry: dict, inc: dict, source: str, now: str) -> bool:
        """Overlay MEANINGFUL incoming fields onto ``entry`` (None / "" / a
        downgrade-to-"unknown" category never wipe a good value), refresh source
        and status, and bump last_seen. Returns True if any persistent field
        (other than last_seen, which always bumps) changed."""
        changed = False
        if inc["type_id"] is not None:
            changed |= self._set(entry, "type_id", inc["type_id"])
        if inc["name"]:
            changed |= self._set(entry, "name", inc["name"])
        if inc["category"] and inc["category"] != "unknown":
            changed |= self._set(entry, "category", inc["category"])
        if inc["system_id"] is not None:
            changed |= self._set(entry, "system_id", inc["system_id"])
        if inc["system_name"]:
            changed |= self._set(entry, "system_name", inc["system_name"])
        if inc["region_id"] is not None:
            changed |= self._set(entry, "region_id", inc["region_id"])
        if inc["gate_to_system_name"]:
            changed |= self._set(entry, "gate_to_system_name", inc["gate_to_system_name"])
        if inc["gate_to_system_id"] is not None:
            changed |= self._set(entry, "gate_to_system_id", inc["gate_to_system_id"])
        if inc["owner_id"] is not None:
            changed |= self._set(entry, "owner_id", inc["owner_id"])
        if inc["position"] is not None:
            changed |= self._set(entry, "position", inc["position"])
        if inc["notes"]:
            changed |= self._set(entry, "notes", inc["notes"])
        changed |= self._set(entry, "source", source)
        changed |= self._set(entry, "status", inc["status"] or "alive")
        entry["last_seen"] = now
        return changed

    def _new_entry(self, inc: dict, source: str, now: str) -> dict:
        entry = dict(_ENTRY_DEFAULTS)
        entry["structure_id"] = inc["structure_id"]
        entry["first_seen"] = now
        entry["last_seen"] = now
        self._apply_incoming(entry, inc, source, now)
        return entry

    def _find_name_key(self, system_name: str, name: str) -> str | None:
        """The key of an existing name-keyed record matching (system_name, name)
        case-folded, or None. Used to upgrade a name entry when its id arrives."""
        want_sys = (system_name or "").casefold()
        want_name = (name or "").casefold()
        for key, entry in self._entries.items():
            if entry.get("structure_id") is None:
                if ((entry.get("system_name") or "").casefold() == want_sys
                        and (entry.get("name") or "").casefold() == want_name):
                    return key
        return None

    def upsert_many(self, parsed: list[dict], source: str) -> dict:
        """Merge a batch of parsed/entry dicts into the store under ``source``.

        Returns a MergeReport ``{"added", "updated", "upgraded", "unchanged"}``.
        Sets first_seen/last_seen (aware-UTC now); status defaults to "alive"
        unless the caller supplied one. An id-bearing row that matches an
        existing name-keyed record upgrades it (name key dropped)."""
        report = {"added": 0, "updated": 0, "upgraded": 0, "unchanged": 0}
        now = self._now_iso()
        with self._lock:
            for raw in (parsed or []):
                inc = self._normalize_incoming(raw)
                if inc["structure_id"] is not None:
                    id_key = str(inc["structure_id"])
                    if id_key in self._entries:
                        changed = self._apply_incoming(self._entries[id_key], inc, source, now)
                        report["updated" if changed else "unchanged"] += 1
                    else:
                        name_key = self._find_name_key(inc["system_name"], inc["name"])
                        if name_key is not None:
                            entry = self._entries.pop(name_key)
                            entry["structure_id"] = inc["structure_id"]
                            self._apply_incoming(entry, inc, source, now)
                            self._entries[id_key] = entry
                            report["upgraded"] += 1
                        else:
                            self._entries[id_key] = self._new_entry(inc, source, now)
                            report["added"] += 1
                else:
                    name_key = self._key_for(None, inc["system_name"], inc["name"])
                    if name_key in self._entries:
                        changed = self._apply_incoming(self._entries[name_key], inc, source, now)
                        report["updated" if changed else "unchanged"] += 1
                    else:
                        self._entries[name_key] = self._new_entry(inc, source, now)
                        report["added"] += 1
            self._save_or_defer()
        return report

    # ── Editing / removal ─────────────────────────────────────────────────────

    def remove(self, keys: list[str]) -> int:
        """Delete records by store key. Returns how many existed and were
        removed; persists only when something changed."""
        removed = 0
        with self._lock:
            for key in (keys or []):
                if key in self._entries:
                    del self._entries[key]
                    removed += 1
            if removed:
                self._save_or_defer()
        return removed

    def set_status(self, key: str, status: str) -> None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return
            entry["status"] = status
            self._save_or_defer()

    def set_notes(self, key: str, notes: str) -> None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return
            entry["notes"] = notes or ""
            self._save_or_defer()

    def set_reinforced(self, key: str, reinforced: bool) -> None:
        """Set the manual reinforced/offline flag on a record (atomic save).

        Mirrors ``set_status``/``set_notes``: RLock-guarded, persists via the
        deferred-save choke point, silent no-op for an unknown key. Independent
        of ``status`` — a record can be both ``dead`` and ``reinforced``, and
        clearing one never touches the other. Coerced to a real bool so the
        stored value is always ``True``/``False``."""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return
            entry["reinforced"] = bool(reinforced)
            self._save_or_defer()

    # ── Read views ────────────────────────────────────────────────────────────

    def entries(self) -> list[dict]:
        """Deep copies of every record, stably ordered by (system_name, name)."""
        with self._lock:
            out = [copy.deepcopy(entry) for entry in self._entries.values()]
        out.sort(key=lambda e: ((e.get("system_name") or ""), (e.get("name") or "")))
        return out

    def by_system(self) -> dict[int, list[dict]]:
        """Deep-copied records grouped by ``system_id``. Records with no resolved
        system (system_id None) are omitted."""
        result: dict[int, list[dict]] = {}
        with self._lock:
            for entry in self._entries.values():
                sid = entry.get("system_id")
                if sid is None:
                    continue
                result.setdefault(sid, []).append(copy.deepcopy(entry))
        for lst in result.values():
            lst.sort(key=lambda e: (e.get("name") or ""))
        return result

    # ── Scan-region configuration + per-system scan timestamps ────────────────

    def get_regions(self) -> list[int]:
        with self._lock:
            return list(self._regions)

    def set_regions(self, region_ids: list[int]) -> None:
        with self._lock:
            out: list[int] = []
            for region in (region_ids or []):
                ri = _as_int(region)
                if ri is not None and ri not in out:
                    out.append(ri)
            self._regions = out
            self._save_or_defer()

    def scan_state(self) -> dict:
        """A defensive deep copy of ``{"regions": {rid: iso}, "systems": {sid: iso}}``.
        Keys are strings (JSON-native), so callers look up by ``str(system_id)``."""
        with self._lock:
            return copy.deepcopy(self._scan_state)

    def mark_system_scanned(self, system_id: int, when_iso: str) -> None:
        with self._lock:
            self._scan_state.setdefault("systems", {})[str(system_id)] = when_iso
            self._save_or_defer()
