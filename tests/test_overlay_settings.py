import pytest
tk = pytest.importorskip("tkinter")
import inspect
import types

import fc_gui
from overlay_rules import CharState
from eveo_tracker import Thumb


class FakeOverlay:
    def __init__(self):
        self.labels = None
        self.retops = 0
        self.visible = False
        self.destroyed = False
    def set_labels(self, items):
        # Mirror the real OverlayWindow contract: set_labels is the SINGLE owner
        # of the topmost re-assert — it calls retop() itself on a non-empty draw.
        self.labels = list(items)
        self.visible = bool([t for _, t in items if t])
        if self.visible:
            self.retop()
    def retop(self):
        self.retops += 1
    def set_font_size(self, s): self.font = s
    def set_color(self, c): self.color = c
    def set_anchor(self, a): self.anchor = a
    def destroy(self): self.destroyed = True


def _host(overlay_cfg=None, thumbs=None, states=None):
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display available")
    root.withdraw()
    host = types.SimpleNamespace()
    host.root = root
    host.config = {"overlay": overlay_cfg} if overlay_cfg is not None else {}
    host.esi_accounts = []
    host._overlay = FakeOverlay()
    host._overlay_states = states or {}
    host._overlay_thumbs_fn = lambda: (thumbs or [])
    host._overlay_after_id = None
    host._overlay_poller = None
    host._overlay_state_ts = {}
    host._overlay_status_label = None
    host._save_config = lambda: None
    # Bind methods AND the class-level constants: _overlay_cfg iterates
    # self._OVERLAY_DEFAULTS and (from Task 9 on) _overlay_state_for reads
    # self._OVERLAY_STALE_SECS — a bare SimpleNamespace has neither, so the
    # bind loop copies non-callables straight across.
    for name in ("_overlay_cfg", "_overlay_compose_items", "_overlay_tick",
                 "_overlay_status_text", "_overlay_state_for", "_overlay_rules",
                 "_overlay_teardown", "_overlay_stop_poller",
                 "_overlay_disable_session",
                 "_OVERLAY_DEFAULTS", "_OVERLAY_STALE_SECS"):
        attr = getattr(fc_gui.FCToolGUI, name, None)
        if attr is None:
            continue
        setattr(host, name,
                types.MethodType(attr, host) if callable(attr) else attr)
    return root, host


def test_overlay_cfg_defaults():
    root, host = _host(overlay_cfg=None)
    try:
        cfg = host._overlay_cfg()
        assert cfg["enabled"] is False
        assert cfg["font_size"] == 11
        assert cfg["color"] == "#00d4ff"
        assert cfg["anchor"] == "top-left"
        assert cfg["dpi_awareness"] == "auto"
        assert cfg["rules"] == [] or isinstance(cfg["rules"], list)
        assert cfg["overrides"] == {}
    finally:
        root.destroy()


def test_compose_items_uses_override():
    thumbs = [Thumb(1, "Alpha", (0, 0, 100, 100))]
    states = {"alpha": CharState(character_id=1, name="Alpha")}
    root, host = _host(
        overlay_cfg={"enabled": True, "rules": [], "overrides": {"alpha": "Cyno"}},
        thumbs=thumbs, states=states)
    try:
        items = host._overlay_compose_items()
        assert items == [((0, 0, 100, 100), "Cyno")]
    finally:
        root.destroy()


def test_compose_items_no_state_still_uses_override():
    # Phase 1: no poller => no CharState. A synthetic state from the thumb name
    # must still let overrides fire.
    thumbs = [Thumb(1, "Bravo", (1, 2, 3, 4))]
    root, host = _host(
        overlay_cfg={"enabled": True, "rules": [], "overrides": {"bravo": "X"}},
        thumbs=thumbs, states={})
    try:
        items = host._overlay_compose_items()
        assert items == [((1, 2, 3, 4), "X")]
    finally:
        root.destroy()


def test_status_text_variants():
    root, host = _host(overlay_cfg={"enabled": False})
    try:
        assert "off" in host._overlay_status_text(0, 0).lower()
        host.config["overlay"]["enabled"] = True
        assert "not detected" in host._overlay_status_text(0, 0).lower()
        assert "matched" in host._overlay_status_text(4, 3).lower()
    finally:
        root.destroy()


def test_tick_sets_labels_and_retops_when_enabled():
    thumbs = [Thumb(1, "Alpha", (0, 0, 100, 100))]
    root, host = _host(
        overlay_cfg={"enabled": True, "rules": [], "overrides": {"alpha": "Cyno"}},
        thumbs=thumbs)
    try:
        host._overlay_tick()
        assert host._overlay.labels == [((0, 0, 100, 100), "Cyno")]
        assert host._overlay.retops >= 1
    finally:
        # cancel the rescheduled after() so the interpreter can be destroyed
        if host._overlay_after_id:
            try: root.after_cancel(host._overlay_after_id)
            except Exception: pass
        root.destroy()


def _ui_host(overlay_cfg=None):
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display available")
    root.withdraw()
    host = types.SimpleNamespace()
    host.root = root
    host.config = {"overlay": overlay_cfg} if overlay_cfg is not None else {}
    host.esi_accounts = []
    host._overlay = FakeOverlay()
    host._overlay_states = {}
    host._overlay_state_ts = {}
    host._overlay_after_id = None
    host._overlay_poller = None
    host._overlay_poller_stop = None
    host._overlay_status_label = None
    host._overlay_thumbs_fn = lambda: []
    host._system_names = ["Jita", "Amarr"]
    saved = {"n": 0}
    host._save_config = lambda: saved.__setitem__("n", saved["n"] + 1)
    host._saved = saved
    host._show_tooltip = lambda e, t: None
    host._hide_tooltip = lambda: None
    host._preview_status_label = None
    host._preview_native_widgets = []
    host._preview_tiles = {}
    host._preview_clients = {}
    host._preview_disabled_session = False
    host._preview_status = ""
    host._preview_gamelog = None
    # Native-mode boot/teardown are recorded (never fired at a real window).
    calls = {"enable_native": 0, "disable_native": 0}
    host._calls = calls
    host._preview_enable_native = lambda: calls.__setitem__(
        "enable_native", calls["enable_native"] + 1)
    host._preview_disable_native = lambda: calls.__setitem__(
        "disable_native", calls["disable_native"] + 1)
    for name in ("_overlay_cfg", "_OVERLAY_DEFAULTS", "_build_preview_section",
                 "_preview_cfg", "_PREVIEW_DEFAULTS", "_preview_set_mode",
                 "_preview_status_text", "_preview_arrange_grid",
                 "_preview_arrange_by_fleet", "_preview_arrange_ordered",
                 "_preview_fleet_order_key",
                 "_preview_apply_native_state", "_preview_sync_native_widgets",
                 "_open_preview_hotkeys_dialog", "_preview_hotkey_preset",
                 "_preview_update_shown_summary", "_preview_all_known_chars",
                 "_preview_shown_chars", "_open_preview_previews_dialog",
                 "_preview_apply_shown_chars", "_preview_sync_gamelog_scope",
                 "_add_section",
                 "_overlay_apply_style",
                 "_overlay_status_text", "_overlay_compose_items",
                 "_overlay_rules", "_overlay_state_for", "_overlay_enable",
                 "_overlay_disable", "_overlay_teardown", "_overlay_ensure_window",
                 "_overlay_start_poller", "_overlay_stop_poller",
                 "_overlay_poll_loop", "_overlay_poll_plan",
                 "_overlay_build_state", "_OVERLAY_LOCSHIP_EVERY",
                 "_OVERLAY_ONLINE_EVERY",
                 "_overlay_disable_session", "_overlay_tick",
                 "_overlay_cycle_color", "_open_overlay_rules_dialog",
                 "_OVERLAY_COLOR_CYCLE", "_OVERLAY_ANCHORS",
                 "_OVERLAY_ANCHOR_TO_CFG", "_OVERLAY_CFG_TO_ANCHOR"):
        attr = getattr(fc_gui.FCToolGUI, name)
        raw = inspect.getattr_static(fc_gui.FCToolGUI, name, None)
        if isinstance(raw, staticmethod):
            setattr(host, name, attr)                 # no implicit host arg
        else:
            setattr(host, name,
                    types.MethodType(attr, host) if callable(attr) else attr)
    return root, host


def test_build_section_creates_mode_radio_and_status():
    root, host = _ui_host(overlay_cfg={"enabled": False})
    host.config["preview"] = {"mode": "off"}
    try:
        frame = tk.Frame(root)
        host._build_preview_section(frame)
        assert host._preview_mode_var.get() == "off"
        assert host._preview_status_label is not None
    finally:
        root.destroy()


def test_mode_radio_persists_and_enables():
    root, host = _ui_host(overlay_cfg={"enabled": False})
    host.config["preview"] = {"mode": "off"}
    try:
        frame = tk.Frame(root)
        host._build_preview_section(frame)
        host._preview_mode_var.set("eveo_labels")
        host._preview_set_mode("eveo_labels")
        assert host.config["preview"]["mode"] == "eveo_labels"
        assert host._saved["n"] >= 1
        # seed rules got created on first enable
        assert len(host.config["overlay"]["rules"]) >= 1
    finally:
        host._overlay_stop_poller()       # stop the daemon poller thread
        if host._overlay_after_id:
            try: root.after_cancel(host._overlay_after_id)
            except Exception: pass
        root.destroy()


def test_style_change_persists_live():
    root, host = _ui_host(overlay_cfg={"enabled": True, "rules": [], "overrides": {}})
    host.config["preview"] = {"mode": "eveo_labels"}
    try:
        frame = tk.Frame(root)
        host._build_preview_section(frame)
        host._overlay_size_var.set(16)
        host._overlay_apply_style()
        assert host.config["overlay"]["font_size"] == 16
        assert host._saved["n"] >= 1
    finally:
        if host._overlay_after_id:
            try: root.after_cancel(host._overlay_after_id)
            except Exception: pass
        root.destroy()


import time as _time
from overlay_rules import OverlayRule


def test_stale_state_dropped_to_name_only():
    thumbs = [Thumb(1, "Alpha", (0, 0, 100, 100))]
    fresh = CharState(character_id=1, name="Alpha", ship_group="Force Recon Ship")
    root, host = _host(
        overlay_cfg={"enabled": True,
                     "rules": [{"when": "ship_group", "value": "Force Recon Ship",
                                "label": "Cyno"}],
                     "overrides": {}},
        thumbs=thumbs, states={"alpha": fresh})
    host._overlay_state_ts = {"alpha": _time.monotonic()}
    # bind the staleness-aware accessor + cap constant
    host._OVERLAY_STALE_SECS = fc_gui.FCToolGUI._OVERLAY_STALE_SECS
    try:
        # fresh: rule matches → "Cyno"
        assert host._overlay_compose_items() == [((0, 0, 100, 100), "Cyno")]
        # now make it stale (older than the cap) → state ignored → no rule match
        host._overlay_state_ts["alpha"] = _time.monotonic() - (host._OVERLAY_STALE_SECS + 1)
        assert host._overlay_compose_items() == [((0, 0, 100, 100), "")]
    finally:
        root.destroy()


def test_teardown_safe_when_never_enabled():
    # App-close teardown must be safe/idempotent even if the overlay was never
    # enabled: no after-loop, no poller, overlay may be a bare fake. Mirrors the
    # guarded _on_close call path.
    root, host = _host(overlay_cfg={"enabled": False})
    try:
        host._overlay_after_id = None
        host._overlay_teardown()          # first call — no crash
        host._overlay_teardown()          # idempotent second call — no crash
        # overlay was withdrawn (set_labels([]) called on the fake) if present
        assert host._overlay.labels == [] or host._overlay.labels is None
    finally:
        root.destroy()


def test_teardown_cancels_after_and_stops_poller():
    # With an active after-loop and a poller placeholder, teardown cancels the
    # after id and clears the poller (the guarded _on_close path relies on this).
    root, host = _host(
        overlay_cfg={"enabled": True, "rules": [], "overrides": {"alpha": "Cyno"}})
    try:
        # schedule a real after so there is an id to cancel
        host._overlay_after_id = root.after(100000, lambda: None)
        host._overlay_teardown()
        assert host._overlay_after_id is None
        assert host._overlay_poller is None
    finally:
        if host._overlay_after_id:
            try: root.after_cancel(host._overlay_after_id)
            except Exception: pass
        root.destroy()
