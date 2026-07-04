## FCTool v2.5.0

### Native EVE-client previews — **FCPreview**
Show **live previews of your own EVE clients** right inside FCTool — no more EVE-O Preview needed. Enable it under **Settings → previews → FCPreview**.

- **Live per-client tiles** with the real game video, **click-to-activate**, and per-character **focus hotkeys** (plus cycle-next/prev across a group).
- **Drag to arrange** with snap-to-grid / snap-to-edges; per-character layout & size persist. **Corner-hover resize**, and a choice of **uniform or individual** tile sizing.
- Hover opacity + hover zoom, hide/minimize rules, active-client highlight, and a one-time **Import EVE-O layout**.
- **Bottom label bar** on each tile — an activity label + the ship's **hull type** (e.g. `Logi - Onyx`, `Cyno - Force Recon`), rendered below the video in your chosen colour/size (and collapsed to one value when the two are identical).
- **Damage flash** — tiles pulse red on incoming combat damage, read only from your own EVE Gamelogs (own-logs-only, the same compliance class as the intel firehose).
- **Three modes:** _Off_ · **Eve-O Preview Enhancement** (activity labels drawn on EVE-O Preview's own thumbnails) · **FCPreview** (native tiles).
- View-only and compliant: mirrors your own full client, performs exactly one OS focus-change per click, and uses only your own-account ESI + your own log files — no input is ever sent into a client.

> Native previews need clients running **windowed** or **windowed-fullscreen** (true exclusive fullscreen has no thumbnail for Windows to mirror).

### Fleet Templates
- A ready-made **"Default"** template (Wing _Subcaps_ with DPS / Logi / Special squads, Wing _Caps_ for capitals) now ships **built-in and fully editable**. Nothing is auto-applied — templates apply only when you choose.
- **Auto-Sort → Auto-move:** the continuous-move toggle was renamed and given a tooltip. It stays **off** by default and respects pilots you've dragged into place.

### Doctrine & overlay
- **Per-doctrine ideal-% exemptions** — exclude specific hulls (e.g. Force Recon, capitals) from the composition denominator, with an editor; the Fleet Management panel and MOTD `+X / -Y` deltas use the adjusted percentage.
- Label-rules dialog renamed **Labels** (with field labels and delete buttons for rules/overrides), and informative tooltips added across every preview setting.

### Fixes
- The preview label now shows the **hull type** (Thrasher, Archon…) instead of the pilot's custom ship name.
- Damage flash rebuilt to pulse on **any** incoming damage by default (higher thresholds optional), with a fix for the 2026 client's combat-log format.

---

_Windows: unzip, run `FCTool.exe`, and sign in with the in-app ESI SSO button — as your own character. This is the first release with native previews; feedback and issue reports are welcome._
