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
        # Mirrors JumpRangeChecker.SHIP_RANGES (JDC-5 range = hull base × 2.0).
        # Passed as custom_ranges, so keep it in sync with the class dict to
        # avoid silently overriding a corrected default (e.g. Rorqual 10.0).
        "ranges_ly": {
            "Dreadnought": 7.0,
            "Lancer Dreadnought": 8.0,
            "Carrier": 7.0,
            "Command Carrier": 7.5,
            "Force Auxiliary": 7.0,
            "Supercarrier": 6.0,
            "Titan": 6.0,
            "Black Ops": 8.0,
            "Jump Freighter": 10.0,
            "Rorqual": 10.0,
        },
    },
    "map": {
        "bloom": True,
        # Eased zoom glide (Task 24, P3). False = instant snap (pre-Phase-F feel)
        # for owners who prefer it; toggled from the map empty-space right-click
        # menu ("Zoom animation") and persisted on hide.
        "zoom_animation": True,
        "layers": {"fleet": True, "staging": True, "threat": False, "range": False,
                   "bridges": True, "route": True, "heat": True, "intel": True,
                   # Kill pings (Task 36): discrete zkill-ALERT radar bursts on the
                   # map (distinct from the ambient kill-heat glow). ON by default --
                   # only fires when the user's zkill monitoring raises an alert.
                   "kill_pings": True,
                   # Sovereignty tint (Task 33): OFF by default -- the palette-noise
                   # call is the owner's. Enabling it lazily fetches ESI sov data.
                   "sov": False},
        # Kill-heat layer (Task 30): hourly ESI ambient kills feed a LOW heat band
        # under the live zkill decay-heat. OWNER-APPROVED 2026-07-12 ("Ok to make 2
        # calls per hour") -> ON by default. Set False to run zkill-only (no ESI).
        "kill_heat_esi": True,
        "threat_ship": "Titan Bridge",
        # Hostile-staging systems (by NAME) excluded from the threat halo via the
        # map's Threat drawer (Task 34). Empty = every staging contributes, so a
        # newly-added staging defaults to INCLUDED.
        "threat_staging_excluded": [],
        "range_ship": "Dreadnought",
        # Keys match map_camera.Camera.to_dict(); None scale = fit universe.
        "camera": {"cx": None, "cy": None, "scale": None},
        "render_mode": "auto",
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
        # Picker display names, persisted alongside the ids so the Settings
        # pickers restore on rebuild without reverse lookups (0-id rows keep "").
        # Written instantly on each pick — see FCToolGUI._market_persist_keys.
        "staging_system_name": "",     # staging system name (autocomplete entry text)
        "staging_station_name": "",    # chosen NPC station display name
        "staging_structure_name": "",  # chosen structure name (no "(id)" suffix)
        "scan_contracts": True,     # allow the (expensive) contract scan
        "scan_on_startup": True,    # background re-scan a few seconds after launch
        "include_alliance_contracts": True,  # also pull corp/alliance contracts (needs scope)
        "contracts_scope": "system",  # "system" (staging system only) | "region" (whole region)
        "seed_target": 20,          # FIXED target quantity of each fit for gap math
    },
    # Friendly-infrastructure scan (infra_* feature, plan 2026-07-11-infra-scan).
    # Simplification (plan §3.10): the configured scan-region list lives ONLY in
    # the store file (infrastructure.json) — config holds just the startup toggle.
    # OFF by default: a region scan spends the shared ESI error budget, so opting
    # in is the owner's call. The map's Infra chip layer defaults OFF via
    # map_tab._LAYERS_OFF_BY_DEFAULT (deliberately NOT a map.layers key here).
    "infra": {
        "auto_scan_on_start": False,
    },
}
