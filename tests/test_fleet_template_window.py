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


def test_rebalance_reuses_clamped_live_squad_no_duplicate(root, tmp_path, monkeypatch):
    import fleet_esi
    import fleet_composer
    import fleet_template_window as ftw
    from fleet_template_window import FleetTemplateWindow

    calls = {"create_wing": 0, "create_squad": 0, "move": []}
    monkeypatch.setattr(fleet_esi, "create_wing",
                        lambda *a, **k: (calls.__setitem__("create_wing", calls["create_wing"] + 1), 9001)[1])
    monkeypatch.setattr(fleet_esi, "create_squad",
                        lambda *a, **k: (calls.__setitem__("create_squad", calls["create_squad"] + 1), 9002)[1])
    monkeypatch.setattr(fleet_esi, "move_member",
                        lambda session, fleet_id, pid, *, wing_id, squad_id, role:
                            calls["move"].append((pid, wing_id, squad_id, role)))

    class _SyncThread:
        def __init__(self, target=None, daemon=None):
            self._t = target
        def start(self):
            self._t()
    monkeypatch.setattr(ftw.threading, "Thread", _SyncThread)

    win = FleetTemplateWindow(
        root, store=_store(tmp_path), fittings=_FakeFittings(), config={},
        esi_session_provider=lambda: object(),
        fleet_info_provider=lambda: {"fleet_id": 1, "is_boss": True},
        doctrine_provider=lambda: None,
        character_names_provider=lambda: [])
    win._fleet_id = 1
    # Live names are clamped to 10 chars; the action targets the full names.
    win._live_structure = {"wings": [{"id": 100, "name": "Logistics ",
                                      "squads": [{"id": 200, "name": "Guardians "}]}]}
    action = fleet_composer.RebalanceAction(
        pilot_id=42, pilot_name="Pilot", source_wing_name="Logistics Wing",
        target_wing_name="Logistics Wing", target_squad_name="Guardians Squad",
        create_squad=False)

    win._execute_rebalance(object(), action)
    win.destroy()

    assert calls["create_wing"] == 0
    assert calls["create_squad"] == 0          # reused existing squad — no duplicate
    assert calls["move"] == [(42, 100, 200, "squad_member")]


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
    t.wings.append(Wing("Cap Wing", None, [Squad("Dreads", None, [])]))
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
