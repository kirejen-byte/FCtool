# FCTool

A desktop intel & fleet-command assistant for **EVE Online** — live zKillboard fight alerts, in-game intel-channel fusion, jump-range and wormhole routing, and an X-up fleet counter, all in one Tkinter app.

[![Download latest](https://img.shields.io/github/v/release/kirejen-byte/FCtool?label=Download&logo=github)](https://github.com/kirejen-byte/FCtool/releases/latest)

> **Windows desktop app — [⬇ Download the latest release](https://github.com/kirejen-byte/FCtool/releases/latest):** grab the `.zip`, unzip, set up `config.json`, run `FCTool.exe`. No Python required.

---

## Features

- **Live engagement feed** — streams zKillboard kills and raises *fight detected / growing* alerts, narrowed by a configurable **Location + Involved-parties** filter with **AND/OR** logic and user-defined **coalitions**. Collapse the filter to maximize the feed.
- **Standings-based capital alerts** — flags hostile capitals using **your own** corp/alliance + ESI standings (nothing hard-coded to any group), with toggles to alert on hostile caps and to bypass the filter for them.
- **Intelligence Fusion** — tails your tracked in-game intel channels (from EVE's chat logs), parses each report (system, pilot count, d-scan link, cyno/camp flags), and cross-references it with live zKillboard activity. Includes a **suggested-channels** picker that scans your logs across all characters.
- **2D Star Map** — a native glowing map of every k-space system on **CCP's official in-game 2D layout**: smooth wheel-zoom from the full universe down to labeled systems, drag panning with no dead edges, and an eased glide (toggle **Zoom animation** off for instant snap). Right-click any system for **jump range from here** (grouped capital classes or a custom LY figure, legality-aware — highsec, Pochven and Zarzakh get struck markers), toggle **threat projection** to shade everything inside hostile-staging jump/bridge range — a **threat drawer** in the tab now picks *which* staging systems project and switches the ship class (Titan-bridge default), and can also cast a **friendly staging projection** that washes your own reachable space in blue (off by default) — and watch live **fleet member pins**, your **own location ring**, friendly/hostile **staging diamonds** and your **Ansiblex bridges** as glowing blue lines — drawn only when both endpoint systems are confirmed to hold a gate, so a one-sided phantom bridge can't appear. Search box **autocompletes** (type a few letters, pick a suggestion tagged with its region) and flies to any system; set destination / Dotlan / staging actions on the same menu. All range and threat math uses true 3D light-year distances, never map distance.
- **Star Map — live tactical layers** — the map doubles as a live tactical board. A **destination route overlay** draws your travel route (Ansiblex-aware, recomputing as you move and clearing on arrival; a destination set here also sets your in-game autopilot). A **kill-heat** glow blooms over systems with active kills — live zKillboard decay (15-minute fade, capital kills ringed) over an hourly ambient baseline from public ESI. **Intel pulses** flash amber on systems named in your tracked intel channels — click a pulse to jump to that line in the **Intelligence** tab. An optional **sovereignty tint** (off by default) washes space in alliance colours with a legend. And **zKillboard kill pings** burst onto the map as red radar pings for five minutes, while every zkill line in the Intelligence tab gains a **[▸ Map]** button that flies the camera to the kill. No new ESI scopes — the only added traffic is two lightweight public calls (the hourly kill baseline, and sovereignty only while that layer is on). A **characters overlay** marks where your own logged-in characters are with magenta squares — hover for the pilot and ship — polling only while the map is on screen and reusing your existing login (no new scopes). A **View on Map** action on the **Characters** tab (plus a **Chars ▾** role menu on the map) narrows that overlay to just the pilots or roles you pick, with a dismissible *Custom filter active* banner while it's on.
- **Infrastructure scan** — a persistent, **local** database of friendly structures. Fill it by **pasting** straight from the in-game structure browser (*Copy Selected With Formatting*) or by running a **rate-safe ESI region scan**, which stamps every structure with the **solar system it's actually in** and self-heals any row an earlier scan mis-filed. The Star Map then carries **per-system count chips** with per-category filters and a **hover breakdown** of each system by type (*3× Fortizar, 1× Astrahus*) — and when only a handful of structures share a system, the hover names each one individually instead, tagged with its owner's **alliance (or corp) ticker**. The **right-click → Structures** list names and tags them the same way (a busy system trails off into an *…and N more* row); imported **Ansiblexes light up the bridges layer** — and when one gets **reinforced** (or its fuel lapses), flag it **Reinforced** in the manager (toolbar button or right-click) to pull that bridge off the map *and* out of route planning until you clear the flag. A tucked-away **manager dialog** handles import preview, region config, and edit / mark-reinforced / mark-dead / delete — with type-ahead region autocomplete, ticker-tagged structure names, and a sortable list of real structure type names. Nothing about your infrastructure ever leaves your machine.
- **Overview Manager** — an **Overview** tab that treats your EVE overview like the asset it is: **import** any in-game overview export (plain YAML — drop it in `Documents\EVE\Overview` and FCTool notices it), keep a **repository of named packs** with fingerprints and notes, and **edit everything** in a full editor — presets built from a searchable **ship/structure group tree**, up to **20 tabs** with per-tab brackets, colors and column overrides, standing/war **colortag & background priorities with blink**, global columns, and ship-label layouts (markup names supported, and nothing you don't touch is ever lost). **Export** a pack straight into EVE's Overview folder (the exact folder the in-game *Import Overview Settings* dialog reads) and work the built-in **per-account checklist** — overview settings are **account-wide**, so every character on an account inherits one import, each account tracked with a ✓ and the in-game click path one button away. FCTool can also **read your live overview straight out of the client's settings files** (strictly read-only) to import *what I'm running right now* from any account, and shows **drift badges** when an account's live overview stops matching the pack you distributed. An FCTool-original **FC Standard** starter (Travel ✈ / Fleet / Targets / Logi / D-scan) seeds the repository, and any pack exports as a normal overview YAML you can hand to fleet members — they import it in-game with zero extra tools.
- **Jump-range checker** — Dread / Carrier / FAX / Super / Titan / Black Ops / Jump Freighter / Rorqual ranges, Ansiblex-aware routing, and editable friendly/hostile staging lists.
- **Fittings, doctrines & MOTD composer** — import fits from **pyfa** or **in-game** (ESI), organize them into **doctrines** with role tags, and build your fleet **MOTD** on a free-form **pill canvas**: rich text and live **token pills** mix freely — FC line, staging line, doctrine line, per-role fit lines (live `+X / -Y` deltas included), channel lines, a single fit / character / system / channel, or the whole doctrine block — all of which **re-resolve on every auto-update push** (over-budget auto-compaction and the stop-when-the-FC-leaves-staging safeguard both still apply). An **add-anything search palette** (collapsible groups — recents, the selected doctrine's fits, lines & blocks, fittings, characters with live **ESI partial-name search**, systems, channels, and doctrines) adds a pill on click or Enter, via **drag-and-drop** (floating drag ghost, live drop-caret), or with an inline `@` for entities and `/` for blocks. A **Quick-Add tray** underneath pre-stocks the doctrine's fits and your recently-used channels as draggable chips that wrap to fit. Templates are **per-doctrine** (Save / Save-as / Rename / Delete, dirty-state guard), and every MOTD you'd already saved **migrates automatically** — old tag names and hand-pasted links included, the latter becoming pills. The canvas sits **right**, the rendered preview **left**, and the raw markup behind a collapsible drawer; the budget meter and its warnings work as before, and typed text is now **escaped**, so a stray `<` shows up literally instead of vanishing as markup. Pasted **EFT "shopping-list" fits** — where a hauler carries the squad's gear in **cargo** — import cleanly: **implants, hardwirings, boosters and deployables** resolve to real items on any machine, fresh install included (no more *Unknown item* warnings on an Amulet set), and **modules** left in cargo ride into the clickable **fit links** as **unfitted** items (per EVE's official fitting-link grammar) instead of being silently dropped. **Doctrine-driven fleet guidance** sets an ideal composition (% or # per fit), shows live *in / under / over* status in **Fleet Management**, and feeds those same numbers into the MOTD's fit-line pills as live `+X / -Y` deltas. Command-burst (links) coverage is tracked from fleet chat. **Fleet Templates** define your wing/squad structure with assignment rules and one-click **Auto-move** pilots into position as the fleet changes; a ready-made **Default** template (Subcaps/Caps) ships built-in and editable, and templates are only applied when you choose.
- **Market scanner** — measures your **staging market's** depth and breadth against each doctrine: how many **complete fits** you can source from local sell stock — counting each fit's **full bill of materials** (fitted modules plus the cargo, ammo and drones it carries) — and the **bottleneck** component holding you back, plus **seed targets you can set per ship, per doctrine, or globally** (50 Stabbers, 20 Scythes, 10 Bifrosts) that colour the readout green/yellow/red. Scans are **doctrine-targeted by default** (only the components your active doctrine needs); a full-market scan lives in Settings with a fair warning that it can take a while. Contract matching folds in public **and** corp/alliance item-exchange contracts that are the same hull at **≥95% match** (modules + T3 subsystems), and every fit line in **Fittings** gets **per-component ✓/✗ availability marks** with live qty and best price. When you're short, **Gaps…** builds a one-click shopping list — the **full bill of materials** for each fit (fitted modules plus everything in cargo: implants, boosters, deployables and spare modules, ammo/charges, and drones), quantities scaled to your seed targets — that you can **copy for Janice** or paste straight into in-game **Multibuy**. The doctrine and fit-availability tables render as soon as the fast order scan finishes (a few seconds); contract-derived figures read **contracts scanning…** until the contract pass completes, then fill in — and that pass is now **much faster**: contract contents download several at a time (still within ESI's rate limits), the progress counter climbs steadily instead of stalling near the end, and known-empty contracts are remembered for hours so they aren't re-fetched every scan. Public contract data works immediately; a **citadel market + contract scan** needs a one-time re-login for the extra ESI scopes (flagged with a ⚠ in the SSO list — until then it degrades to public data). FCTool also quietly **re-scans the market in the background a few seconds after launch** (skipped if the last scan is under 5 minutes old or the market isn't configured yet) so the availability marks are fresh without a manual click — toggle **"Re-scan market on startup"** in Settings if you'd rather scan by hand.
- **Wormhole routing**, an **X-up fleet counter** (configurable trigger / clear words + threshold), **character & asset tracking**, and **text-to-speech audio alerts**.

## Client Previews (native)

FCTool can show **live previews of your own EVE clients** right inside the tool — a small always-on-top tile per client with the live game video and a caption strip — so you no longer need EVE-O Preview running. Left-click a tile to bring that client to the front; assign a global focus **hotkey** per character or cycle hotkeys across a group; drag tiles to arrange them (positions snap and persist per character), or turn on **Snap previews to each other** (opt-in, off by default) so a dragged tile's edge sticks to a neighbor's within ~12px — butted flush or aligned, per axis, nearest edge wins, snapped spot saved. It multiboxes cleanly with per-monitor DPI.

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
- **Reset previews** — a red button next to **Import EVE-O layout…** brings every preview window (including login-screen previews) back on-screen and clears saved positions and per-window size overrides, for when tiles get dragged off-screen or a monitor disconnects. Saved positions also **auto-clamp** onto the visible desktop at restore when they'd otherwise be effectively off-screen; positions that are still reachable (e.g. a second monitor that's merely switched off) are left exactly where you put them.
- **Monitor pinning** — assign each character its own monitor in **Monitor pinning…** (in the FCPreview settings row): when that character logs in, FCTool moves its EVE client onto the chosen monitor. Set a **default monitor** for everyone and override it **per character** (including **never move**). A borderless/**Fixed Window** client fills the target monitor; a smaller window keeps its size and lands in the same spot on the new monitor. **Apply now** moves every running client at once and works **anytime**; automatic on-login placement needs **FCPreview** mode. The client must be in **Fixed Window** or **Windowed** display mode — EVE's true Full Screen can't be moved by another app. It stays completely inert until you assign something, and quietly does nothing if a pinned monitor is unplugged.

### FCTool-exclusive extras

- **Activity label + location bars** — each tile shows the character name in the top strip and, in a **bottom strip**, an activity label plus the ship's **hull type** (e.g. `Logi - Onyx`, `Cyno - Force Recon`), then a **separate line** below it with the pilot's current **system**. All of it renders below the video (never over it) in your chosen colour/size and **auto-shrinks to fit** (keeping your size when it can), so nothing gets clipped. The label comes from your label rules or your active doctrine's fit tag; override any caption with a **Manual tag** in the label rules. (Both bottom lines have their own on/off toggles.)
- **Damage flash** — native tiles **pulse red on incoming combat damage**. This is **native-mode only and default ON** (the separate intel tile-flash is default OFF). Fine print, verbatim: it reads **only your own EVE combat Gamelogs** (the same own-logs-only compliance class as the intel firehose and the EVE-APM precedent); the threshold is **based on base hull HP — fitted ships have more, so it is an approximation**; and it assumes the **English client** (localized clients are out of scope for this version). A **Gamelogs folder** row in Settings (shared with the decloak alert below) shows the effective path FCTool is watching, with **Browse…**/**Auto** to override or re-detect it — re-pointing live, no restart — plus a status line that names the exact state: *watching N file(s)*, *folder not found*, or *no gamelog files found* (EVE's **Log game events to file** setting being off is the #1 cause of dead flashes on a new machine).
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

### Star-map layout data & attribution

`map_layout.json` (the 2D star map's schematic layout) is generated from
**CCP's official Static Data Export** — the same `position2D` layout the
in-game 2D map uses — and bundled under the CCP Developer License.
Regenerate after an expansion that adds or moves systems or stargates:

    py -3.13 tools/gen_map_layout.py --download --out map_layout.json

EVE Online and the EVE logo are the registered trademarks of CCP hf. All
rights are reserved worldwide. FCTool is a free fan-made tool; CCP hf. is
not affiliated with it and does not endorse it.
