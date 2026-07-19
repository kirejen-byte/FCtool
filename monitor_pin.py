"""Per-character EVE-client monitor pinning for FCPreview.

Each character can be assigned a target monitor; when that character logs in (or
via the Apply button) FCTool moves the real client window onto that monitor.
Completely inert until the user assigns something.

COORDINATE CONTRACT (load-bearing):
  * Every rectangle here is stored as EDGES ``(left, top, right, bottom)`` — NOT
    ``(x, y, w, h)``. This mirrors ``fc_gui._virtual_screen_bounds()`` and Win32's
    RECT, and the repo has a documented EDGES-vs-xywh confusion hazard, so the
    shape is spelled out on every boundary. ``plan_move`` is the one place that
    converts to/from an ``(x, y, w, h)`` placement (SetWindowPos's shape).
  * All coordinates are PHYSICAL pixels. The process is PMv2 DPI-aware, so
    GetMonitorInfo (enumeration) and SetWindowPos/SetWindowPlacement (the move,
    in ``window_activator``) are both physical and self-consistent. Never mix in
    Tk logical-pixel geometry.

Testability: the real ctypes backend lives behind ``_RealMonitorWin32`` and is
injected as ``win32`` (mirrors ``eve_client_tracker._RealWin32``); every unit
test passes a fake with the same ``enum_monitors()`` surface, so all logic runs
headless.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

# A window is treated as a borderless/"Fixed Window" fill when it covers at least
# this fraction of its current monitor's FULL rect area. EVE's Fixed Window mode
# covers exactly the monitor rect; plain windowed mode covers much less.
FILL_AREA_RATIO = 0.92

# Special per-character sentinel: never move this character (lets a per-char row
# override a global default with "leave me where I am").
NEVER = "none"


@dataclass(frozen=True)
class MonitorInfo:
    """One display. ``rect`` and ``work`` are EDGES ``(left, top, right, bottom)``
    in physical pixels (see the module docstring). ``device`` is the stable Win32
    device name (e.g. ``\\\\.\\DISPLAY1``) — the most durable persistent identity,
    so assignments are stored by device, not by index."""

    device: str
    rect: tuple           # (left, top, right, bottom) — full monitor bounds
    work: tuple           # (left, top, right, bottom) — usable area (taskbar excluded)
    primary: bool = False

    @property
    def rect_size(self) -> tuple:
        l, t, r, b = self.rect
        return (r - l, b - t)

    @property
    def work_size(self) -> tuple:
        l, t, r, b = self.work
        return (r - l, b - t)


# ── enumeration ──────────────────────────────────────────────────────────────
def sort_monitors(monitors) -> list:
    """Stable display order: primary first, then left-to-right by rect origin
    (left, then top). Predictable indices for the human labels."""
    return sorted(monitors, key=lambda m: (not m.primary, m.rect[0], m.rect[1]))


def list_monitors(win32=None) -> list:
    """Enumerate monitors (primary-first, then left-to-right). Injectable backend
    for headless tests. Never raises on an empty/odd config — returns []."""
    w = win32 or _real_monitor_win32()
    try:
        mons = list(w.enum_monitors())
    except Exception:
        return []
    return sort_monitors(mons)


def monitor_label(mon: MonitorInfo, index: int) -> str:
    """Human label, e.g. ``Monitor 1 (primary) — 2560×1440 @ (0,0)``. ``index`` is
    the 1-based display position from ``list_monitors`` ordering."""
    l, t, r, b = mon.rect
    tag = " (primary)" if mon.primary else ""
    return f"Monitor {index}{tag} — {r - l}×{b - t} @ ({l},{t})"


# ── geometry ─────────────────────────────────────────────────────────────────
def _clamp(v: int, lo: int, hi: int) -> int:
    if hi < lo:          # target smaller than the window in this axis → pin to lo
        return lo
    return max(lo, min(v, hi))


def _intersect_area(a: tuple, b: tuple) -> int:
    """Overlap area of two EDGES rects (0 if disjoint)."""
    al, at, ar, ab = a
    bl, bt, br, bb = b
    ix = max(0, min(ar, br) - max(al, bl))
    iy = max(0, min(ab, bb) - max(at, bt))
    return ix * iy


def find_monitor_for_rect(rect_edges: tuple, monitors) -> "MonitorInfo | None":
    """Which monitor a window currently sits on = the one it overlaps most
    (MonitorFromWindow/NEAREST semantics). None if it overlaps none of them."""
    best, best_area = None, 0
    for m in monitors:
        area = _intersect_area(rect_edges, m.rect)
        if area > best_area:
            best, best_area = m, area
    return best


def plan_move(current_rect_edges: tuple, current_monitor: MonitorInfo,
              target_monitor: MonitorInfo) -> tuple:
    """Pure placement policy. Returns an ``(x, y, w, h)`` placement (SetWindowPos's
    shape) for moving a window from ``current_monitor`` onto ``target_monitor``.

    Two branches:
      * FILL — the window covers >= FILL_AREA_RATIO of its current monitor's FULL
        rect area (borderless / EVE "Fixed Window"): return the target monitor's
        FULL rect, so a 2560×1440 fill maps onto a 1920×1080 monitor as a
        1920×1080 fill.
      * PRESERVE — a plain smaller window: keep its size and its offset from the
        monitor origin, then clamp fully inside the target monitor's WORK area
        (and clamp the size down too if it is larger than the work area, so it
        always stays grabbable).

    ``current_rect_edges`` and both monitors carry EDGES rects (see module
    docstring); the return is xywh."""
    l, t, r, b = current_rect_edges
    w = max(0, r - l)
    h = max(0, b - t)

    cm_l, cm_t, cm_r, cm_b = current_monitor.rect
    cm_area = max(0, cm_r - cm_l) * max(0, cm_b - cm_t)

    tm_l, tm_t, tm_r, tm_b = target_monitor.rect
    tm_w = tm_r - tm_l
    tm_h = tm_b - tm_t

    if cm_area > 0 and (w * h) >= FILL_AREA_RATIO * cm_area:
        # Fill the whole target monitor.
        return (tm_l, tm_t, tm_w, tm_h)

    # Preserve: same size (clamped to the work area), same offset from the
    # monitor origin, clamped so the window stays fully inside the work area.
    wl, wt, wr, wb = target_monitor.work
    work_w = wr - wl
    work_h = wb - wt
    nw = min(w, work_w) if work_w > 0 else w
    nh = min(h, work_h) if work_h > 0 else h
    off_x = l - cm_l
    off_y = t - cm_t
    nx = _clamp(tm_l + off_x, wl, wr - nw)
    ny = _clamp(tm_t + off_y, wt, wb - nh)
    return (nx, ny, nw, nh)


def plan_move_for_client(rect_edges: tuple, monitors, target_device: str):
    """Convenience wrapper for the fc_gui trigger: resolve the target monitor by
    device, figure out which monitor the window is currently on, and plan the
    move. Returns an ``(x, y, w, h)`` placement, or None when the target device is
    not currently present (a disconnected/reconfigured monitor → caller skips)."""
    target = next((m for m in monitors if m.device == target_device), None)
    if target is None:
        return None
    # If the window overlaps no known monitor (e.g. it was on a monitor that has
    # since been unplugged) fall back to the target as "current": the clamp in
    # plan_move still lands it grabbable, and a full-size window still fills.
    current = find_monitor_for_rect(rect_edges, monitors) or target
    return plan_move(rect_edges, current, target)


# ── assignment resolution ────────────────────────────────────────────────────
def resolve_assignment(key: str, assignments: dict, default_device: str):
    """Resolve one character key to a target DEVICE string, or None for no move.

    Order: explicit per-char assignment > global default > no move.
      * ``assignments[key] == NEVER`` ("none") → None (blocks the global default).
      * ``assignments[key] == <device>``        → that device.
      * key absent / empty                      → the global default, else None.

    Matching the stored device against a currently-present monitor happens later
    (``plan_move_for_client`` returns None if the device is gone); this function
    is pure config resolution and never touches Win32."""
    val = (assignments or {}).get(key)
    if val == NEVER:
        return None
    if val:
        return val
    return default_device or None


# ── combobox option models (pure; shared by the UI builder + its tests) ───────
DEFAULT_OFF_LABEL = "(off)"
CHAR_DEFAULT_LABEL = "(default)"
CHAR_NEVER_LABEL = "(never move)"


def _missing_label(device: str) -> str:
    return f"(missing) {device}"


def default_combo_options(monitors, current_value: str = "") -> list:
    """[(label, value)] for the global-default combobox. value '' == off. A
    ``current_value`` device that is no longer present is preserved as a trailing
    "(missing) …" option so a temporary disconnect never silently wipes it."""
    opts = [(DEFAULT_OFF_LABEL, "")]
    present = set()
    for i, m in enumerate(monitors):
        opts.append((monitor_label(m, i + 1), m.device))
        present.add(m.device)
    if current_value and current_value not in present:
        opts.append((_missing_label(current_value), current_value))
    return opts


def char_combo_options(monitors, current_value: str = "") -> list:
    """[(label, value)] for a per-character combobox. value '' == use default,
    NEVER ("none") == never move. Missing-device preservation as above."""
    opts = [(CHAR_DEFAULT_LABEL, ""), (CHAR_NEVER_LABEL, NEVER)]
    present = set()
    for i, m in enumerate(monitors):
        opts.append((monitor_label(m, i + 1), m.device))
        present.add(m.device)
    if current_value and current_value not in (NEVER, "") and current_value not in present:
        opts.append((_missing_label(current_value), current_value))
    return opts


# ── UI builder (settings section) ─────────────────────────────────────────────
class MonitorPinSection:
    """Handle returned by :func:`build_settings_section`. Exposes the Tk vars and
    widgets so the host can drive live updates and tests can interact
    deterministically (``select_default`` / ``select_char`` set a combobox and
    fire its callback without relying on synthetic ``<<ComboboxSelected>>``
    delivery, which is unreliable for unmapped widgets)."""

    def __init__(self):
        self.default_var = None
        self.default_combo = None
        self.char_vars = {}          # key -> tk.StringVar (holds the LABEL)
        self.char_combos = {}        # key -> ttk.Combobox
        self.apply_btn = None
        self._default_map = {}       # label -> value
        self._char_maps = {}         # key -> {label: value}
        self._fire_default = lambda: None
        self._fire_char = lambda key: None

    def default_value(self) -> str:
        if self.default_var is None:
            return ""
        return self._default_map.get(self.default_var.get(), "")

    def char_value(self, key: str) -> str:
        var = self.char_vars.get(key)
        if var is None:
            return ""
        return self._char_maps.get(key, {}).get(var.get(), "")

    def select_default(self, label: str):
        self.default_var.set(label)
        self._fire_default()

    def select_char(self, key: str, label: str):
        self.char_vars[key].set(label)
        self._fire_char(key)


def build_settings_section(parent, *, monitors, assignments, default_device,
                           chars, on_default_change, on_char_change, on_apply):
    """Build the monitor-pinning controls into ``parent`` (a Tk frame/Toplevel).

    Pure UI assembly — every side effect (persist, move a live client) happens in
    the host-supplied callbacks:
      * ``on_default_change(device_or_empty)`` — new global default ('' = off).
      * ``on_char_change(key, value)`` — value in {'', NEVER, <device>}.
      * ``on_apply()`` — the Apply-now sweep.

    ``monitors`` is an already-enumerated primary-first list; ``chars`` is the
    already-unioned+sorted key list. Returns a :class:`MonitorPinSection`.
    tkinter + the shared palette are imported lazily so importing this module for
    its pure logic never needs a display."""
    import tkinter as tk
    from tkinter import ttk
    from ui_theme import BG_PANEL, FG_TEXT, FG_DIM

    section = MonitorPinSection()

    def _fire_default(*_):
        on_default_change(section.default_value())

    def _fire_char(key):
        on_char_change(key, section.char_value(key))

    section._fire_default = _fire_default
    section._fire_char = _fire_char

    tk.Label(parent, text="Move each character's EVE client onto a chosen monitor "
             "when it logs in.", bg=BG_PANEL, fg=FG_TEXT, font=("Consolas", 10),
             justify=tk.LEFT, wraplength=460).pack(anchor="w", padx=10, pady=(10, 6))

    # Global default row.
    drow = tk.Frame(parent, bg=BG_PANEL)
    drow.pack(fill=tk.X, padx=10, pady=(0, 4))
    tk.Label(drow, text="Default for all:", bg=BG_PANEL, fg=FG_TEXT,
             font=("Consolas", 10)).pack(side=tk.LEFT, padx=(0, 6))
    d_opts = default_combo_options(monitors, default_device)
    section._default_map = {lab: val for lab, val in d_opts}
    d_val_to_lab = {val: lab for lab, val in d_opts}
    section.default_var = tk.StringVar(
        value=d_val_to_lab.get(default_device, DEFAULT_OFF_LABEL))
    dcombo = ttk.Combobox(drow, textvariable=section.default_var,
                          values=[lab for lab, _ in d_opts], state="readonly",
                          width=34, font=("Consolas", 10))
    dcombo.pack(side=tk.LEFT)
    dcombo.bind("<<ComboboxSelected>>", lambda e: _fire_default())
    section.default_combo = dcombo

    # Per-character rows (union of live chars + already-assigned chars).
    list_frame = tk.Frame(parent, bg=BG_PANEL)
    list_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)
    chars = list(chars)
    if not chars:
        tk.Label(list_frame, text="(no characters seen yet — log a client in, or "
                 "use the default above)", bg=BG_PANEL, fg=FG_DIM,
                 font=("Consolas", 9), justify=tk.LEFT, wraplength=440).pack(anchor="w")
    for key in chars:
        row = tk.Frame(list_frame, bg=BG_PANEL)
        row.pack(fill=tk.X, pady=1)
        tk.Label(row, text=key, bg=BG_PANEL, fg=FG_TEXT, font=("Consolas", 10),
                 width=20, anchor="w").pack(side=tk.LEFT, padx=(0, 6))
        cur = (assignments or {}).get(key, "")
        c_opts = char_combo_options(monitors, cur)
        section._char_maps[key] = {lab: val for lab, val in c_opts}
        c_val_to_lab = {val: lab for lab, val in c_opts}
        var = tk.StringVar(value=c_val_to_lab.get(cur, CHAR_DEFAULT_LABEL))
        section.char_vars[key] = var
        combo = ttk.Combobox(row, textvariable=var,
                             values=[lab for lab, _ in c_opts], state="readonly",
                             width=34, font=("Consolas", 10))
        combo.pack(side=tk.LEFT)
        combo.bind("<<ComboboxSelected>>", lambda e, k=key: _fire_char(k))
        section.char_combos[key] = combo

    # Apply + note.
    arow = tk.Frame(parent, bg=BG_PANEL)
    arow.pack(fill=tk.X, padx=10, pady=(6, 2))
    section.apply_btn = ttk.Button(arow, text="Apply now", style="Dark.TButton",
                                   command=on_apply)
    section.apply_btn.pack(side=tk.LEFT)

    tk.Label(parent, text="Automatic on-login placement needs FCPreview native "
             "mode; Apply works anytime. The EVE client must be in Fixed Window "
             "or Windowed display mode (Full Screen can't be moved).",
             bg=BG_PANEL, fg=FG_DIM, font=("Consolas", 9), justify=tk.LEFT,
             wraplength=460).pack(anchor="w", padx=10, pady=(2, 8))

    return section


# ── real ctypes backend (Windows only; never touched by tests) ────────────────
class _RealMonitorWin32:  # pragma: no cover — exercised only on a live desktop
    """EnumDisplayMonitors + GetMonitorInfoW(MONITORINFOEXW). Mirrors the
    injectable-wrapper shape of eve_client_tracker._RealWin32."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self._ctypes = ctypes
        self._wintypes = wintypes
        self._user32 = ctypes.windll.user32

        self._MONITORENUMPROC = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
            ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

        class _MONITORINFOEXW(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD),
                        ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT),
                        ("dwFlags", wintypes.DWORD),
                        ("szDevice", wintypes.WCHAR * 32)]
        self._MONITORINFOEXW = _MONITORINFOEXW

        self._user32.EnumDisplayMonitors.argtypes = [
            wintypes.HDC, ctypes.POINTER(wintypes.RECT),
            self._MONITORENUMPROC, wintypes.LPARAM]
        self._user32.EnumDisplayMonitors.restype = wintypes.BOOL
        self._user32.GetMonitorInfoW.argtypes = [
            wintypes.HMONITOR, ctypes.POINTER(_MONITORINFOEXW)]
        self._user32.GetMonitorInfoW.restype = wintypes.BOOL

    def enum_monitors(self) -> list:
        ctypes = self._ctypes
        handles = []

        def _cb(hmon, _hdc, _lprc, _lparam):
            handles.append(hmon)
            return True

        self._user32.EnumDisplayMonitors(
            None, None, self._MONITORENUMPROC(_cb), 0)

        MONITORINFOF_PRIMARY = 0x1
        out = []
        for hmon in handles:
            mi = self._MONITORINFOEXW()
            mi.cbSize = ctypes.sizeof(self._MONITORINFOEXW)
            if not self._user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                continue
            rc, wk = mi.rcMonitor, mi.rcWork
            out.append(MonitorInfo(
                device=mi.szDevice,
                rect=(rc.left, rc.top, rc.right, rc.bottom),
                work=(wk.left, wk.top, wk.right, wk.bottom),
                primary=bool(mi.dwFlags & MONITORINFOF_PRIMARY)))
        return out


_REAL_MONITOR_WIN32 = None


def _real_monitor_win32():  # pragma: no cover
    global _REAL_MONITOR_WIN32
    if _REAL_MONITOR_WIN32 is None:
        if sys.platform != "win32":
            raise RuntimeError("monitor_pin real backend requires Windows")
        _REAL_MONITOR_WIN32 = _RealMonitorWin32()
    return _REAL_MONITOR_WIN32
