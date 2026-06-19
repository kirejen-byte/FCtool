import json
import logging
import os
from datetime import datetime, timedelta, timezone

import pytest

from standings_cache import StandingsCache, is_friendly


def _write_cache(tmp_path, fetched_at, friendly, hostile, source=42):
    path = tmp_path / "standings_cache.json"
    path.write_text(json.dumps({
        "fetched_at": fetched_at.isoformat(),
        "source_character_id": source,
        "friendly_ids": friendly,
        "hostile_ids": hostile,
    }))
    return str(path)


def test_load_returns_empty_when_missing(tmp_path):
    cache = StandingsCache(path=str(tmp_path / "missing.json"))
    cache.load()
    assert cache.friendly_ids == set()
    assert cache.hostile_ids == set()


def test_load_round_trip(tmp_path):
    path = _write_cache(tmp_path, datetime.now(timezone.utc), [1, 2], [3, 4])
    cache = StandingsCache(path=path)
    cache.load()
    assert cache.friendly_ids == {1, 2}
    assert cache.hostile_ids == {3, 4}


def test_save_then_load(tmp_path):
    path = str(tmp_path / "out.json")
    cache = StandingsCache(path=path)
    cache.friendly_ids = {10, 20}
    cache.hostile_ids = {30}
    cache.fetched_at = datetime(2026, 4, 28, tzinfo=timezone.utc)
    cache.source_character_id = 99
    cache.save()
    cache2 = StandingsCache(path=path)
    cache2.load()
    assert cache2.friendly_ids == {10, 20}
    assert cache2.hostile_ids == {30}
    assert cache2.source_character_id == 99


def test_load_corrupt_file_logs_and_resets(tmp_path, caplog):
    """A corrupt cache file is discarded (left empty) and the discard is logged."""
    path = tmp_path / "corrupt.json"
    path.write_text("{ this is not valid json ")
    cache = StandingsCache(path=str(path))
    cache.friendly_ids = {1}  # pre-existing state must be left untouched on failure
    cache.hostile_ids = {2}
    with caplog.at_level(logging.WARNING):
        cache.load()
    # load() returns early on corruption, leaving the in-memory state as-is.
    assert cache.friendly_ids == {1}
    assert cache.hostile_ids == {2}
    assert any("corrupt" in r.getMessage().lower() for r in caplog.records)


def test_save_round_trip_is_valid_json(tmp_path):
    """save() (now via atomic_write_json) writes a fully-valid, re-loadable file."""
    path = tmp_path / "rt.json"
    cache = StandingsCache(path=str(path))
    cache.friendly_ids = {5, 6}
    cache.hostile_ids = {7}
    cache.fetched_at = datetime(2026, 1, 2, tzinfo=timezone.utc)
    cache.source_character_id = 11
    cache.save()
    # No orphan temp file left behind by the atomic write.
    assert not (tmp_path / "rt.json.tmp").exists()
    on_disk = json.loads(path.read_text())
    assert on_disk["friendly_ids"] == [5, 6]
    assert on_disk["hostile_ids"] == [7]
    assert on_disk["source_character_id"] == 11


def test_is_stale_when_old(tmp_path):
    path = _write_cache(tmp_path, datetime.now(timezone.utc) - timedelta(hours=25), [], [])
    cache = StandingsCache(path=path)
    cache.load()
    assert cache.is_stale(max_age_hours=24)


def test_is_stale_when_fresh(tmp_path):
    path = _write_cache(tmp_path, datetime.now(timezone.utc) - timedelta(hours=1), [], [])
    cache = StandingsCache(path=path)
    cache.load()
    assert not cache.is_stale(max_age_hours=24)


def test_refresh_pulls_from_esi(monkeypatch, tmp_path):
    cache = StandingsCache(path=str(tmp_path / "x.json"))

    class FakeAuth:
        _character_id = 42
        def get_personal_contacts(self): return [
            {"contact_id": 1, "contact_type": "character", "standing": 5.0},
            {"contact_id": 99, "contact_type": "character", "standing": -10.0},
        ]
        def get_corp_contacts(self): return [
            {"contact_id": 100, "contact_type": "corporation", "standing": 7.5},
        ]
        def get_alliance_contacts(self): return [
            {"contact_id": 200, "contact_type": "alliance", "standing": 0.0},  # neutral, dropped
            {"contact_id": 201, "contact_type": "alliance", "standing": -5.0},
        ]

    cache.refresh(FakeAuth())
    assert cache.friendly_ids == {1, 100}
    assert cache.hostile_ids == {99, 201}
    assert cache.source_character_id == 42


def test_is_friendly_uses_own_chars():
    assert is_friendly(123, None, None, friendly_ids=set(), own_character_ids={123}) is True


def test_is_friendly_alliance_match():
    assert is_friendly(7, 8, 9, friendly_ids={9}, own_character_ids=set()) is True


def test_is_friendly_corp_match():
    assert is_friendly(7, 8, 9, friendly_ids={8}, own_character_ids=set()) is True


def test_is_friendly_unknown_returns_false():
    assert is_friendly(7, 8, 9, friendly_ids={1}, own_character_ids=set()) is False


def test_is_friendly_handles_none_ids():
    assert is_friendly(None, None, None, friendly_ids={1}, own_character_ids=set()) is False


def test_refresh_handles_getter_failures(tmp_path):
    """If all three contact getters raise, refresh produces an empty cache without crashing."""
    cache = StandingsCache(path=str(tmp_path / "fail.json"))

    class FailingAuth:
        _character_id = 7
        def get_personal_contacts(self):
            raise OSError("network down")
        def get_corp_contacts(self):
            raise RuntimeError("API limited")
        def get_alliance_contacts(self):
            raise ValueError("bad json")

    cache.refresh(FailingAuth())
    assert cache.friendly_ids == set()
    assert cache.hostile_ids == set()
    assert cache.source_character_id == 7
    assert cache.fetched_at is not None


def test_refresh_adds_own_corp_and_alliance_as_friendly(tmp_path):
    """The user's own corp and alliance are auto-friendly because EVE's
    contact endpoints don't list 'yourself'."""
    cache = StandingsCache(path=str(tmp_path / "x.json"))

    class FakeAuth:
        character_id = 90143494
        _character_id = 90143494
        def esi_get(self, path):
            assert path == "/characters/90143494/"
            return {"corporation_id": 5555, "alliance_id": 1900696668, "name": "Securitas"}
        def get_personal_contacts(self): return []
        def get_corp_contacts(self): return []
        def get_alliance_contacts(self): return []

    cache.refresh(FakeAuth())
    assert 5555 in cache.friendly_ids       # own corp
    assert 1900696668 in cache.friendly_ids  # own alliance (The Initiative.)
    assert cache.source_character_id == 90143494


def test_refresh_handles_missing_alliance(tmp_path):
    """Some characters are not in any alliance -- only corp should be added."""
    cache = StandingsCache(path=str(tmp_path / "x.json"))

    class FakeAuth:
        character_id = 1
        def esi_get(self, path):
            return {"corporation_id": 100, "alliance_id": None, "name": "Lonewolf"}
        def get_personal_contacts(self): return []
        def get_corp_contacts(self): return []
        def get_alliance_contacts(self): return []

    cache.refresh(FakeAuth())
    assert 100 in cache.friendly_ids
    # No alliance_id was added (it was None)
    assert cache.friendly_ids == {100}


def test_refresh_handles_esi_get_failure(tmp_path):
    """If /characters/{id}/ fails, refresh still runs but auto-friendly is empty."""
    cache = StandingsCache(path=str(tmp_path / "x.json"))

    class FakeAuth:
        character_id = 1
        def esi_get(self, path):
            raise OSError("network down")
        def get_personal_contacts(self): return [
            {"contact_id": 42, "standing": 5.0},
        ]
        def get_corp_contacts(self): return []
        def get_alliance_contacts(self): return []

    cache.refresh(FakeAuth())
    assert cache.friendly_ids == {42}  # contact still added; auto-friendly skipped
