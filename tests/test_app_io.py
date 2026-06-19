import json
import os

import pytest

from app_io import atomic_write_json


def test_round_trip(tmp_path):
    path = tmp_path / "a.json"
    data = {"name": "fleet", "count": 42, "nested": {"a": [1, 2, 3]}}
    atomic_write_json(str(path), data)
    with open(str(path), encoding="utf-8") as f:
        loaded = json.load(f)
    assert loaded == data


def test_overwrite(tmp_path):
    path = tmp_path / "a.json"
    first = {"version": 1}
    second = {"version": 2, "extra": "value"}
    atomic_write_json(str(path), first)
    atomic_write_json(str(path), second)
    with open(str(path), encoding="utf-8") as f:
        loaded = json.load(f)
    assert loaded == second


def test_no_leftover_tmp_on_success(tmp_path):
    path = tmp_path / "a.json"
    atomic_write_json(str(path), {"ok": True})
    assert not os.path.exists(str(path) + ".tmp")
    assert not (tmp_path / "a.json.tmp").exists()


def test_serialization_failure_preserves_existing(tmp_path):
    path = tmp_path / "a.json"
    original = {"keep": "me"}
    atomic_write_json(str(path), original)

    with pytest.raises((TypeError, Exception)):
        atomic_write_json(str(path), {"x": object()})

    # Original file must remain intact.
    assert os.path.exists(str(path))
    with open(str(path), encoding="utf-8") as f:
        loaded = json.load(f)
    assert loaded == original

    # No temp file should be left behind.
    assert not os.path.exists(str(path) + ".tmp")


def test_indent_respected(tmp_path):
    path = tmp_path / "a.json"
    atomic_write_json(str(path), {"key": "value"}, indent=4)
    with open(str(path), encoding="utf-8") as f:
        text = f.read()
    # A 4-space-indented quoted key should be present.
    assert '    "' in text
