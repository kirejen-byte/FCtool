# doctrine_refits.py
"""Doctrine refits — the chip/menu MODEL, and the thin Tk widgets that draw it.

Design: ``docs/superpowers/specs/2026-09-11-doctrine-refits-design.md`` (§7.1 the
member-row refit strip, §7.3 the ``Refits ▾`` swap menu, §11 bite B2).

Vocabulary (identical in the spec, this code and the UI):

* **slot** — one ``DoctrineMember``. Owns the tags / ideal / seed target.
* **refit** — one of the fits that slot can fly; ``member.refits`` is the ordered
  list of their fit ids. ``refits == []`` is today's plain member.
* **active refit** — ``member.fit_id``. Every non-refit consumer in the app reads
  that field and so keeps seeing exactly one fit per slot.
* **default refit** — ``refits[0]``; what "Reset refits to defaults" returns to.

Two rules this module exists to keep:

* **It imports no ``fc_gui`` and no store.** Members and fits arrive DUCK-TYPED
  (``member.fit_id`` / ``.refits`` / ``.tags``; ``fit.id`` / ``.name`` /
  ``.hull_name``), lookups arrive as a ``get_fit`` callable, and every action is
  an injected callback. That is what lets the model be tested with plain
  namespaces and the widgets be reused by two different hosts (the Doctrines
  member row and the Fleet/MOTD ``Refits ▾`` buttons).
* **Everything above the widget-section separator is PURE** — no GUI imports at
  all — so the model is importable and usable headlessly. A source-text guard in
  ``tests/test_doctrine_refits.py`` pins that separator; keep new pure helpers
  above it and new widget code below it.

Nothing here mutates anything: ``chip_row``/``menu_spec`` are total functions of
their inputs, and a missing or unreadable fit degrades to a labelled placeholder
rather than raising. That placeholder is DEFENCE IN DEPTH, not an expected
state: per the spec's §9 edge-case table the store's ``delete_fit`` cascade
drops a deleted fit from every slot's ``refits``, so a refit chip should never
read ``(missing fit)`` in practice. If one does, the row still renders and the
chip is still removable instead of the pane dying — that is the whole point.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Glyph vocabulary — shared by the chips and the menu so the two surfaces can
# never drift. There are no radiobutton menus in this app; the active entry is
# shown by this prefix instead.
ACTIVE_GLYPH = "●"
INACTIVE_GLYPH = "○"
ADD_GLYPH = "+"

MISSING_LABEL = "(missing fit)"
RESET_LABEL = "Reset refits to defaults"


@dataclass(frozen=True)
class RefitChip:
    """One chip in a member row's refit strip."""
    fit_id: str
    label: str
    active: bool
    is_default: bool
    tooltip: str


@dataclass(frozen=True)
class SlotSpec:
    """One cascade of the ``Refits ▾`` menu: a slot and its refits.

    ``entries`` is ``(fit_id, label, active)`` in refit order.
    """
    label: str
    entries: tuple[tuple[str, str, bool], ...]


@dataclass(frozen=True)
class MenuSpec:
    """The whole ``Refits ▾`` menu: the slots WITH refits, plus whether the
    reset entry does anything (False = every slot already flies its default)."""
    slots: tuple[SlotSpec, ...]
    reset_enabled: bool


def _refits_of(member) -> list[str]:
    """The slot's ordered refit ids — ``[]`` for a plain member, and for a
    member object that predates the ``refits`` field entirely.

    DE-DUPED, first occurrence wins. The store's uniqueness rule makes a repeat
    impossible, but a hand-edited library can carry ``["a", "a"]`` and the whole
    UI downstream is keyed by fit id: two chips for one id would collapse
    ``chip_widgets`` to one entry while drawing two active-looking chips, and
    the menu would show the same refit twice. Normalising here fixes every
    consumer at once (``chip_row``, ``menu_spec``, ``has_refits``)."""
    return list(dict.fromkeys(
        str(f) for f in (getattr(member, "refits", None) or [])))


_LOOKUP_FAILURE_LOGGED = False


def _get(get_fit, fit_id):
    """``get_fit(fit_id)`` with the lookup's own failure folded into "missing"
    — the model must never be the thing that raises inside a row render.

    A raising lookup is a bug in the HOST, not an expected state, so the first
    one in the process is logged at debug with its traceback; the rest stay
    quiet (this runs once per chip per re-render — a broken lookup would
    otherwise flood the log)."""
    global _LOOKUP_FAILURE_LOGGED
    if not callable(get_fit):
        return None
    try:
        return get_fit(fit_id)
    except Exception:
        if not _LOOKUP_FAILURE_LOGGED:
            _LOOKUP_FAILURE_LOGGED = True
            log.debug("doctrine_refits: fit lookup failed for %r "
                      "(further failures silenced)", fit_id, exc_info=True)
        return None


def _text(value) -> str:
    return str(value).strip() if value else ""


def _fit_label(fit_id, fit) -> str:
    """Fit name, else hull name, else the id's first 8 chars; ``(missing fit)``
    when the fit is gone (that chip stays in the row so it can be removed)."""
    if fit is None:
        return MISSING_LABEL
    return (_text(getattr(fit, "name", "")) or _text(getattr(fit, "hull_name", ""))
            or str(fit_id)[:8])


def _chip_tooltip(label, fit, is_default, availability_text) -> str:
    """First line: the fit, its source and "default" joined by ``·``; then the
    market availability line for THIS refit, when a snapshot covered it."""
    parts = [label]
    source = _text(getattr(fit, "source", "")) if fit is not None else ""
    if source:
        parts.append(source)
    if is_default:
        parts.append("default")
    lines = [" · ".join(parts)]
    if availability_text:
        lines.append(str(availability_text))
    return "\n".join(lines)


def chip_row(member, get_fit, availability: dict | None = None) -> list[RefitChip]:
    """The refit strip model for one member — ``[]`` for a plain member.

    One chip per id in ``member.refits``, IN ORDER: ``[0]`` is the default and
    ``member.fit_id`` is the active one. ``availability`` (optional) maps a fit
    id to a ready-made market line, appended to that chip's tooltip.
    """
    refits = _refits_of(member)
    if not refits:
        return []
    active_id = str(getattr(member, "fit_id", "") or "")
    avail = availability or {}
    chips = []
    for index, fit_id in enumerate(refits):
        fit = _get(get_fit, fit_id)
        label = _fit_label(fit_id, fit)
        is_default = index == 0
        chips.append(RefitChip(
            fit_id=fit_id,
            label=label,
            active=fit_id == active_id,
            is_default=is_default,
            tooltip=_chip_tooltip(label, fit, is_default, avail.get(fit_id)),
        ))
    return chips


def _members(doctrine) -> list:
    return list(getattr(doctrine, "members", None) or [])


def has_refits(doctrine) -> bool:
    """True when at least one slot carries refits — the visibility rule for both
    ``Refits ▾`` buttons."""
    return any(_refits_of(m) for m in _members(doctrine))


def _slot_hull(member, get_fit) -> str:
    """A slot's display name: the ACTIVE fit's hull, falling back down the same
    ladder as a chip label (name, then id prefix, then ``(missing fit)``)."""
    fit = _get(get_fit, getattr(member, "fit_id", ""))
    if fit is None:
        return MISSING_LABEL
    return (_text(getattr(fit, "hull_name", ""))
            or _fit_label(getattr(member, "fit_id", ""), fit))


def menu_spec(doctrine, get_fit) -> MenuSpec:
    """The ``Refits ▾`` menu model: one ``SlotSpec`` per slot WITH refits, in
    member order.

    Cascade label = the active fit's hull. When two or more of the LISTED slots
    share a hull the label is disambiguated with the slot's first tag (its role
    is what tells the FC which Muninn row this is), or — for an untagged slot —
    with the active fit's name. Plain members are not listed and so never
    collide with anything.
    """
    slots = [m for m in _members(doctrine) if _refits_of(m)]
    hulls = [_slot_hull(m, get_fit) for m in slots]
    clashing = {h for h in hulls if hulls.count(h) > 1}

    specs = []
    reset_enabled = False
    for member, hull in zip(slots, hulls):
        refits = _refits_of(member)
        active_id = str(getattr(member, "fit_id", "") or "")
        if active_id != refits[0]:
            reset_enabled = True
        entries = tuple(
            (fit_id, _fit_label(fit_id, _get(get_fit, fit_id)), fit_id == active_id)
            for fit_id in refits
        )
        label = hull
        if hull in clashing:
            tags = [t for t in (getattr(member, "tags", None) or []) if _text(t)]
            suffix = _text(tags[0]) if tags else _fit_label(
                active_id, _get(get_fit, active_id))
            label = f"{hull} · {suffix}"
        specs.append(SlotSpec(label=label, entries=entries))
    return MenuSpec(slots=tuple(specs), reset_enabled=reset_enabled)


# ══════════════════════════════════════════════════════════════════════════════
# Tk shell — the widgets. Everything above this line is pure (source-guarded).
# ══════════════════════════════════════════════════════════════════════════════
import tkinter as tk                                              # noqa: E402

from ui_helpers import attach_tooltip                              # noqa: E402
from ui_theme import BG_DARK, BG_PANEL, FG_ACCENT, FG_DIM, FG_TEXT  # noqa: E402

# The member row's tag chips are Consolas 8 labels with 2px internal padding and
# a 3px gap (fc_gui._render_doctrine_member_row); the refit chips deliberately
# reuse those constants so the two clusters read as one visual language.
CHIP_FONT = ("Consolas", 8)
_CHIP_PADX = 2
_CHIP_GAP = (0, 3)

# Palette keys a host may override (the strip is also built inside the MOTD/
# Fleet panes, which are the same BG_PANEL today — the seam exists so a future
# host on a different ground does not have to fork the widget).
_DEFAULT_PALETTE = {
    "bg": BG_PANEL,
    "fg": FG_TEXT,
    "dim": FG_DIM,
    "accent": FG_ACCENT,
    "on_accent": BG_DARK,
}


def _palette(overrides):
    out = dict(_DEFAULT_PALETTE)
    if overrides:
        out.update({k: v for k, v in dict(overrides).items() if v})
    return out


def _call(callback, *args):
    if callable(callback):
        callback(*args)


class RefitStrip(tk.Frame):
    """The ``Refits: ● Muninn Arty ○ Muninn AC +`` sub-line of a member row.

    Packed by the CALLER (the row owns its layout). Built once from a
    ``chip_row`` list; the Doctrines pane re-renders wholesale on every edit, so
    there is no update path to keep in sync — a swap rebuilds the strip.

    Seams (all injected, all optional):
      ``on_pick(fit_id)``   left-click on an INACTIVE chip (the active chip is
                            deliberately inert — clicking what is already
                            active must not dirty the library).
      ``on_add()``          left-click on the trailing ``+`` chip.
      ``on_context(fit_id, event)``  right-click on a refit chip.

    ``chip_widgets`` (fit id -> Label) is a TEST SEAM and a host seam; renaming
    it is an API change.
    """

    def __init__(self, parent, chips, *, on_pick=None, on_add=None,
                 on_context=None, palette=None):
        colours = _palette(palette)
        super().__init__(parent, bg=colours["bg"])
        self._palette_colours = colours
        self._on_pick = on_pick
        self._on_add = on_add
        self._on_context = on_context
        self.chip_widgets: dict[str, tk.Label] = {}

        tk.Label(self, text="Refits:", font=CHIP_FONT, fg=colours["dim"],
                 bg=colours["bg"]).pack(side=tk.LEFT, padx=(0, 4))
        for chip in chips or ():
            self.chip_widgets[chip.fit_id] = self._build_chip(chip)
        self.add_widget = self._build_add_chip()

    def _build_chip(self, chip):
        colours = self._palette_colours
        glyph = ACTIVE_GLYPH if chip.active else INACTIVE_GLYPH
        label = tk.Label(
            self, text=f" {glyph} {chip.label} ", font=CHIP_FONT,
            padx=_CHIP_PADX,
            # The active chip is inert, so it must NOT advertise a click.
            cursor="" if chip.active else "hand2",
            fg=colours["on_accent"] if chip.active else colours["dim"],
            bg=colours["accent"] if chip.active else colours["bg"])
        label.pack(side=tk.LEFT, padx=_CHIP_GAP)
        if not chip.active:
            label.bind("<Button-1>",
                       lambda ev, f=chip.fit_id: _call(self._on_pick, f))
        label.bind("<Button-3>",
                   lambda ev, f=chip.fit_id: _call(self._on_context, f, ev))
        if chip.tooltip:
            attach_tooltip(label, chip.tooltip)
        return label

    def _build_add_chip(self):
        colours = self._palette_colours
        label = tk.Label(self, text=f" {ADD_GLYPH} ", font=CHIP_FONT,
                         padx=_CHIP_PADX, cursor="hand2",
                         fg=colours["dim"], bg=colours["bg"])
        label.pack(side=tk.LEFT, padx=_CHIP_GAP)
        label.bind("<Button-1>", lambda ev: _call(self._on_add))
        attach_tooltip(label, "Add a refit to this ship")
        return label


def _make_menu(parent) -> tk.Menu:
    """A themed, tear-off-free menu — the app's one menu look (fc_gui
    ``_build_fit_context_menu``)."""
    return tk.Menu(parent, tearoff=0, bg=BG_PANEL, fg=FG_TEXT,
                   activebackground=FG_ACCENT, activeforeground=BG_DARK)


def build_refit_menu(parent, spec: MenuSpec, *, on_pick=None,
                     on_reset=None) -> tk.Menu:
    """Build (do NOT post) the ``Refits ▾`` menu for ``spec``.

    One cascade per slot, each entry prefixed by the active/inactive glyph; the
    active entry is DISABLED (picking it would be a no-op that still costs a
    save). Then the reset row, disabled when nothing is off its default.
    Returned for the caller to post and for tests to inspect without entering
    Tk's menu loop.
    """
    menu = _make_menu(parent)
    for slot in getattr(spec, "slots", ()) or ():
        sub = _make_menu(menu)
        for fit_id, label, active in slot.entries:
            glyph = ACTIVE_GLYPH if active else INACTIVE_GLYPH
            sub.add_command(
                label=f"{glyph} {label}",
                state=tk.DISABLED if active else tk.NORMAL,
                command=lambda f=fit_id: _call(on_pick, f))
        menu.add_cascade(label=slot.label, menu=sub)
    if menu.index("end") is not None:
        menu.add_separator()
    menu.add_command(
        label=RESET_LABEL, command=lambda: _call(on_reset),
        state=tk.NORMAL if getattr(spec, "reset_enabled", False) else tk.DISABLED)
    return menu


def make_refits_button(parent, on_post, *, palette=None) -> tk.Button:
    """The flat ``Refits ▾`` microbutton — the map_tab ``▾`` affordance
    (``map_tab.py`` ~1425-1433): no relief, no border, hand cursor, accent on
    hover. The caller owns packing and visibility."""
    colours = _palette(palette)
    return tk.Button(
        parent, text="Refits ▾", command=on_post, font=CHIP_FONT,
        bg=colours["bg"], fg=colours["fg"], activebackground=colours["bg"],
        activeforeground=colours["accent"], relief="flat", borderwidth=0,
        highlightthickness=0, padx=3, pady=0, cursor="hand2")


def post_menu_under(menu, button) -> None:
    """Post ``menu`` just under ``button`` — the app's anchoring for a ``▾``
    microbutton (``map_tab._show_chars_menu``).

    **This function OWNS the menu's lifetime**: menus here are minted per post
    (``build_refit_menu`` reads a fresh spec every time), so the teardown is
    armed here rather than left to the caller — otherwise every post leaks a
    Tk widget for the life of the window. Do NOT pass a long-lived menu.

    Two details are load-bearing, both from fc_gui's cured ``_post_menu``:
    the destroy hangs off ``<Unmap>`` (which fires when Tk unposts the menu)
    and is DEFERRED 100 ms — an immediate destroy races Tk's idle-scheduled
    command invocation and swallows the click; and ``grab_release`` runs in a
    ``finally``, so a menu that fails to post never leaves the pointer grabbed.
    """
    menu.bind("<Unmap>", lambda ev: menu.after(100, menu.destroy))
    try:
        x = button.winfo_rootx()
        y = button.winfo_rooty() + button.winfo_height() + 2
        menu.tk_popup(x, y)
    finally:
        menu.grab_release()
