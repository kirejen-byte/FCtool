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

    def retop(self):
        self.retops += 1
        self._host.retop_order.append(("overlay",))


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
