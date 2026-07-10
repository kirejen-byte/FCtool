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

import queue
import re
import threading
import time
import tkinter as tk

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
# Worker renders this many px beyond the viewport on every side (Task 17). Tk
# still shows a viewport-sized center crop, but the FULL margined frame is cached
# so pan / zoom-out serve real content instead of a black edge within +/-MARGIN.
MARGIN = 224
BG_HEX = "#0a0a14"

# Strips the " (X.X ly)" annotation the range/threat option labels carry so a
# menu selection can be stored back as a bare ship/base name (spec §2.5).
_LY_SUFFIX = re.compile(r"\s*\([^()]*ly\)\s*$")


def _now_ms() -> float:
    return time.monotonic() * 1000.0


def _request_sig(req: dict) -> tuple:
    """Comparable signature of a render request: camera center/scale + viewport
    size + bloom + mode + tint (NOT generation -- that is a staleness token, and
    duplicates by definition carry different generations). Used by the worker to
    suppress a settle re-render that duplicates the crisp frame already applied
    to the canvas (Task 18 Step 1b; see MapTab._applied_sig).

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
            bool(req["bloom"]), req["mode"], req.get("tint"))


class MapTabState:
    """Pure camera/gesture/generation bookkeeping (headless-testable)."""

    def __init__(self, vw: int, vh: int) -> None:
        self.vw = vw
        self.vh = vh
        self.model = None
        self.camera = mc.Camera()
        self.gesture = mc.GestureTracker(settle_ms=SETTLE_MS)
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
        factor = ZOOM_STEP ** delta_steps
        self.camera.zoom_at(factor, sx, sy, self.vw, self.vh)
        self.gesture.touch(now_ms)
        self._dirty = True
        return "gesture"

    def on_drag(self, dx: float, dy: float, now_ms: float | None = None) -> None:
        self.camera.pan_pixels(dx, dy)
        if now_ms is not None:
            self.gesture.touch(now_ms)
        self._dirty = True

    def tick(self, now_ms: float) -> str | None:
        """Called periodically; returns 'crisp' when a settled re-render is due."""
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


class MapTab:
    """Tk widget/controller. Host wires: frame into a Notebook tab,
    on_shown/on_hidden from <<NotebookTabChanged>>, cfg dict + save_cfg,
    callbacks for context-menu actions (all optional):
      set_destination(name), open_dotlan(name), navigate_wh(name),
      titan_bridge(name), copy(name)."""

    def __init__(self, parent, *, model_loader=map_data.load_map_model,
                 cfg: dict | None = None, save_cfg=None, callbacks: dict | None = None,
                 autocomplete_cls=None) -> None:
        self.cfg = cfg or {}
        self.save_cfg = save_cfg or (lambda d: None)
        self.callbacks = callbacks or {}
        self._model_loader = model_loader

        self.frame = tk.Frame(parent, bg=BG_HEX)
        bar = tk.Frame(self.frame, bg=BG_HEX)
        bar.pack(side="top", fill="x")
        tk.Label(bar, text="Search:", bg=BG_HEX, fg="#8b9bb5").pack(side="left", padx=(6, 2))
        entry_cls = autocomplete_cls or tk.Entry
        self.search_entry = entry_cls(bar, width=24)
        self.search_entry.pack(side="left", padx=2, pady=3)
        self.search_entry.bind("<Return>", self._on_search)
        self._bloom_var = tk.BooleanVar(value=bool(self.cfg.get("bloom", True)))
        tk.Checkbutton(bar, text="Bloom", variable=self._bloom_var, bg=BG_HEX,
                       fg="#8b9bb5", selectcolor="#16213e",
                       command=self._on_bloom_toggle).pack(side="left", padx=8)
        # --- Phase D layer toggles (fleet/staging/threat) ---
        _layers = self.cfg.get("layers", {})
        self._layer_vars: dict[str, tk.BooleanVar] = {
            "fleet": tk.BooleanVar(value=bool(_layers.get("fleet", True))),
            "staging": tk.BooleanVar(value=bool(_layers.get("staging", True))),
            "threat": tk.BooleanVar(value=bool(_layers.get("threat", False))),
        }
        # Current threat-ship selection label (radiobutton var; synced lazily
        # from cfg["threat_ship"] when the empty-space menu is built).
        self._threat_var = tk.StringVar()
        for _key, _text in (("fleet", "Fleet"), ("staging", "Staging"),
                            ("threat", "Threat")):
            tk.Checkbutton(bar, text=_text, variable=self._layer_vars[_key],
                           bg=BG_HEX, fg="#8b9bb5", selectcolor="#16213e",
                           command=lambda k=_key: self._on_layer_toggle(k)
                           ).pack(side="left", padx=4)
        self.status = tk.Label(bar, text="", bg=BG_HEX, fg="#8b9bb5", anchor="e")
        self.status.pack(side="right", padx=6)

        self.canvas = tk.Canvas(self.frame, bg=BG_HEX, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self._img_item = self.canvas.create_image(0, 0, anchor="nw")
        self._photo = None                      # keep a ref or Tk drops the image

        self.state = MapTabState(vw=800, vh=600)
        self.renderer = None
        self.frame_cache = mr.FrameCache()
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
        # main thread only (_apply_frame records it; _show_gesture_frame clears it
        # when a quick frame replaces the crisp); READ by the worker (GIL-atomic
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
        self._drag_last: tuple[int, int] | None = None
        self._img_offset = (0.0, 0.0)
        self._last_drag_qf = 0.0        # last drag quick-frame time (throttle, Task 17)

        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<ButtonPress-1>", self._on_drag_start)
        self.canvas.bind("<B1-Motion>", self._on_drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._on_drag_end)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Button-3>", self._on_right_click)
        self.canvas.bind("<Configure>", self._on_configure)

    # ---- lifecycle ------------------------------------------------------------
    def on_shown(self) -> None:
        self._visible = True
        if self.renderer is None:
            model = self._model_loader()
            self.state.attach_model(model)
            self.state.restore_camera(self.cfg.get("camera") or {})
            self.renderer = mr.Renderer(model)
            self._start_worker()
        self.state.force_dirty()
        self._request_crisp()
        self._schedule_tick()

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
        # Persist the live layer-toggle state (range flag is overlay-driven and
        # already carried on self.cfg["layers"]; the 3 vars mirror the toolbar).
        layers = dict(self.cfg.get("layers", {}))
        for key, var in self._layer_vars.items():
            layers[key] = bool(var.get())
        merged["layers"] = layers
        self.cfg.update(merged)
        self.save_cfg(merged)

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
        #2). Pure Tk; cheap enough to run on every gesture/drag frame."""
        canvas = self.canvas
        for tag in ("ov_fleet", "ov_staging", "ov_illegal", "ov_range_strike",
                    "ov_origin", "ov_own"):
            canvas.delete(tag)
        if self.renderer is None or self.state.model is None:
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

    # ---- worker ----------------------------------------------------------------
    def _start_worker(self) -> None:
        if self._worker is None:
            self._worker = threading.Thread(target=self._worker_loop, daemon=True,
                                            name="map-render")
            self._worker.start()

    def _worker_loop(self) -> None:
        while True:
            req = self._req_q.get()
            try:                                   # drain to latest (coalesce)
                while True:
                    req = self._req_q.get_nowait()
            except queue.Empty:
                pass
            result = self._render_locked(req)      # None => duplicate suppressed
            if result is not None:
                self._result_q.put(result)

    def _render_locked(self, req: dict) -> tuple | None:
        """Render one coalesced request on the worker thread, or return None when
        it exactly duplicates the crisp frame already APPLIED to the canvas
        (camera + size + bloom + mode + tint) -- duplicate-settle suppression
        (Task 18 Step 1b): that exact view is on screen, so re-rendering is
        wasted work. The sig travels in the result tuple; _apply_frame records
        it only when the frame actually lands (visible + current generation), so
        a dropped frame can never block its own re-request (see _applied_sig).
        Kept as a method (not inline in _worker_loop) so a duplicate-suppression
        test can drive it directly without the render thread. Touches no Tcl."""
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
                                    tint=req.get("tint"))
        ms = (time.perf_counter() - t0) * 1000.0
        self.stats.record(ms)
        # Tk gets a viewport-sized CENTER CROP (a subsurface view -- no copy;
        # surface_to_ppm reads it directly). Build it transiently so the parent
        # surf is not left subsurface-locked across the queue hand-off.
        ppm = mr.surface_to_ppm(mr.center_subsurface(surf, MARGIN, vw, vh))
        # Hand off to the main thread (no Tcl calls here); _tick applies it.
        # surf is the FULL margined frame -> _apply_frame caches it (view dims).
        return (req["generation"], ppm, surf, cam, vw, vh, ms, sig)

    def _apply_frame(self, generation: int, ppm: bytes, surf, cam,
                     vw: int, vh: int, ms: float, sig: tuple | None = None) -> None:
        if not self._visible or not self.state.is_current(generation):
            return                       # dropped -> _applied_sig stays unrecorded
        self._photo = tk.PhotoImage(data=ppm)   # ppm = viewport-sized center crop
        self.canvas.itemconfig(self._img_item, image=self._photo)
        self.canvas.coords(self._img_item, 0, 0)
        self._img_offset = (0.0, 0.0)
        # surf is the FULL margined frame; store records its surface dims so
        # quick_frame can serve the margin during pan/zoom-out (Task 17).
        self.frame_cache.store(surf, cam, vw, vh)
        self._applied_sig = sig          # this view is now on screen -> the worker
        self.status.config(text=f"render {ms:.0f} ms")  # may suppress duplicates
        self._redraw_overlays()          # reproject Tk overlay items onto the fresh frame

    def _request_crisp(self) -> None:
        if self.renderer is None:
            return
        self._req_q.put({
            "generation": self.state.next_generation(),
            "camera": self.state.camera.to_dict(),
            "vw": self.state.vw, "vh": self.state.vh,
            "bloom": bool(self._bloom_var.get()),
            "mode": self._render_mode,
            "tint": self.state.tint_spec(),   # immutable TintSpec -> thread-safe
        })

    def _show_gesture_frame(self) -> bool:
        """Swap in a FrameCache quick frame for the live camera. Returns True if a
        frame was shown (cache had content), False if the cache was empty. On a
        swap it resets _img_offset to (0, 0): the quick frame is already
        camera-correct, so any drag-slide offset accumulated on the stale image
        must be cleared (Task 17 relies on this)."""
        quick = self.frame_cache.quick_frame(self.state.camera,
                                             self.state.vw, self.state.vh)
        if quick is None:
            return False
        self.state.next_generation()          # invalidate in-flight crisp
        # The canvas no longer shows the applied crisp -> clear the suppression
        # sig, so a settle request that lands back on the exact same camera
        # (e.g. a wheel in+out round-trip) re-renders crisp instead of being
        # suppressed and leaving the smoothscaled quick frame on screen.
        self._applied_sig = None
        self._photo = tk.PhotoImage(data=mr.surface_to_ppm(quick))
        self.canvas.itemconfig(self._img_item, image=self._photo)
        self.canvas.coords(self._img_item, 0, 0)
        self._img_offset = (0.0, 0.0)
        self._redraw_overlays()           # reproject overlays onto the gesture frame
        return True

    # ---- tick loop ---------------------------------------------------------------
    def _schedule_tick(self) -> None:
        if not self._tick_scheduled and self._visible:
            self._tick_scheduled = True
            self.frame.after(TICK_MS, self._tick)

    def _tick(self) -> None:
        self._tick_scheduled = False
        if not self._visible:
            return
        self._drain_results()                    # apply finished frames (main thread)
        if self.state.tick(_now_ms()) == "crisp":
            self._request_crisp()
        self._schedule_tick()

    def _drain_results(self) -> None:
        """Coalesce worker output on the main thread. Two payload shapes share
        the queue: 8-tuple render frames (latest-wins; the generation check in
        _apply_frame drops stale ones; the trailing element is the request sig
        recorded as _applied_sig on a successful apply) and ("threat", frozenset)
        results from the recompute helper thread. Keep them apart so a threat
        result is never dropped by frame coalescing, and so a frame is never
        misread as threat."""
        latest_frame = None
        latest_threat = None
        try:
            while True:
                item = self._result_q.get_nowait()
                if len(item) == 2 and item[0] == "threat":
                    latest_threat = item
                else:
                    latest_frame = item
        except queue.Empty:
            pass
        if latest_frame is not None:
            self._apply_frame(*latest_frame)
        if latest_threat is not None:
            self.set_threat(latest_threat[1])

    # ---- events --------------------------------------------------------------------
    def _on_mousewheel(self, event) -> str:
        steps = 1 if event.delta > 0 else -1
        self.state.on_wheel(steps, event.x, event.y, _now_ms())
        self._show_gesture_frame()
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
        # ALSO serve a throttled real-content frame from the margined cache
        # (Task 17): the slide above covers the 0-33 ms window; a quick frame then
        # swaps in real margin content (resetting _img_offset to 0) so panning
        # shows no black edge within +/-MARGIN. _show_gesture_frame redraws the
        # overlays itself on a swap, so only redraw here when it didn't swap.
        now = _now_ms()
        swapped = False
        if now - self._last_drag_qf >= 33.0:
            self._last_drag_qf = now
            swapped = self._show_gesture_frame()
        if not swapped:
            self._redraw_overlays()                             # keep overlays glued to nodes
        self._schedule_tick()

    def _on_drag_end(self, _event) -> None:
        self._drag_last = None
        self._schedule_tick()

    def _on_motion(self, event) -> None:
        if self.renderer is None:
            return
        sid = self.state.hover_hit(event.x, event.y)
        self.canvas.delete("hover")
        if sid is not None:
            s = self.state.model.systems[sid]
            # The live camera projection already equals the on-canvas position
            # of the translated stale image (pan updates the camera live while
            # the image item is offset to match), so do NOT add _img_offset --
            # that would overshoot by the drag distance during the pre-settle
            # window.
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

    def _on_right_click(self, event) -> None:
        sid = self.state.hover_hit(event.x, event.y) if self.renderer else None
        menu = (self._build_system_menu(sid) if sid is not None
                else self._build_empty_menu())
        menu.tk_popup(event.x_root, event.y_root)

    def _build_system_menu(self, sid: int) -> tk.Menu:
        """Right-click-on-a-system menu (spec §2.5): range submenu, clear-range,
        then the callback-gated system actions incl. staging adds."""
        name = self.state.model.systems[sid].name
        menu = tk.Menu(self.canvas, tearoff=0)
        menu.add_command(label=name, state="disabled")
        menu.add_separator()
        # Jump range submenu: 5 grouped classes (live LY) + Custom.
        range_menu = tk.Menu(menu, tearoff=0)
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
        menu = tk.Menu(self.canvas, tearoff=0)
        menu.add_command(label="Fit universe", command=self._fit_universe)
        menu.add_checkbutton(label="Bloom", variable=self._bloom_var,
                             command=self._on_bloom_toggle)
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
        threat_menu = tk.Menu(menu, tearoff=0)
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
