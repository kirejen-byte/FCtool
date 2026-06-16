import json
from type_catalog import TypeCatalog


def _fixture(tmp_path):
    data = {
        "12015": {"n": "Muninn", "c": 6, "g": 540, "s": None},
        "2048":  {"n": "Damage Control II", "c": 7, "g": 60, "s": "low"},
        "12058": {"n": "Hobgoblin II", "c": 18, "g": 100, "s": None},
        "215":   {"n": "Antimatter Charge M", "c": 8, "g": 86, "s": None},
    }
    p = tmp_path / "fit_types.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return str(p)


def test_resolve_name_and_id(tmp_path):
    cat = TypeCatalog(bundled_path=_fixture(tmp_path), cache_path=str(tmp_path / "c.json"))
    assert cat.resolve_name(12015) == "Muninn"
    assert cat.resolve_id("damage control ii") == 2048      # case-insensitive
    assert cat.resolve_id("Damage Control II") == 2048


def test_category_and_slot(tmp_path):
    cat = TypeCatalog(bundled_path=_fixture(tmp_path), cache_path=str(tmp_path / "c.json"))
    assert cat.category_of(2048) == "module"
    assert cat.slot_of(2048) == "low"
    assert cat.category_of(12058) == "drone"
    assert cat.category_of(215) == "charge"
    assert cat.slot_of(12015) is None


def test_unknown_falls_back_to_esi_and_caches(tmp_path):
    calls = {"n": 0}

    class FakeESI:
        def resolve_names(self, ids):
            calls["n"] += 1
            return {99999: {"name": "New Module", "category": "inventory_type"}}

    cache = str(tmp_path / "c.json")
    cat = TypeCatalog(bundled_path=_fixture(tmp_path), cache_path=cache, esi=FakeESI())
    assert cat.resolve_name(99999) == "New Module"
    # second lookup served from in-memory/file cache, no extra ESI call
    cat2 = TypeCatalog(bundled_path=_fixture(tmp_path), cache_path=cache, esi=FakeESI())
    assert cat2.resolve_name(99999) == "New Module"
    assert calls["n"] == 1
