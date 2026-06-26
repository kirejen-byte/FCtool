"""
Tests for system_cache region name<->id cache and resolution helpers.

All ESI network paths are mocked/monkeypatched — no real network access.
The on-disk REGION_CACHE_FILE constant is redirected to a tmp_path file in
every test that touches the cache so the real project cache is never read or
written.
"""

import json
import os

import pytest

import system_cache


def _write_cache(path, regions=None, region_ids=None, timestamp=123.0):
    """Write a regions_cache.json-shaped file at `path`."""
    data = {"timestamp": timestamp, "regions": regions or {}}
    if region_ids is not None:
        data["region_ids"] = region_ids
    with open(path, "w") as f:
        json.dump(data, f)


# ── corrupt-cache logging (silent-discard paths now log) ────────────────────


def test_load_region_cache_corrupt_returns_none_and_logs(
    tmp_path, monkeypatch, caplog
):
    """A corrupt region cache file -> None, and the failure is logged."""
    cache_file = tmp_path / "regions_cache.json"
    cache_file.write_text("{ not valid json", encoding="utf-8")
    monkeypatch.setattr(system_cache, "REGION_CACHE_FILE", str(cache_file))

    with caplog.at_level("ERROR"):
        assert system_cache.load_region_cache() is None
    assert any(
        "Failed to load region cache" in r.getMessage() for r in caplog.records
    )


def test_load_region_ids_cache_corrupt_returns_none_and_logs(
    tmp_path, monkeypatch, caplog
):
    """A corrupt region cache file -> None for region_ids, and logs."""
    cache_file = tmp_path / "regions_cache.json"
    cache_file.write_text("{ not valid json", encoding="utf-8")
    monkeypatch.setattr(system_cache, "REGION_CACHE_FILE", str(cache_file))

    with caplog.at_level("ERROR"):
        assert system_cache.load_region_ids_cache() is None
    assert any(
        "Failed to load region id cache" in r.getMessage()
        for r in caplog.records
    )


def test_load_cache_corrupt_returns_none_and_logs(
    tmp_path, monkeypatch, caplog
):
    """A corrupt system cache file -> None, and the failure is logged."""
    cache_file = tmp_path / "systems_cache.json"
    cache_file.write_text("{ not valid json", encoding="utf-8")
    monkeypatch.setattr(system_cache, "CACHE_FILE", str(cache_file))

    with caplog.at_level("ERROR"):
        assert system_cache.load_cache() is None
    assert any(
        "Failed to load system cache" in r.getMessage()
        for r in caplog.records
    )


def test_save_cache_uses_atomic_write(tmp_path, monkeypatch):
    """save_cache persists via atomic_write_json (round-trips through disk)."""
    cache_file = tmp_path / "systems_cache.json"
    monkeypatch.setattr(system_cache, "CACHE_FILE", str(cache_file))

    system_cache.save_cache({"Jita": 30000142})

    # No leftover temp file from the atomic write.
    assert not (tmp_path / "systems_cache.json.tmp").exists()
    with open(cache_file) as f:
        data = json.load(f)
    assert data["systems"] == {"Jita": 30000142}
    assert "timestamp" in data


# ── save / load round-trip + backward compat ────────────────────────────────


def test_save_region_cache_persists_region_ids(tmp_path, monkeypatch):
    cache_file = tmp_path / "regions_cache.json"
    monkeypatch.setattr(system_cache, "REGION_CACHE_FILE", str(cache_file))

    system_cache.save_region_cache(
        {"30000142": "The Forge"},
        {"The Forge": 10000002, "Domain": 10000043},
    )

    with open(cache_file) as f:
        data = json.load(f)
    # Existing shape preserved.
    assert data["regions"] == {"30000142": "The Forge"}
    # New additive key present.
    assert data["region_ids"] == {"The Forge": 10000002, "Domain": 10000043}


def test_save_region_cache_omits_region_ids_when_absent(tmp_path, monkeypatch):
    """Calling save_region_cache without region_ids keeps the legacy shape."""
    cache_file = tmp_path / "regions_cache.json"
    monkeypatch.setattr(system_cache, "REGION_CACHE_FILE", str(cache_file))

    system_cache.save_region_cache({"30000142": "The Forge"})

    with open(cache_file) as f:
        data = json.load(f)
    assert data["regions"] == {"30000142": "The Forge"}
    assert "region_ids" not in data


def test_load_region_cache_still_returns_system_map(tmp_path, monkeypatch):
    """Existing contract: load_region_cache returns the system->region map."""
    cache_file = tmp_path / "regions_cache.json"
    monkeypatch.setattr(system_cache, "REGION_CACHE_FILE", str(cache_file))
    _write_cache(
        cache_file,
        regions={"30000142": "The Forge"},
        region_ids={"The Forge": 10000002},
    )
    assert system_cache.load_region_cache() == {"30000142": "The Forge"}


def test_load_region_ids_cache_hit(tmp_path, monkeypatch):
    cache_file = tmp_path / "regions_cache.json"
    monkeypatch.setattr(system_cache, "REGION_CACHE_FILE", str(cache_file))
    _write_cache(
        cache_file,
        regions={"30000142": "The Forge"},
        region_ids={"The Forge": 10000002, "Domain": 10000043},
    )
    assert system_cache.load_region_ids_cache() == {
        "The Forge": 10000002, "Domain": 10000043
    }


def test_load_region_ids_cache_missing_key_returns_none(tmp_path, monkeypatch):
    """An older cache file without region_ids -> None (triggers rebuild)."""
    cache_file = tmp_path / "regions_cache.json"
    monkeypatch.setattr(system_cache, "REGION_CACHE_FILE", str(cache_file))
    _write_cache(cache_file, regions={"30000142": "The Forge"})  # no region_ids
    assert system_cache.load_region_ids_cache() is None


def test_load_region_ids_cache_no_file_returns_none(tmp_path, monkeypatch):
    cache_file = tmp_path / "does_not_exist.json"
    monkeypatch.setattr(system_cache, "REGION_CACHE_FILE", str(cache_file))
    assert system_cache.load_region_ids_cache() is None


def test_load_region_ids_cache_coerces_string_values(tmp_path, monkeypatch):
    """JSON may carry ids as strings; they are coerced to int."""
    cache_file = tmp_path / "regions_cache.json"
    monkeypatch.setattr(system_cache, "REGION_CACHE_FILE", str(cache_file))
    _write_cache(
        cache_file,
        regions={},
        region_ids={"The Forge": "10000002"},
    )
    out = system_cache.load_region_ids_cache()
    assert out == {"The Forge": 10000002}
    assert isinstance(out["The Forge"], int)


# ── get_region_name_to_id ───────────────────────────────────────────────────


def test_get_region_name_to_id_uses_cache(tmp_path, monkeypatch):
    """Local cache hit must NOT call ESI."""
    cache_file = tmp_path / "regions_cache.json"
    monkeypatch.setattr(system_cache, "REGION_CACHE_FILE", str(cache_file))
    _write_cache(
        cache_file,
        regions={"30000142": "The Forge"},
        region_ids={"The Forge": 10000002},
    )

    def boom():
        pytest.fail("_download_region_data must not be called on cache hit")

    monkeypatch.setattr(system_cache, "_download_region_data", boom)
    assert system_cache.get_region_name_to_id() == {"The Forge": 10000002}


def test_get_region_name_to_id_rebuilds_when_missing(tmp_path, monkeypatch):
    """No region_ids in cache -> rebuild from ESI and persist both maps."""
    cache_file = tmp_path / "regions_cache.json"
    monkeypatch.setattr(system_cache, "REGION_CACHE_FILE", str(cache_file))
    # Old-style cache: system map only, no region_ids.
    _write_cache(cache_file, regions={"30000142": "The Forge"})

    fake_sys_map = {"30000142": "The Forge", "30002187": "Domain"}
    fake_name_to_id = {"The Forge": 10000002, "Domain": 10000043}

    monkeypatch.setattr(
        system_cache, "_download_region_data",
        lambda: (fake_sys_map, fake_name_to_id),
    )
    out = system_cache.get_region_name_to_id()
    assert out == fake_name_to_id

    # Both maps were persisted together.
    with open(cache_file) as f:
        data = json.load(f)
    assert data["regions"] == fake_sys_map
    assert data["region_ids"] == fake_name_to_id


def test_get_region_name_to_id_empty_on_total_failure(tmp_path, monkeypatch):
    cache_file = tmp_path / "regions_cache.json"
    monkeypatch.setattr(system_cache, "REGION_CACHE_FILE", str(cache_file))
    monkeypatch.setattr(
        system_cache, "_download_region_data", lambda: ({}, {})
    )
    assert system_cache.get_region_name_to_id() == {}


# ── get_all_region_names ────────────────────────────────────────────────────


def test_get_all_region_names_sorted(tmp_path, monkeypatch):
    cache_file = tmp_path / "regions_cache.json"
    monkeypatch.setattr(system_cache, "REGION_CACHE_FILE", str(cache_file))
    _write_cache(
        cache_file,
        regions={},
        region_ids={"The Forge": 1, "Domain": 2, "Aridia": 3, "Catch": 4},
    )
    assert system_cache.get_all_region_names() == [
        "Aridia", "Catch", "Domain", "The Forge"
    ]


def test_get_all_region_names_deduped(tmp_path, monkeypatch):
    """Map keys are inherently unique; verify no duplicates leak through even
    if the same region name appears once (sanity on the sorted-keys contract)."""
    cache_file = tmp_path / "regions_cache.json"
    monkeypatch.setattr(system_cache, "REGION_CACHE_FILE", str(cache_file))
    _write_cache(
        cache_file, regions={},
        region_ids={"Delve": 10000060, "Querious": 10000050},
    )
    names = system_cache.get_all_region_names()
    assert names == sorted(set(names))
    assert names == ["Delve", "Querious"]


def test_get_all_region_names_empty(tmp_path, monkeypatch):
    cache_file = tmp_path / "regions_cache.json"
    monkeypatch.setattr(system_cache, "REGION_CACHE_FILE", str(cache_file))
    monkeypatch.setattr(
        system_cache, "_download_region_data", lambda: ({}, {})
    )
    assert system_cache.get_all_region_names() == []


# ── search_region ───────────────────────────────────────────────────────────


def test_search_region_local_cache_hit(tmp_path, monkeypatch):
    """Exact-name hit from the local cache; must not touch esi_auth/ESI."""
    cache_file = tmp_path / "regions_cache.json"
    monkeypatch.setattr(system_cache, "REGION_CACHE_FILE", str(cache_file))
    _write_cache(
        cache_file, regions={},
        region_ids={"The Forge": 10000002, "Domain": 10000043},
    )
    # If it tries ESI fallback, fail loudly.
    import esi_auth
    monkeypatch.setattr(
        esi_auth.ESIAuth, "resolve_region",
        lambda self, name: pytest.fail("should not fall back to ESI"),
    )
    assert system_cache.search_region("The Forge") == 10000002


def test_search_region_local_cache_case_insensitive(tmp_path, monkeypatch):
    cache_file = tmp_path / "regions_cache.json"
    monkeypatch.setattr(system_cache, "REGION_CACHE_FILE", str(cache_file))
    _write_cache(
        cache_file, regions={},
        region_ids={"The Forge": 10000002},
    )
    assert system_cache.search_region("the forge") == 10000002
    assert system_cache.search_region("  THE FORGE  ") == 10000002


def test_search_region_falls_back_to_esi(tmp_path, monkeypatch):
    """Name not in local cache -> esi_auth.resolve_region is consulted."""
    cache_file = tmp_path / "regions_cache.json"
    monkeypatch.setattr(system_cache, "REGION_CACHE_FILE", str(cache_file))
    # Cache has region_ids but not the requested region.
    _write_cache(
        cache_file, regions={},
        region_ids={"Domain": 10000043},
    )

    import esi_auth
    called = {}

    def fake_resolve_region(self, name):
        called["name"] = name
        return {"id": 10000002, "name": "The Forge"}

    monkeypatch.setattr(esi_auth.ESIAuth, "resolve_region", fake_resolve_region)

    assert system_cache.search_region("The Forge") == 10000002
    assert called["name"] == "The Forge"


def test_search_region_esi_fallback_when_no_cache(tmp_path, monkeypatch):
    """No region_ids cache at all -> rebuild attempt returns empty, then ESI
    fallback resolves. We force the rebuild to yield nothing so the local map
    is empty and the ESI path is exercised."""
    cache_file = tmp_path / "regions_cache.json"
    monkeypatch.setattr(system_cache, "REGION_CACHE_FILE", str(cache_file))
    # No cache file exists; make the rebuild yield nothing (e.g. ESI down for
    # the bulk traversal) so the local map is empty.
    monkeypatch.setattr(
        system_cache, "_download_region_data", lambda: ({}, {})
    )

    import esi_auth

    def fake_resolve_region(self, name):
        return {"id": 10000002, "name": "The Forge"}

    monkeypatch.setattr(esi_auth.ESIAuth, "resolve_region", fake_resolve_region)
    assert system_cache.search_region("The Forge") == 10000002


def test_search_region_miss_returns_none(tmp_path, monkeypatch):
    """Not in cache and ESI returns None -> None."""
    cache_file = tmp_path / "regions_cache.json"
    monkeypatch.setattr(system_cache, "REGION_CACHE_FILE", str(cache_file))
    _write_cache(cache_file, regions={}, region_ids={"Domain": 10000043})

    import esi_auth
    monkeypatch.setattr(
        esi_auth.ESIAuth, "resolve_region", lambda self, name: None
    )
    assert system_cache.search_region("Nonexistent Region") is None


def test_search_region_empty_input_returns_none(tmp_path, monkeypatch):
    cache_file = tmp_path / "regions_cache.json"
    monkeypatch.setattr(system_cache, "REGION_CACHE_FILE", str(cache_file))
    # Must not even consult the cache/ESI for empty input.
    monkeypatch.setattr(
        system_cache, "get_region_name_to_id",
        lambda: pytest.fail("should not be called for empty input"),
    )
    assert system_cache.search_region("") is None
    assert system_cache.search_region("   ") is None


def test_search_region_esi_exception_returns_none(tmp_path, monkeypatch):
    """If the ESI fallback raises, search_region swallows it and returns None."""
    cache_file = tmp_path / "regions_cache.json"
    monkeypatch.setattr(system_cache, "REGION_CACHE_FILE", str(cache_file))
    _write_cache(cache_file, regions={}, region_ids={"Domain": 10000043})

    import esi_auth

    def boom(self, name):
        raise RuntimeError("network exploded")

    monkeypatch.setattr(esi_auth.ESIAuth, "resolve_region", boom)
    assert system_cache.search_region("The Forge") is None


def test_get_system_names_uses_fresh_cache(monkeypatch):
    import system_cache as sc
    monkeypatch.setattr(sc, "load_cache", lambda: {"Jita": 30000142})
    assert sc.get_system_names() == {"Jita": 30000142}


def test_get_system_names_seeds_from_bundled_table_when_no_cache(monkeypatch):
    import system_cache as sc
    monkeypatch.setattr(sc, "load_cache", lambda: None)
    monkeypatch.setattr(sc, "_seed_from_coords", lambda: {"1DH-SX": 30001234})
    calls = {"refresh": 0, "download": 0}
    monkeypatch.setattr(sc, "_refresh_cache_async",
                        lambda: calls.__setitem__("refresh", calls["refresh"] + 1))
    monkeypatch.setattr(sc, "download_system_names",
                        lambda: (calls.__setitem__("download", calls["download"] + 1), {})[1])
    out = sc.get_system_names()
    assert out == {"1DH-SX": 30001234}     # served instantly from the bundled table
    assert calls["refresh"] == 1           # background ESI refresh kicked off
    assert calls["download"] == 0          # did NOT block on a synchronous download


def test_get_system_names_falls_back_to_download_without_bundle(monkeypatch):
    import system_cache as sc
    monkeypatch.setattr(sc, "load_cache", lambda: None)
    monkeypatch.setattr(sc, "_seed_from_coords", lambda: {})
    monkeypatch.setattr(sc, "download_system_names", lambda: {"Amarr": 30002187})
    monkeypatch.setattr(sc, "save_cache", lambda s: None)
    assert sc.get_system_names() == {"Amarr": 30002187}
