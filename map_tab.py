"""Star-map Tk tab: canvas + bitmap base layer + worker-thread rendering.

Threading (house idiom): one daemon render worker; requests coalesce
latest-wins via a generation counter. The worker renders and puts finished
frames on a thread-safe queue; the main-thread tick loop drains it
(latest-wins) and applies frames. The gesture path (wheel zoom) shows
FrameCache quick-frames instantly; a crisp render lands ~settle_ms after
input stops.

This module never imports fc_gui — the host injects config accessors and
right-click callbacks. Only map_render touches pygame; only this file (and
fc_gui) touch tkinter.
"""
from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from collections import deque

import map_camera as mc
import map_data
import map_overlays as mo
import map_render as mr

ZOOM_STEP = 1.15
SETTLE_MS = 120.0
# 16 ms (~60 fps) drain cadence while the tab is VISIBLE: a finished worker frame
# is applied within a frame of completion instead of the ~50-60 ms tail the old
# 60 ms tick left (Task 18 Step 1a). The tick still no-ops when hidden -- see the
# `not self._visible` early-return in _tick and the visibility guard in
# _schedule_tick -- so a hidden tab costs nothing regardless of this cadence.
TICK_MS = 16
# Adaptive ~30 fps frame pacing (Task 24 / Phase G, P2). The 16 ms tick keeps
# advancing the ZoomAnimator/camera every frame (state.tick ~0.0 ms), but a
# worker GESTURE-FRAME request is only enqueued when the previous gesture apply
# has completed (self._gesture_inflight) AND at least this long has elapsed since
# the last request. Skip-when-behind emerges for free: while an apply is pending
# the ticks advance the camera silently and the next request samples the newer
# camera -- a loaded box gets clean ~30 fps instead of queued 60 fps jank
# (Task 23 diagnosis: fast-burst generated 576-732 ms/s of frame work, > the
# 1000 ms budget once worker+main are summed, so the pipeline collapsed).
GESTURE_MIN_INTERVAL_MS = 30.0
# (Task 24 EXP verdict: a cyclic-GC probe -- gc.disable() during a glide +
# gc.collect(0) on the settle-apply -- was measured against the end-of-glide crisp
# spike (ap_total ~200 ms) and REMOVED. It did not reduce the spike; the A/B put
# gc-disabled runs equal-or-worse (glide loop-lag p95 ~70-79 ms vs ~35 ms without;
# spike present in both). Disabling gc process-wide just batched the churn into a
# worse later collection. The residual intermittent settle-apply spike is a Tk/
# allocation hiccup, not cleanly GC-attributable, and predates Task 24.)
# Worker renders this many px beyond the viewport on every side (Task 17). Tk
# still shows a viewport-sized center crop, but the FULL margined frame is cached
# so pan / zoom-out serve real content instead of a black edge within +/-MARGIN.
MARGIN = 224
BG_HEX = "#0a0a14"

# --- Phase H adaptive conservation pacing (Task 26) --------------------------
# Task 25 convicted the apply RATE as the ONLY lever over the intermittent settle
# spike. The freeze is a periodic Windows working-set-growth stall: under the
# ~230k page-faults/s of per-frame PPM+PhotoImage churn a ~180-260 ms freeze recurs
# ~2.2 s apart, landing on whichever Tcl op is mid-flight (so it reads as an
# "overlay" or "photo" spike but is really a global main-thread stall). The harness
# A/B REJECTED every structural mitigation -- M1 two-PhotoImage ping-pong, M2 hover
# diet, M3 batched overlay delete all left the spike count unchanged (and M1's
# configure(data=) re-decode measured ~2x WORSE at 20 fps: p50 45 ms vs 23, 29 vs 25
# freezes >200 ms). Only throttling the gesture-apply rate moved it: 33->12 fps cut
# >200 ms freezes 25->7 (3.6x) and stretched the interspike gap 2.2s->6.0s. Rather
# than pay the 12 fps glide tax ALWAYS (the owner feels the smoother 33 fps glide on
# a healthy box), MapTab watches its OWN working set per apply and only drops to
# ~12 fps for a few seconds when a stall is actually happening -- an adaptive
# conservation window. A box that never stalls never sees it. GESTURE_MIN_INTERVAL_MS
# (above) is the healthy ~33 fps default.
CONSERVATION_INTERVAL_MS = 80.0     # gesture-apply floor while conserving (~12 fps)
CONSERVATION_HOLD_MS = 8000.0       # hold conservation this long past a stall (rolling)
# A frame apply is a STALL when it BOTH took longer than STALL_MS *and* grew this
# process's working set by more than STALL_WS_KB since the previous apply. The
# duration gate is the PRIMARY discriminator -- a healthy apply is ~20-35 ms, so a
# >100 ms apply is already abnormal; the ws gate then confirms it is specifically the
# working-set-growth (paging) kind and rejects the rare fast-but-alloc-heavy apply
# (a lone normal outlier grew 23 MB in <100 ms -- a lazy import, not a paging stall).
# Two measured stall regimes bracket the ws gate: healthy per-apply growth p95 was
# 20-24 KB in BOTH, while stall applies grew p50 204 KB (a mild session) to 876 KB (a
# harsh one). 150 KB sits ~6-7x above the healthy p95 (clean separation) yet below
# the stall p50 of either regime, so it catches the bulk of stalls whether the box is
# stalling hard or lightly -- where the Task-25-only 500 KB missed ~58% of the mild
# regime's stalls and armed 9 s late. Catching a fraction is enough: stalls cluster
# ~2 s apart and each arms an 8 s hold, so partial detection still holds conservation
# continuously through a bad patch.
STALL_MS = 100.0
STALL_WS_KB = 150
# Task 27: an always-on main-loop stall SENTINEL closes the gap that _m4_note_apply
# (which samples ONLY inside the two _apply_* paths) leaves open during a SUSTAINED
# pan. Measured (tools/spike sustained_pan): ~1/3-1/2 of the ~2 s-cadence working-set
# freezes land on _on_drag_move / overlay redraw -- OUTSIDE any apply -- so the apply-
# path detector never sees them and conservation never arms for the freeze. This is a
# self-rescheduling frame.after() heartbeat on the MAIN thread (started with the tick
# when shown, cancelled on hide): it measures its OWN scheduling delay, and when a beat
# lands late by more than STALL_MS *and* this process's working set grew past
# STALL_WS_KB over the beat it arms the SAME conservation window M4 does (idempotent --
# arming only extends _conserve_until_ms). One 10 Hz timer whose only per-beat cost is
# the ~1.4 us GetProcessMemoryInfo probe (negligible idle cost); wedge-proof (a probe
# failure -> _proc_mem None -> the beat never arms and keeps rescheduling).
STALL_SENTINEL_MS = 100.0


# --- process working-set / page-fault correlate (Task 25, telemetry only) ----
# The apply spike lands on whichever Tcl op is mid-flight (probe: overlay 180 ms,
# itemconfig 53 ms, PhotoImage only 32 ms) -> a global main-thread stall, not a slow
# stage. Page-fault-count and working-set deltas across an apply are the prime
# correlate for an OS/allocator cause (the pipeline churns ~210 MB/s of PPM+image
# bytes). GetProcessMemoryInfo is ~1.4 us/call (probe) so it is sampled every apply
# WHEN TELEMETRY IS ON. Lazily initialised on first use so the ctypes/WinDLL setup
# never runs on the default (telemetry-off) path. _PSAPI: None=uninit, False=
# unavailable, else the resolved (ctypes, GetProcessMemoryInfo, hProcess, struct).
_PSAPI = None


def _init_psapi():
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class _PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD),
                        ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        psapi = ctypes.WinDLL("psapi.dll")
        k32 = ctypes.WinDLL("kernel32.dll")
        gpmi = psapi.GetProcessMemoryInfo
        gpmi.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
        gpmi.restype = wintypes.BOOL
        return (ctypes, gpmi, k32.GetCurrentProcess(), _PMC)
    except Exception:
        return False


def _proc_mem() -> tuple:
    """(working_set_kb, page_fault_count) for THIS process, or (None, None) when
    unavailable. Called only from telemetry-on apply records (guarded)."""
    global _PSAPI
    if _PSAPI is None:
        _PSAPI = _init_psapi()
    if not _PSAPI:
        return (None, None)
    ctypes, gpmi, cur, _PMC = _PSAPI
    try:
        c = _PMC()
        c.cb = ctypes.sizeof(_PMC)
        if gpmi(cur, ctypes.byref(c), c.cb):
            return (int(c.WorkingSetSize) // 1024, int(c.PageFaultCount))
    except Exception:
        pass
    return (None, None)

# Default theme = the map tab's PRE-theming standalone palette, so a MapTab built
# without a theme (standalone / tests) looks exactly as before. fc_gui injects its
# app constants (BG_DARK/BG_PANEL/BG_ENTRY/FG_TEXT/FG_ACCENT/BORDER_COLOR) via
# _build_map_tab so the context menus + toolbar match the rest of the application
# (owner request 2026-07-10). Keys: bg (frame/toolbar/canvas backdrop), panel
# (menu bg + checkbutton selectcolor), entry_bg (search field + menu selection
# bar), fg (toolbar + menu text), accent (menu disabled title + toggle hover),
# border (search-field focus ring). This module never imports fc_gui — the host
# passes the constants in; the defaults keep standalone identical.
_DEFAULT_THEME = {
    "bg": BG_HEX,          # "#0a0a14" — current frame/bar/canvas bg (unchanged)
    "panel": "#16213e",    # current checkbutton selectcolor (== fc_gui BG_PANEL)
    "entry_bg": "#0f3460", # fc_gui BG_ENTRY — new search-field + menu-active bg
    "fg": "#8b9bb5",       # current toolbar text gray (unchanged)
    "accent": "#00d4ff",   # fc_gui FG_ACCENT — menu title + toggle hover accent
    "border": "#2a2a4a",   # fc_gui BORDER_COLOR — search-field focus ring
}

# Strips the " (X.X ly)" annotation the range/threat option labels carry so a
# menu selection can be stored back as a bare ship/base name (spec §2.5).
_LY_SUFFIX = re.compile(r"\s*\([^()]*ly\)\s*$")


def _now_ms() -> float:
    return time.monotonic() * 1000.0


def _request_sig(req: dict) -> tuple:
    """Comparable signature of a render request: camera center/scale + viewport
    size + bloom + mode + tint + bridges (NOT generation -- that is a staleness
    token, and duplicates by definition carry different generations). Used by the
    worker to suppress a settle re-render that duplicates the crisp frame already
    applied to the canvas (Task 18 Step 1b; see MapTab._applied_sig).

    bridges participates so a Bridges-layer toggle (tuple <-> None) is NEVER
    suppressed as a duplicate -- the on-screen frame drew a different bridge set,
    so it must re-render. The bridges value is a hashable tuple of (id, id)
    pairs (or None when the layer is off), so it compares by value here.

    The camera travels as a ``{cx, cy, scale}`` dict (built by Camera.to_dict);
    TintSpec is a frozen dataclass, so it is hashable and compares by value --
    a plain tuple therefore compares two requests structurally. ``mode`` is the
    RAW request value ("auto"/"full"/"degraded"): for an identical view an
    "auto" duplicate would re-produce the same class of frame, and the one on
    screen is already correct for that view, so suppressing it is safe (at
    worst a rare auto full<->degraded flip is deferred to the next real
    camera/toggle change)."""
    c = req["camera"]
    return (c["cx"], c["cy"], c["scale"], req["vw"], req["vh"],
            bool(req["bloom"]), req["mode"], req.get("tint"), req.get("bridges"))


class MapTabState:
    """Pure camera/gesture/generation bookkeeping (headless-testable)."""

    def __init__(self, vw: int, vh: int) -> None:
        self.vw = vw
        self.vh = vh
        self.model = None
        self.camera = mc.Camera()
        self.gesture = mc.GestureTracker(settle_ms=SETTLE_MS)
        # Wheel notches retarget this animator instead of jumping the camera; the
        # tick loop applies its per-frame factors (cursor-anchored) so zoom GLIDES
        # to the target (~120 ms ease) rather than stepping in discrete jumps
        # (owner feedback: "doesn't feel smooth"). Pure/injected-time -> headless.
        self.zoom_anim = mc.ZoomAnimator()
        self._generation = 0
        self._dirty = False
        self._name_to_id: dict[str, int] = {}
        # --- overlay layer state (Phase D) ---
        self.range_overlay = None
        self.threat_set = None
        self.fleet: dict[int, int] = {}
        self.friendly_staging: set[int] = set()
        self.hostile_staging: set[int] = set()
        self.own_system_id = None
        # Resolved Ansiblex bridge id-pairs (hashable tuple of unordered
        # (id_a, id_b) pairs from map_overlays.resolve_bridges). A BASE-layer
        # element -- drawn into the bitmap, not a Tk overlay item.
        self.bridges: tuple = ()

    def tint_spec(self):
        import map_render as _mr
        bright = self.range_overlay.bright_set() if self.range_overlay else None
        return _mr.TintSpec(bright=bright, halo=self.threat_set) \
            if (bright is not None or self.threat_set is not None) else None

    # -- model / camera -------------------------------------------------------
    def attach_model(self, model) -> None:
        self.model = model
        lo, hi = mc.scale_limits(model.bounds, mr.median_edge_length(model),
                                 self.vw, self.vh)
        self.camera.set_scale_limits(lo, hi)
        self.camera.fit_bounds(model.bounds, self.vw, self.vh)
        self._name_to_id = {s.name.lower(): sid for sid, s in model.systems.items()}
        self._dirty = True

    def resize(self, vw: int, vh: int) -> None:
        if vw > 1 and vh > 1 and (vw, vh) != (self.vw, self.vh):
            self.vw, self.vh = vw, vh
            self._dirty = True

    def restore_camera(self, d: dict) -> None:
        if d and d.get("scale"):
            cam = mc.Camera.from_dict(d)
            cam.set_scale_limits(self.camera.min_scale, self.camera.max_scale)
            self.camera = cam
            self._dirty = True

    # -- inputs ----------------------------------------------------------------
    def on_wheel(self, delta_steps: int, sx: float, sy: float, now_ms: float) -> str:
        """Retarget the eased zoom (does NOT move the camera this instant): the
        tick loop advances the glide and mutates the camera. Rapid notches
        compound the TARGET (ZoomAnimator.start bases off the live target while
        active), so three quick notches aim at scale*ZOOM_STEP**3 (Maps feel).
        Touch the settle tracker so a crisp render is scheduled once the glide
        ends; the animator keeps touching it every anim tick so settle waits."""
        self.zoom_anim.start(self.camera.scale, ZOOM_STEP ** delta_steps, sx, sy,
                             now_ms, self.camera.min_scale, self.camera.max_scale)
        self.gesture.touch(now_ms)
        return "anim"

    def zoom_instant(self, delta_steps: int, sx: float, sy: float,
                     now_ms: float) -> None:
        """Instant cursor-anchored zoom (P3 escape hatch, zoom_animation=False):
        moves the camera THIS instant (no eased glide) and touches the settle
        tracker so the tick loop schedules a crisp once input stops -- the
        pre-Phase-F snap behaviour, now driven through the worker gesture-frame
        pipeline (the caller enqueues one quick frame)."""
        self.camera.zoom_at(ZOOM_STEP ** delta_steps, sx, sy, self.vw, self.vh)
        self.gesture.touch(now_ms)
        self._dirty = True

    def on_drag(self, dx: float, dy: float, now_ms: float | None = None) -> None:
        self.camera.pan_pixels(dx, dy)
        if now_ms is not None:
            self.gesture.touch(now_ms)
        self._dirty = True

    def tick(self, now_ms: float) -> str | None:
        """Called periodically. FIRST advances the zoom animation: while it runs
        the camera glides toward the wheel target (cursor-anchored via zoom_at at
        the stored anchor) and the gesture tracker is kept 'touched' so the settle
        crisp waits for the glide to finish -> returns 'anim'. Once the animator
        goes idle, falls through to the settle path: returns 'crisp' when a
        settled re-render is due, else None."""
        f = self.zoom_anim.tick(self.camera.scale, now_ms)
        if f is not None:
            self.camera.zoom_at(f, *self.zoom_anim.anchor, self.vw, self.vh)
            self.gesture.touch(now_ms)      # settle waits until the glide ends
            self._dirty = True
            return "anim"
        if self._dirty and self.gesture.is_settled(now_ms):
            self._dirty = False
            return "crisp"
        return None

    def force_dirty(self) -> None:
        self._dirty = True

    # -- generation ------------------------------------------------------------
    def next_generation(self) -> int:
        self._generation += 1
        return self._generation

    def is_current(self, generation: int) -> bool:
        return generation == self._generation

    # -- queries ----------------------------------------------------------------
    def fly_to(self, name: str) -> bool:
        sid = self._name_to_id.get(name.strip().lower())
        if sid is None or self.model is None:
            return False
        s = self.model.systems[sid]
        self.camera.cx, self.camera.cy = s.x, s.y
        self.camera.scale = max(self.camera.scale, self.camera.max_scale / 3.0)
        self._dirty = True
        return True

    def hover_hit(self, sx: float, sy: float) -> int | None:
        if self.model is None:
            return None
        wx, wy = self.camera.screen_to_world(sx, sy, self.vw, self.vh)
        return self.model.nearest(wx, wy, max_dist=14.0 / self.camera.scale)

    # -- range overlay (menu helper) -------------------------------------------
    def compute_range_overlay(self, sid: int, ly: float, label: str = "",
                              within_fn=None, legal_fn=None):
        """Thin wrapper over map_overlays.compute_range (injectable data fns for
        tests; defaults bind to system_coords via map_overlays)."""
        return mo.compute_range(sid, ly, label,
                                within_fn=within_fn, legal_fn=legal_fn)


class MapTelemetry:
    """Opt-in frame-timing recorder for the map tab (Task 23 feel diagnosis).

    Instantiated by MapTab ONLY when telemetry is enabled (``MapTab(telemetry=
    True)`` or env ``FCTOOL_MAP_TELEMETRY``). When disabled the MapTab holds
    ``self._tele = None`` and every instrumentation site short-circuits on a single
    ``if _c:`` where ``_c = time.perf_counter if self._tele is not None else None``
    -- so the OFF path is one attribute load, one ``is not None`` test, and N cheap
    ``if None:`` branches: no ``perf_counter`` call, no ``_ms()``, no dict, no
    allocation (measured in tools/spike/off_cost.py).

    All series are bounded deques timestamped in monotonic-ms since construction so
    analysis can slice any window by phase mark:
      * ``stages``  name -> (t_ms, dur_ms)  per-stage durations (quick_frame / ppm /
        PhotoImage / itemconfig+coords / overlay redraw / totals / worker render);
      * ``ticks``   per-tick dicts (t, phase: idle|anim|drain-apply, verdict, and
        tick / drain / state-tick ms);
      * ``gaps``    (t_ms, ms-since-previous _photo swap) -- the felt frame cadence;
      * ``lag``     (t_ms, actual_delay_ms - 50) from a 50 ms event-loop heartbeat.
    ``mark(label)`` stamps phase boundaries the harness sets. ``summary()`` reduces
    each series to p50/p90/p95/max/mean; ``dump()`` writes raw + summary JSON."""

    def __init__(self, maxlen: int = 2000) -> None:
        self._t0 = time.monotonic()
        self._maxlen = maxlen
        self.stages: dict[str, deque] = {}
        self.ticks: deque = deque(maxlen=maxlen)
        self.gaps: deque = deque(maxlen=maxlen)
        self.lag: deque = deque(maxlen=maxlen)
        # Phase H (Task 25): one rich dict per frame APPLY (crisp + gesture) carrying
        # the finer sub-stage split (photo / itemconfig / coords / status / overlay-
        # delete / overlay-create) + spike correlates (image-name count, working-set
        # KB + delta, page-fault count + delta, burst length, camera scale). Lets the
        # analyzer attribute each spike to a sub-stage AND correlate it with paging /
        # allocation / burst-following in ONE row instead of joining stage series by
        # timestamp. self.hover is one row per <Motion>: (t, hit_test_ms, draw_ms,
        # hit) -> A2 hover-cost profile + events/sec (bucketed in analysis).
        self.applies: deque = deque(maxlen=maxlen)
        self.hover: deque = deque(maxlen=maxlen)
        # Task 27: one row per main-loop stall-sentinel beat -- (t, observed_delay_ms,
        # ws_delta_kb|None, armed) -- so the harness can see whether the sentinel
        # caught the stalls the apply-path M4 detector missed (recorded only when
        # telemetry is on; the sentinel's ARMING is telemetry-independent).
        self.sentinel: deque = deque(maxlen=maxlen)
        self.marks: list = []
        self._last_swap: float | None = None

    def _ms(self) -> float:
        return (time.monotonic() - self._t0) * 1000.0

    def stage(self, name: str, dur_ms: float, t_ms: float | None = None) -> None:
        d = self.stages.get(name)
        if d is None:
            d = self.stages[name] = deque(maxlen=self._maxlen)
        d.append((self._ms() if t_ms is None else t_ms, dur_ms))

    def tick(self, rec: dict) -> None:
        self.ticks.append(rec)

    def apply_rec(self, rec: dict) -> None:
        self.applies.append(rec)

    def hover_rec(self, hit_ms: float, draw_ms: float, hit: bool) -> None:
        self.hover.append((self._ms(), hit_ms, draw_ms, hit))

    def sentinel_rec(self, t_ms: float, observed_ms: float,
                     ws_delta_kb: int | None, armed: bool) -> None:
        self.sentinel.append((t_ms, observed_ms, ws_delta_kb, armed))

    def swap(self) -> None:
        """Record a _photo swap; append the gap (ms) since the previous swap."""
        now = time.monotonic()
        if self._last_swap is not None:
            self.gaps.append(((now - self._t0) * 1000.0,
                              (now - self._last_swap) * 1000.0))
        self._last_swap = now

    def lag_sample(self, over_ms: float) -> None:
        self.lag.append((self._ms(), over_ms))

    def mark(self, label: str) -> None:
        self.marks.append((self._ms(), label))

    # -- reduction --------------------------------------------------------------
    @staticmethod
    def _pcts(values) -> dict:
        xs = sorted(values)
        n = len(xs)
        if n == 0:
            return {"n": 0}

        def q(p: float) -> float:
            return xs[min(int(n * p), n - 1)]

        return {"n": n, "p50": q(0.50), "p90": q(0.90), "p95": q(0.95),
                "max": xs[-1], "mean": sum(xs) / n}

    def summary(self) -> dict:
        out: dict = {"elapsed_ms": self._ms(), "stages": {}}
        for name, series in self.stages.items():
            out["stages"][name] = self._pcts([d for _, d in series])
        out["gap"] = self._pcts([g for _, g in self.gaps])
        out["lag"] = self._pcts([v for _, v in self.lag])
        out["tick_ms"] = self._pcts([r["tick_ms"] for r in self.ticks])
        phases: dict[str, int] = {}
        for r in self.ticks:
            phases[r["phase"]] = phases.get(r["phase"], 0) + 1
        out["tick_phase_counts"] = phases
        # Phase H apply/hover reductions (Task 25). apply_total is the felt
        # per-frame apply cost (spikes live here); hover_total is the per-<Motion>
        # cost (A2). spike_count is applies whose total exceeds 100 ms.
        out["apply_total"] = self._pcts([r["total"] for r in self.applies])
        out["hover_total"] = self._pcts(
            [h + d for _, h, d, _ in self.hover])
        out["apply_n"] = len(self.applies)
        out["spike_count_100ms"] = sum(1 for r in self.applies
                                       if r.get("total", 0.0) > 100.0)
        return out

    def dump(self, path: str = "tools/spike/telemetry_out.json",
             meta: dict | None = None) -> str:
        payload = {
            "meta": meta or {},
            "summary": self.summary(),
            "marks": list(self.marks),
            "ticks": list(self.ticks),
            "gaps": list(self.gaps),
            "lag": list(self.lag),
            "stages": {k: list(v) for k, v in self.stages.items()},
            "applies": list(self.applies),
            "hover": list(self.hover),
            "sentinel": list(self.sentinel),
        }
        try:
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
        except Exception:
            pass
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return path


class MapTab:
    """Tk widget/controller. Host wires: frame into a Notebook tab,
    on_shown/on_hidden from <<NotebookTabChanged>>, cfg dict + save_cfg,
    callbacks for context-menu actions (all optional):
      set_destination(name), open_dotlan(name), navigate_wh(name),
      titan_bridge(name), copy(name)."""

    def __init__(self, parent, *, model_loader=map_data.load_map_model,
                 cfg: dict | None = None, save_cfg=None, callbacks: dict | None = None,
                 autocomplete_cls=None, theme: dict | None = None,
                 telemetry: bool = False) -> None:
        self.cfg = cfg or {}
        self.save_cfg = save_cfg or (lambda d: None)
        self.callbacks = callbacks or {}
        self._model_loader = model_loader
        # App-palette theme for the menus + toolbar. Merging over _DEFAULT_THEME
        # keeps standalone identical and lets a caller pass a partial dict. fc_gui
        # injects its BG_DARK/BG_PANEL/... constants so the map matches the app.
        self.theme = {**_DEFAULT_THEME, **(theme or {})}
        t = self.theme

        self.frame = tk.Frame(parent, bg=t["bg"])
        bar = tk.Frame(self.frame, bg=t["bg"])
        bar.pack(side="top", fill="x")
        tk.Label(bar, text="Search:", bg=t["bg"], fg=t["fg"]).pack(side="left", padx=(6, 2))
        entry_cls = autocomplete_cls or tk.Entry
        self.search_entry = entry_cls(bar, width=24)
        self.search_entry.pack(side="left", padx=2, pady=3)
        self.search_entry.bind("<Return>", self._on_search)
        # Style the search field to the theme. Guarded: a custom autocomplete
        # widget might reject a Tk option (plain tk.Entry accepts them all).
        try:
            self.search_entry.configure(
                bg=t["entry_bg"], fg=t["fg"], insertbackground=t["fg"],
                relief="flat", highlightthickness=1,
                highlightbackground=t["border"], highlightcolor=t["accent"])
        except tk.TclError:
            pass
        self._bloom_var = tk.BooleanVar(value=bool(self.cfg.get("bloom", True)))
        # Zoom-animation escape hatch (Task 24, P3): True = eased glide (default),
        # False = instant snap (pre-Phase-F feel) for owners who prefer it. No
        # toolbar button -- toggled from the empty-space right-click menu and
        # persisted on hide like bloom/layers.
        self._zoom_anim_var = tk.BooleanVar(
            value=bool(self.cfg.get("zoom_animation", True)))
        tk.Checkbutton(bar, text="Bloom", variable=self._bloom_var, bg=t["bg"],
                       fg=t["fg"], selectcolor=t["panel"],
                       activebackground=t["bg"], activeforeground=t["accent"],
                       command=self._on_bloom_toggle).pack(side="left", padx=8)
        # --- Phase D layer toggles (fleet/staging/threat) ---
        _layers = self.cfg.get("layers", {})
        self._layer_vars: dict[str, tk.BooleanVar] = {
            "fleet": tk.BooleanVar(value=bool(_layers.get("fleet", True))),
            "staging": tk.BooleanVar(value=bool(_layers.get("staging", True))),
            "threat": tk.BooleanVar(value=bool(_layers.get("threat", False))),
            "bridges": tk.BooleanVar(value=bool(_layers.get("bridges", True))),
        }
        # Current threat-ship selection label (radiobutton var; synced lazily
        # from cfg["threat_ship"] when the empty-space menu is built).
        self._threat_var = tk.StringVar()
        for _key, _text in (("fleet", "Fleet"), ("staging", "Staging"),
                            ("threat", "Threat"), ("bridges", "Bridges")):
            tk.Checkbutton(bar, text=_text, variable=self._layer_vars[_key],
                           bg=t["bg"], fg=t["fg"], selectcolor=t["panel"],
                           activebackground=t["bg"], activeforeground=t["accent"],
                           command=lambda k=_key: self._on_layer_toggle(k)
                           ).pack(side="left", padx=4)
        self.status = tk.Label(bar, text="", bg=t["bg"], fg=t["fg"], anchor="e")
        self.status.pack(side="right", padx=6)

        self.canvas = tk.Canvas(self.frame, bg=t["bg"], highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self._img_item = self.canvas.create_image(0, 0, anchor="nw")
        self._photo = None                      # keep a ref or Tk drops the image

        self.state = MapTabState(vw=800, vh=600)
        self.renderer = None
        # FrameCache is now WORKER-OWNED (Task 24, P1): only the render worker
        # thread touches it -- it stores the full margined surface after each
        # crisp render (_render_locked) and serves gesture quick frames from it
        # (_render_gesture), so quick_frame + surface_to_ppm (~11 ms) run OFF the
        # main thread. The main thread only ever applies a finished ppm
        # (PhotoImage + itemconfig, ~18 ms). Created once and shared across worker
        # restarts (shutdown_worker joins before a re-show spawns a new worker, so
        # there is never concurrent access); it survives hide/show like the
        # renderer/model so a reshow can serve gestures from the last crisp.
        self._worker_cache = mr.FrameCache()
        self.stats = mr.SettleStats()
        self._render_mode = self.cfg.get("render_mode", "auto")

        self._req_q: "queue.Queue[dict]" = queue.Queue()
        # Finished frames flow worker -> main via this queue, drained by the
        # main-thread _tick loop. Cross-thread frame.after() would raise
        # "main thread is not in main loop" whenever the host drives Tk with
        # update() rather than mainloop() (e.g. the headless smoke tests), so
        # the worker never touches Tcl; only the main thread applies frames.
        self._result_q: "queue.Queue[tuple]" = queue.Queue()
        self._worker: threading.Thread | None = None
        # Signature of the crisp frame currently APPLIED to the canvas (duplicate-
        # settle suppression, Task 18 Step 1b): the worker skips a request whose
        # sig equals it -- that exact view is already on screen. WRITTEN by the
        # main thread only (_apply_crisp_frame records it; _request_gesture_frame
        # clears it when a quick frame replaces the crisp); READ by the worker (GIL-atomic
        # reference read). A stale read is benign both ways: None/old sig -> one
        # extra render (safe); a sig cleared just after the worker read it -> the
        # gesture that cleared it also touched the settle tracker, so a fresh
        # request for the moved camera follows anyway. NOTE: comparing against
        # the last RENDERED request instead is unsound -- on a real-model first
        # show the gen-1 render outlives TICK_MS, its frame is dropped as stale
        # (the first tick already posted gen 2), and gen 2 would then be
        # suppressed as a "duplicate" of a frame nobody ever saw, leaving the
        # canvas black until the first input (measured, Task 18).
        self._applied_sig: tuple | None = None
        self._visible = False
        self._tick_scheduled = False
        self._tick_after_id: str | None = None   # pending _tick after() id (cancel on hide)
        self._drag_last: tuple[int, int] | None = None
        self._img_offset = (0.0, 0.0)
        self._last_drag_qf = 0.0        # last drag quick-frame time (throttle, Task 17)
        # Adaptive-pacing state (Task 24, P2). _gesture_inflight is True from the
        # moment a gesture request is enqueued until its result is applied (or
        # coalesced away / dropped) -- both apply paths clear it, so it can never
        # wedge. _last_gesture_req_ms is the monotonic-ms stamp of the last
        # gesture request for the >= GESTURE_MIN_INTERVAL_MS gate.
        self._gesture_inflight = False
        self._last_gesture_req_ms = 0.0

        # --- Phase H adaptive conservation pacing (Task 26, M4) ----------------
        # _healthy_interval_ms is the gate interval on a box that is not stalling
        # (the ~33 fps default). _conserve_until_ms is the monotonic-ms deadline of
        # the current conservation window (0 = not conserving); _m4_note_apply pushes
        # it CONSERVATION_HOLD_MS into the future on each detected stall and the gate
        # reads CONSERVATION_INTERVAL_MS until it passes. _m4_ws_last is the previous
        # apply's working-set KB, for the per-apply growth delta (None until the first
        # sample / when the ctypes probe is unavailable -> detector simply idle).
        self._healthy_interval_ms = GESTURE_MIN_INTERVAL_MS
        self._conserve_until_ms = 0.0
        self._m4_ws_last: int | None = None
        # --- Task 27 main-loop stall sentinel -----------------------------------
        # A wall-clock frame.after() heartbeat (started with the tick on show,
        # cancelled on hide) that arms the SAME conservation window M4 does when a
        # stall lands OUTSIDE the apply paths (sustained pan). _stall_sentinel_last_ms
        # is the monotonic-ms stamp of the last beat's schedule (for the observed-delay
        # measurement); _stall_sentinel_ws_last is the previous beat's working-set KB
        # (per-beat growth delta; None re-establishes the baseline after a show).
        self._stall_sentinel_after_id: str | None = None
        self._stall_sentinel_scheduled = False
        self._stall_sentinel_last_ms: float | None = None
        self._stall_sentinel_ws_last: int | None = None
        # Telemetry correlate state (only written on the telemetry-on apply path).
        self._tele_ws_last: int | None = None    # last working-set KB (for delta)
        self._tele_pf_last: int | None = None     # last page-fault count (for delta)
        self._burst_len = 0                        # gesture applies since last crisp
        self._tele_ov_del = 0.0                    # last overlay delete-ms (apply row)
        self._tele_ov_new = 0.0                    # last overlay create-ms (apply row)

        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<ButtonPress-1>", self._on_drag_start)
        self.canvas.bind("<B1-Motion>", self._on_drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._on_drag_end)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Button-3>", self._on_right_click)
        self.canvas.bind("<Configure>", self._on_configure)

        # --- opt-in feel telemetry (Task 23) -----------------------------------
        # Enabled by the `telemetry=True` kwarg OR env FCTOOL_MAP_TELEMETRY (any
        # value other than ""/"0"/"false"; a bare integer >1 also sets the deque
        # maxlen so a long diagnosis run keeps every idle tick). Disabled by
        # default -> self._tele is None -> every instrumentation site is a single
        # short-circuiting `if _c:` (no perf_counter / dict / allocation). The lag
        # heartbeat reschedules UNCONDITIONALLY (not gated on _visible) so it
        # samples loop lag through non-map phases too (H2/H4 comparator).
        _env = os.environ.get("FCTOOL_MAP_TELEMETRY", "")
        _tele_on = bool(telemetry) or (_env not in ("", "0", "false", "False"))
        _maxlen = int(_env) if _env.isdigit() and int(_env) > 1 else 2000
        self._tele = MapTelemetry(maxlen=_maxlen) if _tele_on else None
        self._tele_applied_this_tick = False
        self._tele_lag_last: float | None = None
        if self._tele is not None:
            self._tele_lag_last = time.perf_counter()
            try:
                self.frame.after(50, self._tele_lag_beat)
            except Exception:
                pass

    def _tele_lag_beat(self) -> None:
        """Event-loop lag heartbeat (Task 23). Records actual_delay_ms - 50 each
        50 ms beat and reschedules UNCONDITIONALLY (independent of _visible / the
        map tick), so it measures loop lag through every harness phase -- including
        the non-map-tab control window where the map tick is stopped. No-ops (and
        stops rescheduling) once telemetry is cleared or the root is torn down."""
        tele = self._tele
        if tele is None:
            return
        now = time.perf_counter()
        if self._tele_lag_last is not None:
            tele.lag_sample((now - self._tele_lag_last) * 1000.0 - 50.0)
        self._tele_lag_last = now
        try:
            self.frame.after(50, self._tele_lag_beat)
        except Exception:
            pass

    # ---- lifecycle ------------------------------------------------------------
    def on_shown(self) -> None:
        self._visible = True
        if self.renderer is None:
            model = self._model_loader()
            self.state.attach_model(model)
            self.state.restore_camera(self.cfg.get("camera") or {})
            self.renderer = mr.Renderer(model)
        # (Re)start the render worker on every show. _start_worker is idempotent
        # (no-op while a worker is running), so this spawns a thread only on the
        # first show and after an on_hidden shutdown -- letting a hide->show cycle
        # resurrect the worker that on_hidden kills to stop the leak (Task 22).
        self._start_worker()
        # Refresh Ansiblex bridges from the host on (re)show: config edits or ESI
        # auto-discovery that happened while the tab was hidden are reflected, and
        # the crisp request below carries them into the render (store only -- no
        # extra request; _request_crisp fires immediately after).
        gb = self.callbacks.get("get_bridges")
        if gb is not None:
            try:
                self.state.bridges = tuple(gb() or ())
            except Exception:
                pass
        self.state.force_dirty()
        self._request_crisp()
        self._schedule_tick()
        self._schedule_stall_sentinel()          # Task 27: main-loop stall watchdog

    def on_hidden(self) -> None:
        if self.renderer is None:
            # Hidden before the first show: the camera is still at fresh
            # defaults (the fit-universe sentinel hasn't been applied yet).
            # Persisting it now would clobber restore_camera's fit sentinel and
            # reopen the map at max zoom on empty space -- save nothing.
            self._visible = False
            return
        self._visible = False
        cam = dict(self.state.camera.to_dict())
        merged = dict(self.cfg)
        merged["camera"] = cam
        merged["bloom"] = bool(self._bloom_var.get())
        merged["zoom_animation"] = bool(self._zoom_anim_var.get())   # P3 persist
        # Persist the live layer-toggle state (range flag is overlay-driven and
        # already carried on self.cfg["layers"]; the 3 vars mirror the toolbar).
        layers = dict(self.cfg.get("layers", {}))
        for key, var in self._layer_vars.items():
            layers[key] = bool(var.get())
        merged["layers"] = layers
        self.cfg.update(merged)
        self.save_cfg(merged)
        # Stop the immortal render worker so a hidden tab does not leak a
        # map-render daemon (Task 22). renderer/model/_worker_cache and the last
        # frame on the canvas all survive; on_shown restarts the thread.
        self.shutdown_worker()
        # Cancel the pending _tick after() so it cannot fire against a torn-down
        # root ("invalid command name ..._tick" Tcl stderr noise) and reset the
        # flag so on_shown can schedule a fresh tick.
        if self._tick_after_id is not None:
            try:
                self.frame.after_cancel(self._tick_after_id)
            except Exception:
                pass
            self._tick_after_id = None
        self._tick_scheduled = False
        # Cancel the stall sentinel too (Task 27) and drop its baselines so the next
        # show re-establishes them cleanly (no bogus ws delta across the hidden gap).
        if self._stall_sentinel_after_id is not None:
            try:
                self.frame.after_cancel(self._stall_sentinel_after_id)
            except Exception:
                pass
            self._stall_sentinel_after_id = None
        self._stall_sentinel_scheduled = False
        self._stall_sentinel_last_ms = None
        self._stall_sentinel_ws_last = None

    # ---- overlay layers (Phase D) ----------------------------------------------
    # Two strata: range/threat re-tint the base bitmap (settle re-render via
    # _request_crisp), while fleet/staging/illegal/origin/own are pure Tk canvas
    # items repainted instantly by _redraw_overlays.
    def update_fleet(self, members) -> None:
        self.state.fleet = mo.fleet_counts(members)
        self._redraw_overlays()

    def set_staging(self, friendly_ids, hostile_ids) -> None:
        self.state.friendly_staging = set(friendly_ids or ())
        self.state.hostile_staging = set(hostile_ids or ())
        self._redraw_overlays()

    def apply_range_overlay(self, overlay) -> None:
        self.state.range_overlay = overlay          # base-layer change:
        self.state.force_dirty()                    # settle re-render with tint
        self._request_crisp()
        self._redraw_overlays()

    def clear_range_overlay(self) -> None:
        self.state.range_overlay = None
        # Range flag is overlay-driven: clearing unchecks it (decision log).
        self.cfg.setdefault("layers", {})["range"] = False
        rv = self._layer_vars.get("range")
        if rv is not None:
            rv.set(False)
        self.state.force_dirty()
        self._request_crisp()
        self._redraw_overlays()

    def set_threat(self, threat_set) -> None:
        self.state.threat_set = threat_set          # None clears
        self.state.force_dirty()
        self._request_crisp()
        self._redraw_overlays()

    def set_bridges(self, pairs) -> None:
        """Store resolved Ansiblex bridge id-pairs (a hashable tuple of unordered
        (id_a, id_b) pairs from map_overlays.resolve_bridges) and re-render.
        Bridges are a BASE-layer element -- drawn into the bitmap under the node
        glows -- so this is a settle re-render via _request_crisp, NOT a Tk
        overlay repaint. Safe before the first on_shown: _request_crisp and
        _redraw_overlays no-op while the renderer/model are unset, and the stored
        tuple is picked up by the first crisp request."""
        self.state.bridges = tuple(pairs or ())     # coerce to hashable tuple
        self.state.force_dirty()
        self._request_crisp()
        self._redraw_overlays()

    def set_own_location(self, system_id) -> None:  # spec §5.2: own char distinct
        self.state.own_system_id = system_id
        self._redraw_overlays()

    def _layer_on(self, key: str) -> bool:
        return bool(self.cfg.get("layers", {}).get(key, True))

    def _draw_diamond(self, sx: float, sy: float, r: float,
                      color: str, tag: str) -> None:
        self.canvas.create_polygon(sx, sy - r, sx + r, sy, sx, sy + r, sx - r, sy,
                                   fill=color, tags=tag)

    def _redraw_overlays(self) -> None:
        """Delete + repaint every Tk overlay item, projecting with the LIVE camera
        and WITHOUT adding _img_offset -- the live projection already equals the
        on-canvas position of the translated stale bitmap, so adding the offset
        would double-count the drag during the pre-settle window (Task 9 finding
        #2). Pure Tk; cheap enough to run on every gesture/drag frame.

        Task 25 splits the delete and create phases into ov_delete / ov_create
        stages so the analyzer can tell whether an overlay-carried apply spike sits
        in the tag deletes or the item creates (the split telemetry is kept as the
        standing diagnostic instrument; Task 25 A/B-rejected batching the six deletes
        into one Tcl call -- it did not move the spike, which is an OS working-set
        stall, not Tcl-op count)."""
        tele = self._tele
        _c = time.perf_counter if tele is not None else None
        if _c: _o0 = _c()
        canvas = self.canvas
        _tags = ("ov_fleet", "ov_staging", "ov_illegal", "ov_range_strike",
                 "ov_origin", "ov_own")
        for tag in _tags:
            canvas.delete(tag)
        if _c: _od = _c()
        if self.renderer is None or self.state.model is None:
            if _c:
                _now = tele._ms()
                self._tele_ov_del = (_od - _o0) * 1000.0
                self._tele_ov_new = 0.0
                tele.stage("ov_delete", self._tele_ov_del, _now)
                tele.stage("ov_create", 0.0, _now)
                tele.stage("ov_total", self._tele_ov_del, _now)
            return
        cam = self.state.camera
        vw, vh = self.state.vw, self.state.vh
        systems = self.state.model.systems
        st = self.state

        def project(sid):
            s = systems.get(sid)
            if s is None:
                return None
            sx, sy = cam.world_to_screen(s.x, s.y, vw, vh)
            if sx < -40 or sy < -40 or sx > vw + 40 or sy > vh + 40:
                return None
            return sx, sy

        # -- range: struck rings on illegal-in-sphere systems + origin badge
        ov = st.range_overlay
        if ov is not None and self._layer_on("range"):
            for sid in ov.illegal:
                p = project(sid)
                if p is None:
                    continue
                sx, sy = p
                canvas.create_oval(sx - 7, sy - 7, sx + 7, sy + 7,
                                   outline="#ff5a76", width=2, tags="ov_illegal")
                d = 7 * 0.70710678                  # 45deg strike-through the ring
                canvas.create_line(sx - d, sy - d, sx + d, sy + d,
                                   fill="#ff5a76", width=2, tags="ov_range_strike")
            p = project(ov.origin_id)
            if p is not None:
                sx, sy = p
                canvas.create_oval(sx - 12, sy - 12, sx + 12, sy + 12,
                                   outline="#e0e0e0", width=2, tags="ov_origin")

        # -- staging diamonds (friendly green / hostile red)
        if self._layer_on("staging"):
            for sid in st.friendly_staging:
                p = project(sid)
                if p is not None:
                    self._draw_diamond(p[0], p[1], 8, "#59d98c", "ov_staging")
            for sid in st.hostile_staging:
                p = project(sid)
                if p is not None:
                    self._draw_diamond(p[0], p[1], 8, "#ff5a76", "ov_staging")

        # -- fleet pins + count badges, own-location ring
        if self._layer_on("fleet"):
            for sid, count in st.fleet.items():
                p = project(sid)
                if p is None:
                    continue
                sx, sy = p
                canvas.create_oval(sx - 6, sy - 6, sx + 6, sy + 6,
                                   fill="#00d4ff", outline="#ffffff", width=1,
                                   tags="ov_fleet")
                canvas.create_text(sx + 9, sy, anchor="w", text=str(count),
                                   fill="#e0e0e0", font=("Segoe UI", 9),
                                   tags="ov_fleet")
            own = st.own_system_id
            if own is not None:
                p = project(own)
                if p is not None:
                    sx, sy = p
                    canvas.create_oval(sx - 10, sy - 10, sx + 10, sy + 10,
                                       outline="#ffffff", width=2, tags="ov_own")
                    canvas.create_oval(sx - 2, sy - 2, sx + 2, sy + 2,
                                       fill="#ffffff", outline="", tags="ov_own")
        if _c:
            _o1 = _c()
            _now = tele._ms()
            self._tele_ov_del = (_od - _o0) * 1000.0
            self._tele_ov_new = (_o1 - _od) * 1000.0
            tele.stage("ov_delete", self._tele_ov_del, _now)
            tele.stage("ov_create", self._tele_ov_new, _now)
            tele.stage("ov_total", (_o1 - _o0) * 1000.0, _now)

    # ---- worker ----------------------------------------------------------------
    def _start_worker(self) -> None:
        if self._worker is None:
            self._worker = threading.Thread(target=self._worker_loop, daemon=True,
                                            name="map-render")
            self._worker.start()

    def shutdown_worker(self) -> None:
        """Stop the render worker and drop the handle so the next on_shown can
        start a fresh one (Task 22 leak fix). Enqueues a None sentinel that
        _worker_loop returns on -- whether it surfaces from the blocking get() or
        while coalescing (draining get_nowait to the newest request) -- then joins
        briefly for test determinism (renders are ~8-17 ms, so 2 s is ample).
        Clears _applied_sig so the fresh worker's first render after a re-show
        (same camera as at hide) is NOT duplicate-suppressed: on_shown
        force-dirties and re-requests crisp, and a cleared sig lets that request
        actually render (one extra render, safe -- see _applied_sig). Keeps
        renderer/worker_cache and the on-canvas frame intact; only the thread
        dies."""
        # Guaranteed teardown of the Task-24 pacing state, BEFORE the worker-None
        # early-return: a gesture request that was in flight when the worker died
        # would otherwise never be applied (its result is discarded) and
        # _gesture_inflight would then wedge the next glide.
        self._gesture_inflight = False
        worker = self._worker
        if worker is None:
            return
        self._req_q.put(None)              # sentinel -> _worker_loop returns
        worker.join(timeout=2.0)
        self._worker = None                # next on_shown spawns a fresh worker
        self._applied_sig = None           # don't dup-suppress the re-show render

    def _worker_loop(self) -> None:
        while True:
            req = self._req_q.get()
            if req is None:                        # shutdown sentinel (blocking get)
                return
            try:                                   # drain to latest (coalesce)
                while True:
                    nxt = self._req_q.get_nowait()
                    if nxt is None:                # sentinel queued behind requests
                        return                     # -> it wins; exit without render
                    req = nxt
            except queue.Empty:
                pass
            result = self._render_locked(req)      # None => duplicate suppressed
            if result is not None:
                self._result_q.put(result)

    def _render_locked(self, req: dict) -> tuple | None:
        """Serve one coalesced request on the worker thread. GESTURE requests
        (kind == "gesture") are dispatched to _render_gesture -- the worker crops+
        smoothscales its own FrameCache off the main thread (Task 24, P1). A CRISP
        request renders the full margined frame, stores it into the worker cache
        (so subsequent gesture frames have real content), and returns None when it
        exactly duplicates the crisp frame already APPLIED to the canvas (camera +
        size + bloom + mode + tint) -- duplicate-settle suppression (Task 18 Step
        1b). The sig travels in the result tuple; _apply_crisp_frame records it
        only when the frame actually lands (visible + current generation), so a
        dropped frame can never block its own re-request (see _applied_sig). Kept
        as a method (not inline in _worker_loop) so the duplicate-suppression
        tests can drive the crisp path directly without the render thread. Touches
        no Tcl."""
        if req.get("kind") == "gesture":
            return self._render_gesture(req)
        sig = _request_sig(req)
        if sig == self._applied_sig:
            return None
        cam = mc.Camera.from_dict(req["camera"])
        t0 = time.perf_counter()
        mode = req["mode"]
        if mode == "auto":
            mode = self.stats.suggest_mode()
        vw, vh = req["vw"], req["vh"]
        # Render a MARGIN-px border beyond the viewport on every side. The camera
        # projects around the surface center, so a bigger surface just shows more
        # world all around (no camera math changes) -- Task 17.
        surf = self.renderer.render(cam, vw + 2 * MARGIN, vh + 2 * MARGIN,
                                    bloom=req["bloom"], mode=mode,
                                    tint=req.get("tint"),
                                    bridges=req.get("bridges"))
        ms = (time.perf_counter() - t0) * 1000.0
        self.stats.record(ms)
        # Tk gets a viewport-sized CENTER CROP (a subsurface view -- no copy;
        # surface_to_ppm reads it directly). Build it transiently so the parent
        # surf is not left subsurface-locked before we store it in the cache.
        ppm = mr.surface_to_ppm(mr.center_subsurface(surf, MARGIN, vw, vh))
        # P1: the WORKER owns the FrameCache now -- store the FULL margined surf
        # here (view dims recorded) so gesture frames serve real margin content
        # without the main thread ever touching pygame. Main only gets the ppm.
        self._worker_cache.store(surf, cam, vw, vh)
        # Hand off to the main thread (no Tcl calls here); _drain_results applies.
        return ("crisp", req["generation"], ppm, ms, sig)

    def _render_gesture(self, req: dict) -> tuple:
        """Serve a gesture (zoom-glide / drag) quick frame from the worker's
        FrameCache for the request's camera SNAPSHOT (Task 24, P1). quick_frame +
        surface_to_ppm (~11 ms measured) run here, off the main thread; the main
        thread applies the ppm with only PhotoImage + itemconfig (~18 ms). Always
        returns a tuple so _drain_results can clear the pacing in-flight flag even
        on the FIRST-SHOW empty cache (ppm None -> main keeps the prior photo, the
        pre-Task-24 behaviour). The camera dict rides back so the apply can offset
        the enqueue-time base image to align with the LIVE camera (drag/zoom lag).
        Touches no Tcl."""
        cam = mc.Camera.from_dict(req["camera"])
        vw, vh = req["vw"], req["vh"]
        t0 = time.perf_counter()
        quick = self._worker_cache.quick_frame(cam, vw, vh)
        if quick is None:                      # empty cache (first show, pre-crisp)
            ms = (time.perf_counter() - t0) * 1000.0
            return ("gesture", req["generation"], None, ms, req["camera"])
        ppm = mr.surface_to_ppm(quick)
        ms = (time.perf_counter() - t0) * 1000.0
        return ("gesture", req["generation"], ppm, ms, req["camera"])

    def _make_photo(self, ppm: bytes):
        """Build the tk image for `ppm`: a fresh tk.PhotoImage each frame (the ~4 MB
        Tcl image create + implicit free). Keeps a ref on self._photo so Tk never
        drops the live image. (Task 25 A/B rejected a two-image ping-pong that
        reconfigured a persistent PhotoImage via configure(data=): it did NOT reduce
        the settle spike -- the freeze is an OS working-set stall, not image-object
        churn -- and the in-place re-decode measured ~2x slower, so the simple
        per-frame construct stands.)"""
        self._photo = tk.PhotoImage(data=ppm)
        return self._photo

    def _record_apply(self, tele, t_ms: float, kind: str, total: float,
                      photo: float, item: float, coords: float, status: float,
                      overlay: float, worker_ms: float) -> None:
        """Append one rich per-apply correlate row (Task 25). Correlates are
        sampled AFTER the ap_total window closed, so image-count / working-set /
        page-fault probing never inflates the very stage timings being attributed.
        image-count (~1 us) and GetProcessMemoryInfo (~1.4 us) are cheap enough to
        sample every apply; a page-fault or working-set jump coincident with a
        spike is the OS/allocation-stall fingerprint."""
        img_n = None
        try:
            img_n = len(self.canvas.tk.call("image", "names"))
        except Exception:
            pass
        ws_kb, pf = _proc_mem()
        ws_d = pf_d = None
        if ws_kb is not None and self._tele_ws_last is not None:
            ws_d = ws_kb - self._tele_ws_last
        if pf is not None and self._tele_pf_last is not None:
            pf_d = pf - self._tele_pf_last
        if ws_kb is not None:
            self._tele_ws_last = ws_kb
        if pf is not None:
            self._tele_pf_last = pf
        try:
            scale = self.state.camera.scale
            ppe = scale * self.renderer._median_edge if self.renderer else 0.0
        except Exception:
            scale = ppe = 0.0
        interval = self._effective_gesture_interval(_now_ms())
        tele.apply_rec({
            "t": t_ms, "kind": kind, "total": total,
            "photo": photo, "item": item, "coords": coords,
            "status": status, "overlay": overlay,
            "ov_del": self._tele_ov_del, "ov_new": self._tele_ov_new,
            "worker_ms": worker_ms,
            "img_n": img_n, "ws_kb": ws_kb, "ws_d": ws_d, "pf": pf, "pf_d": pf_d,
            "burst": self._burst_len, "after_burst": self._burst_len > 0,
            "scale": scale, "ppe": ppe,
            # M4 adaptive-pacing state at this apply (acceptance asserts conservation
            # entry via these): the gate interval in force + whether it is widened.
            "interval": interval,
            "conserve": interval > self._healthy_interval_ms,
        })

    def _apply_crisp_frame(self, generation: int, ppm: bytes, ms: float,
                           sig: tuple | None = None) -> None:
        """Apply a finished CRISP frame (main thread). The worker already stored
        the full margined surf in its cache and cropped the ppm, so this does only
        PhotoImage + itemconfig + status + overlay reproject (no pygame). Resets
        _img_offset to 0 (the crisp is for the settled camera). Clears the pacing
        in-flight flag first so a gesture that was superseded by this crisp (and
        thus never reached _apply_gesture_frame) cannot wedge the next glide.

        Task 25 telemetry splits the apply into PhotoImage / itemconfig / coords /
        status / overlay (each its own stage) plus a rich correlate row; the split
        is what lets the analyzer see WHICH sub-stage a given spike landed in (probe:
        the stall lands on overlay or itemconfig far more often than PhotoImage)."""
        self._gesture_inflight = False
        if not self._visible or not self.state.is_current(generation):
            return                       # dropped -> _applied_sig stays unrecorded
        tele = self._tele
        _c = time.perf_counter if tele is not None else None
        _m0 = time.perf_counter()        # always-on apply timer for the M4 stall detector
        img = self._make_photo(ppm)             # ppm = viewport-sized center crop
        if _c: _a1 = _c()
        self.canvas.itemconfig(self._img_item, image=img)
        if _c: _a2 = _c()
        self.canvas.coords(self._img_item, 0, 0)
        self._img_offset = (0.0, 0.0)
        if _c: _a3 = _c()
        self._applied_sig = sig          # this view is now on screen -> the worker
        self.status.config(text=f"render {ms:.0f} ms")  # may suppress duplicates
        if _c: _a4 = _c()
        self._redraw_overlays()          # reproject Tk overlay items onto the fresh frame
        _m1 = time.perf_counter()        # apply work done -> close the M4 window, then sample
        self._m4_note_apply((_m1 - _m0) * 1000.0, _now_ms())
        if _c:
            _now = tele._ms()
            photo = (_a1 - _m0) * 1000.0
            item = (_a2 - _a1) * 1000.0
            coords = (_a3 - _a2) * 1000.0
            status = (_a4 - _a3) * 1000.0
            overlay = (_m1 - _a4) * 1000.0
            total = (_m1 - _m0) * 1000.0
            tele.stage("ap_photoimage", photo, _now)
            tele.stage("ap_itemconfig", item, _now)
            tele.stage("ap_coords", coords, _now)
            tele.stage("ap_status", status, _now)
            tele.stage("ap_overlay", overlay, _now)
            tele.stage("ap_total", total, _now)
            tele.stage("worker_render_ms", ms, _now)  # worker's own render time
            self._record_apply(tele, _now, "crisp", total, photo, item, coords,
                               status, overlay, ms)
            tele.swap()
            self._tele_applied_this_tick = True
        if tele is not None:
            self._burst_len = 0          # a crisp settle ends the gesture burst

    def _apply_gesture_frame(self, generation: int, ppm: bytes | None,
                             ms: float, cam_dict: dict) -> None:
        """Apply a worker-served gesture quick frame (main thread, Task 24 P1).
        Clears the pacing in-flight flag FIRST -- the request has resolved, so the
        next tick may enqueue again -- even on the empty-cache (ppm None) or
        stale-generation drop paths, so pacing can never wedge. On a real frame it
        offsets the enqueue-time base image so it aligns with the LIVE camera: the
        image is a quick frame for the camera SNAPSHOT taken when the request was
        enqueued (~1 tick ago), while overlays are drawn from the live camera, so
        without the offset the base + overlays would drift by the pan since
        enqueue. For a pure pan (drag) the offset is EXACT and equals the
        accumulated live-pan slide, so the swap is position-continuous; for a zoom
        glide it removes the center-pan component (the small residual scale-stretch
        of one glide step is inherent to an approximate gesture frame). Does NOT
        touch _applied_sig (already cleared to None at enqueue -- the canvas no
        longer shows the applied crisp)."""
        self._gesture_inflight = False
        if ppm is None:
            return                       # empty cache (first show): keep prior photo
        if not self._visible or not self.state.is_current(generation):
            return                       # superseded -> drop, prior photo stays
        # Residual offset that aligns the enqueue-camera base image with the live
        # camera (see docstring). Pure-pan-exact; zoom keeps only a tiny stretch.
        # Computed before the timed window (cheap arithmetic, not a stage).
        live = self.state.camera
        ox = (cam_dict["cx"] - live.cx) * live.scale
        oy = (cam_dict["cy"] - live.cy) * live.scale
        tele = self._tele
        _c = time.perf_counter if tele is not None else None
        _m0 = time.perf_counter()        # always-on apply timer for the M4 stall detector
        img = self._make_photo(ppm)
        if _c: _g1 = _c()
        self.canvas.itemconfig(self._img_item, image=img)
        if _c: _g1b = _c()
        self.canvas.coords(self._img_item, ox, oy)
        self._img_offset = (ox, oy)
        if _c: _g2 = _c()
        self._redraw_overlays()          # reproject overlays onto the gesture frame
        _m1 = time.perf_counter()        # apply work done -> close the M4 window, then sample
        self._m4_note_apply((_m1 - _m0) * 1000.0, _now_ms())
        if _c:
            _now = tele._ms()
            photo = (_g1 - _m0) * 1000.0
            item = (_g1b - _g1) * 1000.0
            coords = (_g2 - _g1b) * 1000.0
            overlay = (_m1 - _g2) * 1000.0
            total = (_m1 - _m0) * 1000.0
            tele.stage("gf_photoimage", photo, _now)
            tele.stage("gf_itemconfig", item, _now)
            tele.stage("gf_coords", coords, _now)
            tele.stage("gf_overlay", overlay, _now)
            tele.stage("gf_total", total, _now)
            tele.stage("gf_worker_ms", ms, _now)   # worker quick_frame+ppm (off-main)
            self._record_apply(tele, _now, "gesture", total, photo, item, coords,
                               0.0, overlay, ms)
            tele.swap()
            self._tele_applied_this_tick = True
            self._burst_len += 1         # this gesture apply is part of a burst

    def _request_crisp(self) -> None:
        if self.renderer is None:
            return
        self._req_q.put({
            "kind": "crisp",
            "generation": self.state.next_generation(),
            "camera": self.state.camera.to_dict(),
            "vw": self.state.vw, "vh": self.state.vh,
            "bloom": bool(self._bloom_var.get()),
            "mode": self._render_mode,
            "tint": self.state.tint_spec(),   # immutable TintSpec -> thread-safe
            # Hashable tuple (or None when the layer is off -> byte-identical to
            # pre-bridge output); travels to the worker and into _request_sig.
            "bridges": self.state.bridges if self._layer_on("bridges") else None,
        })

    def _request_gesture_frame(self) -> None:
        """Enqueue a WORKER-served gesture (quick) frame for the LIVE camera
        (Task 24, P1) -- replaces the old main-thread _show_gesture_frame. The
        worker crops+smoothscales its FrameCache (~11 ms off-main) and posts a
        ('gesture', gen, ppm, ms, cam) result that _drain_results applies with
        only PhotoImage + itemconfig (~18 ms main). Bumps the generation so any
        in-flight crisp is invalidated AND this gesture is generation-tagged (a
        stale result drops in _apply_gesture_frame). Clears the duplicate-
        suppression sig: the canvas no longer shows the applied crisp, so a settle
        request that lands back on the exact same camera (wheel in+out round-trip)
        re-renders crisp instead of being suppressed (Task 18). Marks a gesture
        in flight for the pacing gate; the request carries the camera SNAPSHOT so
        the async apply can realign the base image with the live camera."""
        if self.renderer is None:
            return
        gen = self.state.next_generation()
        self._applied_sig = None
        self._gesture_inflight = True
        self._req_q.put({
            "kind": "gesture",
            "generation": gen,
            "camera": self.state.camera.to_dict(),
            "vw": self.state.vw, "vh": self.state.vh,
        })

    # ---- tick loop ---------------------------------------------------------------
    def _effective_gesture_interval(self, now_ms: float) -> float:
        """The gesture-apply min interval in force at now_ms. While an adaptive
        conservation window (armed by _m4_note_apply on a working-set stall) is open
        the gate widens to CONSERVATION_INTERVAL_MS (~12 fps); otherwise the healthy
        ~33 fps default. Single source of truth = self._conserve_until_ms, so entry/
        exit can never disagree with what the detector last recorded. Pure/testable."""
        if now_ms < self._conserve_until_ms:
            return CONSERVATION_INTERVAL_MS
        return self._healthy_interval_ms

    def _m4_note_apply(self, dur_ms: float, now_ms: float) -> None:
        """Adaptive conservation-pacing stall detector (Task 26, M4). Always on and
        telemetry-independent: sampled once per frame APPLY (the ~1.4 us
        GetProcessMemoryInfo probe Task 25 validated), it reads this process's working
        set and compares it to the previous apply's. A STALL -- the apply took longer
        than STALL_MS AND the working set grew more than STALL_WS_KB -- is the
        fingerprint of the periodic Windows working-set-growth freeze Task 25
        convicted, and arms (or rolls forward) a CONSERVATION_HOLD_MS window during
        which the gate drops the gesture-apply rate to ~12 fps (measured 3.6x fewer
        >200 ms freezes). A box whose applies never grow the working set past the gate
        never arms it and keeps the smooth ~33 fps glide. Wedge-proof: _proc_mem
        returns None on any non-Windows / ctypes failure -> the detector idles (never
        conserves, never raises); the whole body is defensively wrapped so a probe
        hiccup can never break the apply path. Called AFTER the timed apply window so
        its ~1.4 us cost is never attributed to a stage, and only during active
        gestures (nothing samples while the map sits still)."""
        try:
            ws_kb, _pf = _proc_mem()
            if ws_kb is None:
                return                       # probe unavailable -> detector idle
            last = self._m4_ws_last
            self._m4_ws_last = ws_kb
            if last is None:
                return                       # first sample: establish the baseline only
            if dur_ms > STALL_MS and (ws_kb - last) > STALL_WS_KB:
                self._conserve_until_ms = now_ms + CONSERVATION_HOLD_MS
        except Exception:
            pass                             # detector must never break the apply path

    def _gesture_gate_open(self, now_ms: float) -> bool:
        """P2 pacing gate for glide gesture frames: open only when the previous
        gesture apply has completed (in-flight flag cleared) AND at least the
        effective interval (healthy ~33 fps, or the M4 conservation ~12 fps floor
        while a stall window is open) has elapsed since the last request. Pure/
        testable."""
        return (not self._gesture_inflight
                and now_ms - self._last_gesture_req_ms
                    >= self._effective_gesture_interval(now_ms))

    def _schedule_tick(self) -> None:
        if not self._tick_scheduled and self._visible:
            self._tick_scheduled = True
            # Keep the after() id so on_hidden can cancel a pending tick (Task 22).
            self._tick_after_id = self.frame.after(TICK_MS, self._tick)

    # ---- main-loop stall sentinel (Task 27) ----------------------------------
    def _schedule_stall_sentinel(self) -> None:
        """Arm the next stall-sentinel beat. Mirrors _schedule_tick: admits exactly one
        pending beat, only while visible, and stamps the schedule time so the beat can
        measure its own scheduling delay. Cancelled (and its baselines dropped) on hide,
        so a hidden tab spends nothing here."""
        if not self._stall_sentinel_scheduled and self._visible:
            self._stall_sentinel_scheduled = True
            self._stall_sentinel_last_ms = _now_ms()
            try:
                self._stall_sentinel_after_id = self.frame.after(
                    int(STALL_SENTINEL_MS), self._stall_sentinel_beat)
            except Exception:
                self._stall_sentinel_scheduled = False

    def _stall_sentinel_beat(self) -> None:
        """One heartbeat of the main-loop stall sentinel (Task 27). Measures how late
        this beat fired versus its scheduled STALL_SENTINEL_MS interval -- a delay that
        captures a stall landing on ANY main-thread op (the _on_drag_move / overlay
        redraw a sustained pan spends most of its time in, which the apply-sampled M4
        detector never sees) -- then delegates the arm decision to the pure
        _stall_sentinel_note and reschedules. The hidden early-return stops the loop on
        hide exactly like _tick."""
        self._stall_sentinel_scheduled = False
        if not self._visible:
            return
        now = _now_ms()
        last = self._stall_sentinel_last_ms
        observed = (now - last) if last is not None else float(STALL_SENTINEL_MS)
        ws_delta, armed = self._stall_sentinel_note(observed, now)
        tele = self._tele
        if tele is not None:
            try:
                tele.sentinel_rec(tele._ms(), observed, ws_delta, armed)
            except Exception:
                pass
        self._schedule_stall_sentinel()

    def _stall_sentinel_note(self, observed_ms: float, now_ms: float) -> tuple:
        """Pure stall-sentinel decision, split from the Tk timer so it is unit-testable
        (mirrors _m4_note_apply). Samples this process's working set and arms the SAME
        conservation window M4 does iff the beat landed late by more than STALL_MS on
        top of its interval AND the working set grew past STALL_WS_KB since the previous
        beat. Arming only assigns _conserve_until_ms = now + CONSERVATION_HOLD_MS, which
        is idempotent with M4's arm (monotonic now -> a later arm from either source
        only ever rolls the window forward, never shrinks it -- no double-arm guard
        needed). Returns (ws_delta_kb|None, armed) for telemetry. Wedge-proof: a
        _proc_mem probe that is unavailable (None) or throws yields (…, False), never
        arms, never raises into the main loop."""
        ws_delta = None
        try:
            ws_kb, _pf = _proc_mem()
            if ws_kb is None:
                return (None, False)             # probe unavailable -> sentinel idle
            last = self._stall_sentinel_ws_last
            self._stall_sentinel_ws_last = ws_kb
            if last is None:
                return (None, False)             # first beat: establish the baseline only
            ws_delta = ws_kb - last
            if (observed_ms - STALL_SENTINEL_MS) > STALL_MS and ws_delta > STALL_WS_KB:
                self._conserve_until_ms = now_ms + CONSERVATION_HOLD_MS
                return (ws_delta, True)
        except Exception:
            return (ws_delta, False)             # must never break the main loop
        return (ws_delta, False)

    def _tick(self) -> None:
        self._tick_scheduled = False
        if not self._visible:
            return
        # Reschedule FIRST so the next tick's TICK_MS timer runs CONCURRENT with
        # this tick's drain/anim/settle work instead of being tacked on after it:
        # cadence becomes max(TICK_MS, work) rather than TICK_MS + work, so the
        # ~16 ms after-delay no longer stacks on top of the ~8-17 ms frame cost
        # during a zoom glide (Task 20b pacing fix). The hidden early-return above
        # still stops rescheduling when the tab is hidden, and _tick_scheduled
        # (cleared at entry, set again here) still admits exactly one pending tick
        # -- the work below touches neither flag, so this is a pure reorder.
        self._schedule_tick()
        tele = self._tele
        _c = time.perf_counter if tele is not None else None
        if _c:
            self._tele_applied_this_tick = False
            _k0 = _c()
        self._drain_results()                    # apply finished frames (main thread)
        if _c: _k1 = _c()
        verdict = self.state.tick(_now_ms())
        if _c: _k2 = _c()
        if verdict == "anim":
            # Glide active (Task 24). Enqueue a WORKER gesture frame -- but PACE it
            # (P2): only when the previous gesture apply has completed AND
            # >= GESTURE_MIN_INTERVAL_MS elapsed since the last request. When the
            # gate is closed the camera still advanced (state.tick above), so the
            # NEXT request samples the newer camera -- skip-when-behind for free,
            # ~30 fps instead of queued 60 fps jank. Empty cache (first show, no
            # crisp yet) -> the worker returns ppm None and the previous crisp
            # stays while the camera keeps moving; the settle crisp lands at the
            # end (no black flash). Each gesture enqueue clears _applied_sig so the
            # post-glide crisp is never duplicate-suppressed (Task 18).
            now = _now_ms()
            if self._gesture_gate_open(now):
                self._last_gesture_req_ms = now
                self._request_gesture_frame()
        elif verdict == "crisp":
            self._request_crisp()
        if _c:
            _k3 = _c()
            phase = ("anim" if verdict == "anim"
                     else "drain-apply" if self._tele_applied_this_tick else "idle")
            tele.tick({"t": tele._ms(), "phase": phase, "verdict": verdict,
                       "tick_ms": (_k3 - _k0) * 1000.0,
                       "drain_ms": (_k1 - _k0) * 1000.0,
                       "state_tick_ms": (_k2 - _k1) * 1000.0})

    def _drain_results(self) -> None:
        """Coalesce worker output on the main thread. Every result is a tuple led
        by a string tag: ('crisp', gen, ppm, ms, sig), ('gesture', gen, ppm|None,
        ms, cam) and ('threat', frozenset). Frames (crisp OR gesture) share one
        latest-wins slot -- the worker produces them in strictly increasing
        generation order, so the last one drained has the highest generation and
        any earlier one it superseded would be dropped by the is_current check in
        the apply anyway (a queued crisp thus correctly supersedes older gesture
        frames -- it also carries a newer cache). Threat results are kept in a
        SEPARATE slot so a threat is never dropped by frame coalescing (nor a
        frame misread as threat). Dispatch by tag: crisp and gesture take
        different apply paths (sig vs base-image realignment)."""
        latest_frame = None
        latest_threat = None
        try:
            while True:
                item = self._result_q.get_nowait()
                if item[0] == "threat":
                    latest_threat = item
                else:
                    latest_frame = item          # 'crisp' or 'gesture' (latest-wins)
        except queue.Empty:
            pass
        if latest_frame is not None:
            if latest_frame[0] == "crisp":
                self._apply_crisp_frame(*latest_frame[1:])
            else:
                self._apply_gesture_frame(*latest_frame[1:])
        if latest_threat is not None:
            self.set_threat(latest_threat[1])

    # ---- events --------------------------------------------------------------------
    def _on_mousewheel(self, event) -> str:
        steps = 1 if event.delta > 0 else -1
        if self._zoom_anim_var.get():
            # Eased glide (default): retarget only -- the tick loop owns every
            # animation frame (paced worker gesture frames + settle crisp), so the
            # wheel just (re)arms the animator and ensures the loop is running. One
            # code path keeps the motion continuous instead of an instant jump.
            self.state.on_wheel(steps, event.x, event.y, _now_ms())
        else:
            # Instant snap (P3 escape hatch, zoom_animation=False): move the camera
            # THIS instant and enqueue ONE worker gesture frame for immediate
            # feedback; the tick loop then settles to a crisp ~SETTLE_MS later.
            # The pre-Phase-F feel, routed through the new P1 pipeline.
            self.state.zoom_instant(steps, event.x, event.y, _now_ms())
            self._request_gesture_frame()
        self._schedule_tick()
        return "break"

    def _on_drag_start(self, event) -> None:
        self._drag_last = (event.x, event.y)

    def _on_drag_move(self, event) -> None:
        if self._drag_last is None:
            return
        dx = event.x - self._drag_last[0]
        dy = event.y - self._drag_last[1]
        self._drag_last = (event.x, event.y)
        self.state.on_drag(dx, dy, _now_ms())
        ox, oy = self._img_offset
        self._img_offset = (ox + dx, oy + dy)
        self.canvas.coords(self._img_item, *self._img_offset)   # cheap live pan
        # ALSO request a throttled real-content frame through the WORKER gesture
        # pipeline (Task 24 P1): the slide above covers the sub-frame window; the
        # async quick frame swaps in real margin content (Task 17) realigned to the
        # live camera by _apply_gesture_frame -- for a pure pan the realign offset
        # EQUALS the accumulated live-pan slide, so the swap is position-continuous
        # (no black edge within +/-MARGIN). Gate on the EFFECTIVE gesture interval AND
        # the in-flight flag so a fast drag never piles requests. Task 27: read
        # _effective_gesture_interval (not a hardcoded 33 ms) so a SUSTAINED pan honours
        # conservation -- a healthy box keeps ~30 fps pan frames, but while a stall
        # window is armed (by M4 or the new main-loop sentinel) the pan drops to the
        # ~12 fps conservation floor exactly like a zoom glide. Without this the pan
        # kept requesting at full rate even while conservation was armed (measured:
        # apply-gap held ~47 ms / ~21 fps while 71% of applies were flagged conserve).
        now = _now_ms()
        if (not self._gesture_inflight
                and now - self._last_drag_qf >= self._effective_gesture_interval(now)):
            self._last_drag_qf = now
            self._request_gesture_frame()
        self._redraw_overlays()             # keep overlays glued to the live camera
        self._schedule_tick()

    def _on_drag_end(self, _event) -> None:
        self._drag_last = None
        self._schedule_tick()

    def _on_motion(self, event) -> None:
        """Hover: hit-test the cursor system and draw a ring + label. Task 25 cleared
        A2 (hover churn) as a spike cause -- per-<Motion> cost measured p95 ~0.3 ms
        even at close zoom with the cursor riding systems, and the M2 diet (throttle +
        persistent items) was A/B-rejected -- so the simple delete+recreate path
        stands. The split hit-test/draw telemetry is kept as the standing instrument."""
        if self.renderer is None:
            return
        tele = self._tele
        _c = time.perf_counter if tele is not None else None
        if _c: _h0 = _c()
        sid = self.state.hover_hit(event.x, event.y)
        if _c: _h1 = _c()
        self.canvas.delete("hover")
        if sid is not None:
            s = self.state.model.systems[sid]
            # The live camera projection already equals the on-canvas position of the
            # translated stale image (pan updates the camera live while the image item
            # is offset to match), so do NOT add _img_offset -- that would overshoot
            # by the drag distance during the pre-settle window.
            sx, sy = self.state.camera.world_to_screen(s.x, s.y,
                                                       self.state.vw, self.state.vh)
            self.canvas.create_oval(sx - 9, sy - 9, sx + 9, sy + 9,
                                    outline="#e0e0e0", width=1, tags="hover")
            label = f"{s.name}  {s.sec:.1f}"
            ov = self.state.range_overlay
            if ov is not None:                        # enrich with range info
                ly = ov.distances.get(sid)
                if ly is not None:
                    label += f" · {ly:.1f} ly"
                if sid in ov.illegal:
                    label += " · ILLEGAL DEST"
            self.canvas.create_text(sx + 12, sy - 12, anchor="w", tags="hover",
                                    text=label, fill="#e0e0e0",
                                    font=("Segoe UI", 9))
        if _c:
            _h2 = _c()
            tele.hover_rec((_h1 - _h0) * 1000.0, (_h2 - _h1) * 1000.0, sid is not None)

    def _on_right_click(self, event) -> None:
        sid = self.state.hover_hit(event.x, event.y) if self.renderer else None
        menu = (self._build_system_menu(sid) if sid is not None
                else self._build_empty_menu())
        menu.tk_popup(event.x_root, event.y_root)

    def _make_menu(self, parent) -> tk.Menu:
        """Factory for EVERY map context menu AND cascade so they all carry the
        app palette (owner ask 2026-07-10: match the rest of the application).
        Mirrors the intel system menu's dark idiom (panel bg + text fg, tearoff=0)
        and extends it with a flat, accent-lit treatment — disabled titles in
        accent, a solid selection bar, no bevel — so the popups read as native to
        the dark app instead of the OS-default light menu the map used before.
        On Windows tk popup menus honor bg/fg/active*/relief/borderwidth (asserted
        in tests/test_map_theme.py via cget)."""
        t = self.theme
        return tk.Menu(
            parent, tearoff=0,
            bg=t["panel"], fg=t["fg"],
            activebackground=t["entry_bg"], activeforeground=t["fg"],
            disabledforeground=t["accent"],
            borderwidth=0, activeborderwidth=0, relief="flat",
        )

    def _build_system_menu(self, sid: int) -> tk.Menu:
        """Right-click-on-a-system menu (spec §2.5): range submenu, clear-range,
        then the callback-gated system actions incl. staging adds."""
        name = self.state.model.systems[sid].name
        menu = self._make_menu(self.canvas)
        menu.add_command(label=name, state="disabled")
        menu.add_separator()
        # Jump range submenu: 5 grouped classes (live LY) + Custom.
        range_menu = self._make_menu(menu)
        for label, ly in mo.range_options():
            range_menu.add_command(
                label=label,
                command=lambda s=sid, y=ly, l=label: self._menu_apply_range(s, y, l))
        range_menu.add_separator()
        range_menu.add_command(label="Custom…",
                               command=lambda s=sid: self._menu_custom_range(s))
        menu.add_cascade(label="Jump range from here", menu=range_menu)
        if self.state.range_overlay is not None:
            menu.add_command(label="Clear range overlay",
                             command=self.clear_range_overlay)
        menu.add_separator()
        self._menu_add(menu, "Set destination", "set_destination", name)
        self._menu_add(menu, "Open in Dotlan", "open_dotlan", name)
        self._menu_add(menu, "Navigate WH route", "navigate_wh", name)
        self._menu_add(menu, "Titan bridge check", "titan_bridge", name)
        self._menu_add(menu, "Copy name", "copy", name)
        self._menu_add(menu, "Add to friendly staging", "add_friendly_staging", name)
        self._menu_add(menu, "Add to hostile staging", "add_hostile_staging", name)
        return menu

    def _build_empty_menu(self) -> tk.Menu:
        """Right-click-on-empty-space menu: view + layer controls."""
        menu = self._make_menu(self.canvas)
        menu.add_command(label="Fit universe", command=self._fit_universe)
        menu.add_checkbutton(label="Bloom", variable=self._bloom_var,
                             command=self._on_bloom_toggle)
        menu.add_checkbutton(label="Zoom animation", variable=self._zoom_anim_var,
                             command=self._on_zoom_anim_toggle)
        menu.add_separator()
        menu.add_checkbutton(label="Threat projection",
                             variable=self._layer_vars["threat"],
                             command=lambda: self._on_layer_toggle("threat"))
        # Keep the radio selection in sync with the persisted threat ship.
        opts = mo.threat_options()
        current = self.cfg.get("threat_ship", "Titan Bridge")
        sel = next((l for l, _ in opts if self._strip_ly_suffix(l) == current),
                   opts[0][0] if opts else "")
        self._threat_var.set(sel)
        threat_menu = self._make_menu(menu)
        for label, _ly in opts:
            threat_menu.add_radiobutton(
                label=label, variable=self._threat_var, value=label,
                command=lambda l=label: self._on_threat_ship(l))
        menu.add_cascade(label="Threat ship", menu=threat_menu)
        menu.add_separator()
        menu.add_checkbutton(label="Fleet", variable=self._layer_vars["fleet"],
                             command=lambda: self._on_layer_toggle("fleet"))
        menu.add_checkbutton(label="Staging", variable=self._layer_vars["staging"],
                             command=lambda: self._on_layer_toggle("staging"))
        menu.add_checkbutton(label="Bridges", variable=self._layer_vars["bridges"],
                             command=lambda: self._on_layer_toggle("bridges"))
        return menu

    def _menu_add(self, menu, label: str, key: str, name: str) -> None:
        cb = self.callbacks.get(key)
        if cb is not None:
            menu.add_command(label=label, command=lambda: cb(name))

    # ---- range / threat menu helpers -------------------------------------------
    def _menu_apply_range(self, sid: int, ly: float, label: str) -> None:
        ov = self.state.compute_range_overlay(sid, ly, label)
        self.apply_range_overlay(ov)
        self.cfg.setdefault("layers", {})["range"] = True   # auto-enable the layer
        rv = self._layer_vars.get("range")
        if rv is not None:
            rv.set(True)

    def _menu_custom_range(self, sid: int) -> None:
        from tkinter import simpledialog
        ly = simpledialog.askfloat("Custom jump range", "Range (ly):",
                                   parent=self.frame, minvalue=0.0, maxvalue=40.0)
        if ly is None or ly <= 0 or ly > 40:       # clamp to (0, 40] (decision log)
            return
        self._menu_apply_range(sid, ly, f"Custom ({ly:.1f} ly)")

    def _strip_ly_suffix(self, label: str) -> str:
        return _LY_SUFFIX.sub("", label).strip()

    def _on_threat_ship(self, label: str) -> None:
        self.cfg["threat_ship"] = self._strip_ly_suffix(label)   # store base name
        self._threat_var.set(label)
        self._recompute_threat()

    def _threat_ly(self) -> float:
        """LY for the configured threat ship. The grouped option base-labels are
        NOT SHIP_RANGES keys, so match them against threat_options first, then
        fall back to ly_for_ship (real ship names + Titan Bridge), then default."""
        ship = self.cfg.get("threat_ship", "Titan Bridge")
        for label, ly in mo.threat_options():
            if self._strip_ly_suffix(label) == ship:
                return ly
        return mo.ly_for_ship(ship)

    def _on_layer_toggle(self, key: str) -> None:
        var = self._layer_vars.get(key)
        if var is None:
            return
        self.cfg.setdefault("layers", {})[key] = bool(var.get())
        if key == "threat":
            self._recompute_threat()
        elif key == "bridges":
            # Bridges live in the base bitmap, so a toggle needs a settle
            # re-render (the request carries the pairs, or None when off). The
            # bridges key in _request_sig keeps this from being duplicate-suppressed.
            self.state.force_dirty()
            self._request_crisp()
            self._redraw_overlays()
        else:
            self._redraw_overlays()

    def _recompute_threat(self) -> None:
        """Recompute the hostile-staging threat halo. Off / no staging -> clear.
        Otherwise run the (per-staging ~5.5k-system) scan on a helper thread that
        touches NO Tcl, feeding the result back through the main-thread result
        queue as a ("threat", frozenset) tuple (drained by _drain_results)."""
        tv = self._layer_vars.get("threat")
        on = bool(tv.get()) if tv is not None else False
        hostile = set(self.state.hostile_staging)
        if not on or not hostile:
            self.set_threat(None)
            return
        ly = self._threat_ly()

        def work():
            try:
                fset = mo.compute_threat(hostile, ly)
            except Exception as exc:               # never crash the helper thread
                print(f"[MAP] threat recompute failed: {exc}")
                return
            self._result_q.put(("threat", fset))   # applied on the main thread

        threading.Thread(target=work, daemon=True, name="map-threat").start()

    def _on_configure(self, event) -> None:
        self.state.resize(event.width, event.height)
        self._schedule_tick()

    def _on_search(self, _event=None) -> None:
        if self.state.fly_to(self.search_entry.get()):
            self._request_crisp()
        else:
            self.status.config(text="system not found")

    def _fit_universe(self) -> None:
        self.state.camera.fit_bounds(self.state.model.bounds,
                                     self.state.vw, self.state.vh)
        self.state.force_dirty()
        self._request_crisp()

    def _on_bloom_toggle(self) -> None:
        self.state.force_dirty()
        self._request_crisp()

    def _on_zoom_anim_toggle(self) -> None:
        """P3: persist the zoom-animation preference to cfg immediately (also
        re-saved on hide). No re-render needed -- it only changes how the NEXT
        wheel notch behaves (eased glide vs instant snap)."""
        self.cfg["zoom_animation"] = bool(self._zoom_anim_var.get())
