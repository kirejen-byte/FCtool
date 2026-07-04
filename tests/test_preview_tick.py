"""Tests for the native-preview controller tick (Task A8).

House pattern: bind the unbound FCToolGUI methods onto a bare SimpleNamespace
host (no real Tk / Win32 / DWM). Every boundary is a fake:
  - tracker: host._preview_find_clients returns scripted ClientWindow lists;
    eve_client_tracker.still_same_client is monkeypatched per-test.
  - tiles: host._preview_make_tile is a recording factory returning FakeTile.
  - activator: window_activator.activate is monkeypatched to record calls.
  - hotkeys: a fake service exposing .events (a queue) preloaded per-test.

NON-DISRUPTIVE: nothing here touches a real window, registers a hotkey, or
grabs focus — the activation path is exercised only through the fake.
"""
import inspect
import queue
import types
from types import SimpleNamespace

import pytest

import fc_gui
import eve_client_tracker
import window_activator
import preview_layout


CW = eve_client_tracker.ClientWindow


def _cw(hwnd, char_name, title=None, rect=(0, 0, 800, 600),
        is_iconic=False, pid=None):
    return CW(hwnd=hwnd, char_name=char_name,
              title=title if title is not None else (
                  "EVE" if not char_name else f"EVE - {char_name}"),
              rect=rect, is_iconic=is_iconic,
              pid=pid if pid is not None else hwnd)


class FakeTile:
    """Records every controller->tile call; no Tk."""

    def __init__(self, host, key):
        self._host = host
        self.key = key
        self.attached = None
        self.badges = []
        self.captions = []
        self.retops = 0
        self.destroyed = False
        self.refreshes = 0
        self.borders = []
        self.hidden = False
        self.hide_calls = 0
        self.show_calls = 0
        self.excluded = False
        self.video_labels = []          # caption-onvideo: set_video_label calls
        self.label_styles = []          # caption-onvideo: set_label_style calls

    def hide(self):
        self.hidden = True
        self.hide_calls += 1

    def show(self):
        self.hidden = False
        self.show_calls += 1

    def set_key(self, char_key):
        self.key = char_key

    def place(self, x, y, w, body_h):
        self.placed = (x, y, w, body_h)

    def attach_source(self, src_hwnd):
        self.attached = src_hwnd

    def set_badge(self, text):
        self.badges.append(text)

    def set_caption(self, *parts):
        self.captions.append(parts)

    def set_video_label(self, text):
        self.video_labels.append(text)

    def video_label_text(self):
        return self.video_labels[-1] if self.video_labels else ""

    def set_label_style(self, color=None, size=None, anchor=None):
        self.label_styles.append((color, size, anchor))

    def refresh_source_size(self):
        self.refreshes += 1

    def set_border(self, color):
        self.borders.append(color)

    def set_excluded(self, flag):
        self.excluded = bool(flag)

    def retop(self):
        self.retops += 1
        self._host.retop_order.append(("tile", self.key))

    def detach(self):
        pass

    def destroy(self):
        self.destroyed = True
        self._host.tiles_destroyed.append(self.key)


class FakeOverlay:
    def __init__(self, host):
        self._host = host
        self.retops = 0
        self.label_pushes = []

    def retop(self):
        self.retops += 1
        self._host.retop_order.append(("overlay",))

    def set_labels(self, items):
        self.label_pushes.append(list(items))


class FakeHotkeys:
    def __init__(self):
        self.events = queue.Queue()


def make_host(layouts=None, mode="native", disabled_chars=None,
              hotkeys=None, overlay=None):
    preview = {"mode": mode}
    if layouts is not None:
        preview["layouts"] = dict(layouts)
    if disabled_chars is not None:
        preview["disabled_chars"] = list(disabled_chars)
    if hotkeys is not None:
        preview["hotkeys"] = hotkeys
    host = SimpleNamespace(
        config={"preview": preview},
        _save_config=lambda: None,
        root=SimpleNamespace(after=lambda *a, **k: "after-id",
                             after_cancel=lambda *a, **k: None),
        _overlay=overlay,
        _overlay_states={},
        _overlay_state_ts={},
    )
    # controller state (mirror __init__ block)
    host._preview_tiles = {}
    host._preview_tile_rects = {}
    host._preview_clients = {}
    host._preview_hotkeys = None
    host._preview_hotkey_map = {}
    host._preview_after_id = None
    host._preview_intel = {}
    host._preview_disabled_session = False
    host._preview_tick_count = 0
    host._preview_tick_fails = 0
    host._preview_last_key = ""
    host._preview_find_clients = lambda: []
    host._preview_status = ""
    # B6 damage-flash state (mirror __init__ block)
    import damage_flash as _df
    host._preview_damage = _df.DamageFlashTracker()
    host._preview_gamelog = None
    host._preview_layer_hp = {}
    host._preview_damage_until = {}
    host._preview_damage_since = {}
    # C2 hide-rules / shown-chars state (mirror __init__ block)
    host._preview_lost_focus_since = None
    host._preview_win32 = None            # foreground backend; None → treat as focused
    # C4 active-highlight / cycle-exclusion / switch-external state
    host._preview_excluded = set()        # session-only cycle-exclusion keys
    host._preview_last_external_hwnd = None

    # Caption-composition boundaries (the tick body now calls the real
    # _preview_compose_captions — Task B1). Stub the ESI/doctrine/rules edges so
    # the default tick exercises compose without error; the B1 compose tests
    # override these with richer fakes.
    host.fittings = None
    host._overlay_rules = lambda: []
    host._overlay_cfg = lambda: host.config.setdefault(
        "overlay", {"rules": [], "overrides": {}})
    host._active_doctrine_obj = lambda: None
    host._preview_state_for = lambda key: None
    host._preview_role_chip = lambda client: ""

    # recording buffers used by test assertions
    host.tiles_created = []
    host.tiles_destroyed = []
    host.retop_order = []
    host.disabled_session_called = 0

    # bind the real methods under test
    for name in ("_preview_cfg", "_preview_native_tick_body",
                 "_preview_spawn_tile", "_preview_rekey_tile",
                 "_preview_retire_tile", "_preview_retire_all_tiles",
                 "_preview_style_tile", "_preview_switch_to",
                 "_preview_drain_hotkeys", "_preview_compose_captions",
                 "_preview_compose_video_labels",
                 "_preview_caption_parts", "_preview_should_flash",
                 "_preview_on_damage",
                 "_preview_visibility", "_preview_shown_chars",
                 "_preview_all_known_chars", "_preview_foreground_info",
                 "_preview_sync_gamelog_scope",
                 "_preview_resolve_size", "_preview_apply_tile_size",
                 "_preview_tile_rect", "_preview_tracked_names"):
        fn = getattr(fc_gui.FCToolGUI, name, None)
        if fn is None:
            continue
        # Staticmethods must NOT receive `host` as an implicit first arg.
        raw = inspect.getattr_static(fc_gui.FCToolGUI, name, None)
        if isinstance(raw, staticmethod):
            setattr(host, name, fn)
        else:
            setattr(host, name, types.MethodType(fn, host))

    # recording tile factory (replaces real TileWindow construction)
    def _make_tile(key, x, y, w, body_h):
        host.tiles_created.append((key, x, y, w, body_h))
        return FakeTile(host, key)
    host._preview_make_tile = _make_tile

    # recording disable-for-session
    def _disable():
        host.disabled_session_called += 1
        host._preview_disabled_session = True
    host._preview_disable_session = _disable

    return host


def tick(host):
    return host._preview_native_tick_body()


@pytest.fixture(autouse=True)
def _no_eveo(monkeypatch):
    # Default: EVE-O not running, HWNDs stay valid. Individual tests override.
    monkeypatch.setattr(fc_gui, "preview_running", lambda: False)
    monkeypatch.setattr(eve_client_tracker, "still_same_client",
                        lambda c, **k: True)


# ── (a) new client → tile created at saved layout + source attached ──────────
def test_tick_creates_tile_for_new_client_and_preserves_layout_on_close():
    host = make_host(layouts={"kirejen": [10, 20, 384, 216]})
    client = _cw(1, "Kirejen", rect=(0, 0, 800, 600))
    host._preview_find_clients = lambda: [client]
    tick(host)
    assert host.tiles_created == [("kirejen", 10, 20, 384, 216)]
    tile = host._preview_tiles[1]
    assert tile.attached == 1                      # attach_source(hwnd)
    # close it
    host._preview_find_clients = lambda: []
    tick(host)
    assert host.tiles_destroyed == ["kirejen"]
    assert tile.destroyed
    # layout entry PRESERVED (the EVE-O foot-gun we are fixing)
    assert host.config["preview"]["layouts"]["kirejen"] == [10, 20, 384, 216]


def test_new_login_client_uses_login_stack_when_no_saved_layout():
    host = make_host(layouts={})
    login = _cw(2, "", title="EVE")
    host._preview_find_clients = lambda: [login]
    tick(host)
    # login screen with no saved layout → login-stack position (index 0 → base)
    base = tuple(fc_gui.FCToolGUI._PREVIEW_DEFAULTS["login_position"])
    key, x, y, w, h = host.tiles_created[0]
    assert (x, y) == preview_layout.login_stack_pos(0, base)
    assert w == 384 and h == 216


# ── uniform_size: a global cfg size change re-places every live tile ─────────
def test_uniform_size_change_replaces_all_tiles_at_new_global_size():
    host = make_host(layouts={"kirejen": [10, 20, 384, 216]})
    host.config["preview"]["uniform_size"] = True
    client = _cw(1, "Kirejen", rect=(0, 0, 800, 600))
    host._preview_find_clients = lambda: [client]
    tick(host)                                      # spawn at 384x216
    tile = host._preview_tiles[1]
    # user bumped the global size (e.g. via the tile-w spin or another tile's resize)
    host.config["preview"]["tile_w"] = 500
    host.config["preview"]["tile_body_h"] = 300
    tick(host)                                      # tick must re-place at new size
    assert tile.placed == (10, 20, 500, 300)        # x,y kept; w/body_h updated


def test_uniform_false_applies_per_char_size_override_on_tick():
    host = make_host(layouts={"kirejen": [10, 20, 384, 216]})
    host.config["preview"]["uniform_size"] = False
    host.config["preview"]["sizes"] = {"kirejen": [260, 150]}
    client = _cw(1, "Kirejen", rect=(0, 0, 800, 600))
    host._preview_find_clients = lambda: [client]
    tick(host)
    tile = host._preview_tiles[1]
    # per-char override wins over the (default) global size
    assert tile.placed == (10, 20, 260, 150)


# ── (b) retitle login→char → re-key + move to char layout ────────────────────
def test_retitle_rekeys_tile_and_moves_to_char_layout():
    host = make_host(layouts={"kirejen": [30, 40, 384, 216]})
    login = _cw(5, "", title="EVE")
    host._preview_find_clients = lambda: [login]
    tick(host)
    created_tile = host._preview_tiles[5]
    # now the same hwnd retitles to a character
    named = _cw(5, "Kirejen")
    host._preview_find_clients = lambda: [named]
    tick(host)
    # same hwnd, tile re-keyed (not destroyed+recreated)
    assert host._preview_tiles[5] is created_tile
    assert created_tile.key == "kirejen"
    # caption/badge updated to the char (badge cleared: not login, not iconic)
    assert None in created_tile.badges


# ── (c) removed client → detach+destroy, layout entry preserved ──────────────
def test_removed_client_destroys_tile_keeps_layout():
    host = make_host(layouts={"alt": [1, 2, 384, 216]})
    c = _cw(7, "Alt")
    host._preview_find_clients = lambda: [c]
    tick(host)
    host._preview_find_clients = lambda: []
    tick(host)
    assert host._preview_tiles == {}
    assert host.config["preview"]["layouts"]["alt"] == [1, 2, 384, 216]


# ── (d) iconic client → set_badge("MINIMIZED") ───────────────────────────────
def test_minimized_client_gets_badge():
    host = make_host(layouts={"kirejen": [0, 0, 384, 216]})
    c = _cw(1, "Kirejen", is_iconic=True)
    host._preview_find_clients = lambda: [c]
    tick(host)
    tile = host._preview_tiles[1]
    assert tile.badges[-1] == "MINIMIZED"


def test_login_client_gets_login_badge():
    host = make_host()
    c = _cw(1, "", title="EVE")
    host._preview_find_clients = lambda: [c]
    tick(host)
    tile = host._preview_tiles[1]
    assert tile.badges[-1] == "login screen"


# ── (e) tiles retopped, then overlay retopped (order) ────────────────────────
def test_retop_order_tiles_then_overlay():
    overlay = None
    host = make_host(layouts={"kirejen": [0, 0, 384, 216]})
    host._overlay = FakeOverlay(host)
    c = _cw(1, "Kirejen")
    host._preview_find_clients = lambda: [c]
    tick(host)
    # last two retop events: the tile, then the overlay
    assert host.retop_order[-1] == ("overlay",)
    assert host.retop_order[-2] == ("tile", "kirejen")


# ── (f) hotkey queue drained → activate once per event ───────────────────────
def test_hotkey_focus_events_activate_mapped_client(monkeypatch):
    activated = []
    monkeypatch.setattr(window_activator, "activate",
                        lambda hwnd, **k: activated.append(hwnd) or True)
    host = make_host(layouts={"kirejen": [0, 0, 384, 216]})
    svc = FakeHotkeys()
    svc.events.put(101)
    host._preview_hotkeys = svc
    host._preview_hotkey_map = {101: ("focus", "kirejen")}
    c = _cw(1, "Kirejen")
    host._preview_find_clients = lambda: [c]
    tick(host)
    assert activated == [1]      # mapped key → live client hwnd, activated once


def test_hotkey_cycle_activates_next_live_client(monkeypatch):
    activated = []
    monkeypatch.setattr(window_activator, "activate",
                        lambda hwnd, **k: activated.append(hwnd) or True)
    host = make_host(layouts={})
    svc = FakeHotkeys()
    svc.events.put(200)
    host._preview_hotkeys = svc
    host._preview_hotkey_map = {200: ("cycle", 0, +1)}
    host._preview_last_key = "a"
    ca = _cw(1, "A")
    cc = _cw(3, "C")
    host._preview_find_clients = lambda: [ca, cc]
    tick(host)
    # cycle from "a" +1 over live {a, c} → "c" → hwnd 3
    assert activated == [3]


# ── (g) tracker exception → skip tick, disable only after >=5 in a row ────────
def test_tracker_exception_skips_tick_without_crashing_or_disabling():
    host = make_host()

    def boom():
        raise RuntimeError("enum blew up")
    host._preview_find_clients = boom
    # a single failed tick must NOT disable the session (log-and-continue)
    host._preview_native_tick_body()
    assert host.disabled_session_called == 0
    assert host._preview_tick_fails == 1


def test_five_consecutive_failed_ticks_disable_session():
    host = make_host()

    def boom():
        raise RuntimeError("enum blew up")
    host._preview_find_clients = boom
    for _ in range(4):
        host._preview_native_tick_body()
    assert host.disabled_session_called == 0        # 4 in a row: still alive
    assert host._preview_tick_fails == 4
    host._preview_native_tick_body()                # 5th consecutive failure
    assert host.disabled_session_called == 1
    assert host._preview_disabled_session is True


def test_four_failures_then_success_does_not_disable():
    host = make_host(layouts={"kirejen": [0, 0, 384, 216]})

    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] <= 4:
            raise RuntimeError("enum blew up")
        return [_cw(1, "Kirejen")]
    host._preview_find_clients = flaky
    for _ in range(4):
        host._preview_native_tick_body()
    assert host._preview_tick_fails == 4
    host._preview_native_tick_body()                # 5th tick succeeds
    assert host.disabled_session_called == 0
    assert host._preview_tick_fails == 0            # counter reset on success
    # a subsequent failure starts the count fresh (not immediately fatal)
    host._preview_find_clients = flaky              # calls["n"] now >4 → returns client
    # force a failure again to confirm the counter restarts from 0
    def boom():
        raise RuntimeError("boom")
    host._preview_find_clients = boom
    host._preview_native_tick_body()
    assert host._preview_tick_fails == 1
    assert host.disabled_session_called == 0


# ── (h) EVE-O running in native mode → tiles NOT started, status mentions EVE-O
def test_eveo_running_refuses_and_retires_tiles(monkeypatch):
    monkeypatch.setattr(fc_gui, "preview_running", lambda: True)
    host = make_host(layouts={"kirejen": [0, 0, 384, 216]})
    # pre-existing tile from a prior tick
    pre = FakeTile(host, "kirejen")
    host._preview_tiles[1] = pre
    c = _cw(1, "Kirejen")
    host._preview_find_clients = lambda: [c]
    status = tick(host)
    assert host.tiles_created == []            # nothing new spawned
    assert pre.destroyed                       # existing tiles retired
    assert host._preview_tiles == {}
    assert "EVE-O" in status


# ── disabled_chars are excluded from the client set ──────────────────────────
def test_disabled_char_gets_no_tile():
    host = make_host(layouts={}, disabled_chars=["kirejen"])
    c = _cw(1, "Kirejen")
    host._preview_find_clients = lambda: [c]
    tick(host)
    assert host.tiles_created == []
    assert host._preview_tiles == {}


# ── HWND-reuse guard: still_same_client False → retire ───────────────────────
def test_hwnd_reuse_guard_retires_stale_tile(monkeypatch):
    host = make_host(layouts={"kirejen": [0, 0, 384, 216]})
    c = _cw(1, "Kirejen")
    host._preview_find_clients = lambda: [c]
    tick(host)
    assert 1 in host._preview_tiles
    # next tick: the hwnd was reused by a non-EVE window
    monkeypatch.setattr(eve_client_tracker, "still_same_client",
                        lambda cl, **k: False)
    tick(host)
    assert host._preview_tiles == {}


# ── per-tile OSError → retire THIS tile only, session stays up ───────────────
def test_per_tile_oserror_retires_only_that_tile(monkeypatch):
    host = make_host(layouts={"kirejen": [0, 0, 384, 216]})
    c = _cw(1, "Kirejen")
    host._preview_find_clients = lambda: [c]
    tick(host)
    tile = host._preview_tiles[1]

    def boom(*a, **k):
        raise OSError("dwm gone")
    tile.set_badge = boom
    # must not disable the session; just retire the tile
    tick(host)
    assert host.disabled_session_called == 0
    assert host._preview_tiles == {}


# ── per-tile NON-OSError → retire ONLY that tile, session + loop survive ──────
def test_per_tile_arbitrary_exception_retires_only_that_tile_session_survives(
        monkeypatch):
    # Two live clients; one tile's per-tile work throws a plain Exception. Only
    # the offending tile is retired; the other tile survives and the loop stays
    # scheduled (session NOT disabled). This is the core BUG-A regression guard.
    host = make_host(layouts={"a": [0, 0, 384, 216], "b": [1, 1, 384, 216]})
    ca, cb = _cw(1, "A"), _cw(2, "B")
    host._preview_find_clients = lambda: [ca, cb]
    tick(host)
    assert set(host._preview_tiles) == {1, 2}
    bad = host._preview_tiles[1]
    good = host._preview_tiles[2]

    def boom(*a, **k):
        raise RuntimeError("per-tile blew up after foreground change")
    bad.set_badge = boom
    status = tick(host)
    # session survived: not disabled, counter reset (a successful tick overall)
    assert host.disabled_session_called == 0
    assert host._preview_disabled_session is False
    assert host._preview_tick_fails == 0
    # ONLY the offending tile retired; the other still populated
    assert 1 not in host._preview_tiles
    assert host._preview_tiles.get(2) is good
    # the tick returned a normal status (loop still schedules)
    assert isinstance(status, str) and status.startswith("●")


# ── (BUG A part 2) activation immediately re-asserts tile+overlay z-order ─────
def test_switch_to_retops_tiles_and_overlay_immediately_after_activate(monkeypatch):
    order = []
    monkeypatch.setattr(window_activator, "activate",
                        lambda hwnd, **k: order.append(("activate", hwnd)) or True)
    host = make_host(mode="native", layouts={"a": [0, 0, 384, 216],
                                             "b": [1, 1, 384, 216]})
    for name in ("_preview_switch_to",):
        setattr(host, name, types.MethodType(getattr(fc_gui.FCToolGUI, name), host))
    ca, cb = _cw(1, "A"), _cw(2, "B")
    host._preview_clients = {1: ca, 2: cb}
    ta = FakeTile(host, "a")
    tb = FakeTile(host, "b")
    host._preview_tiles = {1: ta, 2: tb}
    host._overlay = FakeOverlay(host)
    # patch tile.retop to also record ordering relative to activate
    orig_ta_retop, orig_tb_retop = ta.retop, tb.retop
    ta.retop = lambda: (order.append(("tile", "a")), orig_ta_retop())
    tb.retop = lambda: (order.append(("tile", "b")), orig_tb_retop())
    host._overlay.retop = lambda: order.append(("overlay",))

    host._preview_switch_to(cb)                          # activate B

    # activate happened, then tiles retopped, then overlay retopped
    assert order[0] == ("activate", 2)
    assert ("tile", "a") in order and ("tile", "b") in order
    assert order[-1] == ("overlay",)
    # retops happen AFTER activate (immediate z-order re-assert)
    assert order.index(("activate", 2)) < order.index(("tile", "a"))
    assert order.index(("activate", 2)) < order.index(("overlay",))


def test_switch_to_retop_survives_no_overlay(monkeypatch):
    # Overlay absent (None) → activation still retops tiles, no crash.
    monkeypatch.setattr(window_activator, "activate", lambda hwnd, **k: True)
    host = make_host(mode="native")
    setattr(host, "_preview_switch_to",
            types.MethodType(fc_gui.FCToolGUI._preview_switch_to, host))
    cb = _cw(2, "B")
    host._preview_clients = {2: cb}
    tb = FakeTile(host, "b")
    host._preview_tiles = {2: tb}
    host._overlay = None
    host._preview_switch_to(cb)
    assert tb.retops == 1                                # tile retopped once


# ── (A10) hotkey service lifecycle wiring ────────────────────────────────────
class FakeService:
    """Records start/restart/stop + bindings; exposes .events + .failures."""

    def __init__(self):
        self.events = queue.Queue()
        self.failures = {}
        self.started_with = []
        self.stopped = 0

    def start(self, bindings):
        self.started_with.append(dict(bindings))

    def restart(self, bindings):
        self.started_with.append(dict(bindings))

    def stop(self):
        self.stopped += 1


def _wire_hotkey_methods(host):
    import inspect
    for name in ("_preview_restart_hotkeys", "_preview_hotkey_bindings"):
        fn = getattr(fc_gui.FCToolGUI, name, None)
        if fn is None:
            continue
        raw = inspect.getattr_static(fc_gui.FCToolGUI, name, None)
        if isinstance(raw, staticmethod):
            setattr(host, name, fn)          # static: no self
        else:
            setattr(host, name, types.MethodType(fn, host))


def test_restart_hotkeys_lazily_creates_service_and_starts_with_bindings():
    host = make_host(
        hotkeys={"focus": {"kirejen": "F13"},
                 "groups": [{"next": ["F14"], "prev": [], "order": []}],
                 "minimize_all": []})
    _wire_hotkey_methods(host)
    created = []

    def factory():
        svc = FakeService()
        created.append(svc)
        return svc
    host._preview_hotkey_factory = factory
    host._preview_clients = {1: _cw(1, "Kirejen")}

    host._preview_restart_hotkeys()

    assert len(created) == 1                      # lazy: created once
    svc = created[0]
    assert host._preview_hotkeys is svc
    assert len(svc.started_with) == 1
    bindings = svc.started_with[0]
    assert len(bindings) == 2                     # F13 focus + F14 next
    # action map rebuilt in lockstep with the bindings
    assert set(host._preview_hotkey_map) == set(bindings)
    assert ("focus", "kirejen") in set(host._preview_hotkey_map.values())
    assert ("cycle", 0, +1) in set(host._preview_hotkey_map.values())


def test_restart_hotkeys_reuses_existing_service():
    host = make_host(
        hotkeys={"focus": {}, "groups": [{"next": ["F14"], "prev": [], "order": []}],
                 "minimize_all": []})
    _wire_hotkey_methods(host)
    created = []
    host._preview_hotkey_factory = lambda: created.append(FakeService()) or created[-1]
    host._preview_restart_hotkeys()
    first = host._preview_hotkeys
    host._preview_restart_hotkeys()
    assert host._preview_hotkeys is first         # not re-created
    assert len(created) == 1
    assert len(first.started_with) == 2           # restarted with fresh bindings


# ── (B1) captions: tracked-names provider, caption parts precedence, compose ──
import overlay_rules
import fleet_composer


def _bind_caption_methods(host):
    for name in ("_preview_caption_parts", "_preview_tracked_names",
                 "_preview_state_for", "_active_doctrine_obj",
                 "_preview_compose_captions"):
        fn = getattr(fc_gui.FCToolGUI, name, None)
        if fn is not None:
            setattr(host, name, types.MethodType(fn, host))


CS = overlay_rules.CharState


# --- (a) provider: native mode names come from _preview_clients, feeding the
#     SAME _overlay_states dict the poller writes (single-writer invariant) ---
def test_tracked_names_native_mode_from_preview_clients():
    host = make_host(mode="native")
    _bind_caption_methods(host)
    host._preview_clients = {1: _cw(1, "Kirejen"), 2: _cw(2, ""),  # login excluded
                             3: _cw(3, "Alt Two")}
    names = host._preview_tracked_names()
    assert set(names) == {"kirejen", "alt two"}    # lowercased, login dropped


def test_tracked_names_eveo_mode_from_thumbs():
    host = make_host(mode="eveo_labels")
    _bind_caption_methods(host)
    host._overlay_thumbs_fn = lambda: [
        SimpleNamespace(char_name="Bob"), SimpleNamespace(char_name="Carol")]
    names = host._preview_tracked_names()
    assert set(names) == {"bob", "carol"}


# --- (b) _preview_caption_parts precedence: manual > rule > doctrine tag > "" ---
def _caption_host():
    host = make_host(mode="native")
    _bind_caption_methods(host)
    return host


def test_caption_login_client_has_no_dot():
    host = _caption_host()
    login = _cw(2, "", title="EVE")
    name, dot, chip, tag = host._preview_caption_parts(
        login, None, [], {}, "", {}, True)
    assert name == "login screen"
    assert dot is None
    assert tag == ""


def test_caption_dot_color_reflects_online_state():
    host = _caption_host()
    c = _cw(1, "Kirejen")
    online = CS(character_id=1, name="Kirejen", online=True)
    offline = CS(character_id=1, name="Kirejen", online=False)
    unknown = CS(character_id=1, name="Kirejen", online=None)
    assert host._preview_caption_parts(c, online, [], {}, "", {}, True)[1] == fc_gui.FG_GREEN
    assert host._preview_caption_parts(c, offline, [], {}, "", {}, True)[1] == fc_gui.FG_RED
    assert host._preview_caption_parts(c, unknown, [], {}, "", {}, True)[1] == fc_gui.FG_DIM
    # no state at all (stale/dropped) → dim grey
    assert host._preview_caption_parts(c, None, [], {}, "", {}, True)[1] == fc_gui.FG_DIM


def test_caption_docked_appends_anchor_glyph():
    host = _caption_host()
    c = _cw(1, "Kirejen")
    docked = CS(character_id=1, name="Kirejen", online=True, docked=True)
    name, dot, chip, tag = host._preview_caption_parts(
        c, docked, [], {}, "", {}, True)
    assert name.startswith("Kirejen")
    assert "⚓" in name


def test_caption_role_chip_passed_through():
    host = _caption_host()
    c = _cw(1, "Kirejen")
    st = CS(character_id=1, name="Kirejen", online=True)
    parts = host._preview_caption_parts(c, st, [], {}, "FC", {}, True)
    assert parts[2] == "FC"


def test_caption_manual_override_beats_rule_and_doctrine_tag():
    host = _caption_host()
    c = _cw(1, "Kirejen")
    st = CS(character_id=1, name="Kirejen", online=True, ship_type_id=999,
            ship_group="Force Recon Ship")
    rules = [overlay_rules.OverlayRule("ship_group", "Force Recon Ship", "Cyno")]
    overrides = {"kirejen": "MyTag"}
    tag_index = {999: {"Logi", "Anchor"}}
    _, _, _, tag = host._preview_caption_parts(
        c, st, rules, overrides, "", tag_index, True)
    assert tag == "MyTag"


def test_caption_rule_label_beats_doctrine_tag():
    host = _caption_host()
    c = _cw(1, "Kirejen")
    st = CS(character_id=1, name="Kirejen", online=True, ship_type_id=999,
            ship_group="Force Recon Ship")
    rules = [overlay_rules.OverlayRule("ship_group", "Force Recon Ship", "Cyno")]
    tag_index = {999: {"Logi", "Anchor"}}
    _, _, _, tag = host._preview_caption_parts(
        c, st, rules, {}, "", tag_index, True)
    assert tag == "Cyno"


def test_caption_doctrine_tag_shows_when_no_override_no_rule():
    host = _caption_host()
    c = _cw(1, "Kirejen")
    st = CS(character_id=1, name="Kirejen", online=True, ship_type_id=999)
    tag_index = {999: {"Logi", "Anchor"}}
    _, _, _, tag = host._preview_caption_parts(
        c, st, [], {}, "", tag_index, True)
    assert tag == "Anchor"          # sorted(...)[0], deterministic multi-tag pick


def test_caption_doctrine_tag_suppressed_when_toggle_off():
    host = _caption_host()
    c = _cw(1, "Kirejen")
    st = CS(character_id=1, name="Kirejen", online=True, ship_type_id=999)
    tag_index = {999: {"Anchor"}}
    _, _, _, tag = host._preview_caption_parts(
        c, st, [], {}, "", tag_index, False)      # doctrine_tag_captions=False
    assert tag == ""


def test_caption_doctrine_tag_inert_without_hull_or_index():
    host = _caption_host()
    c = _cw(1, "Kirejen")
    # unknown ship_type_id
    st = CS(character_id=1, name="Kirejen", online=True, ship_type_id=None)
    assert host._preview_caption_parts(c, st, [], {}, "", {999: {"X"}}, True)[3] == ""
    # empty tag index (no doctrine active — build_tag_index returns {})
    st2 = CS(character_id=1, name="Kirejen", online=True, ship_type_id=999)
    assert host._preview_caption_parts(c, st2, [], {}, "", {}, True)[3] == ""


def test_caption_empty_override_hides_tag():
    host = _caption_host()
    c = _cw(1, "Kirejen")
    st = CS(character_id=1, name="Kirejen", online=True, ship_type_id=999)
    tag_index = {999: {"Anchor"}}
    # empty-string override means "hide", beating the doctrine tag
    _, _, _, tag = host._preview_caption_parts(
        c, st, [], {"kirejen": ""}, "", tag_index, True)
    assert tag == ""


# --- (c) _preview_compose_captions integration: builds one tag index, looks up
#     staleness-checked state, calls tile.set_caption(*parts) for each tile ---
def _compose_host(**kw):
    host = make_host(mode="native", **kw)
    _bind_caption_methods(host)
    # doctrine/fittings/rules/overrides needed by compose
    host.fittings = None
    host.config["preview"]["captions"] = True
    host.config["preview"]["show_role_chip"] = False
    host.config["preview"]["doctrine_tag_captions"] = True
    # _overlay_cfg / _overlay_rules read class-level _OVERLAY_DEFAULTS which a
    # bare SimpleNamespace host lacks; stub them at the boundary (empty rules +
    # overrides is the "no rule / no manual tag" caption case these tests want).
    host.config.setdefault("overlay", {"rules": [], "overrides": {}})
    host._overlay_cfg = lambda: host.config["overlay"]
    host._overlay_rules = lambda: []
    for name in ("_preview_state_for", "_preview_role_chip"):
        fn = getattr(fc_gui.FCToolGUI, name, None)
        if fn is not None:
            setattr(host, name, types.MethodType(fn, host))
    return host


def test_compose_captions_sets_caption_on_each_live_tile(monkeypatch):
    host = _compose_host()
    # no doctrine → tag_index {}
    monkeypatch.setattr(fc_gui.FCToolGUI, "_active_doctrine_obj",
                        lambda self: None, raising=False)
    c = _cw(1, "Kirejen")
    tile = FakeTile(host, "kirejen")
    host._preview_tiles = {1: tile}
    host._overlay_states = {"kirejen": CS(character_id=1, name="Kirejen", online=True)}
    host._overlay_state_ts = {"kirejen": 10 ** 12}   # fresh (far-future ts vs monotonic)
    import time as _t
    monkeypatch.setattr(_t, "monotonic", lambda: 0.0)
    host._preview_compose_captions({1: c})
    assert tile.captions, "set_caption should have been called"
    name, dot, chip, tag = tile.captions[-1]
    assert name == "Kirejen"
    assert dot == fc_gui.FG_GREEN


def test_compose_captions_uses_doctrine_tag_when_active(monkeypatch):
    host = _compose_host()
    doctrine = object()
    monkeypatch.setattr(fc_gui.FCToolGUI, "_active_doctrine_obj",
                        lambda self: doctrine, raising=False)
    monkeypatch.setattr(fleet_composer, "build_tag_index",
                        lambda d, f: {999: {"Anchor", "Logi"}})
    c = _cw(1, "Kirejen")
    tile = FakeTile(host, "kirejen")
    host._preview_tiles = {1: tile}
    host._overlay_states = {"kirejen": CS(character_id=1, name="Kirejen",
                                          online=True, ship_type_id=999)}
    host._overlay_state_ts = {"kirejen": 10 ** 12}
    import time as _t
    monkeypatch.setattr(_t, "monotonic", lambda: 0.0)
    host._preview_compose_captions({1: c})
    name, dot, chip, tag = tile.captions[-1]
    assert tag == "Anchor"          # deterministic first tag


def test_compose_captions_disabled_when_captions_off(monkeypatch):
    host = _compose_host()
    host.config["preview"]["captions"] = False
    monkeypatch.setattr(fc_gui.FCToolGUI, "_active_doctrine_obj",
                        lambda self: None, raising=False)
    c = _cw(1, "Kirejen")
    tile = FakeTile(host, "kirejen")
    host._preview_tiles = {1: tile}
    host._preview_compose_captions({1: c})
    assert tile.captions == []      # captions off → no set_caption calls


# ── (caption-onvideo) on-video activity label drawn DIRECTLY on the tile ──────
from preview_tile import STRIP_H


def test_compose_pushes_onvideo_label_label_and_ship(monkeypatch):
    # labels_on_video on → the tile gets '<label> - <ShipType>' on its body.
    host = _compose_host()
    host.config["preview"]["labels_on_video"] = True
    monkeypatch.setattr(fc_gui.FCToolGUI, "_active_doctrine_obj",
                        lambda self: None, raising=False)
    host._overlay_rules = lambda: [
        overlay_rules.OverlayRule("ship_group", "Force Recon Ship", "Cyno")]
    st = CS(character_id=1, name="Kirejen", online=True,
            ship_group="Force Recon Ship", ship_type_name="Onyx")
    host._preview_state_for = lambda key: st if key == "kirejen" else None
    tile = FakeTile(host, "kirejen")
    host._preview_tiles = {1: tile}
    host._preview_compose_captions({1: _cw(1, "Kirejen")})
    assert tile.video_labels[-1] == "Cyno - Onyx"


def test_compose_onvideo_label_ship_only_when_no_rule(monkeypatch):
    # No matching rule/override → activity empty; ship type alone is shown.
    host = _compose_host()
    host.config["preview"]["labels_on_video"] = True
    monkeypatch.setattr(fc_gui.FCToolGUI, "_active_doctrine_obj",
                        lambda self: None, raising=False)
    host._overlay_rules = lambda: []
    st = CS(character_id=1, name="Kirejen", online=True, ship_type_name="Onyx")
    host._preview_state_for = lambda key: st
    tile = FakeTile(host, "kirejen")
    host._preview_tiles = {1: tile}
    host._preview_compose_captions({1: _cw(1, "Kirejen")})
    assert tile.video_labels[-1] == "Onyx"


def test_compose_onvideo_label_empty_when_labels_off(monkeypatch):
    # labels_on_video off → the on-video label is cleared to '' every tick.
    host = _compose_host()
    host.config["preview"]["labels_on_video"] = False
    monkeypatch.setattr(fc_gui.FCToolGUI, "_active_doctrine_obj",
                        lambda self: None, raising=False)
    host._overlay_rules = lambda: [
        overlay_rules.OverlayRule("ship_group", "Force Recon Ship", "Cyno")]
    st = CS(character_id=1, name="Kirejen", online=True,
            ship_group="Force Recon Ship", ship_type_name="Onyx")
    host._preview_state_for = lambda key: st
    tile = FakeTile(host, "kirejen")
    host._preview_tiles = {1: tile}
    host._preview_compose_captions({1: _cw(1, "Kirejen")})
    assert tile.video_labels[-1] == ""


def test_compose_onvideo_label_hidden_for_login(monkeypatch):
    # Login screens have no ship + no rule → the on-video label is hidden ('').
    host = _compose_host()
    host.config["preview"]["labels_on_video"] = True
    monkeypatch.setattr(fc_gui.FCToolGUI, "_active_doctrine_obj",
                        lambda self: None, raising=False)
    host._overlay_rules = lambda: []
    host._preview_state_for = lambda key: None
    tile = FakeTile(host, "login")
    host._preview_tiles = {1: tile}
    login = _cw(1, "")          # empty char name → login screen client
    host._preview_compose_captions({1: login})
    assert tile.video_labels[-1] == ""


def test_compose_video_labels_clears_legacy_overlay():
    # The retired native OverlayWindow routing: if a legacy overlay lingers, the
    # method clears it with [] (native tiles now own the on-video label).
    host = make_host(mode="native")
    ov = FakeOverlay(host)
    host._overlay = ov
    host._preview_compose_video_labels({})
    assert ov.label_pushes[-1] == []


def test_compose_video_labels_no_overlay_is_noop():
    # No legacy overlay → nothing to clear, no crash.
    host = make_host(mode="native")
    host._overlay = None
    host._preview_compose_video_labels({})   # must not raise
    assert host._overlay is None


# ── (B3) intel flash: own-log system index + tile-border alerts ──────────────
from intel_monitor import IntelReport
import datetime as _dt


def _report(system_id, report_type="hostile"):
    return IntelReport(
        timestamp=_dt.datetime(2026, 7, 3, 18, 0, 0),
        channel="Intel", reporter="Scout", system_name="Foo",
        system_id=system_id, report_type=report_type)


def _bind_intel_methods(host):
    for name in ("_preview_intel_note", "_preview_should_flash"):
        fn = getattr(fc_gui.FCToolGUI, name)
        setattr(host, name, types.MethodType(fn, host))


# --- pure _preview_intel_note(index, report, now) -----------------------------
def test_intel_note_hostile_inserts_system_with_timestamp():
    host = make_host(mode="native")
    _bind_intel_methods(host)
    idx = {}
    host._preview_intel_note(idx, _report(30000142, "hostile"), 100.0)
    assert idx == {30000142: (100.0, "hostile")}


def test_intel_note_clear_deletes_entry():
    host = make_host(mode="native")
    _bind_intel_methods(host)
    idx = {30000142: (50.0, "hostile")}
    host._preview_intel_note(idx, _report(30000142, "clear"), 100.0)
    assert 30000142 not in idx


def test_intel_note_ignores_report_types_not_selected():
    # default intel_report_types == ["hostile"]; a dscan/info report is ignored
    host = make_host(mode="native")
    _bind_intel_methods(host)
    idx = {}
    host._preview_intel_note(idx, _report(30000142, "dscan"), 100.0)
    host._preview_intel_note(idx, _report(30000142, "info"), 100.0)
    assert idx == {}


def test_intel_note_ignores_report_without_system_id():
    host = make_host(mode="native")
    _bind_intel_methods(host)
    idx = {}
    host._preview_intel_note(idx, _report(None, "hostile"), 100.0)
    assert idx == {}


def test_intel_note_respects_custom_report_types():
    host = make_host(mode="native")
    host.config["preview"]["intel_report_types"] = ["hostile", "info"]
    _bind_intel_methods(host)
    idx = {}
    host._preview_intel_note(idx, _report(30000142, "info"), 100.0)
    assert idx == {30000142: (100.0, "info")}


# --- pure _preview_should_flash(index, state, cfg, now) -----------------------
def test_should_flash_true_when_system_recent_and_enabled():
    host = make_host(mode="native")
    _bind_intel_methods(host)
    cfg = host._preview_cfg()
    cfg["intel_flash"] = True
    cfg["intel_flash_secs"] = 10
    idx = {30000142: (100.0, "hostile")}
    st = CS(character_id=1, name="K", solar_system_id=30000142)
    assert host._preview_should_flash(idx, st, cfg, 105.0) is True


def test_should_flash_false_when_expired():
    host = make_host(mode="native")
    _bind_intel_methods(host)
    cfg = host._preview_cfg()
    cfg["intel_flash"] = True
    cfg["intel_flash_secs"] = 10
    idx = {30000142: (100.0, "hostile")}
    st = CS(character_id=1, name="K", solar_system_id=30000142)
    assert host._preview_should_flash(idx, st, cfg, 111.0) is False


def test_should_flash_false_when_disabled():
    host = make_host(mode="native")
    _bind_intel_methods(host)
    cfg = host._preview_cfg()
    cfg["intel_flash"] = False
    idx = {30000142: (100.0, "hostile")}
    st = CS(character_id=1, name="K", solar_system_id=30000142)
    assert host._preview_should_flash(idx, st, cfg, 101.0) is False


def test_should_flash_false_when_system_not_in_index():
    host = make_host(mode="native")
    _bind_intel_methods(host)
    cfg = host._preview_cfg()
    cfg["intel_flash"] = True
    idx = {30000142: (100.0, "hostile")}
    st = CS(character_id=1, name="K", solar_system_id=30000999)
    assert host._preview_should_flash(idx, st, cfg, 101.0) is False


def test_should_flash_false_when_state_or_system_missing():
    host = make_host(mode="native")
    _bind_intel_methods(host)
    cfg = host._preview_cfg()
    cfg["intel_flash"] = True
    idx = {30000142: (100.0, "hostile")}
    assert host._preview_should_flash(idx, None, cfg, 101.0) is False
    st = CS(character_id=1, name="K", solar_system_id=None)
    assert host._preview_should_flash(idx, st, cfg, 101.0) is False


# --- tick integration: flashing tile gets the border, cleared after expiry ---
def _flash_tick_host(monkeypatch, now):
    """A tick-ready host with one live client whose ESI state sits in a hostile
    system, intel_flash on, and a controlled monotonic clock."""
    host = make_host(mode="native", layouts={"kirejen": [10, 20, 384, 216]})
    cfg = host._preview_cfg()
    cfg["intel_flash"] = True
    cfg["intel_flash_secs"] = 10
    cfg["intel_flash_color"] = "#ff3b30"
    host._preview_find_clients = lambda: [_cw(1, "Kirejen")]
    st = CS(character_id=1, name="Kirejen", online=True, solar_system_id=30000142)
    host._preview_state_for = lambda key: st
    monkeypatch.setattr(fc_gui.time, "monotonic", lambda: now[0])
    return host


def test_tick_sets_flash_border_for_client_in_hostile_system(monkeypatch):
    now = [100.0]
    host = _flash_tick_host(monkeypatch, now)
    host._preview_intel = {30000142: (100.0, "hostile")}
    tick(host)
    tile = host._preview_tiles[1]
    assert tile.borders and tile.borders[-1] == "#ff3b30"


def test_tick_clears_flash_border_after_expiry(monkeypatch):
    now = [100.0]
    host = _flash_tick_host(monkeypatch, now)
    host._preview_intel = {30000142: (100.0, "hostile")}
    tick(host)
    assert host._preview_tiles[1].borders[-1] == "#ff3b30"
    # advance past the flash window → the border is cleared (None)
    now[0] = 120.0
    tick(host)
    assert host._preview_tiles[1].borders[-1] is None


def test_tick_no_flash_border_when_intel_flash_disabled(monkeypatch):
    now = [100.0]
    host = _flash_tick_host(monkeypatch, now)
    host._preview_cfg()["intel_flash"] = False
    host._preview_intel = {30000142: (100.0, "hostile")}
    tick(host)
    tile = host._preview_tiles[1]
    # border is only ever set to the "clear" value (None); never the flash color
    assert "#ff3b30" not in tile.borders


# ── (B6) damage flash: gamelog ingest + tick border precedence + hold ─────────
def test_on_damage_ingests_lowercased_into_tracker(monkeypatch):
    host = make_host(mode="native")
    host._preview_on_damage = types.MethodType(
        fc_gui.FCToolGUI._preview_on_damage, host)
    monkeypatch.setattr(fc_gui.time, "monotonic", lambda: 100.0)
    from gamelog_monitor import DamageEvent
    host._preview_on_damage(DamageEvent(timestamp="", character_name="Kirejen",
                                        amount=250, attacker=""))
    # 10% of weakest layer 2000 = 200; 250 >= 200 → the tracker would flash
    cfg = {"damage_flash_pct": 10, "damage_flash_window_s": 5,
           "damage_flash_cooldown_s": 3, "damage_flash_reference": "weakest"}
    hp = {"shield": 4000.0, "armor": 3000.0, "hull": 2000.0}
    assert host._preview_damage.should_flash("kirejen", hp, cfg, 100.1)


def test_on_damage_ignores_blank_character(monkeypatch):
    host = make_host(mode="native")
    host._preview_on_damage = types.MethodType(
        fc_gui.FCToolGUI._preview_on_damage, host)
    monkeypatch.setattr(fc_gui.time, "monotonic", lambda: 100.0)
    from gamelog_monitor import DamageEvent
    host._preview_on_damage(DamageEvent(timestamp="", character_name="   ",
                                        amount=999, attacker=""))
    cfg = {"damage_flash_pct": 10, "damage_flash_window_s": 5,
           "damage_flash_cooldown_s": 3, "damage_flash_reference": "weakest"}
    hp = {"shield": 4000.0, "armor": 3000.0, "hull": 2000.0}
    # nothing was ingested under any key → no flash
    assert not host._preview_damage.should_flash("", hp, cfg, 100.1)


def _damage_tick_host(monkeypatch, now):
    """A tick-ready host with one live client, damage_flash on, known base HP,
    and a controlled monotonic clock."""
    host = make_host(mode="native", layouts={"kirejen": [10, 20, 384, 216]})
    cfg = host._preview_cfg()
    cfg["damage_flash"] = True
    cfg["damage_flash_mode"] = "threshold"     # exercise the pct-of-HP path here
    cfg["damage_flash_pct"] = 10
    cfg["damage_flash_window_s"] = 5
    cfg["damage_flash_cooldown_s"] = 3
    cfg["damage_flash_reference"] = "weakest"
    cfg["damage_flash_color"] = "#ff3b30"
    host._preview_find_clients = lambda: [_cw(1, "Kirejen")]
    host._preview_layer_hp = {"kirejen": {"shield": 4000.0, "armor": 3000.0,
                                          "hull": 2000.0}}
    host._preview_state_for = lambda key: None
    monkeypatch.setattr(fc_gui.time, "monotonic", lambda: now[0])
    return host


def test_tick_sets_damage_border_when_windowed_damage_crosses_threshold(monkeypatch):
    now = [100.0]
    host = _damage_tick_host(monkeypatch, now)
    # 250 dmg >= 10% of weakest (2000)=200 → flash
    host._preview_damage.add("kirejen", 250, now[0])
    tick(host)
    tile = host._preview_tiles[1]
    assert tile.borders and tile.borders[-1] == "#ff3b30"


def test_tick_holds_damage_border_across_frames_then_clears(monkeypatch):
    now = [100.0]
    host = _damage_tick_host(monkeypatch, now)
    host._preview_damage.add("kirejen", 250, now[0])
    tick(host)
    # At the arming frame the pulse is at its peak (elapsed 0) → the peak colour.
    assert host._preview_tiles[1].borders[-1] == "#ff3b30"
    # 1 s later: no new damage, but the hold (window_s=5) keeps a non-empty
    # pulsing border (a valid #rrggbb, never None).
    now[0] = 101.0
    tick(host)
    mid = host._preview_tiles[1].borders[-1]
    assert mid is not None and mid.startswith("#") and len(mid) == 7
    # past the hold (seeded at 100.0 + window_s 5 = 105.0): border clears to None.
    now[0] = 106.0
    tick(host)
    assert host._preview_tiles[1].borders[-1] is None


# ── (C2) shown-chars set: all_known - disabled (pure, show-oriented) ──────────
def test_shown_chars_subtracts_disabled_case_insensitively():
    fn = fc_gui.FCToolGUI._preview_shown_chars
    all_known = {"kirejen", "alt two", "boss"}
    assert fn(all_known, ["Boss", "KIREJEN"]) == {"alt two"}
    assert fn(all_known, []) == {"kirejen", "alt two", "boss"}
    assert fn(all_known, ["kirejen", "alt two", "boss"]) == set()


def test_all_known_chars_unions_esi_live_and_saved_layouts():
    host = make_host(mode="native", layouts={"ghost": [0, 0, 384, 216],
                                             "kirejen": [1, 1, 384, 216]})
    host.esi_accounts = [SimpleNamespace(character_name="Kirejen"),
                         SimpleNamespace(character_name="Bob")]
    host._preview_clients = {1: _cw(1, "Alt Two"), 2: _cw(2, "")}  # login excluded
    known = host._preview_all_known_chars()
    # union, lowercased, login screens contribute nothing
    assert known == {"kirejen", "bob", "alt two", "ghost"}


# ── (C2) _preview_visibility pure hide rules ─────────────────────────────────
def _vis(cur, fg_info, cfg, tick_count=0, lost_since=None):
    return fc_gui.FCToolGUI._preview_visibility(
        cur, fg_info, cfg, tick_count, lost_since)


def _fg(active_hwnd=None, focused=True):
    return SimpleNamespace(active_hwnd=active_hwnd, focused=focused)


def test_visibility_all_rules_off_hides_nothing():
    cur = {1: _cw(1, "A"), 2: _cw(2, "")}
    cfg = {"hide_active": False, "hide_login": False,
           "hide_on_lost_focus": False, "hide_delay_ticks": 4}
    hidden, lost = _vis(cur, _fg(active_hwnd=1, focused=True), cfg)
    assert hidden == set()
    assert lost is None


def test_visibility_hide_active_hides_foreground_client_only():
    cur = {1: _cw(1, "A"), 2: _cw(2, "B")}
    cfg = {"hide_active": True, "hide_login": False,
           "hide_on_lost_focus": False, "hide_delay_ticks": 4}
    hidden, _ = _vis(cur, _fg(active_hwnd=2, focused=True), cfg)
    assert hidden == {2}
    # nothing foregrounded → nothing hidden by this rule
    hidden, _ = _vis(cur, _fg(active_hwnd=None, focused=True), cfg)
    assert hidden == set()


def test_visibility_hide_login_hides_login_tiles():
    cur = {1: _cw(1, "A"), 2: _cw(2, ""), 3: _cw(3, "")}
    cfg = {"hide_active": False, "hide_login": True,
           "hide_on_lost_focus": False, "hide_delay_ticks": 4}
    hidden, _ = _vis(cur, _fg(active_hwnd=1, focused=True), cfg)
    assert hidden == {2, 3}


def test_visibility_lost_focus_delays_then_hides_all_then_resets():
    cur = {1: _cw(1, "A"), 2: _cw(2, "B")}
    cfg = {"hide_active": False, "hide_login": False,
           "hide_on_lost_focus": True, "hide_delay_ticks": 3}
    # tick 10: focus just lost → countdown seeded, nothing hidden yet
    hidden, lost = _vis(cur, _fg(focused=False), cfg, tick_count=10, lost_since=None)
    assert hidden == set() and lost == 10
    # tick 12: still < delay ticks elapsed (12-10=2 < 3) → not yet
    hidden, lost = _vis(cur, _fg(focused=False), cfg, tick_count=12, lost_since=10)
    assert hidden == set() and lost == 10
    # tick 13: 3 ticks elapsed → hide every tile
    hidden, lost = _vis(cur, _fg(focused=False), cfg, tick_count=13, lost_since=10)
    assert hidden == {1, 2} and lost == 10
    # focus returns → countdown cleared, nothing hidden
    hidden, lost = _vis(cur, _fg(focused=True), cfg, tick_count=14, lost_since=10)
    assert hidden == set() and lost is None


def test_visibility_lost_focus_zero_delay_hides_immediately():
    cur = {1: _cw(1, "A")}
    cfg = {"hide_active": False, "hide_login": False,
           "hide_on_lost_focus": True, "hide_delay_ticks": 0}
    hidden, lost = _vis(cur, _fg(focused=False), cfg, tick_count=5, lost_since=None)
    assert hidden == {1} and lost == 5


# ── (C2) tick integration: hidden tiles withdrawn (not destroyed), skip retop ─
class _FgWin32:
    """Foreground-only fake: get_foreground() returns a scripted hwnd."""

    def __init__(self, fg_hwnd):
        self._fg = fg_hwnd

    def get_foreground(self):
        return self._fg


def test_tick_hide_active_withdraws_foreground_tile_keeps_layout():
    host = make_host(mode="native", layouts={"a": [0, 0, 384, 216],
                                             "b": [1, 1, 384, 216]})
    host._preview_cfg().update(hide_active=True)
    ca, cb = _cw(1, "A"), _cw(2, "B")
    host._preview_find_clients = lambda: [ca, cb]
    host._preview_win32 = _FgWin32(1)          # client A is foreground
    tick(host)
    ta, tb = host._preview_tiles[1], host._preview_tiles[2]
    assert ta.hidden is True and ta.hide_calls == 1
    assert tb.hidden is False
    # hidden tile is NOT retopped this tick
    assert ("tile", "a") not in host.retop_order
    assert ("tile", "b") in host.retop_order
    # layout preserved (never wiped)
    assert host.config["preview"]["layouts"]["a"] == [0, 0, 384, 216]
    # foreground moves to B → A re-shown, B hidden
    host._preview_win32 = _FgWin32(2)
    tick(host)
    assert ta.hidden is False and ta.show_calls == 1
    assert tb.hidden is True


def test_tick_disabled_char_never_spawns_tile_and_keeps_layout():
    host = make_host(mode="native", layouts={"alt": [3, 4, 384, 216]},
                     disabled_chars=["alt"])
    host._preview_find_clients = lambda: [_cw(1, "Alt")]
    tick(host)
    assert host._preview_tiles == {}          # disabled → no tile
    assert host.config["preview"]["layouts"]["alt"] == [3, 4, 384, 216]


def test_tick_pushes_shown_chars_to_gamelog_monitor():
    host = make_host(mode="native", disabled_chars=["boss"])
    host.esi_accounts = [SimpleNamespace(character_name="Kirejen"),
                         SimpleNamespace(character_name="Boss")]
    tracked = {}
    host._preview_gamelog = SimpleNamespace(
        set_tracked_characters=lambda names: tracked.__setitem__(
            "names", set(names)))
    host._preview_find_clients = lambda: [_cw(1, "Alt Two")]
    tick(host)
    # shown = all_known(kirejen, boss, alt two) - disabled(boss)
    assert tracked["names"] == {"kirejen", "alt two"}


def test_tick_damage_flash_beats_intel_flash(monkeypatch):
    now = [100.0]
    host = _damage_tick_host(monkeypatch, now)
    # ALSO in a hostile system with intel flash on — damage must win precedence
    cfg = host._preview_cfg()
    cfg["intel_flash"] = True
    cfg["intel_flash_secs"] = 10
    cfg["intel_flash_color"] = "#0000ff"
    st = CS(character_id=1, name="Kirejen", online=True, solar_system_id=30000142)
    host._preview_state_for = lambda key: st
    host._preview_intel = {30000142: (100.0, "hostile")}
    host._preview_damage.add("kirejen", 250, now[0])
    tick(host)
    tile = host._preview_tiles[1]
    assert tile.borders[-1] == "#ff3b30"     # damage color, not intel's blue


def test_tick_threshold_mode_degrades_to_flash_when_hp_unknown(monkeypatch):
    # THE ROOT-CAUSE FIX: in threshold mode with UNKNOWN base HP the flash must
    # NOT be silently suppressed — it degrades to any-damage and still fires.
    now = [100.0]
    host = _damage_tick_host(monkeypatch, now)        # mode == 'threshold'
    host._preview_layer_hp = {}                        # no base HP known
    host._preview_damage.add("kirejen", 62, now[0])    # a real death-log hit
    tick(host)
    tile = host._preview_tiles[1]
    assert tile.borders[-1] == "#ff3b30"               # flashed (elapsed 0 → peak)


def test_tick_any_mode_default_flashes_on_any_damage_without_hp(monkeypatch):
    # The DEFAULT mode ('any'): no HP, no threshold — any incoming damage flashes.
    now = [100.0]
    host = _damage_tick_host(monkeypatch, now)
    host._preview_cfg()["damage_flash_mode"] = "any"
    host._preview_layer_hp = {}
    host._preview_damage.add("kirejen", 1, now[0])     # one point of damage
    tick(host)
    assert host._preview_tiles[1].borders[-1] == "#ff3b30"


def test_tick_no_damage_border_when_damage_flash_disabled(monkeypatch):
    now = [100.0]
    host = _damage_tick_host(monkeypatch, now)
    host._preview_cfg()["damage_flash"] = False
    host._preview_damage.add("kirejen", 99999, now[0])
    tick(host)
    tile = host._preview_tiles[1]
    assert "#ff3b30" not in tile.borders


# ── (C3) minimize-inactive on activation switch + never-minimize list ─────────
def _bind_switch_methods(host):
    """Bind the C3 activation/minimize surface onto a bare host.

    NON-DISRUPTIVE: the activation + minimize primitives (window_activator.*) are
    monkeypatched by every test below — nothing here touches a real window,
    changes real focus, or minimizes a live client.
    """
    for name in ("_preview_switch_to", "_preview_on_tile_activate",
                 "_preview_on_tile_minimize", "_preview_drain_hotkeys"):
        fn = getattr(fc_gui.FCToolGUI, name)
        setattr(host, name, types.MethodType(fn, host))


def _minmax_host(monkeypatch, minimize_inactive=True, never=None, last_key=""):
    """Host with two live clients (a=hwnd1, b=hwnd2), recording activate/minimize."""
    activated, minimized = [], []
    monkeypatch.setattr(window_activator, "activate",
                        lambda hwnd, **k: activated.append(hwnd) or True)
    monkeypatch.setattr(window_activator, "minimize",
                        lambda hwnd, **k: minimized.append(hwnd))
    host = make_host(mode="native")
    _bind_switch_methods(host)
    cfg = host._preview_cfg()
    cfg["minimize_inactive"] = minimize_inactive
    cfg["never_minimize"] = list(never or [])
    host._preview_clients = {1: _cw(1, "A"), 2: _cw(2, "B")}
    host._preview_last_key = last_key
    return host, activated, minimized


# --- _preview_switch_to: the shared activation choke-point -------------------
def test_switch_to_minimizes_previous_active_when_enabled(monkeypatch):
    host, activated, minimized = _minmax_host(monkeypatch, last_key="a")
    host._preview_switch_to(host._preview_clients[2])   # switch a -> b
    assert activated == [2]                              # new client foregrounded
    assert minimized == [1]                             # previous (a) minimized
    assert host._preview_last_key == "b"                # anchor updated


def test_switch_to_does_not_minimize_when_disabled(monkeypatch):
    host, activated, minimized = _minmax_host(
        monkeypatch, minimize_inactive=False, last_key="a")
    host._preview_switch_to(host._preview_clients[2])
    assert activated == [2]
    assert minimized == []                              # feature off → no minimize
    assert host._preview_last_key == "b"


def test_switch_to_respects_never_minimize_list(monkeypatch):
    host, activated, minimized = _minmax_host(
        monkeypatch, never=["a"], last_key="a")
    host._preview_switch_to(host._preview_clients[2])
    assert activated == [2]
    assert minimized == []                              # a is exempt → not minimized
    assert host._preview_last_key == "b"


def test_switch_to_never_minimizes_the_client_being_activated(monkeypatch):
    # Re-activating the already-active client must not minimize it (prev == new).
    host, activated, minimized = _minmax_host(monkeypatch, last_key="a")
    host._preview_switch_to(host._preview_clients[1])   # switch a -> a
    assert activated == [1]
    assert minimized == []                              # never minimize self
    assert host._preview_last_key == "a"


def test_switch_to_no_previous_key_minimizes_nothing(monkeypatch):
    # First activation of the session (no prior anchor) → nothing to minimize.
    host, activated, minimized = _minmax_host(monkeypatch, last_key="")
    host._preview_switch_to(host._preview_clients[1])
    assert activated == [1]
    assert minimized == []
    assert host._preview_last_key == "a"


def test_switch_to_ignores_previous_key_with_no_live_client(monkeypatch):
    # The prior active client is gone (closed/undocked); only the new one is live.
    host, activated, minimized = _minmax_host(monkeypatch, last_key="ghost")
    host._preview_switch_to(host._preview_clients[2])
    assert activated == [2]
    assert minimized == []                              # ghost has no live hwnd
    assert host._preview_last_key == "b"


# --- the three activation entry points all route through _preview_switch_to ---
def test_tile_activate_minimizes_previous_active(monkeypatch):
    host, activated, minimized = _minmax_host(monkeypatch, last_key="a")
    host._preview_on_tile_activate("b")                 # click B's tile
    assert activated == [2] and minimized == [1]
    assert host._preview_last_key == "b"


def test_hotkey_focus_minimizes_previous_active(monkeypatch):
    host, activated, minimized = _minmax_host(monkeypatch, last_key="a")
    svc = FakeHotkeys()
    svc.events.put(101)
    host._preview_hotkeys = svc
    host._preview_hotkey_map = {101: ("focus", "b")}
    host._preview_drain_hotkeys()
    assert activated == [2] and minimized == [1]
    assert host._preview_last_key == "b"


def test_hotkey_cycle_minimizes_previous_active(monkeypatch):
    host, activated, minimized = _minmax_host(monkeypatch, last_key="a")
    svc = FakeHotkeys()
    svc.events.put(200)
    host._preview_hotkeys = svc
    host._preview_hotkey_map = {200: ("cycle", 0, +1)}
    # cycle order a -> b over live {a, b}
    host._preview_cfg()["hotkeys"] = {
        "focus": {}, "groups": [{"next": [], "prev": [], "order": ["a", "b"]}],
        "minimize_all": []}
    host._preview_drain_hotkeys()
    assert activated == [2] and minimized == [1]
    assert host._preview_last_key == "b"


# --- Ctrl+Left on a tile still minimizes THAT client (A5 wiring preserved) ----
def test_tile_minimize_minimizes_that_client(monkeypatch):
    host, activated, minimized = _minmax_host(monkeypatch, last_key="a")
    host._preview_on_tile_minimize("b")
    assert minimized == [2]                             # the named tile's client
    assert activated == []                             # minimize is not an activate


# --- minimize_all hotkey skips the never-minimize list ------------------------
def test_minall_hotkey_skips_never_minimize(monkeypatch):
    host, activated, minimized = _minmax_host(
        monkeypatch, never=["a"], last_key="")
    svc = FakeHotkeys()
    svc.events.put(300)
    host._preview_hotkeys = svc
    host._preview_hotkey_map = {300: ("minall",)}
    host._preview_drain_hotkeys()
    assert set(minimized) == {2}                        # b minimized, a exempt
    assert activated == []


# ── (C4) active highlight border: highlight > none, damage/intel still win ─────
def test_tick_highlights_active_client_tile_by_last_key(monkeypatch):
    host = make_host(mode="native", layouts={"a": [0, 0, 384, 216],
                                             "b": [1, 1, 384, 216]})
    host._preview_cfg().update(highlight_active=True, highlight_color="#00d4ff")
    host._preview_last_key = "a"                        # A is the active client
    host._preview_find_clients = lambda: [_cw(1, "A"), _cw(2, "B")]
    tick(host)
    assert host._preview_tiles[1].borders[-1] == "#00d4ff"   # active → highlight
    assert host._preview_tiles[2].borders[-1] is None        # inactive → no border


def test_tick_highlights_active_client_tile_by_foreground(monkeypatch):
    host = make_host(mode="native", layouts={"a": [0, 0, 384, 216],
                                             "b": [1, 1, 384, 216]})
    host._preview_cfg().update(highlight_active=True, highlight_color="#00d4ff")
    host._preview_last_key = ""                         # no activation yet
    host._preview_win32 = _FgWin32(2)                  # B is foreground
    host._preview_find_clients = lambda: [_cw(1, "A"), _cw(2, "B")]
    tick(host)
    assert host._preview_tiles[2].borders[-1] == "#00d4ff"   # foreground → highlight
    assert host._preview_tiles[1].borders[-1] is None


def test_tick_no_highlight_when_disabled(monkeypatch):
    host = make_host(mode="native", layouts={"a": [0, 0, 384, 216]})
    host._preview_cfg().update(highlight_active=False, highlight_color="#00d4ff")
    host._preview_last_key = "a"
    host._preview_find_clients = lambda: [_cw(1, "A")]
    tick(host)
    assert "#00d4ff" not in host._preview_tiles[1].borders


def test_tick_damage_flash_beats_active_highlight(monkeypatch):
    now = [100.0]
    host = _damage_tick_host(monkeypatch, now)
    host._preview_cfg().update(highlight_active=True, highlight_color="#00d4ff")
    host._preview_last_key = "kirejen"                  # would otherwise highlight
    host._preview_damage.add("kirejen", 250, now[0])
    tick(host)
    assert host._preview_tiles[1].borders[-1] == "#ff3b30"   # damage wins


def test_tick_intel_flash_beats_active_highlight(monkeypatch):
    now = [100.0]
    host = _flash_tick_host(monkeypatch, now)
    host._preview_cfg().update(highlight_active=True, highlight_color="#00d4ff",
                               intel_flash_color="#ff3b30")
    host._preview_last_key = "kirejen"                  # would otherwise highlight
    host._preview_intel = {30000142: (100.0, "hostile")}
    tick(host)
    assert host._preview_tiles[1].borders[-1] == "#ff3b30"   # intel wins over highlight


# ── (C4) cycle exclusion: Shift+Left toggles, excluded keys skipped by cycle ───
def test_tile_exclude_toggles_session_exclusion_set_and_badge():
    host = make_host(mode="native")
    host._preview_on_tile_exclude = types.MethodType(
        fc_gui.FCToolGUI._preview_on_tile_exclude, host)
    ta = FakeTile(host, "a")
    host._preview_clients = {1: _cw(1, "A")}
    host._preview_tiles = {1: ta}
    host._preview_on_tile_exclude("a")
    assert "a" in host._preview_excluded
    assert ta.excluded is True                         # badge dot pushed to the tile
    host._preview_on_tile_exclude("a")                 # toggle back
    assert "a" not in host._preview_excluded
    assert ta.excluded is False


def test_cycle_skips_excluded_keys(monkeypatch):
    host, activated, minimized = _minmax_host(monkeypatch, last_key="a")
    host._preview_clients = {1: _cw(1, "A"), 2: _cw(2, "B"), 3: _cw(3, "C")}
    host._preview_excluded = {"b"}                      # B excluded from cycling
    svc = FakeHotkeys()
    svc.events.put(200)
    host._preview_hotkeys = svc
    host._preview_hotkey_map = {200: ("cycle", 0, +1)}
    host._preview_cfg()["hotkeys"] = {
        "focus": {}, "groups": [{"next": [], "prev": [],
                                 "order": ["a", "b", "c"]}],
        "minimize_all": []}
    host._preview_drain_hotkeys()
    assert activated == [3]                             # a -> c (b skipped)
    assert host._preview_last_key == "c"


# ── (C4) switch-to-non-EVE: external hwnd captured in tick, activated on demand ─
class _FgWin32Ext:
    """Foreground fake reporting an hwnd that is neither an EVE client nor ours."""

    def __init__(self, fg_hwnd):
        self._fg = fg_hwnd

    def get_foreground(self):
        return self._fg


def test_tick_captures_external_foreground_hwnd(monkeypatch):
    host = make_host(mode="native", layouts={"a": [0, 0, 384, 216]})
    host._preview_find_clients = lambda: [_cw(1, "A")]
    host._preview_win32 = _FgWin32Ext(9999)            # a non-EVE, non-ours window
    tick(host)
    assert host._preview_last_external_hwnd == 9999


def test_tick_does_not_capture_eve_client_as_external(monkeypatch):
    host = make_host(mode="native", layouts={"a": [0, 0, 384, 216]})
    host._preview_find_clients = lambda: [_cw(1, "A")]
    host._preview_win32 = _FgWin32(1)                  # foreground IS an EVE client
    tick(host)
    assert host._preview_last_external_hwnd is None


def test_switch_external_activates_captured_hwnd(monkeypatch):
    activated = []
    monkeypatch.setattr(window_activator, "activate",
                        lambda hwnd, **k: activated.append(hwnd) or True)
    host = make_host(mode="native")
    host._preview_on_tile_switch_external = types.MethodType(
        fc_gui.FCToolGUI._preview_on_tile_switch_external, host)
    host._preview_last_external_hwnd = 4242
    host._preview_on_tile_switch_external()
    assert activated == [4242]


def test_switch_external_noop_when_nothing_captured(monkeypatch):
    activated = []
    monkeypatch.setattr(window_activator, "activate",
                        lambda hwnd, **k: activated.append(hwnd) or True)
    host = make_host(mode="native")
    host._preview_on_tile_switch_external = types.MethodType(
        fc_gui.FCToolGUI._preview_on_tile_switch_external, host)
    host._preview_last_external_hwnd = None
    host._preview_on_tile_switch_external()
    assert activated == []


# ── native enable/teardown lifecycle: ESI poller + seed rules + state clears ──
# The bug fixed here: native mode never started the ESI poller (the ONLY writer
# of _overlay_states/_preview_layer_hp), so captions never showed rule labels or
# doctrine tags, status dots stayed dim, and damage flash could never fire.
# These tests run the REAL _preview_enable_native/_preview_teardown with the
# REAL _overlay_start_poller/_overlay_stop_poller; only the poller THREAD BODY
# is a probe (no ESI), and the heavy native edges (hotkeys/gamelog/win32/tick)
# are stubs. NON-DISRUPTIVE: no window, hotkey, focus grab, or network call.
import threading
import time as _time


class _PollerProbe:
    """Replaces the _overlay_poll_loop BODY only (start/stop stay real): each
    run records (thread, captured stop Event) — the same first read the real
    daemon loop does — then parks on that Event until it is fired."""

    def __init__(self, host):
        self.host = host
        self.runs = []                    # [(threading.Thread, threading.Event)]

    def __call__(self):
        stop = self.host._overlay_poller_stop
        self.runs.append((threading.current_thread(), stop))
        stop.wait(10.0)


def _wait_until(cond, timeout=2.0):
    deadline = _time.monotonic() + timeout
    while not cond():
        if _time.monotonic() > deadline:
            raise AssertionError("condition not reached within %ss" % timeout)
        _time.sleep(0.01)


def _lifecycle_host():
    host = make_host(mode="native")
    for name in ("_preview_enable_native", "_preview_teardown",
                 "_preview_disable_native",
                 "_overlay_seed_rules_if_empty",
                 "_overlay_start_poller", "_overlay_stop_poller"):
        setattr(host, name,
                types.MethodType(getattr(fc_gui.FCToolGUI, name), host))
    host._overlay_poller = None
    host._overlay_poller_stop = None
    probe = _PollerProbe(host)
    host._overlay_poll_loop = probe       # Thread target resolves this attribute
    # heavy native edges stubbed (recorded where an assertion wants them)
    host.lifecycle_calls = []
    host._preview_restart_hotkeys = (
        lambda: host.lifecycle_calls.append("hotkeys"))
    host._preview_start_gamelog = (
        lambda: host.lifecycle_calls.append("gamelog"))
    host._preview_stop_gamelog = (
        lambda: host.lifecycle_calls.append("gamelog_stop"))
    host._preview_tick = lambda: host.lifecycle_calls.append("tick")
    host._preview_win32 = object()        # already resolved → no real Win32 lookup
    return host, probe


def _stop_probe(host, probe):
    """Belt-and-braces cleanup: fire every recorded stop Event + the current."""
    try:
        host._overlay_stop_poller()
    except Exception:
        pass
    for _t, ev in probe.runs:
        ev.set()


def test_enable_native_starts_the_esi_poller():
    host, probe = _lifecycle_host()
    try:
        host._preview_enable_native()
        t = host._overlay_poller
        assert t is not None and t.is_alive()
        _wait_until(lambda: probe.runs)
        # the body captured the freshly-created (not yet fired) stop Event
        assert probe.runs[0][1] is host._overlay_poller_stop
        assert not probe.runs[0][1].is_set()
        # poller starts after the damage-flash source (gamelog) is up
        assert "gamelog" in host.lifecycle_calls
    finally:
        _stop_probe(host, probe)


def test_enable_native_twice_reuses_the_one_alive_poller():
    host, probe = _lifecycle_host()
    try:
        host._preview_enable_native()
        _wait_until(lambda: len(probe.runs) == 1)
        t1 = host._overlay_poller
        host._preview_enable_native()     # idempotent re-boot (set_mode re-assert)
        assert host._overlay_poller is t1
        assert len(probe.runs) == 1       # no second thread body ever ran
    finally:
        _stop_probe(host, probe)


def test_enable_native_seeds_label_rules_when_empty():
    host, probe = _lifecycle_host()
    try:
        assert not host.config.get("overlay", {}).get("rules")
        host._preview_enable_native()
        rules = host.config["overlay"]["rules"]
        # same seed set + dict shape _overlay_enable plants
        expected = [{"when": r.when, "value": r.value, "label": r.label}
                    for r in overlay_rules.seed_rules()]
        assert rules == expected
        assert {"when": "ship_group", "value": "Force Recon Ship",
                "label": "Cyno"} in rules
    finally:
        _stop_probe(host, probe)


def test_enable_native_keeps_existing_label_rules():
    host, probe = _lifecycle_host()
    mine = [{"when": "ship_group", "value": "Logistics Cruiser", "label": "Logi"}]
    host.config["overlay"] = {"rules": list(mine), "overrides": {}}
    try:
        host._preview_enable_native()
        assert host.config["overlay"]["rules"] == mine   # never overwritten
    finally:
        _stop_probe(host, probe)


def test_preview_teardown_stops_poller_and_clears_state_dicts():
    host, probe = _lifecycle_host()
    try:
        host._preview_enable_native()
        _wait_until(lambda: probe.runs)
        t1, ev1 = probe.runs[0]
        # poller-fed state present from a "previous poll pass"
        host._overlay_states = {"kirejen": object()}
        host._overlay_state_ts = {"kirejen": 1.0}
        host._preview_layer_hp = {"kirejen": {"shield": 4000.0}}

        host._preview_teardown()

        assert host._overlay_poller is None
        assert ev1.is_set()               # its own stop Event was fired
        t1.join(2.0)
        assert not t1.is_alive()
        # the three poller-fed dicts are cleared (no stale captions/dots/HP)
        assert host._overlay_states == {}
        assert host._overlay_state_ts == {}
        assert host._preview_layer_hp == {}
    finally:
        _stop_probe(host, probe)


def test_disable_session_stops_the_poller_via_teardown():
    host, probe = _lifecycle_host()
    # the REAL session-disable (make_host installs a recorder by default)
    host._preview_disable_session = types.MethodType(
        fc_gui.FCToolGUI._preview_disable_session, host)
    try:
        host._preview_enable_native()
        _wait_until(lambda: probe.runs)
        t1, ev1 = probe.runs[0]
        host._preview_disable_session()
        assert host._preview_disabled_session is True
        assert host._overlay_poller is None and ev1.is_set()
        t1.join(2.0)
        assert not t1.is_alive()
    finally:
        _stop_probe(host, probe)
