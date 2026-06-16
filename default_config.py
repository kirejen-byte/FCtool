"""Built-in default configuration.

Used when no ``config.json`` is present next to the app (e.g. a fresh source
clone, or a first run), so FCTool works out of the box: the public ESI Client
ID is baked in (PKCE sign-in, no secret) and the EVE chat-logs folder is
auto-detected at load. A real ``config.json`` beside the app fully overrides
these — see ``FCToolGUI._load_config``.

The Client ID is a public OAuth identifier (it rides in every login redirect),
not a secret, so it is safe to ship in source.
"""

DEFAULT_CONFIG = {
    "eve_logs_path": "",
    "poll_interval_seconds": 1.0,
    "autostart": True,
    "tracked_character": "",
    "sound_on_ready": True,
    "ansiblex_connections": [],
    "xup": {
        "trigger_word": "x",
        "fire_word": "FIRE",
        "threshold": 50,
        "channel_name": "Fleet",
        "case_sensitive": False,
    },
    "zkillboard": {
        "enabled": True,
        "watch_regions": [],
        "watch_regions_names": {},
        "watch_alliances": [],
        "watch_alliances_names": {},
        "watch_systems": [],
        "min_kill_value_millions": 0,
        "min_pilots_involved": 25,
        "alert_window_seconds": 300,
        "staging_system": "",
    },
    "esi": {
        "client_id": "5373b3f588614a7eae20f409c5adbdc4",
        "client_secret": "",
        "callback_url": "http://localhost:8834/callback",
    },
    "jump_range": {
        "origin_system": "",
        "ship_type": "Dreadnought",
        "jump_drive_calibration_level": 5,
        "ranges_ly": {
            "Dreadnought": 7.0,
            "Carrier": 7.0,
            "Supercarrier": 6.0,
            "Titan": 6.0,
            "Black Ops": 8.0,
            "Jump Freighter": 10.0,
            "Rorqual": 5.0,
        },
    },
}
