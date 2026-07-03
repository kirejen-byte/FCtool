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
    host._preview_gamelog = None
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
                 "_preview_arrange_grid", "_preview_arrange_by_fleet",
                 "_preview_arrange_ordered", "_preview_fleet_order_key",
                 "_preview_apply_native_state",
                 "_preview_sync_native_widgets",
                 "parse_eveo_config", "_preview_import_eveo",
                 "_preview_merge_eveo",
                 "_open_preview_hotkeys_dialog", "_preview_hotkey_preset",
                 "_preview_restart_hotkeys",
                 "_preview_update_shown_summary", "_preview_all_known_chars",
                 "_preview_shown_chars", "_open_preview_previews_dialog",
                 "_preview_apply_shown_chars", "_preview_sync_gamelog_scope",
                 "_open_preview_never_minimize_dialog",
                 "_preview_apply_never_minimize",
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
                 "_preview_arrange_by_fleet", "_preview_arrange_ordered",
                 "_preview_fleet_order_key",
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


# ── (C2) hide-rule checkboxes persist via _preview_apply_native_state ─────────
def test_hide_rule_checkboxes_persist_live():
    root, host = _ui_host(preview_cfg={"mode": "native"})
    try:
        frame = tk.Frame(root)
        host._build_preview_section(frame)
        host._preview_hide_active_var.set(True)
        host._preview_hide_login_var.set(True)
        host._preview_hide_lost_focus_var.set(True)
        host._preview_apply_native_state()
        pcfg = host.config["preview"]
        assert pcfg["hide_active"] is True
        assert pcfg["hide_login"] is True
        assert pcfg["hide_on_lost_focus"] is True
        # a saved layout is never touched by toggling a comfort option
        assert "layouts" in pcfg
    finally:
        root.destroy()


# ── (C2) Previews… modal: show-oriented checklist over the known-char union ──
def _previews_ui_host(preview_cfg, esi_names=(), live_names=()):
    root, host = _ui_host(preview_cfg=preview_cfg)
    host.esi_accounts = [SimpleNamespace(character_name=n) for n in esi_names]
    host._preview_clients = {
        i + 1: _cw(i + 1, nm) for i, nm in enumerate(live_names)}
    host._preview_gamelog = None
    for name in ("_open_preview_previews_dialog", "_preview_shown_chars",
                 "_preview_all_known_chars", "_preview_sync_gamelog_scope",
                 "_preview_apply_shown_chars", "_preview_update_shown_summary"):
        attr = getattr(fc_gui.FCToolGUI, name, None)
        if attr is None:
            continue
        setattr(host, name,
                _bind_attr(name, attr, host) if callable(attr) else attr)
    return root, host


def test_previews_modal_builds_headless_and_lists_known_chars():
    root, host = _previews_ui_host(
        preview_cfg={"mode": "native", "disabled_chars": ["bob"],
                     "layouts": {"ghost": [0, 0, 384, 216]}},
        esi_names=("Kirejen", "Bob"), live_names=("Alt Two",))
    try:
        win = host._open_preview_previews_dialog(_test_no_wait=True)
        assert win is not None
        # one checkbox var per known char (union, lowercased)
        assert set(host._preview_show_vars) == {
            "kirejen", "bob", "ghost", "alt two"}
        # show-oriented: disabled char is UNchecked, everyone else checked
        assert host._preview_show_vars["bob"].get() is False
        assert host._preview_show_vars["kirejen"].get() is True
        win.destroy()
    finally:
        root.destroy()


def test_previews_modal_apply_writes_disabled_chars_show_oriented():
    root, host = _previews_ui_host(
        preview_cfg={"mode": "native", "disabled_chars": []},
        esi_names=("Kirejen", "Bob"))
    try:
        win = host._open_preview_previews_dialog(_test_no_wait=True)
        # uncheck Kirejen (= hide/disable it); Bob stays shown
        host._preview_show_vars["kirejen"].set(False)
        host._preview_apply_shown_chars()
        assert host.config["preview"]["disabled_chars"] == ["kirejen"]
        win.destroy()
    finally:
        root.destroy()


# ── (C3) Never-minimize modal: exempt-oriented checklist over known chars ─────
def _never_ui_host(preview_cfg, esi_names=(), live_names=()):
    root, host = _ui_host(preview_cfg=preview_cfg)
    host.esi_accounts = [SimpleNamespace(character_name=n) for n in esi_names]
    host._preview_clients = {
        i + 1: _cw(i + 1, nm) for i, nm in enumerate(live_names)}
    for name in ("_open_preview_never_minimize_dialog",
                 "_preview_apply_never_minimize", "_preview_all_known_chars"):
        attr = getattr(fc_gui.FCToolGUI, name, None)
        if attr is None:
            continue
        setattr(host, name,
                _bind_attr(name, attr, host) if callable(attr) else attr)
    return root, host


def test_never_minimize_modal_builds_and_checks_current_members():
    root, host = _never_ui_host(
        preview_cfg={"mode": "native", "never_minimize": ["boss"],
                     "layouts": {"ghost": [0, 0, 384, 216]}},
        esi_names=("Kirejen", "Boss"), live_names=("Alt Two",))
    try:
        win = host._open_preview_never_minimize_dialog(_test_no_wait=True)
        assert set(host._preview_never_vars) == {
            "kirejen", "boss", "ghost", "alt two"}
        # exempt-oriented: only the saved member is checked
        assert host._preview_never_vars["boss"].get() is True
        assert host._preview_never_vars["kirejen"].get() is False
        win.destroy()
    finally:
        root.destroy()


def test_never_minimize_modal_apply_writes_checked_keys():
    root, host = _never_ui_host(
        preview_cfg={"mode": "native", "never_minimize": []},
        esi_names=("Kirejen", "Boss"))
    try:
        win = host._open_preview_never_minimize_dialog(_test_no_wait=True)
        host._preview_never_vars["boss"].set(True)
        host._preview_apply_never_minimize()
        assert host.config["preview"]["never_minimize"] == ["boss"]
        win.destroy()
    finally:
        root.destroy()


def test_minimize_inactive_toggle_persists_live():
    root, host = _ui_host(preview_cfg={"mode": "native",
                                       "minimize_inactive": False})
    try:
        frame = tk.Frame(root)
        host._build_preview_section(frame)
        host._preview_minimize_inactive_var.set(True)
        host._preview_apply_native_state()
        assert host.config["preview"]["minimize_inactive"] is True
    finally:
        root.destroy()


# ── (C5) EVE-O config import — pure parser + fill-only merge ──────────────────
# A captured real-shape EVE-O-Preview.json sample (Proopai fork). FlatLayout maps
# window titles → "x, y" point strings; ClientHotkey maps titles → hotkey strings;
# CycleGroup1ClientsOrder maps titles → an integer position (cycle order = sort by
# value). Prefixes "EVE - " / "EVE Frontier - " strip to the char key (lowercased).
_EVEO_SAMPLE = """{
  "FlatLayout": {
    "EVE - Kirejen": "100, 200",
    "EVE Frontier - Alt Two": "640, 200",
    "EVE": "5, 5",
    "EVE - Broken": "not-a-point"
  },
  "ClientHotkey": {
    "EVE - Kirejen": "Control+F1",
    "EVE Frontier - Alt Two": "F13",
    "EVE - Kirejen Bad": "Win+F2"
  },
  "CycleGroup1ClientsOrder": {
    "EVE - Kirejen": 1,
    "EVE Frontier - Alt Two": 0
  }
}"""


def _parse_eveo(text):
    return fc_gui.FCToolGUI.parse_eveo_config(text)


def test_parse_eveo_strips_prefixes_and_parses_points():
    parsed = _parse_eveo(_EVEO_SAMPLE)
    layouts = parsed["layouts"]
    assert layouts["kirejen"] == (100, 200)
    assert layouts["alt two"] == (640, 200)
    # bare "EVE" (login) has no char name → excluded; bad point → excluded
    assert "" not in layouts
    assert "broken" not in layouts


def test_parse_eveo_validates_hotkeys_and_reports_invalid():
    parsed = _parse_eveo(_EVEO_SAMPLE)
    fh = parsed["focus_hotkeys"]
    assert fh["kirejen"] == "Control+F1"
    assert fh["alt two"] == "F13"
    # Win-modified hotkey is rejected by parse_hotkey → skipped, not raised
    assert "kirejen bad" not in fh
    assert any("Win" in e or "kirejen bad" in e.lower() for e in parsed["errors"])


def test_parse_eveo_cycle_order_sorted_by_value():
    parsed = _parse_eveo(_EVEO_SAMPLE)
    # value 0 (alt two) comes before value 1 (kirejen)
    assert parsed["cycle_order"] == ["alt two", "kirejen"]


def test_parse_eveo_tolerates_empty_or_missing_sections():
    parsed = _parse_eveo("{}")
    assert parsed["layouts"] == {}
    assert parsed["focus_hotkeys"] == {}
    assert parsed["cycle_order"] == []
    assert parsed["errors"] == []


def test_parse_eveo_bad_json_returns_error_not_raise():
    parsed = _parse_eveo("{not json")
    assert parsed["layouts"] == {} and parsed["focus_hotkeys"] == {}
    assert parsed["cycle_order"] == []
    assert parsed["errors"]  # at least one error explaining the parse failure


def _import_host(preview_cfg):
    """Pure host with the import method + a fixed tile size for layout merge."""
    host = _pure_host(preview_cfg=preview_cfg)
    for name in ("parse_eveo_config", "_preview_import_eveo",
                 "_preview_merge_eveo"):
        attr = getattr(fc_gui.FCToolGUI, name, None)
        if attr is None:
            continue
        setattr(host, name, _bind_attr(name, attr, host))
    return host


def test_import_eveo_merge_is_fill_only_and_never_overwrites(monkeypatch):
    # existing FCTool data must survive; only NEW keys are filled.
    host = _import_host(preview_cfg={
        "mode": "native",
        "tile_w": 300, "tile_body_h": 180,
        "layouts": {"kirejen": [999, 999, 300, 180]},   # pre-existing → kept
        "hotkeys": {"focus": {"kirejen": "F9"},
                    "groups": [{"next": [], "prev": [], "order": ["kirejen"]}],
                    "minimize_all": []},
    })
    infos = []
    monkeypatch.setattr(fc_gui.filedialog, "askopenfilename",
                        lambda **k: "EVE-O-Preview.json")
    monkeypatch.setattr(fc_gui, "_read_text_file_for_import",
                        lambda p: _EVEO_SAMPLE, raising=False)
    monkeypatch.setattr(fc_gui.messagebox, "showinfo",
                        lambda *a, **k: infos.append((a, k)))
    monkeypatch.setattr(fc_gui.messagebox, "showerror",
                        lambda *a, **k: infos.append(("err", a, k)))

    host._preview_import_eveo()

    cfg = host.config["preview"]
    # existing kirejen layout + hotkey untouched
    assert cfg["layouts"]["kirejen"] == [999, 999, 300, 180]
    assert cfg["hotkeys"]["focus"]["kirejen"] == "F9"
    # new alt two filled in with the configured tile size
    assert cfg["layouts"]["alt two"] == [640, 200, 300, 180]
    assert cfg["hotkeys"]["focus"]["alt two"] == "F13"
    # cycle order fill-only: kirejen already present → keep; alt two appended
    assert cfg["hotkeys"]["groups"][0]["order"] == ["kirejen", "alt two"]
    assert host._saved["n"] >= 1
    assert infos  # a summary messagebox was shown


def test_import_eveo_cancel_dialog_is_noop(monkeypatch):
    host = _import_host(preview_cfg={"mode": "native", "layouts": {}})
    monkeypatch.setattr(fc_gui.filedialog, "askopenfilename", lambda **k: "")
    called = {"read": 0}
    monkeypatch.setattr(fc_gui, "_read_text_file_for_import",
                        lambda p: called.__setitem__("read", 1) or "{}",
                        raising=False)
    host._preview_import_eveo()   # cancelled dialog → no read, no save
    assert called["read"] == 0
    assert host.config["preview"]["layouts"] == {}
    assert host._saved["n"] == 0
