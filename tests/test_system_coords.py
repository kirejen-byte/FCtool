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
