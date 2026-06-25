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
