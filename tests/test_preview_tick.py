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

    def refresh_source_size(self):
        self.refreshes += 1

    def set_border(self, color):
        self.borders.append(color)

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
    host._preview_last_key = ""
    host._preview_find_clients = lambda: []
    host._preview_status = ""

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
                 "_preview_drain_hotkeys", "_preview_compose_captions",
                 "_preview_compose_video_labels",
                 "_preview_caption_parts", "_preview_should_flash",
                 "_preview_tile_rect", "_preview_tracked_names"):
        fn = getattr(fc_gui.FCToolGUI, name, None)
        if fn is not None:
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


# ── (g) tracker exception → disable-for-session, no crash ─────────────────────
def test_tracker_exception_disables_session_without_crashing():
    host = make_host()

    def boom():
        raise RuntimeError("enum blew up")
    host._preview_find_clients = boom
    # must not raise
    host._preview_native_tick_body()
    assert host.disabled_session_called == 1


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


# ── (B2) on-video rule labels via the existing OverlayWindow ──────────────────
from preview_tile import STRIP_H


def _labels_host(labels_on_video, overlay=None):
    host = make_host(mode="native")
    host.config["preview"]["labels_on_video"] = labels_on_video
    host._overlay = overlay
    host.config.setdefault("overlay", {"rules": [], "overrides": {}})
    host._overlay_cfg = lambda: host.config["overlay"]
    host._overlay_rules = lambda: []
    host._preview_state_for = lambda key: None
    # lazy-overlay creator: hand back a FakeOverlay and remember it
    def _ensure():
        if host._overlay is None:
            host._overlay = FakeOverlay(host)
        return host._overlay
    host._overlay_ensure_window = _ensure
    for name in ("_preview_compose_video_labels",):
        fn = getattr(fc_gui.FCToolGUI, name, None)
        if fn is not None:
            setattr(host, name, types.MethodType(fn, host))
    return host


def test_video_labels_pushed_for_tiles_with_rule_label(monkeypatch):
    host = _labels_host(labels_on_video=True)
    # two live clients; only Kirejen matches a rule → only it gets a label
    st = CS(character_id=1, name="Kirejen", online=True, ship_group="Force Recon Ship")
    host._overlay_rules = lambda: [
        overlay_rules.OverlayRule("ship_group", "Force Recon Ship", "Cyno")]
    host._preview_state_for = lambda key: st if key == "kirejen" else None
    ck = _cw(1, "Kirejen")
    ca = _cw(2, "Alt")
    host._preview_tiles = {1: FakeTile(host, "kirejen"), 2: FakeTile(host, "alt")}
    # controller-known screen rects (x, y, w, body_h)
    host._preview_tile_rects = {1: (100, 200, 384, 216), 2: (600, 200, 384, 216)}
    host._preview_compose_video_labels({1: ck, 2: ca})
    ov = host._overlay
    assert ov is not None and ov.label_pushes, "overlay should have been created + drawn"
    items = ov.label_pushes[-1]
    # body rect = (x, y+STRIP_H, x+w, y+STRIP_H+body_h); only the matched tile
    assert items == [((100, 200 + STRIP_H, 100 + 384, 200 + STRIP_H + 216), "Cyno")]


def test_video_labels_empty_when_disabled():
    ov = FakeOverlay(None)
    host = _labels_host(labels_on_video=False, overlay=ov)
    host._overlay_rules = lambda: [
        overlay_rules.OverlayRule("ship_group", "Force Recon Ship", "Cyno")]
    st = CS(character_id=1, name="Kirejen", online=True, ship_group="Force Recon Ship")
    host._preview_state_for = lambda key: st
    host._preview_tiles = {1: FakeTile(host, "kirejen")}
    host._preview_tile_rects = {1: (0, 0, 384, 216)}
    host._preview_compose_video_labels({1: _cw(1, "Kirejen")})
    # existing overlay is cleared with [] (labels carried by the strips instead)
    assert ov.label_pushes[-1] == []


def test_video_labels_disabled_does_not_create_overlay():
    host = _labels_host(labels_on_video=False, overlay=None)
    host._preview_tiles = {1: FakeTile(host, "kirejen")}
    host._preview_tile_rects = {1: (0, 0, 384, 216)}
    host._preview_compose_video_labels({1: _cw(1, "Kirejen")})
    # no overlay existed and the option is off → none is lazily created
    assert host._overlay is None


def test_video_labels_no_matches_pushes_empty_list(monkeypatch):
    host = _labels_host(labels_on_video=True)
    # rules present but no client matches → drawn set is empty
    host._overlay_rules = lambda: [
        overlay_rules.OverlayRule("ship_group", "Force Recon Ship", "Cyno")]
    host._preview_state_for = lambda key: CS(character_id=1, name="Kirejen",
                                             online=True, ship_group="Battleship")
    host._preview_tiles = {1: FakeTile(host, "kirejen")}
    host._preview_tile_rects = {1: (0, 0, 384, 216)}
    host._preview_compose_video_labels({1: _cw(1, "Kirejen")})
    # option is on → overlay created, but pushed list is empty (no matches)
    assert host._overlay is not None
    assert host._overlay.label_pushes[-1] == []


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
