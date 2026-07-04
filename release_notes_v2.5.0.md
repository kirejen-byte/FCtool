## FCTool v2.5.0

### New — Native client previews (beta)

FCTool can now show **live previews of your own EVE clients right inside the tool** — one small always-on-top tile per client with the live game video and a caption strip — so you no longer need **EVE-O Preview** running.

- **Settings → the previews section**: choose **Native previews (beta)**. (The other modes stay available: **Off**, and **Label EVE-O thumbnails** — the existing activity-label overlay on EVE-O Preview's own thumbnails.)
- **Click a tile** to bring that client to the front. Assign a global **focus hotkey** per character, or **cycle** next/prev across a group.
- **Drag to arrange**; positions snap (grid + edges) and are **saved per character**, along with each tile's size. Login screens stack neatly.
- **EVE-O parity**: hover opacity + hover zoom (9 anchors), hide rules (active / login / lost-focus), minimize-inactive with a never-minimize priority list and minimize-all, active-client highlight, per-tile cycle exclusion, switch-to-non-EVE, arrange-in-grid, and a one-time **Import EVE-O layout…** that fills in matching positions, focus hotkeys, and cycle order (fill-only — it never overwrites what you've already set in FCTool).
- Multiboxes cleanly with **per-monitor DPI**.

**Requirements:** clients must run **windowed** (borderless-window or windowed-fullscreen — exclusive fullscreen has no thumbnail to mirror). Focus hotkeys are **swallowed** by Windows while registered, so pick keys you don't otherwise use in EVE.

### New — FCTool-exclusive extras

- **Doctrine-tag captions** (default ON): each tile's caption defaults to the pilot's **doctrine-fit tag** from your own active doctrine, so you can see at a glance who's flying what. Override any caption with a **Manual tag**.
- **Damage flash** (native-mode only, **default ON**): native tiles **pulse red on incoming combat damage**. It reads **only your own EVE combat Gamelogs** (same own-logs-only compliance class as the intel firehose and the EVE-APM precedent). The threshold is **based on base hull HP — fitted ships have more, so it's an approximation** — and it assumes the **English client** (localized clients are out of scope for this version). The separate intel tile-flash remains **default OFF**.

### Compliance

Native previews are **view-only** — they mirror the full client area of **your own** running clients (no cropped sub-regions; no reading another client's screen, overview, or local). Clicking a tile or pressing a focus hotkey performs **exactly one OS window-focus change** and nothing else — **no input is ever sent into an EVE client**. All caption, label, and flash data comes **only from your own SSO-authorized characters' ESI and your own chat/combat log files** — never another player's data, no scraping, no injection, no telemetry; everything stays local. This matches CCP's stated position that view-only full-client previews (as EVE-O Preview does them) are fine, and that switching clients must bring the client to the front.

---

**Install:** unzip `FCTool.exe` and `config.json` into the same folder and run the exe. Updating? Replace only `FCTool.exe` and keep your existing `*.json` files.
