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
                   "bridges": True, "route": True,
                   # Kill-heat (Task 30): key stays for programmatic/config
                   # enablement, but the toolbar/menu control is removed (owner
                   # ask 2026-07-12) -- default OFF so a hidden layer doesn't
                   # silently draw or spend the ambient ESI call.
                   "heat": False, "intel": True,
                   # Kill pings (Task 36): discrete zkill-ALERT radar bursts on the
                   # map (distinct from the ambient kill-heat glow). ON by default --
                   # only fires when the user's zkill monitoring raises an alert.
                   "kill_pings": True,
                   # Sovereignty tint (Task 33): OFF by default -- the palette-noise
                   # call is the owner's. Enabling it lazily fetches ESI sov data.
                   "sov": False,
                   # Characters overlay (owner ask): magenta markers at each authed
                   # character's system. ON by default -- the 60 s poll runs ONLY
                   # while the Map tab is shown, so idle cost is nil.
                   "chars": True},
        # Kill-heat layer (Task 30): hourly ESI ambient kills feed a LOW heat band
        # under the live zkill decay-heat. Owner-approved 2026-07-12 to spend the
        # call ("Ok to make 2 calls per hour") WHILE the layer was on the toolbar;
        # now that the layer is hidden by default (owner ask 2026-07-12, same day),
        # default OFF so a hidden layer doesn't spend the hourly ESI call. Set True
        # to run the ambient fetch when heat is force-enabled by config.
        "kill_heat_esi": False,
        "threat_ship": "Titan Bridge",
        # Hostile-staging systems (by NAME) excluded from the threat halo via the
        # map's Threat drawer (Task 34). Empty = every staging contributes, so a
        # newly-added staging defaults to INCLUDED.
        "threat_staging_excluded": [],
        # Friendly-staging PROJECTION (owner ask): a SECOND, blue halo showing the
        # jump/bridge reach of your OWN stagings, opt-in from the map's Threat drawer.
        # Default OFF -- a new visual the owner enables. The by-NAME exclusion list
        # mirrors threat_staging_excluded above (empty = every friendly staging
        # contributes). The SAME threat_ship class drives both halos.
        "threat_friendly_enabled": False,
        "threat_friendly_excluded": [],
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
        # Naming convention for FUTURE boolean startup-scan-style keys: `<verb>_on_startup`
        # (as below). infra.auto_scan_on_start (below) predates this convention and is
        # grandfathered as-is -- renaming a persisted config key needs a migration, which
        # isn't worth it on its own (OPTIMIZATION_REVIEW.md F2).
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
        # Grandfathered spelling -- predates the `<verb>_on_startup` convention documented
        # at market.scan_on_startup above; not renamed since persisted-key renames need a
        # migration path (OPTIMIZATION_REVIEW.md F2).
        "auto_scan_on_start": False,
    },
    # Implant-removal reminder (spike 2026-07-25-implant-reminder). A small
    # transient toast over the docked client reminding a pilot carrying expensive
    # implants to pull them after docking back at staging following a fleet.
    #
    # DEFAULT OFF, and that is load-bearing: it needs esi-clones.read_implants.v1,
    # which every pre-existing character token lacks until the owner re-consents,
    # so a nag defaulting ON would misfire for every install on upgrade day.
    # While `enabled` is False NO worker runs, NO ESI implant call is made and the
    # scope is never exercised — the poller hook returns before touching anything.
    #
    # Trigger = docked at staging AND known to be carrying implants worth pulling.
    # "Staging" is whatever system sits under Settings > Staging System
    # (zkillboard.staging_system) — the SAME key that drives route-from-staging
    # on kill alerts and the MOTD leave-staging guard. Docked at ANY structure or
    # station in that system counts (owner correction, 2026-07-25): this is
    # deliberately NOT narrowed down to one exact citadel, and it deliberately
    # never reads the separate market/seeding-citadel config block, even when
    # that block happens to name a structure in the same system — see
    # implant_reminder.resolve_staging. The two override keys below win outright
    # over the normal resolution. With nothing set the feature stays inert
    # rather than firing on every dock anywhere — and says so once in the log,
    # so it can never be an undiagnosable no-op.
    "implant_reminder": {
        "enabled": False,           # MASTER GATE
        "toast_seconds": 12,        # hold before the fade (clamped 3..60)
        # "valuable" (default) = the High-/Mid-/Low-grade SET implants, the +5
        # ("- Improved") attribute implants, and top-tier + named hardwirings;
        # never boosters and never the +4-and-below attribute implants pilots fly
        # permanently. "any" = every implant. "custom" = substring match against
        # match_names below (an EMPTY match_names falls back to "valuable" — a
        # mode that can never fire is a silent no-op, not a feature).
        "match_mode": "valuable",
        "match_names": [],          # only consulted when match_mode == "custom"
        "min_hardwiring_grade": 5,  # 1..6; the trailing digit of e.g. "SU-606"
        # "ladder" (default) = the override below if set, else the normal
        # Settings > Staging System resolution. "structure" = exact-dock only —
        # the ONLY source of an exact dock is now the staging_structure_id
        # override below, so this scope can never be satisfied any other way.
        # "system" = docked anywhere in the staging system (same as "ladder"
        # minus honoring an exact-structure override).
        "staging_scope": "ladder",
        # Explicit staging OVERRIDE — either one set wins outright over the
        # normal zkillboard.staging_system resolution. Use when this reminder's
        # staging should differ from Settings > Staging System (e.g. a second
        # staging, or testing). 0 / "" = unset (the normal case).
        "staging_structure_id": 0,  # exact citadel/structure id
        "staging_system": "",       # system NAME, e.g. "SVM-3K"
        # Show-oriented UX, disabled-oriented storage (mirrors
        # preview.disabled_chars): a brand-new character defaults to reminded.
        "disabled_chars": [],
    },
    # Fleet loss tracking source (spike 2026-07-25-zkill-loss-source). The
    # capsule-transition detector in loss_tracker.py is FAST (~20-35s) but blind
    # to podded pilots (measured 26-43% of PvP ship losses) and does not run at
    # all when the user is not fleet boss (ESI 403s the member list). zKillboard
    # is ground truth but ~2 min behind (median 120s, p90 267s), which is far too
    # late for the 10/25/50% threshold alerts.
    #   "esi"    — today's behaviour EXACTLY; the reconciler is never attached,
    #              so the zKill feed does no extra work at all.
    #   "zkill"  — killmail-derived losses drive the display (the non-boss case).
    #   "hybrid" — DEFAULT. ESI keeps driving the live threshold alerts
    #              UNCHANGED; zKill reconciles behind them for real ISK and the
    #              losses ESI structurally cannot see.
    # config.json is never deep-merged, so an upgraded install has no block at
    # all: every key is read with a fallback and an unknown "source" normalizes
    # to the default (loss_reconciler.from_config / .source_from_config).
    # NOTE: the audio toggle stays the bare top-level "loss_audio_enabled" key —
    # relocating it here would silently reset every existing user's preference.
    "loss_tracking": {
        "source": "hybrid",
        # Asymmetric reconcile window, in seconds: a killmail's time is the TRUE
        # death instant while the ESI inference fires 15-45s AFTER it, so an
        # inferred death may TRAIL the killmail by match_window_s and LEAD it by
        # match_lead_s (clock skew / an early poll).
        "match_window_s": 300,
        "match_lead_s": 30,
    },
    "overview": {
        # Hidden by default (feature in progress); set true to show the Overview tab.
        "tab_enabled": False,
        "overview_dir_override": "",
        "account_nicknames": {},
        "distribution_log": {},
        "dat_sync_enabled": False,
        "dat_sync_acceptance_passed": False,
    },
    # NOTE: the "fittings" block is NOT seeded here — its keys are created lazily
    # (setdefault) by the Fittings/MOTD subsystem the first time they are needed,
    # so the shape of this defaults dict is unchanged. For reference, the MOTD
    # composer manages these "fittings" sub-keys:
    #   * "saved_motds"        — list of named per-doctrine MOTD templates. v2
    #     entries are {name, doctrine, fc, doc: [runs…], version: 2} (a pill-canvas
    #     document; a v1 entry that pre-dates the composer redesign auto-migrates
    #     on load and is rewritten as v2 only when the user next saves it, keeping
    #     its old fit list as "legacy_fits" for the empty-tag_line fallback).
    #   * "motd_recent_tokens" — MRU list (cap 10) of recently-inserted token specs
    #     {kind, params, label} feeding the palette's Recent group + tray.
    #   * "motd_budget", "motd_link_interval_s", "motd_stop_leave_staging",
    #     "logi_channel" — the length ceiling, auto-update interval, leave-staging
    #     guard toggle, and remembered default logi channel.
}
