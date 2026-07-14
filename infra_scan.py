"""Rate-governed ESI region scanner for friendly-infrastructure discovery.

``InfraScanner`` walks the systems of one or more regions, runs the authenticated
structure ``/search/`` per system, and resolves the returned structure ids via
``/universe/structures/``. It is deliberately Tk-free: the ONE marshalling point
is the injected ``ui_post`` callable (``fc_gui._post_ui`` in production), through
which every ``on_progress``/``on_done`` invocation is routed. Everything else
runs on a single daemon worker thread.

Architecture (plan §2): may import ``esi_auth`` (owned this wave), ``map_data``
(read-only, for the ``MapModel`` shape) and ``rate_limiter`` — NEVER a sibling
``infra_*`` module and NEVER ``fc_gui``/``map_tab``. Dependencies
(``auth``/``store``/``model``/``ui_post``) are injected instances, duck-typed, so
the unit tests drive it with fakes.

Governor (plan §3.5): after EVERY ESI call it inspects the
``X-ESI-Error-Limit-Remain``/``-Reset`` headers that ``esi_get_ex`` surfaces; on
a 420 or a remaining budget below ``ERROR_FLOOR`` it sleeps out the reset window
before continuing, and it paces ``PACE_SECONDS`` between calls so a bulk scan can
never outrun the shared error budget. A run of ``MAX_403_STREAK`` consecutive
resolve 403s aborts the resolve phase for the rest of that scan (403s are the
dominant error-budget sink — role-gated gates 403 by design).
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from app_log import get_logger

log = get_logger(__name__)


def _now_iso() -> str:
    """Aware-UTC ISO timestamp (house rule: never naive)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Duplicated BY VALUE from the frozen §3.2 contract (single source: infra_parser.TYPE_CATEGORY); architecture rule forbids sibling imports — keep in lockstep.
TYPE_CATEGORY = {
    35832: "citadel", 35833: "citadel", 35834: "citadel",            # Astrahus, Fortizar, Keepstar
    47512: "citadel", 47513: "citadel", 47514: "citadel",            # faction Fortizars
    47515: "citadel", 47516: "citadel",
    35825: "engineering", 35826: "engineering", 35827: "engineering",# Raitaru, Azbel, Sotiyo
    35835: "refinery", 35836: "refinery", 81826: "refinery",         # Athanor, Tatara, Metenox
    35841: "gate",                                                   # Ansiblex
    35840: "flex", 37534: "flex",                                    # Pharolux, Tenebrex
}


def categorize(type_id: int | None, structure_id: int | None) -> str:
    if structure_id is not None and structure_id < 1_000_000_000:
        return "npc"                       # NPC stations: ids ~6.0e7 (fixture-proven)
    return TYPE_CATEGORY.get(type_id or 0, "unknown")


class InfraScanner:
    PACE_SECONDS = 0.35          # bulk pacing between ESI calls (~3/s; bucket is 15/s)
    ERROR_FLOOR = 20             # pause when X-ESI-Error-Limit-Remain < this
    MAX_403_STREAK = 30          # abort resolve phase for this scan after N straight 403s
    RESCAN_GATE_S = 3600         # skip systems searched within the last hour (server cache)
    MIN_QUERY_LEN = 3            # ESI /search/ rejects terms shorter than 3 chars

    def __init__(self, auth, store, model, ui_post,
                 on_progress=None, on_done=None):
        """auth: ESIAuth (primary character). store: InfraStore. model: map_data
        MapModel (``.systems`` is dict[int, MapSystem]; ``.id``/``.name``/
        ``.region_id`` on each). ui_post: fc_gui._post_ui — every callback goes
        through it. on_progress/on_done: optional dict-payload callbacks."""
        self.auth = auth
        self.store = store
        self.model = model
        self.ui_post = ui_post
        self.on_progress = on_progress
        self.on_done = on_done
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._running = False
        self._thread: threading.Thread | None = None
        self._sleep = time.sleep    # injectable in tests (no real pacing)

    # ── public API ────────────────────────────────────────────────────────────

    def scan_regions(self, region_ids) -> bool:
        """Start a background scan of ``region_ids``. Returns False when a scan is
        already running or no character is authenticated (plan §3.5)."""
        with self._lock:
            if self._running:
                return False
            if not getattr(self.auth, "is_authenticated", False):
                return False
            self._cancel.clear()
            self._running = True
        regions = list(region_ids or [])
        self._thread = threading.Thread(
            target=self._thread_body, args=(regions,),
            name="infra-scan", daemon=True)
        self._thread.start()
        return True

    def cancel(self) -> None:
        """Request the running scan stop at the next system/id boundary."""
        self._cancel.set()

    def is_running(self) -> bool:
        with self._lock:
            return self._running

    # ── worker ────────────────────────────────────────────────────────────────

    def _thread_body(self, region_ids) -> None:
        try:
            self._run_worker(region_ids)
        except Exception:
            log.exception("[infra-scan] worker crashed")
        finally:
            with self._lock:
                self._running = False

    def _run_worker(self, region_ids) -> None:
        """Synchronous scan body — the testable core. Builds the invisible queue,
        searches + resolves per system, governs the error budget, and emits one
        ``on_done`` at the end. Runs on the worker thread in production; the unit
        tests invoke it directly with ``_sleep`` stubbed."""
        queue = self._build_queue(region_ids)
        total = len(queue)
        report = {"added": 0, "updated": 0, "upgraded": 0, "unchanged": 0}
        cancelled = False
        resolve_aborted = False
        streak_403 = 0
        found = 0
        errors = 0

        # Batch every store write for this run into a single on-disk flush
        # (perf finding C2): upsert_many + mark_system_scanned each persist
        # the whole ~300 KB file, so uncached this loop was 2 whole-file
        # rewrites PER scanned system — hundreds on a big region. queue
        # already flattens every requested region into ONE flat pass (built
        # above by _build_queue, which has no per-region grouping/boundary),
        # so "once per scan run" is the boundary this loop actually has, not
        # "once per region". deferred_save() flushes in its own finally, so
        # an early break (cancel) or an unexpected raise out of this loop
        # still persists everything merged before that point — the same
        # partial-work-survives spirit as the cancel/403-abort handling
        # already below (only _thread_body wraps this in a try/except, so an
        # exception here propagates there after the flush).
        with self.store.deferred_save():
            for idx, (sys_id, sys_name) in enumerate(queue, start=1):
                if self._cancel.is_set():
                    cancelled = True
                    break

                self._emit(self.on_progress, {
                    "phase": "search", "done": idx, "total": total,
                    "system": sys_name, "found": found, "errors": errors})

                ids, search_errored = self._search(sys_name)
                if search_errored:
                    errors += 1

                known = self._known_type_ids()
                rows: list[dict] = []
                system_cancelled = False
                for sid in ids:
                    if self._cancel.is_set():
                        cancelled = system_cancelled = True
                        break
                    if sid in known:
                        # Already resolved elsewhere: a light "seen" row bumps
                        # last_seen without spending an ESI call or error budget.
                        rows.append({"structure_id": sid, "type_id": None,
                                     "category": categorize(None, sid),
                                     "system_id": sys_id, "system_name": sys_name,
                                     "status": "alive"})
                        continue
                    if resolve_aborted:
                        rows.append(self._stub(sid, sys_id, sys_name))
                        errors += 1
                        continue
                    self._emit(self.on_progress, {
                        "phase": "resolve", "done": idx, "total": total,
                        "system": sys_name, "found": found, "errors": errors})
                    info, status, headers = self.auth.esi_get_ex(
                        f"/universe/structures/{sid}/")
                    self._governor(status, headers)
                    self._sleep(self.PACE_SECONDS)
                    if isinstance(info, dict):
                        rows.append(self._resolved_row(sid, info, sys_id, sys_name))
                        found += 1
                        streak_403 = 0
                    else:
                        rows.append(self._stub(sid, sys_id, sys_name))
                        errors += 1
                        if status == 403:
                            streak_403 += 1
                            if streak_403 >= self.MAX_403_STREAK:
                                resolve_aborted = True
                        else:
                            streak_403 = 0

                if rows:
                    self._merge_report(report, self.store.upsert_many(rows, "esi_scan"))
                if system_cancelled:
                    break
                # Only mark fully-completed systems scanned so a cancel mid-system
                # re-scans next time rather than skipping via the rescan gate.
                self.store.mark_system_scanned(sys_id, _now_iso())

        done = dict(report)
        done["cancelled"] = cancelled
        done["resolve_aborted"] = resolve_aborted
        self._emit(self.on_done, done)

    # ── steps ─────────────────────────────────────────────────────────────────

    def _search(self, sys_name):
        """Run the authed structure search for one system. Returns
        ``(structure_ids, errored)``; governs + paces after the call. Path/params
        mirror ESIAuth.search_structures (esi_auth.py:1436)."""
        result, status, headers = self.auth.esi_get_ex(
            f"/characters/{self.auth.character_id}/search/",
            {"categories": "structure", "search": sys_name, "strict": "false"})
        self._governor(status, headers)
        self._sleep(self.PACE_SECONDS)
        ids: list[int] = []
        if isinstance(result, dict):
            raw = result.get("structure")
            if isinstance(raw, list):
                ids = [i for i in raw if isinstance(i, int)]
        return ids, not isinstance(result, dict)

    def _build_queue(self, region_ids):
        """FIFO of (system_id, system_name) for systems in ``region_ids``,
        dropping <3-char names and systems searched within ``RESCAN_GATE_S``
        (the invisible queue — plan §3.5)."""
        region_set = set(region_ids or [])
        state = self.store.scan_state() or {}
        sys_state = state.get("systems", {}) or {}
        now = datetime.now(timezone.utc)
        queue = []
        for s in self.model.systems.values():
            if getattr(s, "region_id", None) not in region_set:
                continue
            name = (getattr(s, "name", "") or "").strip()
            if len(name) < self.MIN_QUERY_LEN:
                continue
            last = sys_state.get(s.id, sys_state.get(str(s.id)))
            if last and self._within(last, now, self.RESCAN_GATE_S):
                continue
            queue.append((s.id, name))
        return queue

    def _known_type_ids(self):
        """Structure ids the store already has a type_id for — skip resolving
        these (their type never changes) to save error budget."""
        known = set()
        try:
            for e in self.store.entries():
                if e.get("type_id") is not None and e.get("structure_id") is not None:
                    known.add(e["structure_id"])
        except Exception:
            log.debug("[infra-scan] store.entries() failed", exc_info=True)
        return known

    @staticmethod
    def _stub(sid, sys_id, sys_name):
        """A 403/unreachable structure: recorded with the system context from the
        queue (known even when resolve fails) so it still places on the map. With
        no type_id, categorize() reads only the id: "npc" for an NPC-range id,
        else "unknown" — never a wrong guess for a player structure."""
        return {"structure_id": sid, "type_id": None,
                "category": categorize(None, sid), "system_id": sys_id,
                "system_name": sys_name, "status": "unresolved"}

    @staticmethod
    def _resolved_row(sid, info, sys_id, sys_name):
        pos = info.get("position")
        position = None
        if isinstance(pos, dict) and all(k in pos for k in ("x", "y", "z")):
            position = [pos["x"], pos["y"], pos["z"]]
        ssid = info.get("solar_system_id")
        type_id = info.get("type_id")
        return {
            "structure_id": sid,
            "type_id": type_id,
            "category": categorize(type_id, sid),
            "name": info.get("name", ""),
            "system_id": ssid if isinstance(ssid, int) else sys_id,
            "system_name": sys_name,
            "owner_id": info.get("owner_id"),
            "position": position,
            "status": "alive",
        }

    # ── governor + helpers ─────────────────────────────────────────────────────

    def _governor(self, status, headers):
        """Sleep out the ESI error-budget reset window on a 420 or a remaining
        budget below ``ERROR_FLOOR`` (plan §3.5)."""
        remain = self._header_int(headers, "X-ESI-Error-Limit-Remain")
        if status == 420 or (remain is not None and remain < self.ERROR_FLOOR):
            reset = self._header_int(headers, "X-ESI-Error-Limit-Reset")
            self._sleep((reset if reset is not None else 60) + 1)

    @staticmethod
    def _header_int(headers, key):
        """Case-insensitive int header read (production headers arrive with the
        server's own casing; tests use the canonical key). None on absent/junk."""
        if not headers:
            return None
        val = headers.get(key)
        if val is None:
            lk = key.lower()
            for k, v in headers.items():
                if isinstance(k, str) and k.lower() == lk:
                    val = v
                    break
        try:
            return int(val)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _within(last_iso, now, seconds):
        try:
            dt = datetime.fromisoformat(last_iso)
        except (TypeError, ValueError):
            return False
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (now - dt).total_seconds() < seconds

    @staticmethod
    def _merge_report(acc, rep):
        if not isinstance(rep, dict):
            return
        for k in ("added", "updated", "upgraded", "unchanged"):
            acc[k] = acc.get(k, 0) + int(rep.get(k, 0) or 0)

    def _emit(self, cb, payload):
        """Route a callback through ui_post (the one Tk marshalling point). A
        raising/absent callback never breaks the scan."""
        if cb is None:
            return
        try:
            self.ui_post(cb, payload)
        except Exception:
            log.debug("[infra-scan] callback failed", exc_info=True)
