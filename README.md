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
- **Wormhole routing**, an **X-up fleet counter** (configurable trigger / clear words + threshold), **character & asset tracking**, and **text-to-speech audio alerts**.

## Download & run (end users)

1. Open **[releases/latest](https://github.com/kirejen-byte/FCtool/releases/latest)** and download the `.zip`.
2. Unzip it anywhere — you'll get `FCTool.exe` and `config.example.json`.
3. Copy `config.example.json` to **`config.json`** (in the same folder as the exe) and edit:
   - **`eve_logs_path`** — your EVE chat-logs folder, e.g. `C:/Users/<you>/Documents/EVE/logs/Chatlogs`.
   - **`esi`** — register a free application at **[developers.eveonline.com](https://developers.eveonline.com/)** with callback URL `http://localhost:8834/callback`. **Recommended:** choose the **native / PKCE** app type (it issues a `client_id` with no secret) — paste that `client_id` and **leave `client_secret` blank**. *(A confidential app also works: paste both its `client_id` and `client_secret`.)*
4. Run **`FCTool.exe`** and log your character in via the in-app ESI SSO button. Set your intel channels, staging system, and filters in **Settings**.

> Your `config.json` and ESI token files stay on your machine — don't share them.

## Build from source (developers)

Requires **Python 3.11+** (3.13 recommended) on Windows.

```bash
git clone https://github.com/kirejen-byte/FCtool.git
cd FCtool
pip install -r requirements.txt
python fc_gui.py              # run from source
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

### System coordinate table

`system_coords.json` is a committed, bundled snapshot of New Eden system
coordinates/names/regions/security used for instant, offline jump-range checks.
Regenerate it after a CCP expansion that changes the universe:

    py -3.13 tools/gen_system_coords.py

Coordinates are static and change very rarely, so refreshes are infrequent.
