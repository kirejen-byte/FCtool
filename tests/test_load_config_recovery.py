"""Unit tests for FCToolGUI._load_config corrupt-recovery hardening.

_load_config never touches ``self`` in its body (it reads module globals
CONFIG_PATH / DEFAULT_CONFIG / resolve_eve_logs_path), so it can be exercised
in isolation by calling the unbound method with a throwaway object — no Tk, no
network. We monkeypatch fc_gui.CONFIG_PATH to a temp file per case.

Covers:
  * a corrupt EXISTING config.json is backed up to <path>.corrupt and the app
    falls back to DEFAULT_CONFIG instead of crashing,
  * an absent config.json falls back to defaults (no backup written),
  * a valid config.json is loaded through unchanged.
"""

import json
import os

import fc_gui
from default_config import DEFAULT_CONFIG


def _load(monkeypatch, cfg_path):
    """Run _load_config with CONFIG_PATH pointed at cfg_path."""
    monkeypatch.setattr(fc_gui, "CONFIG_PATH", str(cfg_path))
    # Body ignores self, so any object works as the bound instance.
    return fc_gui.FCToolGUI._load_config(object())


def test_corrupt_config_is_backed_up_and_defaults_returned(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("{ this is not valid json :::", encoding="utf-8")

    cfg = _load(monkeypatch, cfg_path)

    # App still starts on defaults rather than raising.
    assert cfg["poll_interval_seconds"] == DEFAULT_CONFIG["poll_interval_seconds"]
    assert cfg["esi"]["client_id"] == DEFAULT_CONFIG["esi"]["client_id"]
    # The bad file is preserved for recovery, not lost.
    backup = tmp_path / "config.json.corrupt"
    assert backup.exists()
    assert backup.read_text(encoding="utf-8") == "{ this is not valid json :::"
    # eve_logs_path auto-detect still ran (key present after resolution).
    assert "eve_logs_path" in cfg


def test_corrupt_recovery_returns_deep_copy_not_shared_default(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("not json", encoding="utf-8")

    cfg = _load(monkeypatch, cfg_path)
    # Mutating the returned config must not bleed into the module default.
    cfg["esi"]["client_id"] = "MUTATED"
    assert DEFAULT_CONFIG["esi"]["client_id"] != "MUTATED"


def test_corrupt_backup_overwrites_existing_corrupt_file(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    backup = tmp_path / "config.json.corrupt"
    backup.write_text("OLD BACKUP", encoding="utf-8")
    cfg_path.write_text("still bad}", encoding="utf-8")

    _load(monkeypatch, cfg_path)

    assert backup.read_text(encoding="utf-8") == "still bad}"


def test_absent_config_returns_defaults_no_backup(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"  # never created
    assert not cfg_path.exists()

    cfg = _load(monkeypatch, cfg_path)

    assert cfg["poll_interval_seconds"] == DEFAULT_CONFIG["poll_interval_seconds"]
    assert not (tmp_path / "config.json.corrupt").exists()


def test_valid_config_is_loaded_through(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    payload = json.loads(json.dumps(DEFAULT_CONFIG))
    payload["esi"]["client_id"] = "MY_OWN_ID"
    cfg_path.write_text(json.dumps(payload), encoding="utf-8")

    cfg = _load(monkeypatch, cfg_path)

    assert cfg["esi"]["client_id"] == "MY_OWN_ID"
    # Valid load writes no .corrupt sidecar.
    assert not (tmp_path / "config.json.corrupt").exists()
