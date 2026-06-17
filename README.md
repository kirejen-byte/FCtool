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
- **Fittings, doctrines & MOTD writer** — import fits from **pyfa** or **in-game** (ESI), organize them into **doctrines** with role tags, and compose a clickable fleet **MOTD** (FC link, staging system, role-grouped fit links, logi channel) with a WYSIWYG markup editor and a live raw/rendered preview. **Doctrine-driven fleet guidance** sets an ideal composition (% or # per fit), shows live *in / under / over* status in **Fleet Management**, and annotates the MOTD with `+X / -Y` pilot deltas for the current fleet. Command-burst (links) coverage is tracked from fleet chat.
- **Wormhole routing**, an **X-up fleet counter** (configurable trigger / clear words + threshold), **character & asset tracking**, and **text-to-speech audio alerts**.

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
