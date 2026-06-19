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


def test_prime_batches_unknowns_in_one_call(tmp_path):
    """prime() resolves only the unknown ids and does so in a SINGLE ESI call;
    afterwards resolve_name() for those ids hits no further ESI."""
    calls = {"n": 0, "ids": []}

    class FakeESI:
        def resolve_names(self, ids):
            calls["n"] += 1
            calls["ids"].append(sorted(ids))
            return {
                88888: {"name": "Unknown Alpha", "category": "inventory_type"},
                77777: {"name": "Unknown Bravo", "category": "inventory_type"},
            }

    cache = str(tmp_path / "c.json")
    cat = TypeCatalog(bundled_path=_fixture(tmp_path), cache_path=cache, esi=FakeESI())

    # Mix of a known (bundled) id and two unknowns, with a duplicate unknown.
    cat.prime([12015, 88888, 77777, 88888])

    # Exactly ONE ESI call, carrying only the two distinct unknown ids.
    assert calls["n"] == 1
    assert calls["ids"] == [[77777, 88888]]

    # Primed names resolve with NO further ESI calls.
    assert cat.resolve_name(88888) == "Unknown Alpha"
    assert cat.resolve_name(77777) == "Unknown Bravo"
    assert cat.resolve_name(12015) == "Muninn"
    assert calls["n"] == 1

    # Primed entries persisted to cache: a fresh catalog (no esi) still resolves.
    cat2 = TypeCatalog(bundled_path=_fixture(tmp_path), cache_path=cache)
    assert cat2.resolve_name(88888) == "Unknown Alpha"
    assert cat2.resolve_name(77777) == "Unknown Bravo"


def test_prime_noops_when_all_known(tmp_path):
    """prime() makes no ESI call when every id is already known."""
    calls = {"n": 0}

    class FakeESI:
        def resolve_names(self, ids):
            calls["n"] += 1
            return {}

    cache = str(tmp_path / "c.json")
    cat = TypeCatalog(bundled_path=_fixture(tmp_path), cache_path=cache, esi=FakeESI())
    cat.prime([12015, 2048, 215])
    assert calls["n"] == 0


def test_prime_is_defensive_on_esi_failure(tmp_path):
    """prime() never raises if the ESI adapter blows up."""

    class BoomESI:
        def resolve_names(self, ids):
            raise RuntimeError("network down")

    cache = str(tmp_path / "c.json")
    cat = TypeCatalog(bundled_path=_fixture(tmp_path), cache_path=cache, esi=BoomESI())
    cat.prime([12015, 55555])  # must not raise
    assert cat.resolve_name(12015) == "Muninn"


def test_corrupt_cache_discarded_and_logged(tmp_path, caplog):
    """A corrupt cache file is discarded (not fatal) with a warning logged,
    and the catalog still resolves bundled ids."""
    cache = tmp_path / "c.json"
    cache.write_text("{not valid json", encoding="utf-8")

    with caplog.at_level("WARNING"):
        cat = TypeCatalog(bundled_path=_fixture(tmp_path), cache_path=str(cache))

    assert cat.resolve_name(12015) == "Muninn"  # bundled still works
    assert any("corrupt" in r.message.lower() for r in caplog.records)


def test_cache_write_roundtrips_via_atomic_writer(tmp_path):
    """A resolved ESI entry is persisted through atomic_write_json and read back
    by a fresh catalog with no ESI adapter (valid, parseable JSON on disk)."""

    class FakeESI:
        def resolve_names(self, ids):
            return {424242: {"name": "Atomic Module", "category": "inventory_type"}}

    cache = tmp_path / "c.json"
    cat = TypeCatalog(
        bundled_path=_fixture(tmp_path), cache_path=str(cache), esi=FakeESI()
    )
    assert cat.resolve_name(424242) == "Atomic Module"

    # On-disk cache is valid JSON containing the resolved entry.
    written = json.loads(cache.read_text(encoding="utf-8"))
    assert written["424242"]["n"] == "Atomic Module"
    # No leftover temp file from the atomic write.
    assert not (tmp_path / "c.json.tmp").exists()

    # Fresh catalog (no esi) still resolves from the persisted cache.
    cat2 = TypeCatalog(bundled_path=_fixture(tmp_path), cache_path=str(cache))
    assert cat2.resolve_name(424242) == "Atomic Module"
