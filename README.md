# FCTool

A desktop intel & fleet-command assistant for **EVE Online** — live zKillboard fight alerts, in-game intel-channel fusion, jump-range and wormhole routing, and an X-up fleet counter, all in one Tkinter app.

[![Download latest](https://img.shields.io/github/v/release/kirejen-byte/FCtool?label=Download&logo=github)](https://github.com/kirejen-byte/FCtool/releases/latest)

> **Windows desktop app — [⬇ Download the latest release](https://github.com/kirejen-byte/FCtool/releases/latest):** grab the `.zip`, unzip, set up `config.json`, run `FCTool.exe`. No Python required.

---

## Features

- **Live engagement feed** — streams zKillboard kills and raises *fight detected / growing* alerts, narrowed by a configurable **Location + Involved-parties** filter with **AND/OR** logic and user-defined **coalitions**. Collapse the filter to maximize the feed.
- **Standings-based capital alerts** — flags hostile capitals using **your own** corp/alliance + ESI standings (nothing hard-coded to any group), with toggles to alert on hostile caps and to bypass the filter for them.
- **Intelligence Fusion** — tails your tracked in-game intel channels (from EVE's chat logs), parses each report (system, pilot count, d-scan link, cyno/camp flags), and cross-references it with live zKillboard activity. Includes a **suggested-channels** picker that scans your logs across all characters.
- **Jump-range checker** — Dread / Carrier / FAX / Super / Titan / Black Ops / Jump Freighter / Rorqual ranges, Ansiblex-aware routing, and editable friendly/hostile staging lists.
- **Fittings, doctrines & MOTD writer** — import fits from **pyfa** or **in-game** (ESI), organize them into **doctrines** with role tags, and compose a clickable fleet **MOTD** (FC link, staging system, role-grouped fit links, logi channel) with a WYSIWYG markup editor and a live raw/rendered preview. **Doctrine-driven fleet guidance** sets an ideal composition (% or # per fit), shows live *in / under / over* status in **Fleet Management**, and annotates the MOTD with `+X / -Y` pilot deltas for the current fleet. Command-burst (links) coverage is tracked from fleet chat. **Fleet Templates** define your wing/squad structure with assignment rules and one-click **Auto-move** pilots into position as the fleet changes; a ready-made **Default** template (Subcaps/Caps) ships built-in and editable, and templates are only applied when you choose.
- **Market scanner** — measures your **staging market's** depth and breadth against each doctrine: how many **complete fits** you can source from local sell stock and the **bottleneck** component holding you back, plus **seed targets you can set per ship, per doctrine, or globally** (50 Stabbers, 20 Scythes, 10 Bifrosts) that colour the readout green/yellow/red. Scans are **doctrine-targeted by default** (only the components your active doctrine needs); a full-market scan lives in Settings with a fair warning that it can take a while. Contract matching folds in public **and** corp/alliance item-exchange contracts that are the same hull at **≥95% match** (modules + T3 subsystems), and every fit line in **Fittings** gets **per-component ✓/✗ availability marks** with live qty and best price. When you're short, **Gaps…** builds a one-click shopping list you can **copy for Janice** or paste straight into in-game **Multibuy**. Public contract data works immediately; a **citadel market + contract scan** needs a one-time re-login for the extra ESI scopes (flagged with a ⚠ in the SSO list — until then it degrades to public data). FCTool also quietly **re-scans the market in the background a few seconds after launch** (skipped if the last scan is under 5 minutes old or the market isn't configured yet) so the availability marks are fresh without a manual click — toggle **"Re-scan market on startup"** in Settings if you'd rather scan by hand.
- **Wormhole routing**, an **X-up fleet counter** (configurable trigger / clear words + threshold), **character & asset tracking**, and **text-to-speech audio alerts**.

## Client Previews (native)

FCTool can show **live previews of your own EVE clients** right inside the tool — a small always-on-top tile per client with the live game video and a caption strip — so you no longer need EVE-O Preview running. Left-click a tile to bring that client to the front; assign a global focus **hotkey** per character or cycle hotkeys across a group; drag tiles to arrange them (positions snap and persist per character). It multiboxes cleanly with per-monitor DPI.

Turn it on in **Settings → the previews section**: pick **FCPreview**. (The other modes are **Off** and **Eve-O Preview Enhancement**, which keeps the activity-label overlay on EVE-O Preview's own thumbnails.)

**EVE-O Preview parity (condensed):**

| Capability | FCTool native |
|---|---|
| Live per-client thumbnails | ✓ |
| Click-to-activate | ✓ |
| Per-character focus hotkeys + cycle-next/prev groups | ✓ |
| Drag to arrange, snap-to-grid / snap-to-edges, per-character saved layout & size | ✓ |
| Login-screen stacking | ✓ |
| Hover opacity + hover zoom (9 anchors) | ✓ |
| Hide active / hide login / hide-on-lost-focus rules | ✓ |
| Minimize-inactive + never-minimize (priority) list + minimize-all | ✓ |
| Active-client highlight, per-tile cycle exclusion, switch-to-non-EVE | ✓ |
| Arrange-in-grid, one-time **Import EVE-O layout** | ✓ |

**Requirements & notes:**
- Clients must run **windowed** (borderless-window or windowed-fullscreen). True exclusive fullscreen has no thumbnail for Windows to mirror.
- Focus hotkeys are **swallowed** by Windows while registered — they will not also reach the game or other apps, so pick keys you don't otherwise use in EVE.
- **Cycle groups** — name your own groups of clients in **Cycle groups…** (in the FCPreview settings row, next to **Hotkeys…**) and give each group its own next/prev cycle keys; a group with **no members cycles all clients**. Like focus keys, cycle keys are **swallowed** system-wide while FCPreview runs. Importing an `EVE-O-Preview.json` also brings in its **CycleGroup1–5** groups (fill-only — your own groups are never overwritten).
- **Import EVE-O layout…** reads an existing `EVE-O-Preview.json` and fills in matching per-character positions, focus hotkeys, and cycle order (fill-only — it never overwrites layouts you've already set in FCTool).

### FCTool-exclusive extras

- **Activity label + location bars** — each tile shows the character name in the top strip and, in a **bottom strip**, an activity label plus the ship's **hull type** (e.g. `Logi - Onyx`, `Cyno - Force Recon`), then a **separate line** below it with the pilot's current **system**. All of it renders below the video (never over it) in your chosen colour/size and **auto-shrinks to fit** (keeping your size when it can), so nothing gets clipped. The label comes from your label rules or your active doctrine's fit tag; override any caption with a **Manual tag** in the label rules. (Both bottom lines have their own on/off toggles.)
- **Damage flash** — native tiles **pulse red on incoming combat damage**. This is **native-mode only and default ON** (the separate intel tile-flash is default OFF). Fine print, verbatim: it reads **only your own EVE combat Gamelogs** (the same own-logs-only compliance class as the intel firehose and the EVE-APM precedent); the threshold is **based on base hull HP — fitted ships have more, so it is an approximation**; and it assumes the **English client** (localized clients are out of scope for this version).
- **Decloak alert** — when one of your characters gets **decloaked** (proximity to a gate/structure/ship, or a **Mobile Observatory pulse**), that tile **flashes yellow** and its label bar becomes a **DECLOAKED hazard banner** for 10 seconds, with an optional spoken **"Decloaked!"** cue (off by default). Suppressed while that client is the focused window — you already saw it. Same own-Gamelogs-only source as the damage flash; damage red takes priority over decloak yellow.

### Compliance

Previews are **view-only**: they mirror the full client area of **your own** running clients (no cropped sub-regions, no overlays that read another client's screen, overview, or local). Clicking a tile or pressing a focus hotkey performs **exactly one OS window-focus change** and **nothing else** — no input of any kind is ever sent into an EVE client. All caption/label/flash data comes **only from your own SSO-authorized characters' ESI and your own chat/combat log files** — never another player's data, no scraping, no injection, no telemetry, everything stays local. This matches CCP's stated position that view-only full-client previews (as EVE-O Preview does them) are fine, and that switching clients must bring the client to the front.

## Download & run (end users)

1. Open **[releases/latest](https://github.com/kirejen-byte/FCtool/releases/latest)** and download the `.zip`.
2. Unzip it anywhere — you'll get `FCTool.exe` and a ready-to-go **`config.json`** (the ESI Client ID is already set; PKCE sign-in, no secret, no app to register).
3. Run **`FCTool.exe`** and sign in via the in-app ESI SSO button — as your own character. Your EVE chat-logs folder is auto-detected; intel channels, staging system, and filters live in **Settings**.

> Your `config.json` and ESI token files stay on your machine — don't share them.

## Build from source (developers)

Requires **Python 3.11+** (3.13 recommended) on Windows.

```bash
git clone https://github.com/kirejen-byte/FCtool.git
cd FCtool
pip install -r requirements.txt
python fc_gui.py             # runs out of the box: built-in Client ID, logs auto-detected
python -m pytest -q          # run the test suite
```

Build a standalone one-file EXE (PyInstaller):

```bash
pip install pyinstaller
pyinstaller --clean --noconfirm FCTool.spec
# -> dist/FCTool.exe
```

## Notes

- Windows desktop app (Tkinter). Needs internet access for ESI, zKillboard, and TTS.
- **Sharing the app:** sign-in uses PKCE, so you can hand someone `FCTool.exe` + a `config.json` containing only your **`client_id`** (with `client_secret` left blank) — there's no secret to leak. Each person signs in with their own EVE character.
- Runtime caches (`esi_cache.json`, `systems_cache.json`, `regions_cache.json`, …) plus your `config.json` and ESI tokens are gitignored and regenerate locally on first run. The static EVE stargate graph (`stargate_jumps.json`) **is** committed so the project builds from a clean clone.
- Built with `requests`, `pygame` (audio), and `edge-tts` (text-to-speech).

### EVE logs on Linux (Wine / Proton / Lutris)

EVE doesn't run natively on Linux, so its chat logs live inside the Wine/Proton
prefix instead of a normal `~/Documents`. The tool now auto-detects the common
prefix locations — `~/.wine`, Steam-Proton `compatdata`, and Lutris `~/Games`
prefixes — and looks for `.../drive_c/users/<user>/Documents/EVE/logs/Chatlogs`.

If auto-detection can't find your logs, set **`eve_logs_path`** in `config.json`
to your prefix's chat-logs folder, e.g.
`~/.wine/drive_c/users/<user>/Documents/EVE/logs/Chatlogs`.

### System coordinate table

`system_coords.json` is a committed, bundled snapshot of New Eden system
coordinates/names/regions/security used for instant, offline jump-range checks.
Regenerate it after a CCP expansion that changes the universe:

    py -3.13 tools/gen_system_coords.py

Coordinates are static and change very rarely, so refreshes are infrequent.
