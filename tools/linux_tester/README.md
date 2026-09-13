# FCTool Linux preview tester build

Tester build of the Proton/Wine preview backend. Branch
`worktree-linux-x11-previews`, version `5.9.2-linux1`.

This build is a GitHub **pre-release**: it is excluded from `/releases/latest`,
and its own updater stays silent (a suffixed version does not parse), so no
Windows user is ever offered it.

## For the tester

1. **Start EVE first.** Under protontricks the wineserver belongs to whatever
   started it; if FCTool starts first, the prefix dies when FCTool exits.
2. Launch the tester exe in EVE's prefix, the way you already do, e.g.
   `protontricks-launch --appid 8500 FCTool.exe`.
3. In FCTool: FCPreview settings -> enable native previews.
4. Press **"Linux preview diagnostic..."**. The report opens in a dialog and
   is copied to the clipboard.
5. Paste the clipboard into your reply. Attach `fctool.log` as well - the same
   report is written there at INFO.

### What "working" looks like

- Each tile shows LIVE video of its client (not a still, not a black box).
- Clicking a tile focuses that EVE client.
- The preview status line says `Linux/X11 previews (ready)`.

### Known non-goals for this build

- Clients in **exclusive fullscreen** (use windowed / borderless fullscreen).
- `PROTON_ENABLE_WAYLAND=1` - Wine's Wayland driver gives the clients no X11
  window, so there is nothing to mirror. Previews need X11/Xwayland, the same
  requirement EVE Preview Manager has. The report says `NO-X-WINDOW`.
- **Proton 7 or older** (its Steam runtime ships Python 3.7; the helper needs
  3.9+). The report's `[host]` line lists the interpreters that were found.

### What the report contains

Seven sections, plain ASCII, under 4 KB:
`[fctool]` version/build, `[host]` Wine + launch kind + DISPLAY + interpreter
candidates, `[helper]` helper state and X extension versions, `[clients]` one
line per EVE window (title, X id or `NO-X-WINDOW`, viewable, depth, size),
`[probe]` 5-second damage/composite counts per client, `[stats]` frame and
error counters, `[last_error]`.

It deliberately carries **no** auth token, **no** XAUTHORITY path (only
yes/no) and **no** client command line.

## For the maintainer (build recipe)

1. `git worktree` on `worktree-linux-x11-previews`; copy `tests/` in;
   `git merge --ff-only master`.
2. Set the suffixed version in `app_version.py`
   (`APP_VERSION = "5.9.2-linux1"`). **Never merge the suffix to master.**
3. `py -3.12 tools/build_x11helper_zip.py` (deterministic vendor zip).
4. Build with the existing spec: `py -3.13 -m PyInstaller FCTool.spec`.
5. `gh release create v5.9.2-linux1 --prerelease --title "Linux preview tester build" FCTool.zip`
   - never `--latest`, and do not touch the release-log memory for a tester
   build.
6. Each fix round ships as a new pre-release with a bumped suffix
   (`-linux2`, ...).
