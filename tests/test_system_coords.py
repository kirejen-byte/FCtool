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


def test_systems_within_range_filters_and_sorts(monkeypatch, tmp_path):
    LY = 9.46e15
    table = {
        "30000001": {"name": "Origin", "x": 0.0, "y": 0.0, "z": 0.0,
                     "region_id": 10000060, "security": -0.2},
        "30000002": {"name": "Near", "x": 1.0 * LY, "y": 0.0, "z": 0.0,
                     "region_id": 10000060, "security": -0.2},   # 1 ly, legal
        "30000003": {"name": "Far", "x": 8.0 * LY, "y": 0.0, "z": 0.0,
                     "region_id": 10000060, "security": -0.2},   # 8 ly, out of 5 ly
        "30000004": {"name": "NearHigh", "x": 2.0 * LY, "y": 0.0, "z": 0.0,
                     "region_id": 10000002, "security": 0.9},    # 2 ly but highsec
    }
    _load_fixture(monkeypatch, tmp_path, table)

    legal = system_coords.systems_within_range(30000001, 5.0, legal_only=True)
    assert [sid for sid, _ in legal] == [30000002]  # Far out of range, NearHigh illegal

    everything = system_coords.systems_within_range(30000001, 5.0, legal_only=False)
    assert [sid for sid, _ in everything] == [30000002, 30000004]  # sorted by distance
    assert everything[0][1] == pytest.approx(1.0, abs=1e-6)
    assert everything[1][1] == pytest.approx(2.0, abs=1e-6)


def test_get_kspace_name_to_id_filters_to_kspace(monkeypatch):
    import system_coords as scd
    monkeypatch.setattr(scd, "_loaded", True)
    monkeypatch.setattr(scd, "_name_of",
                        {30000142: "Jita", 30001234: "1DH-SX", 31000005: "J123456"})
    out = scd.get_kspace_name_to_id()
    assert out == {"Jita": 30000142, "1DH-SX": 30001234}   # J-space (31m) excluded
