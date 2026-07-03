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


def test_search_prefix_returns_matching_names(tmp_path):
    import json
    from type_catalog import TypeCatalog
    bundled = tmp_path / "fit_types.json"
    bundled.write_text(json.dumps({
        "19720": {"n": "Revelation", "c": 6, "g": 485, "s": None},
        "19724": {"n": "Moros", "c": 6, "g": 485, "s": None},
        "587":   {"n": "Rifter", "c": 6, "g": 25, "s": None},
        "17738": {"n": "Rev Fleet Nope", "c": 6, "g": 485, "s": None},
    }), encoding="utf-8")
    cat = TypeCatalog(bundled_path=str(bundled),
                      cache_path=str(tmp_path / "cache.json"), esi=None)
    got = cat.search_prefix("rev")
    assert "Revelation" in got
    assert "Rev Fleet Nope" in got
    assert "Moros" not in got
    assert "Rifter" not in got


def test_search_prefix_short_prefix_is_empty(tmp_path):
    import json
    from type_catalog import TypeCatalog
    bundled = tmp_path / "fit_types.json"
    bundled.write_text(json.dumps({"587": {"n": "Rifter", "c": 6, "g": 25, "s": None}}),
                       encoding="utf-8")
    cat = TypeCatalog(bundled_path=str(bundled),
                      cache_path=str(tmp_path / "c.json"), esi=None)
    assert cat.search_prefix("r") == []
    assert cat.search_prefix("") == []


def test_search_prefix_caps_at_limit(tmp_path):
    import json
    from type_catalog import TypeCatalog
    bundled = tmp_path / "fit_types.json"
    bundled.write_text(json.dumps(
        {str(1000 + i): {"n": f"Shipxx{i:02d}", "c": 6, "g": 25, "s": None}
         for i in range(30)}), encoding="utf-8")
    cat = TypeCatalog(bundled_path=str(bundled),
                      cache_path=str(tmp_path / "c.json"), esi=None)
    assert len(cat.search_prefix("shipxx", limit=5)) == 5


# ── ship_type_names / ship_group_names (overlay rule-value scoping) ───────────

def _ship_fixture(tmp_path):
    import json
    data = {
        # ships (category 6) across several groups, incl. a shuttle (group 31)
        # and a Force Recon Ship (group 833) that also lives in the name map.
        "11134": {"n": "Amarr Shuttle", "c": 6, "g": 31, "s": None},
        "672":   {"n": "Caldari Shuttle", "c": 6, "g": 31, "s": None},
        "11957": {"n": "Falcon", "c": 6, "g": 833, "s": None},
        "12015": {"n": "Muninn", "c": 6, "g": 358, "s": None},
        # a non-ship (module) that must NEVER show up in ship_* results
        "2048":  {"n": "Damage Control II", "c": 7, "g": 60, "s": "low"},
        # a ship whose group id is absent from SHIP_GROUP_NAMES: its TYPE name
        # is still offered, but it contributes no GROUP name.
        "99001": {"n": "Mystery Hull", "c": 6, "g": 999999, "s": None},
    }
    p = tmp_path / "fit_types.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return str(p)


def test_ship_type_names_ship_only_incl_shuttles(tmp_path):
    cat = TypeCatalog(bundled_path=_ship_fixture(tmp_path),
                      cache_path=str(tmp_path / "c.json"))
    all_ships = cat.ship_type_names()
    assert "Amarr Shuttle" in all_ships          # the reported-missing case
    assert "Falcon" in all_ships
    assert "Muninn" in all_ships
    assert "Mystery Hull" in all_ships           # unknown-group ship still listed
    assert "Damage Control II" not in all_ships  # module excluded
    # sorted output
    assert all_ships == sorted(all_ships)


def test_ship_type_names_prefix_is_substring(tmp_path):
    cat = TypeCatalog(bundled_path=_ship_fixture(tmp_path),
                      cache_path=str(tmp_path / "c.json"))
    shuttles = cat.ship_type_names("shuttle")     # substring, not just prefix
    assert set(shuttles) == {"Amarr Shuttle", "Caldari Shuttle"}
    assert "Falcon" not in shuttles


def test_ship_group_names_catalog_aligned_incl_shuttle(tmp_path):
    cat = TypeCatalog(bundled_path=_ship_fixture(tmp_path),
                      cache_path=str(tmp_path / "c.json"))
    groups = cat.ship_group_names()
    # groups present among the fixture's ships, resolved to authoritative names
    assert "Shuttle" in groups                    # group 31
    assert "Force Recon Ship" in groups           # group 833
    assert "Heavy Assault Cruiser" in groups      # group 358
    # a ship group id not in SHIP_GROUP_NAMES contributes no bare-number entry
    assert "999999" not in groups
    # no module/system contamination
    assert "Damage Control II" not in groups
    assert groups == sorted(groups)


def test_ship_group_names_fallback_when_no_ships(tmp_path):
    import json
    from type_catalog import SHIP_GROUP_NAMES
    # catalog with zero ships -> falls back to the full name map (never empty)
    bundled = tmp_path / "fit_types.json"
    bundled.write_text(json.dumps(
        {"2048": {"n": "Damage Control II", "c": 7, "g": 60, "s": "low"}}),
        encoding="utf-8")
    cat = TypeCatalog(bundled_path=str(bundled),
                      cache_path=str(tmp_path / "c.json"))
    groups = cat.ship_group_names()
    assert set(groups) == set(SHIP_GROUP_NAMES.values())
    assert "Shuttle" in groups
