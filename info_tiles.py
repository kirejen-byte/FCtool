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
- **The battle tile never claims freshness and carries no delay caption.**
  Every string ``battle_lines`` composes is held to
  ``battle_ledger.assert_no_live_language`` (test-enforced), and the view's
  ``stamp_line`` is deliberately NOT rendered (owner directive 2026-07-30:
  players know killboard data trails a fight; the tile shows the numbers).
- **Intel fails OPEN.** A line whose distance is unknown is SHOWN with ``?j``;
  hiding a possibly-adjacent hostile report is the dangerous direction. A line
  naming no system never reaches this tile, and with no reference system the
  tile shows the unset notice and filters nothing rather than becoming a
  firehose.
- **The resolver is the only worker and never touches Tk.** Results reach the
  UI exclusively through the injected ``post_ui``.
- **No per-tick ``place()``.** ``InfoTileWindow.place`` routes through
  ``SetWindowPos(HWND_TOPMOST)`` -- it IS a retop, and a per-tick retop is the
  measured DWM-thrash / in-game FPS bug. Tiles are placed at spawn, on
  ``arrange``/``reset_layouts``, and by their own drags. Nowhere else.
- **Log strings stay ASCII** (this box's console is cp1252 and a stray glyph
  raises inside logging's StreamHandler). Tile TEXT may use anything -- it is
  never logged.
"""
from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime
from tkinter import ttk

import battle_ledger as bl
import battle_ledger_panel as blp
import intel_stream
import preview_layout
import system_coords
import ui_theme
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
_FONT_HEAD = ("Consolas", 8, "bold")

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
INTEL_KEEP = 50
INTEL_SHOW = 12

FLEET_MAX_ROWS = 8
OTHER_LABEL = "Other"

# Honest empty states. Each names the ACTUAL gap: the ESI members route is
# fleet-boss-only (it 403s otherwise), so "not boss" is a real answer, not a
# failure to fetch.
FLEET_STATUS_NO_AUTH = "No ESI character"
FLEET_STATUS_NOT_BOSS = "Not in fleet / not fleet boss"
FLEET_STATUS_NO_DATA = "No fleet data yet"

BATTLE_STATUS_NO_LEDGER = "No battle ledger"
INTEL_STATUS_EMPTY = "No intel in range"
# Named blind spot, not a silent empty tile: with nothing to measure from, the
# jump filter cannot mean anything, so the tile says so and shows nothing.
REFERENCE_UNSET = "No reference system - set staging or override"

# Intel keyword spans that mark a line worth the eye. `clear` is deliberately
# absent: an all-clear is the opposite signal.
PRIORITY_SPAN_KINDS = frozenset({"cyno", "camp", "spike"})

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

@dataclass(frozen=True)
class FleetCompModel:
    """What the fleet tile draws.

    `status` is "" when there are real numbers behind it, and otherwise names
    the gap (no character / not boss / no snapshot). `total` is 0 whenever
    `status` is set: a gated tile must not show a fleet size it cannot vouch
    for.

    Each row is ``(group_name, count, hull_breakdown)`` -- count-descending,
    top 8 plus an ``Other`` row that always sorts last -- where
    `hull_breakdown` is ``((hull_name, count), ...)``, also count-descending,
    summing to the row's count. The breakdown feeds the hover tooltip
    (``fleet_group_tip``); plain tuples rather than dicts so the whole model is
    frozen, hashable and diffable by ``==`` on the render path."""
    status: str
    total: int
    rows: tuple


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

    other = groups.pop(OTHER_LABEL, Counter())
    named = sorted(((g, sum(c.values()), c) for g, c in groups.items()),
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
    """Hover copy for one group row: ``75 Machariel, 5 Nestor, 1 Vindicator``.

    Mirrors ``battle_ledger_panel.composition_tip``'s job (what the count was
    actually made of) in this feature's own row shape. Pure, so the wording is
    testable without a display."""
    try:
        breakdown = row[2]
    except (TypeError, IndexError, KeyError):
        return ""
    return ", ".join(f"{count} {name}" for name, count in breakdown)


def fleet_row_text(row) -> str:
    """``  81  Battleship`` -- count right-aligned so the column reads down."""
    try:
        return f"{row[1]:>4}  {row[0]}"
    except (TypeError, IndexError, KeyError):
        return ""


# ── intel ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class IntelEntry:
    """One pushed intel line, annotated once at push time.

    Built by the controller from ``intel_stream.annotate`` and kept in the
    controller's OWN bounded deque. fc_gui's ``_intel_buffer`` is never read
    and never mutated -- this tile is a second consumer of the fan-out, not a
    second owner of the stream."""
    ts: str
    text: str
    system_ids: tuple
    priority: bool


@dataclass(frozen=True)
class IntelRowVM:
    """One rendered intel row. `jumps` is None when the distance is not (yet)
    known, which renders as ``?j`` -- see the fail-open rule."""
    ts: str
    system: str
    text: str
    jumps: int | None
    priority: bool


@dataclass(frozen=True)
class IntelTileModel:
    """The intel tile's whole state: rows plus the named reason there are
    none. Frozen so the renderer can diff it with ``==``."""
    status: str
    rows: tuple


def build_intel_model(entries, distance_of, max_jumps: int,
                      limit: int = INTEL_SHOW,
                      resolve_system_name=None) -> tuple:
    """Filter/annotate pushed intel entries into rows, newest LAST.

    `distance_of(system_id) -> int | None` is the resolver's cache read.

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
                               priority=bool(entry.priority)))
    cap = _as_int(limit, 0)
    return tuple(rows[-cap:]) if cap > 0 else tuple(rows)


def intel_row_text(row) -> str:
    """``12:04  Jita  10 reds gate  (2j)`` -- ``?j`` while unresolved."""
    badge = "?j" if row.jumps is None else f"{row.jumps}j"
    return f"{row.ts}  {row.system}  {row.text}  ({badge})"


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


# ── renderers ───────────────────────────────────────────────────────────────

class _LabelPool:
    """A fixed pool of row labels, built once and reused.

    Rebuilding widgets per repaint would churn Tk AND force a tooltip
    re-attach, and ``attach_tooltip`` binds with ``add="+"`` -- re-attaching
    stacks a fresh handler set every time. So the pool attaches ONCE per label
    at construction and re-words through ``update_tooltip`` afterwards."""

    def __init__(self, parent, size: int, palette: dict, tooltips=False):
        self._palette = palette
        self._tooltips = tooltips
        self._labels = []
        self._packed = []
        bg = palette.get("BG_DARK", ui_theme.BG_DARK)
        for _ in range(size):
            label = tk.Label(parent, text="", font=_FONT_ROW, bg=bg,
                             fg=palette.get("FG_TEXT", ui_theme.FG_TEXT),
                             anchor="w", justify="left")
            if tooltips:
                attach_tooltip(label, "")
            self._labels.append(label)
            self._packed.append(False)

    def render(self, items) -> None:
        """`items` is a sequence of ``(text, palette_key, tip_or_None)``."""
        for index, label in enumerate(self._labels):
            if index < len(items):
                text, colour_key, tip = items[index]
                label.configure(
                    text=text,
                    fg=self._palette.get(colour_key,
                                         self._palette.get("FG_TEXT")))
                if self._tooltips:
                    update_tooltip(label, tip or "")
                if not self._packed[index]:
                    label.pack(fill="x", padx=4)
                    self._packed[index] = True
            elif self._packed[index]:
                label.pack_forget()
                self._packed[index] = False


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
        self._pool.render([(text, colour, None)
                           for text, colour in battle_lines(view)])


class FleetRenderer(_TileRenderer):
    """Fleet size + inv-group rollup, each row hovering its hull breakdown."""

    def __init__(self, parent, palette: dict):
        super().__init__(parent, palette)
        self._head = tk.Label(self.frame, text="", font=_FONT_HEAD,
                              bg=self._palette.get("BG_DARK"),
                              fg=self._palette.get("FG_ACCENT"),
                              anchor="w")
        self._head.pack(fill="x", padx=4)
        self._pool = _LabelPool(self.frame, FLEET_MAX_ROWS + 1, self._palette,
                                tooltips=True)

    def _draw(self, model):
        if model.status:
            self._head.configure(text=model.status,
                                 fg=self._palette.get("FG_DIM"))
            self._pool.render([])
            return
        self._head.configure(text=f"Total: {model.total}",
                             fg=self._palette.get("FG_ACCENT"))
        self._pool.render([(fleet_row_text(row),
                            "FG_DIM" if row[0] == OTHER_LABEL else "FG_TEXT",
                            fleet_group_tip(row)) for row in model.rows])


class IntelRenderer(_TileRenderer):
    """Nearby intel, newest last, with a jump badge per row."""

    def __init__(self, parent, palette: dict):
        super().__init__(parent, palette)
        self._head = tk.Label(self.frame, text="", font=_FONT_ROW,
                              bg=self._palette.get("BG_DARK"),
                              fg=self._palette.get("FG_DIM"), anchor="w")
        self._pool = _LabelPool(self.frame, INTEL_SHOW, self._palette)
        self._head_packed = False

    def _draw(self, model):
        if model.status:
            # Status and rows are mutually exclusive (the controller sets a
            # status only when there is nothing to list). Rendering them that
            # way here too means the status label's pack order relative to the
            # row pool can never become visible -- `pack` APPENDS, so a status
            # that first appeared mid-session would otherwise land under the
            # rows it is supposed to explain.
            self._head.configure(text=model.status)
            if not self._head_packed:
                self._head.pack(fill="x", padx=4)
                self._head_packed = True
            self._pool.render([])
            return
        if self._head_packed:
            self._head.pack_forget()
            self._head_packed = False
        self._pool.render([(intel_row_text(row),
                            "FG_YELLOW" if row.priority else "FG_TEXT", None)
                           for row in model.rows])


#: key -> tile spec. Adding a tile type is one entry plus one renderer: the
#: chrome, layout persistence, snapping and settings all read this registry.
TILE_SPECS = {
    "battle": {"title": "Battle", "default_size": (260, 150),
               "render": BattleRenderer},
    "fleet": {"title": "Fleet", "default_size": (240, 200),
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
        stays closed across a restart."""
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
            self._spawn(key)
            self._render(key)
        else:
            self._teardown(key)

    def _tile_enabled(self, key) -> bool:
        return bool(self._tile_block(key).get("enabled", False))

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
        """Snap candidates for one tile: its SIBLINGS plus the preview family.
        The two registries stay separate and meet only here (polluting
        ``_preview_tile_rects`` with non-hwnd keys is a known regression
        class); each source is guarded on its own so a raising one costs only
        its own candidates."""
        rects = []
        for other_key, tile in self._tiles.items():
            if other_key == key:
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
        self._tiles[key] = tile
        self._renderers[key] = renderer
        x, y, w, h = self._spawn_rect(key)
        _call(tile.place, x, y, w, h)
        _call(tile.set_lock_layout, self._lock_layout())
        _call(tile.configure_snap, self._snap_enabled(), None,
              lambda k=key: self._neighbour_rects(k), self._screens)
        _call(tile.set_alpha, self._opacity())
        # place() records + positions; show() is what MAPS the window and owns
        # the single retop. Placing without showing leaves an invisible tile.
        _call(tile.show)
        if key == "intel":
            self.resolver.start()

    def _teardown(self, key):
        tile = self._tiles.pop(key, None)
        self._renderers.pop(key, None)
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
        model has not moved. It NEVER calls ``place()`` -- that is a retop."""
        if not self._enabled:
            return
        opacity = self._opacity()
        lock = self._lock_layout()
        snap = self._snap_enabled()
        for key in list(self._tiles):
            tile = self._tiles.get(key)
            if tile is None:
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
        return build_fleet_comp_model(
            members, ship_counts, total, authenticated, fleet_id, is_boss,
            resolve_type_name=lambda tid: offline_type_name(tid, catalog),
            resolve_group_name=lambda tid: offline_group_name(tid, catalog))

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
        lands. Tolerates the tile having gone away in the meantime."""
        if self._enabled and "intel" in self._tiles:
            self._render("intel")

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
        for span in spans:
            kind = getattr(span, "kind", "")
            if kind in PRIORITY_SPAN_KINDS:
                priority = True
                continue
            if kind != "system":
                continue
            system_id = _as_int((getattr(span, "payload", None) or {})
                                .get("system_id"), 0)
            if system_id and system_id not in system_ids:
                system_ids.append(system_id)
        self.intel_entries.append(IntelEntry(ts=_clock(when), text=text,
                                             system_ids=tuple(system_ids),
                                             priority=priority))
        if self._enabled and "intel" in self._tiles:
            self._render("intel")

    # -- layout commands -----------------------------------------------------
    def tile_rects(self) -> list:
        """Live tile rects, for fc_gui to merge into the PREVIEW snap provider.
        Full window heights, strip included (see info_tile's module note about
        preview's body-height convention).

        The MIRROR of the ``HudHost.preview_rects`` contract, and it runs the
        other way: preview_tile's snap math ADDS its own ``STRIP_H`` to every
        rect its provider hands it, so a caller feeding these full-height
        rects into that provider must SUBTRACT ``preview_tile.STRIP_H`` first
        or every info tile will look 20 px taller than it is to a preview tile
        snapping against it. Same reason as on the way in, the arithmetic
        belongs to fc_gui: this module never imports preview_tile."""
        rects = []
        for tile in self._tiles.values():
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
