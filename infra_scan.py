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

Freshness (2026-10-24): an already-typed structure is NOT resolved again on
every pass -- it gets a cheap ``{"structure_id": ..., "status": "alive"}`` ping
that bumps ``last_seen`` only -- but that skip is now gated on a ``RESOLVE_TTL_S``
(7 day) re-resolve TTL read from the record's ``last_resolved`` stamp, because a
ping carries no owner or name: before the TTL, ``owner_id`` and ``name`` froze
permanently after the FIRST resolve (780 of the owner's 784 alive records, one
of them a Fortizar still naming its former owner three months after the sale).
Gates stay exempt from the skip entirely (the phantom-bridge self-heal), and a
structure the store has NO type for is always resolved.

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
    RESOLVE_TTL_S = 7 * 24 * 3600   # re-resolve an already-typed structure after this (refreshes owner + name)
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
        # Undead-structure reaper bookkeeping (owner "SVM again" bug, 2026-07-23;
        # generalised past gates 2026-10-24).
        # scan_start = the aware-UTC instant this run began, floored to whole
        # seconds so it can never trail a same-second upsert "seen" bump.
        # scanned_ok = {system_id: system_name} for systems this run FULLY walked
        # with a SUCCESSFUL, NON-EMPTY search -- the only systems whose structure
        # roster is trusted as authoritative (see _reap_missing_structures).
        scan_start = datetime.now(timezone.utc).replace(microsecond=0)
        scanned_ok: dict[int, str] = {}

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

                typed, gate_ids, fresh = self._known_ids()
                rows: list[dict] = []
                system_cancelled = False
                for sid in ids:
                    if self._cancel.is_set():
                        cancelled = system_cancelled = True
                        break
                    if sid in fresh and sid not in gate_ids:
                        # Recently-resolved NON-gate: a light "seen" ping bumps
                        # last_seen without an ESI call and carries NO placement.
                        # It must NEVER restamp system_id/system_name from the
                        # search term (sys_id/sys_name) -- the system we happened to
                        # be scanning, not the structure's true home (the
                        # phantom-bridge root cause). GATES are deliberately excluded
                        # so they are re-resolved every scan and a mis-filed gate
                        # self-heals to its true solar_system_id (gates are few ->
                        # negligible budget). A structure whose last_resolved is
                        # older than RESOLVE_TTL_S is NOT in `fresh`, so it falls
                        # through to a full resolve -- a ping carries no owner_id
                        # or name, so without that TTL both froze forever.
                        rows.append({"structure_id": sid, "status": "alive"})
                        continue
                    if resolve_aborted:
                        # A TYPED structure keeps its record on abort (a "seen"
                        # bump); only a genuinely new id becomes a placeholder stub.
                        # Gated on `typed`, NEVER on `fresh`: a stale-but-known
                        # structure must not be stubbed (a stub restamps its system
                        # from the search term and downgrades type/category).
                        rows.append({"structure_id": sid, "status": "alive"}
                                    if sid in typed
                                    else self._stub(sid, sys_id, sys_name))
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
                        rows.append(self._resolved_row(sid, info, sys_id, sys_name,
                                                       self._system_name))
                        found += 1
                        streak_403 = 0
                    else:
                        # A TYPED structure that failed to re-resolve (e.g. a role-
                        # gated 403) must NOT be stubbed: a stub restamps its system
                        # from the search term and downgrades type/category. Keep the
                        # existing record with a "seen" bump; only a new id stubs.
                        # `typed`, NEVER `fresh` -- a structure past the TTL reaches
                        # this branch precisely BECAUSE it was due a re-resolve, and
                        # stubbing it would re-introduce the mis-file bug.
                        rows.append({"structure_id": sid, "status": "alive"}
                                    if sid in typed
                                    else self._stub(sid, sys_id, sys_name))
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
                # This system was fully walked; trust its gate roster for reaping
                # ONLY when the search actually SUCCEEDED and returned something --
                # an errored or empty result must never mass-reap on an ESI hiccup.
                if not search_errored and ids:
                    scanned_ok[sys_id] = sys_name

            # After the walk (still inside the batch so bump_miss/set_status fold
            # into the single flush): dead-mark structures a searched system should
            # have returned but did not -- they are gone from ESI (see below).
            reaped = self._reap_missing_structures(scanned_ok, scan_start)

        done = dict(report)
        done["cancelled"] = cancelled
        done["resolve_aborted"] = resolve_aborted
        done["reaped"] = reaped
        self._emit(self.on_done, done)

    def _reap_missing_structures(self, scanned_ok: dict, scan_start) -> int:
        """Mark ``status='dead'`` any stored ALIVE, non-reinforced structure -- of
        ANY category -- that a fully-searched system this run should have returned
        but did NOT, once it has been missed often enough for its category. The
        structure is gone from ESI (unanchored/destroyed/sold out of the search's
        reach). Leaving it 'alive' is not harmless: the store's contract is "scan
        absence NEVER deletes a record" and ``stale`` is only an OPTIONAL display
        filter, so an undead record renders as a normal live chip forever (296 of
        them on the owner's live store, 2026-10-24: 214 refinery, 49 citadel, 27
        engineering, 6 flex, the oldest last seen 2026-07-13). A dead GATE is worse
        still: it keeps emitting a ``gate_pairs`` pair, and because both endpoint
        systems may still host a DIFFERENT current gate, ``corroborated_gate_pairs``
        cannot tell the stale cross-pair from a real bridge -- a phantom bridge in
        the map AND in jump-range routing (owner "SVM again" bug, 2026-07-23).
        ``status='dead'`` is the existing hide-but-keep mechanism:
        ``_entry_survives`` drops it from chips/lists and ``gate_pairs`` skips it, so
        the phantom vanishes from map + routing at once; the record is PRESERVED
        (reversible -- a rebuilt structure flips back to 'alive' on its next
        sighting, which also zeroes its miss counter).

        TWO STRIKES for a non-gate, ONE for a gate (owner's decision):
          * ``store.bump_miss`` counts CONSECUTIVE qualifying misses (any upsert at
            all, the cheap "seen" ping included, resets it to 0), and a non-gate is
            dead-marked only on the SECOND one. A citadel/refinery missed once is
            far more likely to be an incomplete ESI search result than a destroyed
            structure, and a wrongly hidden chip is invisible until the owner
            notices it is gone.
          * A GATE still dies on the first miss. That behaviour is field-proven (it
            fixed the owner's phantom-bridge bug on 2026-07-23) and a phantom
            bridge corrupts ROUTING, not just a chip, so it must not linger an
            extra pass.

        Why absence is trustworthy here, and the guards that keep it so:
          * A live structure in system X is named ``"X - ..."`` (a gate ``"X >> Y"``,
            whose arrow glyph is immaterial), so ESI's fuzzy NAME ``/search/`` for
            X returns it -> its record's ``last_seen`` is bumped past
            ``scan_start``. A structure whose id has
            vanished from that authoritative search keeps its OLD ``last_seen`` --
            the miss signal.
          * Only ``scanned_ok`` systems (this run FULLY walked, search SUCCEEDED and
            was NON-EMPTY) are trusted -- a transient errored/empty search never
            mass-reaps, and never even counts a miss.
          * Extra guard: count a miss only when the scanned system's NAME is a
            substring of the stored name, i.e. the search that ran WOULD have
            returned this structure if it still existed. Protects a live structure
            whose name lost its system tag from ever being deaded on absence.
          * ``reinforced`` is the owner's explicit manual flag -> never auto-reaped.
          * Category ``npc`` is EXEMPT: an NPC station is indestructible, so its
            absence carries no information and a reap is always a false positive.
          * A record whose ``source`` is not ``esi_scan`` is EXEMPT: ESI never
            confirmed it (it was pasted or typed from in-game info), so absence
            proves nothing. Self-correcting -- a sighting rewrites the source.

        Returns the count NEWLY marked dead this run -- a first miss is not a reap,
        so the ``on_done`` payload's ``reaped`` keeps meaning "just hidden". No-op
        (and never calls ``store_key``/``bump_miss``/``set_status``) when nothing
        matches, so FakeStore-driven tests whose ``entries()`` is empty are
        untouched. Runs inside the caller's ``deferred_save()`` batch, so N misses
        still cost ONE whole-file write; the broad except keeps a reaper failure
        from ever killing a scan."""
        if not scanned_ok:
            return 0
        reaped = 0
        try:
            for e in self.store.entries():
                if e.get("status") != "alive":
                    continue
                if e.get("reinforced"):
                    continue
                if e.get("category") == "npc":
                    # An NPC station is INDESTRUCTIBLE: it cannot be destroyed,
                    # unanchored or sold, so its absence from the authed
                    # structure /search/ can never mean it is gone -- reaping one
                    # is always a false positive. ``categorize`` files any
                    # structure_id < 1e9 as "npc", so such a record CAN exist in
                    # the store (a clipboard import with NPC stations included
                    # is one route); this guard makes that safe whether or not
                    # the authed search ever returns one.
                    continue
                if e.get("source") != "esi_scan":
                    # Only a record ESI ITSELF discovered is reapable. A
                    # clipboard-pasted or manually added record (``infra_parser``
                    # writes source "clipboard"; the manual-add path "manual")
                    # was typed from in-game information and may never appear in
                    # the authed search at all, so its absence proves nothing --
                    # dead-marking it contradicts the store's "absence is not
                    # destruction" philosophy for unproven records. SELF-
                    # CORRECTING: the moment a scan sights such a record,
                    # ``_apply_incoming`` overwrites its source with "esi_scan",
                    # so it becomes reapable once ESI has actually confirmed it.
                    continue
                sys_name = scanned_ok.get(e.get("system_id"))
                if not sys_name:
                    continue
                if sys_name.casefold() not in (e.get("name") or "").casefold():
                    continue
                last = self._parse_seen(e.get("last_seen"))
                if last is not None and last >= scan_start:
                    continue                       # re-seen this run -> still alive
                key = self.store.store_key(e)
                n = self.store.bump_miss(key)
                limit = 1 if e.get("category") == "gate" else 2
                if n >= limit:
                    self.store.set_status(key, "dead")
                    reaped += 1
        except Exception:
            log.exception("[infra-scan] structure reaper failed")
        return reaped

    @staticmethod
    def _parse_seen(value):
        """Aware-UTC datetime from a store ISO ``last_seen``; None on missing/junk.
        Naive values are coerced to UTC (store house-rule is aware, but tolerate a
        hand-edited file) so the reaper comparison never raises."""
        try:
            dt = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

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

    def _known_ids(self):
        """``(typed_ids, gate_ids, fresh_ids)`` from ONE store pass.

        ``typed_ids``: structures the store already has a ``type_id`` for.
        ``gate_ids``: structures of category ``gate``.
        ``fresh_ids``: the subset of ``typed_ids`` whose ``last_resolved`` parses to
        an instant within ``RESOLVE_TTL_S`` of now. An empty, absent or unparsable
        stamp means NOT fresh (``""`` = never resolved, which is every record
        written before the field existed -- their one-time refresh is the point).

        The two gates the caller applies are DELIBERATELY different sets, and
        conflating them re-introduces a fixed bug:

        * ``fresh`` + not ``gate_ids`` drives the cheap-ping SKIP: no ESI call, no
          placement. Gates are exempt so a mis-filed gate re-resolves every scan
          and self-heals to its true ``solar_system_id`` (the phantom-bridge heal);
          they are few, so the budget cost is negligible.
        * ``typed`` alone drives the "a resolve that FAILED keeps the existing
          record instead of stubbing it" decision, in BOTH failure branches. If
          that decision were moved onto ``fresh``, a stale structure whose resolve
          403s (role-gated structures 403 by design) would be ``_stub``bed -- and a
          stub restamps ``system_id``/``system_name`` from the SEARCH TERM and
          downgrades type/category, i.e. exactly the phantom-bridge mis-file fixed
          in July (see ``tests/test_infra_scan_heal.py``).

        Timestamp parsing is the module's existing ``_within`` helper (tolerant:
        junk/None/naive all handled), so the TTL can never raise out of here; an
        unavailable store yields three empty sets."""
        typed, gates, fresh = set(), set(), set()
        now = datetime.now(timezone.utc)
        try:
            for e in self.store.entries():
                sid = e.get("structure_id")
                if sid is None:
                    continue
                if e.get("type_id") is not None:
                    typed.add(sid)
                    if self._within(e.get("last_resolved"), now, self.RESOLVE_TTL_S):
                        fresh.add(sid)
                if e.get("category") == "gate":
                    gates.add(sid)
        except Exception:
            log.debug("[infra-scan] store.entries() failed", exc_info=True)
        return typed, gates, fresh

    @staticmethod
    def _stub(sid, sys_id, sys_name):
        """A 403/unreachable structure: recorded with the system context from the
        queue (known even when resolve fails) so it still places on the map. With
        no type_id, categorize() reads only the id: "npc" for an NPC-range id,
        else "unknown" — never a wrong guess for a player structure."""
        return {"structure_id": sid, "type_id": None,
                "category": categorize(None, sid), "system_id": sys_id,
                "system_name": sys_name, "status": "unresolved"}

    def _system_name(self, system_id):
        """Local (no-ESI) solar-system id -> canonical name via the injected
        MapModel (``self.model.systems[id].name``), so a scanned structure is filed
        under its TRUE system. None for an id the model does not know (the caller
        then falls back to the search-term name)."""
        try:
            sysobj = self.model.systems.get(int(system_id))
        except (AttributeError, TypeError, ValueError):
            return None
        return getattr(sysobj, "name", None) if sysobj is not None else None

    @staticmethod
    def _resolved_row(sid, info, sys_id, sys_name, name_of=None):
        """Build a store row from a resolved ``/universe/structures`` payload.

        The structure is filed under its TRUE system -- the payload's
        ``solar_system_id`` -- NOT the search term ``sys_name`` (the system we
        happened to be scanning). ESI structure ``/search/`` is a fuzzy substring
        on the structure NAME, so scanning one system returns gates that physically
        live in OTHER systems; stamping ``system_name`` from the search term
        mis-filed those gates and drew phantom bridges (owner bug, 2026-07-22).
        ``name_of`` (the injected ``self._system_name``) resolves the true id ->
        canonical name locally (no ESI); it falls back to ``sys_id``/``sys_name``
        only when ``solar_system_id`` is absent or unknown to the local map model.

        The row carries ``resolved: True`` -- the store's explicit "this came from a
        FULL /universe/structures/ read" flag, which stamps ``last_resolved`` and so
        restarts the ``RESOLVE_TTL_S`` clock. It is the ONLY place in the scanner
        that sets it: neither ``_stub`` nor the cheap "seen" ping may, or the TTL
        would be suppressed and the owner/name freeze (bug A) would return."""
        pos = info.get("position")
        position = None
        if isinstance(pos, dict) and all(k in pos for k in ("x", "y", "z")):
            position = [pos["x"], pos["y"], pos["z"]]
        ssid = info.get("solar_system_id")
        true_sid = ssid if isinstance(ssid, int) else sys_id
        true_name = (name_of(true_sid) if (name_of is not None
                                           and isinstance(ssid, int)) else None)
        type_id = info.get("type_id")
        return {
            "structure_id": sid,
            "type_id": type_id,
            "category": categorize(type_id, sid),
            "name": info.get("name", ""),
            "system_id": true_sid,
            "system_name": true_name or sys_name,
            "owner_id": info.get("owner_id"),
            "position": position,
            "status": "alive",
            "resolved": True,
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
