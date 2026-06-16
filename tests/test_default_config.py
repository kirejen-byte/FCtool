"""Tests for the built-in default configuration used when no config.json exists."""

import json

from default_config import DEFAULT_CONFIG


def test_has_public_client_id():
    cid = DEFAULT_CONFIG["esi"]["client_id"]
    assert cid and cid != "YOUR_ESI_CLIENT_ID"


def test_has_no_secret():
    assert DEFAULT_CONFIG["esi"]["client_secret"] == ""


def test_no_personal_data():
    assert DEFAULT_CONFIG["eve_logs_path"] == ""
    assert DEFAULT_CONFIG["tracked_character"] == ""
    assert DEFAULT_CONFIG["zkillboard"]["watch_alliances"] == []
    assert DEFAULT_CONFIG["ansiblex_connections"] == []


def test_json_round_trip_deep_copy_is_independent():
    # _load_config deep-copies via json.loads(json.dumps(...)); verify that
    # works and yields an object independent of the module-level default.
    clone = json.loads(json.dumps(DEFAULT_CONFIG))
    assert clone == DEFAULT_CONFIG
    clone["esi"]["client_id"] = "mutated"
    assert DEFAULT_CONFIG["esi"]["client_id"] != "mutated"
