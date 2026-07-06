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
    "fleet_default_seeded": False,
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
        "watch_alliances": [],
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
    # Market Scanner (design 2026-07-06-market-scanner-design.md §6.1). All ids
    # default to 0 = "not set" → the scanner returns an empty snapshot with a
    # "configure staging market" status and touches no network. Phase B adds the
    # structure-picker UI that populates these; nothing reads this block yet.
    "market": {
        "staging_structure_id": 0,  # citadel market (0 = not set); authed pull, primary
        "staging_station_id": 0,    # NPC station id (0 = not set); filters the region pull
        "staging_region_id": 0,     # region for orders + contracts pull
        "staging_system_id": 0,     # resolved staging system (0 = unknown); contract filter
        "scan_contracts": True,     # allow the (expensive) contract scan
        "include_alliance_contracts": True,  # also pull corp/alliance contracts (needs scope)
        "contracts_scope": "system",  # "system" (staging system only) | "region" (whole region)
        "seed_target": 20,          # FIXED target quantity of each fit for gap math
    },
}
