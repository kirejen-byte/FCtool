import json

import pytest

import system_coords


def _load_fixture(monkeypatch, tmp_path, table):
    """Point system_coords at a tiny tmp table and force a clean reload."""
    path = tmp_path / "system_coords.json"
    path.write_text(json.dumps(table), encoding="utf-8")
    monkeypatch.setattr(system_coords, "_data_path", lambda: str(path))
    # Reset module globals so _load() re-reads our fixture.
    system_coords._loaded = False
    for d in (system_coords._coords, system_coords._region_of,
              system_coords._security_of, system_coords._name_of,
              system_coords._id_of_name):
        d.clear()


FIXTURE = {
    "30000142": {"name": "Jita", "x": 1.0, "y": 0.0, "z": 0.0,
                 "region_id": 10000002, "security": 0.9459},
    "30002187": {"name": "Amarr", "x": 2.0, "y": 0.0, "z": 0.0,
                 "region_id": 10000043, "security": 0.9281},
}


def test_load_populates_tables(monkeypatch, tmp_path):
    _load_fixture(monkeypatch, tmp_path, FIXTURE)
    system_coords._load()
    assert system_coords._coords[30000142] == (1.0, 0.0, 0.0)
    assert system_coords._region_of[30000142] == 10000002
    assert system_coords._security_of[30000142] == pytest.approx(0.9459)
    assert system_coords._id_of_name["jita"] == 30000142


def test_load_missing_file_degrades_gracefully(monkeypatch):
    monkeypatch.setattr(system_coords, "_data_path", lambda: None)
    system_coords._loaded = False
    system_coords._coords.clear()
    system_coords._load()  # must not raise
    assert system_coords._coords == {}
    assert system_coords._loaded is True


def test_get_position_returns_esi_compatible_dict(monkeypatch, tmp_path):
    _load_fixture(monkeypatch, tmp_path, FIXTURE)
    pos = system_coords.get_position(30000142)
    assert pos == {"x": 1.0, "y": 0.0, "z": 0.0}
    assert system_coords.get_position(39999999) is None  # unknown id


def test_resolve_name_is_case_insensitive(monkeypatch, tmp_path):
    _load_fixture(monkeypatch, tmp_path, FIXTURE)
    assert system_coords.resolve_name("Jita") == 30000142
    assert system_coords.resolve_name("jita") == 30000142
    assert system_coords.resolve_name("AMARR") == 30002187
    assert system_coords.resolve_name("Nowhere") is None


LEGALITY_FIXTURE = {
    "30000142": {"name": "Jita", "x": 0.0, "y": 0.0, "z": 0.0,
                 "region_id": 10000002, "security": 0.9459},     # highsec -> illegal
    "30000789": {"name": "Nullhole", "x": 0.0, "y": 0.0, "z": 0.0,
                 "region_id": 10000060, "security": -0.21},      # nullsec -> legal
    "30001161": {"name": "Lowhole", "x": 0.0, "y": 0.0, "z": 0.0,
                 "region_id": 10000040, "security": 0.31},       # lowsec -> legal
    "30000021": {"name": "Kuharah", "x": 0.0, "y": 0.0, "z": 0.0,
                 "region_id": 10000070, "security": -0.05},      # Pochven -> illegal
    "30100000": {"name": "Zarzakh", "x": 0.0, "y": 0.0, "z": 0.0,
                 "region_id": 10001000, "security": -1.0},       # Zarzakh -> illegal
    "30000444": {"name": "Edgecase", "x": 0.0, "y": 0.0, "z": 0.0,
                 "region_id": 10000050, "security": 0.45},       # 0.45 == highsec -> illegal
}


def test_is_legal_jump_destination(monkeypatch, tmp_path):
    _load_fixture(monkeypatch, tmp_path, LEGALITY_FIXTURE)
    assert system_coords.is_legal_jump_destination(30000789) is True   # nullsec
    assert system_coords.is_legal_jump_destination(30001161) is True   # lowsec
    assert system_coords.is_legal_jump_destination(30000142) is False  # highsec
    assert system_coords.is_legal_jump_destination(30000021) is False  # Pochven region
    assert system_coords.is_legal_jump_destination(30100000) is False  # Zarzakh id
    assert system_coords.is_legal_jump_destination(30000444) is False  # 0.45 is highsec
    assert system_coords.is_legal_jump_destination(31000001) is False  # WH id range
    assert system_coords.is_legal_jump_destination(99999999) is False  # unknown
