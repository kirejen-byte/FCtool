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
import threading
import time
import tkinter as tk

import map_camera as mc
import map_data
import map_render as mr

ZOOM_STEP = 1.15
SETTLE_MS = 120.0
TICK_MS = 60
BG_HEX = "#0a0a14"


def _now_ms() -> float:
    return time.monotonic() * 1000.0


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
        self._visible = False
        self._tick_scheduled = False
        self._drag_last: tuple[int, int] | None = None
        self._img_offset = (0.0, 0.0)

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
        self.cfg.update(merged)
        self.save_cfg(merged)

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
            cam = mc.Camera.from_dict(req["camera"])
            t0 = time.perf_counter()
            mode = req["mode"]
            if mode == "auto":
                mode = self.stats.suggest_mode()
            surf = self.renderer.render(cam, req["vw"], req["vh"],
                                        bloom=req["bloom"], mode=mode)
            ms = (time.perf_counter() - t0) * 1000.0
            self.stats.record(ms)
            ppm = mr.surface_to_ppm(surf)
            # Hand off to the main thread (no Tcl calls here); _tick applies it.
            self._result_q.put((req["generation"], ppm, surf, cam,
                                req["vw"], req["vh"], ms))

    def _apply_frame(self, generation: int, ppm: bytes, surf, cam,
                     vw: int, vh: int, ms: float) -> None:
        if not self._visible or not self.state.is_current(generation):
            return
        self._photo = tk.PhotoImage(data=ppm)
        self.canvas.itemconfig(self._img_item, image=self._photo)
        self.canvas.coords(self._img_item, 0, 0)
        self._img_offset = (0.0, 0.0)
        self.frame_cache.store(surf, cam, vw, vh)
        self.status.config(text=f"render {ms:.0f} ms")

    def _request_crisp(self) -> None:
        if self.renderer is None:
            return
        self._req_q.put({
            "generation": self.state.next_generation(),
            "camera": self.state.camera.to_dict(),
            "vw": self.state.vw, "vh": self.state.vh,
            "bloom": bool(self._bloom_var.get()),
            "mode": self._render_mode,
        })

    def _show_gesture_frame(self) -> None:
        quick = self.frame_cache.quick_frame(self.state.camera,
                                             self.state.vw, self.state.vh)
        if quick is not None:
            self.state.next_generation()          # invalidate in-flight crisp
            self._photo = tk.PhotoImage(data=mr.surface_to_ppm(quick))
            self.canvas.itemconfig(self._img_item, image=self._photo)
            self.canvas.coords(self._img_item, 0, 0)
            self._img_offset = (0.0, 0.0)

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
        """Coalesce worker output to the newest frame and apply it here on the
        main thread (the generation check in _apply_frame drops stale ones)."""
        latest = None
        try:
            while True:
                latest = self._result_q.get_nowait()
        except queue.Empty:
            pass
        if latest is not None:
            self._apply_frame(*latest)

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
            self.canvas.create_text(sx + 12, sy - 12, anchor="w", tags="hover",
                                    text=f"{s.name}  {s.sec:.1f}", fill="#e0e0e0",
                                    font=("Segoe UI", 9))

    def _on_right_click(self, event) -> None:
        sid = self.state.hover_hit(event.x, event.y) if self.renderer else None
        menu = tk.Menu(self.canvas, tearoff=0)
        if sid is not None:
            name = self.state.model.systems[sid].name
            menu.add_command(label=name, state="disabled")
            menu.add_separator()
            self._menu_add(menu, "Set destination", "set_destination", name)
            self._menu_add(menu, "Open in Dotlan", "open_dotlan", name)
            self._menu_add(menu, "Navigate WH route", "navigate_wh", name)
            self._menu_add(menu, "Titan bridge check", "titan_bridge", name)
            self._menu_add(menu, "Copy name", "copy", name)
        else:
            menu.add_command(label="Fit universe", command=self._fit_universe)
            menu.add_checkbutton(label="Bloom", variable=self._bloom_var,
                                 command=self._on_bloom_toggle)
        menu.tk_popup(event.x_root, event.y_root)

    def _menu_add(self, menu, label: str, key: str, name: str) -> None:
        cb = self.callbacks.get(key)
        if cb is not None:
            menu.add_command(label=label, command=lambda: cb(name))

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
