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


def test_atomic_write_json_retries_transient_lock(tmp_path, monkeypatch):
    import os, json
    import app_io
    monkeypatch.setattr(app_io.time, "sleep", lambda *_a, **_k: None)  # no real sleeping
    path = str(tmp_path / "s.json")
    real_replace = os.replace
    n = {"c": 0}

    def flaky(src, dst):
        n["c"] += 1
        if n["c"] < 3:
            raise PermissionError(32, "locked by another process")
        return real_replace(src, dst)

    monkeypatch.setattr(app_io.os, "replace", flaky)
    app_io.atomic_write_json(path, {"k": 1})
    assert n["c"] == 3                              # succeeded on the 3rd try
    assert json.load(open(path)) == {"k": 1}
    assert not os.path.exists(path + ".tmp")


def test_atomic_write_json_raises_after_persistent_lock(tmp_path, monkeypatch):
    import os
    import pytest
    import app_io
    monkeypatch.setattr(app_io.time, "sleep", lambda *_a, **_k: None)
    path = str(tmp_path / "s.json")

    def always_locked(src, dst):
        raise PermissionError(32, "locked")

    monkeypatch.setattr(app_io.os, "replace", always_locked)
    with pytest.raises(PermissionError):
        app_io.atomic_write_json(path, {"k": 1})
    assert not os.path.exists(path + ".tmp")        # tmp cleaned up on give-up
