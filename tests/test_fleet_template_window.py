# tests/test_fleet_template_window.py
import os
import pytest

# Skip the whole module when there is no display (CI headless without Tk).
tk = pytest.importorskip("tkinter")


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError:
        pytest.skip("no display available")
    r.withdraw()
    yield r
    r.destroy()


def _menu_labels_for(win, item):
    """Capture the labels the right-click menu would build for `item` without
    popping it up (tk_popup is patched to a no-op)."""
    import tkinter as tk
    import types
    captured = []
    real_menu_cls = tk.Menu

    class _CapMenu(real_menu_cls):
        def add_command(self, *a, label=None, **k):
            if label is not None:
                captured.append(label)
            return super().add_command(*a, label=label, **k)

        def add_cascade(self, *a, label=None, **k):
            if label is not None:
                captured.append(label)
            return super().add_cascade(*a, label=label, **k)

        def tk_popup(self, *a, **k):
            pass

        def grab_release(self):
            pass

    tk.Menu = _CapMenu
    try:
        evt = types.SimpleNamespace(x=0, y=0, x_root=0, y_root=0)
        win._tree.identify_row = lambda _y: item
        win._on_tree_right_click(evt)
    finally:
        tk.Menu = real_menu_cls
    return captured


def _store(tmp_path):
    from fleet_template_store import FleetTemplateStore
    s = FleetTemplateStore(str(tmp_path / "fleet_templates.json"))
    s.load()
    s.add_template("Test Fleet")
    return s


class _FakeFittings:
    tags = ["DPS", "Links", "Logistics"]

    def list_doctrines(self):
        return []

    def get_doctrine(self, _id):
        return None

    def get_fit(self, _id):
        return None


def test_window_builds_and_defaults_to_template_mode(root, tmp_path):
    from fleet_template_window import FleetTemplateWindow
    win = FleetTemplateWindow(
        root,
        store=_store(tmp_path),
        fittings=_FakeFittings(),
        config={},
        esi_session_provider=lambda: None,
        fleet_info_provider=lambda: None,
        doctrine_provider=lambda: None,
        character_names_provider=lambda: ["Kyra Dawnfall"],
    )
    assert win.mode == "template"
    # Apply disabled in template mode, Rebalance disabled.
    assert str(win._apply_btn["state"]) == "disabled"
    win.destroy()


def test_mode_toggle_to_live_enables_apply(root, tmp_path):
    from fleet_template_window import FleetTemplateWindow
    win = FleetTemplateWindow(
        root, store=_store(tmp_path), fittings=_FakeFittings(), config={},
        esi_session_provider=lambda: None,
        fleet_info_provider=lambda: {"fleet_id": 1, "is_boss": True},
        doctrine_provider=lambda: None,
        character_names_provider=lambda: [],
    )
    win.set_mode("live")
    assert win.mode == "live"
    assert str(win._apply_btn["state"]) == "normal"
    win.destroy()


def test_live_tree_mirrors_real_fleet(root, tmp_path):
    from fleet_template_window import FleetTemplateWindow
    win = FleetTemplateWindow(
        root, store=_store(tmp_path), fittings=_FakeFittings(), config={},
        esi_session_provider=lambda: None,
        fleet_info_provider=lambda: {"fleet_id": 1, "is_boss": True},
        doctrine_provider=lambda: None,
        character_names_provider=lambda: [])
    win.mode = "live"
    win._fleet_id = 1
    win._live_structure = {"wings": [{"id": 1, "name": "Alpha", "squads": [
        {"id": 10, "name": "Squad 1"}]}]}
    win._live_members = [
        {"character_id": 5, "name": "Placed", "ship_type_id": 1,
         "ship_type_name": "Megathron", "ship_class": None, "role": "squad_member",
         "wing_id": 1, "squad_id": 10, "join_time": ""},
        {"character_id": 6, "name": "Floater", "ship_type_id": 2,
         "ship_type_name": "Rifter", "ship_class": None, "role": "squad_member",
         "wing_id": -1, "squad_id": -1, "join_time": ""},
    ]
    win._reload_live_tree()
    kinds = [m[0] for m in win._node_meta.values()]
    assert "livewing" in kinds and "livesquad" in kinds
    # The placed pilot is a livepilot node (NOT in the unassigned group);
    # the floater is unassigned.
    livepilots = [v[1] for v in win._node_meta.values() if v[0] == "livepilot"]
    unassigned = [v[1][0] for v in win._node_meta.values() if v[0] == "unassigned"]
    assert 5 in livepilots and 6 in unassigned and 5 not in unassigned
    win.destroy()


def _win(root, tmp_path, fittings=None, **providers):
    from fleet_template_window import FleetTemplateWindow
    defaults = dict(
        esi_session_provider=lambda: None,
        fleet_info_provider=lambda: None,
        doctrine_provider=lambda: None,
        character_names_provider=lambda: [],
    )
    defaults.update(providers)
    return FleetTemplateWindow(root, store=_store(tmp_path),
                               fittings=fittings or _FakeFittings(),
                               config={}, **defaults)


def test_ship_class_label_uses_get_group_name(root, tmp_path, monkeypatch):
    import ship_classes
    monkeypatch.setattr(ship_classes, "get_group_name",
                        lambda tid: "Heavy Assault Cruiser" if tid == 12345 else None)
    win = _win(root, tmp_path)
    try:
        assert win.ship_class_label(12345) == "Heavy Assault Cruiser"
        assert win.ship_class_label(0) is None
    finally:
        win.destroy()


def test_enrich_members_stamps_is_capital(root, tmp_path, monkeypatch):
    import ship_classes
    import fleet_template_window as ftw
    monkeypatch.setattr(ship_classes, "is_capital", lambda tid: tid == 19720)
    monkeypatch.setattr(ftw, "resolve_name", lambda cid, kind: "X", raising=False)
    # zkill_monitor.resolve_name is imported inside _enrich_members; patch there too.
    import zkill_monitor
    monkeypatch.setattr(zkill_monitor, "resolve_name", lambda cid, kind: "X")
    monkeypatch.setattr(ship_classes, "get_group_name", lambda tid: "Dreadnought")
    win = _win(root, tmp_path)
    try:
        out = win._enrich_members([
            {"character_id": 1, "ship_type_id": 19720, "role": "squad_member",
             "wing_id": None, "squad_id": None, "join_time": ""},
            {"character_id": 2, "ship_type_id": 587, "role": "squad_member",
             "wing_id": None, "squad_id": None, "join_time": ""},
        ])
        by_id = {m["character_id"]: m for m in out}
        assert by_id[1]["is_capital"] is True
        assert by_id[2]["is_capital"] is False
    finally:
        win.destroy()


def test_condition_types_include_capital_subcap_default():
    import fleet_template_window as ftw
    assert ftw.CONDITION_TYPES == [
        "ship_type", "ship_class", "character", "doctrine_tag",
        "capital", "subcap", "default"]


def test_update_rule_does_not_reload_all_rows(root, tmp_path, monkeypatch):
    win = _win(root, tmp_path)
    try:
        win._add_rule()   # one rule exists
        calls = {"n": 0}
        monkeypatch.setattr(win, "_reload_rules", lambda: calls.__setitem__("n", calls["n"] + 1))
        win._update_rule(0, cval="Revelation")
        assert calls["n"] == 0                      # in-place, no full rebuild
        t = win.current_template()
        assert t.rules[0].condition.value == "Revelation"
    finally:
        win.destroy()


def test_update_rule_capital_forces_empty_value(root, tmp_path):
    win = _win(root, tmp_path)
    try:
        win._add_rule()
        win._update_rule(0, cval="junk")
        win._update_rule(0, ctype="capital")
        t = win.current_template()
        assert t.rules[0].condition.type == "capital"
        assert t.rules[0].condition.value == ""       # value cleared for value-less type
    finally:
        win.destroy()


def test_condition_values_ship_class_includes_common(root, tmp_path):
    win = _win(root, tmp_path)
    try:
        vals = win._condition_values("ship_class")
        assert "Dreadnought" in vals and "Force Auxiliary" in vals
    finally:
        win.destroy()


def test_ship_type_suggestions_uses_catalog(root, tmp_path):
    class _CatFittings:
        tags = ["DPS"]
        class catalog:
            @staticmethod
            def search_prefix(prefix, limit=20):
                return ["Revelation", "Reaper"] if prefix.lower().startswith("re") else []
    win = _win(root, tmp_path, fittings=_CatFittings())
    try:
        assert win._ship_type_suggestions("rev") == ["Revelation", "Reaper"]
        assert win._ship_type_suggestions("z") == []   # short/no match
    finally:
        win.destroy()


def _win_with_structure(root, tmp_path):
    win = _win(root, tmp_path)
    t = win.current_template()
    from fleet_template_store import Wing, Squad
    t.wings = [Wing("Cap Wing", None, [Squad("Dreads", None, [])])]
    win.store.save()
    return win


def test_quick_add_capitals_rule(root, tmp_path):
    win = _win_with_structure(root, tmp_path)
    try:
        win._quick_add_rule("capital", "", "Cap Wing", "Dreads", "squad_member")
        t = win.current_template()
        r = t.rules[-1]
        assert r.condition.type == "capital"
        assert r.condition.value == ""
        assert (r.action.wing_name, r.action.squad_name, r.action.role) \
            == ("Cap Wing", "Dreads", "squad_member")
        assert r.broken is False
    finally:
        win.destroy()


def test_quick_add_class_rule_sets_value(root, tmp_path):
    win = _win_with_structure(root, tmp_path)
    try:
        win._quick_add_rule("ship_class", "Dreadnought", "Cap Wing", "Dreads",
                            "squad_commander")
        r = win.current_template().rules[-1]
        assert (r.condition.type, r.condition.value) == ("ship_class", "Dreadnought")
        assert r.action.role == "squad_commander"
    finally:
        win.destroy()


def test_quick_add_priorities_increment(root, tmp_path):
    win = _win_with_structure(root, tmp_path)
    try:
        win._quick_add_rule("capital", "", "Cap Wing", "Dreads", "squad_member")
        win._quick_add_rule("subcap", "", "Cap Wing", "Dreads", "squad_member")
        pri = [r.priority for r in win.current_template().rules]
        assert pri == sorted(pri) and len(set(pri)) == len(pri)   # unique, ordered
    finally:
        win.destroy()


def test_rules_tab_has_quick_add_buttons(root, tmp_path):
    # The buttons exist as children of the rules tab's top bar.
    win = _win(root, tmp_path)
    try:
        texts = []
        def walk(w):
            for c in w.winfo_children():
                try:
                    texts.append(str(c.cget("text")))
                except tk.TclError:
                    pass
                walk(c)
        walk(win._rules_tab)
        joined = " ".join(texts)
        assert "Capitals" in joined and "Subcaps" in joined
        assert "Class" in joined and "Tag" in joined
    finally:
        win.destroy()


# append — Phase B: write path enqueues onto the executor
import types


def _live_win(root, tmp_path, **providers):
    from fleet_template_window import FleetTemplateWindow
    base = dict(
        store=_store(tmp_path), fittings=_FakeFittings(), config={},
        esi_session_provider=lambda: object(),
        fleet_info_provider=lambda: {"fleet_id": 1, "is_boss": True},
        doctrine_provider=lambda: None, character_names_provider=lambda: [])
    base.update(providers)
    win = FleetTemplateWindow(root, **base)
    win._fleet_id = 1
    return win


class _SyncThread:
    """Synchronous stand-in for threading.Thread (runs target() on .start()).

    Copied from the Apply-path scaffold in the (now-deleted)
    test_rebalance_reuses_clamped_live_squad_no_duplicate. Monkeypatch
    fleet_template_window.threading.Thread with this for ANY test that drives the
    Apply path (which preps structure on a worker thread); pump root.update() so
    the _post()-marshaled _enqueue callback runs on the Tk thread. The DRAG path
    needs none of this — it enqueues synchronously (test below)."""
    def __init__(self, target=None, daemon=None):
        self._t = target

    def start(self):
        if self._t is not None:
            self._t()


def test_drag_drop_enqueues_moves_not_sleeps(root, tmp_path):
    # DRAG path is fully synchronous: no thread, no _post. Stubbing submit and
    # calling _live_drop_pilots directly captures the job with no pumping.
    win = _live_win(root, tmp_path)
    win.mode = "live"
    win._live_structure = {"wings": [{"id": 100, "name": "Alpha",
                                      "squads": [{"id": 200, "name": "S1"}]}]}
    win._live_members = [{"character_id": 42, "name": "Zed",
                          "role": "squad_commander", "wing_id": 100,
                          "squad_id": 999}]
    submitted = []
    win._ensure_executor()
    win._executor.submit = lambda job: submitted.append(job)
    win._live_drop_pilots([42], wing_id=100, squad_id=200)
    assert len(submitted) == 1
    job = submitted[0]
    assert job.pilot_id == 42
    assert job.wing_id == 100 and job.squad_id == 200
    assert job.role == "squad_commander"     # current role preserved, not flattened
    assert job.source == "drag"
    win.destroy()


def test_no_hard_sleep_or_cooldown_attr(root, tmp_path):
    # Scoped to the refactored write path only. A whole-file grep would still
    # hit the legacy rebalancer methods, which are deleted in Task 7 — this
    # Task-5 commit must be green while they still exist. The whole-file audit
    # lives in Task 7 / Task 8.
    import inspect
    from fleet_template_window import FleetTemplateWindow
    src = inspect.getsource(FleetTemplateWindow._execute_moves)
    src += inspect.getsource(FleetTemplateWindow.__init__)
    assert "time.sleep(0.5)" not in src
    assert "_last_write_monotonic" not in src


# append — Phase B: sync diff + pins
def test_sync_diff_clears_pin_on_ship_change_and_leave(root, tmp_path):
    win = _live_win(root, tmp_path)
    win.mode = "live"
    # Pilot 1 pinned in a Rifter (id 587); pilot 2 pinned; pilot 3 present.
    win._pins = {1: 587, 2: 111}
    win._prev_members = [
        {"character_id": 1, "ship_type_id": 587},
        {"character_id": 2, "ship_type_id": 111},
        {"character_id": 3, "ship_type_id": 222},
    ]
    new = [
        {"character_id": 1, "ship_type_id": 999},   # ship change → clear pin 1
        {"character_id": 3, "ship_type_id": 222},   # pilot 2 left → drop pin 2
    ]
    events = win._diff_members(win._prev_members, new)
    assert 1 in events["ship_changed"]
    assert 2 in events["left"]
    assert 3 in events["joined"] or 3 not in events["joined"]  # 3 stayed
    win._apply_member_diff(events)
    assert 1 not in win._pins        # cleared on ship change
    assert 2 not in win._pins        # dropped on leave
    win.destroy()


def test_clear_pins_button_label_and_action(root, tmp_path):
    win = _live_win(root, tmp_path)
    win._pins = {1: 10, 2: 20}
    win._refresh_pins_button()
    assert "2" in str(win._clear_pins_btn["text"])
    win._clear_pins()
    assert win._pins == {}
    win.destroy()


def test_sync_generation_discards_stale_worker_result(root, tmp_path):
    win = _live_win(root, tmp_path)
    win.mode = "live"
    win._sync_generation = 5
    # A worker that started at generation 4 must not overwrite state.
    applied = win._apply_sync_result(4, {"wings": [{"id": 9, "name": "X",
                                                    "squads": []}]}, [], None)
    assert applied is False
    assert win._live_structure.get("wings") == []   # unchanged
    win.destroy()


def test_active_cadence_when_recent_write(root, tmp_path):
    import time
    win = _live_win(root, tmp_path)
    t = win.current_template()
    win._auto_sort_on = False
    win._last_write_wall = time.time()      # a write just happened
    assert win._sync_delay_ms() == t.settings.sync_active_s * 1000
    win._last_write_wall = time.time() - 120  # long ago, auto-sort off
    assert win._sync_delay_ms() == t.settings.sync_idle_s * 1000
    win.destroy()


# append — Phase B: auto-sort toggle + tick
def test_auto_sort_button_label(root, tmp_path):
    win = _live_win(root, tmp_path)
    win.set_mode("live")
    assert "Auto-sort" in str(win._auto_sort_btn["text"])
    win._toggle_auto_sort()
    assert win._auto_sort_on is True
    assert "ON" in str(win._auto_sort_btn["text"])
    win.destroy()


def test_auto_sort_tick_enqueues_only_unpinned_needed(root, tmp_path):
    import fleet_composer
    win = _live_win(root, tmp_path)
    win.mode = "live"
    win._auto_sort_on = True
    win._ensure_executor()
    submitted = []
    win._executor.submit = lambda job: submitted.append(job)
    win._pins = {1: 587}          # pilot 1 pinned → skip
    win._live_structure = {"wings": [{"id": 100, "name": "Alpha",
                                      "squads": [{"id": 200, "name": "S1"}]}]}
    win._live_members = [
        {"character_id": 1, "name": "Pinned", "ship_type_id": 587,
         "is_capital": False, "ship_class": "Frigate", "wing_id": 100,
         "squad_id": 999, "role": "squad_member", "join_time": "1"},
        {"character_id": 2, "name": "Free", "ship_type_id": 588,
         "is_capital": False, "ship_class": "Frigate", "wing_id": 100,
         "squad_id": 999, "role": "squad_member", "join_time": "2"},
    ]
    # A template with a default rule → routes everyone to Alpha/S1.
    t = win.current_template()
    from fleet_template_store import (AssignmentRule, RuleCondition, RuleAction,
                                      Wing, Squad)
    t.wings = [Wing("Alpha", None, [Squad("S1", None, [])])]
    t.rules = [AssignmentRule(0, RuleCondition("default", ""),
                              RuleAction("squad_member", "Alpha", "S1"))]
    win._auto_sort_tick()
    ids = [j.pilot_id for j in submitted]
    assert 1 not in ids            # pinned skipped
    assert 2 in ids                # free pilot enqueued
    assert all(j.source == "autosort" for j in submitted)
    win.destroy()


def test_apply_clears_pins(root, tmp_path, monkeypatch):
    win = _live_win(root, tmp_path)
    win.mode = "live"
    win._pins = {1: 10, 2: 20}
    win._live_members = []
    win._live_structure = {"wings": []}
    # Stub the enqueue path so _apply just runs the clear-pins side effect.
    win._execute_moves = lambda *a, **k: None
    import fleet_template_window as ftw
    captured = {}
    monkeypatch.setattr(ftw, "_ApplyPreviewDialog",
                        lambda parent, rows, big_warning, on_confirm:
                        captured.setdefault("confirm", on_confirm))
    win._apply()
    captured["confirm"]()
    assert win._pins == {}
    win.destroy()


def test_settings_tab_exposes_v2_fields(root, tmp_path):
    win = _live_win(root, tmp_path)
    for key in ("sync_active_s", "sync_idle_s", "move_spacing_ms",
                "burst_cap", "settle_s", "bulk_apply_threshold"):
        assert key in win._settings_vars
    assert "rebalance_interval_s" not in win._settings_vars
    assert "move_cooldown_s" not in win._settings_vars
    win.destroy()


def test_resolve_names_provider_is_stored_and_called(root, tmp_path):
    from fleet_template_window import FleetTemplateWindow
    seen = {}

    def fake_resolver(names):
        seen["names"] = list(names)
        return {"kyra dawnfall": 95, "someone else": 96}

    win = FleetTemplateWindow(
        root, store=_store(tmp_path), fittings=_FakeFittings(), config={},
        esi_session_provider=lambda: None,
        fleet_info_provider=lambda: None,
        doctrine_provider=lambda: None,
        character_names_provider=lambda: [],
        resolve_names_provider=fake_resolver,
    )
    out = win._resolve_names_provider(["Kyra Dawnfall", "Someone Else"])
    assert out == {"kyra dawnfall": 95, "someone else": 96}
    assert seen["names"] == ["Kyra Dawnfall", "Someone Else"]
    win.destroy()


def test_fc_gui_resolve_names_lowercases_keys_and_survives_no_auth():
    import types
    import fc_gui

    # No esi_auth → provider returns {} (graceful no-auth).
    gui = types.SimpleNamespace(esi_auth=None)
    provider = fc_gui.FCToolGUI._resolve_names.__get__(gui, fc_gui.FCToolGUI)
    assert provider(["Kyra Dawnfall"]) == {}

    # With an auth whose resolve_names_to_ids returns proper-cased names,
    # the provider lowercases the keys for case-insensitive matching.
    class _Auth:
        is_authenticated = True
        def resolve_names_to_ids(self, names):
            return {"Kyra Dawnfall": 95}
    gui2 = types.SimpleNamespace(esi_auth=_Auth())
    provider2 = fc_gui.FCToolGUI._resolve_names.__get__(gui2, fc_gui.FCToolGUI)
    assert provider2(["Kyra Dawnfall"]) == {"kyra dawnfall": 95}
    assert provider2([]) == {}


def test_parse_pilot_lines_strips_dedupes_dropsblanks():
    from fleet_template_window import parse_pilot_lines
    text = "  Kyra Dawnfall \n\nkyra dawnfall\nBob McTest\n   \nBob McTest\n"
    assert parse_pilot_lines(text) == ["Kyra Dawnfall", "Bob McTest"]


def test_bulk_add_creates_pinned_named_slots_and_caches(root, tmp_path, monkeypatch):
    import types
    import fleet_template_window as ftw
    from fleet_template_window import FleetTemplateWindow

    store = _store(tmp_path)
    # give the template a wing+squad to add into
    t = store.templates[0]
    from fleet_template_store import Wing, Squad
    t.wings = [Wing(name="W1", max_size=None,
                    squads=[Squad(name="S1", max_size=None, slots=[])])]

    def fake_resolver(names):
        return {"kyra dawnfall": 95}   # only Kyra resolves; "Ghost Name" does not

    win = FleetTemplateWindow(
        root, store=store, fittings=_FakeFittings(), config={},
        esi_session_provider=lambda: None,
        fleet_info_provider=lambda: None,
        doctrine_provider=lambda: None,
        character_names_provider=lambda: [],
        resolve_names_provider=fake_resolver,
    )
    # Run the resolver worker synchronously and capture the unresolved-dialog call.
    monkeypatch.setattr(ftw.threading, "Thread", _SyncThread)
    captured = {}
    monkeypatch.setattr(win, "_show_unresolved_dialog",
                        lambda wi, si, unresolved: captured.setdefault("unres", unresolved))

    win._bulk_add_pilots_names(0, 0, ["Kyra Dawnfall", "Ghost Name"])
    root.update()   # pump the _post-marshaled result back onto the Tk thread

    squad = win.current_template().wings[0].squads[0]
    named = {s.character: s for s in squad.slots}
    assert "Kyra Dawnfall" in named
    assert named["Kyra Dawnfall"].character_id == 95
    assert named["Kyra Dawnfall"].role == "squad_member"
    # resolved pair cached for autocomplete
    assert win.store.cached_id("Kyra Dawnfall") == 95
    # unresolved surfaced for the Add anyway / Skip decision
    assert captured["unres"] == ["Ghost Name"]
    win.destroy()


def test_named_slot_without_id_renders_unvalidated(root, tmp_path):
    from fleet_template_window import FleetTemplateWindow
    from fleet_template_store import Slot
    win = _win(root, tmp_path)
    validated = Slot(character="Known", tag=None, role="squad_member",
                     character_id=42)
    unvalidated = Slot(character="Typed", tag=None, role="squad_member",
                       character_id=None)
    assert "⚠" not in win._slot_label(validated)
    assert "⚠" in win._slot_label(unvalidated)
    win.destroy()


def test_slot_editor_resolves_and_caches_on_ok(root, tmp_path, monkeypatch):
    import fleet_template_window as ftw
    from fleet_template_window import SlotEditor
    from fleet_template_store import Slot

    store = _store(tmp_path)
    slot = Slot(character=None, tag=None, role="squad_member")
    done = {"ok": False}
    monkeypatch.setattr(ftw.threading, "Thread", _SyncThread)

    ed = SlotEditor(
        root, slot, _FakeFittings(), ["Kyra Dawnfall"],
        resolve_names=lambda names: {"kyra dawnfall": 95},
        store=store, post=lambda fn, *a: fn(*a),
        on_ok=lambda: done.__setitem__("ok", True))
    ed._char.set("Kyra Dawnfall")
    ed._ok()
    root.update()
    assert slot.character == "Kyra Dawnfall"
    assert slot.character_id == 95
    assert store.cached_id("Kyra Dawnfall") == 95
    assert done["ok"] is True


def test_slot_editor_not_found_shows_warning_then_save_anyway(root, tmp_path, monkeypatch):
    import fleet_template_window as ftw
    from fleet_template_window import SlotEditor
    from fleet_template_store import Slot

    store = _store(tmp_path)
    slot = Slot(character=None, tag=None, role="squad_member")
    saved = {"ok": False}
    monkeypatch.setattr(ftw.threading, "Thread", _SyncThread)

    ed = SlotEditor(
        root, slot, _FakeFittings(), [],
        resolve_names=lambda names: {},          # not found
        store=store, post=lambda fn, *a: fn(*a),
        on_ok=lambda: saved.__setitem__("ok", True))
    ed._char.set("Ghost Name")
    ed._ok()
    root.update()
    # OK did NOT commit yet — a warning is shown and the editor stays open.
    assert saved["ok"] is False
    assert ed._warning_shown is True
    # Save anyway commits the slot unvalidated (character_id stays None), caches name.
    ed._save_anyway()
    assert slot.character == "Ghost Name"
    assert slot.character_id is None
    assert store.cached_id("Ghost Name") is None   # cached name, no id
    assert saved["ok"] is True


def test_add_my_characters_creates_slots(root, tmp_path, monkeypatch):
    import fleet_template_window as ftw
    from fleet_template_store import Wing, Squad
    store = _store(tmp_path)
    t = store.templates[0]
    t.wings = [Wing(name="W1", max_size=None,
                    squads=[Squad(name="S1", max_size=None, slots=[])])]
    win = _win(root, tmp_path if False else tmp_path,  # keep same store below
               character_names_provider=lambda: ["Alpha", "Bravo"],
               resolve_names_provider=lambda names: {"alpha": 1, "bravo": 2})
    # rebind to the store that already has W1/S1
    win.store = store
    win._current_template_id = t.id
    monkeypatch.setattr(ftw.threading, "Thread", _SyncThread)
    win._add_my_chars_names(0, 0, ["Alpha"])   # only Alpha checked
    root.update()
    squad = win.current_template().wings[0].squads[0]
    named = {s.character: s for s in squad.slots}
    assert "Alpha" in named and named["Alpha"].character_id == 1
    assert "Bravo" not in named
    win.destroy()


def test_import_live_as_template_builds_pins_and_selects(root, tmp_path, monkeypatch):
    import fleet_template_window as ftw
    from datetime import datetime
    win = _live_win(root, tmp_path) if "_live_win" in dir() else _win(
        root, tmp_path, fleet_info_provider=lambda: {"fleet_id": 1, "is_boss": True})
    win.mode = "live"
    win._fleet_id = 1
    win._live_structure = {"wings": [
        {"id": 1, "name": "Alpha", "squads": [{"id": 10, "name": "S1"}]}]}
    win._live_members = [
        {"character_id": 5, "name": "Placed", "ship_type_id": 1,
         "ship_type_name": "Megathron", "ship_class": None, "role": "squad_member",
         "wing_id": 1, "squad_id": 10, "join_time": ""},
    ]
    # Freeze time deterministically.
    monkeypatch.setattr(ftw, "datetime", type("D", (), {
        "now": staticmethod(lambda: datetime(2026, 7, 2, 21, 15))}))
    before = len(win.store.templates)
    win._import_live_as_template()
    assert len(win.store.templates) == before + 1
    t = win.current_template()
    assert t.name == "Import 2026-07-02 21:15"
    assert win.mode == "template"           # switched to template mode
    slot = t.wings[0].squads[0].slots[0]
    assert slot.character == "Placed" and slot.character_id == 5
    assert 5 in win._pins                    # every imported member pinned
    win.destroy()


def test_duplicate_template_button_creates_copy_and_selects(root, tmp_path):
    win = _win(root, tmp_path)
    try:
        src = win.current_template()
        before = len(win.store.templates)
        win._duplicate_template()
        assert len(win.store.templates) == before + 1
        cur = win.current_template()
        assert cur.id != src.id
        assert cur.name == "Test Fleet (copy)"
    finally:
        win.destroy()


def test_add_squad_slot_buttons_enable_by_selection(root, tmp_path):
    win = _win(root, tmp_path)
    try:
        t = win.current_template()
        t.wings = []                       # start empty for a clean count
        win.store.save()
        win._reload_tree()
        # Nothing selected: both disabled.
        win._refresh_add_buttons()
        assert str(win._add_squad_btn["state"]) == "disabled"
        assert str(win._add_slot_btn["state"]) == "disabled"
        # Add a wing, select it → + Squad enabled, + Slot still disabled.
        win._add_wing()
        wid = win._tree.get_children()[0]
        win._tree.selection_set(wid)
        win._refresh_add_buttons()
        assert str(win._add_squad_btn["state"]) == "normal"
        assert str(win._add_slot_btn["state"]) == "disabled"
        # Add a squad under it, select the squad → + Slot enabled.
        win._add_squad_from_selection()
        # _add_squad rebuilds the tree, so re-fetch the wing node id.
        wid = win._tree.get_children()[0]
        sid = win._tree.get_children(wid)[0]
        win._tree.selection_set(sid)
        win._refresh_add_buttons()
        assert str(win._add_slot_btn["state"]) == "normal"
    finally:
        win.destroy()


def test_add_slot_from_selection_appends_slot(root, tmp_path):
    win = _win(root, tmp_path)   # seeded Wing 1 / Squad 1
    try:
        wid = win._tree.get_children()[0]
        sid = win._tree.get_children(wid)[0]
        win._tree.selection_set(sid)
        win._add_slot_from_selection()
        t = win.current_template()
        assert len(t.wings[0].squads[0].slots) == 1
    finally:
        win.destroy()


def test_wing_menu_has_no_set_max_size(root, tmp_path):
    # Build the wing context menu and assert 'Set max size' is absent (squads keep it).
    win = _win(root, tmp_path)
    try:
        wid = win._tree.get_children()[0]
        win._tree.selection_set(wid)
        labels = _menu_labels_for(win, wid)
        assert "Add Squad" in labels
        assert "Set max size" not in labels
        # Squad still has it.
        sid = win._tree.get_children(wid)[0]
        win._tree.selection_set(sid)
        slabels = _menu_labels_for(win, sid)
        assert "Set max size" in slabels
    finally:
        win.destroy()


def test_commit_inline_rename_wing_and_squad(root, tmp_path):
    win = _win(root, tmp_path)   # seeded Wing 1 / Squad 1
    try:
        wid = win._tree.get_children()[0]
        sid = win._tree.get_children(wid)[0]
        # Capture the model paths up front — the first commit reloads the tree and
        # regenerates the Tk item ids in _node_meta, so re-indexing sid afterwards
        # would KeyError. Paths (wi,) / (wi, si) are stable across reloads.
        wing_path = win._node_meta[wid][1]
        squad_path = win._node_meta[sid][1]
        win._commit_inline_rename("wing", wing_path, "Assault")
        win._commit_inline_rename("squad", squad_path, "DPS")
        t = win.current_template()
        assert t.wings[0].name == "Assault"
        assert t.wings[0].squads[0].name == "DPS"
    finally:
        win.destroy()


def test_commit_inline_rename_slot_sets_character(root, tmp_path):
    win = _win(root, tmp_path)
    try:
        # add a slot to Squad 1
        t = win.current_template()
        from fleet_template_store import Slot
        t.wings[0].squads[0].slots.append(Slot(None, None, "squad_member"))
        win._reload_tree()
        wid = win._tree.get_children()[0]
        sid = win._tree.get_children(wid)[0]
        lid = win._tree.get_children(sid)[0]
        win._commit_inline_rename("slot", win._node_meta[lid][1], "Kyra Dawnfall")
        assert t.wings[0].squads[0].slots[0].character == "Kyra Dawnfall"
    finally:
        win.destroy()


def test_commit_inline_rename_ignores_blank(root, tmp_path):
    win = _win(root, tmp_path)
    try:
        wid = win._tree.get_children()[0]
        win._commit_inline_rename("wing", win._node_meta[wid][1], "   ")
        assert win.current_template().wings[0].name == "Wing 1"   # unchanged
    finally:
        win.destroy()


def test_inline_rename_counter_text():
    from fleet_template_window import inline_rename_counter
    # wing/squad names clamp to 10 on ESI → show over/under.
    assert inline_rename_counter("wing", "Assault") == "7/10"
    assert inline_rename_counter("squad", "TooLongName") == "11/10 ⚠"
    # slots aren't ESI-clamped → no counter.
    assert inline_rename_counter("slot", "Kyra Dawnfall") == ""


def test_mode_banner_text_updates(root, tmp_path):
    win = _live_win(root, tmp_path)
    try:
        # Template mode banner is the dim sandbox line.
        win.set_mode("template")
        assert "TEMPLATE" in str(win._banner["text"])
        assert "sandbox" in str(win._banner["text"]).lower()
        # Live mode banner warns.
        win.set_mode("live")
        assert "LIVE" in str(win._banner["text"])
        assert "real fleet" in str(win._banner["text"]).lower()
    finally:
        win.destroy()


def test_banner_text_helper():
    from fleet_template_window import mode_banner_text
    assert "TEMPLATE" in mode_banner_text("template")
    assert "LIVE" in mode_banner_text("live")


def test_settings_spinbox_saves_on_focusout(root, tmp_path):
    win = _live_win(root, tmp_path)
    try:
        var, lo, hi = win._settings_vars["burst_cap"]
        var.set(17)
        # Simulate the FocusOut/Return path (not an arrow click).
        win._on_settings_changed()
        assert win.current_template().settings.burst_cap == 17
        # The Spinbox must carry FocusOut + Return bindings (save-on-type).
        binds = win._settings_spinboxes["burst_cap"].bind()
        assert "<FocusOut>" in binds or "FocusOut" in " ".join(binds)
    finally:
        win.destroy()


def test_geometry_persisted_on_destroy_and_restored(root, tmp_path):
    from fleet_template_window import FleetTemplateWindow
    store = _store(tmp_path)
    win = FleetTemplateWindow(
        root, store=store, fittings=_FakeFittings(), config={},
        esi_session_provider=lambda: None, fleet_info_provider=lambda: None,
        doctrine_provider=lambda: None, character_names_provider=lambda: [])
    win.win.geometry("1001x611+30+30")
    win.win.update_idletasks()
    win.destroy()
    assert "geometry" in store.ui
    # Reopen: a fresh window restores from store.ui without raising.
    win2 = FleetTemplateWindow(
        root, store=store, fittings=_FakeFittings(), config={},
        esi_session_provider=lambda: None, fleet_info_provider=lambda: None,
        doctrine_provider=lambda: None, character_names_provider=lambda: [])
    assert store.ui["geometry"].startswith("1001x611")
    win2.destroy()


def _fake_compose_result(moves, unassigned=None, unassigned_reasons=None,
                         warnings=None):
    """ComposeResult-shaped fake. `moves` are Move-shaped (no ship name);
    `unassigned` are enriched member dicts; `unassigned_reasons` is
    {character_id: reason}."""
    import types
    return types.SimpleNamespace(
        executable=moves,
        unassigned=(unassigned or []),
        unassigned_reasons=(unassigned_reasons or {}),
        warnings=(warnings or []))


def test_build_apply_preview_rows_formats_moves():
    import types
    from fleet_template_window import build_apply_preview_rows
    # Move carries NO ship name — the ship comes from the live member dicts.
    mv = types.SimpleNamespace(pilot_id=42, pilot_name="Kyra",
                               target_wing_name="Cap", target_squad_name="Dreads",
                               target_role="squad_commander")
    members = [{"character_id": 42, "name": "Kyra",
                "ship_type_name": "Revelation"}]
    res = _fake_compose_result(
        [mv],
        unassigned=[{"character_id": 7, "name": "Bob"}],
        unassigned_reasons={7: "target full"},
        warnings=["heads up"])
    rows = build_apply_preview_rows(res, members)
    assert rows["moves"] == ["Kyra — Revelation → Cap/Dreads [squad_commander]"]
    assert rows["unassigned"] == ["Bob — target full"]   # joined by character_id
    assert rows["warnings"] == ["heads up"]


def test_build_apply_preview_rows_missing_ship_stays_well_formed():
    import types
    from fleet_template_window import build_apply_preview_rows
    # Pilot not in members (or no ship) -> ship segment omitted, no doubled ' — '.
    mv = types.SimpleNamespace(pilot_id=99, pilot_name="Ghost",
                               target_wing_name="A", target_squad_name="S",
                               target_role="squad_member")
    rows = build_apply_preview_rows(_fake_compose_result([mv]), members=[])
    assert rows["moves"] == ["Ghost → A/S [squad_member]"]
    assert " —  → " not in rows["moves"][0]     # no empty-ship artifact


def test_build_apply_preview_rows_unassigned_reason_fallback():
    from fleet_template_window import build_apply_preview_rows
    # One id has a reason, one has none -> the second falls back cleanly.
    res = _fake_compose_result(
        [],
        unassigned=[{"character_id": 1, "name": "Has"},
                    {"character_id": 2, "name": "None"}],
        unassigned_reasons={1: "target full"})
    rows = build_apply_preview_rows(res, members=[])
    assert rows["unassigned"] == ["Has — target full", "None — no matching rule"]


def test_apply_opens_preview_and_confirm_executes(root, tmp_path, monkeypatch):
    import fleet_template_window as ftw
    win = _live_win(root, tmp_path)
    try:
        win.mode = "live"
        win._live_members = [{"character_id": 1, "name": "Kyra",
                              "ship_type_name": "Rifter"}]
        win._live_structure = {"wings": []}
        executed = {"n": 0}
        win._execute_moves = lambda *a, **k: executed.__setitem__("n", executed["n"] + 1)
        import types
        mv = types.SimpleNamespace(pilot_id=1, pilot_name="Kyra",
                                   target_wing_name="A", target_squad_name="S",
                                   target_role="squad_member")
        monkeypatch.setattr(win, "_compose_preview",
                            lambda: _fake_compose_result([mv]))
        # Auto-confirm the dialog by capturing it and invoking its confirm.
        captured = {}
        monkeypatch.setattr(ftw, "_ApplyPreviewDialog",
                            lambda parent, rows, big_warning, on_confirm:
                            captured.setdefault("confirm", on_confirm))
        win._apply()
        assert "confirm" in captured
        captured["confirm"]()
        assert executed["n"] == 1
        assert win._pins == {}      # Apply clears pins on confirm
    finally:
        win.destroy()


def test_apply_big_warning_line_over_threshold(root, tmp_path, monkeypatch):
    import fleet_template_window as ftw
    win = _live_win(root, tmp_path)
    try:
        win.mode = "live"
        win._live_members = []
        win._live_structure = {"wings": []}
        win._execute_moves = lambda *a, **k: None
        t = win.current_template()
        t.settings.bulk_apply_threshold = 1
        import types
        moves = [types.SimpleNamespace(pilot_id=i, pilot_name=f"P{i}",
                                       target_wing_name="A", target_squad_name="S",
                                       target_role="squad_member")
                 for i in range(3)]
        monkeypatch.setattr(win, "_compose_preview",
                            lambda: _fake_compose_result(moves))
        captured = {}
        monkeypatch.setattr(ftw, "_ApplyPreviewDialog",
                            lambda parent, rows, big_warning, on_confirm:
                            captured.update(big=big_warning))
        win._apply()
        assert captured["big"]        # non-empty warning string when 3 > 1
    finally:
        win.destroy()
