"""Tests for _preview_cfg — per-key defaults + legacy overlay.enabled migration.

House pattern: bind the unbound method onto a bare SimpleNamespace host
(see tests/test_overlay_poll_plan.py + the _preview_cfg docstring note).
"""
import types
from types import SimpleNamespace

import fc_gui


def _host(config):
    host = SimpleNamespace(config=config, _save_config=lambda: None)
    host._preview_cfg = types.MethodType(fc_gui.FCToolGUI._preview_cfg, host)
    return host


def test_defaults_filled_and_mode_off_by_default():
    h = _host({})
    cfg = h._preview_cfg()
    assert cfg["mode"] == "off"
    assert cfg["tile_w"] == 384 and cfg["tile_body_h"] == 216
    assert cfg["opacity_inactive"] == 0.85
    assert cfg["hotkeys"]["groups"][0] == {"next": [], "prev": [], "order": []}
    assert cfg["layouts"] == {}
    assert cfg["doctrine_tag_captions"] is True      # caveat #4 default (B1)
    assert cfg["damage_flash"] is True               # caveat #3 default (B6)
    assert cfg["damage_flash_pct"] == 10 and cfg["damage_flash_reference"] == "weakest"


def test_legacy_overlay_enabled_migrates_to_eveo_labels_mode_once():
    h = _host({"overlay": {"enabled": True}})
    assert h._preview_cfg()["mode"] == "eveo_labels"
    # migration is sticky, not re-derived: turning mode off must survive overlay.enabled
    h.config["preview"]["mode"] = "off"
    assert h._preview_cfg()["mode"] == "off"


def test_existing_preview_values_never_overwritten():
    h = _host({"preview": {"mode": "native", "layouts": {"kirejen": [1, 2, 384, 216]}}})
    cfg = h._preview_cfg()
    assert cfg["mode"] == "native"
    assert cfg["layouts"] == {"kirejen": [1, 2, 384, 216]}
    assert cfg["snap"] is True   # missing keys still filled
