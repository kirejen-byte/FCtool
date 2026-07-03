import pytest
tk = pytest.importorskip("tkinter")
from tkinter import ttk
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
        assert cfg["color"] == "#ffffff"    # Fix 3: readable white default
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


def test_status_text_hidden_thumbnails_middle_state():
    # Fix 1: process running but 0 thumbnails => the "hidden" middle state,
    # distinct from both "not detected" and the thumbnail-count state.
    root, host = _host(overlay_cfg={"enabled": True})
    try:
        hidden = host._overlay_status_text(0, 0, preview_running=True).lower()
        assert "hidden" in hidden
        assert "running" in hidden
        assert "not detected" not in hidden
        # process running is irrelevant once thumbnails ARE visible
        shown = host._overlay_status_text(2, 1, preview_running=True).lower()
        assert "matched" in shown and "hidden" not in shown
        # no process, no thumbnails => plain not-detected
        assert "not detected" in host._overlay_status_text(
            0, 0, preview_running=False).lower()
    finally:
        root.destroy()


def test_tick_picks_hidden_state_from_preview_running_fn():
    # Fix 1: the controller tick probes _overlay_preview_running_fn when there
    # are 0 thumbnails and shows the "hidden" state on the live status label.
    root, host = _host(overlay_cfg={"enabled": True, "rules": [], "overrides": {}},
                       thumbs=[])
    try:
        host._overlay_preview_running_fn = lambda: True
        host._overlay_status_label = tk.Label(root)
        host._overlay_tick()
        assert "hidden" in host._overlay_status_label.cget("text").lower()
    finally:
        if host._overlay_after_id:
            try: root.after_cancel(host._overlay_after_id)
            except Exception: pass
        root.destroy()


# ── Fix 2: rule-value autocomplete scoped per `when` kind ────────────────────

def _suggestion_host():
    host = types.SimpleNamespace()
    host._system_names = ["Jita", "Amarr", "Hek"]
    from type_catalog import TypeCatalog
    host.type_catalog = TypeCatalog()   # real bundled catalog
    host._OVERLAY_VALUELESS_WHENS = fc_gui.FCToolGUI._OVERLAY_VALUELESS_WHENS
    host._overlay_rule_value_suggestions = types.MethodType(
        fc_gui.FCToolGUI._overlay_rule_value_suggestions, host)
    return host


def test_rule_value_suggestions_scoped_per_kind():
    host = _suggestion_host()
    groups = host._overlay_rule_value_suggestions("ship_group")
    types_ = host._overlay_rule_value_suggestions("ship_type")
    systems = host._overlay_rule_value_suggestions("system")

    # ship_group -> group names incl. Shuttle; NO system names
    assert "Shuttle" in groups
    assert "Force Recon Ship" in groups
    assert "Jita" not in groups            # system must not leak into groups
    assert "Amarr Shuttle" not in groups   # a TYPE name must not appear here

    # ship_type -> type names incl. a shuttle (the reported-missing case)
    assert "Amarr Shuttle" in types_
    assert "Jita" not in types_
    assert "Shuttle" not in types_         # a GROUP name is not a type name

    # system -> system names only
    assert systems == ["Jita", "Amarr", "Hek"]

    # valueless kinds -> no suggestions
    for kind in ("docked", "offline", "capital", "subcap"):
        assert host._overlay_rule_value_suggestions(kind) == []


def test_rule_value_suggestions_degrade_without_catalog():
    host = types.SimpleNamespace()
    host._system_names = ["Jita"]
    host.type_catalog = None
    fn = types.MethodType(fc_gui.FCToolGUI._overlay_rule_value_suggestions, host)
    assert fn("ship_group") == []
    assert fn("ship_type") == []
    assert fn("system") == ["Jita"]


def test_rules_dialog_value_field_scopes_and_disables():
    # End-to-end on the modal: after switching a rule's `when` to a valueless
    # kind the value entry is cleared+disabled; switching to ship_group offers
    # group names; ship_type offers ship type names.
    root, host = _ui_host(overlay_cfg={"enabled": True, "rules": [], "overrides": {}})
    try:
        from type_catalog import TypeCatalog
        host.type_catalog = TypeCatalog()
        host._overlay_rule_value_suggestions = types.MethodType(
            fc_gui.FCToolGUI._overlay_rule_value_suggestions, host)
        host._OVERLAY_VALUELESS_WHENS = fc_gui.FCToolGUI._OVERLAY_VALUELESS_WHENS
        # open the dialog in a non-blocking way: build it, then close before
        # wait_window would block by scheduling a destroy.
        host.config["overlay"]["rules"] = [
            {"when": "ship_group", "value": "Shuttle", "label": "shuttle!"}]
        # Drive the modal builder but avoid the terminal wait_window block by
        # patching it to a no-op via the root's after/return; simplest: call the
        # builder and immediately destroy any created Toplevel.
        created = {}
        orig_wait = host.root.wait_window
        host.root.wait_window = lambda w: created.__setitem__("win", w)
        host._open_overlay_rules_dialog()
        win = created.get("win")
        assert win is not None
        # find the AutocompleteEntry (value field) and its when combobox
        from autocomplete import AutocompleteEntry
        entries = [w for w in win.winfo_children()]
        # walk the rules_frame for the value entry + combobox
        acs, combos = [], []
        def _walk(w):
            for c in w.winfo_children():
                if isinstance(c, AutocompleteEntry):
                    acs.append(c)
                elif isinstance(c, ttk.Combobox):
                    combos.append(c)
                _walk(c)
        _walk(win)
        assert acs, "expected a value AutocompleteEntry in the dialog"
        ve = acs[0]
        # seeded ship_group -> completions include group names
        assert "Shuttle" in ve._completions
        # switch the row's when to 'capital' (valueless) -> disabled + cleared
        rule_combo = combos[0]
        rule_combo.set("capital")
        rule_combo.event_generate("<<ComboboxSelected>>")
        assert str(ve.cget("state")) == "disabled"
        # switch to ship_type -> re-enabled and completions are ship type names
        rule_combo.set("ship_type")
        rule_combo.event_generate("<<ComboboxSelected>>")
        assert str(ve.cget("state")) == "normal"
        assert "Amarr Shuttle" in ve._completions
        win.destroy()
    finally:
        host.root.wait_window = orig_wait
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
    for name in ("_overlay_cfg", "_OVERLAY_DEFAULTS", "_build_overlay_section",
                 "_overlay_toggle_changed", "_overlay_apply_style",
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
        setattr(host, name, types.MethodType(attr, host) if callable(attr) else attr)
    return root, host


def test_build_section_creates_toggle_and_status():
    root, host = _ui_host(overlay_cfg={"enabled": False})
    try:
        frame = tk.Frame(root)
        host._build_overlay_section(frame)
        assert host._overlay_enabled_var.get() is False
        assert host._overlay_status_label is not None
    finally:
        root.destroy()


def test_toggle_persists_and_enables():
    root, host = _ui_host(overlay_cfg={"enabled": False})
    try:
        frame = tk.Frame(root)
        host._build_overlay_section(frame)
        host._overlay_enabled_var.set(True)
        host._overlay_toggle_changed()
        assert host.config["overlay"]["enabled"] is True
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
    try:
        frame = tk.Frame(root)
        host._build_overlay_section(frame)
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
