"""FC HUD engine: what each info tile says, and when it says it.

``info_tile.py`` is content-agnostic window furniture (drag / resize / snap /
strip). THIS module is everything else: the tile registry, the pure adapters
that turn FCTool's existing feeds into view models, the one background worker
(gate-jump distances), the controller that owns tile lifecycle + layout
persistence, and the settings popup.

    fc_gui  ->  HudHost seams  ->  InfoTileController  ->  renderers  ->  tiles

Nothing here imports ``fc_gui``: every host-side value arrives through the
``HudHost`` dataclass, which is what keeps this testable without booting a GUI
and what keeps fc_gui's side of the feature down to wiring.

LOAD-BEARING RULES (each one is a scar, not a preference):

- **The settings popup must NEVER ``grab_set()``.** A Tk grab is
  application-wide, so it silently deafens the FCPreview tiles -- their
  click-to-switch is a plain ``<Button-1>`` on another Toplevel of this same
  process -- while DWM keeps their thumbnails animating, so the symptom reads
  as "the previews froze" and never as "that window did it" (v4.1.0 shipped
  exactly this). The popup is therefore REFERENCE class: non-modal,
  singleton-guarded, ``<Escape>``-closable, live-applying.
  ``ui_helpers.make_modal`` is deliberately not used -- its default contract
  grabs.
- **Tooltips attach ONCE per widget and are re-worded with
  ``update_tooltip``.** ``attach_tooltip``'s three binds use ``add="+"``, so a
  panel that re-attached on every repaint would leak a handler set per second.
  **Every renderer-side attach passes ``topmost=True``** (``_CompGrid``,
  ``_CoverageCell``) -- the tile that hosts them is ``HWND_TOPMOST``, and a tip
  with no topmost handling of its own is created BELOW it at the pointer
  position (owner-reported 2026-08-02: hovering a fleet row showed nothing).
  Settings-popup attaches (``_grid_row``, ``_check``, the button row) leave the
  default False -- that window is a normal Toplevel, and an always-topmost tip
  there would float over OTHER applications too.
- **The battle tile never claims freshness and carries no delay caption.**
  Every string ``battle_lines`` composes is held to
  ``battle_ledger.assert_no_live_language`` (test-enforced), and the view's
  ``stamp_line`` is deliberately NOT rendered (owner directive 2026-07-30:
  players know killboard data trails a fight; the tile shows the numbers).
- **The links line is NOT gated by the ESI fleet state.** Command-burst
  charges are parsed out of FLEET CHAT, so the coverage report is real with no
  fleet boss and no ESI at all. The fleet tile therefore renders its links
  line underneath whatever the comp rollup's gate decided -- including "Not
  fleet boss".
- **The fleet tile is a GLANCE surface, not the Fleet tab.** It shows
  abbreviated comp buckets in two columns and fleet-wide burst COVERAGE on one
  line. The per-pilot charge rows, the not-boss caveat and the overflow
  summary stay on the Fleet tab's Specialized Roles area, which has the width
  for them (owner directive 2026-08-02, after live smoke: "for how small the
  preview window is, there is probably no room to list the individual link
  pilots").
- **It is sized to fit a PREVIEW-sized window** -- 160x136, the owner's own
  ``preview.tile_w``/``tile_body_h`` -- with every section present: header,
  seven bucket rows (top 6 + Other), four coverage icons. The arithmetic
  behind every font and pad is written out under "fleet tile metrics" below
  and pinned by tests, because the failure mode is silent: a section that
  does not fit is not drawn small, it is not drawn at all (see
  ``_LinksPanel``).
- **Intel fails OPEN.** A line whose distance is unknown is SHOWN with ``?j``;
  hiding a possibly-adjacent hostile report is the dangerous direction. A line
  naming no system never reaches this tile, and with no reference system the
  tile shows the unset notice and filters nothing rather than becoming a
  firehose.
- **The intel tile reads NEWEST FIRST and is fitted to its own window.** These
  are one rule, not two (owner report 2026-08-15: "the intel log also goes off
  the tile screen and the top should be the newest intel, not the bottom").
  The body CLIPS -- see the propagation guard below -- so the order decides
  WHICH rows get thrown away, and newest-last threw away exactly the lines an
  FC opens the tile for. On top of the ordering, ``IntelRenderer`` packs only
  the rows the frame's CURRENT height holds and ellipsizes each row's FREE TEXT
  to its CURRENT width, so the trailing ``(2j)`` badge -- the most
  decision-relevant token on the row, and the rightmost -- survives a long
  line. Every fit degrades towards showing MORE: an unmeasured width or
  height falls back to the old pack-them-all/no-truncation behaviour, whose
  worst case is the clip, never a blank tile.
- **The resolver is the only worker and never touches Tk.** Results reach the
  UI exclusively through the injected ``post_ui``.
- **No per-tick ``place()``.** ``InfoTileWindow.place`` routes through
  ``SetWindowPos(HWND_TOPMOST)`` -- it IS a retop, and a per-tick retop is the
  measured DWM-thrash / in-game FPS bug. Tiles are placed at spawn, on
  ``arrange``/``reset_layouts``, and by their own drags. Nowhere else.
- **The HUD follows EVE on and off screen, EDGE-TRIGGERED.** The tiles are
  WITHDRAWN (never destroyed -- geometry, config and content survive) while
  the host's ``should_show`` seam says no, and mapped again when it says yes.
  fc_gui owns that predicate and ANDs two conditions -- an EVE client window
  exists, AND the foreground window belongs to EVE or to FCTool -- so the
  tiles vanish both when the last client closes and when the owner alt-tabs
  to another application. Only the transition does work: the controller
  tracks the answer itself and calls ``show()``/``hide()`` only on a change.
  NOT because a level-triggered "show every beat" would retop -- it would
  not: ``InfoTileWindow.show()`` already early-returns on its own
  ``_visible`` flag, so a steady-state call is a cheap no-op on its own.
  Edge-triggering exists so the transition is explicit and independently
  testable, instead of resting correctness on ``show()``'s idempotence guard
  staying intact. On the reveal edge each tile is rendered before ``show()``
  maps it, so the first frame on screen is already fresh, not whatever was
  there before the tiles were withdrawn.
  The seam is FAIL-VISIBLE -- absent, non-callable or raising all read as
  "show", because a HUD hidden by a wiring bug cannot be asked back from the
  overlay itself.
- **A tile is also gated on its own SUBJECT being live** (owner request
  2026-08-03: "the tiles should be persistent ... as long as it makes sense
  to be there, IE there is a fleet open or an active battle"). The fleet tile
  is on screen while there IS a fleet, the battle tile while the ledger's own
  view says a fight is showing, the intel tile always. This is the SECOND
  visibility layer and it sits UNDER the global one; both are edge-triggered
  and both are pure screen state -- **relevance NEVER writes config.** The
  memory the owner asked for is the config the feature already had: a tile
  the owner enabled stays enabled across restarts, and only its ✕ or its
  checkbox turns it off. So a fleet tile enabled once comes back by itself
  the next time a fleet exists, with no click. A relevance-hidden tile keeps
  COLLECTING (the intel deque, the distance cache) and simply never renders
  -- a beat that maps to nothing on screen is a beat that writes nothing at
  all. Same reveal discipline as the global layer: render, THEN ``show()``.
  Same fail-visible direction too, but only for the "could not ask" case: a
  seam that RAISES or is not callable reads as relevant, while a seam that
  ANSWERS "no fleet" / "no ledger" hides -- that answer is the whole feature.
- **Log strings stay ASCII** (this box's console is cp1252 and a stray glyph
  raises inside logging's StreamHandler). Tile TEXT may use anything -- it is
  never logged.
"""
from __future__ import annotations

import logging
import os
import queue
import textwrap
import threading
import tkinter as tk
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass, field, replace
from datetime import datetime
from tkinter import font as tkfont
from tkinter import ttk

import battle_ledger as bl
import battle_ledger_panel as blp
import command_bursts as cb
import intel_stream
import preview_layout
import system_coords
import ui_theme
from app_path import bundle_dir
from info_tile import InfoTileWindow
from ui_helpers import attach_tooltip, update_tooltip

log = logging.getLogger(__name__)

# ── palette + type ──────────────────────────────────────────────────────────
# Every value is a REFERENCE to ui_theme's object, never a re-typed hex literal
# (the palette guard asserts import identity).
PALETTE = {
    "BG_DARK": ui_theme.BG_DARK,
    "BG_PANEL": ui_theme.BG_PANEL,
    "BG_ENTRY": ui_theme.BG_ENTRY,
    "FG_TEXT": ui_theme.FG_TEXT,
    "FG_DIM": ui_theme.FG_DIM,
    "FG_ACCENT": ui_theme.FG_ACCENT,
    "FG_GREEN": ui_theme.FG_GREEN,
    "FG_RED": ui_theme.FG_RED,
    "FG_ORANGE": ui_theme.FG_ORANGE,
    "FG_YELLOW": ui_theme.FG_YELLOW,
    "BORDER_COLOR": ui_theme.BORDER_COLOR,
}

# Fonts are plain tuples, never tkfont.Font objects: a Font belongs to the
# interpreter that made it, so a module-level one breaks across roots.
_FONT_ROW = ("Consolas", 8)

# ── fleet tile metrics ──────────────────────────────────────────────────────
# The fleet tile is the only one that must carry a header, up to seven bucket
# rows (top 6 + Other) in two columns AND the four-icon coverage strip inside
# a window the size of a PREVIEW tile -- the owner runs 160x116+20 (config
# `preview.tile_w` / `tile_body_h`, uniform), which is what everything below
# is measured against.
#
# ROUND 5 (owner: "increase the font a bit"): the fleet-scoped font moved back
# up to the SAME Consolas 8 the battle/intel pool uses -- it is still a
# SEPARATE named constant (zero label padding, spacing coming from the
# geometry manager instead), but the point size no longer differs. The fit
# now comes from a smaller bucket cap (8 -> 6) and a shorter worst-case status
# string (FLEET_STATUS_NOT_BOSS), not from a smaller font.
#
# MEASURED 2026-08-03 (96 dpi / tk scaling 1.333, tight labels: bd=0, padx=0,
# pady=0, highlightthickness=0):
#
#   Consolas 8      -> 6 px per character, 13 px line height (row AND bold
#                      head alike -- bold does not widen this font at this
#                      size)
#   burst icon      -> 21x21 (25x25 with a default Label's border)
#   check/cross     -> 13x13 at Consolas 8 bold, tight (19x19 at the old
#                      Consolas 8 WITH the default label border -- the
#                      pre-round-4 number this stays well under)
#
# HEIGHT, worst case (6 buckets + Other = 4 grid rows), against the 116 px body:
#   head        13 + 1 pady               = 14
#   comp grid   4 rows x (13 + 1 pady)    = 56
#   links row   21 (icon) + 2 pady        = 23
#                                    total  93   -> 23 px of slack
#   The standard 6-bucket board (no overflow) is 3 grid rows: 79 -> 37 px of
#   slack.
#
# WIDTH, against the 160 px body:
#   comp cell   80 px column (two uniform halves) - 2 padx = 78 available;
#               FLEET_ROW_CHARS=12 x 6 px         = 72     ->  6 px of slack
#               (13 chars would be 78 -- exactly zero slack; 12 keeps a
#               margin, same spirit as the rest of this budget)
#   links row   4 x (21 icon + 13 mark + 3 gap)   = 148 in 156 available
#               (row padx 2)                              ->  8 px of slack
#               (measured total strip request 152 against the 160 px body)
#   the widest status string is now "No fleet data yet" (17 chars) = 102 px.
#   FLEET_STATUS_NOT_BOSS itself was shortened from 29 to 14 chars ("Not
#   fleet boss") specifically so the head could move to Consolas 8 -- at 29
#   chars it would ask for 174 px against ~156 available, which is what held
#   this font at 7 through round 4.
_FLEET_FONT_ROW = ("Consolas", 8)
_FLEET_FONT_HEAD = ("Consolas", 8, "bold")
#: Zero-padding label options. Spacing is the geometry manager's job here, so
#: the arithmetic above stays readable -- a Label's own default border (2 px)
#: and padx/pady (1 px) would add 6 px to EVERY row and 4 px to every icon.
_FLEET_TIGHT = {"bd": 0, "padx": 0, "pady": 0, "highlightthickness": 0}
#: Comp-cell padding, inside the 80 px half-column.
_FLEET_CELL_PADX = (2, 0)
_FLEET_CELL_PADY = (0, 1)
#: The coverage strip's own inset from the tile edge, and the gap between two
#: discipline cells.
_LINKS_ROW_PADX = 2
_LINKS_ROW_PADY = (2, 0)
_LINKS_CELL_GAP = 3
#: Weight of the empty gutter column that follows the discipline cells. It has
#: no content, so it can only ever take SPARE width -- which keeps the strip a
#: strip on a wide tile instead of spreading four icons across 380 px -- and it
#: has nothing to give back when the row is too narrow, so a deficit still
#: falls on the equal-weight discipline columns and shrinks them together.
_LINKS_GUTTER_WEIGHT = 1000
#: One comp cell's character budget (count + space + bucket), sized to the
#: 78 px available inside a half-column of a 160 px tile at 6 px/char (leaves
#: 6 px of slack -- 13 chars would exactly fill it). Longer bucket names are
#: ellipsized rather than left to be cut off mid-glyph by the grid -- the
#: count is never the part that gets dropped.
FLEET_ROW_CHARS = 12
#: Right-aligned field for the count. Three digits covers a 256-pilot fleet
#: several times over; a wider count simply spends the label's budget.
FLEET_COUNT_CHARS = 3
FLEET_ELLIPSIS = "…"

MIN_TILE_H = InfoTileWindow.MIN_H     # alias; info_tile owns the height floor

# ── config contract (spec section 7) ────────────────────────────────────────
DEFAULT_OPACITY = 0.92
# Below this a tile is neither readable nor findable, and the user's only way
# back is the settings popup they may not realise did it.
MIN_OPACITY = 0.2
DEFAULT_MAX_JUMPS = 5
MAX_JUMPS_CEILING = 30

# How many intel lines the controller keeps (its OWN deque -- fc_gui's
# _intel_buffer is never read and never mutated) and how many a tile shows.
# INTEL_SHOW is the CEILING, not the count: a tile shorter than twelve rows
# shows what fits (``intel_rows_that_fit``), because a row packed below the
# body is a row the propagation guard clips away unseen.
INTEL_KEEP = 50
INTEL_SHOW = 12
#: The glyph an ellipsized intel line ends its free text with. Same character
#: as the comp cell's ``FLEET_ELLIPSIS``, kept under its own name because the
#: two budgets are measured in different units -- PIXELS here (against the real
#: font), characters there.
INTEL_ELLIPSIS = "…"
#: Fewest glyphs an ellipsized STATUS notice may keep and still be worth
#: printing. A row's ellipsis eats only free text -- the stamp, system and jump
#: badge survive any width -- but a status line is one indivisible sentence, so
#: a narrow enough tile would cut it to "No…", which identifies nothing. Below
#: this floor the tile hands over the WHOLE notice and lets the frame's clip
#: take it: same direction as every other degrade here (clipped beats
#: truncated into uselessness), and the reader at least sees the opening words.
#: A floor, not a routine path -- the tile's own MIN_W of 120 px still leaves
#: 106 px, about 17 glyphs of Consolas 8.
INTEL_STATUS_MIN_CHARS = 12
#: Clicking an intel row raises a transient pop-up carrying the WHOLE report --
#: the answer to the fit above, which cuts the free text so the jump badge
#: survives (owner ask 2026-08-15). Two numbers, both deliberate:
#:
#: * how long it holds before fading. "Briefly" was the ask, and this is a
#:   glance at one line the FC just chose to read -- the implant reminder's
#:   12 s is a dock-time nag with a different urgency;
#: * how wide the body wraps, in CHARACTERS. The host's window is
#:   ``client_toast.DEFAULT_W`` (430 px) and its body font is Consolas 9,
#:   measured at 7 px per glyph on this box, leaving 412 px of text once the
#:   1 px accent border and the 8 px padding on each side are gone: 58 glyphs
#:   fit in 406, 60 would overrun. The wrap is CHARACTER-counted rather than
#:   pixel-measured on purpose -- it keeps this module Tk-free, and Consolas is
#:   monospaced, so the two agree.
INTEL_DETAIL_SECONDS = 8.0
INTEL_DETAIL_WRAP = 58
#: Row-pool geometry, MEASURED 2026-08-15 on this box (96 dpi, tk scaling
#: 1.333). ``_LabelPool`` packs each row with `_POOL_PADX` on both sides, and a
#: default ``tk.Label`` spends `_POOL_LABEL_CHROME` more per axis on its own
#: border and padding (bd 2 + pad 1, both sides = 6). Consolas 8 reports a
#: 13 px linespace, so one packed row is 19 px tall and a 200 px body carries
#: ten of the twelve -- which is precisely how the owner's newest two lines
#: went off the bottom of a default-sized tile.
_POOL_PADX = 4
_POOL_LABEL_CHROME = 6

FLEET_MAX_ROWS = 6
OTHER_LABEL = "Other"
#: The comp rollup renders in this many columns (row-major, heaviest first).
#: Two, because the tile is as wide as a preview tile and a single column
#: wasted the right half while pushing the links line off the bottom.
COMP_COLUMNS = 2

# Honest empty states. Each names the ACTUAL gap: the ESI members route is
# fleet-boss-only (it 403s otherwise), so "not boss" is a real answer, not a
# failure to fetch.
FLEET_STATUS_NO_AUTH = "No ESI character"
FLEET_STATUS_NOT_BOSS = "Not fleet boss"
FLEET_STATUS_NO_DATA = "No fleet data yet"

BATTLE_STATUS_NO_LEDGER = "No battle ledger"
INTEL_STATUS_EMPTY = "No intel in range"
# Named blind spot, not a silent empty tile: with nothing to measure from, the
# jump filter cannot mean anything, so the tile says so and shows nothing.
REFERENCE_UNSET = "No reference system - set staging or override"

# Intel keyword spans that mark a line worth the eye even from far away.
# `clear` is deliberately absent: an all-clear is the opposite signal, and it
# gets its own flag below.
PRIORITY_SPAN_KINDS = frozenset({"cyno", "camp", "spike"})

#: The `intel_stream.annotate` span kind that means "this system is EMPTY" --
#: emitted from `intel_monitor.CLEAR_PATTERN`, i.e. `clr` / `clear` / `nv` /
#: `nvi`, the same match that makes `parse_intel_message` classify a line as
#: report_type "clear". Named here so the tile reads the signal off the
#: annotation it already has: the render path stays OFFLINE and imports no
#: monitor (see the AST guard).
CLEAR_SPAN_KIND = "clear"

#: Gate-jump radius inside which an intel line stops being merely traffic and
#: becomes "close enough to act on" -- those rows paint RED (owner request
#: 2026-08-15: "make sure any hostiles flagged within 2 jumps are highlighted in
#: red", corrected 2026-08-16 to mean ANY report, not only a keyworded one --
#: see ``intel_row_colour_key``).
#:
#: This is the SAME number as the FCPreview intel flash's reach
#: (`preview.intel_flash_jumps`, default 2) and the two are siblings BY INTENT
#: -- but deliberately INDEPENDENT constants, not one dial read twice. They
#: answer different questions on different surfaces (a list the FC reads versus
#: a border pulsing on a client window) and can legitimately want different
#: radii; and pointing this tile at a PREVIEW config key would make the HUD's
#: colours depend on the preview subsystem's settings, which is the wrong
#: direction for a dependency. One shared dial is a deliberate follow-up if the
#: owner ever asks for it, never a refactor to do on the way past.
INTEL_DANGER_JUMPS = 2

# Bucket / state -> palette KEY (not a colour): the composers stay pure and
# palette-free, and the renderer resolves the key against its own palette.
_BUCKET_COLOUR = {
    bl.BUCKET_OURS: "FG_RED",
    bl.BUCKET_ALLIES: "FG_ORANGE",
    bl.BUCKET_ENEMIES: "FG_GREEN",
}
_STATE_COLOUR = {
    bl.STATE_FILLING: "FG_YELLOW",
    bl.STATE_SETTLED: "FG_DIM",
    bl.STATE_IDLE: "FG_RED",
}

# Default placement: a block against the primary monitor's top-right.
_ARRANGE_MARGIN = 10
_ARRANGE_GAP = 8
_FALLBACK_BOUNDS = (0, 0, 1920, 1080)


def default_info_tiles_config() -> dict:
    """A FRESH copy of the ``info_tiles`` config block (spec section 7).

    The feature ships dark: the master switch is off AND every tile is off, so
    flipping only the master shows nothing until a tile is chosen (owner
    decision 2026-07-30). Returned by value so a caller mutating the result --
    which is exactly what seeding does -- cannot poison the next call.
    """
    return {
        "enabled": False,
        "lock_layout": False,
        "snap_enabled": True,
        "opacity": DEFAULT_OPACITY,
        "tiles": {
            "battle": {"enabled": False},
            "fleet": {"enabled": False},
            "intel": {"enabled": False, "max_jumps": DEFAULT_MAX_JUMPS,
                      "reference_system": ""},
        },
        "layouts": {},
    }


# ── small guards ────────────────────────────────────────────────────────────

#: "the seam could not be asked at all" -- absent, not callable, or it raised.
#: A sentinel rather than None because None is a perfectly good ANSWER from
#: several seams (no ledger, no location), and the visibility layers have to
#: take opposite directions on the two: an answer of "nothing" hides a tile,
#: an unanswerable seam leaves it up (see ``InfoTileController._relevant``).
_UNKNOWN = object()


def _call(fn, *args, default=None):
    """Call an injected seam, answering `default` if it is missing or raises.

    Host seams reach ESI, Tk and config; one of them failing must cost only its
    own tile, never the beat (the preview provider-isolation lesson)."""
    if not callable(fn):
        return default
    try:
        return fn(*args)
    except Exception:
        return default


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _ellipsize(text: str, max_chars: int) -> str:
    """`text` cut to at most `max_chars` GLYPHS, the ellipsis counted among
    them. ``0`` yields ``""`` (nothing fits). Pure -- no font, no Tk."""
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars == 1:
        return INTEL_ELLIPSIS
    return text[:max_chars - 1] + INTEL_ELLIPSIS


def _fit_to_width(text: str, max_width, measure, compose=None,
                  min_chars: int = 0):
    """The longest ellipsized `text` whose COMPOSED line measures within
    `max_width` PIXELS -- or ``None`` for "do not truncate at all".

    The one width fit on this tile: rows and the status notice differ only in
    what they wrap the fitted text in (`compose`) and how little they are
    willing to keep (`min_chars`). Pure and Tk-free -- `measure(text) -> px` is
    injected, which is what keeps every caller unit-testable without a display.

    ``None`` -- meaning "return the whole line" -- covers EVERY degrade
    direction: an unrealised width, no measurer, nothing to eat, a measurer
    that raises, and a budget too small to leave anything recognisable. An
    un-truncated line is merely clipped by the frame and still says most of
    what it says; a line truncated against a width nobody could measure is
    information destroyed, silently and every second.
    """
    shape = compose if callable(compose) else (lambda body: body)
    width = _as_int(max_width, 0)
    if width <= 1 or not callable(measure) or not text:
        return None
    try:
        if measure(shape(text)) <= width:
            return None
        # Largest character budget whose COMPOSED line still fits. Binary
        # search because each probe is a Tcl round-trip: ~8 for a 200-char
        # line instead of 200, and the common (already-fits) case costs one.
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if measure(shape(_ellipsize(text, mid))) <= width:
                lo = mid
            else:
                hi = mid - 1
        if lo < min_chars:
            return None
        return shape(_ellipsize(text, lo))
    except Exception:
        return None


def _clamp_opacity(value, default=DEFAULT_OPACITY) -> float:
    """Window alpha from config or a typed Spinbox, floored and capped.

    Tk's ``Spinbox(from_=, to=)`` bounds the ARROW BUTTONS ONLY -- typed text
    reaches the variable verbatim -- so every numeric input in this feature is
    clamped at the STORE (the measured zero-width-preview-tile trap)."""
    try:
        amount = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    return min(1.0, max(MIN_OPACITY, amount))


def _clamp_jumps(value) -> int:
    """Jump radius from config or a typed Spinbox, clamped to [0, ceiling].

    Junk resolves to 0 (own system only) rather than to the default: while the
    user is mid-edit the field is briefly empty, and the safe direction for a
    filter is to show LESS, not to widen silently."""
    try:
        n = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0
    return min(MAX_JUMPS_CEILING, max(0, n))


def _system_name(system_id) -> str | None:
    """A system's name from the BUNDLED offline table.

    ``system_coords.get_name`` on purpose, never
    ``zkill_monitor.resolve_name(sid, "solar_system")``: this runs on the Tk
    thread every second and the ESI path does a rate-limited HTTP GET on a
    cache miss."""
    try:
        return system_coords.get_name(int(system_id))
    except Exception:
        return None


def _clock(when=None) -> str:
    """`when` as a local ``HH:MM`` stamp.

    Timestamps in this project are MIXED aware/naive, so an aware value is
    converted to local rather than formatted in whatever zone it arrived in."""
    try:
        if isinstance(when, datetime):
            dt = when.astimezone() if when.tzinfo is not None else when
        else:
            dt = datetime.now()
        return dt.strftime("%H:%M")
    except Exception:
        return datetime.now().strftime("%H:%M")


# ── fleet composition ───────────────────────────────────────────────────────

#: Inventory GROUP name -> the bucket label the tile prints. OWNER-TUNABLE:
#: this table is the whole contract, it is meant to be edited in place, and an
#: entry that is missing simply prints the SDE's own group name (identity
#: fallback) -- so a wrong-looking label is one line away from fixed and a new
#: CCP group can never crash or blank a row.
#:
#: Two entries are deliberate MERGES rather than abbreviations -- several
#: groups sharing one label, whose counts and hull breakdowns aggregate into a
#: single bucket:
#:   * Command Ship + Command Destroyer -> "Links".  An FC counts LINKS, not
#:     hull classes: three Claymores and two Bifrosts are five link ships, and
#:     splitting them across two rows of a six-row tile buys nothing.
#:   * Combat Recon Ship + Force Recon Ship -> "Recon", for the same reason.
#: The hull breakdown behind a merged bucket still names every hull (hover it),
#: so the merge costs no information -- only rows.
#:
#: Keys are the exact ``type_catalog.SHIP_GROUP_NAMES`` values; abbreviations
#: are the widely-used in-game ones (HAC, HIC, T3C, FAX, Blops...), never
#: invented shorthand -- an FC has to read these at a glance mid-fight.
SHIP_GROUP_ABBREV = {
    # -- links + recon (MERGES, see above) --
    "Command Ship": "Links",
    "Command Destroyer": "Links",
    "Combat Recon Ship": "Recon",
    "Force Recon Ship": "Recon",
    # -- sub-capital line --
    "Battleship": "BS",
    "Battlecruiser": "BC",
    "Attack Battlecruiser": "Attack BC",
    "Combat Battlecruiser": "Combat BC",
    "Heavy Assault Cruiser": "HAC",
    "Heavy Interdiction Cruiser": "HIC",
    "Strategic Cruiser": "T3C",
    "Logistics": "Logi",
    "Logistics Frigate": "Logi Frig",
    "Interdictor": "Dictor",
    "Interceptor": "Ceptor",
    "Assault Frigate": "AF",
    "Electronic Attack Ship": "EAF",
    "Covert Ops": "CovOps",
    "Stealth Bomber": "Bomber",
    "Tactical Destroyer": "T3D",
    "Frigate": "Frig",
    "Black Ops": "Blops",
    # -- capitals --
    "Force Auxiliary": "FAX",
    "Dreadnought": "Dread",
    "Lancer Dreadnought": "Lancer",
    "Supercarrier": "Super",
    "Command Carrier": "Cmd Carrier",
    # -- everything else an FC sees on grid --
    "Capsule": "Pod",
    "Mining Barge": "Barge",
    "Jump Freighter": "JF",
    "Blockade Runner": "BR",
    "Deep Space Transport": "DST",
    # -- long identity-fallback names that clip the 280px tile (>=22 chars;
    # verified against the bundled SDE, fit_types.json, which hull(s) each
    # group actually holds): singleton groups take the hull's own in-game
    # name, same as an FC would say it, rather than an invented abbreviation
    # nobody uses; multi-hull groups keep a short generic tag --
    "Capital Industrial Ship": "Rorqual",          # sole member: Rorqual
    "Industrial Command Ship": "Ind Cmd",          # Orca, Porpoise
    "Expedition Command Ship": "Odysseus",         # sole member: Odysseus
    "Prototype Exploration Ship": "Zephyr",        # sole member: Zephyr
    "Special Edition Yachts": "Yacht",             # five named yacht hulls
}


def bucket_label(group_name) -> str:
    """The tile's label for one inventory group. Pure, table-driven.

    An unmapped group answers ITSELF: a table that has not caught up with the
    SDE prints a long name, which is a cosmetic problem. Inventing an
    abbreviation, or dropping the row, would not be."""
    name = str(group_name or "").strip()
    if not name:
        return OTHER_LABEL
    return SHIP_GROUP_ABBREV.get(name, name)


@dataclass(frozen=True)
class FleetCompModel:
    """What the fleet tile draws.

    `status` is "" when there are real numbers behind it, and otherwise names
    the gap (no character / not boss / no snapshot). `total` is 0 whenever
    `status` is set: a gated tile must not show a fleet size it cannot vouch
    for.

    Each row is ``(bucket_label, count, hull_breakdown)`` -- count-descending,
    top 6 plus an ``Other`` row that always sorts last -- where `bucket_label`
    is ``SHIP_GROUP_ABBREV``'s compact name for the inventory group (several
    groups may MERGE into one bucket) and `hull_breakdown` is
    ``((hull_name, count), ...)``, also count-descending, summing to the row's
    count. The breakdown feeds the hover tooltip (``fleet_group_tip``); plain
    tuples rather than dicts so the whole model is frozen, hashable and
    diffable by ``==`` on the render path.

    `links` is the command-burst COVERAGE report (``LinksVM``) or None when
    there is nothing tracked -- a separate, nested-frozen component so the
    renderer's ``==`` diff still covers it, and so a fleet snapshot the ESI
    gate rejects can still carry one: charge tracking is CHAT-sourced and owes
    ESI nothing (see ``build_links_model``)."""
    status: str
    total: int
    rows: tuple
    links: "LinksVM | None" = None


_SHARED_CATALOG = None


def shared_type_catalog():
    """A private, lazily-built offline ``TypeCatalog``.

    Only reached when the host did not hand us the app's own catalog
    (``HudHost.type_catalog``) -- fc_gui already owns one, and passing it costs
    nothing while this fallback parses the ~500 KB bundled table a second
    time."""
    global _SHARED_CATALOG
    if _SHARED_CATALOG is None:
        from type_catalog import TypeCatalog
        _SHARED_CATALOG = TypeCatalog()
    return _SHARED_CATALOG


def _catalog_entry(catalog, type_id):
    """One raw entry from a TypeCatalog's merged bundled-SDE + resolved-cache
    table, or None.

    Reads ``_by_id`` DIRECTLY, exactly as ``fc_gui._ledger_ship_group`` does
    and for the same reason: the public ``resolve_name``/``group_of`` fall
    through to ``_resolve_unknown`` on a miss, which does ``rate_limit("esi")``
    plus an HTTP GET. That is intolerable here -- this runs on the Tk thread,
    once per distinct hull, at 1 Hz, and an ESI outage would turn a five-second
    timeout per unknown hull into a frozen UI (the 1.15 s stall class, only
    worse). Every step is guarded: a catalog that is missing, of the wrong
    shape, or mid-construction answers None rather than raising into a repaint.
    """
    by_id = getattr(catalog, "_by_id", None)
    if not isinstance(by_id, dict):
        return None
    try:
        entry = by_id.get(int(type_id))
    except (TypeError, ValueError, OverflowError):
        return None
    return entry if isinstance(entry, dict) else None


def offline_type_name(type_id, catalog=None) -> str | None:
    """Hull display name from the bundled SDE table, or None. Never networks."""
    entry = _catalog_entry(catalog if catalog is not None
                           else shared_type_catalog(), type_id)
    name = entry.get("n") if entry else None
    return name if isinstance(name, str) and name else None


def offline_group_name(type_id, catalog=None) -> str | None:
    """Inventory GROUP name for a hull ("Battleship", "Logistics"), from the
    bundled table, or None. Never networks.

    Deliberately NOT ``ship_classes.get_group_name``: that resolves through two
    rate-limited ESI hops on a cold cache, and this is Tk-thread work. An
    unmapped hull answers None and lands in the tile's "Other" bucket -- the
    same honest degradation the battle ledger's composition already accepts.
    """
    entry = _catalog_entry(catalog if catalog is not None
                           else shared_type_catalog(), type_id)
    group_id = entry.get("g") if entry else None
    if not isinstance(group_id, int):
        return None
    from type_catalog import SHIP_GROUP_NAMES
    return SHIP_GROUP_NAMES.get(group_id)


def _hull_name(type_id, resolver) -> str:
    """A displayable hull name, or ``Type {id}``.

    ``zkill_monitor.resolve_name`` answers ``str(id)`` when ESI cannot resolve
    a type, so a bare id is an UNRESOLVED answer, not a name -- rendering it
    raw would read as a hull called "606"."""
    name = _call(resolver, type_id)
    name = str(name).strip() if name else ""
    if not name or name == str(type_id):
        return f"Type {type_id}"
    return name


def _breakdown(counter: Counter) -> tuple:
    """``((hull, count), ...)`` count-desc, ties alphabetical (stable across
    repaints, which a tooltip that re-words every second needs)."""
    return tuple(sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])))


def build_fleet_comp_model(members, ship_counts, total,
                           authenticated: bool, fleet_id, is_boss: bool,
                           resolve_type_name=None,
                           resolve_group_name=None) -> FleetCompModel:
    """Roll a fleet snapshot up into the tile's view model. Pure.

    `members` is accepted (and unused) because the snapshot fc_gui publishes is
    ``(members, ship_counts, total)`` and a future tile may want it; taking the
    whole tuple keeps the host seam honest rather than teaching fc_gui to
    unpack for us.

    Both resolvers are injectable so the adapter is testable in isolation; the
    defaults are the OFFLINE bundled-SDE lookups, because this runs on the Tk
    thread once a second (see ``_catalog_entry``).
    """
    if not authenticated:
        return FleetCompModel(FLEET_STATUS_NO_AUTH, 0, ())
    if not fleet_id or not is_boss:
        # The ESI members route is boss-only (403 otherwise), so this is an
        # answer about the world, not a failure to fetch.
        return FleetCompModel(FLEET_STATUS_NOT_BOSS, 0, ())
    if not isinstance(ship_counts, dict) or not ship_counts:
        return FleetCompModel(FLEET_STATUS_NO_DATA, 0, ())

    name_of = resolve_type_name or offline_type_name
    group_of = resolve_group_name or offline_group_name

    groups: dict[str, Counter] = {}
    for type_id, count in ship_counts.items():
        n = _as_int(count, 0)
        if n <= 0:
            continue
        group = _call(group_of, type_id)
        group = str(group).strip() if group else ""
        groups.setdefault(group or OTHER_LABEL, Counter())[
            _hull_name(type_id, name_of)] += n

    # The merge layer, deliberately AFTER the group rollup and BEFORE the sort:
    # buckets compete on their MERGED weight, so "Links" (Command Ships +
    # Command Destroyers together) can out-rank a group that beat either of
    # them alone -- and the top-6 cut sees the same numbers the tile prints.
    buckets: dict[str, Counter] = {}
    for group, counter in groups.items():
        buckets.setdefault(bucket_label(group), Counter()).update(counter)

    other = buckets.pop(OTHER_LABEL, Counter())
    named = sorted(((g, sum(c.values()), c) for g, c in buckets.items()),
                   key=lambda row: (-row[1], row[0]))
    for _g, _n, counter in named[FLEET_MAX_ROWS:]:
        other.update(counter)

    rows = [(g, n, _breakdown(c)) for g, n, c in named[:FLEET_MAX_ROWS]]
    if other:
        # Other always LAST, whatever its count: it is the remainder, and a
        # remainder that outranks a named group reads as a real class of ship.
        rows.append((OTHER_LABEL, sum(other.values()), _breakdown(other)))
    return FleetCompModel("", _as_int(total, 0), tuple(rows))


def fleet_group_tip(row) -> str:
    """Hover copy for one bucket row: ``75 Machariel, 5 Nestor, 1 Vindicator``.

    Mirrors ``battle_ledger_panel.composition_tip``'s job (what the count was
    actually made of) in this feature's own row shape, and it is what makes a
    MERGED bucket lossless: "Links" hovers "3 Claymore, 2 Bifrost, 1 Vulture".
    Pure, so the wording is testable without a display."""
    try:
        breakdown = row[2]
    except (TypeError, IndexError, KeyError):
        return ""
    return ", ".join(f"{count} {name}" for name, count in breakdown)


def fleet_row_text(row) -> str:
    """`` 81 BS`` -- count right-aligned so the column reads down.

    Capped at ``FLEET_ROW_CHARS``, because a comp cell is half of a tile that
    may be as narrow as a preview window: past that the grid clips the label
    mid-glyph, which reads as a corrupted name rather than a shortened one. The
    COUNT is never what gets cut -- it is the number an FC is actually reading
    -- and the full hull breakdown is one hover away (``fleet_group_tip``)."""
    try:
        prefix = f"{row[1]:>{FLEET_COUNT_CHARS}} "
        label = str(row[0])
    except (TypeError, IndexError, KeyError):
        return ""
    room = FLEET_ROW_CHARS - len(prefix)
    if room < 1:
        return prefix.rstrip()
    if len(label) > room:
        label = label[:room - len(FLEET_ELLIPSIS)] + FLEET_ELLIPSIS
    return prefix + label


# ── links: the command-burst COVERAGE line ──────────────────────────────────
# The Fleet tab's Specialized Roles coverage strip, compacted onto one line of
# the fleet tile: per discipline, the bundled burst icon and a ✓/✗.
#
# WHAT THIS TILE DELIBERATELY DOES NOT CARRY (owner directive 2026-08-02, after
# live smoke on a real client): the per-pilot charge rows, the not-boss caveat
# and the "+N more" overflow. They need width the tile does not have, and the
# Fleet tab keeps every one of them. The tile answers the glance question --
# "are my links up?" -- and the tab answers "who is linking what".
#
# THE DATA IS CHAT-SOURCED. ``charge_tracker`` parses fleet chat, so this line
# is meaningful with no fleet boss and no ESI at all -- which is why it renders
# INDEPENDENTLY of ``FleetCompModel.status``: a tile can honestly say "Not in
# fleet / not fleet boss" and still show coverage underneath.
#
# NOR DOES IT CARRY A CAPTION any more (owner-reported 2026-08-03: "I put in
# shield links and the only other icon that showed was armor"). A "Links:"
# caption plus four default-padded cells wanted 250 px of row; at the owner's
# 160 px tile ``pack`` handed the first two cells their width and the last two
# ZERO -- skirmish and information were not clipped, they were never laid out,
# and nothing said so. The icons are the caption now.

#: full? -> the mark beside a discipline's icon. GUI text only, never logged.
COVERAGE_GLYPH = {True: "✓", False: "✗"}

#: discipline -> the bundled icon's file stem. The SAME four files fc_gui's
#: Specialized Roles strip loads (``assets/bursts/<stem>_21.png``, a
#: pre-rendered LANCZOS downscale of the 64 px master), so the two surfaces
#: cannot drift into different artwork.
BURST_ICON_FILES = {
    cb.SHIELD: "shield",
    cb.ARMOR: "armor",
    cb.SKIRMISH: "skirmish",
    cb.INFORMATION: "info",
}
BURST_ICON_PX = 21


def load_burst_icons(widget) -> dict:
    """``{discipline: tk.PhotoImage | None}`` for `widget`'s interpreter.

    PER-INSTANCE, never module-level, and always ``master=widget``: a
    ``PhotoImage`` belongs to the interpreter that made it, exactly like a
    ``tkfont.Font`` (see the module-level font note), so a cached one would
    break across roots -- and the caller must hold the returned dict for the
    widgets' lifetime or Tk garbage-collects the images out from under them
    (the classic empty-label trap).

    EVERY load is guarded on its own. A missing asset, a ``TclError`` from a
    file Tk cannot decode, or a frozen build whose bundle layout moved answers
    None for that discipline, and the renderer falls back to the two-letter
    text label -- a tile that renders without pictures beats a tile that does
    not render."""
    icons = {}
    try:
        base = os.path.join(bundle_dir(), "assets", "bursts")
    except Exception:
        log.debug("info tiles: no bundle dir for burst icons", exc_info=True)
        base = ""
    for discipline, stem in BURST_ICON_FILES.items():
        image = None
        if base:
            try:
                image = tk.PhotoImage(
                    master=widget,
                    file=os.path.join(base, f"{stem}_{BURST_ICON_PX}.png"))
            except Exception:
                log.debug("info tiles: burst icon %s did not load", stem,
                          exc_info=True)
                image = None
        icons[discipline] = image
    return icons


@dataclass(frozen=True)
class LinkCoverageVM:
    """One discipline's fleet-wide coverage: which discipline (the renderer's
    icon key), its two-letter text fallback label, full/not, and the tooltip
    naming what is missing (or how deep the redundancy runs)."""
    discipline: str
    label: str
    full: bool
    tip: str


@dataclass(frozen=True)
class LinksVM:
    """The whole links line. A tuple of frozen rows, so ``FleetCompModel``
    stays ``==``-diffable on the render path."""
    coverage: tuple = ()


def _links_coverage_rows(coverage) -> tuple:
    """``(LinkCoverageVM, ...)`` in ``DISCIPLINES`` order.

    A discipline the host did not report is OMITTED rather than rendered as a
    fabricated miss -- "no data" and "nobody linked it" are different claims.
    Tooltip wording comes from ``command_bursts.coverage_tip``, the single
    source shared with the Specialized Roles strip
    (``fc_gui._render_coverage_strip``) so the two surfaces read the same."""
    statuses = coverage if isinstance(coverage, dict) else {}
    rows = []
    for discipline in cb.DISCIPLINES:
        status = statuses.get(discipline)
        if status is None:
            continue
        label = cb.DISCIPLINE_LABEL[discipline]
        full = bool(getattr(status, "full", False))
        rows.append(LinkCoverageVM(discipline=discipline, label=label[:2],
                                   full=full,
                                   tip=cb.coverage_tip(status, label)))
    return tuple(rows)


def build_links_model(coverage) -> "LinksVM | None":
    """Roll the charge tracker's coverage up into the tile's links line. Pure.

    `coverage` is ``charge_tracker.coverage()``'s ``{discipline:
    CoverageStatus}``, which fc_gui already computed for the Fleet tab and
    stores for this seam (recomputing it would take the tracker's lock on the
    Tk thread).

    Returns **None** -- the line is then absent and the tile stays compact --
    when NO discipline reports anything present. The host's other three
    ``links_snapshot`` members (the per-pilot rows, their hull names and the
    boss flag) are deliberately not taken: the tile stopped rendering pilots,
    and a builder that accepted arguments it ignores would invite them back.
    """
    rows = _links_coverage_rows(coverage)
    statuses = coverage.values() if isinstance(coverage, dict) else ()
    if not any(getattr(s, "present", ()) for s in statuses):
        return None
    return LinksVM(coverage=rows)


# ── intel ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class IntelEntry:
    """One pushed intel line, annotated once at push time.

    Built by the controller from ``intel_stream.annotate`` and kept in the
    controller's OWN bounded deque. fc_gui's ``_intel_buffer`` is never read
    and never mutated -- this tile is a second consumer of the fan-out, not a
    second owner of the stream.

    `priority` and `is_clear` are both read off the SAME annotation, once, at
    push time -- ``PRIORITY_SPAN_KINDS`` and ``CLEAR_SPAN_KIND`` respectively.
    They are not opposites: a line can carry neither (the commonest hostile
    report of all) and the colour rule treats that case on distance alone."""
    ts: str
    text: str
    system_ids: tuple
    priority: bool
    #: Defaulted so a duck-typed or older-shaped entry stays constructible; the
    #: safe answer for an unknown line is "nobody said it was clear".
    is_clear: bool = False


@dataclass(frozen=True)
class IntelRowVM:
    """One rendered intel row. `jumps` is None when the distance is not (yet)
    known, which renders as ``?j`` -- see the fail-open rule."""
    ts: str
    system: str
    text: str
    jumps: int | None
    priority: bool
    #: Carried from the entry (see ``IntelEntry``) because the colour rule needs
    #: it a second later, on the row, inside the 1 Hz repaint. Defaulted for the
    #: same reason.
    is_clear: bool = False


@dataclass(frozen=True)
class IntelTileModel:
    """The intel tile's whole state: rows plus the named reason there are
    none. Frozen so the renderer can diff it with ``==``."""
    status: str
    rows: tuple


def build_intel_model(entries, distance_of, max_jumps: int,
                      limit: int = INTEL_SHOW,
                      resolve_system_name=None) -> tuple:
    """Filter/annotate pushed intel entries into rows, newest FIRST.

    `distance_of(system_id) -> int | None` is the resolver's cache read.

    ORDER IS A SAFETY PROPERTY HERE, not a preference (owner report
    2026-08-15: "the top should be the newest intel, not the bottom"). The tile
    CLIPS whatever does not fit -- deliberately, because the alternative is a
    window that resizes itself out from under the owner's placement -- so
    whichever end of this list is last is the end that gets thrown away. Newest
    LAST therefore clipped exactly the lines an FC is reading the tile for. The
    entries are still COLLECTED chronologically, so the `limit` slice below
    still selects the most RECENT ones; only the finished rows are reversed.
    ``intel_rows_that_fit`` then trims the tail further to what the tile is
    actually tall enough to show.

    Three rules, in this order:
      * a line naming NO system is not this tile's business (the Intelligence
        tab remains the firehose);
      * a line whose distance is unknown is SHOWN with `jumps=None` -- FAIL
        OPEN, because hiding a possibly-adjacent hostile report is the
        dangerous direction;
      * a line whose KNOWN distance exceeds `max_jumps` is dropped.

    A line naming several systems is judged by its NEAREST known one -- the
    closest threat is the one the filter must not lose.
    """
    name_of = resolve_system_name or _system_name
    ceiling = _clamp_jumps(max_jumps)
    rows = []
    for entry in entries or ():
        system_ids = tuple(getattr(entry, "system_ids", ()) or ())
        if not system_ids:
            continue
        best_id, best_jumps = None, None
        for system_id in system_ids:
            jumps = _call(distance_of, system_id)
            if jumps is None:
                continue
            if best_jumps is None or jumps < best_jumps:
                best_id, best_jumps = system_id, jumps
        if best_jumps is None:
            best_id = system_ids[0]
        elif best_jumps > ceiling:
            continue
        label = _call(name_of, best_id) or str(best_id)
        rows.append(IntelRowVM(ts=entry.ts, system=str(label), text=entry.text,
                               jumps=best_jumps,
                               priority=bool(entry.priority),
                               is_clear=bool(getattr(entry, "is_clear",
                                                     False))))
    cap = _as_int(limit, 0)
    # Slice FIRST (chronological, so the cap keeps the most recent), reverse
    # SECOND (so the tile reads newest-first). Reversing first would keep the
    # OLDEST `cap` entries, which is the bug this ordering exists to fix.
    kept = rows[-cap:] if cap > 0 else rows
    return tuple(reversed(kept))


def _intel_row_parts(row) -> tuple:
    """``(stamp, system, badge)`` -- a row's FIXED tokens, the ones no fit ever
    cuts. Single-sourced because two surfaces compose them: the row itself
    (``intel_row_text``) and the click-through pop-up's title
    (``intel_detail_title``), and a pop-up that badged a clicked row
    differently from the row would be worse than no pop-up. Pure, Tk-free, and
    defensive on every field like its callers."""
    jumps = _as_int(getattr(row, "jumps", None), default=None)
    return (str(getattr(row, "ts", "") or ""),
            str(getattr(row, "system", "") or ""),
            "?j" if jumps is None else f"{jumps}j")


def intel_row_text(row, max_width: int = 0, measure=None) -> str:
    """``12:04  Jita  10 reds gate  (2j)`` -- ``?j`` while unresolved.

    Pure and Tk-FREE by default, which is what keeps the wording unit-testable
    without a display. Given a positive `max_width` in PIXELS and a
    ``measure(text) -> px`` callable -- the renderer injects its real font's --
    the line is fitted to that width by eating the FREE TEXT only. The stamp,
    the system and the jump badge are never what gets cut: the badge is the
    most decision-relevant token on the row and it sits at the far RIGHT, so a
    row left to overrun loses precisely the part worth reading (owner report
    2026-08-15: "the intel log also goes off the tile screen").

    EVERY degrade direction returns the whole line -- no measurer, an
    unrealised width, a measurer that raises. An un-truncated row is merely
    clipped by the frame and still says most of what it says; a row truncated
    against a width nobody could measure is information destroyed, silently and
    every second.

    Defensive on every field, like its list-comprehension partner
    ``intel_row_colour_key``: both run over the SAME row inside the 1 Hz
    repaint, so a duck-typed or junk row that one tolerates and the other
    raises on is a dead tile either way. An unparseable distance reads as
    UNMEASURED here too -- ``?j`` beside the partner's yellow.
    """
    stamp, system, badge = _intel_row_parts(row)
    text = str(getattr(row, "text", "") or "")

    def compose(body: str) -> str:
        # Empty parts drop out rather than leaving a four-space hole, so a row
        # whose free text was eaten entirely still reads as a line.
        return "  ".join(p for p in (stamp, system, body, f"({badge})") if p)

    fitted = _fit_to_width(text, max_width, measure, compose)
    return compose(text) if fitted is None else fitted


def intel_status_text(status, max_width: int = 0, measure=None) -> str:
    """The head/status notice, fitted to the tile the same way a row is.

    Pure and Tk-FREE by default (see ``intel_row_text``). This is the one line
    the row fit did not reach, and it is the LONGEST string the tile ever
    writes: ``REFERENCE_UNSET`` measures 270 px, while a preview-sized 160 px
    tile leaves 146 px -- so the notice explaining why the tile is empty was
    itself clipped mid-sentence (owner report 2026-08-15, "goes off the tile
    screen").

    Unlike a row it has no fixed tail to protect and no free text to spend:
    the whole notice IS the message, so it is ellipsized as a whole -- and only
    while ``INTEL_STATUS_MIN_CHARS`` glyphs still survive. Under that floor the
    whole notice is returned and the frame's clip takes it, because "No…"
    identifies nothing while "No reference system - s…" identifies everything.
    """
    text = str(status or "")
    fitted = _fit_to_width(text, max_width, measure,
                           min_chars=INTEL_STATUS_MIN_CHARS)
    return text if fitted is None else fitted


def intel_detail_title(row) -> str:
    """The clicked row's pop-up TITLE: ``12:04  EC-P8R  (2j)``. Pure, Tk-free.

    Exactly the tokens the tile never cuts (``_intel_row_parts``), so the
    pop-up opens by repeating what the FC clicked on -- identification first --
    and spends its body on the half he could not read. Empty parts drop out
    rather than leaving a hole, the same rule ``intel_row_text`` follows."""
    stamp, system, badge = _intel_row_parts(row)
    return "  ".join(p for p in (stamp, system, f"({badge})") if p)


def intel_detail_body(row) -> str:
    """The clicked row's pop-up BODY: the WHOLE report, wrapped. Pure, Tk-free.

    This is the entire point of the click (owner ask 2026-08-15). The row on
    the tile is ellipsized to protect the jump badge, so the pop-up must lose
    NOTHING -- no ellipsis, no truncation, no re-fitting against a width. Only
    whitespace moves.

    Wrapping happens here rather than in the window because the host's Labels
    do not wrap on their own and a Toplevel placed by ``SetWindowPos`` cannot
    grow to fit its text -- one long line would simply be clipped by the
    window, which is the bug this feature exists to answer. ``INTEL_DETAIL_WRAP``
    documents how that character count matches the host's pixel width.

    Long unbroken tokens (a pasted URL, a 60-character corp tag) are broken
    rather than allowed to overrun: an unreadable wrapped token still shows
    every character, while an overrunning one hides them behind the frame.
    """
    text = str(getattr(row, "text", "") or "")
    if not text:
        return ""
    return textwrap.fill(text, width=max(8, _as_int(INTEL_DETAIL_WRAP, 58)),
                         break_long_words=True, break_on_hyphens=False)


def intel_row_colour_key(row) -> str:
    """One intel row's palette KEY -- never a colour. Pure, Tk-free.

    PROXIMITY IS THE RULE AND "CLEAR" IS THE ONLY EXCEPTION -- a DENYLIST, not
    an allowlist (owner correction 2026-08-16: "are intel reports from less than
    or equal to 2j away being put in red-text ... this should be the case UNLESS
    someone puts 'clear' or 'clr' (or other permutations) which would indicate
    that the system is now empty").

    The shipped rule was the other way round: red required a `cyno`/`camp`/
    `spike` span, so the commonest hostile report of all -- `20 reds gate
    EC-P8R`, which annotates to a count and a system and no keyword whatsoever
    -- rendered in ORDINARY TEXT from the system next door. That is the
    dangerous direction to be wrong in, so the keyword no longer gates red; it
    only earns yellow out past the radius, where the distance says nothing.

    Three states, in escalating order:

    * ``FG_TEXT``   -- an ALL-CLEAR at any distance (``is_clear``: a `clr` /
      `clear` / `nv` / `nvi` span, see ``CLEAR_SPAN_KIND``), or ordinary traffic
      measured beyond the radius. Clear takes precedence over everything below:
      it is the report that the system is EMPTY, and painting it red at two
      jumps would invert its meaning exactly where the FC reads fastest.
    * ``FG_YELLOW`` -- worth the eye but not confirmed close: an UNRESOLVED
      distance, or a ``priority`` (cyno/camp/spike) line further out.
    * ``FG_RED``    -- a KNOWN distance of ``INTEL_DANGER_JUMPS`` gate jumps or
      fewer. Close is close, whatever words the reporter used.

    UNRESOLVED DISTANCE STAYS YELLOW, and that is not a fail-open violation --
    it is a different axis. The tile fails open on VISIBILITY: a line whose
    distance is unknown is still SHOWN and still badged ``?j``
    (``build_intel_model``); here it is still escalated, just not to red. Red
    carries the narrower claim -- *this one is measured close enough to act on*
    -- and spending it on a line nobody has measured would cry wolf until the
    colour stops meaning anything. Nothing is hidden in either direction; only
    the loudness differs.

    Defensive like its neighbours (this runs inside the 1 Hz repaint): a
    duck-typed or junk row degrades to a key the palette really carries rather
    than raising, and an unparseable distance is treated as unmeasured.
    """
    if getattr(row, "is_clear", False):
        return "FG_TEXT"
    jumps = _as_int(getattr(row, "jumps", None), default=None)
    if jumps is not None and jumps <= INTEL_DANGER_JUMPS:
        return "FG_RED"
    if jumps is None or getattr(row, "priority", False):
        return "FG_YELLOW"
    return "FG_TEXT"


def intel_rows_that_fit(height, row_height, limit: int = INTEL_SHOW) -> int:
    """How many intel rows a content frame `height` px tall can show.

    Pure/Tk-free -- the renderer measures and passes the numbers in, so the
    arithmetic is testable without a display.

    Both degrade directions deliberately show MORE rather than fewer:

    * an UNREALISED height (a fresh frame reports 1, and the first draw happens
      before the tile is mapped) or an unmeasurable row answers `limit` -- the
      old pack-them-all behaviour, whose worst case is the clip this function
      exists to aim, never a blank tile;
    * a frame too short for even one row still answers 1. One clipped row is
      readable; zero rows look identical to "no intel", and the owner has no
      way to tell those apart.
    """
    cap = max(0, _as_int(limit, 0))
    px = _as_int(height, 0)
    pitch = _as_int(row_height, 0)
    if cap <= 0:
        return 0
    if px <= 1 or pitch <= 0:
        return cap
    return max(1, min(cap, px // pitch))


def _intel_text_width(frame_width) -> int:
    """Pixels a row's TEXT gets, out of the content frame's current width.

    The pool packs each row with ``_POOL_PADX`` on both sides and the Label
    spends ``_POOL_LABEL_CHROME`` more on its own border/padding. ``0`` means
    "not realised / nothing left", which every caller reads as "measure
    nothing" -- see ``intel_row_text``'s degrade rule.
    """
    width = _as_int(frame_width, 0)
    if width <= 1:
        return 0
    return max(0, width - 2 * _POOL_PADX - _POOL_LABEL_CHROME)


def intel_title(max_jumps: int, reference_label: str, reference_id) -> str:
    """The strip title, carrying the live filter so the tile always says what
    it is hiding: ``Intel (<=5j of P-ZMZV)``. GUI text only -- never logged."""
    if not reference_id:
        return "Intel"
    return f"Intel (≤{_clamp_jumps(max_jumps)}j of {reference_label})"


def resolve_reference(manual_name: str, own_system_id, staging_name: str,
                      resolve_name=system_coords.resolve_name):
    """The intel tile's reference-system ladder: manual -> own location ->
    staging. Returns ``(system_id | None, display_label)``.

    An unresolvable MANUAL override falls THROUGH to the automatic ladder
    rather than blanking the tile: a typo in a settings box must not be able to
    switch the HUD off. Staging is ``zkillboard.staging_system`` (the FC one) --
    the ``market.*`` staging is a different system on this install and is never
    read here. With nothing resolvable the tile is UNCONFIGURED and inert: it
    names the gap and shows nothing, never a silent fallback to some other
    system's neighbourhood.
    """
    manual = str(manual_name or "").strip()
    if manual:
        system_id = _call(resolve_name, manual)
        if system_id:
            return int(system_id), manual
    own = _as_int(own_system_id, 0)
    if own:
        return own, (_system_name(own) or f"System {own}")
    staging = str(staging_name or "").strip()
    if staging:
        system_id = _call(resolve_name, staging)
        if system_id:
            return int(system_id), staging
    return None, REFERENCE_UNSET


# ── battle ──────────────────────────────────────────────────────────────────

def battle_lines(view) -> tuple:
    """The battle tile's ``(text, palette_key)`` lines. Pure.

    Renders the SAME ``BattleLedgerView`` the Fleet-tab panel consumes,
    through the panel's own ``format_row``, so the two surfaces can never
    disagree about a number. What it deliberately does NOT render:

      * ``stamp_line`` / any "behind" caption -- owner directive 2026-07-30:
        the tile shows the numbers with no delay text;
      * the blind-spot notes -- they are paragraphs, and the panel (which has
        a scroll canvas) remains the place that carries them.

    Every string composed here is held to
    ``battle_ledger.assert_no_live_language`` by the tests: a suppressed bucket
    keeps the panel's blank marker rather than a zero, and a hidden ledger
    renders the view's OWN words rather than an invented status.
    """
    if view is None:
        return ((BATTLE_STATUS_NO_LEDGER, "FG_DIM"),)
    if not getattr(view, "visible", False):
        label = str(getattr(view, "state_label", "") or "").strip()
        return ((label or BATTLE_STATUS_NO_LEDGER, "FG_DIM"),)

    lines = []
    title = str(getattr(view, "title", "") or "").strip()
    if title:
        lines.append((title, "FG_ACCENT"))
    state_label = str(getattr(view, "state_label", "") or "").strip()
    if state_label:
        lines.append((state_label,
                      _STATE_COLOUR.get(view.state, "FG_DIM")))
    for row in getattr(view, "rows", ()) or ():
        text = blp.format_row(row, floor_prefix=view.floor_prefix)
        if row.qualifier:
            text = f"{text}  ({row.qualifier})"
        lines.append((text, "FG_DIM" if row.suppressed
                      else _BUCKET_COLOUR.get(row.bucket, "FG_TEXT")))
    fast = getattr(view, "fast", None)
    if fast is not None:
        # Relayed verbatim from the engine's separate fast lane, which carries
        # its own latency label and is the ONE thing battle_ledger exempts from
        # the live-language rule (it really is fast).
        lines.append((str(fast.text), "FG_ACCENT"))
    return tuple(lines)


# ── relevance: is a tile's SUBJECT live right now? ──────────────────────────
# The lower of the two visibility layers (owner request 2026-08-03). Both
# predicates below are PURE -- they judge a seam's ANSWER, never call one --
# so what makes a tile relevant is testable without a controller, a host or a
# display, and the controller's job shrinks to asking and diffing.
#
# Each one answers about the WORLD, and each is deliberately the same source
# the tile's own content comes from, so "the tile is up" and "the tile has
# something to say" can never disagree:
#
#   fleet   the ESI fleet poll (``fleet_state``) reports a fleet id;
#   battle  the battle ledger's OWN view says it is showing (``visible``) --
#           its ARMED/FILLING/SETTLED lifecycle is the authority, and a
#           dismissed or reset ledger hides the tile with it;
#   intel   always -- an intel tile is an FC's standing watch, and there is
#           no "no intel is happening" state to gate on.
#
# What they do NOT decide: whether the tile EXISTS. That is config
# (``tiles.<key>.enabled``), it survives restarts, and only the popup's
# checkbox or the tile's own ✕ clears it.

def fleet_is_live(state) -> bool:
    """Is there a fleet, per the ``fleet_state`` seam's ``(authenticated,
    fleet_id, is_boss)``?

    A fleet id and nothing else. Authentication is IMPLIED -- fc_gui cannot
    learn a fleet id without a token, so an unauthenticated host answers no
    fleet and the tile stays down. Boss-ness is deliberately NOT required: an
    FC who is in someone else's fleet still wants the tile up, where it says
    honestly that it cannot read the roster and still carries the chat-sourced
    links coverage line, which owes ESI nothing.

    TRUTHINESS, not ``is not None``, so this agrees exactly with
    ``build_fleet_comp_model``'s own gate (``if not fleet_id or not is_boss``).
    A 0 slipping through as "a fleet" would put a tile on screen whose comp
    half is gated shut for a fleet that does not exist -- the two gates reading
    the same value differently is the drift this alignment prevents.

    A shape that cannot be unpacked reads as LIVE: that is not the world
    saying "no fleet", it is nobody being able to say, and the fail-visible
    direction is up (see ``InfoTileController._relevant``).
    """
    try:
        _authenticated, fleet_id, _is_boss = state
    except (TypeError, ValueError):
        return True
    return bool(fleet_id)


def battle_is_live(view) -> bool:
    """Is a fight showing, per the ledger's own ``BattleLedgerView``?

    ``view.visible`` and nothing else -- the ledger already owns the whole
    ARMED/FILLING/SETTLED lifecycle, the owner's off switch, the dismiss and
    the reset, and re-deriving any of that here would be a second opinion
    about a question that has an answer. It is the same view the tile then
    renders, so the tile can never be up with nothing in it.

    Note the ONE non-fight case that still counts as showing: the J-space
    blind-spot notice (``_blind_view``) is ``visible=True`` with no numbers,
    because a wormhole fight produces no killmails and the ledger names that
    blind spot rather than hiding it. A tile carrying that notice is the
    ledger being honest, so it belongs on screen too.

    ``None`` -- the seam's own inert answer, and what a host that never wired
    a ledger gives -- is a real "there is nothing to show", so it hides."""
    return bool(getattr(view, "visible", False))


# ── renderers ───────────────────────────────────────────────────────────────

class _LabelPool:
    """A fixed pool of row labels, built once and reused.

    Rebuilding widgets per repaint would churn Tk AND force a tooltip
    re-attach, and ``attach_tooltip`` binds with ``add="+"`` -- re-attaching
    stacks a fresh handler set every time. So pools build once and re-word
    afterwards. (This one carries no tooltips: the surface that needs them is
    the fleet comp, and it is gridded -- see ``_CompGrid``.)

    ``on_click(index) -> str | None`` is bound ONCE per label, at construction,
    and carries the label's INDEX -- never the item that happened to be in it.
    The pool is reused and re-worded every second, so a binding that closed
    over the content would answer with whatever was on screen when it was
    built; the caller resolves the current item at CLICK time. Same
    bind-once discipline as ``_CompGrid``'s tooltips, for the same reason
    (``bind`` with a fresh handler per repaint stacks handlers)."""

    def __init__(self, parent, size: int, palette: dict, on_click=None):
        self._palette = palette
        self._labels = []
        self._packed = []
        bg = palette.get("BG_DARK", ui_theme.BG_DARK)
        for index in range(size):
            label = tk.Label(parent, text="", font=_FONT_ROW, bg=bg,
                             fg=palette.get("FG_TEXT", ui_theme.FG_TEXT),
                             anchor="w", justify="left")
            if on_click is not None:
                label.bind("<Button-1>",
                           lambda _event, i=index: on_click(i))
            self._labels.append(label)
            self._packed.append(False)

    def render(self, items) -> None:
        """`items` is a sequence of ``(text, palette_key)``.

        The pool always packs a PREFIX of itself, in ascending index order, so
        ``pack``'s append-only behaviour is harmless here: a row that gets
        content late is appended BELOW the rows already packed above it, which
        is exactly its slot. (The intel tile now varies this count with the
        tile's HEIGHT as well as with the number of rows, so the property does
        real work -- ``_CompGrid`` documents the same hazard from the other
        side.)"""
        for index, label in enumerate(self._labels):
            if index < len(items):
                text, colour_key = items[index]
                label.configure(
                    text=text,
                    fg=self._palette.get(colour_key,
                                         self._palette.get("FG_TEXT")))
                if not self._packed[index]:
                    label.pack(fill="x", padx=_POOL_PADX)
                    self._packed[index] = True
            elif self._packed[index]:
                label.pack_forget()
                self._packed[index] = False


class _CompGrid:
    """The comp rollup's cells: a fixed pool of labels in a `columns`-wide
    grid, filled ROW-MAJOR so the heaviest bucket reads top-left.

    It owns its own frame, for the same reason ``_LinksPanel`` owns one -- the
    renderer's containers are packed at construction and content can then only
    move WITHIN a container (``pack`` appends).

    Two disciplines:

    * **the tooltip attaches ONCE.** ``attach_tooltip`` binds with ``add="+"``,
      so a pool that re-attached per repaint would stack a fresh handler set
      every second; cells are re-worded with ``update_tooltip`` instead.
    * **every ``grid()`` call passes the FULL option set.** Tk RETAINS the
      options it is not given across a re-grid (``grid_forget`` discards
      them) -- the stale-columnspan class of bug. A cell's slot here is a pure
      function of its pool index so it never actually moves, but the rule is
      cheap to keep and expensive to rediscover.

    Unlike a packed pool, a cell that first gets content LATE still lands in
    its own slot: grid placement is absolute, so the lazy-first-pack ordering
    hazard simply cannot arise inside this container."""

    def __init__(self, parent, palette: dict, size: int,
                 columns: int = COMP_COLUMNS, font=_FLEET_FONT_ROW):
        self._palette = palette
        self._columns = max(1, int(columns))
        bg = palette.get("BG_DARK", ui_theme.BG_DARK)
        self.frame = tk.Frame(parent, bg=bg)
        for column in range(self._columns):
            # Equal halves (uniform), so the second column starts at a fixed x
            # and both columns of counts read down instead of wandering with
            # whatever the widest label happens to be this second.
            self.frame.columnconfigure(column, weight=1, uniform="comp")
        self._labels = []
        self._shown = []
        for _ in range(max(0, int(size))):
            # Zero label padding (see _FLEET_TIGHT): row pitch is set by the
            # grid's pady, which is the number the height budget is written in.
            label = tk.Label(self.frame, text="", font=font, bg=bg,
                             fg=palette.get("FG_TEXT", ui_theme.FG_TEXT),
                             anchor="w", justify="left", **_FLEET_TIGHT)
            # topmost=True: this cell lives inside a HWND_TOPMOST tile -- a
            # tip with no topmost handling of its own is stacked BELOW the
            # tile at the pointer position (created, never seen).
            attach_tooltip(label, "", topmost=True)
            self._labels.append(label)
            self._shown.append(False)

    def slot(self, index) -> tuple:
        """``(row, column)`` for pool index `index`. Row-major."""
        return divmod(int(index), self._columns)

    def render(self, items) -> None:
        """`items` is a sequence of ``(text, palette_key, tip_or_None)``."""
        for index, label in enumerate(self._labels):
            if index < len(items):
                text, colour_key, tip = items[index]
                label.configure(
                    text=text,
                    fg=self._palette.get(colour_key,
                                         self._palette.get("FG_TEXT")))
                update_tooltip(label, tip or "")
                if not self._shown[index]:
                    row, column = self.slot(index)
                    label.grid(row=row, column=column, sticky="w",
                               padx=_FLEET_CELL_PADX, pady=_FLEET_CELL_PADY)
                    self._shown[index] = True
            elif self._shown[index]:
                label.grid_forget()
                self._shown[index] = False


class _CoverageCell:
    """One discipline on the links line: its burst icon (or the two-letter
    text fallback) plus a ✓/✗ mark, both hovering the SAME tooltip.

    Icon-or-text is decided ONCE, at construction, from whatever
    ``load_burst_icons`` managed to load -- a cell never changes its mind
    mid-session, so there is no swap path to get wrong. When there is no
    icon, the fallback text comes from ``LinkCoverageVM.label`` at render
    time -- the VM is already the single source for that two-letter code
    (``_links_coverage_rows`` derives it from ``command_bursts.
    DISCIPLINE_LABEL`` once); re-deriving it here a second time from the
    discipline this cell happens to be built for is exactly the drift a
    single-source field exists to prevent. The image reference is held on
    the instance as well as in the panel's cache: a ``PhotoImage`` that only
    Tk still points at gets collected, and the label silently goes blank."""

    def __init__(self, parent, palette: dict, icon):
        self._palette = palette
        bg = palette.get("BG_DARK", ui_theme.BG_DARK)
        fg = palette.get("FG_TEXT", ui_theme.FG_TEXT)
        self.frame = tk.Frame(parent, bg=bg)
        self.icon_image = icon              # strong ref -- see the docstring
        # _FLEET_TIGHT on both halves: a default Label border alone put 4 px
        # around each 21 px icon and 8 px around each mark, which is 24 px of a
        # 160 px row spent on nothing -- and this row had none to spare.
        if icon is not None:
            self.icon = tk.Label(self.frame, image=icon, bg=bg, **_FLEET_TIGHT)
        else:
            self.icon = tk.Label(self.frame, text="", font=_FLEET_FONT_HEAD,
                                 bg=bg, fg=fg, **_FLEET_TIGHT)
        self.icon.pack(side="left")
        self.mark = tk.Label(self.frame, text="", font=_FLEET_FONT_HEAD, bg=bg,
                             fg=fg, **_FLEET_TIGHT)
        self.mark.pack(side="left")
        for widget in (self.icon, self.mark):
            # Same HWND_TOPMOST-tile reasoning as _CompGrid above.
            attach_tooltip(widget, "", topmost=True)

    def render(self, row) -> None:
        if self.icon_image is None:
            self.icon.configure(text=row.label)
        self.mark.configure(
            text=COVERAGE_GLYPH[bool(row.full)],
            fg=self._palette.get("FG_GREEN" if row.full else "FG_RED"))
        for widget in (self.icon, self.mark):
            update_tooltip(widget, row.tip or "")


class _LinksPanel:
    """The links line: its own container, one row, one cell per discipline.

    **The cells are GRIDDED into equal-weight uniform columns, not packed.**
    That is the fix for the owner-reported missing icons (2026-08-03), and it
    is structural rather than a matter of having found smaller numbers: when a
    packed row runs out of cavity the LAST slaves silently get zero width and
    are never laid out at all, so at 160 px the shield and armor cells looked
    perfectly healthy while skirmish and information simply did not exist. A
    grid with equal weights shrinks EVERY column together instead, so a row too
    narrow for its content loses a few pixels off each mark rather than losing
    whole disciplines -- and a fifth discipline, if EVE ever grows one, would
    narrow the four rather than fall off the end.

    TWO ordering hazards, both of the "``pack`` APPENDS" family:

    * against the comp grid -- this panel lives in its OWN container frame,
      packed at construction, so a comp bucket that first appears mid-session
      can never land under it;
    * inside the row -- a discipline the host never reported is omitted (a
      fabricated ✗ would be a lie), so the cell SET can change. Rather than
      track which cell may follow which, ``_relayout`` re-grids the row IN
      ORDER whenever the laid-out set changes and does nothing at all when it
      does not. The set changes far less often than the glyphs do.

    The whole row is hidden when no discipline reports anything: an empty strip
    of nothing is worse than no line at all. There is no caption -- see the
    section comment above.
    """

    def __init__(self, parent, palette: dict, icons=None):
        self._palette = palette
        bg = palette.get("BG_DARK", ui_theme.BG_DARK)
        self.frame = tk.Frame(parent, bg=bg)
        self.frame.pack(fill="x")
        self.row = tk.Frame(self.frame, bg=bg)
        #: PER-INSTANCE (a PhotoImage belongs to one interpreter) and held for
        #: the cells' whole lifetime -- see ``load_burst_icons``.
        self.icons = (load_burst_icons(self.frame) if icons is None
                      else dict(icons))
        self._cells = {d: _CoverageCell(self.row, palette, self.icons.get(d))
                       for d in cb.DISCIPLINES}
        self._layout: list = []
        self._packed = False
        self.frame.bind("<Destroy>", self._release_icons, add="+")

    def _release_icons(self, _event=None) -> None:
        """Drop the images on the TK THREAD, while the interpreter is alive.

        ``PhotoImage.__del__`` calls back into Tcl (``image delete``) and
        catches only ``TclError`` -- so an image collected on a WORKER thread
        raises ``RuntimeError: main thread is not in main loop`` and prints an
        ignored-exception traceback. This app runs several worker threads and a
        tile the owner closes mid-fight drops its renderer, which would leave
        the collection to land on whichever thread allocates next. Releasing
        the last references from the destroy handler makes it deterministic."""
        for cell in self._cells.values():
            cell.icon_image = None
        self.icons.clear()

    def render(self, model) -> None:
        """Draw the line. `model` is a ``LinksVM``, or None = line absent,
        which leaves the fleet tile exactly as compact as before."""
        rows = (tuple(getattr(model, "coverage", ()) or ())
                if model is not None else ())
        wanted = []
        for row in rows:
            cell = self._cells.get(getattr(row, "discipline", None))
            if cell is None:
                continue
            cell.render(row)
            wanted.append(cell.frame)
        self._relayout(wanted)

    def _relayout(self, wanted) -> None:
        if wanted != self._layout:
            for widget in self._layout:
                widget.grid_forget()
            for column, widget in enumerate(wanted):
                # FULL option set on every grid(): Tk RETAINS the options it is
                # not given across a re-grid (the stale-columnspan class).
                widget.grid(row=0, column=column, sticky="w",
                            padx=(0, _LINKS_CELL_GAP), pady=0)
            for column in range(len(self._cells)):
                # Equal shares for the columns in use, nothing for the rest --
                # a stale weight on an empty column would eat width the visible
                # disciplines need.
                live = column < len(wanted)
                self.row.columnconfigure(column, weight=1 if live else 0,
                                         uniform="cov" if live else "")
            self.row.columnconfigure(len(self._cells),
                                     weight=_LINKS_GUTTER_WEIGHT, uniform="")
            self._layout = list(wanted)
        show = bool(wanted)
        if show != self._packed:
            if show:
                self.row.pack(fill="x", padx=_LINKS_ROW_PADX,
                              pady=_LINKS_ROW_PADY)
            else:
                self.row.pack_forget()
            self._packed = show


class _TileRenderer:
    """Base: own the widgets inside one tile's body, and write to Tk only when
    the model actually moved.

    The last-drawn key latches only AFTER a successful draw, so a ``TclError``
    from a torn-down widget can never convince the renderer that a failed
    write landed (the guarded-setter invariant the tile chrome also keeps)."""

    def __init__(self, parent, palette: dict):
        self._palette = dict(palette or PALETTE)
        self._last = None
        self.frame = tk.Frame(parent, width=1, height=1,
                              bg=self._palette.get("BG_DARK",
                                                   ui_theme.BG_DARK))
        self.frame.pack(fill="both", expand=True)
        # width=1/height=1 + pack_propagate(False) is LOAD-BEARING, not
        # tidiness (the battle-ledger panel's trick, for a sharper reason). A
        # tile is sized in PHYSICAL px by SetWindowPos, but Tk still resizes a
        # Toplevel to its children's REQUESTED size -- so one long intel line
        # would grow the window out from under the placement the owner dragged
        # it to, and every subsequent snap/persist would follow the inflated
        # rect. Content is clipped instead; the tile is resized by its corner,
        # never by what happens to arrive in it.
        self.frame.pack_propagate(False)

    def update(self, model) -> bool:
        """Draw `model`; return True iff anything was written."""
        key = self._key(model)
        if self._last is not None and key == self._last:
            return False
        try:
            self._draw(model)
        except tk.TclError:
            return False
        self._last = key
        return True

    def _key(self, model):
        return model

    def _draw(self, model):
        raise NotImplementedError


class BattleRenderer(_TileRenderer):
    """Killed-vs-lost, straight off the shared ``BattleLedgerView``."""

    LINES = 8

    def __init__(self, parent, palette: dict):
        super().__init__(parent, palette)
        self._pool = _LabelPool(self.frame, self.LINES, self._palette)

    def _key(self, view):
        # Deliberately NOT the whole view: `stamp_line` advances every second
        # while the counts sit still, and this tile does not draw it, so
        # diffing the whole view would repaint once a second forever. Only the
        # fields actually rendered (plus the engine's revision) count.
        if view is None:
            return ("none",)
        return (bool(getattr(view, "visible", False)),
                getattr(view, "revision", None),
                getattr(view, "title", ""), getattr(view, "state", ""),
                getattr(view, "state_label", ""),
                getattr(view, "floor_prefix", ""),
                tuple(getattr(view, "rows", ()) or ()),
                getattr(view, "fast", None))

    def _draw(self, view):
        self._pool.render(battle_lines(view))


class FleetRenderer(_TileRenderer):
    """Fleet size + abbreviated inv-group buckets in TWO columns, each cell
    hovering its hull breakdown, with the command-burst coverage line
    underneath."""

    def __init__(self, parent, palette: dict):
        super().__init__(parent, palette)
        bg = self._palette.get("BG_DARK")
        # Two containers, both packed HERE and in this order, because content
        # inside either can appear late and ``pack`` appends -- see
        # ``_LinksPanel``. The head joins the TOP container rather than sitting
        # loose in the body, for exactly that reason.
        self._rows_holder = tk.Frame(self.frame, bg=bg)
        self._rows_holder.pack(fill="x")
        self._rows_holder.columnconfigure(0, weight=1)
        self._head = tk.Label(self._rows_holder, text="", font=_FLEET_FONT_HEAD,
                              bg=bg, fg=self._palette.get("FG_ACCENT"),
                              anchor="w", **_FLEET_TIGHT)
        # columnspan: the total -- and the gate status that replaces it -- is a
        # caption for BOTH columns of buckets underneath. Same padding as a
        # comp cell, so the header and the first column share a left edge.
        self._head.grid(row=0, column=0, columnspan=COMP_COLUMNS, sticky="w",
                        padx=_FLEET_CELL_PADX, pady=_FLEET_CELL_PADY)
        self._grid = _CompGrid(self._rows_holder, self._palette,
                               FLEET_MAX_ROWS + 1)
        self._grid.frame.grid(row=1, column=0, columnspan=COMP_COLUMNS,
                              sticky="ew")
        self._links = _LinksPanel(self.frame, self._palette)

    def _draw(self, model):
        if model.status:
            self._head.configure(text=model.status,
                                 fg=self._palette.get("FG_DIM"))
            self._grid.render([])
        else:
            self._head.configure(text=f"Total: {model.total}",
                                 fg=self._palette.get("FG_ACCENT"))
            self._grid.render([
                (fleet_row_text(row),
                 "FG_DIM" if row[0] == OTHER_LABEL else "FG_TEXT",
                 fleet_group_tip(row)) for row in model.rows])
        # Deliberately OUTSIDE the status branch: the coverage report is
        # chat-sourced, so a tile gated to "not fleet boss" still carries one.
        self._links.render(getattr(model, "links", None))


class IntelRenderer(_TileRenderer):
    """Nearby intel, newest FIRST, with a jump badge per row -- and fitted to
    the tile it is actually in, both ways.

    The body's propagation guard clips whatever does not fit (it must: the
    alternative is a Toplevel that resizes itself out from under the owner's
    placement). This renderer's job is to make sure the clip has nothing left
    to take: it packs only as many rows as the frame's CURRENT height holds,
    and it ellipsizes each row's free text to the frame's CURRENT width so the
    trailing jump badge survives. Both are measured with the real row font.
    The head/status notice goes through the same width fit -- it is the longest
    string the tile ever writes, so "nothing left to take" is not true without
    it.

    Because both depend on the frame's size and a RESIZE does not move the
    model at all, the size rides the diff key -- see ``_key``.

    Recorded residual: the first draw of a tile happens BEFORE the window maps
    (render-then-show, on every reveal), so it fits against an unrealised 1x1
    frame and degrades to the un-fitted layout for one beat. The size in the
    key is what closes that -- the next 1 Hz tick sees the real geometry and
    repaints. Same one-beat catch-up after a drag-resize."""

    def __init__(self, parent, palette: dict):
        super().__init__(parent, palette)
        self._head = tk.Label(self.frame, text="", font=_FONT_ROW,
                              bg=self._palette.get("BG_DARK"),
                              fg=self._palette.get("FG_DIM"), anchor="w")
        #: What the pool is CURRENTLY showing, in pool order. Written by every
        #: draw, read by every click -- see ``_row_clicked``.
        self._rows: tuple = ()
        #: The controller's row-click seam, filled by ``set_row_click`` right
        #: after construction (the pool's bindings are already live by then and
        #: resolve this attribute at click time, so there is no window in which
        #: a click reaches a half-wired renderer).
        self._row_click = None
        self._pool = _LabelPool(self.frame, INTEL_SHOW, self._palette,
                                on_click=self._row_clicked)
        self._head_packed = False
        self._font_obj = None

    def set_row_click(self, callback) -> None:
        """Wire (or clear) what a click on a row means. Tk thread."""
        self._row_click = callback

    def _row_clicked(self, index):
        """A left click on pooled row `index`: hand the CURRENT row over.

        The row is looked up now, not captured when the label was worded --
        the pool is a fixed, reused set of labels and the tile re-words itself
        every second, so a captured row would go stale on the next intel line.

        Always returns ``"break"``, and that is load-bearing rather than tidy:
        the tile's Toplevel binds ``<Button-1>`` to corner-resize ARMING
        (``info_tile._on_body_b1_press``), so a row press that fell through
        would start a resize whenever the pointer sat in a corner zone. The
        close glyph returns ``"break"`` for exactly this reason. The tile is
        moved by dragging its title STRIP, not its body, so nothing else on
        this button competes.

        Guarded end to end: an index with nothing under it (the pool is
        ``INTEL_SHOW`` wide and only a prefix is ever packed), a callback
        nobody wired, and a callback that raises all cost the click and
        nothing else."""
        rows = self._rows
        callback = self._row_click
        if callable(callback) and 0 <= index < len(rows):
            try:
                callback(rows[index])
            except Exception:
                log.debug("info tiles: intel row click failed", exc_info=True)
        return "break"

    def _frame_size(self) -> tuple:
        """The content frame's CURRENT laid-out size in px.

        A frame that has never been laid out answers ``1x1`` and a destroyed
        one answers ``(0, 0)``; both are "unrealised" to every consumer, which
        then degrades to the pack-them-all/no-truncation behaviour rather than
        fitting against a number that means nothing."""
        try:
            return (int(self.frame.winfo_width()),
                    int(self.frame.winfo_height()))
        except tk.TclError:
            return (0, 0)

    def _font(self):
        """This renderer's OWN ``tkfont.Font`` for the row font, built lazily.

        PER-INSTANCE, never module-level: a Font belongs to the interpreter
        that made it, so a shared one would hand a destroyed interpreter's font
        to the next tile (and, under pytest, to the next root) -- the same rule
        the module header states for the plain font tuples, and the same one
        ``preview_tile._label_font`` keeps. A FAILURE is deliberately not
        cached: the Tk failure class on this box is transient (the AV-filter
        trap cured in tests/conftest.py), and a cached degraded font would
        leave the tile un-fitted forever."""
        if self._font_obj is None:
            try:
                self._font_obj = tkfont.Font(root=self.frame, font=_FONT_ROW)
            except (tk.TclError, RuntimeError):
                return None
        return self._font_obj

    def _row_height(self, font) -> int:
        """One packed row's pitch: the font's own line height plus the Label's
        chrome (`_POOL_LABEL_CHROME`, measured -- see the constant).

        The chrome is a constant rather than a ``winfo_reqheight()`` read so
        the count is right on the FIRST draw after a resize, before the pool's
        labels have been laid out again."""
        if font is None:
            return 0
        try:
            return int(font.metrics("linespace")) + _POOL_LABEL_CHROME
        except (tk.TclError, TypeError, ValueError):
            return 0

    def _key(self, model):
        # The frame's SIZE rides the key because the draw now depends on it:
        # the row count and every row's truncation are computed from the
        # current width/height, and a tile RESIZE moves neither the model nor
        # anything else the base class diffs. Without this a dragged corner
        # would leave a stale row count and stale truncation on screen until
        # the intel itself happened to change -- and the key latches only after
        # a successful draw, so the base class's TclError contract is intact.
        return (model, self._frame_size())

    def _draw(self, model):
        # Measure ONCE, for both branches: the status notice is packed with the
        # same padding into the same frame in the same font as a row, so it is
        # fitted by the same numbers. It is also the LONGEST string this tile
        # writes, which is why leaving it out of the row fit left the owner's
        # "goes off the tile screen" alive in exactly one place.
        width, height = self._frame_size()
        font = self._font()
        available = _intel_text_width(width)
        # No font or no realised width => no measurer, and both fitters return
        # the whole line. Clipped beats truncated-against-nothing.
        fitting = font is not None and available > 0
        measure = font.measure if fitting else None
        if model.status:
            # Status and rows are mutually exclusive (the controller sets a
            # status only when there is nothing to list). Rendering them that
            # way here too means the status label's pack order relative to the
            # row pool can never become visible -- `pack` APPENDS, so a status
            # that first appeared mid-session would otherwise land under the
            # rows it is supposed to explain.
            self._head.configure(text=intel_status_text(model.status,
                                                        available, measure))
            if not self._head_packed:
                self._head.pack(fill="x", padx=_POOL_PADX)
                self._head_packed = True
            self._pool.render([])
            # The labels outlive their content, so the click map has to be
            # cleared with them -- otherwise a click on an empty tile would
            # still open the report that was there before the status replaced
            # it.
            self._rows = ()
            return
        if self._head_packed:
            self._head.pack_forget()
            self._head_packed = False
        # Fit to the tile, both ways, with the row font's own metrics. The
        # model already hands the rows over newest-first, so the tail this
        # drops is the OLDEST intel -- the direction the clip used to take the
        # newest lines in.
        limit = intel_rows_that_fit(height, self._row_height(font))
        rows = tuple(model.rows[:limit])
        self._pool.render([(intel_row_text(row, available, measure),
                            intel_row_colour_key(row)) for row in rows])
        # The click map is exactly what is PACKED, in pool order, so index N
        # means the row the owner is looking at in slot N -- including after a
        # resize changed how many slots there are.
        self._rows = rows


#: key -> tile spec. Adding a tile type is one entry plus one renderer: the
#: chrome, layout persistence, snapping and settings all read this registry.
TILE_SPECS = {
    "battle": {"title": "Battle", "default_size": (260, 150),
               "render": BattleRenderer},
    # The fleet tile carries the comp rollup AND the coverage strip, so its
    # first-spawn default has to fit both -- a section clipped by the body's
    # (deliberate) propagation guard is a section the owner never discovers.
    # RE-MEASURED 2026-08-03 (round 5: top-6 buckets, Consolas 8) against the
    # WORST case (6 buckets + Other in two columns, plus the four-icon
    # coverage strip) with the compact metrics at the top of this module:
    # 152x93 of content, i.e. 113 px of window once info_tile's 20 px strip is
    # added. 180x120 still carries that worst case -- 28 px width slack, 7 px
    # height slack (tighter than round 4's 26/11: Consolas 8's taller line
    # more than offsets losing two grid rows, but still clearly positive) --
    # and still lands well above the floors (MIN_W 120 / MIN_H 90), so the
    # default did not need to move. Stored layouts win over this at spawn, so
    # an existing 280x170 tile keeps its size.
    "fleet": {"title": "Fleet", "default_size": (180, 120),
              "render": FleetRenderer},
    "intel": {"title": "Intel", "default_size": (380, 220),
              "render": IntelRenderer},
}


# ── distance resolver (the ONLY worker) ─────────────────────────────────────

class IntelDistanceResolver:
    """Gate-jump distances from one reference system, resolved off the Tk
    thread.

    One daemon worker, one queue, an LRU cache, in-flight de-duplication, and
    both edges injected: ``route_fn(origin, dest) -> list[int] | None`` (the
    ESI/BFS route -- systems ALONG the path, origin first, so jumps is
    ``len(route) - 1``) and ``post_ui(callable)`` (fc_gui's UI dispatcher).

    **This class never touches Tk.** Results reach the UI only by handing the
    caller's callback to ``post_ui``. A ``route_fn`` that RAISES leaves the
    system unanswered so a later request retries it; a ``route_fn`` that
    returns None has ANSWERED (there is no route), which is cached so the same
    dead pair is not re-queued forever. Either way the model sees "distance
    unknown" and fails open."""

    def __init__(self, route_fn, post_ui, cache_cap: int = 2048):
        self._route_fn = route_fn
        self._post_ui = post_ui
        self._cap = max(1, _as_int(cache_cap, 2048))
        self._lock = threading.Lock()
        self._cache: OrderedDict = OrderedDict()
        self._inflight: set = set()
        self._reference = None
        self._generation = 0          # bumped on every reference change
        self._queue: queue.Queue = queue.Queue()
        self._thread = None
        self._stopping = threading.Event()

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> None:
        """Idempotent: a second call while the worker is alive is a no-op."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="info-tiles-distance")
        self._thread.start()

    def is_running(self) -> bool:
        return bool(self._thread is not None and self._thread.is_alive())

    def stop(self) -> None:
        """Wake the worker and JOIN it, so a shutdown cannot leave a route
        lookup running against a dead UI. Idempotent.

        The flag matters as much as the sentinel: on app close there may be a
        backlog of queued systems behind it, and answering them all first (five
        seconds each if ESI is timing out) would hold the close up for minutes.
        The flag makes the worker DROP the backlog instead."""
        thread = self._thread
        self._thread = None
        self._stopping.set()
        if thread is None:
            return
        self._queue.put(None)
        thread.join(timeout=2.0)
        self._drain()

    def _drain(self) -> None:
        """Empty the queue (and the in-flight marks) after the worker is gone.

        LOAD-BEARING FOR RESTART, not housekeeping. The worker has two ways
        out: it takes the None sentinel, or it takes a BACKLOG item and quits
        on the stopping flag. Down the second path the sentinel is still
        sitting in the queue, so the next ``start()`` would hand its fresh
        thread that None as its very first item and the thread would die on
        the spot -- silently, because ``start()`` has already returned and
        nothing else looks. (``stop`` then ``start`` is reachable from the app
        itself: ``shutdown()`` stops the resolver, and re-enabling the master
        switch spawns the intel tile, which calls ``start()``.)

        The in-flight marks go with it: a system whose lookup was abandoned
        with the backlog is no longer coming, and leaving it marked would keep
        ``request`` from ever re-queueing it -- the row would read ``?j``
        forever."""
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        with self._lock:
            self._inflight.clear()

    # -- state ---------------------------------------------------------------
    def set_reference(self, system_id) -> None:
        """Point the resolver at a system. A CHANGE invalidates everything:
        cached distances are relative to the old origin and would otherwise
        quietly describe the wrong neighbourhood. In-flight answers are dropped
        by generation, never written under the new reference."""
        new = _as_int(system_id, 0) or None
        with self._lock:
            if new == self._reference:
                return
            self._reference = new
            self._generation += 1
            self._cache.clear()
            self._inflight.clear()

    def lookup_cached(self, system_id) -> int | None:
        """The known jump distance, or None for "not answered yet" AND for
        "answered: unreachable". Both mean the same thing to the model (show
        it, badge it ``?j``); ``request`` is what tells them apart, so a dead
        pair is not re-queued.

        A READ COUNTS AS A USE. Without the recency bump the cache would be
        insert-ordered -- FIFO wearing an ``OrderedDict``'s clothes -- and the
        entries evicted first would be the OLDEST-LEARNED, which in a long
        fight are exactly the systems the tile has been showing all along.
        Re-resolving those costs a route lookup each and re-badges live rows
        ``?j`` while it happens."""
        key = _as_int(system_id, -1)
        with self._lock:
            if key not in self._cache:
                return None
            self._cache.move_to_end(key)
            return self._cache[key]

    def request(self, system_ids, on_resolved) -> None:
        """Queue any unanswered, not-in-flight systems. ``on_resolved()`` is
        marshaled through ``post_ui`` once per resolution. A no-op until a
        reference is set -- there is nothing to measure from."""
        todo = []
        with self._lock:
            reference, generation = self._reference, self._generation
            if reference is None:
                return
            for raw in system_ids or ():
                system_id = _as_int(raw, 0)
                if not system_id:
                    continue
                if system_id in self._cache:
                    # Being ASKED ABOUT is a use too, the same as being read:
                    # a system the tile keeps requesting must not age out from
                    # under it just because its answer never changes.
                    self._cache.move_to_end(system_id)
                    continue
                if system_id in self._inflight:
                    continue
                self._inflight.add(system_id)
                todo.append(system_id)
        for system_id in todo:
            self._queue.put((generation, reference, system_id, on_resolved))

    # -- worker --------------------------------------------------------------
    def _run(self):
        while True:
            item = self._queue.get()
            if item is None or self._stopping.is_set():
                return
            generation, reference, system_id, on_resolved = item
            try:
                self._resolve_one(generation, reference, system_id, on_resolved)
            except Exception:
                # Belt to the braces below: this thread must outlive anything
                # a route lookup or a UI dispatcher can throw.
                log.debug("info tiles: distance worker item failed",
                          exc_info=True)

    def _resolve_one(self, generation, reference, system_id, on_resolved):
        with self._lock:
            stale = generation != self._generation
        if stale:
            with self._lock:
                self._inflight.discard(system_id)
            return
        failed = False
        route = None
        try:
            route = self._route_fn(reference, system_id)
        except Exception:
            failed = True
            log.debug("info tiles: route lookup failed", exc_info=True)
        jumps = None
        if not failed:
            try:
                jumps = (len(route) - 1) if route else None
            except TypeError:
                jumps = None
        with self._lock:
            self._inflight.discard(system_id)
            fresh = generation == self._generation
            if fresh and not failed:
                self._cache[system_id] = jumps
                self._cache.move_to_end(system_id)
                while len(self._cache) > self._cap:
                    self._cache.popitem(last=False)
        if failed or not fresh or not callable(on_resolved):
            return
        try:
            self._post_ui(on_resolved)
        except Exception:
            log.debug("info tiles: post_ui rejected a resolution",
                      exc_info=True)


# ── config heal ─────────────────────────────────────────────────────────────

def heal_info_tile_layouts(cfg) -> bool:
    """Floor every PERSISTED tile rect in `cfg`, IN PLACE. True iff changed.

    The ``heal_preview_sizes`` contract, tile for tile:
      * a rect whose size is under the floors is FLOORED (a hand-edited config
        must not leave the owner with a tile too small to grab and fix);
      * a MALFORMED entry is SKIPPED, not repaired -- rewriting it would hide a
        fault somewhere else;
      * an ABSENT key stays absent -- healing is not seeding, and inventing
        defaults here would silently promote a missing block into a real one;
      * IDEMPOTENT: a second call on a healed dict returns False and touches
        nothing, so the caller writes config exactly once.

    ``OverflowError`` is in the malformed net for a reason that is invisible
    until it bites: ``json.load`` accepts the non-standard ``Infinity`` /
    ``NaN`` literals by DEFAULT, so a config carrying one loads cleanly and
    then makes ``int(float('inf'))`` raise -- and this runs at boot, which
    would turn a bad rect into a dead app.
    """
    if not isinstance(cfg, dict):
        return False
    block = cfg.get("info_tiles")
    if not isinstance(block, dict):
        return False
    layouts = block.get("layouts")
    if not isinstance(layouts, dict):
        return False
    changed = False
    for key, value in list(layouts.items()):
        try:
            x, y = int(value[0]), int(value[1])
            w, h = int(value[2]), int(value[3])
        except (TypeError, ValueError, IndexError, KeyError, OverflowError):
            continue
        healed = preview_layout.clamp_size(w, h, InfoTileWindow.MIN_W,
                                           InfoTileWindow.MIN_H)
        if healed != (w, h):
            layouts[key] = [x, y, healed[0], healed[1]]
            changed = True
    return changed


# ── host seams ──────────────────────────────────────────────────────────────

def _none(*_args, **_kwargs):
    return None


def _empty(*_args, **_kwargs):
    return []


def _no_fleet(*_args, **_kwargs):
    return (False, None, False)


def _always_show(*_args, **_kwargs):
    """Inert default for ``HudHost.should_show`` -- see the field.

    True, deliberately: every OTHER seam's inert default is the empty/absent
    answer, but this one gates VISIBILITY, so its inert answer has to be the
    one that leaves the tiles on screen."""
    return True


@dataclass
class HudHost:
    """Every fc_gui seam the HUD needs, injected.

    All callables are invoked on the TK THREAD except ``route_fn``, which the
    distance worker calls off-thread and which must therefore touch no Tk and
    no Tk variable. Defaults are inert on purpose: a seam fc_gui forgets to
    wire degrades to an honest empty state instead of quietly doing the wrong
    thing (``post_ui`` in particular DROPS the callback rather than running it
    inline, so a missing dispatcher can never become a worker-thread Tk call).
    """
    config: dict = field(default_factory=dict)
    save_config: object = _none
    post_ui: object = _none
    ledger_view: object = _none          # () -> BattleLedgerView | None
    fleet_snapshot: object = _none       # () -> (members, ship_counts, total)
    fleet_state: object = _no_fleet      # () -> (auth, fleet_id, is_boss)
    #: () -> (rows_by_name, coverage, ship_names, is_boss) -- the command-burst
    #: charge report fc_gui already computes for the Fleet tab's Specialized
    #: Roles area (``_apply_booster_compute`` stores all four on the Tk thread).
    #: Optional and inert: a host that never wires it simply has no links
    #: section, and the fleet tile stays exactly as compact as before.
    #:
    #: This feed is CHAT-sourced, so it is deliberately NOT gated by
    #: ``fleet_state`` -- see ``build_links_model``.
    links_snapshot: object = _none
    own_system_id: object = _none        # () -> int | None
    staging_name: object = _none         # () -> str
    #: () -> [(x, y, w, h)] -- the FCPreview tiles' rects, and a CONTRACT the
    #: host has to honour: h must already be the FULL window height, strip
    #: INCLUDED, because that is what an info tile's own rects are and
    #: ``snap_rect`` compares the two directly.
    #:
    #: fc_gui's ``_preview_tile_rects`` stores ``(x, y, w, BODY_H)`` --
    #: preview_tile re-adds its own ``STRIP_H`` inside its snap math -- so the
    #: wiring lambda adds ``preview_tile.STRIP_H`` there. The conversion lives
    #: in fc_gui ON PURPOSE and not here: this module imports NEITHER preview
    #: module (``preview_tile`` is a sibling family, deliberately uncoupled --
    #: test-asserted), so it cannot read that constant, and re-typing 20 would
    #: be a second owner for a number preview already owns. Un-converted rects
    #: do not raise; they snap 20 px off, which reads as "snapping feels
    #: sloppy" and never points here.
    #:
    #: ``_neighbour_rects`` is therefore pass-through: it merges these with
    #: sibling tiles' rects and does no height maths of its own.
    preview_rects: object = _empty
    #: () -> (w, full_h) | None -- the FCPreview tiles' CURRENT uniform size,
    #: already in THIS module's full-height convention (see ``preview_rects``
    #: just above for why the strip is added on the fc_gui side and never
    #: here). Backs the settings popup's one-shot "Match preview size" button
    #: (``InfoTileController.match_preview_size``) -- nothing calls this seam
    #: from the render beat. Optional and inert: a host that never wires it,
    #: or whose preview subsystem answers nothing sane, makes the button a
    #: safe no-op rather than a raise. The name says SIZE, not RECT:
    #: FCPreview's tiles share one uniform (w, body_h) only while preview's
    #: own ``uniform_size`` is on, and even then they share no POSITION, so
    #: this seam answers a size only -- ``match_preview_size`` places each HUD
    #: tile at ITS OWN existing (x, y).
    preview_tile_size: object = _none
    #: () -> bool -- should the tiles be on screen right now? Read once per
    #: beat; the controller HIDES every tile on the True->False edge and SHOWS
    #: them again on the False->True one.
    #:
    #: The HOST owns the predicate, not this module. fc_gui ANDs two
    #: conditions: at least one EVE client window exists (so the HUD does not
    #: float over a bare desktop after the last client closes -- login windows
    #: count, that is the tracker's own semantics and a login screen is
    #: exactly when an FC wants his tiles back), AND the foreground window
    #: belongs to an EVE client or to FCTool itself (so alt-tabbing to a
    #: browser takes the tiles with it, while the main window and this
    #: module's own settings popup keep them up for arranging).
    #:
    #: FAIL-VISIBLE, and the ONE seam whose inert default is not the empty
    #: answer: absent, non-callable or raising all read as SHOW (see
    #: ``_always_show``). A seam fc_gui forgets to wire, or one that breaks,
    #: must leave the tiles up -- a HUD hidden by a wiring bug looks like a
    #: dead feature, and there is no way to ask for it back from the overlay
    #: itself.
    should_show: object = _always_show
    #: ``(title, body, tile_rect) -> None`` -- show one clicked intel report in
    #: a transient pop-up. Called on the TK THREAD, from a user click only,
    #: never from the beat.
    #:
    #: This module composes WHAT is said (``intel_detail_title`` /
    #: ``intel_detail_body``, both pure) and the host owns the WINDOW it is
    #: said in -- the same split every other seam keeps. It must not: fc_gui's
    #: pop-up sits over an EVE client, and this module knows nothing about
    #: clients and imports no preview module.
    #:
    #: ``tile_rect`` is the intel tile's own ``(x, y, w, h)`` -- NOT edges --
    #: so the host has somewhere to put the window when no client is on
    #: screen. Unlike the implant reminder, which skips silently in that case,
    #: this one has to appear: the owner just clicked.
    #:
    #: Inert by default like every other seam: a host that never wires it
    #: makes the click a no-op rather than an error.
    show_intel_detail: object = _none
    screen_rects: object = _empty        # () -> [(x, y, w, h)]
    route_fn: object = _none             # (origin, dest) -> [ids] | None
    #: The app's own ``TypeCatalog`` (fc_gui's ``self.type_catalog``). Not a
    #: callable and not required -- when absent this module builds its own,
    #: which merely re-parses the bundled table. Pass it.
    type_catalog: object = None


# ── controller ──────────────────────────────────────────────────────────────

class InfoTileController:
    """Owns tile lifecycle, the 1 Hz render beat, and layout persistence.

    Tk thread only (the distance worker is the single exception and never
    reaches back except through ``post_ui``)."""

    def __init__(self, root, host: HudHost, tile_window_cls=InfoTileWindow):
        self._root = root
        self._host = host
        self._tile_cls = tile_window_cls
        self._tiles: dict = {}
        self._renderers: dict = {}
        self._enabled = False
        #: GLOBAL visibility layer. True while every tile is WITHDRAWN because
        #: the host's ``should_show`` seam says no -- no EVE client on screen,
        #: or the foreground belongs to neither EVE nor FCTool (see
        #: ``_sync_visibility``). Orthogonal to ``_enabled``: the tiles still
        #: exist, keep their geometry and keep their config, they are merely
        #: unmapped.
        self._suspended = False
        #: PER-TILE visibility layer: ``{key: is this tile mapped right now}``,
        #: one entry per LIVE tile, written only by ``_apply_visible``. It is
        #: the edge detector for both layers at once -- a tile's mapped state
        #: is a pure function of (global gate AND its own enabled flag AND its
        #: relevance), and only a CHANGE of that product touches Tk. Keys
        #: appear at spawn and leave at teardown, so ``.get(key, False)`` on a
        #: dead tile is the honest answer rather than a KeyError.
        self._tile_visible: dict = {}
        #: this controller's OWN bounded history. fc_gui's `_intel_buffer` is
        #: never read and never mutated.
        self.intel_entries = deque(maxlen=INTEL_KEEP)
        self.resolver = IntelDistanceResolver(host.route_fn, host.post_ui)

    # -- config access -------------------------------------------------------
    def _block(self, create=False) -> dict:
        cfg = self._host.config
        if not isinstance(cfg, dict):
            return {}
        block = cfg.get("info_tiles")
        if not isinstance(block, dict):
            if not create:
                return {}
            block = default_info_tiles_config()
            cfg["info_tiles"] = block
        return block

    def _tile_block(self, key, create=False) -> dict:
        block = self._block(create=create)
        tiles = block.get("tiles")
        if not isinstance(tiles, dict):
            if not create:
                return {}
            tiles = {}
            block["tiles"] = tiles
        entry = tiles.get(key)
        if not isinstance(entry, dict):
            if not create:
                return {}
            entry = {}
            tiles[key] = entry
        return entry

    def _save(self):
        _call(self._host.save_config)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _opacity(self):
        return _clamp_opacity(self._block().get("opacity", DEFAULT_OPACITY))

    def _snap_enabled(self):
        return bool(self._block().get("snap_enabled", True))

    def _lock_layout(self):
        return bool(self._block().get("lock_layout", False))

    def _max_jumps(self):
        return _clamp_jumps(
            self._tile_block("intel").get("max_jumps", DEFAULT_MAX_JUMPS))

    # -- enable / disable ----------------------------------------------------
    def set_enabled(self, flag: bool) -> None:
        """Master switch: spawn every per-tile-enabled tile, or tear them all
        down. Config is written only on a real change, so the construct-time
        call that mirrors the stored value costs no save."""
        flag = bool(flag)
        block = self._block(create=True)
        if bool(block.get("enabled", False)) != flag:
            block["enabled"] = flag
            self._save()
        if flag == self._enabled:
            return
        self._enabled = flag
        if flag:
            # Visibility BEFORE the first spawn, not after: enabling the HUD
            # with no client running (or with EVE not in front) must spawn
            # the tiles HIDDEN. Deciding it in the tick below instead would
            # spawn them mapped and withdraw them a moment later -- a
            # visible flash over the desktop. (The per-tile relevance layer
            # needs no equivalent priming: `_spawn` reads it directly, so a
            # tile whose subject is not live is never mapped in the first
            # place.)
            self._suspended = not self._should_show()
            for key in TILE_SPECS:
                if self._tile_enabled(key):
                    self._spawn(key)
            self.tick()
        else:
            for key in list(self._tiles):
                self._teardown(key)

    def set_tile_enabled(self, key: str, flag: bool) -> None:
        """Per-tile switch. Also the close-glyph path: the tile's strip X and
        the popup's checkbox are the SAME operation, so a tile closed on screen
        stays closed across a restart.

        **This flag IS the HUD's memory** (owner request 2026-08-03: tiles
        "should be persistent ... once the user sets them up, until the option
        is unchecked or the user closes it with an X"). It is config, it
        survives restarts, and these two gestures are the ONLY things that
        clear it -- neither visibility layer ever writes it. That is what lets
        an FC tick "Fleet" once and have the tile turn up by itself every time
        he forms up, and it is why a tile going away on its own must never be
        implemented by flipping this."""
        if key not in TILE_SPECS:
            return
        flag = bool(flag)
        entry = self._tile_block(key, create=True)
        if bool(entry.get("enabled", False)) != flag:
            entry["enabled"] = flag
            self._save()
        if not self._enabled:
            return
        if flag:
            # `_spawn` always places and shows only when BOTH layers agree --
            # and when it does show, it renders first. So a tile checked while
            # the HUD is withdrawn (or while its subject is not live) appears
            # by itself at the next edge, with a fresh first frame, and this
            # path owes it no render of its own: rendering into a window
            # nobody can see is a Tk write nobody can see.
            self._spawn(key)
        else:
            self._teardown(key)

    def _tile_enabled(self, key) -> bool:
        return bool(self._tile_block(key).get("enabled", False))

    # -- visibility: should a tile be on screen? ------------------------------
    def _should_show(self) -> bool:
        """Guarded read of the ``should_show`` seam -- the GLOBAL layer.
        FAIL-VISIBLE: a seam that is absent, not callable or raising answers
        SHOW, so a wiring bug can never hide the HUD (see the field's own
        note). WHAT makes the answer False is entirely the host's business --
        no EVE client on screen, or the foreground belonging to neither EVE
        nor FCTool."""
        seam = getattr(self._host, "should_show", None)
        if not callable(seam):
            return True
        try:
            return bool(seam())
        except Exception:
            log.debug("info tiles: should_show seam raised", exc_info=True)
            return True

    def _relevant(self, key) -> bool:
        """Is `key`'s SUBJECT live -- the PER-TILE layer (owner request
        2026-08-03).

        Reads that tile's own seam and hands the ANSWER to the matching pure
        predicate; the seam being unaskable is a different thing from its
        answer, and the two go opposite ways:

        * the seam ANSWERED "no fleet" / "no ledger" -> HIDE. That answer is
          the entire feature -- a fleet tile with no fleet is the clutter the
          owner asked to be rid of, and it comes back by itself the moment a
          tracked character opens one;
        * the seam could not be asked at all -- absent, not callable, raising
          -- -> SHOW. Same fail-visible reasoning as the global gate: a tile
          hidden by a wiring bug looks like a dead feature, and the overlay
          offers no way to ask it back.

        Reads NOTHING and writes NOTHING outside the seam call: relevance is
        screen state, never config. The tile stays "enabled" throughout, which
        is what makes the owner's memory work -- see ``set_tile_enabled``.

        Unregistered keys and ``intel`` answer True: an intel tile is a
        standing watch with no idle state to gate on.
        """
        if key == "fleet":
            state = _call(getattr(self._host, "fleet_state", None),
                          default=_UNKNOWN)
            return state is _UNKNOWN or fleet_is_live(state)
        if key == "battle":
            view = _call(getattr(self._host, "ledger_view", None),
                         default=_UNKNOWN)
            return view is _UNKNOWN or battle_is_live(view)
        return True

    def _wants_visible(self, key) -> bool:
        """The whole visibility product for one tile: the global gate AND the
        tile's own (persistent) enabled flag AND its relevance.

        Short-circuit order is deliberate. While the HUD is globally suspended
        NO seam is read at all -- a suspended beat costs one ``should_show``
        call and nothing else, which is what keeps it zero-work as well as
        zero-write, and what makes a relevance flip that happens while the
        tiles are withdrawn a non-event until the reveal re-evaluates it."""
        return (not self._suspended and self._tile_enabled(key)
                and self._relevant(key))

    def _apply_visible(self, key, want: bool) -> None:
        """Map or withdraw ONE tile, and only on a CHANGE.

        The single edge detector for both layers -- every show/hide in this
        module comes through here, so "exactly one call per transition, zero
        in steady state" is one rule in one place rather than a property of
        two machines that have to agree.

        Edge-triggering is load-bearing for clarity, NOT because a
        level-triggered "call show() every beat" would retop: it would not --
        ``InfoTileWindow.show()`` already early-returns on ``self._visible``,
        so a steady-state call is a cheap no-op by itself. Tracking the
        transition here means correctness does not rest on that guard, and the
        transition is independently testable.

        RENDER BEFORE SHOW on every reveal: ``show()`` maps the window and can
        flush whatever was last drawn, so content has to be written first for
        "no stale frame" to actually hold. That is also why a hidden tile is
        never rendered -- there is nothing on screen to keep fresh, and the
        reveal always paints."""
        want = bool(want)
        if want == self._tile_visible.get(key, False):
            return                            # no transition: no Tk write
        tile = self._tiles.get(key)
        if tile is None:
            return
        if want:
            self._render(key)
            _call(tile.show)
        else:
            _call(tile.hide)
        self._tile_visible[key] = want

    def _sync_visibility(self) -> bool:
        """Bring every live tile's mapped state into line with BOTH layers.
        Returns True while globally suspended ("the caller should stop here").

        One pass, one rule: each tile is shown iff the global gate, its own
        enabled flag and its relevance all say yes, and only the tiles whose
        answer MOVED touch Tk. The global layer therefore keeps its old
        semantics exactly -- one hide per tile when EVE goes away, one show
        when it comes back -- except that the reveal now shows only the tiles
        that are ALSO relevant, which is the point of the second layer."""
        self._suspended = not self._should_show()
        for key in list(self._tiles):
            self._apply_visible(key, self._wants_visible(key))
        return self._suspended

    # -- geometry ------------------------------------------------------------
    def _screens(self):
        rects = _call(self._host.screen_rects, default=None) or []
        out = []
        for rect in rects:
            try:
                x, y, w, h = rect
                out.append((int(x), int(y), int(w), int(h)))
            except (TypeError, ValueError, OverflowError):
                continue
        return out

    def _bounds(self):
        """The primary monitor's rect (first entry, fc_gui's convention), or a
        sane fallback when the host cannot say."""
        screens = self._screens()
        return screens[0] if screens else _FALLBACK_BOUNDS

    def _rescue(self, x, y, w, h):
        """clamp_visible against EVERY screen: a rect that is usably visible on
        any one of them is left alone (a multi-monitor owner with a display
        temporarily off must not have his tiles silently rearranged); anything
        stranded is clamped onto the primary."""
        screens = self._screens()
        if not screens:
            return x, y
        for screen in screens:
            if preview_layout.clamp_visible(x, y, w, h, screen) == (x, y):
                return x, y
        return preview_layout.clamp_visible(x, y, w, h, screens[0])

    def _grid_rects(self, keys):
        """Default placement for `keys`: preview_layout's own grid math, then
        mirrored so the block sits against the primary monitor's top-RIGHT
        (spec section 5) instead of over the top-left of whatever is running
        there. One uniform cell (the largest tile) keeps the columns from
        overlapping when the tiles differ in size."""
        bounds = self._bounds()
        bx, by, bw, _bh = bounds
        sizes = [TILE_SPECS[k]["default_size"] for k in keys] or [(240, 150)]
        cell_w = max(w for w, _h in sizes)
        cell_h = max(h for _w, h in sizes)
        laid = preview_layout.grid_arrange(
            len(keys), cell_w, cell_h, bounds,
            origin=(bx + _ARRANGE_MARGIN, by + _ARRANGE_MARGIN),
            gap=_ARRANGE_GAP)
        out = {}
        origin_x = bx + _ARRANGE_MARGIN
        right_cell = bx + bw - _ARRANGE_MARGIN - cell_w
        for key, (x, y, _w, _h) in zip(keys, laid):
            tile_w, tile_h = TILE_SPECS[key]["default_size"]
            # Mirror the column, then right-align the tile inside its cell, so
            # a narrow tile still ends flush with the block's right edge.
            cell_x = right_cell - (x - origin_x)
            tile_x, tile_y = self._rescue(cell_x + cell_w - tile_w, y,
                                          tile_w, tile_h)
            out[key] = (tile_x, tile_y, tile_w, tile_h)
        return out

    def _spawn_rect(self, key):
        saved = self._block().get("layouts", {})
        rect = saved.get(key) if isinstance(saved, dict) else None
        try:
            x, y = int(rect[0]), int(rect[1])
            w, h = int(rect[2]), int(rect[3])
        except (TypeError, ValueError, IndexError, KeyError, OverflowError):
            # Defaults are computed for the WHOLE registry, not just the live
            # tiles, so a tile's default slot does not move depending on which
            # of its siblings happened to be enabled first.
            # OverflowError is in the net for the same reason as in
            # `heal_info_tile_layouts`: JSON's non-standard ``Infinity``
            # survives ``json.load`` and only dies at ``int()`` -- here that
            # would take the whole `set_enabled(True)` down with it.
            return self._grid_rects(list(TILE_SPECS))[key]
        w, h = preview_layout.clamp_size(w, h, InfoTileWindow.MIN_W,
                                         InfoTileWindow.MIN_H)
        x, y = self._rescue(x, y, w, h)
        return (x, y, w, h)

    def _neighbour_rects(self, key):
        """Snap candidates for one tile: its VISIBLE siblings plus the preview
        family. The two registries stay separate and meet only here (polluting
        ``_preview_tile_rects`` with non-hwnd keys is a known regression
        class); each source is guarded on its own so a raising one costs only
        its own candidates.

        A sibling that is withdrawn -- by either visibility layer -- is not a
        candidate: it is not on screen, so snapping to its edge would park the
        dragged tile against nothing the owner can see. Same rule, same
        reason, as ``tile_rects()`` on the way out."""
        rects = []
        for other_key, tile in self._tiles.items():
            if other_key == key or not self._tile_visible.get(other_key, False):
                continue
            rect = _call(tile.rect)
            if rect:
                rects.append(tuple(rect))
        for rect in (_call(self._host.preview_rects, default=None) or []):
            try:
                x, y, w, h = rect
                rects.append((int(x), int(y), int(w), int(h)))
            except (TypeError, ValueError, OverflowError):
                continue
        return rects

    # -- spawn / teardown ----------------------------------------------------
    def _spawn(self, key):
        if key in self._tiles or key not in TILE_SPECS:
            return
        spec = TILE_SPECS[key]
        try:
            tile = self._tile_cls(self._root, key, spec["title"], PALETTE,
                                  on_move_end=self._on_move_end,
                                  on_resize_end=self._on_resize_end,
                                  on_close=self._on_close)
        except Exception:
            log.warning("info tiles: could not create the %s tile", key,
                        exc_info=True)
            return
        try:
            renderer = spec["render"](tile.body, PALETTE)
        except Exception:
            log.warning("info tiles: could not build the %s body", key,
                        exc_info=True)
            _call(tile.destroy)
            return
        # Row clicks: the RENDERER owns which row was clicked, the CONTROLLER
        # owns what a click means, and the HOST owns the window it opens.
        # getattr-guarded because `tile_window_cls`/`render` are injectable and
        # a renderer without the seam must cost the tile nothing.
        _call(getattr(renderer, "set_row_click", None),
              self._on_intel_row_click)
        self._tiles[key] = tile
        self._renderers[key] = renderer
        self._tile_visible[key] = False
        x, y, w, h = self._spawn_rect(key)
        _call(tile.place, x, y, w, h)
        _call(tile.set_lock_layout, self._lock_layout())
        _call(tile.configure_snap, self._snap_enabled(), None,
              lambda k=key: self._neighbour_rects(k), self._screens)
        _call(tile.set_alpha, self._opacity())
        # place() records + positions; show() is what MAPS the window and owns
        # the single retop. Placing without showing leaves an invisible tile --
        # which is PRECISELY what a spawn wants whenever either layer says no:
        # geometry is always applied (arrange / reset / match-preview-size keep
        # working while the tiles are hidden), and the visibility edge is the
        # one thing that maps it. Through `_apply_visible`, so a spawn that
        # DOES show renders first, exactly like every other reveal.
        self._apply_visible(key, self._wants_visible(key))
        if key == "intel":
            self.resolver.start()

    def _teardown(self, key):
        tile = self._tiles.pop(key, None)
        self._renderers.pop(key, None)
        self._tile_visible.pop(key, None)
        if tile is not None:
            _call(tile.destroy)

    # -- tile callbacks ------------------------------------------------------
    def _persist_rect(self, key, x, y, w, h):
        block = self._block(create=True)
        layouts = block.get("layouts")
        if not isinstance(layouts, dict):
            layouts = {}
            block["layouts"] = layouts
        layouts[key] = [_as_int(x), _as_int(y), _as_int(w), _as_int(h)]

    def _on_move_end(self, key, x, y, w, h):
        self._persist_rect(key, x, y, w, h)
        self._save()

    def _on_resize_end(self, key, x, y, w, h):
        self._persist_rect(key, x, y, w, h)
        self._save()

    def _on_close(self, key):
        """The strip's X. Disabling (rather than hiding) is what makes the
        glyph mean the same thing as the settings checkbox."""
        self.set_tile_enabled(key, False)

    # -- the beat ------------------------------------------------------------
    def tick(self) -> None:
        """Re-render every live tile. Rides fc_gui's 1 Hz EVE-clock beat.

        Cheap by construction: a no-op while disabled, guarded setters that
        skip unchanged values, and renderers that write nothing when their
        model has not moved. It NEVER calls ``place()`` -- that is a retop.

        The visibility pass runs FIRST and can end the beat: with the tiles
        withdrawn globally (no EVE client on screen, or another application in
        front) this writes NOTHING at all -- not a setter, not a render -- and
        a tile withdrawn by its own relevance gate is skipped the same way, so
        an FC not in a fleet pays nothing for an enabled fleet tile. The
        reveal is still fresh because ``_apply_visible`` renders each tile
        BEFORE calling ``show()``: content is rendered before the window maps,
        so there is no pre-hide frame left on screen for the map to flash. A
        tile revealed by this beat's own sync then falls straight through into
        the loop below and picks up the current opacity/lock/snap."""
        if not self._enabled:
            return
        if self._sync_visibility():
            return
        opacity = self._opacity()
        lock = self._lock_layout()
        snap = self._snap_enabled()
        for key in list(self._tiles):
            tile = self._tiles.get(key)
            if tile is None or not self._tile_visible.get(key, False):
                continue
            # `configure_snap(flag)` only: None means LEAVE UNCHANGED, so the
            # providers wired at spawn survive every per-toggle push.
            _call(tile.set_alpha, opacity)
            _call(tile.set_lock_layout, lock)
            _call(tile.configure_snap, snap)
            self._render(key)

    def _render(self, key):
        renderer = self._renderers.get(key)
        tile = self._tiles.get(key)
        if renderer is None or tile is None:
            return
        try:
            if key == "battle":
                model = _call(self._host.ledger_view)
                title = TILE_SPECS[key]["title"]
            elif key == "fleet":
                model = self._fleet_model()
                title = TILE_SPECS[key]["title"]
            else:
                model, title = self._intel_model()
            _call(tile.set_title, title)
            renderer.update(model)
        except Exception:
            log.debug("info tiles: render failed for %s", key, exc_info=True)

    def _fleet_model(self) -> FleetCompModel:
        state = _call(self._host.fleet_state, default=None) or (False, None,
                                                                False)
        try:
            authenticated, fleet_id, is_boss = state
        except (TypeError, ValueError):
            authenticated, fleet_id, is_boss = False, None, False
        snapshot = _call(self._host.fleet_snapshot, default=None)
        members, ship_counts, total = (), None, 0
        if snapshot:
            try:
                members, ship_counts, total = snapshot
            except (TypeError, ValueError):
                members, ship_counts, total = (), None, 0
        catalog = getattr(self._host, "type_catalog", None)
        model = build_fleet_comp_model(
            members, ship_counts, total, authenticated, fleet_id, is_boss,
            resolve_type_name=lambda tid: offline_type_name(tid, catalog),
            resolve_group_name=lambda tid: offline_group_name(tid, catalog))
        return replace(model, links=self._links_model())

    def _links_model(self):
        """The command-burst coverage line, or None when nothing is tracked.

        Built from its OWN seam and NOT from ``fleet_state``: charges are read
        out of fleet chat, so the report is meaningful with no ESI character
        and no fleet boss (the gate the comp rollup above must respect).

        The seam still answers fc_gui's whole four-tuple -- it feeds the Fleet
        tab's Specialized Roles area too, and this module does not get to
        reshape it -- but the TILE consumes only the coverage member: its
        per-pilot rows moved back to the tab (owner directive 2026-08-02)."""
        snapshot = _call(self._host.links_snapshot, default=None)
        coverage = {}
        if snapshot:
            try:
                _rows_by_name, coverage, _ship_names, _is_boss = snapshot
            except (TypeError, ValueError):
                coverage = {}
        return build_links_model(coverage)

    def _reference(self):
        return resolve_reference(
            str(self._tile_block("intel").get("reference_system", "") or ""),
            _call(self._host.own_system_id),
            str(_call(self._host.staging_name, default="") or ""))

    def _intel_model(self):
        reference_id, label = self._reference()
        self.resolver.set_reference(reference_id)
        max_jumps = self._max_jumps()
        if reference_id is None:
            # Unconfigured is not "show everything": with nothing to measure
            # from, the tile names the gap and filters nothing INTO view.
            return IntelTileModel(label, ()), intel_title(max_jumps, label,
                                                          None)
        self._request_distances()
        rows = build_intel_model(self.intel_entries,
                                 self.resolver.lookup_cached, max_jumps)
        status = "" if rows else INTEL_STATUS_EMPTY
        return (IntelTileModel(status, rows),
                intel_title(max_jumps, label, reference_id))

    def _request_distances(self):
        unknown = []
        for entry in self.intel_entries:
            for system_id in entry.system_ids:
                if (system_id not in unknown
                        and self.resolver.lookup_cached(system_id) is None):
                    unknown.append(system_id)
        if unknown:
            self.resolver.request(unknown, self._on_distances)

    def _on_distances(self):
        """Marshaled back onto the Tk thread by ``post_ui`` when a distance
        lands. Tolerates the tile having gone away -- or having been withdrawn
        because the last EVE client closed / the owner alt-tabbed away -- in
        the meantime. ``_tile_visible`` is read rather than ``_suspended``
        because it answers for BOTH layers at once, and the next reveal
        repaints anyway."""
        if self._enabled and self._tile_visible.get("intel", False):
            self._render("intel")

    def _on_intel_row_click(self, row) -> None:
        """One intel row was clicked: hand the WHOLE report to the host.

        The tile ellipsizes every row's free text so the jump badge survives
        (``intel_row_text``), which is right for a glance and useless for the
        one line the FC decides to read -- this is that line's way out (owner
        ask 2026-08-15). What is SAID is composed here, purely; the window it
        is said in belongs to the host (``HudHost.show_intel_detail``).

        The tile's own rect travels with it so the host can always place the
        pop-up: a click that produced nothing visible would read as a dead
        feature. Tk thread, user click only -- never the beat."""
        tile = self._tiles.get("intel")
        rect = _call(getattr(tile, "rect", None)) if tile is not None else None
        _call(getattr(self._host, "show_intel_detail", None),
              intel_detail_title(row), intel_detail_body(row), rect)

    # -- intel push ----------------------------------------------------------
    def on_intel_line(self, channel: str, text: str, when=None) -> None:
        """Take one rendered intel line from fc_gui's fan-out. TK THREAD.

        Annotation happens ONCE, here, rather than per repaint. `channel` is
        accepted for symmetry with the fan-out (and for a future per-channel
        filter); the tile itself does not display it.

        TWO gates, deliberately asymmetric:

        * **master off** (the SHIPPED DEFAULT) returns immediately, BEFORE
          ``intel_stream.annotate``. Every FC has this feature switched off
          until he turns it on, and this sits on the intel fan-out -- a
          firehose during a fight -- so a feature nobody enabled must cost one
          attribute read per line, not a regex pass over it;
        * **master on, intel TILE off** keeps COLLECTING. Annotation of one
          line is cheap next to the beat that is already running, and it buys
          the tile a full history the instant it is enabled instead of an
          empty box that fills up over the next minute -- which is precisely
          when an FC turns it on.
        """
        if not text or not self._enabled:
            return
        text = str(text)
        try:
            spans = intel_stream.annotate(text) or []
        except Exception:
            spans = []
        system_ids = []
        priority = False
        is_clear = False
        for span in spans:
            kind = getattr(span, "kind", "")
            if kind in PRIORITY_SPAN_KINDS:
                priority = True
                continue
            # The all-clear rides the SAME annotation (no second matcher, no
            # `intel_monitor` import on this path): one `clear` span is the
            # whole signal, and the colour rule reads it off the row.
            if kind == CLEAR_SPAN_KIND:
                is_clear = True
                continue
            if kind != "system":
                continue
            system_id = _as_int((getattr(span, "payload", None) or {})
                                .get("system_id"), 0)
            if system_id and system_id not in system_ids:
                system_ids.append(system_id)
        self.intel_entries.append(IntelEntry(ts=_clock(when), text=text,
                                             system_ids=tuple(system_ids),
                                             priority=priority,
                                             is_clear=is_clear))
        # Collection continues while the tiles are withdrawn (same reason the
        # per-tile switch does not gate it: the reveal must land on a full
        # history, not an empty box), but the REPAINT does not -- writing into
        # a withdrawn window is a Tk round-trip nobody can see. One read of
        # `_tile_visible` covers both visibility layers.
        if self._enabled and self._tile_visible.get("intel", False):
            self._render("intel")

    # -- layout commands -----------------------------------------------------
    def tile_rects(self) -> list:
        """VISIBLE tile rects, for fc_gui to merge into the PREVIEW snap
        provider. Full window heights, strip included (see info_tile's module
        note about preview's body-height convention).

        Visible, not merely live (2026-08-03): a tile withdrawn by either
        visibility layer is not a snap target. An FCPreview tile jumping to
        the edge of a window that is not on screen -- because the owner has no
        fleet open, or because the HUD is hidden while he arranges previews
        with EVE in the background -- reads as a broken snap, not as a
        feature. The tiles keep their geometry throughout and rejoin the
        provider the moment they are mapped again, so this costs nothing on
        the way back.

        The MIRROR of the ``HudHost.preview_rects`` contract, and it runs the
        other way: preview_tile's snap math ADDS its own ``STRIP_H`` to every
        rect its provider hands it, so a caller feeding these full-height
        rects into that provider must SUBTRACT ``preview_tile.STRIP_H`` first
        or every info tile will look 20 px taller than it is to a preview tile
        snapping against it. Same reason as on the way in, the arithmetic
        belongs to fc_gui: this module never imports preview_tile."""
        rects = []
        for key, tile in self._tiles.items():
            if not self._tile_visible.get(key, False):
                continue
            rect = _call(tile.rect)
            if rect:
                rects.append(tuple(rect))
        return rects

    def arrange(self) -> None:
        """Re-place every live tile on the default grid and persist it. One of
        the three sanctioned ``place()`` paths (spawn / arrange / drag)."""
        keys = [k for k in TILE_SPECS if k in self._tiles]
        if not keys:
            return
        rects = self._grid_rects(keys)
        for key in keys:
            x, y, w, h = rects[key]
            _call(self._tiles[key].place, x, y, w, h)
            self._persist_rect(key, x, y, w, h)
        self._save()

    def reset_layouts(self) -> None:
        """Forget every saved rect, then re-arrange -- the way back from a tile
        dragged off a monitor that no longer exists.

        The save is NOT delegated to ``arrange`` alone: with no tile on screen
        (master off, or every tile closed -- which is exactly the state an
        owner is in when a saved rect is what he wants gone) ``arrange`` has
        nothing to place and returns before saving, so the clear would live
        only in memory and die with the process. Whenever the block actually
        moved, this writes."""
        block = self._block(create=True)
        cleared = bool(block.get("layouts"))
        block["layouts"] = {}
        # `arrange` re-places + re-persists + saves, but only when there is
        # something live to place; its gate is exactly `self._tiles`.
        placing = bool(self._tiles)
        self.arrange()
        if cleared and not placing:
            self._save()

    def match_preview_size(self) -> bool:
        """One-shot: resize every HUD tile -- live or merely saved -- to the
        FCPreview tiles' current (w, full height), keeping each tile's own
        (x, y). The popup's "Match preview size" button; NOT a per-tick path
        -- like ``arrange()``, its ``place()`` calls are one of the
        sanctioned retop sites (a user-initiated click, never the beat).

        A key that is neither live nor carries a stored layout is SKIPPED:
        there is no position to keep it at, and inventing one is
        ``arrange``'s job, not this one's. ``HudHost.preview_tile_size`` is
        optional and read through the guarded-call helper -- a host that
        never wired it, one that raises, or one that answers anything other
        than a plain (w, h) pair all make this a safe no-op. A numeric
        answer under the floor is clamped exactly like any other stored
        size, through ``InfoTileWindow``'s OWN (taller) floor -- these are
        info tiles, not FCPreview tiles. A malformed stored layout (not a
        proper [x, y, w, h] list -- a hand-edited config, say) is SKIPPED
        exactly like ``heal_info_tile_layouts``'s own malformed net: not
        repaired, not zeroed to (0, 0).

        Returns True iff at least one tile's stored size actually changed,
        so the popup button (and the tests) can tell "did something" from
        "nothing to resize"; config is saved AT MOST once regardless of how
        many tiles moved, and a key whose stored rect already matches the
        target [x, y, w, h] is left untouched -- a repeat click on an
        unchanged HUD costs no write.
        """
        raw = _call(self._host.preview_tile_size)
        try:
            w, h = raw
            w, h = int(w), int(h)
        except (TypeError, ValueError, OverflowError):
            return False
        w, h = preview_layout.clamp_size(w, h, InfoTileWindow.MIN_W,
                                         InfoTileWindow.MIN_H)
        stored = self._block().get("layouts")
        stored = stored if isinstance(stored, dict) else {}
        changed = False
        for key in TILE_SPECS:
            tile = self._tiles.get(key)
            if tile is not None:
                rect = _call(tile.rect)
                try:
                    x, y = _as_int(rect[0]), _as_int(rect[1])
                except (TypeError, IndexError, KeyError):
                    continue
                _call(tile.place, x, y, w, h)
            else:
                saved = stored.get(key)
                try:
                    x, y = int(saved[0]), int(saved[1])
                except (TypeError, ValueError, IndexError, KeyError,
                        OverflowError):
                    continue      # neither live nor positioned -- nothing to keep
            if stored.get(key) == [x, y, w, h]:
                continue          # unchanged -- no redundant persist/write
            self._persist_rect(key, x, y, w, h)
            changed = True
        if changed:
            self._save()
        return changed

    def shutdown(self) -> None:
        self.resolver.stop()
        for key in list(self._tiles):
            self._teardown(key)
        self._enabled = False


# ── settings: the launcher button + the popup ───────────────────────────────

#: The one live settings window, if any. A non-grabbing window can be opened
#: twice, so its opener owns the single-window guard the grab used to provide
#: implicitly.
_SETTINGS_WINDOW = None

_TIP_MASTER = "Show the FC HUD info tiles over the EVE clients."
_TIP_TILE = "Each tile is enabled on its own; the master switch alone shows nothing."
_TIP_JUMPS = ("Intel from systems farther than this many gate jumps from the "
              "reference system is not shown. Lines whose distance is not "
              "known yet are shown with ?j.")
_TIP_REFERENCE = ("Measure jumps from this system. Blank = automatic: your "
                  "own location, else the staging system.")
_TIP_OPACITY = "Tile opacity, applied to every tile."
_TIP_SNAP = "Tiles stick to each other, to preview tiles and to screen edges."
_TIP_LOCK = "Freeze tile positions. The close glyph still works."
_TIP_ARRANGE = "Re-place the visible tiles on the default grid."
_TIP_RESET = "Forget saved positions and re-place the tiles."
_TIP_MATCH_PREVIEW = ("Resize every tile to the FCPreview tiles' current "
                      "size, keeping each tile's position.")


def build_hud_button(parent, open_cmd):
    """The launcher fc_gui packs under the FCPreview settings block."""
    return ttk.Button(parent, text="FC HUD...", style="Dark.TButton",
                      command=open_cmd)


def _grid_row(parent, row, text, widget, tip):
    label = tk.Label(parent, text=text, font=_FONT_ROW, bg=ui_theme.BG_DARK,
                     fg=ui_theme.FG_TEXT, anchor="w")
    label.grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
    widget.grid(row=row, column=1, sticky="w", pady=2)
    attach_tooltip(label, tip)
    return widget


def _check(parent, var, text, tip):
    """A themed checkbox. No ``command=``: the boxes apply through their
    variable's write trace, so a click and a programmatic set take the SAME
    path (and a control cannot apply twice per click). The typed reference-
    system field is the one control that is NOT trace-driven -- a trace there
    fires per KEYSTROKE (see ``apply_reference``)."""
    box = tk.Checkbutton(parent, text=text, variable=var,
                         font=_FONT_ROW, bg=ui_theme.BG_DARK,
                         fg=ui_theme.FG_TEXT, activebackground=ui_theme.BG_DARK,
                         activeforeground=ui_theme.FG_TEXT,
                         selectcolor=ui_theme.BG_ENTRY, anchor="w",
                         highlightthickness=0)
    attach_tooltip(box, tip)
    return box


def open_hud_settings(root, controller, host):
    """Open (or re-focus) the FC HUD settings popup and return its Toplevel.

    **Never grabs.** A Tk grab is application-wide: it would deafen the
    FCPreview tiles -- how the FC switches EVE clients mid-fight -- while their
    DWM thumbnails kept animating, so the failure would read as "the previews
    froze" (v4.1.0). This window is REFERENCE class: the owner reads and
    adjusts it WHILE FLYING, with the tiles live in front of him. Hence also:
    singleton (a second click raises the live window instead of stacking a
    duplicate whose controls would fight the first's), ``<Escape>`` closes, and
    every control LIVE-APPLIES -- writes config, saves, and calls the
    controller -- so the effect is visible without an OK button. Clicked and
    spun controls apply on change; the one TYPED field applies when it is left
    or Enter is pressed (and on close), because a per-keystroke apply here
    means a per-keystroke config write.

    ``ui_helpers.make_modal`` is deliberately not used: its contract grabs by
    default, and this window needs none of the rest of it badly enough to risk
    a future edit flipping that flag back.
    """
    global _SETTINGS_WINDOW
    existing = _SETTINGS_WINDOW
    if existing is not None:
        try:
            if existing.winfo_exists():
                existing.deiconify()
                existing.lift()
                existing.focus_set()     # focus, never grab
                # A tile can have been closed by its own strip X since this
                # window was built, so re-open re-reads config rather than
                # showing a control that disagrees with the screen.
                _call(getattr(existing, "hud_sync", None))
                return existing
        except tk.TclError:
            pass
        _SETTINGS_WINDOW = None

    win = tk.Toplevel(root)
    win.title("FC HUD")
    win.configure(bg=ui_theme.BG_DARK)
    try:
        win.transient(root)
    except tk.TclError:
        pass

    def _close(_event=None):
        global _SETTINGS_WINDOW
        # A reference system still being typed is not lost: Escape and the
        # Close button can both fire without the entry ever losing focus, and
        # this window has no OK to press. Guarded + no-op when nothing changed.
        _call(apply_reference)
        if _SETTINGS_WINDOW is win:
            _SETTINGS_WINDOW = None
        try:
            win.destroy()
        except tk.TclError:
            pass

    win.bind("<Escape>", _close)
    try:
        win.protocol("WM_DELETE_WINDOW", _close)
    except tk.TclError:
        pass

    def block():
        cfg = host.config if isinstance(host.config, dict) else {}
        found = cfg.get("info_tiles")
        if not isinstance(found, dict):
            found = default_info_tiles_config()
            if isinstance(cfg, dict):
                cfg["info_tiles"] = found
        return found

    def intel_block():
        found = block()
        tiles = found.get("tiles")
        if not isinstance(tiles, dict):
            tiles = {}
            found["tiles"] = tiles
        entry = tiles.get("intel")
        if not isinstance(entry, dict):
            entry = {"enabled": False, "max_jumps": DEFAULT_MAX_JUMPS,
                     "reference_system": ""}
            tiles["intel"] = entry
        return entry

    current = block()
    variables = {
        "enabled": tk.BooleanVar(win, bool(current.get("enabled", False))),
        "snap_enabled": tk.BooleanVar(win,
                                      bool(current.get("snap_enabled", True))),
        "lock_layout": tk.BooleanVar(win,
                                     bool(current.get("lock_layout", False))),
        "opacity": tk.StringVar(win, str(current.get("opacity",
                                                     DEFAULT_OPACITY))),
        "max_jumps": tk.StringVar(win, str(intel_block().get(
            "max_jumps", DEFAULT_MAX_JUMPS))),
        "reference_system": tk.StringVar(win, str(intel_block().get(
            "reference_system", "") or "")),
    }
    for key in TILE_SPECS:
        tiles = current.get("tiles") if isinstance(current.get("tiles"),
                                                   dict) else {}
        entry = tiles.get(key) if isinstance(tiles.get(key), dict) else {}
        variables[f"tile_{key}"] = tk.BooleanVar(
            win, bool(entry.get("enabled", False)))
    #: Exposed for the wiring tests (and for a future "apply" from fc_gui):
    #: the popup's controls ARE these variables.
    win.hud_vars = variables

    body = tk.Frame(win, bg=ui_theme.BG_DARK, padx=14, pady=12)
    body.pack(fill="both", expand=True)

    def apply_master(*_a):
        controller.set_enabled(bool(variables["enabled"].get()))

    def apply_tile(key):
        def _apply(*_a):
            controller.set_tile_enabled(key, bool(variables[f"tile_{key}"].get()))
        return _apply

    def apply_chrome(*_a):
        current_block = block()
        current_block["snap_enabled"] = bool(variables["snap_enabled"].get())
        current_block["lock_layout"] = bool(variables["lock_layout"].get())
        # Clamped at the STORE, never trusted from the widget: a Spinbox's
        # from_/to bounds its arrows only, and typed text arrives verbatim.
        current_block["opacity"] = _clamp_opacity(variables["opacity"].get())
        _call(host.save_config)
        controller.tick()

    def apply_intel(*_a):
        entry = intel_block()
        entry["max_jumps"] = _clamp_jumps(variables["max_jumps"].get())
        _call(host.save_config)
        controller.tick()

    def apply_reference(_event=None):
        """Persist the reference system on ``<FocusOut>``/``<Return>`` -- the
        ``fc_gui._autosave_staging_system`` cadence, and deliberately NOT the
        write trace every other control here uses.

        A system name is TYPED, and each keystroke's trace would cost a full
        config write (an atomic rewrite + fsync of the whole file) AND, via
        ``set_reference`` on the next beat, a wipe of every resolved distance
        -- so typing "P-ZMZV" would be six saves and six cache flushes, five of
        them for prefixes that name no system. The no-op guard makes a focus
        pass over an untouched field free, which matters because tabbing
        through the popup produces one ``<FocusOut>`` per visit."""
        entry = intel_block()
        text = str(variables["reference_system"].get()).strip()
        if entry.get("reference_system") == text:
            return
        entry["reference_system"] = text
        _call(host.save_config)
        controller.tick()

    _check(body, variables["enabled"], "Show info tiles",
           _TIP_MASTER).pack(anchor="w")
    for key in TILE_SPECS:
        _check(body, variables[f"tile_{key}"],
               f"  {TILE_SPECS[key]['title']} tile",
               _TIP_TILE).pack(anchor="w")

    grid = tk.Frame(body, bg=ui_theme.BG_DARK)
    grid.pack(fill="x", pady=(8, 4))
    _grid_row(grid, 0, "Intel jumps", tk.Spinbox(
        grid, from_=0, to=MAX_JUMPS_CEILING, width=5,
        textvariable=variables["max_jumps"], font=_FONT_ROW,
        bg=ui_theme.BG_ENTRY, fg=ui_theme.FG_TEXT,
        insertbackground=ui_theme.FG_TEXT, highlightthickness=0), _TIP_JUMPS)
    reference_entry = _grid_row(grid, 1, "Reference system", tk.Entry(
        grid, width=18, textvariable=variables["reference_system"],
        font=_FONT_ROW, bg=ui_theme.BG_ENTRY, fg=ui_theme.FG_TEXT,
        insertbackground=ui_theme.FG_TEXT, highlightthickness=0),
        _TIP_REFERENCE)
    reference_entry.bind("<FocusOut>", apply_reference, add="+")
    reference_entry.bind("<Return>", apply_reference, add="+")
    #: Exposed like ``hud_vars``: the wiring tests drive the apply cadence
    #: through the widget, because the cadence IS the widget's bindings.
    win.hud_reference_entry = reference_entry
    _grid_row(grid, 2, "Opacity", tk.Spinbox(
        grid, from_=MIN_OPACITY, to=1.0, increment=0.02, width=5,
        textvariable=variables["opacity"], font=_FONT_ROW,
        bg=ui_theme.BG_ENTRY, fg=ui_theme.FG_TEXT,
        insertbackground=ui_theme.FG_TEXT, highlightthickness=0), _TIP_OPACITY)

    _check(body, variables["snap_enabled"], "Snap to edges",
           _TIP_SNAP).pack(anchor="w")
    _check(body, variables["lock_layout"], "Lock layout",
           _TIP_LOCK).pack(anchor="w")

    buttons = tk.Frame(body, bg=ui_theme.BG_DARK)
    buttons.pack(fill="x", pady=(10, 0))
    arrange = ttk.Button(buttons, text="Arrange tiles", style="Dark.TButton",
                         command=controller.arrange)
    arrange.pack(side="left")
    attach_tooltip(arrange, _TIP_ARRANGE)
    reset = ttk.Button(buttons, text="Reset positions", style="Dark.TButton",
                       command=controller.reset_layouts)
    reset.pack(side="left", padx=(6, 0))
    attach_tooltip(reset, _TIP_RESET)
    match_preview = ttk.Button(buttons, text="Match preview size",
                               style="Dark.TButton",
                               command=controller.match_preview_size)
    match_preview.pack(side="left", padx=(6, 0))
    attach_tooltip(match_preview, _TIP_MATCH_PREVIEW)
    close = ttk.Button(buttons, text="Close", style="Dark.TButton",
                       command=_close)
    close.pack(side="right")

    def sync_from_config(_event=None):
        """Pull every control back into line with config.

        Only ever writes a variable that actually DIFFERS, so a sync with no
        drift fires no traces -- and therefore no config write and no save.
        """
        found = block()
        intel = intel_block()
        tiles = found.get("tiles") if isinstance(found.get("tiles"),
                                                 dict) else {}
        pairs = [("enabled", bool(found.get("enabled", False))),
                 ("snap_enabled", bool(found.get("snap_enabled", True))),
                 ("lock_layout", bool(found.get("lock_layout", False))),
                 ("opacity", str(found.get("opacity", DEFAULT_OPACITY))),
                 ("max_jumps", str(intel.get("max_jumps",
                                             DEFAULT_MAX_JUMPS))),
                 ("reference_system", str(intel.get("reference_system", "")
                                          or ""))]
        for key_name in TILE_SPECS:
            entry = (tiles.get(key_name)
                     if isinstance(tiles.get(key_name), dict) else {})
            pairs.append((f"tile_{key_name}", bool(entry.get("enabled",
                                                             False))))
        for name, value in pairs:
            variable = variables[name]
            try:
                if variable.get() != value:
                    variable.set(value)
            except tk.TclError:
                continue

    win.hud_sync = sync_from_config

    # Write traces for the CLICKED and SPUN controls: a checkbox click and a
    # spinner step are each one whole edit, so one apply per change is exactly
    # right. The typed reference-system field is the exception and is bound to
    # <FocusOut>/<Return> above -- per-keystroke there means per-keystroke
    # config writes and cache flushes (see `apply_reference`).
    for name, handler in (("enabled", apply_master),
                          ("opacity", apply_chrome),
                          ("snap_enabled", apply_chrome),
                          ("lock_layout", apply_chrome),
                          ("max_jumps", apply_intel)):
        variables[name].trace_add("write", handler)
    for key in TILE_SPECS:
        variables[f"tile_{key}"].trace_add("write", apply_tile(key))

    _SETTINGS_WINDOW = win
    return win
