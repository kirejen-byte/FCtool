"""Tests for the native-preview settings section (Task A9).

Two host flavors, both house-standard:
  - _ui_host(): a real (withdrawn) Tk root with the settings-relevant methods +
    constants bound onto a bare SimpleNamespace; used for building the section,
    the mode radio, and native-row enable/disable.
  - a pure SimpleNamespace host (no widgets) for _preview_set_mode routing,
    _preview_status_text, and _preview_arrange_grid math.

NON-DISRUPTIVE: no real EVE window, hotkey, or focus grab is ever touched —
mode boot/teardown are recorded on the host; grid math is pure.
"""
import inspect
import types
from types import SimpleNamespace

import pytest

tk = pytest.importorskip("tkinter")

import fc_gui
import preview_layout
import eve_client_tracker


CW = eve_client_tracker.ClientWindow


def _bind_attr(name, attr, host):
    """Bind a class attribute onto a bare host. Staticmethods must NOT receive
    `self` (accessed via the class they are already unwrapped), so leave them as
    plain functions; everything else is an instance method → MethodType-bind."""
    raw = inspect.getattr_static(fc_gui.FCToolGUI, name, None)
    if isinstance(raw, staticmethod):
        return attr
    return types.MethodType(attr, host)


def _cw(hwnd, char_name, rect=(0, 0, 800, 600), is_iconic=False):
    return CW(hwnd=hwnd, char_name=char_name,
              title=("EVE" if not char_name else f"EVE - {char_name}"),
              rect=rect, is_iconic=is_iconic, pid=hwnd)


# ── UI host (real withdrawn Tk root) ─────────────────────────────────────────
def _ui_host(preview_cfg=None, overlay_cfg=None):
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display available")
    root.withdraw()
    host = SimpleNamespace()
    host.root = root
    host.config = {}
    if preview_cfg is not None:
        host.config["preview"] = preview_cfg
    if overlay_cfg is not None:
        host.config["overlay"] = overlay_cfg
    host.esi_accounts = []
    host._overlay = None
    host._overlay_states = {}
    host._overlay_state_ts = {}
    host._overlay_after_id = None
    host._overlay_poller = None
    host._overlay_status_label = None
    host._overlay_thumbs_fn = lambda: []
    host._system_names = ["Jita", "Amarr"]
    # native controller state
    host._preview_tiles = {}
    host._preview_clients = {}
    host._preview_hotkeys = None
    host._preview_hotkey_map = {}
    host._preview_after_id = None
    host._preview_intel = {}
    host._preview_disabled_session = False
    host._preview_tick_count = 0
    host._preview_last_key = ""
    host._preview_status = ""
    host._preview_status_label = None
    saved = {"n": 0}
    host._save_config = lambda: saved.__setitem__("n", saved["n"] + 1)
    host._saved = saved
    # record mode boot/teardown so tests can assert routing without real windows
    calls = {"enable_native": 0, "disable_native": 0,
             "overlay_enable": 0, "overlay_disable": 0}
    host._calls = calls
    host._preview_enable_native = lambda: calls.__setitem__(
        "enable_native", calls["enable_native"] + 1)
    host._preview_disable_native = lambda: calls.__setitem__(
        "disable_native", calls["disable_native"] + 1)
    host._overlay_enable = lambda: calls.__setitem__(
        "overlay_enable", calls["overlay_enable"] + 1)
    host._overlay_disable = lambda: calls.__setitem__(
        "overlay_disable", calls["overlay_disable"] + 1)

    for name in ("_preview_cfg", "_PREVIEW_DEFAULTS", "_overlay_cfg",
                 "_OVERLAY_DEFAULTS", "_build_preview_section",
                 "_preview_set_mode", "_preview_status_text",
                 "_preview_arrange_grid", "_preview_apply_native_state",
                 "_preview_sync_native_widgets",
                 "_open_preview_hotkeys_dialog", "_preview_hotkey_preset",
                 "_preview_restart_hotkeys",
                 "_add_section", "_overlay_apply_style",
                 "_open_overlay_rules_dialog", "_overlay_cycle_color",
                 "_overlay_status_text", "_overlay_rules",
                 "_OVERLAY_COLOR_CYCLE", "_OVERLAY_ANCHORS",
                 "_OVERLAY_ANCHOR_TO_CFG", "_OVERLAY_CFG_TO_ANCHOR"):
        attr = getattr(fc_gui.FCToolGUI, name, None)
        if attr is None:
            continue
        setattr(host, name,
                _bind_attr(name, attr, host) if callable(attr) else attr)
    return root, host


# ── pure host (no widgets) ───────────────────────────────────────────────────
def _pure_host(preview_cfg=None):
    host = SimpleNamespace()
    host.config = {"preview": preview_cfg} if preview_cfg is not None else {}
    saved = {"n": 0}
    host._save_config = lambda: saved.__setitem__("n", saved["n"] + 1)
    host._saved = saved
    host._preview_tiles = {}
    host._preview_clients = {}
    host._overlay = None
    host._overlay_after_id = None
    host._overlay_poller = None
    calls = {"enable_native": 0, "disable_native": 0,
             "overlay_enable": 0, "overlay_disable": 0}
    host._calls = calls
    host._preview_enable_native = lambda: calls.__setitem__(
        "enable_native", calls["enable_native"] + 1)
    host._preview_disable_native = lambda: calls.__setitem__(
        "disable_native", calls["disable_native"] + 1)
    host._overlay_enable = lambda: calls.__setitem__(
        "overlay_enable", calls["overlay_enable"] + 1)
    host._overlay_disable = lambda: calls.__setitem__(
        "overlay_disable", calls["overlay_disable"] + 1)
    host.root = SimpleNamespace(
        winfo_screenwidth=lambda: 1920, winfo_screenheight=lambda: 1080)
    host._preview_status_label = None
    host._preview_native_widgets = []
    host._preview_disabled_session = False
    host._preview_status = ""
    for name in ("_preview_cfg", "_PREVIEW_DEFAULTS", "_preview_set_mode",
                 "_preview_status_text", "_preview_arrange_grid",
                 "_preview_sync_native_widgets"):
        attr = getattr(fc_gui.FCToolGUI, name, None)
        if attr is None:
            continue
        setattr(host, name,
                types.MethodType(attr, host) if callable(attr) else attr)
    return host


# ── build ────────────────────────────────────────────────────────────────────
def test_build_section_creates_mode_radio_and_status():
    root, host = _ui_host(preview_cfg={"mode": "off"})
    try:
        frame = tk.Frame(root)
        host._build_preview_section(frame)
        assert host._preview_mode_var.get() == "off"
        assert host._preview_status_label is not None
    finally:
        root.destroy()


def test_build_section_reflects_native_mode():
    root, host = _ui_host(preview_cfg={"mode": "native"})
    try:
        frame = tk.Frame(root)
        host._build_preview_section(frame)
        assert host._preview_mode_var.get() == "native"
    finally:
        root.destroy()


# ── mode radio → routing + persistence ───────────────────────────────────────
def test_mode_radio_writes_cfg_and_boots_native():
    root, host = _ui_host(preview_cfg={"mode": "off"})
    try:
        frame = tk.Frame(root)
        host._build_preview_section(frame)
        host._preview_mode_var.set("native")
        host._preview_set_mode("native")
        assert host.config["preview"]["mode"] == "native"
        assert host._saved["n"] >= 1
        assert host._calls["enable_native"] == 1
    finally:
        root.destroy()


def test_set_mode_off_from_native_tears_down_native():
    host = _pure_host(preview_cfg={"mode": "native"})
    host._preview_set_mode("off")
    assert host.config["preview"]["mode"] == "off"
    assert host._calls["disable_native"] == 1
    assert host._calls["enable_native"] == 0
    assert host._saved["n"] >= 1


def test_set_mode_eveo_from_native_teardown_native_then_enable_overlay():
    host = _pure_host(preview_cfg={"mode": "native"})
    host._preview_set_mode("eveo_labels")
    assert host.config["preview"]["mode"] == "eveo_labels"
    assert host._calls["disable_native"] == 1
    assert host._calls["overlay_enable"] == 1


def test_set_mode_native_from_eveo_disables_overlay_then_enable_native():
    host = _pure_host(preview_cfg={"mode": "eveo_labels"})
    host._preview_set_mode("native")
    assert host._calls["overlay_disable"] == 1
    assert host._calls["enable_native"] == 1


def test_set_mode_never_clears_saved_layouts_or_hotkeys():
    # The EVE-O foot-gun we fix: switching mode must never wipe saved data.
    host = _pure_host(preview_cfg={
        "mode": "native",
        "layouts": {"kirejen": [1, 2, 384, 216]},
        "hotkeys": {"focus": {"kirejen": [2, 120]}, "groups": [], "minimize_all": []},
    })
    host._preview_set_mode("off")
    host._preview_set_mode("native")
    host._preview_set_mode("eveo_labels")
    assert host.config["preview"]["layouts"] == {"kirejen": [1, 2, 384, 216]}
    assert host.config["preview"]["hotkeys"]["focus"] == {"kirejen": [2, 120]}


# ── native rows disabled when mode != native ─────────────────────────────────
def _state_of(widget):
    try:
        return str(widget.cget("state"))
    except tk.TclError:
        return str(widget.state())


def test_native_rows_disabled_when_mode_off():
    root, host = _ui_host(preview_cfg={"mode": "off"})
    try:
        frame = tk.Frame(root)
        host._build_preview_section(frame)
        assert host._preview_native_widgets, "native widgets registered"
        assert all(_state_of(w) in ("disabled", ("disabled",))
                   for w in host._preview_native_widgets)
    finally:
        root.destroy()


def test_native_rows_enabled_when_mode_native():
    root, host = _ui_host(preview_cfg={"mode": "native"})
    try:
        frame = tk.Frame(root)
        host._build_preview_section(frame)
        assert host._preview_native_widgets
        assert all(_state_of(w) not in ("disabled", ("disabled",))
                   for w in host._preview_native_widgets)
    finally:
        root.destroy()


# ── status text (blocked-by-EVE-O string) ────────────────────────────────────
def test_status_text_off():
    host = _pure_host(preview_cfg={"mode": "off"})
    assert "off" in host._preview_status_text().lower()


def test_status_text_blocked_by_eveo(monkeypatch):
    monkeypatch.setattr(fc_gui, "preview_running", lambda: True)
    host = _pure_host(preview_cfg={"mode": "native"})
    assert "eve-o" in host._preview_status_text().lower()


def test_status_text_uses_live_status_when_native_and_unblocked(monkeypatch):
    monkeypatch.setattr(fc_gui, "preview_running", lambda: False)
    host = _pure_host(preview_cfg={"mode": "native"})
    host._preview_status = "● 3 clients · 3 tiles"
    assert host._preview_status_text() == "● 3 clients · 3 tiles"


# ── Arrange in grid → snapped layouts written for live tiles ─────────────────
def test_arrange_grid_writes_layouts_for_live_non_login_tiles():
    host = _pure_host(preview_cfg={"mode": "native", "layouts": {}})
    ca = _cw(1, "Alpha")
    cb = _cw(2, "Bravo")
    login = _cw(3, "")            # login screen → excluded from arrange
    host._preview_clients = {1: ca, 2: cb, 3: login}
    # fake tiles so re-placement is a recorded no-op
    placed = {}

    class T:
        def __init__(self, key):
            self.key = key
        def place(self, x, y, w, body_h):
            placed[self.key] = (x, y, w, body_h)
    host._preview_tiles = {1: T("alpha"), 2: T("bravo"), 3: T("")}

    host._preview_arrange_grid()

    layouts = host.config["preview"]["layouts"]
    assert set(layouts) == {"alpha", "bravo"}      # login excluded
    bounds = (0, 0, 1920, 1080)
    expected = preview_layout.grid_arrange(
        2, 384, 216, bounds, origin=(10, 10), gap=8)
    assert layouts["alpha"] == [expected[0][0], expected[0][1], 384, 216]
    assert layouts["bravo"] == [expected[1][0], expected[1][1], 384, 216]
    # tiles were re-placed to the new grid positions
    assert placed["alpha"] == (expected[0][0], expected[0][1], 384, 216)
    assert placed["bravo"] == (expected[1][0], expected[1][1], 384, 216)
    assert host._saved["n"] >= 1


def test_arrange_grid_noop_when_no_live_clients():
    host = _pure_host(preview_cfg={"mode": "native", "layouts": {}})
    host._preview_arrange_grid()      # must not raise
    assert host.config["preview"]["layouts"] == {}


# ── (A10) hotkey binding builder — pure, no Tk/ctypes ────────────────────────
import hotkey_service


def _build(hk, live_keys=()):
    return fc_gui.FCToolGUI._preview_hotkey_bindings(hk, set(live_keys))


def test_hotkey_builder_focus_group_and_minall_get_distinct_ids_and_actions():
    hk = {
        "focus": {"kirejen": "F13", "alt one": "Control+F9"},
        "groups": [{"next": ["F14"], "prev": ["F15"], "order": []}],
        "minimize_all": ["Alt+M"],
    }
    bindings, actions, errors = _build(hk)
    assert errors == []
    # every binding id is distinct and appears in both maps
    assert set(bindings) == set(actions)
    assert len(bindings) == 5
    assert len(set(bindings)) == 5
    # every binding parses to a (mods, vk) pair
    for hk_id, mv in bindings.items():
        assert isinstance(mv, tuple) and len(mv) == 2
    action_set = set(actions.values())
    assert ("focus", "kirejen") in action_set
    assert ("focus", "alt one") in action_set
    assert ("cycle", 0, +1) in action_set        # next
    assert ("cycle", 0, -1) in action_set        # prev
    assert ("minall",) in action_set
    # the parsed values match hotkey_service.parse_hotkey
    inv = {v: k for k, v in actions.items()}
    assert bindings[inv[("focus", "kirejen")]] == hotkey_service.parse_hotkey("F13")
    assert bindings[inv[("cycle", 0, +1)]] == hotkey_service.parse_hotkey("F14")


def test_hotkey_builder_collects_invalid_strings_into_errors_not_raise():
    hk = {
        "focus": {"kirejen": "NotAKey", "alt": "F13"},
        "groups": [{"next": ["F14", "Win+F1"], "prev": [], "order": []}],
        "minimize_all": [""],
    }
    bindings, actions, errors = _build(hk)
    # valid ones still bound
    assert ("focus", "alt") in set(actions.values())
    assert ("cycle", 0, +1) in set(actions.values())
    # three invalid strings collected, nothing raised
    assert len(errors) == 3
    joined = " ".join(errors).lower()
    assert "notakey" in joined
    # invalid entries produced no binding
    assert ("focus", "kirejen") not in set(actions.values())


def test_hotkey_builder_multiple_next_keys_each_get_a_binding():
    hk = {"focus": {}, "minimize_all": [],
          "groups": [{"next": ["F14", "F16"], "prev": [], "order": []}]}
    bindings, actions, errors = _build(hk)
    assert errors == []
    # both next keys map to the same cycle action but distinct ids
    ids = [i for i, a in actions.items() if a == ("cycle", 0, +1)]
    assert len(ids) == 2 and len(set(ids)) == 2


def test_hotkey_builder_empty_config_is_empty():
    bindings, actions, errors = _build(
        {"focus": {}, "groups": [{"next": [], "prev": [], "order": []}],
         "minimize_all": []})
    assert bindings == {} and actions == {} and errors == []


def test_hotkey_builder_tolerates_missing_keys():
    # a sparse/legacy hotkeys dict must not KeyError
    bindings, actions, errors = _build({})
    assert bindings == {} and actions == {} and errors == []


# ── (A10) modal builds headless + preset button fills group 0 ────────────────
def _hotkey_ui_host(preview_cfg):
    root, host = _ui_host(preview_cfg=preview_cfg)
    for name in ("_open_preview_hotkeys_dialog", "_preview_hotkey_bindings",
                 "_preview_restart_hotkeys", "_preview_hotkey_preset"):
        attr = getattr(fc_gui.FCToolGUI, name, None)
        if attr is None:
            continue
        setattr(host, name,
                _bind_attr(name, attr, host) if callable(attr) else attr)
    return root, host


def test_hotkey_modal_builds_headless_and_closes():
    root, host = _hotkey_ui_host(preview_cfg={
        "mode": "native",
        "hotkeys": {"focus": {}, "groups": [{"next": [], "prev": [], "order": []}],
                    "minimize_all": []}})
    try:
        win = host._open_preview_hotkeys_dialog(_test_no_wait=True)
        assert win is not None
        win.destroy()
    finally:
        root.destroy()


def test_hotkey_preset_fills_group0_next_prev_without_touching_focus():
    root, host = _hotkey_ui_host(preview_cfg={
        "mode": "native",
        "hotkeys": {"focus": {"kirejen": "F5"},
                    "groups": [{"next": [], "prev": [], "order": []}],
                    "minimize_all": []}})
    try:
        host._preview_hotkey_preset()
        hk = host.config["preview"]["hotkeys"]
        assert hk["groups"][0]["next"] == ["F14"]
        assert hk["groups"][0]["prev"] == ["F13"]
        # focus keys untouched (EVE-O parity: preset only sets cycle group 0)
        assert hk["focus"] == {"kirejen": "F5"}
    finally:
        root.destroy()
