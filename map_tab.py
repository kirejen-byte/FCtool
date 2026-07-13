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

import copy
import json
import math
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

# Destination-route overlay (Task 35). Gold dashed polyline for stargate hops;
# Ansiblex hops reuse the base-layer BRIDGE_BLUE (map_render) as a dash-dot line
# with diamond endpoint markers, so the overlay matches the bridge tint drawn
# into the bitmap. Both are pure Tk overlay items (crisp during gestures). The
# blue is derived from mr.BRIDGE_BLUE so the two stay in lockstep.
ROUTE_GOLD = "#ffcc44"
BRIDGE_BLUE_HEX = "#%02x%02x%02x" % mr.BRIDGE_BLUE
# Route-overlay Ansiblex-hop colour: a LIGHTENED tint of the bridge-layer blue,
# drawn BRIGHTER and WIDER than the resting bridge glow (map_render._draw_bridges
# lays down up to a 4px line in dim(BRIDGE_BLUE, …)). A route that RIDES a bridge
# was previously painted in BRIDGE_BLUE_HEX at width 2 -- the SAME hue, NARROWER
# than the glow beneath it -- so it vanished blue-on-blue and the owner saw "no
# highlight" over the Ansiblex. Same #3A86FF family, so it still reads as a bridge.
ROUTE_BRIDGE_HEX = "#%02x%02x%02x" % tuple(
    min(255, round(c + (255 - c) * 0.45)) for c in mr.BRIDGE_BLUE)

# Kill-heat layer (Task 30). Capital-kill markers reuse a red hex derived from
# the base-layer HEAT_COLOR family. The periodic decay refresh re-requests a
# crisp at most every HEAT_REFRESH_MS while the layer is on and heat is live, so
# the under-glow shrinks with decay even absent camera/kill activity (the 16 ms
# tick is far too fast to re-render every frame). The ESI ambient fetch loop runs
# ~hourly (owner-approved: "2 calls per hour"; we fetch ONE endpoint -- see
# _fetch_ambient_heat's jumps decision), with a short first-fetch delay so a rapid
# hide/show right after launch never fires a burst.
CAPKILL_RED = "#ff3b30"
HEAT_REFRESH_MS = 60_000.0
AMBIENT_START_DELAY_S = 5.0
AMBIENT_INTERVAL_S = 3600.0

# Intel pulse layer (Task 31). Systems named in tracked intel channels pulse with
# a ~5-min decay. AMBER (#ffd166) is chosen so the ring reads DISTINCTLY from the
# red threat/capital family (#ff5a76 / #ff3b30) and the blue Ansiblex bridges -- a
# warm "attention" hue no other overlay claims. Rings OSCILLATE (radius + line
# width) on a gentle ~1.1 s period; the oscillation AMPLITUDE scales with the
# mention's decay intensity, so a fresh call throbs hard and an old one barely
# breathes. Structure (which systems have rings, reprojection after the set
# changed) is rebuilt at ~1 s granularity (INTEL_STRUCT_MS -- the Task 30 heat-
# refresh idiom) while each 16 ms tick only MUTATES the existing ring items
# (coords / itemconfigure), never delete/recreate -- so the hot path stays cheap.
INTEL_AMBER = "#ffd166"
INTEL_R0 = 9.0               # base ring radius (px) at the oscillation trough
INTEL_AMP = 6.0              # max radial oscillation amplitude (px), * intensity
INTEL_PULSE_MS = 1100.0      # oscillation period (~0.9 Hz throb)
INTEL_HIT_R = 16.0           # click hit radius (>= max ring radius R0 + AMP = 15)
INTEL_STRUCT_MS = 1000.0     # cull + reproject cadence (structure changes only)
INTEL_CLICK_SLOP2 = 25       # (5 px)^2: press/release within this = a click, not a pan
_TWO_PI = 2.0 * math.pi

# Kill-ping layer (Task 36). DISCRETE zkill ALERT pings -- distinct from the
# ambient decaying kill-heat glow (Task 30). A fresh alert BURSTS as expanding
# radar rings in a vivid hostile RED (#ff1744) chosen to read apart from the amber
# intel pulses (#ffd166), the static capital double-ring (#ff3b30 / #ff5a76 family)
# and the soft heat glow; then it LINGERS as a small steady diamond marker for
# ~5 min so an older kill stays findable. Rings emanate on a ~1 s radar sweep whose
# phase is shared (time-based, like the intel oscillator) so the per-tick tween
# only MUTATES existing ring items (coords / width), never delete/recreates; the
# linger marker is static (no tween). Structure changes (burst<->linger, cull,
# reproject) are batched to ~1 s (KILLPING_STRUCT_MS) exactly like the intel idiom.
KILLPING_RED = "#ff1744"
KILLPING_R0 = 5.0            # inner (birth) ring radius (px)
KILLPING_SPAN = 22.0        # radial travel of an emanating ring (px), non-capital
KILLPING_SPAN_CAP = 32.0    # capital burst reaches farther (bigger, louder ping)
KILLPING_RINGS = 2          # concurrent emanating rings (non-capital)
KILLPING_RINGS_CAP = 3      # capital burst adds a third emanating ring
KILLPING_SWEEP_MS = 1000.0  # radar-sweep period (one ring's birth -> death)
KILLPING_MARK_R = 5.0       # linger diamond marker half-size (px)
KILLPING_MARK_R_CAP = 7.0   # capital linger marker is a touch larger (doubled)
KILLPING_STRUCT_MS = 1000.0 # cull + reproject + stage-flip cadence (structure only)

# Sovereignty tint layer (Task 33). ESI /sovereignty/map/ (public, cached long)
# gives per-system sovereign alliance; a dim hashed tint per alliance washes the
# map behind the nodes (map_render._draw_sov / map_overlays.sov_color). OFF by
# default -- the palette-noise call is the owner's, so ZERO network until enabled.
# While the layer is on the data is refreshed at most once per SOV_REFRESH_S via a
# ONE-SHOT daemon thread (not a persistent loop): it fetches, posts to the result
# queue, and dies -- nothing to join on hide.
SOV_REFRESH_S = 3600.0

# Characters overlay (owner ask: "see where all your characters are"). A 60 s
# poll enumerates the tool's AUTHED characters (fc_gui injects the fetch -- the
# map holds NO auth/token logic, exactly like it holds no infra logic) and marks
# each occupied system. MAGENTA is a hue FREE in the current palette: distinct
# from cyan fleet (#00d4ff), the red staging/capital/ping family (#ff5a76 /
# #ff3b30 / #ff1744), amber intel (#ffd166), green range (#59d98c), blue-purple
# threat (map_render.THREAT_PURPLE #8e5bd6), gold route (#ffcc44) and blue
# bridges. Drawn as a filled SQUARE (no other overlay uses an axis-aligned
# square -- fleet is a circle, staging a diamond, the pulses are rings) so the
# shape reads apart from the cyan fleet pins at a glance too. The poll mirrors
# the Task-30 ambient daemon EXACTLY (map-chars Event.wait thread, ~2 s first
# fetch, prompt join on hide -> no leak) but runs ONLY while the tab is shown AND
# the layer is on, so idle cost is nil.
CHARS_MAGENTA = "#ff44e1"
CHARS_POLL_S = 60.0
CHARS_START_DELAY_S = 2.0

# Layers whose _layer_on() default is FALSE when cfg omits the key (everything
# else defaults True). Sov is off-by-default AND, unlike range (always gated by a
# live overlay object), _layer_on("sov") directly controls both the fetch and the
# render -- so an absent key must read False, not the blanket True. Infra (Task 5)
# joins it: the friendly-structure chips stay dark until the user enables the layer
# AND the host has pushed badges, so _layer_on("infra") gates its request value.
_LAYERS_OFF_BY_DEFAULT = frozenset({"sov", "infra"})

# Infrastructure overlay filter state (Task 5). This module imports NO infra_*
# module (architecture rule: the map holds ZERO infra logic -- fc_gui composes
# parser/store/overlay and pushes PRE-COMPUTED badges via set_infrastructure), so
# it keeps a LOCAL copy of infra_overlay.FILTER_DEFAULTS's shape (§3.7) purely to
# seed the toolbar/popover UI. Only fc_gui bridges this UI state back to the real
# infra_overlay.FILTER_DEFAULTS. "enabled" mirrors the toolbar Infra checkbutton;
# the popover writes categories / stale_only. A private deepcopy is handed to the
# host's get_infrastructure callback on every change.
INFRA_FILTER_DEFAULTS = {
    "enabled": True,
    "categories": {"citadel": True, "engineering": True, "refinery": True,
                   "gate": True, "flex": True, "npc": False, "unknown": True},
    "regions": None,
    "stale_only": False,
    "sources": None,
    "types": None,            # None = no per-type restriction (all types shown);
                              # else a tuple of checked type_ids. Mirrors
                              # infra_overlay.FILTER_DEFAULTS (a test pins equality).
}
# Ordered (display label, category key) pairs for the filter popover checkbuttons.
_INFRA_CATEGORY_LABELS = (
    ("Citadels", "citadel"), ("Engineering", "engineering"),
    ("Refineries", "refinery"), ("Ansiblexes", "gate"), ("Flex", "flex"),
    ("NPC stations", "npc"), ("Unknown", "unknown"),
)

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

# --- reusable hover summary tooltip engine (owner feedback round B) -----------
# A layer-gated hover tooltip: providers registered via register_hover_provider
# each contribute a section for the system under the cursor; enabled sections are
# joined (blank-line separated) into one reused themed Toplevel shown after a
# short dwell. The <Motion> path is near-zero when no provider's layer is on (the
# FIRST guard short-circuits before any allocation), so idle hover cost is
# unchanged from the pre-tooltip renderer -- the map's perf discipline is sacred.
HOVER_TOOLTIP_DELAY_MS = 350        # dwell before the summary tooltip appears
HOVER_TOOLTIP_MOVE_R2 = 100         # px^2: motion beyond ~10px reschedules/hides

# --- toolbar control tooltips (owner ask 2026-07-12) -------------------------
# Distinct from the canvas summary tooltip above: a per-control hover tip on the
# toolbar widgets (search box / layer checkboxes / ▾ drawer buttons). Same dwell-
# then-single-Toplevel discipline, longer dwell (500ms) so a fly-over across the
# strip never flashes tips, and hidden instantly on leave/press so it can never
# sit between the cursor and a click.
TOOLBAR_TOOLTIP_DELAY_MS = 500      # dwell before a toolbar control's tip appears

# Cap on structure rows in the right-click "Structures (N)" submenu (owner
# feedback round B): a busy staging system can hold dozens of structures, so the
# grouped list is truncated with a trailing "…and N more (Manage…)" row rather
# than spawning a menu taller than the screen.
_STRUCT_MENU_CAP = 25


def _now_ms() -> float:
    return time.monotonic() * 1000.0


def _default_route_fn(origin_id: int, dest_id: int, connections=None):
    """Default travel-route solver for the destination overlay (Task 35): the
    app's Ansiblex-aware stargate BFS, reusing the Navigation tab's invocation
    exactly (``get_stargate_route(origin_id, dest_id, connections=<'a|b' str list>)``
    -- default preference "shortest"). Lazily imported so map_tab keeps its light
    import surface (jump_range pulls in requests / system_coords) and tests /
    standalone can inject a stub via ``MapTab(route_fn=...)``. Returns the ordered
    system-id path (origin..dest) or None when unreachable."""
    from jump_range import get_stargate_route
    return get_stargate_route(origin_id, dest_id, connections=connections)


def _request_sig(req: dict) -> tuple:
    """Comparable signature of a render request: camera center/scale + viewport
    size + bloom + mode + tint + bridges + heat + sov + infra (NOT generation --
    that is a staleness token, and duplicates carry different generations).

    infra participates exactly like sov/heat/bridges: the canonical infra tuple
    (or None when the layer is off / no badges) compares by value, so an Infra
    toggle, a filter change, or a fresh push is never suppressed as a duplicate,
    while a settle on an unchanged view+infra still is.

    sov participates exactly like heat/bridges: the canonical sov tuple (or None
    when the layer is off / no data) compares by value, so a sov toggle or a fresh
    sov fetch is never suppressed as a duplicate, while a settle on an unchanged
    view+sov still is.
    Used by the worker to suppress a settle re-render that duplicates the crisp
    frame already applied to the canvas (Task 18 Step 1b; see MapTab._applied_sig).

    heat participates exactly like bridges: the canonical rounded heat tuple (or
    None when the layer is off / no activity) compares by value, so a kill arrival,
    an ambient refresh, or a decay step that MOVES the rounded heat is never
    suppressed as a duplicate, while a settle on an unchanged view+heat still is.

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
            bool(req["bloom"]), req["mode"], req.get("tint"), req.get("bridges"),
            req.get("heat"), req.get("sov"), req.get("infra"))


def _canonical_chars(payload) -> tuple:
    """Normalise a characters_fetch payload ({system_id: [(char, ship), ...]})
    into a sorted, hashable tuple ((system_id, ((char, ship), ...)), ...) with the
    per-system pairs sorted by character name. Mirrors the ambient/sov canonical
    idiom so the worker posts a frozen, thread-safe snapshot (no dict aliased
    across the poll thread and the main thread) and both the markers and the
    name-sorted hover read a deterministic order. Empty / malformed systems are
    dropped. Tolerant of a bad payload (returns () rather than raising) so a
    broken fetch never wedges the drain."""
    out = []
    try:
        items = payload.items()
    except AttributeError:
        return ()
    for sid, pairs in items:
        try:
            sp = tuple(sorted((str(n), str(s)) for n, s in (pairs or ())))
        except (TypeError, ValueError):
            continue
        if sp:
            out.append((int(sid), sp))
    out.sort()
    return tuple(out)


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
        # Sorted lowercase-name keys (rebuilt at attach_model) so fly_to's
        # prefix/contains fallback is one linear pass in a deterministic,
        # sorted-first order (sub-ms over ~5,485 names).
        self._sorted_names: list[str] = []
        # --- overlay layer state (Phase D) ---
        self.range_overlay = None
        self.threat_set = None
        self.fleet: dict[int, int] = {}
        self.friendly_staging: set[int] = set()
        self.hostile_staging: set[int] = set()
        self.own_system_id = None
        # --- destination route overlay (Task 35, SESSION-scoped, not persisted) --
        # route_dest: the tool-set destination system id (pushed by fc_gui's
        # _set_destination_or_copy after a successful ESI set_waypoint).
        # route_path: the resolved ordered system-id path origin..dest from the
        # Ansiblex-aware BFS, or None when unrouted / cleared.
        self.route_dest = None
        self.route_path: tuple | None = None
        # Resolved Ansiblex bridge id-pairs (hashable tuple of unordered
        # (id_a, id_b) pairs from map_overlays.resolve_bridges). A BASE-layer
        # element -- drawn into the bitmap, not a Tk overlay item.
        self.bridges: tuple = ()
        # --- kill-heat layer (Task 30) ---
        # kill_heat: the LIVE zkill decay-heat ring (mutated on the MAIN thread
        # via add_kill, marshaled from fc_gui's zkill worker callback through
        # _post_ui). ambient_heat: the last hourly ESI ambient counts
        # ({system_id: ship+pod kills}), applied on the main thread from the
        # result queue. Both feed KillHeat.merge_ambient at request-build time to
        # produce the 0..1 heat the renderer draws as a red-orange under-glow.
        self.kill_heat = mo.KillHeat()
        self.ambient_heat: dict[int, int] = {}
        # --- intel pulse layer (Task 31) ---
        # Systems named in tracked intel channels. note()d on the MAIN thread
        # (fc_gui marshals the intel stream through _post_ui before the push), read
        # via active() by MapTab to draw the amber pulse rings. Pure decay model.
        self.intel_pulses = mo.IntelPulses()
        # --- kill-ping layer (Task 36) ---
        # Discrete zkill ALERT pings (distinct from the ambient kill-heat glow):
        # ping()ed on the MAIN thread -- fc_gui fires these from the report-render
        # path (_show_zkill_alert -> _push_kill_ping_to_map -> add_kill_ping) AFTER
        # the display gates pass, so pings match rendered reports 1:1. Read via
        # active() to draw the red radar-burst rings + linger markers. Pure decay.
        self.kill_pings = mo.KillPings()
        # --- sovereignty tint layer (Task 33) ---
        # sov_map: {system_id: alliance_id} from ESI /sovereignty/map/, applied on
        # the MAIN thread from the result queue (canonical_sov -> the render request
        # tuple the base bitmap draws). sov_names: {alliance_id: name} for the
        # legend + right-click info row, bulk-resolved via /universe/names/. Both
        # stay empty until the layer is first enabled (OFF by default -> zero
        # network until then).
        self.sov_map: dict[int, int] = {}
        self.sov_names: dict[int, str] = {}
        # --- infrastructure overlay layer (Task 5) ---
        # infra: PRE-COMPUTED badges {system_id: {"total", "counts", "top",
        # "stale"}} pushed by the host via MapTab.set_infrastructure (None = layer
        # off / no data). The map holds ZERO infra logic -- fc_gui computes badges
        # from the store through infra_overlay and pushes them here. Folded into
        # the render request + sig as a canonical ((sid, total, top, stale), ...)
        # tuple by _infra_request_value (that tuple is BOTH the sig component and
        # the data the renderer iterates).
        self.infra: dict | None = None
        # Filter UI state (LOCAL mirror of infra_overlay.FILTER_DEFAULTS's shape --
        # this file imports no infra module). "enabled" tracks the toolbar Infra
        # checkbutton; the popover writes categories / stale_only. A deepcopy is
        # handed to the host's get_infrastructure callback on every change.
        self.infra_filters: dict = copy.deepcopy(INFRA_FILTER_DEFAULTS)
        # --- characters overlay ---
        # {system_id: [(char_name, ship_type_name), ...]} for the authed
        # characters, applied on the MAIN thread from the result queue (the
        # map-chars poll posts a canonical snapshot). A pure Tk overlay (like
        # fleet) -- NOT part of the render request/sig; _redraw_overlays paints a
        # magenta square per occupied system. Empty until the first poll drains.
        self.chars: dict[int, list] = {}

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
        self._sorted_names = sorted(self._name_to_id)
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
        """Center + zoom onto a system resolved from a (partial) name. Resolution
        order: exact (case-insensitive) -> sorted-first case-insensitive PREFIX
        (startswith) -> sorted-first case-insensitive CONTAINS -> miss. An exact
        hit always wins over a longer prefix sibling (e.g. "Jita" beats "Jitand"),
        and among equal-class matches the sorted-first name is chosen so the
        result is deterministic. Empty input is a miss (never matches everything
        via the empty-prefix). Camera behavior is unchanged from the exact-only
        version (recenter + min-zoom bump + dirty)."""
        if self.model is None:
            return False
        q = name.strip().lower()
        if not q:
            return False
        sid = self._name_to_id.get(q)                       # 1) exact
        if sid is None:                                     # 2) prefix (sorted-first)
            sid = next((self._name_to_id[k] for k in self._sorted_names
                        if k.startswith(q)), None)
        if sid is None:                                     # 3) contains (sorted-first)
            sid = next((self._name_to_id[k] for k in self._sorted_names
                        if q in k), None)
        if sid is None:
            return False
        s = self.model.systems[sid]
        self.camera.cx, self.camera.cy = s.x, s.y
        self.camera.scale = max(self.camera.scale, self.camera.max_scale / 3.0)
        self._dirty = True
        return True

    def selected_hostile_staging(self, excluded_names=()) -> set[int]:
        """Hostile-staging ids that CONTRIBUTE to the threat halo: the full
        ``hostile_staging`` set minus those whose display name is excluded.

        Exclusions are stored by NAME (config staging lists are names), so each
        hostile id is resolved to its display name via the model. An id with no
        model record (name unknown) can't match an exclusion and stays INCLUDED
        -- new/unknown staging defaults to contributing (Task 34). Pure /
        headless-testable; MapTab supplies ``excluded_names`` from
        ``cfg["threat_staging_excluded"]``."""
        excluded = set(excluded_names or ())
        if not excluded:
            return set(self.hostile_staging)
        systems = self.model.systems if self.model is not None else {}
        keep: set[int] = set()
        for sid in self.hostile_staging:
            s = systems.get(sid)
            if s is not None and s.name in excluded:
                continue                         # excluded by name
            keep.add(sid)                        # included (or name unknown)
        return keep

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
                 telemetry: bool = False, route_fn=None, ambient_fetch=None,
                 sov_fetch=None, names_fetch=None, characters_fetch=None,
                 infra_type_index=None) -> None:
        self.cfg = cfg or {}
        self.save_cfg = save_cfg or (lambda d: None)
        self.callbacks = callbacks or {}
        self._model_loader = model_loader
        # Per-type infra filter index (owner ask 2026-07-12) injected by the host
        # (fc_gui builds it from infra_parser -- the map imports no infra module).
        # [(category, [(type_id, display_name), …]), …], categories in display
        # order, types name-sorted, npc/unknown omitted. Empty in standalone/tests
        # (no index) -> the popover shows the coarse category toggles only and the
        # emitted filters["types"] stays None (no restriction).
        self._infra_type_index = infra_type_index or []
        # Injectable travel-route solver (Task 35); defaults to the Ansiblex-aware
        # stargate BFS. Tests / standalone pass a stub so _recompute_route runs
        # deterministically without the jump_range import.
        self._route_fn = route_fn or _default_route_fn
        # Injectable ESI ambient-heat fetcher (Task 30); defaults to the real
        # /universe/system_kills/ pull. Tests inject a stub so the ambient loop's
        # lifecycle can be exercised without any network.
        self._ambient_fetch = ambient_fetch or self._fetch_ambient_heat
        # Injectable ESI sov fetchers (Task 33); default to the real /sovereignty/
        # map/ pull + /universe/names/ bulk resolve. Tests inject stubs so the
        # one-shot fetch lifecycle runs with no network.
        self._sov_fetch = sov_fetch or self._fetch_sov_map
        self._names_fetch = names_fetch or self._fetch_alliance_names
        # Injectable AUTHED-characters fetcher (owner ask). Unlike the ambient/sov
        # fetchers (public endpoints the map hits itself), enumerating the tool's
        # characters needs their per-character tokens, which live in fc_gui -- so
        # this is ALWAYS injected by the host (fc_gui._map_characters_fetch) and
        # defaults to a no-op returning {} for standalone/tests (no auth -> no
        # characters). Called on the map-chars poll thread -> MUST stay Tk-free.
        self._characters_fetch = characters_fetch or (lambda: {})
        # App-palette theme for the menus + toolbar. Merging over _DEFAULT_THEME
        # keeps standalone identical and lets a caller pass a partial dict. fc_gui
        # injects its BG_DARK/BG_PANEL/... constants so the map matches the app.
        self.theme = {**_DEFAULT_THEME, **(theme or {})}
        t = self.theme

        self.frame = tk.Frame(parent, bg=t["bg"])
        bar = tk.Frame(self.frame, bg=t["bg"])
        bar.pack(side="top", fill="x")
        # Toolbar hover-tooltip state (owner ask 2026-07-12): a dwell-delayed tip
        # shown ~500ms after the cursor settles on a control, reusing ONE Toplevel
        # slot (self._tooltip) and dismissed instantly on leave/press. Initialised
        # here (before the first control is built) so _attach_tooltip can register
        # the search box below. _toolbar_tooltips is the widget->text registry a
        # test walks to assert every toolbar control is documented.
        self._toolbar_tooltips: dict = {}
        self._tooltip = None                 # the single live tip Toplevel (lazy)
        self._tooltip_after = None           # pending dwell-timer id (after_cancel)
        self._tooltip_delay_ms = TOOLBAR_TOOLTIP_DELAY_MS   # instance-tunable (tests)
        tk.Label(bar, text="Search:", bg=t["bg"], fg=t["fg"]).pack(side="left", padx=(6, 2))
        entry_cls = autocomplete_cls or tk.Entry
        # An injected AutocompleteEntry takes the completion contract
        # (completions/labels/on_select); on_select flies to the picked system
        # immediately (a dropdown selection never raises <KeyRelease>). Gate on
        # the update_completions capability rather than try/except: a plain
        # tk.Entry FORWARDS unknown options to Tcl and raises tk.TclError -- not
        # TypeError -- AND leaves a half-registered orphan child, so a bare
        # (bar, width=24) construction is the right path for it (and for any
        # non-autocomplete widget). The inner TypeError guard still tolerates an
        # autocomplete widget with a stricter ctor signature.
        if hasattr(entry_cls, "update_completions"):
            try:
                self.search_entry = entry_cls(bar, completions=[], labels={},
                                              on_select=self._on_search, width=24)
            except TypeError:
                self.search_entry = entry_cls(bar, width=24)
        else:
            self.search_entry = entry_cls(bar, width=24)
        self.search_entry.pack(side="left", padx=2, pady=3)
        # add="+" so we DON'T clobber AutocompleteEntry's own <Return> dropdown-
        # select binding -- both fire (typed-name Enter flies via _on_search; a
        # highlighted dropdown row is selected by the widget's own handler, which
        # then calls on_select=_on_search). fc_gui.py:2441 documents the same trap.
        self.search_entry.bind("<Return>", self._on_search, add="+")
        # Style the search field to the theme. Guarded: a custom autocomplete
        # widget might reject a Tk option (plain tk.Entry accepts them all).
        try:
            self.search_entry.configure(
                bg=t["entry_bg"], fg=t["fg"], insertbackground=t["fg"],
                relief="flat", highlightthickness=1,
                highlightbackground=t["border"], highlightcolor=t["accent"])
        except tk.TclError:
            pass
        self._attach_tooltip(
            self.search_entry,
            "Find a system — autocompletes as you type; Enter flies to the best match")
        self._bloom_var = tk.BooleanVar(value=bool(self.cfg.get("bloom", True)))
        # Zoom-animation escape hatch (Task 24, P3): True = eased glide (default),
        # False = instant snap (pre-Phase-F feel) for owners who prefer it. No
        # toolbar button -- toggled from the empty-space right-click menu and
        # persisted on hide like bloom/layers.
        self._zoom_anim_var = tk.BooleanVar(
            value=bool(self.cfg.get("zoom_animation", True)))
        _bloom_cb = tk.Checkbutton(bar, text="Bloom", variable=self._bloom_var,
                       bg=t["bg"], fg=t["fg"], selectcolor=t["panel"],
                       activebackground=t["bg"], activeforeground=t["accent"],
                       command=self._on_bloom_toggle)
        _bloom_cb.pack(side="left", padx=8)
        self._attach_tooltip(_bloom_cb,
                             "Cinematic glow rendering; off = flat tactical look")
        # --- Phase D layer toggles (fleet/staging/threat) ---
        _layers = self.cfg.get("layers", {})
        self._layer_vars: dict[str, tk.BooleanVar] = {
            "fleet": tk.BooleanVar(value=bool(_layers.get("fleet", True))),
            "staging": tk.BooleanVar(value=bool(_layers.get("staging", True))),
            "threat": tk.BooleanVar(value=bool(_layers.get("threat", False))),
            "bridges": tk.BooleanVar(value=bool(_layers.get("bridges", True))),
            "route": tk.BooleanVar(value=bool(_layers.get("route", True))),
            # Kill-heat layer (Task 30): ON by default (owner-approved ESI ambient).
            "heat": tk.BooleanVar(value=bool(_layers.get("heat", True))),
            # Intel pulse layer (Task 31): ON by default -- tracked-channel system
            # mentions pulse amber and fade over ~5 min.
            "intel": tk.BooleanVar(value=bool(_layers.get("intel", True))),
            # Kill-ping layer (Task 36): ON by default -- it only fires when the
            # user's configured zkill monitoring raises an alert (the checkbox is
            # the requested opt-out). Discrete red radar bursts, distinct from heat.
            "kill_pings": tk.BooleanVar(value=bool(_layers.get("kill_pings", True))),
            # Sovereignty tint layer (Task 33): OFF by default (owner's palette-
            # noise call) -- so the var default is False, matching _layer_on's
            # off-by-default handling and keeping the fetch dormant until enabled.
            "sov": tk.BooleanVar(value=bool(_layers.get("sov", False))),
            # Infrastructure chips (Task 5): OFF by default (like sov) -- the
            # friendly-structure count badges stay dark until the user enables the
            # layer AND the host pushes badges via set_infrastructure.
            "infra": tk.BooleanVar(value=bool(_layers.get("infra", False))),
            # Characters overlay (owner ask): ON by default -- magenta markers at
            # each authed character's system. The poll only runs while the tab is
            # shown, so a visible-by-default layer still costs nothing when idle.
            "chars": tk.BooleanVar(value=bool(_layers.get("chars", True))),
        }
        # Infra filter popover state (Task 5): per-category + stale-only vars
        # mirroring INFRA_FILTER_DEFAULTS (self.state is built later, so seed from
        # the module constant; the "enabled" flag is synced from the toolbar var
        # once self.state exists). Each popover checkbutton writes back into
        # self.state.infra_filters and re-emits to the host. The popover Toplevel
        # is created lazily and REUSED per open (built in _build_infra_popover).
        self._infra_cat_vars: dict[str, tk.BooleanVar] = {
            key: tk.BooleanVar(value=bool(INFRA_FILTER_DEFAULTS["categories"][key]))
            for _label, key in _INFRA_CATEGORY_LABELS}
        self._infra_stale_var = tk.BooleanVar(
            value=bool(INFRA_FILTER_DEFAULTS["stale_only"]))
        # Per-type filter vars (owner ask 2026-07-12): one BooleanVar per known
        # structure type from the injected index, default ALL-checked so the
        # emitted filters["types"] collapses to None (no restriction -- keeps the
        # pre-feature emit byte-identical). The popover renders these grouped under
        # their category master; writes fold into state.infra_filters["types"] via
        # _recompute_infra_types. _infra_types_by_cat keeps the per-category
        # (type_id, name) lists for the master-mirrors-children toggle + layout.
        self._infra_types_by_cat: dict[str, list] = {}
        self._infra_type_vars: dict[int, tk.BooleanVar] = {}
        for _cat, _types in self._infra_type_index:
            self._infra_types_by_cat[_cat] = list(_types)
            for _tid, _name in _types:
                self._infra_type_vars[_tid] = tk.BooleanVar(value=True)
        self._infra_popover = None
        # Current threat-ship selection label (radiobutton var; synced lazily
        # from cfg["threat_ship"] when the empty-space menu is built).
        self._threat_var = tk.StringVar()
        # Concise, behaviour-accurate hover tips for every layer toggle (owner ask
        # 2026-07-12). Keyed by layer key; the ▾ drawer buttons get their own tips
        # in the loop below. Route keeps its ESI caveat (destinations set only in
        # the game client can't be read back -- ESI exposes no route endpoint).
        _layer_tips = {
            "fleet": "Pins where your fleet members are (from the boss fleet poll)",
            "staging": "Diamond markers on friendly (green) and hostile (red) staging systems",
            "threat": "Purple shade over systems inside hostile jump/bridge range",
            "bridges": "Your Ansiblex gates as blue lines",
            "route": "Gold route to a tool-set destination; game-set routes can't be read (no ESI endpoint)",
            "intel": "Amber pulses at systems named in tracked intel channels; click to open the report",
            "kill_pings": "Radar bursts for zkill reports that match your alert settings",
            "sov": "Dim alliance-color wash over sovereign space",
            "infra": "Friendly structure count chips from your infrastructure DB",
            "chars": "Magenta squares where your logged characters are; hover a system for pilot + ship",
        }
        for _key, _text in (("fleet", "Fleet"), ("staging", "Staging"),
                            ("threat", "Threat"), ("bridges", "Bridges"),
                            ("route", "Route"),
                            ("intel", "Intel"), ("kill_pings", "Pings"),
                            ("sov", "Sov"), ("infra", "Infra"),
                            ("chars", "Chars")):
            _cb = tk.Checkbutton(bar, text=_text, variable=self._layer_vars[_key],
                                 bg=t["bg"], fg=t["fg"], selectcolor=t["panel"],
                                 activebackground=t["bg"], activeforeground=t["accent"],
                                 command=lambda k=_key: self._on_layer_toggle(k))
            _cb.pack(side="left", padx=4)
            # Every layer toggle gets a concise hover tip (owner ask 2026-07-12).
            self._attach_tooltip(_cb, _layer_tips.get(_key, ""))
            if _key == "threat":
                # Drawer affordance (owner ask 2026-07-12): a "▾" microbutton right
                # after the Threat toggle -- the SAME compact pattern as Sov ▾ /
                # Infra ▾ -- replacing the old standalone "Threat ▾" button that sat
                # detached at the far end of the bar (owner: the toggle + its opener
                # should read as one compact control). Opens the in-tab threat
                # drawer (master overlay toggle + ship-class picker + per-staging
                # rows). DELIBERATELY always enabled -- UNLIKE Sov's legend (only
                # useful once the tint is drawn), the drawer HOSTS the master
                # overlay toggle, so it must stay reachable to turn the layer ON
                # while it is still off.
                self._threat_drawer_btn = tk.Button(
                    bar, text="▾", command=self._toggle_threat_drawer,
                    bg=t["bg"], fg=t["fg"], activebackground=t["bg"],
                    activeforeground=t["accent"], relief="flat", borderwidth=0,
                    highlightthickness=0, padx=3, pady=0, cursor="hand2")
                self._threat_drawer_btn.pack(side="left", padx=(0, 4))
                self._attach_tooltip(
                    self._threat_drawer_btn,
                    "Threat settings — ship class and which hostile stagings project")
            if _key == "infra":
                # Filter affordance (Task 5): a "▾" microbutton right after the
                # Infra toggle opening the borderless filter popover (per-category
                # toggles, Stale-only, Manage…). Always enabled -- unlike sov's
                # legend it is useful even when the layer is off (it hosts Manage…
                # and pre-shapes what appears once enabled).
                self._infra_filter_btn = tk.Button(
                    bar, text="▾", command=self._show_infra_filters,
                    bg=t["bg"], fg=t["fg"], activebackground=t["bg"],
                    activeforeground=t["accent"], relief="flat", borderwidth=0,
                    highlightthickness=0, padx=3, pady=0, cursor="hand2")
                self._infra_filter_btn.pack(side="left", padx=(0, 4))
                self._attach_tooltip(self._infra_filter_btn,
                                     "Infra filters and the structure manager")
            if _key == "sov":
                # Legend affordance (Task 33): a small "▾" microbutton right after
                # the Sov toggle, ENABLED only while the layer is on. Opens the
                # per-alliance color/name/count legend popup.
                self._sov_legend_btn = tk.Button(
                    bar, text="▾", command=self._show_sov_legend,
                    bg=t["bg"], fg=t["fg"], activebackground=t["bg"],
                    activeforeground=t["accent"], relief="flat", borderwidth=0,
                    highlightthickness=0, padx=3, pady=0, cursor="hand2",
                    state=("normal" if bool(_layers.get("sov", False))
                           else "disabled"))
                self._sov_legend_btn.pack(side="left", padx=(0, 4))
                self._attach_tooltip(self._sov_legend_btn,
                                     "Sov legend — top alliances by systems shown")
        self.status = tk.Label(bar, text="", bg=t["bg"], fg=t["fg"], anchor="e")
        self.status.pack(side="right", padx=6)

        # Canvas + right-side threat drawer share the body row: the canvas packs
        # side=left/expand and the drawer (when open) packs side=right/fill=y, so
        # opening it just reflows the canvas narrower and its <Configure> updates
        # the renderer viewport (Task 34). With no drawer packed the canvas fills
        # the whole body exactly as before (side=left == side=top for a lone
        # expanding slave -- expand redistributes leftover space at the end).
        self.canvas = tk.Canvas(self.frame, bg=t["bg"], highlightthickness=0)
        self.canvas.pack(side="left", fill="both", expand=True)
        self._img_item = self.canvas.create_image(0, 0, anchor="nw")
        self._photo = None                      # keep a ref or Tk drops the image
        # Transient threat-settings drawer (Task 34): a fixed-width themed panel
        # created hidden; _open/_close_threat_drawer pack/pack_forget it on the
        # right. State is NOT persisted (transient UI). pack_propagate(False)
        # holds the ~230px width regardless of the (short) EVE system names.
        self._threat_drawer = tk.Frame(self.frame, bg=t["panel"], width=230)
        self._threat_drawer.pack_propagate(False)
        self._threat_drawer_open = False
        self._threat_rows = None                 # staging-row container (built on open)
        self._threat_staging_vars: dict[str, tk.BooleanVar] = {}   # name -> included?

        self.state = MapTabState(vw=800, vh=600)
        # Sync the infra layer's enabled flag from its toolbar var (OFF by default);
        # categories / stale_only already match INFRA_FILTER_DEFAULTS in the fresh
        # state, so only "enabled" needs reconciling with cfg["layers"]["infra"].
        self.state.infra_filters["enabled"] = self._layer_on("infra")
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

        # --- kill-heat ESI ambient loop + decay refresh (Task 30) ---------------
        # A dedicated daemon loop thread (started on show, stopped on hide) fetches
        # hourly ESI ambient kills and posts them onto the result queue; the stop
        # Event lets on_hidden join it PROMPTLY (Event.wait returns the instant the
        # event is set, so no thread leaks -- the Task-22 lesson). _last_heat_refresh_ms
        # gates the periodic decay re-render (see _heat_refresh_due).
        self._ambient_thread: threading.Thread | None = None
        self._ambient_stop = threading.Event()
        self._last_heat_refresh_ms = 0.0

        # --- characters overlay poll (map-chars daemon) -------------------------
        # Same no-leak contract as the ambient loop (Task 22): a daemon started on
        # show / STOPPED on hide, its stop Event letting on_hidden join it promptly.
        # UNLIKE ambient it also starts/stops on the toolbar toggle and runs ONLY
        # while _layer_on("chars") -- so a hidden tab or a disabled layer keeps zero
        # poll threads. state.chars survives on the state object across a hide/show.
        self._chars_thread: threading.Thread | None = None
        self._chars_stop = threading.Event()

        # --- sovereignty tint layer lazy fetch (Task 33) ------------------------
        # OFF by default -> ZERO network until enabled. When the layer is toggled
        # on (or is on at show time) and the data is stale, a ONE-SHOT daemon
        # thread ("map-sov") fetches /sovereignty/map/ + resolves alliance names,
        # posts the results onto the result queue, and DIES (not a persistent loop
        # -> nothing to join on hide). _sov_inflight guards against a double-spawn
        # (set on the main thread before the spawn, cleared on the main thread when
        # the result drains); _sov_fetched_ms is the last successful-fetch stamp
        # for the hourly freshness gate. _sov_legend holds the open legend popup.
        self._sov_inflight = False
        self._sov_fetched_ms = 0.0
        self._sov_legend = None

        # --- intel pulse layer (Task 31) -----------------------------------------
        # _intel_items maps system_id -> the canvas ring item drawn for it (rebuilt
        # by _redraw_overlays, in lockstep with the ov_intel tag deletes);
        # _intel_cache holds the per-system decay intensity captured at the last
        # structure rebuild (the per-tick tween reads it so it need NOT re-run the
        # decay every 16 ms); _intel_struct_ms gates the ~1 s cull/reproject.
        # _press_xy is the last ButtonPress-1 position, for click-vs-pan
        # discrimination in _on_drag_end (a pulse click focuses the Intel tab).
        self._intel_items: dict[int, int] = {}
        self._intel_cache: dict[int, float] = {}
        self._intel_struct_ms = 0.0
        self._press_xy: tuple[int, int] | None = None

        # --- kill-ping layer (Task 36) -------------------------------------------
        # _killping_items maps system_id -> the LIST of canvas item ids drawn for it
        # (a burst has KILLPING_RINGS[_CAP] ring ovals; a linger has 1-2 diamond
        # markers), rebuilt by _redraw_overlays in lockstep with the ov_killping tag
        # deletes; _killping_cache holds each ping's (stage, capital) captured at the
        # last structure rebuild so the per-tick tween knows which items are bursting
        # (animate) vs lingering (static) WITHOUT re-running active() every 16 ms;
        # _killping_struct_ms gates the ~1 s cull/reproject/stage-flip. _pending_focus_sid
        # is a focus_kill target deferred until on_shown (the notebook <<TabChanged>>
        # that runs on_shown fires ASYNC -- see _apply_pending_focus), applied AFTER
        # restore_camera so the kill focus wins over the restored camera.
        self._killping_items: dict[int, list[int]] = {}
        self._killping_cache: dict[int, tuple] = {}
        self._killping_struct_ms = 0.0
        self._pending_focus_sid: int | None = None

        # --- reusable hover summary tooltip engine (owner feedback round B) ----
        # _hover_providers: (layer_key, fn) pairs. fn(system_id) -> list[str] |
        # None; sections from every provider whose layer_key is _layer_on() are
        # joined into ONE reused themed Toplevel (_hover_tip) shown ~350 ms after
        # the cursor settles on a system. Kept minimal so the <Motion> idle path
        # (no provider layer on) does zero tooltip work. Task D will register a
        # characters-layer provider through the same register_hover_provider API.
        self._hover_providers: list[tuple[str, "callable"]] = []
        self._hover_delay_ms = HOVER_TOOLTIP_DELAY_MS   # instance-tunable (tests)
        self._hover_tip = None                          # reused Toplevel (lazy)
        self._hover_tip_label = None                    # the Label inside it
        self._hover_after_id = None                     # pending delayed-show after()
        self._hover_sid = None                          # system the pending/shown tip is for
        self._hover_anchor: tuple[int, int] | None = None   # canvas-xy where dwell began
        # Infra chips summary (Task 5 / round B): per-type counts for the hovered
        # system, honouring the SAME filters as the chips (the host already folded
        # them into the pushed badges' "type_counts"). Registered here because the
        # infra badge state (state.infra) lives on this tab.
        self.register_hover_provider("infra", self._infra_hover_lines)
        # Characters overlay hover (owner ask): "CharName — ShipType" per character
        # in the hovered system, gated on _layer_on("chars") by the engine and
        # composed alongside the infra section.
        self.register_hover_provider("chars", self._chars_hover_lines)

        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<ButtonPress-1>", self._on_drag_start)
        self.canvas.bind("<B1-Motion>", self._on_drag_move)
        self.canvas.bind("<ButtonRelease-1>", self._on_drag_end)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Button-3>", self._on_right_click)
        self.canvas.bind("<Leave>", self._on_canvas_leave)
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
            self._wire_completions(model)       # first show only: fill autocomplete
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
        self._start_ambient_loop()               # Task 30: hourly ESI ambient heat
        # Characters overlay: start the 60 s poll ONLY if the layer is on at show
        # time (ON by default). Idempotent; the toolbar toggle starts/stops it too.
        if self._layer_on("chars"):
            self._start_chars_loop()
        # Task 33: fetch the sov map ONLY if the layer is on at show time (OFF by
        # default -> no-op -> zero network). The one-shot thread dies after one
        # fetch; an in-flight fetch across a hide/show is not double-spawned.
        self._maybe_start_sov_fetch()
        # Task 36: apply a focus_kill target that arrived while the tab was hidden.
        # Runs LAST -- after restore_camera above -- so a "jump to this kill from the
        # Intelligence tab" wins over the restored camera (the <<NotebookTabChanged>>
        # that runs on_shown fires asynchronously, so focus_kill can't center before
        # this point). No-op when nothing is pending.
        self._apply_pending_focus()

    def on_hidden(self) -> None:
        self._hover_cancel()                 # never leave a tooltip over a hidden tab
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
        # Stop the kill-heat ESI ambient loop too (Task 30) -- same no-leak rule:
        # a hidden tab must not keep an hourly fetch daemon alive. on_shown
        # restarts it; kill_heat/ambient_heat state survives on the state object.
        self.shutdown_ambient_loop()
        # Characters overlay: stop the poll daemon too (same no-leak rule). on_shown
        # restarts it when the layer is on; state.chars survives on the state object.
        self.shutdown_chars_loop()
        # Task 33: dismiss any open sov legend popup (the one-shot sov FETCH thread
        # is not a loop -> nothing to stop; sov_map/sov_names survive on state, and
        # an in-flight fetch's _sov_inflight flag is intentionally left set so a
        # re-show does not double-spawn -- the daemon posts its result either way).
        self._hide_sov_legend()
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
        self._rebuild_threat_staging_rows()      # keep the drawer's rows in sync

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
        # Ansiblex list changed (Task 35): a live route may now take (or drop) a
        # bridge, so re-solve it from the current origin.
        if self.state.route_dest is not None:
            self._recompute_route()

    def set_infrastructure(self, badges: dict[int, dict] | None) -> None:
        """Store PRE-COMPUTED infrastructure badges from the host and re-render.
        ``badges`` maps ``system_id -> {"total", "counts", "top", "stale"}``;
        ``None`` (or an empty dict) means no chips -- an on-but-empty layer draws
        the same frame as off. The chips are a BASE-layer element -- drawn into
        the bitmap in the label pass (map_render._draw_infra_chips), riding the
        system-label zoom LOD -- so this is a settle re-render via _request_crisp
        (mirrors set_bridges), NOT a Tk overlay repaint. The host owns the data:
        it computes badges from the store via infra_overlay and pushes them here,
        so this file keeps ZERO infra imports. Safe before the first on_shown:
        _request_crisp / _redraw_overlays no-op while the renderer / model are
        unset, and the stored badges are picked up by the first crisp request."""
        self.state.infra = badges if badges else None
        self.state.force_dirty()
        self._request_crisp()
        self._redraw_overlays()

    def set_own_location(self, system_id) -> None:  # spec §5.2: own char distinct
        prev = self.state.own_system_id
        self.state.own_system_id = system_id
        # Route overlay (Task 35): the 15s own-location poll re-pushes the SAME id
        # every tick, so react only when the own system actually changed. Arrival
        # (own == destination) auto-clears the route; any other move re-solves it
        # from the new origin.
        if system_id != prev and self.state.route_dest is not None:
            if system_id is not None and system_id == self.state.route_dest:
                self.clear_route()
                return
            self._recompute_route()
        self._redraw_overlays()

    # ---- destination route overlay (Task 35) ----------------------------------
    def set_route_destination(self, system_id) -> None:
        """Set (or replace) the travel-route destination and re-solve the route
        overlay from the tracked character's current location. Called by fc_gui
        after a successful in-tool 'Set destination' (ESI set_waypoint) -- a Tk
        menu/bind handler, so it runs on the main thread. A new destination
        REPLACES any prior one; the route is session-scoped (never persisted).
        ``None`` clears."""
        if system_id is None:
            self.clear_route()
            return
        self.state.route_dest = system_id
        self.state.route_path = None             # replace: drop any stale path
        self._recompute_route()                  # async BFS (handles own == dest)
        self._redraw_overlays()                  # show the destination ring at once

    def clear_route(self) -> None:
        """Drop the destination-route overlay (arrival, replacement, or the user's
        'Clear route' menu action). Session state only -- nothing persisted."""
        self.state.route_dest = None
        self.state.route_path = None
        self._redraw_overlays()

    def _recompute_route(self) -> None:
        """Re-solve the travel route from own location to route_dest on a worker
        thread (Ansiblex-aware BFS), feeding the result back through the main-
        thread result queue as a ``("route", tuple)`` message (drained by
        _drain_results, applied by _apply_route -- the same off-thread pattern as
        _recompute_threat). No-ops while origin / dest / model are missing (the
        destination ring still draws); arrival (own == dest) clears. Ansiblex
        connection strings are built from the resolved bridge pairs the map
        already holds -- the SAME resolution the jump-range BFS consumes -- so no
        fc_gui import is needed."""
        dest = self.state.route_dest
        origin = self.state.own_system_id
        if dest is None or origin is None or self.state.model is None:
            return
        if origin == dest:                       # already there -> arrival clear
            self.clear_route()
            return
        # "id1|id2" strings for get_stargate_route's extra-edge parser (order does
        # not matter -- _bfs_route adds both directions). Empty -> None (gate-only).
        conns = [f"{a}|{b}" for (a, b) in self.state.bridges] or None
        route_fn = self._route_fn

        def work():
            try:
                path = route_fn(origin, dest, connections=conns)
            except Exception as exc:             # never crash the helper thread
                print(f"[MAP] route recompute failed: {exc}")
                return
            self._result_q.put(("route", tuple(path or ())))

        threading.Thread(target=work, daemon=True, name="map-route").start()

    def _apply_route(self, path) -> None:
        """Apply a worker-resolved route path on the main thread (Task 35). An
        empty path (no gate route found) clears the polyline but KEEPS route_dest
        so the destination ring still marks the target."""
        self.state.route_path = tuple(path) or None
        self._redraw_overlays()

    # ---- kill-heat layer (Task 30) --------------------------------------------
    def add_kill_heat(self, system_id, kill_count, capital=False) -> None:
        """Record a zkill engagement alert into the live kill-heat ring. Called on
        the MAIN thread -- fc_gui marshals the zkill worker-thread callback
        (_on_zkill_alert) through _post_ui(_push_kill_heat_to_map, alert), which
        calls this. Stamps the event with wall-clock time (the kill just arrived; zkill
        is near-real-time). When the heat layer is on, force-dirties + re-requests
        a crisp so the new hot system lights up promptly, and repaints overlays so
        a capital marker can appear at once. Guarded/no-throw so a malformed alert
        never breaks the zkill path."""
        try:
            self.state.kill_heat.add_kill(system_id, kill_count, time.time(),
                                          bool(capital))
        except Exception as exc:
            print(f"[MAP] add_kill_heat failed: {exc}")
            return
        if self._layer_on("heat"):
            self._last_heat_refresh_ms = _now_ms()   # a fresh crisp resets the decay clock
            self.state.force_dirty()
            self._request_crisp()
            self._redraw_overlays()

    def _current_heat_dict(self) -> dict:
        """Merged live+ambient 0..1 heat for NOW (empty dict when no activity).
        Single source used by both the request builder and the decay-refresh
        gate so they can never disagree on what heat is live."""
        return self.state.kill_heat.merge_ambient(self.state.ambient_heat,
                                                  time.time())

    def _heat_request_value(self):
        """Canonical hashable heat tuple for the render request + sig, or None
        when the heat layer is off OR there is no live heat -- both collapse to
        None so a heat-off (or heat-on-but-empty) frame is byte-identical to the
        pre-heat output and its sig matches, keeping duplicate suppression sound."""
        if not self._layer_on("heat"):
            return None
        return mo.canonical_heat(self._current_heat_dict()) or None

    def _heat_refresh_due(self, now_ms: float) -> bool:
        """True when the periodic decay refresh should re-request a crisp: the
        heat layer is on, at least HEAT_REFRESH_MS has elapsed since the last
        heat-driven refresh, AND there is currently live heat. The cheap time gate
        is checked FIRST so the (bounded but non-trivial) heat merge runs at most
        once per HEAT_REFRESH_MS, not every 16 ms tick. Pure/testable (injected
        clock)."""
        if not self._layer_on("heat"):
            return False
        if now_ms - self._last_heat_refresh_ms < HEAT_REFRESH_MS:
            return False
        return bool(mo.canonical_heat(self._current_heat_dict()))

    # ---- kill-heat ESI ambient loop (Task 30) ---------------------------------
    def _start_ambient_loop(self) -> None:
        """Start the hourly ESI ambient-heat fetch thread. Idempotent: a no-op
        when disabled in cfg (cfg["kill_heat_esi"], default True) or when a loop
        is already running. Owner-approved (2026-07-12: "Ok to make 2 calls per
        hour") -> ON by default; we actually issue ONE call/hour (see
        _fetch_ambient_heat's jumps decision)."""
        if not self.cfg.get("kill_heat_esi", True):
            return
        if self._ambient_thread is not None and self._ambient_thread.is_alive():
            return
        self._ambient_stop.clear()
        self._ambient_thread = threading.Thread(
            target=self._ambient_loop, daemon=True, name="map-ambient-heat")
        self._ambient_thread.start()

    def _ambient_loop(self) -> None:
        """Daemon body: after a short first-fetch delay, fetch ambient kills and
        post them onto the result queue, then repeat every ~hour. The stop Event
        is WAITED on (not sleep) for both the initial delay and the interval, so
        on_hidden's shutdown_ambient_loop wakes it the instant the event is set --
        it never sleeps out a full hour past a hide (the no-leak guarantee).
        Failures silent-degrade: _ambient_fetch returns None and the loop simply
        retries next cycle (zkill-only heat meanwhile)."""
        stop = self._ambient_stop
        if stop.wait(AMBIENT_START_DELAY_S):
            return                               # stopped during the initial delay
        while not stop.is_set():
            try:
                kills = self._ambient_fetch()
            except Exception as exc:             # never let the loop die on a fetch
                print(f"[MAP] ambient heat loop error: {exc}")
                kills = None
            if kills and not stop.is_set():
                # Post the sorted (sid, count) pairs; _apply_ambient rebuilds the
                # dict on the main thread. A stale put after a hide is harmless --
                # the queue just holds it until the next show drains it.
                self._result_q.put(("ambient", tuple(sorted(kills.items()))))
            if stop.wait(AMBIENT_INTERVAL_S):
                return                           # stopped during the hourly wait

    def shutdown_ambient_loop(self) -> None:
        """Stop the ambient loop and join it briefly so a hidden tab leaks no
        fetch daemon (Task 22 lesson). Idempotent: safe when no loop is running.
        Setting the Event wakes a loop blocked in stop.wait() immediately; the
        join only has to outlast an in-flight request (~10 s worst case, but the
        loop re-checks is_set right after), so the thread exits promptly."""
        self._ambient_stop.set()
        t = self._ambient_thread
        if t is not None:
            t.join(timeout=2.0)
            self._ambient_thread = None

    def _fetch_ambient_heat(self):
        """Fetch hourly ESI ambient kills (public endpoint, no auth) ->
        ``{system_id: ship+pod kills}``, or None on any failure (silent-degrade to
        zkill-only heat). Uses the repo rate limiter + ESI_HEADERS. Runs ONLY on
        the ambient loop thread.

        JUMPS DECISION (Task 30, owner delegated "skip jumps entirely if you judge
        it dead weight"): the sibling /universe/system_jumps/ endpoint is NOT
        fetched. Nothing consumes jump counts yet -- the future hover tooltip that
        would use them is unbuilt -- so fetching them would be dead weight AND a
        second ESI call for zero rendered effect. We issue ONE call/hour, well
        under the owner-approved 2/hour budget. Add the jumps fetch alongside the
        tooltip that needs it."""
        try:
            import requests
            from esi_constants import ESI_BASE, ESI_HEADERS
            from rate_limiter import rate_limit
            rate_limit("esi")
            resp = requests.get(f"{ESI_BASE}/universe/system_kills/",
                                timeout=10, headers=ESI_HEADERS)
            if not resp.ok:
                return None
            return mo.parse_system_kills(resp.json())
        except Exception as exc:
            print(f"[MAP] ambient heat fetch failed: {exc}")
            return None

    def _apply_ambient(self, pairs) -> None:
        """Apply worker-fetched hourly ambient kills on the MAIN thread (drained
        from the result queue by _drain_results). Stores the {sid: ship+pod}
        counts; when the heat layer is on, force-dirties + re-requests a crisp so
        the low ambient band updates. ``pairs`` is the sorted ((sid, count), ...)
        tuple the ambient loop posted."""
        self.state.ambient_heat = {int(sid): int(c) for sid, c in pairs}
        if self._layer_on("heat"):
            self._last_heat_refresh_ms = _now_ms()
            self.state.force_dirty()
            self._request_crisp()
            self._redraw_overlays()

    # ---- characters overlay poll ----------------------------------------------
    def _start_chars_loop(self) -> None:
        """Start the map-chars poll thread. Idempotent: a no-op when the layer is
        off or a loop is already running. Mirrors _start_ambient_loop; the caller
        (on_shown / the toolbar toggle) has already confirmed the tab is shown."""
        if not self._layer_on("chars"):
            return
        if self._chars_thread is not None and self._chars_thread.is_alive():
            return
        self._chars_stop.clear()
        self._chars_thread = threading.Thread(
            target=self._chars_loop, daemon=True, name="map-chars")
        self._chars_thread.start()

    def _chars_loop(self) -> None:
        """Daemon body: after a short first-fetch delay, fetch the authed
        characters' locations+ships and post a canonical snapshot onto the result
        queue, then repeat every CHARS_POLL_S. The stop Event is WAITED on (not
        slept) for both the initial delay and the interval so shutdown_chars_loop
        wakes it the instant the event is set -- no leak past a hide/toggle-off.
        The fetch silent-degrades (per-character inside fc_gui; a total failure
        returns None here) -> a None result is simply not posted, leaving the last
        snapshot on screen; an empty dict IS posted so markers clear when every
        character has logged off. Runs on this thread only -> the injected fetch
        must be Tk-free."""
        stop = self._chars_stop
        if stop.wait(CHARS_START_DELAY_S):
            return                               # stopped during the initial delay
        while not stop.is_set():
            try:
                payload = self._characters_fetch()
            except Exception as exc:             # never let the loop die on a fetch
                print(f"[MAP] characters loop error: {exc}")
                payload = None
            if payload is not None and not stop.is_set():
                # A stale put after a hide is harmless -- the queue just holds it
                # until the next show drains it (and on_hidden left state.chars as-is).
                self._result_q.put(("chars", _canonical_chars(payload)))
            if stop.wait(CHARS_POLL_S):
                return                           # stopped during the interval wait

    def shutdown_chars_loop(self) -> None:
        """Stop the chars poll and join it briefly so a hidden tab / disabled layer
        leaks no daemon (Task 22 lesson). Idempotent: safe when no loop is running.
        Setting the Event wakes a loop blocked in stop.wait() immediately; the join
        only has to outlast an in-flight sweep, then the handle is dropped so a
        later _start_chars_loop spawns a fresh thread."""
        self._chars_stop.set()
        t = self._chars_thread
        if t is not None:
            t.join(timeout=2.0)
            self._chars_thread = None

    def _apply_chars(self, pairs) -> None:
        """Apply a worker-fetched characters snapshot on the MAIN thread (drained
        from the result queue). ``pairs`` is the canonical ((sid, ((char, ship),
        ...)), ...) tuple the poll posted. Rebuilds state.chars and -- when the
        layer is on -- repaints the overlays so the magenta markers track the
        latest positions (an empty tuple clears them). Pure Tk overlay: no crisp
        re-render (the base bitmap is unaffected)."""
        self.state.chars = {int(sid): list(sp) for sid, sp in pairs}
        if self._layer_on("chars"):
            self._redraw_overlays()

    def _chars_hover_lines(self, sid) -> list[str] | None:
        """Characters hover-tooltip provider: one ``"CharName — ShipType"`` line
        per character currently in the hovered system, sorted by name (the stored
        snapshot is already name-sorted by _canonical_chars). None when no tracked
        character is there. Gated on _layer_on("chars") by the hover engine."""
        chars = self.state.chars
        if not chars:
            return None
        here = chars.get(sid)
        if not here:
            return None
        return [f"{name} — {ship}" for name, ship in here]

    # ---- intel pulse layer (Task 31) ------------------------------------------
    def add_intel_pulse(self, system_id_or_name, now=None) -> None:
        """Record/refresh an intel mention of a system so it pulses on the map.
        Accepts a system id (int / numeric str) OR a system NAME (resolved via the
        model's exact case-insensitive name index; anything unresolvable -> a silent
        no-op). Called on the MAIN thread (fc_gui marshals the intel stream through
        _post_ui before pushing, so no extra hop here). Stamps the mention with
        wall-clock time; when the layer is on, rebuilds the ring overlay at once so a
        fresh call lights up immediately (the tick loop then animates it).
        Guarded/no-throw so a malformed push never breaks the intel path."""
        sid = self._resolve_intel_target(system_id_or_name)
        if sid is None:
            return
        ts = time.time() if now is None else now
        try:
            self.state.intel_pulses.note(sid, ts)
        except Exception as exc:
            print(f"[MAP] intel pulse note failed: {exc}")
            return
        if self._layer_on("intel"):
            self._redraw_overlays()          # draw the new ring now
            self._schedule_tick()            # ensure the tick loop animates it

    def _resolve_intel_target(self, system_id_or_name):
        """Resolve an intel push target to a system id. int / numeric-str -> that id;
        a name -> the model's exact (case-insensitive) name index; anything
        unresolvable (unknown name, empty, bool, wrong type) -> None (silent skip)."""
        v = system_id_or_name
        if isinstance(v, bool):
            return None
        if isinstance(v, int):
            return v
        if isinstance(v, str):
            q = v.strip()
            if not q:
                return None
            if q.lstrip("-").isdigit():
                try:
                    return int(q)
                except ValueError:
                    return None
            return self.state._name_to_id.get(q.lower())
        return None

    def _intel_osc(self, now_ms: float) -> float:
        """Shared 0..1 oscillator for the pulse ring (time-based phase so every ring
        breathes in unison)."""
        return 0.5 + 0.5 * math.sin((now_ms / INTEL_PULSE_MS) * _TWO_PI)

    def _intel_radius(self, intensity: float, now_ms: float) -> float:
        """Oscillating ring radius: base R0 plus a sinusoid whose amplitude scales
        with the mention's decay intensity (fresh -> full throb, old -> nearly
        static)."""
        return INTEL_R0 + INTEL_AMP * intensity * self._intel_osc(now_ms)

    def _intel_width(self, intensity: float, now_ms: float) -> int:
        """Oscillating ring line width (1..3 px); amplitude scaled by intensity so a
        decayed pulse also thins out."""
        return 1 + int(round(2.0 * intensity * self._intel_osc(now_ms)))

    def _animate_intel_pulses(self, now_ms: float) -> None:
        """Per-tick intel-pulse animation. HARD empty-fast-path FIRST: zero extra
        work when the layer is off or nothing is pulsing (the common case) -- one
        dict lookup + one O(1) truthiness, then return. While pulses are live, MUTATE
        the existing ring items in place (coords + width) so the hot 16 ms path never
        allocates or delete/recreates. Structure changes (culling an expired mention,
        reprojecting the set) are batched to ~1 s via a cheap time gate that defers to
        _redraw_overlays -- the same idiom as the Task 30 heat-refresh gate."""
        if not self._layer_on("intel"):
            return
        pulses = self.state.intel_pulses
        if not pulses.has_any():
            return
        if now_ms - self._intel_struct_ms >= INTEL_STRUCT_MS:
            self._redraw_overlays()          # cull + reproject; resets _intel_struct_ms
            return
        if not self._intel_items or self.state.model is None:
            return
        cam = self.state.camera
        vw, vh = self.state.vw, self.state.vh
        systems = self.state.model.systems
        canvas = self.canvas
        for sid, item in self._intel_items.items():
            s = systems.get(sid)
            if s is None:
                continue
            intensity = self._intel_cache.get(sid, 1.0)
            sx, sy = cam.world_to_screen(s.x, s.y, vw, vh)
            r = self._intel_radius(intensity, now_ms)
            try:
                canvas.coords(item, sx - r, sy - r, sx + r, sy + r)
                canvas.itemconfigure(item, width=self._intel_width(intensity, now_ms))
            except tk.TclError:
                pass

    def _intel_click_hit(self, sx: float, sy: float) -> bool:
        """Fire the ``on_intel_click`` callback if (sx, sy) lands within an active
        pulse's hit radius (nearest pulse wins). Returns True when a pulse consumed
        the click. No-op (False) when the layer is off, no callback is wired, no
        model, or no pulse is near. Guarded so a bad callback never breaks the
        click-release path."""
        if not self._layer_on("intel") or self.state.model is None:
            return False
        cb = self.callbacks.get("on_intel_click")
        if cb is None:
            return False
        active = self.state.intel_pulses.active(time.time())
        if not active:
            return False
        cam = self.state.camera
        vw, vh = self.state.vw, self.state.vh
        systems = self.state.model.systems
        best = None
        best_d2 = None
        for sid in active:
            s = systems.get(sid)
            if s is None:
                continue
            px, py = cam.world_to_screen(s.x, s.y, vw, vh)
            d2 = (px - sx) ** 2 + (py - sy) ** 2
            if d2 <= INTEL_HIT_R ** 2 and (best_d2 is None or d2 < best_d2):
                best, best_d2 = sid, d2
        if best is None:
            return False
        try:
            cb(best)
        except Exception as exc:
            print(f"[MAP] intel click callback failed: {exc}")
        return True

    # ---- kill-ping layer (Task 36) --------------------------------------------
    def add_kill_ping(self, system_id, capital=False, count=1, now=None) -> None:
        """Record a discrete zkill ALERT ping so its system radar-bursts on the map
        (Task 36). Called on the MAIN thread -- fc_gui fires this from the report-
        render path (_show_zkill_alert -> _push_kill_ping_to_map) AFTER the display
        gates pass, so pings match rendered reports 1:1 (NOT alongside the broad
        kill-heat push, which stays wider by design -- owner's "4 pings, 1 report"
        fix). Stamps the ping with wall-clock time (the alert just fired; zkill is
        near-real-time). When the layer is on,
        rebuilds the overlay at once so the burst lights up immediately (the tick
        loop then animates it). INDEPENDENTLY gated from heat by its own layer
        toggle. Guarded/no-throw so a malformed alert never breaks the zkill path."""
        ts = time.time() if now is None else now
        try:
            self.state.kill_pings.ping(system_id, ts, capital=bool(capital),
                                       count=count)
        except Exception as exc:
            print(f"[MAP] kill ping note failed: {exc}")
            return
        if self._layer_on("kill_pings"):
            self._redraw_overlays()          # draw the new burst now
            self._schedule_tick()            # ensure the tick loop animates it

    def focus_kill(self, system_id) -> None:
        """Center the camera on ``system_id`` at a readable zoom and REPLAY its burst
        (Task 36). Invoked by fc_gui's Intelligence-tab '[Map]' affordance after
        selecting the Map tab. Robust to the tab not having been shown yet: because
        the <<NotebookTabChanged>> that runs on_shown fires ASYNCHRONOUSLY (verified
        empirically), this can't center before on_shown's restore_camera runs, so the
        target is DEFERRED to _apply_pending_focus (called at the end of on_shown,
        after restore_camera) and applied there. When the map is already the visible
        tab, centers immediately. Silently no-ops on a bad / unknown id."""
        if isinstance(system_id, bool):
            return
        try:
            sid = int(system_id)
        except (TypeError, ValueError):
            return
        known = self.state.model is not None and sid in self.state.model.systems
        # Retrigger the burst so it replays on arrival (no-op if the ping was culled).
        try:
            self.state.kill_pings.retrigger(sid, time.time())
        except Exception:
            pass
        if self._visible and known:
            self._center_on_kill(sid)        # already shown -> focus now
        elif not self._visible:
            # Hidden / never-shown: defer to on_shown (the model may load only then;
            # deferring past restore_camera also prevents a camera clobber). We stash
            # regardless of `known` -- on_shown re-checks the id against the model.
            self._pending_focus_sid = sid
        # visible + unknown id -> silent no-op (nothing to center on)

    def _center_on_kill(self, sid: int) -> None:
        """Recenter + zoom the camera onto ``sid`` (Task 36). Reuses fly_to's camera
        pathway: set center to the system's world coords and bump to a readable zoom
        (max_scale / 3), then force a crisp re-render + overlay repaint. No-op on an
        unknown id."""
        s = self.state.model.systems.get(sid) if self.state.model is not None else None
        if s is None:
            return
        cam = self.state.camera
        cam.cx, cam.cy = s.x, s.y
        cam.scale = max(cam.scale, cam.max_scale / 3.0)
        self.state.force_dirty()
        self._request_crisp()
        self._redraw_overlays()
        self._schedule_tick()

    def _apply_pending_focus(self) -> None:
        """Apply a focus_kill target that was deferred until the tab was shown (Task
        36). Called at the END of on_shown -- AFTER restore_camera -- so the kill
        focus wins over the restored camera. Retriggers the burst so it replays on
        arrival, then centers. No-op when nothing is pending or the id is unknown to
        the (now-loaded) model."""
        sid = self._pending_focus_sid
        if sid is None:
            return
        self._pending_focus_sid = None
        if self.state.model is None or sid not in self.state.model.systems:
            return
        try:
            self.state.kill_pings.retrigger(sid, time.time())
        except Exception:
            pass
        self._center_on_kill(sid)

    def _killping_ring(self, k: int, n: int, span: float,
                       now_ms: float) -> tuple[float, int]:
        """Radar ring (radius, width) for emanating ring ``k`` of ``n`` at ``now_ms``.
        Each ring rides the shared sweep phase offset by k/n so the rings chase each
        other outward continuously; radius grows from KILLPING_R0 across ``span`` as
        the phase advances, and the line THINS (3 -> 1 px) as it expands so a ring
        visually fades out at the edge (Tk ovals have no alpha)."""
        ph = ((now_ms / KILLPING_SWEEP_MS) + (k / n)) % 1.0
        r = KILLPING_R0 + ph * span
        w = max(1, int(round(3.0 * (1.0 - ph))))
        return r, w

    def _animate_kill_pings(self, now_ms: float) -> None:
        """Per-tick kill-ping animation (Task 36). HARD empty-fast-path FIRST: zero
        work when the layer is off or nothing is pinging (one dict-get + one O(1)
        truthiness, then return) -- so ~100 idle ticks cause zero canvas mutation.
        While bursts are live, MUTATE the existing burst ring items (coords + width)
        from the shared radar sweep so the hot 16 ms path never allocates or
        delete/recreates; the linger markers are static (skipped). Structure changes
        (burst->linger, cull, reproject) are batched to ~1 s (KILLPING_STRUCT_MS) via
        a cheap time gate that defers to _redraw_overlays -- the intel-pulse idiom."""
        if not self._layer_on("kill_pings"):
            return
        pings = self.state.kill_pings
        if not pings.has_any():
            return
        if now_ms - self._killping_struct_ms >= KILLPING_STRUCT_MS:
            self._redraw_overlays()          # cull + reproject + stage flips
            return
        if not self._killping_items or self.state.model is None:
            return
        cam = self.state.camera
        vw, vh = self.state.vw, self.state.vh
        systems = self.state.model.systems
        canvas = self.canvas
        for sid, ids in self._killping_items.items():
            cached = self._killping_cache.get(sid)
            if cached is None or cached[0] != "burst":
                continue                     # linger markers are static -> no tween
            s = systems.get(sid)
            if s is None:
                continue
            cap = cached[1]
            n = KILLPING_RINGS_CAP if cap else KILLPING_RINGS
            span = KILLPING_SPAN_CAP if cap else KILLPING_SPAN
            sx, sy = cam.world_to_screen(s.x, s.y, vw, vh)
            for k, item in enumerate(ids):
                r, w = self._killping_ring(k, n, span, now_ms)
                try:
                    canvas.coords(item, sx - r, sy - r, sx + r, sy + r)
                    canvas.itemconfigure(item, width=w)
                except tk.TclError:
                    pass

    # ---- sovereignty tint layer (Task 33) -------------------------------------
    def _sov_request_value(self):
        """Canonical hashable sov tuple for the render request + sig, or None when
        the sov layer is off OR there is no sov data -- both collapse to None so a
        sov-off (or on-but-empty) frame is byte-identical to the pre-sov output and
        its sig matches, keeping duplicate suppression sound."""
        if not self._layer_on("sov"):
            return None
        return mo.canonical_sov(self.state.sov_map) or None

    def _infra_request_value(self):
        """Canonical hashable infra tuple for the render request + sig, or None
        when the layer is off OR there are no badges -- both collapse to None so an
        infra-off (or on-but-empty) frame is byte-identical to the pre-infra output
        and its sig matches (determinism, exactly like sov/heat/bridges). The tuple
        is ((system_id, total, top_category, stale), ...), sorted for determinism;
        it is BOTH the sig component AND the exact data the renderer iterates
        (map_render._draw_infra_chips)."""
        if not self._layer_on("infra"):
            return None
        infra = self.state.infra
        if not infra:
            return None
        return tuple(sorted(
            (sid, b["total"], b["top"], b["stale"]) for sid, b in infra.items()))

    def _emit_infra_filters(self) -> None:
        """Sync the enabled flag from the toolbar Infra var and hand a private
        deepcopy of the filter state to the host's get_infrastructure callback,
        which recomputes badges and pushes them back via set_infrastructure. A COPY
        so a host that retains the dict can't mutate our internal filter state (and
        vice-versa). No-op (beyond the enabled sync) when no host is wired."""
        var = self._layer_vars.get("infra")
        if var is not None:
            self.state.infra_filters["enabled"] = bool(var.get())
        cb = self.callbacks.get("get_infrastructure")
        if cb is not None:
            cb(copy.deepcopy(self.state.infra_filters))

    def _maybe_start_sov_fetch(self, now_ms: float | None = None) -> None:
        """Spawn the ONE-SHOT sov fetch thread iff the layer is on, no fetch is in
        flight, and the data is stale (older than SOV_REFRESH_S). OFF by default ->
        never spawns until the owner enables the layer (zero network). Idempotent:
        the in-flight flag blocks a double-spawn (a hide/show while a fetch runs, or
        the per-tick gate racing a toggle), and the freshness gate blocks an
        immediate re-fetch on a toggle within the hour. The thread dies after one
        fetch -- no loop, nothing to join on hide. Cheap-gate-first so the per-tick
        call costs a dict-get + bool when the layer is off."""
        if not self._layer_on("sov"):
            return
        if self._sov_inflight:
            return
        now = _now_ms() if now_ms is None else now_ms
        # Throttle EVERY attempt (success, failure OR empty result) to at most one
        # per SOV_REFRESH_S by stamping the attempt time at SPAWN, not only on
        # success: a failed/empty fetch leaves sov_map empty, so a success-only
        # stamp gated on sov_map would leave the gate permanently open and
        # retry-STORM the endpoint every tick. _sov_fetched_ms == 0 is the
        # never-fetched sentinel, so the first enable always fetches.
        if self._sov_fetched_ms > 0.0 and \
                (now - self._sov_fetched_ms) < SOV_REFRESH_S * 1000.0:
            return                               # attempted recently -> no re-fetch
        self._sov_fetched_ms = now               # stamp the ATTEMPT (storm guard)
        self._sov_inflight = True
        threading.Thread(target=self._sov_fetch_worker, daemon=True,
                         name="map-sov").start()

    def _sov_fetch_worker(self) -> None:
        """One-shot daemon body (Task 33): fetch the sov map, then bulk-resolve the
        distinct alliance names, posting each result onto the MAIN-thread result
        queue. Touches NO Tk. ALWAYS posts a ('sov', payload|None) message so the
        main thread clears the in-flight flag on every outcome (a None payload =
        failed/empty fetch -> flag cleared, sov_map + freshness stamp left untouched
        so the next enable retries). A successful map ALSO triggers a best-effort
        name resolve posted as ('sov_names', ...); a name failure still leaves the
        tint applied with raw-id legend entries (silent degrade). Exits after one
        pass -- there is no loop to leak."""
        try:
            mapping = self._sov_fetch()
        except Exception as exc:                 # never let the one-shot die noisily
            print(f"[MAP] sov fetch failed: {exc}")
            mapping = None
        if not mapping:
            self._result_q.put(("sov", None))    # failure -> clear in-flight, keep stale
            return
        self._result_q.put(("sov", tuple(sorted(mapping.items()))))
        try:
            ids = sorted(set(mapping.values()))
            names = self._names_fetch(ids) if ids else {}
        except Exception as exc:
            print(f"[MAP] sov name resolve failed: {exc}")
            names = None
        if names:
            self._result_q.put(("sov_names", tuple(sorted(names.items()))))

    def _apply_sov(self, pairs) -> None:
        """Apply a worker-fetched sov map on the MAIN thread (drained from the result
        queue). ALWAYS clears the in-flight flag so a later refresh can spawn again.
        A None payload is a failed/empty fetch: clear the flag and leave sov_map
        untouched (map stays untinted); the SPAWN-time freshness stamp
        (_maybe_start_sov_fetch) throttles the next attempt to the hourly gate --
        silent degrade, no retry storm. A real payload replaces sov_map, re-stamps
        the fetch time (refreshing the hourly gate) and -- when the layer is on --
        force-dirties + re-requests a crisp so the tint appears, plus a redraw so
        the right-click info row reflects it."""
        self._sov_inflight = False
        if pairs is None:
            return
        self.state.sov_map = {int(sid): int(aid) for sid, aid in pairs}
        self._sov_fetched_ms = _now_ms()
        if self._layer_on("sov"):
            self.state.force_dirty()
            self._request_crisp()
            self._redraw_overlays()

    def _apply_sov_names(self, pairs) -> None:
        """Apply worker-resolved alliance names on the MAIN thread (legend data).
        Does NOT trigger a re-render -- names only enrich the legend popup and the
        right-click info row, which read state.sov_names lazily when opened."""
        self.state.sov_names = {int(aid): str(name) for aid, name in pairs}

    def _fetch_sov_map(self):
        """Fetch ESI /sovereignty/map/ (public, no auth, long-cached) ->
        {system_id: alliance_id}, or None on any failure (silent-degrade -> the map
        stays untinted). Uses the repo rate limiter + ESI_HEADERS. Runs ONLY on the
        one-shot sov thread."""
        try:
            import requests
            from esi_constants import ESI_BASE, ESI_HEADERS
            from rate_limiter import rate_limit
            rate_limit("esi")
            resp = requests.get(f"{ESI_BASE}/sovereignty/map/",
                                timeout=10, headers=ESI_HEADERS)
            if not resp.ok:
                return None
            return mo.parse_sov_map(resp.json())
        except Exception as exc:
            print(f"[MAP] sov map fetch failed: {exc}")
            return None

    def _fetch_alliance_names(self, alliance_ids):
        """Bulk-resolve alliance ids -> {id: name} via POST ESI /universe/names/
        (public, <=1000 ids/call, JSON body = list of ids), or None on failure (the
        legend then falls back to raw ids). Keeps only 'alliance'-category rows.
        Chunks defensively at 1000 though EVE has far fewer alliances. Runs ONLY on
        the one-shot sov thread."""
        try:
            import requests
            from esi_constants import ESI_BASE, ESI_HEADERS_JSON
            from rate_limiter import rate_limit
            out: dict[int, str] = {}
            ids = [int(a) for a in alliance_ids]
            for i in range(0, len(ids), 1000):
                chunk = ids[i:i + 1000]
                if not chunk:
                    continue
                rate_limit("esi")
                resp = requests.post(f"{ESI_BASE}/universe/names/",
                                     json=chunk, timeout=10,
                                     headers=ESI_HEADERS_JSON)
                if not resp.ok:
                    return None
                for row in resp.json() or ():
                    if row.get("category") == "alliance":
                        out[int(row["id"])] = row.get("name") or str(row["id"])
            return out
        except Exception as exc:
            print(f"[MAP] alliance name resolve failed: {exc}")
            return None

    # ---- sov legend popup (Task 33) -------------------------------------------
    def _sync_sov_legend_btn(self) -> None:
        """Enable the Sov legend "▾" microbutton only while the sov layer is on."""
        btn = getattr(self, "_sov_legend_btn", None)
        if btn is not None:
            try:
                btn.configure(state=("normal" if self._layer_on("sov")
                                     else "disabled"))
            except tk.TclError:
                pass

    def _show_sov_legend(self) -> None:
        """Popup a compact themed legend of the top sov alliances (color swatch +
        name + tinted-system count), anchored under the "▾" microbutton. Dismisses
        on click-away (focus-out) or Escape. Rebuilt fresh each open from the live
        sov map + resolved names (raw id when a name is unresolved). Matches the
        _make_menu dark idiom -- a 1px border-color frame around a panel-bg body."""
        self._hide_sov_legend()
        t = self.theme
        rows = mo.sov_legend_rows(self.state.sov_map, self.state.sov_names)
        try:
            anchor = getattr(self, "_sov_legend_btn", None) or self.frame
            x = anchor.winfo_rootx()
            y = anchor.winfo_rooty() + anchor.winfo_height() + 2
            top = self._sov_legend = tk.Toplevel(anchor)
            top.wm_overrideredirect(True)
            top.wm_geometry(f"+{x}+{y}")
            top.configure(bg=t["border"])                # 1px themed border
            inner = tk.Frame(top, bg=t["panel"])
            inner.pack(padx=1, pady=1, fill="both")
            tk.Label(inner, text="Sovereignty", bg=t["panel"], fg=t["accent"],
                     font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=8,
                                                         pady=(6, 2))
            if not rows:
                tk.Label(inner, text="No sovereignty data yet.", bg=t["panel"],
                         fg=t["fg"], font=("Segoe UI", 8)).pack(anchor="w", padx=8,
                                                                pady=(0, 6))
            for _aid, label, cnt, rgb in rows:
                r = tk.Frame(inner, bg=t["panel"])
                r.pack(fill="x", padx=8, pady=1)
                sw = tk.Frame(r, bg="#%02x%02x%02x" % rgb, width=12, height=12)
                sw.pack_propagate(False)
                sw.pack(side="left", padx=(0, 6))
                tk.Label(r, text=f"{label}  ({cnt})", bg=t["panel"], fg=t["fg"],
                         font=("Segoe UI", 8), anchor="w").pack(side="left")
            tk.Frame(inner, bg=t["panel"], height=4).pack()
            top.bind("<Escape>", lambda _e: self._hide_sov_legend())
            top.bind("<FocusOut>", lambda _e: self._hide_sov_legend())
            top.focus_set()
        except Exception:
            self._sov_legend = None

    def _hide_sov_legend(self) -> None:
        top = getattr(self, "_sov_legend", None)
        if top is not None:
            try:
                top.destroy()
            except Exception:
                pass
            self._sov_legend = None

    # ---- infrastructure filter popover (Task 5) -------------------------------
    def _show_infra_filters(self) -> None:
        """Open the borderless infra filter popover under the "▾" microbutton:
        per-category toggles, a Stale-only toggle, and a Manage… button. Built
        once and REUSED per open (withdrawn on close, never destroyed); never
        grabs the pointer; dismisses on Escape or click-away (FocusOut). Matches
        the sov-legend dark idiom -- a 1px border-color frame around a panel body."""
        try:
            pop = self._infra_popover
            if pop is None or not pop.winfo_exists():
                pop = self._infra_popover = self._build_infra_popover()
            # Reflect the live filter state onto the checkbutton vars before showing
            # (defensive -- the popover is their only writer, but keep them in sync).
            for _label, key in _INFRA_CATEGORY_LABELS:
                self._infra_cat_vars[key].set(
                    bool(self.state.infra_filters["categories"].get(key, True)))
            self._infra_stale_var.set(bool(self.state.infra_filters["stale_only"]))
            # Re-sync the per-type boxes from state (types=None -> all checked;
            # else checked iff the type_id is in the restriction set). Defensive --
            # the popover is their only writer, but keep them in lockstep.
            _types = self.state.infra_filters.get("types")
            for tid, var in self._infra_type_vars.items():
                var.set(_types is None or tid in _types)
            anchor = getattr(self, "_infra_filter_btn", None) or self.frame
            x = anchor.winfo_rootx()
            y = anchor.winfo_rooty() + anchor.winfo_height() + 2
            pop.wm_geometry(f"+{x}+{y}")
            pop.deiconify()
            pop.lift()
            pop.focus_set()
        except Exception:
            self._infra_popover = None

    def _build_infra_popover(self) -> tk.Toplevel:
        t = self.theme
        pop = tk.Toplevel(self.frame)
        pop.wm_overrideredirect(True)
        pop.withdraw()
        pop.configure(bg=t["border"])                    # 1px themed border
        inner = tk.Frame(pop, bg=t["panel"])
        inner.pack(padx=1, pady=1, fill="both")
        tk.Label(inner, text="Show categories", bg=t["panel"], fg=t["accent"],
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=8, pady=(6, 2))
        for label, key in _INFRA_CATEGORY_LABELS:
            tk.Checkbutton(
                inner, text=label, variable=self._infra_cat_vars[key], anchor="w",
                bg=t["panel"], fg=t["fg"], selectcolor=t["panel"],
                activebackground=t["entry_bg"], activeforeground=t["accent"],
                command=lambda k=key: self._on_infra_cat_toggle(k)
            ).pack(anchor="w", fill="x", padx=8)
            # Indented per-type checkboxes under any category that has known types
            # (from the injected index; npc/unknown never do). The master above is
            # unchanged (category on/off); these narrow WHICH types show.
            types = self._infra_types_by_cat.get(key)
            if types:
                self._build_infra_type_group(inner, types)
        tk.Frame(inner, bg=t["border"], height=1).pack(fill="x", padx=8, pady=4)
        tk.Checkbutton(
            inner, text="Stale only", variable=self._infra_stale_var, anchor="w",
            bg=t["panel"], fg=t["fg"], selectcolor=t["panel"],
            activebackground=t["entry_bg"], activeforeground=t["accent"],
            command=self._on_infra_stale_toggle
        ).pack(anchor="w", fill="x", padx=8)
        tk.Button(
            inner, text="Manage…", command=self._infra_manage_clicked, anchor="w",
            bg=t["panel"], fg=t["fg"], activebackground=t["entry_bg"],
            activeforeground=t["accent"], relief="flat", borderwidth=0,
            highlightthickness=0, cursor="hand2"
        ).pack(anchor="w", fill="x", padx=8, pady=(4, 6))
        pop.bind("<Escape>", lambda _e: self._hide_infra_filters())
        pop.bind("<FocusOut>", lambda _e: self._hide_infra_filters())
        return pop

    def _build_infra_type_group(self, parent, types) -> None:
        """Render the indented per-type checkboxes for one category group inside
        the infra popover. Two columns when a group has >3 types (keeps the popover
        compact -- Citadels has 8), a single column otherwise. Each box drives
        self._infra_type_vars[type_id] and re-emits via _on_infra_type_toggle."""
        t = self.theme
        grp = tk.Frame(parent, bg=t["panel"])
        grp.pack(anchor="w", fill="x", padx=(26, 8))
        two_col = len(types) > 3
        for i, (tid, name) in enumerate(types):
            cb = tk.Checkbutton(
                grp, text=name, variable=self._infra_type_vars[tid], anchor="w",
                bg=t["panel"], fg=t["fg"], selectcolor=t["panel"],
                activebackground=t["entry_bg"], activeforeground=t["accent"],
                font=("Segoe UI", 8),
                command=lambda i_tid=tid: self._on_infra_type_toggle(i_tid))
            if two_col:
                cb.grid(row=i // 2, column=i % 2, sticky="w", padx=(0, 10))
            else:
                cb.grid(row=i, column=0, sticky="w")

    def _hide_infra_filters(self) -> None:
        pop = getattr(self, "_infra_popover", None)
        if pop is not None:
            try:
                pop.withdraw()                           # reused -> withdraw, not destroy
            except Exception:
                pass

    def _on_infra_cat_toggle(self, key: str) -> None:
        var = self._infra_cat_vars.get(key)
        if var is not None:
            state = bool(var.get())
            self.state.infra_filters["categories"][key] = state
            # Master mirrors onto its children (owner ask): ticking/unticking a
            # category sets every per-type box in that group to match, then folds
            # the whole type selection back into filters["types"].
            for tid, _name in self._infra_types_by_cat.get(key, []):
                tv = self._infra_type_vars.get(tid)
                if tv is not None:
                    tv.set(state)
            self._recompute_infra_types()
        self._emit_infra_filters()

    def _on_infra_type_toggle(self, type_id: int) -> None:
        self._recompute_infra_types()
        self._emit_infra_filters()

    def _recompute_infra_types(self) -> None:
        """Fold the per-type checkbox vars into filters["types"]: None when EVERY
        box is checked (no restriction -- byte-identical to the pre-feature emit,
        and the all-checked default), else the sorted tuple of checked type_ids.
        The SOLE writer of state.infra_filters["types"]. With no injected index
        (standalone/tests) there are no type vars, so this stays None."""
        checked = [tid for tid, var in self._infra_type_vars.items() if var.get()]
        if len(checked) == len(self._infra_type_vars):
            self.state.infra_filters["types"] = None
        else:
            self.state.infra_filters["types"] = tuple(sorted(checked))

    def _on_infra_stale_toggle(self) -> None:
        self.state.infra_filters["stale_only"] = bool(self._infra_stale_var.get())
        self._emit_infra_filters()

    def _infra_manage_clicked(self) -> None:
        """Popover 'Manage…' -> open the infra manager for the whole DB (no
        pre-filter). Hide the popover first so it does not linger over the dialog."""
        self._hide_infra_filters()
        cb = self.callbacks.get("open_infra_manager")
        if cb is not None:
            cb(None)

    # ---- hover tooltip (self-contained; no fc_gui import) -----------------------
    def _attach_tooltip(self, widget, text: str) -> None:
        """Attach a dwell-delayed hover tooltip to a toolbar control and register
        it in ``self._toolbar_tooltips`` (owner ask 2026-07-12). The tip appears
        ~500 ms after the cursor settles on the widget (never on a fly-over),
        reuses the single ``self._tooltip`` slot, and is dismissed instantly on
        <Leave>, ANY mouse press, or the widget's <Destroy> -- so it can never sit
        between the cursor and a click. ``add='+'`` so it never clobbers the
        widget's own bindings, and the timer is only armed on <Enter>, so an idle
        toolbar (cursor elsewhere) costs nothing. Empty text is a no-op. Shared by
        every toolbar control -- the layer checkboxes, the search box, and the
        Sov / Infra / Threat ▾ drawer buttons. The registry lets a test walk the
        toolbar and assert every control is documented (fails if a new one isn't)."""
        if not text:
            return
        self._toolbar_tooltips[widget] = text
        widget.bind("<Enter>",
                    lambda _e, w=widget, s=text: self._tooltip_schedule(w, s),
                    add="+")
        widget.bind("<Leave>", lambda _e: self._hide_tooltip(), add="+")
        widget.bind("<ButtonPress>", lambda _e: self._hide_tooltip(), add="+")
        widget.bind("<Destroy>", lambda _e: self._hide_tooltip(), add="+")

    def _tooltip_schedule(self, widget, text: str) -> None:
        """Arm the dwell timer for ``widget``'s tip, first tearing down any pending
        timer / live tip so only the newest hover ever shows (after_cancel
        discipline). Uses self.frame.after so it rides the tab's own event loop."""
        self._hide_tooltip()
        try:
            self._tooltip_after = self.frame.after(
                self._tooltip_delay_ms, lambda: self._show_tooltip(widget, text))
        except Exception:
            self._tooltip_after = None

    def _show_tooltip(self, widget, text: str) -> None:
        """Create + place the tip NOW below ``widget`` in the single ``self._tooltip``
        slot. Called by the dwell timer (and directly by tests). Any prior tip is
        torn down first; a torn-down widget mid-dwell degrades to no tip (guarded)."""
        self._hide_tooltip()
        t = self.theme
        try:
            x = widget.winfo_rootx()
            y = widget.winfo_rooty() + widget.winfo_height() + 2
            tip = self._tooltip = tk.Toplevel(widget)
            tip.wm_overrideredirect(True)
            tip.wm_geometry(f"+{x}+{y}")
            tk.Label(tip, text=text, justify="left", wraplength=280,
                     bg=t["panel"], fg=t["fg"], relief="solid", borderwidth=1,
                     padx=6, pady=4).pack()
        except Exception:
            self._tooltip = None

    def _hide_tooltip(self) -> None:
        """Single dismissal path: cancel a pending dwell timer AND destroy any live
        tip (idempotent). <Leave>, a press, a destroy, and the next <Enter> all
        route here, so a tip never lingers or stacks."""
        aid = getattr(self, "_tooltip_after", None)
        if aid is not None:
            try:
                self.frame.after_cancel(aid)
            except Exception:
                pass
            self._tooltip_after = None
        tip = getattr(self, "_tooltip", None)
        if tip is not None:
            try:
                tip.destroy()
            except Exception:
                pass
            self._tooltip = None

    def _layer_on(self, key: str) -> bool:
        # Most layers default ON when cfg omits the key; sov (and any future
        # off-by-default layer) defaults OFF -- see _LAYERS_OFF_BY_DEFAULT.
        default = key not in _LAYERS_OFF_BY_DEFAULT
        return bool(self.cfg.get("layers", {}).get(key, default))

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
                 "ov_origin", "ov_own", "ov_route", "ov_capkill", "ov_intel",
                 "ov_killping", "ov_chars")
        for tag in _tags:
            canvas.delete(tag)
        # Task 31 / Task 36: the ov_intel + ov_killping items were just deleted ->
        # drop their stale ids so the dicts stay in lockstep with the canvas
        # (repopulated in the intel / kill-ping blocks below; left empty on the
        # no-model early-return, which is correct).
        self._intel_items = {}
        self._intel_cache = {}
        self._killping_items = {}
        self._killping_cache = {}
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

        # -- destination route (Task 35): gold dashed stargate hops + BRIDGE_BLUE
        # dash-dot Ansiblex hops (diamond endpoint markers) + a gold dest ring.
        # Drawn FIRST so node markers (staging / fleet / own) sit on top. Segments
        # are culled by endpoint visibility (draw when EITHER end is near-viewport)
        # so the route reaches one hop past the screen edge, like the node cull.
        rp = st.route_path
        if self._layer_on("route") and rp and len(rp) >= 2:
            _RCULL = 60.0

            def route_pt(sid):
                s = systems.get(sid)
                if s is None:
                    return None
                px, py = cam.world_to_screen(s.x, s.y, vw, vh)
                vis = (-_RCULL <= px <= vw + _RCULL and -_RCULL <= py <= vh + _RCULL)
                return (px, py, vis)

            for a_id, b_id, kind in mo.classify_route_segments(rp, st.bridges):
                pa, pb = route_pt(a_id), route_pt(b_id)
                if pa is None or pb is None or not (pa[2] or pb[2]):
                    continue
                if kind == "bridge":
                    # Brighter + wider than the resting bridge glow (map_render
                    # _draw_bridges draws up to 4px in dim(BRIDGE_BLUE, …)) so a
                    # route RIDING an Ansiblex reads instantly instead of vanishing
                    # blue-on-blue over the bridge line it sits on. Still the
                    # #3A86FF family + dashed, so it stays legible as a bridge hop.
                    canvas.create_line(pa[0], pa[1], pb[0], pb[1],
                                       fill=ROUTE_BRIDGE_HEX, width=5,
                                       dash=(10, 6), tags="ov_route")
                    if pa[2]:
                        self._draw_diamond(pa[0], pa[1], 6, ROUTE_BRIDGE_HEX, "ov_route")
                    if pb[2]:
                        self._draw_diamond(pb[0], pb[1], 6, ROUTE_BRIDGE_HEX, "ov_route")
                else:
                    canvas.create_line(pa[0], pa[1], pb[0], pb[1], fill=ROUTE_GOLD,
                                       width=2, dash=(6, 4), tags="ov_route")
        # destination ring marker (whenever a destination is set AND on-screen)
        if self._layer_on("route") and st.route_dest is not None:
            p = project(st.route_dest)
            if p is not None:
                sx, sy = p
                canvas.create_oval(sx - 11, sy - 11, sx + 11, sy + 11,
                                   outline=ROUTE_GOLD, width=2, tags="ov_route")
                canvas.create_oval(sx - 4, sy - 4, sx + 4, sy + 4,
                                   outline=ROUTE_GOLD, width=1, tags="ov_route")

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

        # -- fleet pins + high-contrast count chips, own-location ring
        # The member count is COMMAND-CRITICAL and must stay legible even where
        # systems pack tightly together (owner feedback): each count is bold fleet-
        # cyan text on a dark theme-panel chip with a bright cyan border, offset
        # above-right of the pin so the node glow / base-layer name label never sits
        # under it. The chips are tag_raise-d above EVERY other overlay at the end of
        # this method (counts win), and a cheap greedy pass below nudges colliding
        # chips apart. Chips carry BOTH ov_fleet (so the tag sweep at the top of this
        # method deletes them with the layer, and the legacy "count is an ov_fleet
        # text item" contract holds) AND ov_fleet_count (the raise + nudge group).
        # EMPTY-FAST-PATH: _fleet_chips stays [] when the layer is off or no fleet is
        # tracked, so the nudge loop AND the end-of-method raise are both skipped --
        # zero extra canvas work.
        _fleet_chips: list = []          # (rect_id, text_id, x0, y0, x1, y1) per chip
        if self._layer_on("fleet"):
            t = self.theme
            _chip_fill, _chip_edge = t["panel"], t["accent"]
            for sid, count in st.fleet.items():
                p = project(sid)
                if p is None:
                    continue
                sx, sy = p
                canvas.create_oval(sx - 6, sy - 6, sx + 6, sy + 6,
                                   fill="#00d4ff", outline="#ffffff", width=1,
                                   tags="ov_fleet")
                # Count text FIRST (so bbox can measure it) then a backing chip
                # lowered behind it -- the dark plate + bright border is what makes
                # the number readable over any node glow or adjacent label.
                txt = canvas.create_text(
                    sx + 10, sy - 11, anchor="w", text=str(count),
                    fill=_chip_edge, font=("Segoe UI", 9, "bold"),
                    tags=("ov_fleet", "ov_fleet_count"))
                bb = canvas.bbox(txt)
                if bb is None:                       # defensive: estimate if unmeasured
                    _w = 7 * len(str(count)) + 4
                    bb = (sx + 10, sy - 18, sx + 10 + _w, sy - 4)
                x0, y0, x1, y1 = bb[0] - 3, bb[1] - 1, bb[2] + 3, bb[3] + 1
                rect = canvas.create_rectangle(
                    x0, y0, x1, y1, fill=_chip_fill, outline=_chip_edge, width=1,
                    tags=("ov_fleet", "ov_fleet_count"))
                canvas.tag_lower(rect, txt)          # chip plate sits behind its count
                _fleet_chips.append((rect, txt, x0, y0, x1, y1))
            # De-overlap nudge (structure path only; <=~30 counted systems): push each
            # later chip that intersects an already-placed one straight DOWN past it,
            # bounded iterations, so every count stays readable. Chips with no
            # collision never move (dy stays 0 -> no canvas.move) -- distant counts
            # are left exactly where they landed.
            if len(_fleet_chips) > 1:
                _placed: list = []
                for _i, (rect, txt, x0, y0, x1, y1) in enumerate(_fleet_chips):
                    dy = 0.0
                    for _ in range(16):              # bounded cascade
                        _below = None
                        for (px0, py0, px1, py1) in _placed:
                            if x0 < px1 and px0 < x1 and y0 < py1 and py0 < y1:
                                _below = py1
                                break
                        if _below is None:
                            break
                        _shift = (_below - y0) + 2.0  # drop just past the blocker + gap
                        y0 += _shift; y1 += _shift; dy += _shift
                    if dy:
                        canvas.move(rect, 0, dy)
                        canvas.move(txt, 0, dy)
                    _placed.append((x0, y0, x1, y1))
                    _fleet_chips[_i] = (rect, txt, x0, y0, x1, y1)
            own = st.own_system_id
            if own is not None:
                p = project(own)
                if p is not None:
                    sx, sy = p
                    canvas.create_oval(sx - 10, sy - 10, sx + 10, sy + 10,
                                       outline="#ffffff", width=2, tags="ov_own")
                    canvas.create_oval(sx - 2, sy - 2, sx + 2, sy + 2,
                                       fill="#ffffff", outline="", tags="ov_own")
        # -- characters overlay (owner ask): a magenta filled SQUARE at each system
        # holding an authed character (a shape + hue both free in the palette --
        # distinct from the cyan fleet circles), with a count label when more than
        # one character is there; the hover tooltip names each character + ship.
        # Pure Tk -> crisp during gestures. EMPTY-FAST-PATH: the whole block is
        # skipped when the layer is off OR no character is tracked, so an enabled-
        # but-empty layer performs zero canvas ops here.
        if self._layer_on("chars") and st.chars:
            for sid, occ in st.chars.items():
                p = project(sid)
                if p is None:
                    continue
                sx, sy = p
                canvas.create_rectangle(sx - 5, sy - 5, sx + 5, sy + 5,
                                        fill=CHARS_MAGENTA, outline="#ffffff",
                                        width=1, tags="ov_chars")
                if len(occ) > 1:
                    canvas.create_text(sx + 9, sy, anchor="w", text=str(len(occ)),
                                       fill=CHARS_MAGENTA, font=("Segoe UI", 9),
                                       tags="ov_chars")
        # -- capital-kill markers (Task 30): a double-ring red marker at systems
        # with a capital kill in the last ~30 min. Gated by the heat layer (they
        # accompany the base-layer heat under-glow) and drawn LAST so they sit on
        # top of the heat glow and the other node markers. Pure Tk -> crisp during
        # gestures. capital_systems iterates the bounded (<=500) event ring, cheap
        # enough per frame.
        if self._layer_on("heat"):
            for sid in st.kill_heat.capital_systems(time.time()):
                p = project(sid)
                if p is None:
                    continue
                sx, sy = p
                canvas.create_oval(sx - 9, sy - 9, sx + 9, sy + 9,
                                   outline=CAPKILL_RED, width=2, tags="ov_capkill")
                canvas.create_oval(sx - 4, sy - 4, sx + 4, sy + 4,
                                   outline=CAPKILL_RED, width=2, tags="ov_capkill")
        # -- intel pulses (Task 31): amber oscillating rings at systems named in
        # tracked intel channels, fading over ~5 min. This is the STRUCTURE path --
        # it creates the ring items (drawn topmost, after the node markers) and
        # records each item id + intensity so the per-tick _animate_intel_pulses can
        # tween them in place without delete/recreate. Only un-expired mentions draw
        # (active() culls the rest); off-screen ones are skipped by `project`. Sets
        # the structure clock so the 1 s cull gate does not immediately re-fire.
        if self._layer_on("intel"):
            _now_ms_i = _now_ms()
            for sid, intensity in st.intel_pulses.active(time.time()).items():
                p = project(sid)
                if p is None:
                    continue
                sx, sy = p
                r = self._intel_radius(intensity, _now_ms_i)
                item = canvas.create_oval(
                    sx - r, sy - r, sx + r, sy + r, outline=INTEL_AMBER,
                    width=self._intel_width(intensity, _now_ms_i), tags="ov_intel")
                self._intel_items[sid] = item
                self._intel_cache[sid] = intensity
            self._intel_struct_ms = _now_ms_i
        # -- kill pings (Task 36): discrete zkill-ALERT radar bursts (vivid red,
        # expanding rings) that decay to a small steady diamond marker over ~5 min --
        # DISTINCT from the amber intel pulses and the ambient heat glow. STRUCTURE
        # path: create the burst rings / linger marker items (drawn topmost) and
        # cache each ping's (stage, capital) so the per-tick _animate_kill_pings can
        # tween the burst rings in place without delete/recreate. active() culls
        # expired pings; off-screen ones are skipped by `project`. Sets the structure
        # clock so the 1 s cull/stage-flip gate does not immediately re-fire.
        if self._layer_on("kill_pings"):
            _now_ms_k = _now_ms()
            for sid, pstate in st.kill_pings.active(time.time()).items():
                p = project(sid)
                if p is None:
                    continue
                sx, sy = p
                cap = pstate.capital
                if pstate.stage == "burst":
                    n = KILLPING_RINGS_CAP if cap else KILLPING_RINGS
                    span = KILLPING_SPAN_CAP if cap else KILLPING_SPAN
                    ids = []
                    for k in range(n):
                        r, w = self._killping_ring(k, n, span, _now_ms_k)
                        ids.append(canvas.create_oval(
                            sx - r, sy - r, sx + r, sy + r, outline=KILLPING_RED,
                            width=w, tags="ov_killping"))
                    self._killping_items[sid] = ids
                else:                        # linger: small steady diamond outline
                    mrk = KILLPING_MARK_R_CAP if cap else KILLPING_MARK_R
                    ids = [canvas.create_polygon(
                        sx, sy - mrk, sx + mrk, sy, sx, sy + mrk, sx - mrk, sy,
                        fill="", outline=KILLPING_RED, width=1, tags="ov_killping")]
                    if cap:                  # capital linger doubles the marker
                        m2 = mrk + 3.0
                        ids.append(canvas.create_polygon(
                            sx, sy - m2, sx + m2, sy, sx, sy + m2, sx - m2, sy,
                            fill="", outline=KILLPING_RED, width=1, tags="ov_killping"))
                    self._killping_items[sid] = ids
                self._killping_cache[sid] = (pstate.stage, cap)
            self._killping_struct_ms = _now_ms_k
        # Fleet member counts are command-critical -> lift the count chips (dark
        # plate + bright text) above EVERY other overlay just drawn (staging
        # diamonds, chars squares, capkill rings, intel / kill pulses, route, and
        # any infra hover target) so a densely-packed count is never occluded.
        # Single-arg tag_raise moves the whole group to the top of the display list
        # while preserving its internal order (each chip's plate stays behind its own
        # text). Guarded by _fleet_chips so the empty-fast-path adds no canvas work.
        if _fleet_chips:
            canvas.tag_raise("ov_fleet_count")
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
                                    bridges=req.get("bridges"),
                                    heat=req.get("heat"),
                                    sov=req.get("sov"),
                                    infra=req.get("infra"))
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
            # Canonical rounded heat tuple, or None when the layer is off / no
            # activity (byte-identical to pre-heat output). Joins _request_sig.
            "heat": self._heat_request_value(),
            # Canonical sov tuple, or None when the layer is off / no data
            # (byte-identical to pre-sov output). Joins _request_sig.
            "sov": self._sov_request_value(),
            # Canonical infra tuple ((sid, total, top, stale), ...), or None when
            # the layer is off / no badges (byte-identical to pre-infra output).
            # Joins _request_sig; the renderer draws it as per-system count chips.
            "infra": self._infra_request_value(),
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
            self._last_heat_refresh_ms = _now_ms()   # settle crisp already reflects decay
        elif verdict is None and self._heat_refresh_due(_now_ms()):
            # (c) periodic kill-heat decay refresh (Task 30): while the heat layer
            # is on and heat is live, re-request a crisp at most every
            # HEAT_REFRESH_MS so the red-orange under-glow shrinks as events decay,
            # even with no camera/kill activity. The pure _heat_refresh_due gate
            # (cheap time check first) keeps the heat merge off the hot 16 ms path.
            self._last_heat_refresh_ms = _now_ms()
            self.state.force_dirty()
            self._request_crisp()
        # (Task 31) intel-pulse animation: a cheap empty-fast-path when nothing is
        # pulsing (the common case); otherwise tween the amber rings, with structure
        # changes batched to ~1 s. Runs regardless of verdict -- pulses breathe
        # whether or not the camera is moving.
        self._animate_intel_pulses(_now_ms())
        # (Task 36) kill-ping radar animation: same cheap empty-fast-path idiom --
        # zero work when the layer is off or nothing is pinging; otherwise tween the
        # red burst rings (structure changes batched to ~1 s). Runs every tick so a
        # burst emanates whether or not the camera is moving.
        self._animate_kill_pings(_now_ms())
        # (Task 33) sov hourly re-fetch gate: fully guarded + cheap (layer-off /
        # in-flight / freshness checks short-circuit before any work), so calling it
        # each tick costs a dict-get + bool when the layer is off -- and spawns the
        # one-shot fetch at most once per SOV_REFRESH_S while the layer is on.
        self._maybe_start_sov_fetch()
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
        ms, cam), ('threat', frozenset), ('route', tuple), ('ambient', tuple),
        ('sov', tuple|None) and ('sov_names', tuple). Frames (crisp OR
        gesture) share one latest-wins slot -- the worker produces them in strictly
        increasing generation order, so the last one drained has the highest
        generation and any earlier one it superseded would be dropped by the
        is_current check in the apply anyway (a queued crisp thus correctly
        supersedes older gesture frames -- it also carries a newer cache). Threat,
        route, ambient and sov results are kept in SEPARATE slots so none is dropped
        by frame coalescing (nor a frame misread as one). Dispatch by tag: crisp and
        gesture take different apply paths (sig vs base-image realignment)."""
        latest_frame = None
        latest_threat = None
        latest_route = None
        latest_ambient = None
        latest_sov = None
        latest_sov_names = None
        latest_chars = None
        try:
            while True:
                item = self._result_q.get_nowait()
                if item[0] == "threat":
                    latest_threat = item
                elif item[0] == "route":
                    latest_route = item
                elif item[0] == "ambient":
                    latest_ambient = item        # Task 30: hourly ESI ambient heat
                elif item[0] == "sov":
                    latest_sov = item            # Task 33: sov map (one-shot fetch)
                elif item[0] == "sov_names":
                    latest_sov_names = item       # Task 33: alliance-name legend data
                elif item[0] == "chars":
                    latest_chars = item          # characters overlay (60 s poll)
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
        if latest_route is not None:
            self._apply_route(latest_route[1])
        if latest_ambient is not None:
            self._apply_ambient(latest_ambient[1])
        # sov names BEFORE sov so a same-drain names+map pair leaves the legend
        # populated when the sov-triggered redraw runs (order is belt-and-suspenders
        # -- the legend reads state lazily on open regardless).
        if latest_sov_names is not None:
            self._apply_sov_names(latest_sov_names[1])
        if latest_sov is not None:
            self._apply_sov(latest_sov[1])
        if latest_chars is not None:
            self._apply_chars(latest_chars[1])

    # ---- events --------------------------------------------------------------------
    def _on_mousewheel(self, event) -> str:
        self._hover_cancel()                 # a zoom gesture dismisses the tooltip
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
        self._hover_cancel()                 # a press / drag dismisses the tooltip
        self._drag_last = (event.x, event.y)
        self._press_xy = (event.x, event.y)   # click-vs-pan discrimination (Task 31)

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

    def _on_drag_end(self, event) -> None:
        press = self._press_xy
        self._drag_last = None
        self._press_xy = None
        # Click (not a pan): press and release within a few px -> let an active intel
        # pulse under the cursor consume it (focus the Intelligence tab). A real drag
        # moves farther and just pans, exactly as before -- integrating here (rather
        # than a naive tag binding) is what keeps the pulse click from fighting the
        # pan. Guarded so a hit-test/callback error never breaks button release.
        if press is not None:
            try:
                dx = event.x - press[0]
                dy = event.y - press[1]
                if dx * dx + dy * dy <= INTEL_CLICK_SLOP2:
                    self._intel_click_hit(event.x, event.y)
            except Exception:
                pass
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
        # Drive the delayed summary tooltip, REUSING the hover hit-test above (no
        # second nearest() call). The engine's own first guard makes this ~free
        # when no provider's layer is on (idle hover cost unchanged).
        self._hover_tooltip_motion(event, sid)

    # ---- reusable hover summary tooltip engine (owner feedback round B) ---------
    def register_hover_provider(self, layer_key: str, fn) -> None:
        """Register a hover-tooltip section provider. ``fn(system_id)`` returns a
        list of display lines (or None / [] for no contribution). The section is
        included only while ``_layer_on(layer_key)`` is true, so a provider costs
        nothing until its layer is enabled. Sections from all enabled providers
        are blank-line separated in one reused themed tooltip. Public so sibling
        layers (Task D's characters layer) plug in without touching the engine."""
        self._hover_providers.append((layer_key, fn))

    def _hover_any_layer_on(self) -> bool:
        """True iff at least one registered provider's layer is enabled. Plain
        loop (no generator / allocation) so the <Motion> idle guard stays cheap."""
        for key, _fn in self._hover_providers:
            if self._layer_on(key):
                return True
        return False

    def _hover_gesture_active(self) -> bool:
        """True while a pan / zoom / worker-gesture is in flight -- the tooltip
        must never schedule or show mid-gesture. Cheap attribute reads only."""
        return (self._drag_last is not None
                or self._gesture_inflight
                or self.state.zoom_anim.active)

    def _hover_tooltip_motion(self, event, sid) -> None:
        """<Motion> driver for the summary tooltip. FIRST guard: if no provider's
        layer is on, do nothing beyond dismissing any lingering tip -- near-zero
        idle cost, no allocation. Otherwise (re)arm a single delayed show for the
        hovered system, re-anchoring only when the cursor changes system or moves
        beyond a small radius (jitter within the radius does NOT restack after()s)."""
        if not self._hover_any_layer_on():
            if self._hover_after_id is not None or self._hover_tip is not None:
                self._hover_cancel()
            return
        # Never show mid pan/zoom, or when off a system.
        if sid is None or self._hover_gesture_active():
            self._hover_cancel()
            return
        x, y = event.x, event.y
        anc = self._hover_anchor
        if sid == self._hover_sid and anc is not None:
            dx = x - anc[0]
            dy = y - anc[1]
            if dx * dx + dy * dy <= HOVER_TOOLTIP_MOVE_R2:
                return                       # same target, within radius -> leave armed
        # New target (system changed or moved past the radius): reschedule ONCE.
        self._hover_cancel()
        self._hover_sid = sid
        self._hover_anchor = (x, y)
        self._hover_after_id = self.canvas.after(
            self._hover_delay_ms, lambda s=sid: self._hover_tooltip_show(s))

    def _hover_compose(self, sid) -> str | None:
        """Join the sections from every enabled provider for ``sid`` (blank-line
        separated), or None when nothing contributes. A provider raising is
        treated as no content -- a broken provider can never break hover."""
        sections: list[str] = []
        for key, fn in self._hover_providers:
            if not self._layer_on(key):
                continue
            try:
                lines = fn(sid)
            except Exception:
                lines = None
            if lines:
                sections.append("\n".join(str(ln) for ln in lines))
        if not sections:
            return None
        return "\n\n".join(sections)

    def _hover_tooltip_show(self, sid) -> None:
        """Delayed-show callback: re-validate (layer still on, same target, no
        gesture), compose the text, and place the reused themed Toplevel near the
        cursor. Never shows an empty tooltip."""
        self._hover_after_id = None
        if sid != self._hover_sid or not self._hover_any_layer_on():
            return
        if self._hover_gesture_active():
            return
        text = self._hover_compose(sid)
        if not text:
            return                           # no content -> no window
        t = self.theme
        try:
            tip = self._hover_tip
            if tip is None or not tip.winfo_exists():
                tip = self._hover_tip = tk.Toplevel(self.canvas)
                tip.wm_overrideredirect(True)
                self._hover_tip_label = tk.Label(
                    tip, justify="left", bg=t["panel"], fg=t["fg"],
                    relief="solid", borderwidth=1, padx=6, pady=4,
                    font=("Segoe UI", 9))
                self._hover_tip_label.pack()
            self._hover_tip_label.configure(text=text)
            anc = self._hover_anchor or (0, 0)
            x = self.canvas.winfo_rootx() + anc[0] + 14
            y = self.canvas.winfo_rooty() + anc[1] + 18
            tip.wm_geometry(f"+{x}+{y}")
            tip.deiconify()
            tip.lift()
        except Exception:
            # A torn-down canvas / display race must never crash a hover.
            self._hover_tip = None
            self._hover_tip_label = None

    def _hover_cancel(self) -> None:
        """Cancel any pending delayed show and withdraw the tip (reused, not
        destroyed -- same idiom as the sov legend / infra popover)."""
        if self._hover_after_id is not None:
            try:
                self.canvas.after_cancel(self._hover_after_id)
            except Exception:
                pass
            self._hover_after_id = None
        self._hover_sid = None
        self._hover_anchor = None
        tip = self._hover_tip
        if tip is not None:
            try:
                tip.withdraw()
            except Exception:
                pass

    def _on_canvas_leave(self, _event) -> None:
        """Cursor left the canvas -> dismiss the summary tooltip."""
        self._hover_cancel()

    def _infra_hover_lines(self, sid) -> list[str] | None:
        """Infra hover-tooltip provider: per-type structure counts for the hovered
        system, e.g. ["3× Fortizar", "1× Athanor"], sorted by count desc then
        name, with a dim "(stale)" line when the badge is stale-flagged. Reads the
        PRE-COMPUTED "type_counts" the host folded into the pushed badges (already
        filter-respecting), so this file keeps zero infra logic. None when the
        hovered system has no badge / no counts."""
        infra = self.state.infra
        if not infra:
            return None
        badge = infra.get(sid)
        if not badge:
            return None
        type_counts = badge.get("type_counts") or {}
        if not type_counts:
            return None
        items = sorted(type_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        lines = [f"{count}× {name}" for name, count in items]
        if badge.get("stale"):
            lines.append("(stale)")
        return lines

    def _on_right_click(self, event) -> None:
        self._hover_cancel()                 # a click dismisses the summary tooltip
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
        # Sov affiliation (Task 33): a disabled info row near the top when the layer
        # is on and this system is tinted. Alliance name from the resolved legend
        # map; raw-id fallback when names haven't resolved yet.
        if self._layer_on("sov"):
            aid = self.state.sov_map.get(sid)
            if aid is not None:
                alliance = self.state.sov_names.get(aid) or str(aid)
                menu.add_command(label=f"Sov: {alliance}", state="disabled")
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
        if self.state.route_dest is not None:      # Task 35: drop the route overlay
            menu.add_command(label="Clear route", command=self.clear_route)
        menu.add_separator()
        self._menu_add(menu, "Set destination", "set_destination", name)
        self._menu_add(menu, "Open in Dotlan", "open_dotlan", name)
        self._menu_add(menu, "Navigate WH route", "navigate_wh", name)
        self._menu_add(menu, "Titan bridge check", "titan_bridge", name)
        self._menu_add(menu, "Copy name", "copy", name)
        self._menu_add(menu, "Add to friendly staging", "add_friendly_staging", name)
        self._menu_add(menu, "Add to hostile staging", "add_hostile_staging", name)
        # Structures here… (Task 5): open the infra manager PRE-FILTERED to this
        # system. NOT routed through _menu_add -- that binds cb(name); the infra
        # manager wants the system ID, so bind a bespoke command passing sid.
        cb_infra = self.callbacks.get("open_infra_manager")
        if cb_infra is not None:
            menu.add_command(label="Structures here…",
                             command=lambda s=sid: cb_infra(s))
        # Organized structure list (owner feedback round B): when the infra layer
        # is on and the host supplies grouped structures for this system, add a
        # "Structures (N)" cascade grouping the FILTER-SURVIVING structures by
        # type. The host applies the SAME filters the chips use, so the menu shows
        # exactly what the overlay shows.
        if self._layer_on("infra") and cb_infra is not None:
            gss = self.callbacks.get("get_system_structures")
            groups = None
            if gss is not None:
                try:
                    groups = gss(sid)
                except Exception:
                    groups = None
            if groups:
                self._add_structures_submenu(menu, sid, groups, cb_infra)
        return menu

    def _add_structures_submenu(self, menu, sid, groups, cb_infra) -> None:
        """Attach a "Structures (N)" cascade grouping ``groups`` -- a list of
        (type_display_name, [structure_names]) already ordered by the host -- under
        disabled "— Type —" header rows. Each structure row and the trailing
        "Manage…" row open the infra manager at ``sid`` via ``cb_infra``. Rows are
        capped at _STRUCT_MENU_CAP with a trailing disabled "…and N more (Manage…)"
        row so a busy system stays navigable."""
        total = sum(len(names) for _t, names in groups)
        sub = self._make_menu(menu)
        shown = 0
        capped = False
        for type_name, names in groups:
            if not names:
                continue
            if shown >= _STRUCT_MENU_CAP:
                capped = True
                break
            sub.add_command(label=f"— {type_name} —", state="disabled")
            for nm in names:
                if shown >= _STRUCT_MENU_CAP:
                    capped = True
                    break
                sub.add_command(label=nm, command=lambda s=sid: cb_infra(s))
                shown += 1
        if capped:
            sub.add_command(label=f"…and {total - shown} more (Manage…)",
                            state="disabled")
        sub.add_separator()
        sub.add_command(label="Manage…", command=lambda s=sid: cb_infra(s))
        menu.add_cascade(label=f"Structures ({total})", menu=sub)

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
        # The old "Threat ship" cascade was the discoverability failure (owner
        # 2026-07-12): replace it with a single opener for the in-tab drawer,
        # which owns the ship-class picker AND per-staging selection. One source
        # of truth -- the drawer's master checkbutton shares _layer_vars["threat"]
        # with the "Threat projection" item above, and the ship radios drive the
        # same _threat_var/cfg["threat_ship"] the cascade did.
        menu.add_command(label="Threat settings…",
                         command=self._open_threat_drawer)
        menu.add_separator()
        menu.add_checkbutton(label="Fleet", variable=self._layer_vars["fleet"],
                             command=lambda: self._on_layer_toggle("fleet"))
        menu.add_checkbutton(label="Staging", variable=self._layer_vars["staging"],
                             command=lambda: self._on_layer_toggle("staging"))
        menu.add_checkbutton(label="Bridges", variable=self._layer_vars["bridges"],
                             command=lambda: self._on_layer_toggle("bridges"))
        menu.add_checkbutton(label="Route", variable=self._layer_vars["route"],
                             command=lambda: self._on_layer_toggle("route"))
        menu.add_checkbutton(label="Intel", variable=self._layer_vars["intel"],
                             command=lambda: self._on_layer_toggle("intel"))
        # Kill pings (Task 36): discrete zkill-alert radar bursts; ON by default.
        menu.add_checkbutton(label="Pings", variable=self._layer_vars["kill_pings"],
                             command=lambda: self._on_layer_toggle("kill_pings"))
        # Sov tint (Task 33): OFF by default; enabling it kicks the lazy ESI fetch.
        menu.add_checkbutton(label="Sov", variable=self._layer_vars["sov"],
                             command=lambda: self._on_layer_toggle("sov"))
        # Infra chips (Task 5): OFF by default; the toolbar "▾" hosts the filters.
        menu.add_checkbutton(label="Infra", variable=self._layer_vars["infra"],
                             command=lambda: self._on_layer_toggle("infra"))
        # Characters overlay (owner ask): ON by default; magenta markers + hover.
        menu.add_checkbutton(label="Chars", variable=self._layer_vars["chars"],
                             command=lambda: self._on_layer_toggle("chars"))
        if self.state.route_dest is not None:      # Task 35: drop the route overlay
            menu.add_command(label="Clear route", command=self.clear_route)
        # Manage infrastructure… (Task 5): open the manager for the whole DB.
        cb_infra = self.callbacks.get("open_infra_manager")
        if cb_infra is not None:
            menu.add_separator()
            menu.add_command(label="Manage infrastructure…",
                             command=lambda: cb_infra(None))
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
        elif key in ("bridges", "heat", "sov"):
            # Bridges, kill-heat AND sov live in the base bitmap, so a toggle needs
            # a settle re-render (the request carries the layer value, or None when
            # off). Their keys in _request_sig keep this from being duplicate-
            # suppressed. Heat also repaints overlays so the capital markers appear/
            # vanish with the layer.
            if key == "sov":
                # Turning sov ON kicks the lazy fetch (zero network until now) and
                # enables the legend microbutton; OFF disables the legend. The
                # freshness/in-flight guards inside _maybe_start_sov_fetch make the
                # OFF case (and a re-ON within the hour) a no-op.
                self._sync_sov_legend_btn()
                self._maybe_start_sov_fetch()
            self.state.force_dirty()
            self._request_crisp()
            self._redraw_overlays()
        elif key == "infra":
            # Infra badges are a base-bitmap layer (chips ride the label pass) fed
            # by PRE-COMPUTED data from the host. Toggling gates the request value
            # (like sov) for an immediate settle re-render AND asks the host to
            # (re)push or clear the overlay to match the new enabled flag; the two
            # crisp requests coalesce latest-wins, so it is one render.
            self._emit_infra_filters()
            self.state.force_dirty()
            self._request_crisp()
            self._redraw_overlays()
        elif key == "chars":
            # Characters overlay: match the 60 s poll to the new layer state. Start
            # only while the tab is shown (on_shown restarts it after a hidden
            # toggle-on); stop as soon as it is turned off. A pure Tk overlay -> just
            # repaint (no crisp) so the magenta markers appear now / clear at once.
            if self._layer_on("chars") and self._visible:
                self._start_chars_loop()
            else:
                self.shutdown_chars_loop()
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
        # Union only over the staging the drawer left INCLUDED (exclusions persist
        # by name in cfg["threat_staging_excluded"]; empty list -> all included).
        excluded = self.cfg.get("threat_staging_excluded", []) or []
        hostile = self.state.selected_hostile_staging(excluded)
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

    # ---- threat drawer (Task 34) ----------------------------------------------
    def _toggle_threat_drawer(self) -> None:
        """Threat ▾ microbutton handler: open the drawer if closed, else close it."""
        if self._threat_drawer_open:
            self._close_threat_drawer()
        else:
            self._open_threat_drawer()

    def _open_threat_drawer(self) -> None:
        """Build (fresh) and slide the drawer in on the right. Idempotent -- a
        second open is a no-op. Packing narrows the canvas, whose <Configure>
        updates the renderer viewport (no manual resize needed)."""
        if self._threat_drawer_open:
            return
        self._threat_drawer_open = True
        self._build_threat_drawer()
        self._threat_drawer.pack(side="right", fill="y")

    def _close_threat_drawer(self) -> None:
        """Slide the drawer out (pack_forget); the canvas reflows back to full
        width via its <Configure>. Children are kept until the next open, which
        rebuilds them fresh from current cfg/staging."""
        if not self._threat_drawer_open:
            return
        self._threat_drawer_open = False
        self._threat_drawer.pack_forget()

    def _build_threat_drawer(self) -> None:
        """(Re)build the drawer contents top-to-bottom: master overlay toggle,
        ship-class radio list, then the per-staging checkbox rows. Themed via the
        shared theme dict; the drawer bg is the panel colour, so select
        indicators use entry_bg for contrast against it."""
        t = self.theme
        d = self._threat_drawer
        for w in d.winfo_children():
            w.destroy()
        tk.Label(d, text="Threat overlay", bg=t["panel"], fg=t["accent"],
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=10,
                                                      pady=(10, 4))
        # 1) master toggle -- the SAME var the toolbar/menu use (one source of truth)
        tk.Checkbutton(d, text="Show threat overlay",
                       variable=self._layer_vars["threat"],
                       command=lambda: self._on_layer_toggle("threat"),
                       bg=t["panel"], fg=t["fg"], selectcolor=t["entry_bg"],
                       activebackground=t["panel"], activeforeground=t["accent"],
                       anchor="w").pack(fill="x", padx=8, pady=2)
        # 2) ship-class radios (Titan Bridge first); base name -> cfg["threat_ship"]
        tk.Label(d, text="Ship class", bg=t["panel"], fg=t["fg"]).pack(
            anchor="w", padx=10, pady=(8, 0))
        opts = mo.threat_options()
        current = self.cfg.get("threat_ship", "Titan Bridge")
        sel = next((l for l, _ in opts if self._strip_ly_suffix(l) == current),
                   opts[0][0] if opts else "")
        self._threat_var.set(sel)
        for label, _ly in opts:
            tk.Radiobutton(d, text=label, variable=self._threat_var, value=label,
                           command=lambda l=label: self._on_threat_ship(l),
                           bg=t["panel"], fg=t["fg"], selectcolor=t["entry_bg"],
                           activebackground=t["panel"],
                           activeforeground=t["accent"], anchor="w").pack(
                               fill="x", padx=8)
        # 3) per-staging selection: All/None links + one checkbox per staging
        head = tk.Frame(d, bg=t["panel"])
        head.pack(fill="x", padx=10, pady=(10, 0))
        tk.Label(head, text="Staging systems", bg=t["panel"], fg=t["fg"]).pack(
            side="left")
        tk.Button(head, text="None", command=self._threat_staging_none,
                  bg=t["panel"], fg=t["fg"], activebackground=t["entry_bg"],
                  activeforeground=t["accent"], relief="flat", borderwidth=0,
                  highlightthickness=0, cursor="hand2").pack(side="right")
        tk.Button(head, text="All", command=self._threat_staging_all,
                  bg=t["panel"], fg=t["fg"], activebackground=t["entry_bg"],
                  activeforeground=t["accent"], relief="flat", borderwidth=0,
                  highlightthickness=0, cursor="hand2").pack(side="right",
                                                             padx=(0, 4))
        self._threat_rows = tk.Frame(d, bg=t["panel"])
        self._threat_rows.pack(fill="x", padx=8, pady=(2, 8))
        self._rebuild_threat_staging_rows()

    def _rebuild_threat_staging_rows(self) -> None:
        """Repopulate the per-staging checkbox rows from the current hostile
        staging (id->name via the model). Checked = contributes to the threat
        union; unchecking persists a NAME exclusion. No-op before the drawer has
        ever been built (rows container None) so it is safe to call from
        set_staging at any time. Empty staging shows a dim hint instead of rows."""
        rows = self._threat_rows
        if rows is None:
            return
        for w in rows.winfo_children():
            w.destroy()
        self._threat_staging_vars = {}
        t = self.theme
        model = self.state.model
        systems = model.systems if model is not None else {}
        names = sorted({systems[sid].name for sid in self.state.hostile_staging
                        if sid in systems})
        if not names:
            tk.Label(rows,
                     text="No hostile staging configured — right-click a system "
                          "→ Add to hostile staging.",
                     bg=t["panel"], fg=t["fg"], justify="left", wraplength=205,
                     font=("Segoe UI", 8)).pack(anchor="w")
            return
        excluded = set(self.cfg.get("threat_staging_excluded", []) or [])
        for name in names:
            var = tk.BooleanVar(value=(name not in excluded))
            self._threat_staging_vars[name] = var
            tk.Checkbutton(rows, text=name, variable=var,
                           command=lambda n=name: self._on_threat_staging_toggle(n),
                           bg=t["panel"], fg=t["fg"], selectcolor=t["entry_bg"],
                           activebackground=t["panel"],
                           activeforeground=t["accent"], anchor="w").pack(fill="x")

    def _on_threat_staging_toggle(self, name: str) -> None:
        """A staging row toggled: add/drop the NAME from the persisted exclusion
        list (reassigned, never mutated in place -- the DEFAULT_CONFIG list may be
        shared) and recompute the halo over the new subset."""
        var = self._threat_staging_vars.get(name)
        if var is None:
            return
        excluded = set(self.cfg.get("threat_staging_excluded", []) or [])
        if var.get():
            excluded.discard(name)               # included
        else:
            excluded.add(name)                   # excluded
        self.cfg["threat_staging_excluded"] = sorted(excluded)
        self._recompute_threat()

    def _threat_staging_all(self) -> None:
        """Include every staging: clear the exclusion list, check all rows."""
        self.cfg["threat_staging_excluded"] = []
        for var in self._threat_staging_vars.values():
            var.set(True)
        self._recompute_threat()

    def _threat_staging_none(self) -> None:
        """Exclude every CURRENT staging: persist their names, uncheck all rows."""
        self.cfg["threat_staging_excluded"] = sorted(self._threat_staging_vars)
        for var in self._threat_staging_vars.values():
            var.set(False)
        self._recompute_threat()

    def _on_configure(self, event) -> None:
        self.state.resize(event.width, event.height)
        self._schedule_tick()

    def _wire_completions(self, model) -> None:
        """First-show only: push display-case system names + "Name (Region)"
        labels into the search widget's autocomplete dropdown. No-op for a plain
        tk.Entry (no update_completions). Names are the DISPLAY-case
        model.systems[sid].name (not the lowercase fly_to keys); the region name
        comes from model.region_anchors[region_id][0], omitted from the label
        when a region has no anchor."""
        uc = getattr(self.search_entry, "update_completions", None)
        if uc is None:
            return
        anchors = getattr(model, "region_anchors", {}) or {}
        names: list[str] = []
        labels: dict[str, str] = {}
        for s in model.systems.values():
            names.append(s.name)
            anc = anchors.get(s.region_id)
            region = anc[0] if anc else ""
            labels[s.name] = f"{s.name} ({region})" if region else s.name
        names.sort()
        try:
            uc(names, labels)
        except Exception:
            pass

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
