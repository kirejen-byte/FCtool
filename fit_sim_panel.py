# fit_sim_panel.py
"""Fit-detail stats readout — the pure formatter, then its Tk shell.

The fittings library's detail pane grows one framed "Stats" block showing what
`fit_sim_stats.simulate` computed for the selected fit: DPS, volley, weapon
ranges, EHP, per-layer resists, the assumed command-link tier, and — only when
there is one — an honest "Unmodeled" line. Spec §5.5 of
``docs/superpowers/specs/2026-09-07-fit-stats-simulator-design.md``.

House shape (``range_check.py`` / ``cycle_roles.py``): the PURE core comes
first and imports nothing but the tier vocabulary; the Tk shell follows below,
with its own imports kept down beside it so the boundary is visible in the file
itself. Everything a formatter decides is therefore testable without a display,
and the shell is left with widget plumbing and nothing else.

Load-bearing decisions, each a place this readout could be quietly wrong:

* **The numbers are formatted, never recomputed.** Every value on screen is a
  field of the frozen :class:`fit_sim_stats.FitStats` handed in. This module
  owns no fitting maths at all — a discrepancy between the pane and the FC HUD
  aggregate can only ever be a different tier, never a different formula.
* **``partial`` is shown, not swallowed.** A fit carrying something the engine
  could not model gets a leading ``~`` on DPS and volley: the number is a
  floor, and an FC reading it off the pane is entitled to know that. The
  ``Unmodeled`` row names what was skipped.
* **``notes`` are NOT gaps and never take a row.** They are diagnostics (a hull
  bonus that found no matching module), so they ride a tooltip on the Links
  row rather than reading like a defect list. Only ``unmodeled`` — the real
  gaps — is promoted to a visible row.
* **A chargeless weapon still prints its range**, tagged ``(no charge)``: the
  gun is fitted and its reach is real, it just contributes no DPS. Dropping
  the row would make an empty-racked fit look unarmed rather than unloaded.
* **The panel never computes on the Tk thread.** It has no simulate call and
  no ``dogma_data`` import: the caller's worker does that and hands the result
  to :meth:`FitStatsPanel.set_result`. ``set_pending`` / ``set_unavailable``
  are the other two states, so the block is never blank and never stale.
* **No ``grab_set``** — anywhere in this module. A grab in this one-Tk-process
  app deafens the FCPreview tiles (map/preview.md), and this is a passive
  readout inside an existing pane, not a dialog.
"""
from __future__ import annotations

import fit_sim_links

# ===========================================================================
# pure core
# ===========================================================================

#: Below this, a number reads in full with thousands separators (``9,999``);
#: at or above it, compactly (``10.0k``). The split is where the third digit
#: stops being information: a DPS of 1,234 is a number an FC compares, a DPS
#: of 41,236 is "41.2k" and the 36 is noise.
COMPACT_FLOOR = 10_000

#: Metres below which a range prints in metres rather than kilometres.
METRE_FLOOR = 1_000

#: Marks a stat computed from a fit the engine could not fully model.
PARTIAL_MARK = "~"

#: What the ``skills`` field's vocabulary reads as on screen.
SKILL_LABELS = {"all_v": "All V"}

#: Hover copy for the Resists rows — the four numbers are positional, so the
#: order has to be stated SOMEWHERE or "0/20/40/50%" is unreadable.
RESIST_ORDER_TIP = ("Resist %, in order: EM / Thermal / Kinetic / Explosive\n"
                    "Rows: S = shield, A = armor, H = hull")

#: Hover copy for the Links row when the fit carries no notes.
LINKS_TIP = ("The command-link tier this readout assumes, and the discipline "
             "it was applied in.\nAll-V skills; the boost is a canonical "
             "booster, not a pilot in your fleet.")

#: One-letter layer tags, in :data:`fit_sim_stats.LAYER_SPECS` order.
LAYER_TAGS = {"shield": "S", "armor": "A", "hull": "H"}


#: Where the ``k`` band ends. Not 1e6: ``999,999`` would otherwise print as
#: ``"1000.0k"``, which is a wrong-looking number rather than a rounded one.
#: The bound is the value that rounds UP to 1000k at one decimal.
_K_CEILING = 999_950


#: The ``config["fittings"]`` keys this readout owns, and their defaults —
#: the same three values ``default_config.DEFAULT_CONFIG`` seeds. Kept HERE as
#: well because ``_load_config`` does not deep-merge: an existing config.json
#: that already carries a "fittings" block never gains the new keys, so every
#: read has to carry the default with it.
DEFAULTS = {
    "sim_enabled": True,
    "sim_links_tier": fit_sim_links.TIER_NONE,
    "sim_links_disciplines": fit_sim_links.MODE_AUTO,
}


def settings(config) -> tuple:
    """``(enabled, tier, disciplines)`` out of one app-config dict.

    Takes the config BY VALUE on every call and keeps nothing: the app replaces
    its config dict wholesale when settings are saved, so anything that held a
    reference would read from an orphan (CODEBASE_MAP, ``_save_settings``).

    Garbage — a missing block, a non-dict, a null, an empty string, or a value
    outside the tier/discipline vocabulary — falls back to :data:`DEFAULTS`
    rather than raising: a hand-edited config must not be able to stop the
    fittings pane from rendering, nor hand the Tk shell a Combobox value its
    ``values`` list doesn't contain.
    """
    block = config.get("fittings") if isinstance(config, dict) else None
    if not isinstance(block, dict):
        block = {}
    tier = str(block.get("sim_links_tier") or DEFAULTS["sim_links_tier"])
    if tier not in fit_sim_links.TIERS:
        tier = DEFAULTS["sim_links_tier"]
    disciplines = str(block.get("sim_links_disciplines")
                       or DEFAULTS["sim_links_disciplines"])
    if disciplines not in fit_sim_links.DISCIPLINE_MODES:
        disciplines = DEFAULTS["sim_links_disciplines"]
    return (bool(block.get("sim_enabled", DEFAULTS["sim_enabled"])),
            tier, disciplines)


def format_number(value) -> str:
    """``1234 -> "1,234"``, ``58200 -> "58.2k"``, ``1.2e6 -> "1.20M"``.

    One decimal throughout the ``k`` band — ``278.8k`` for an EHP figure, not
    ``279k``: thousands is where the readout's numbers live (EHP, volley, big
    DPS), and the tenth of a k is a real 100 HP the reader compares between two
    fits. Above it, three significant figures are plenty.

    Junk answers ``"0"`` rather than raising — this formats numbers that came
    out of a simulation over data that may be partial, and a readout must never
    be the thing that raises.
    """
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "0"
    if amount != amount or amount in (float("inf"), float("-inf")):
        return "0"
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    if amount < COMPACT_FLOOR:
        return f"{sign}{amount:,.0f}"
    if amount < _K_CEILING:
        return f"{sign}{amount / 1e3:.1f}k"
    for divisor, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if amount >= divisor:
            scaled = amount / divisor
            if scaled < 10:
                return f"{sign}{scaled:.2f}{suffix}"
            if scaled < 100:
                return f"{sign}{scaled:.1f}{suffix}"
            return f"{sign}{scaled:.0f}{suffix}"
    return f"{sign}{amount / 1e6:.2f}M"


def format_range(metres) -> str:
    """``7800 -> "7.8 km"``, ``120000 -> "120 km"``, ``850 -> "850 m"``.

    One decimal below 100 km and none above it: a 240.3 km missile flight time
    is spurious precision, while a 7.8 km optimal genuinely differs from 7 km.
    """
    try:
        amount = float(metres)
    except (TypeError, ValueError):
        return "0 m"
    if amount != amount or amount in (float("inf"), float("-inf")):
        return "0 m"
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    if amount < METRE_FLOOR:
        return f"{sign}{amount:,.0f} m"
    km = amount / 1000.0
    # Test the ROUNDED value, not the raw one: 99,950 m is 99.95 km, which
    # the one-decimal branch would print as "100.0 km" — a number that looks
    # rounded rather than one that is. round() puts it in the right branch.
    return (f"{sign}{km:.0f} km" if round(km, 1) >= 100
            else f"{sign}{km:.1f} km")


def format_weapon_range(row) -> str:
    """One :class:`fit_sim_stats.WeaponRange` as a line.

    ``7× 800mm Repeating Cannon II: 7.8 km + 87.2 km`` — the ``+ falloff`` half
    is OMITTED when the falloff is zero, because a missile has no falloff and
    ``+ 0 m`` would read as one that happens to be tiny. A weapon with no
    charge is tagged, not dropped (see the module docstring).
    """
    line = (f"{int(row.count)}× {row.name}: "
            f"{format_range(row.optimal_m)}")
    if row.falloff_m:
        line += f" + {format_range(row.falloff_m)}"
    if not row.loaded:
        line += " (no charge)"
    return line


def format_links(stats) -> str:
    """``"max · shield"`` — the tier, then what ``auto`` actually resolved to.

    The DISCIPLINES are what is reported, never the mode: "auto" tells the
    reader nothing about which tank the buff landed on, and
    ``disciplines_applied`` is exactly the answer the simulator reached.
    """
    tier = str(stats.links or fit_sim_links.TIER_NONE)
    applied = tuple(stats.disciplines_applied or ())
    if tier == fit_sim_links.TIER_NONE or not applied:
        return tier
    return f"{tier} · {', '.join(applied)}"


def format_stats(stats) -> list:
    """``[(label, value), …]`` — the whole readout, in display order.

    Rows repeat their label only ONCE: the second and third Range/Resists lines
    carry an empty label so the two-column grid reads as one block per stat
    rather than three identically-titled rows.

    A fit with no weapons contributes no Range rows at all (rather than a
    "none" placeholder) — the module list above it already says the racks are
    empty. ``Unmodeled`` appears only when there is something to name.
    """
    mark = PARTIAL_MARK if stats.partial else ""
    rows = [
        ("DPS", f"{mark}{format_number(stats.dps_total)}  "
                f"(T {format_number(stats.dps_turret)}"
                f" · M {format_number(stats.dps_missile)}"
                f" · D {format_number(stats.dps_drone)})"),
        ("Volley", f"{mark}{format_number(stats.volley)}"),
    ]
    for index, row in enumerate(stats.ranges or ()):
        rows.append(("Range" if index == 0 else "", format_weapon_range(row)))
    rows.append(
        ("EHP", f"{format_number(stats.ehp_total)}  "
                f"(S {format_number(stats.ehp_shield)}"
                f" · A {format_number(stats.ehp_armor)}"
                f" · H {format_number(stats.ehp_hull)})"))
    for index, (layer, resists) in enumerate(stats.resists or ()):
        tag = LAYER_TAGS.get(layer, str(layer)[:1].upper())
        numbers = "/".join(f"{value * 100:.0f}" for value in resists)
        rows.append(("Resists" if index == 0 else "", f"{tag} {numbers}%"))
    rows.append(("Links", format_links(stats)))
    rows.append(("Skills", SKILL_LABELS.get(stats.skills, str(stats.skills))))
    unmodeled = tuple(stats.unmodeled or ())
    if unmodeled:
        rows.append(("Unmodeled", ", ".join(unmodeled)))
    return rows


def links_tip(stats) -> str:
    """Hover copy for the Links row: the standing caveat, plus any ``notes``.

    ``notes`` are diagnostics rather than gaps (module docstring), so this is
    where they surface — visible on demand, never occupying a row.
    """
    tip = LINKS_TIP
    notes = tuple(getattr(stats, "notes", ()) or ())
    if notes:
        tip += "\n\n" + "\n".join(notes)
    return tip


# ===========================================================================
# Tk shell
# ===========================================================================

import tkinter as tk                                              # noqa: E402
from tkinter import ttk                                           # noqa: E402

# House palette, single source (ui_theme imports nothing and never drifts).
from ui_theme import BG_PANEL, FG_DIM, FG_GREEN, FG_TEXT          # noqa: E402
from ui_helpers import attach_tooltip                             # noqa: E402

PENDING_TEXT = "computing…"
UNAVAILABLE_TEXT = "stats unavailable"

_LABEL_FONT = ("Consolas", 9, "bold")
_VALUE_FONT = ("Consolas", 9)
_HEADER_FONT = ("Consolas", 9, "bold")
#: Wrap width for a value cell. The detail pane is ~380 px wide and the label
#: column eats ~75 of it; a long Unmodeled list must wrap rather than widen the
#: grid, which would push the pane's horizontal scroll out.
_VALUE_WRAP = 290


class FitStatsPanel:
    """The Stats block for one fit. Composition, not a ``tk.Frame`` subclass:
    the caller packs :attr:`frame` wherever it likes and the three state
    methods repaint its inside.

    Ctor seams:

    ``tier_var`` / ``disciplines_var``  the caller's ``tk.StringVar``s, created
                                        ONCE and reused across fits so the
                                        pick survives re-selecting a fitting.
                                        The panel reads and displays them; it
                                        never persists anything itself.
    ``on_change()``                     fired after a user pick in either
                                        combobox — the caller persists the
                                        value and re-requests the simulation.

    Three states, and it is always in exactly one: ``set_pending`` (the worker
    is running), ``set_result`` (numbers), ``set_unavailable`` (no table, or
    the simulation raised). The block is never left blank.
    """

    def __init__(self, parent, *, tier_var, disciplines_var, on_change):
        self._on_change = on_change
        #: The last rows :meth:`set_result` painted (``[]`` in the other two
        #: states) — the assertable twin of the widgets.
        self.rows: list = []
        #: ``"pending" | "result" | "unavailable"``.
        self.state = "pending"

        self.frame = tk.Frame(parent, bg=BG_PANEL)

        header = tk.Frame(self.frame, bg=BG_PANEL)
        header.pack(anchor=tk.W, fill=tk.X)
        tk.Label(header, text="Stats", font=_HEADER_FONT, fg=FG_GREEN,
                 bg=BG_PANEL).pack(side=tk.LEFT)
        tk.Label(header, text="links", font=_VALUE_FONT, fg=FG_DIM,
                 bg=BG_PANEL).pack(side=tk.LEFT, padx=(10, 3))
        self.tier_combo = ttk.Combobox(
            header, textvariable=tier_var, values=list(fit_sim_links.TIERS),
            state="readonly", width=8, font=_VALUE_FONT)
        self.tier_combo.pack(side=tk.LEFT)
        self.disciplines_combo = ttk.Combobox(
            header, textvariable=disciplines_var,
            values=list(fit_sim_links.DISCIPLINE_MODES),
            state="readonly", width=7, font=_VALUE_FONT)
        self.disciplines_combo.pack(side=tk.LEFT, padx=(3, 0))
        for combo in (self.tier_combo, self.disciplines_combo):
            combo.bind("<<ComboboxSelected>>", self._fire_change, add="+")

        self._grid = tk.Frame(self.frame, bg=BG_PANEL)
        self._grid.pack(anchor=tk.W, fill=tk.X, pady=(2, 0))
        self._grid.grid_columnconfigure(1, weight=1)

        self.set_pending()

    # ── state ────────────────────────────────────────────────────────────
    def set_pending(self) -> None:
        """The worker is running: say so rather than showing stale numbers."""
        self.state = "pending"
        self.rows = []
        self._message(PENDING_TEXT)

    def set_unavailable(self, reason: str) -> None:
        """No numbers, and WHY — a missing dogma table and a simulation that
        raised are different problems, and the pane is where either is seen."""
        self.state = "unavailable"
        self.rows = []
        text = UNAVAILABLE_TEXT
        if reason:
            text += f" ({reason})"
        self._message(text)

    def set_result(self, stats) -> None:
        """Paint one :class:`fit_sim_stats.FitStats`."""
        self.state = "result"
        self.rows = format_stats(stats)
        self._clear()
        # A continuation row (the 2nd/3rd Resists line) carries an empty
        # label — format_stats' own convention (module docstring) — so the
        # group a row belongs to has to be tracked forward, the same way
        # tests/test_fit_sim_panel.py's `_rows` helper reads the table back.
        group = ""
        for index, (label, value) in enumerate(self.rows):
            group = label or group
            name = tk.Label(self._grid, text=label, font=_LABEL_FONT,
                            fg=FG_DIM, bg=BG_PANEL, anchor=tk.NW)
            name.grid(row=index, column=0, sticky="nw", padx=(0, 6))
            cell = tk.Label(self._grid, text=value, font=_VALUE_FONT,
                            fg=FG_TEXT, bg=BG_PANEL, anchor=tk.W,
                            justify=tk.LEFT, wraplength=_VALUE_WRAP)
            cell.grid(row=index, column=1, sticky="w")
            if group == "Resists":
                attach_tooltip(name, RESIST_ORDER_TIP)
            elif group == "Links":
                attach_tooltip(cell, links_tip(stats))

    # ── internals ────────────────────────────────────────────────────────
    def _fire_change(self, _event=None):
        """A user pick in either combobox. Never fired by a programmatic
        ``StringVar.set`` — the caller re-seeding the vars for a newly selected
        fit must not look like the user changing the tier."""
        if callable(self._on_change):
            self._on_change()

    def _clear(self):
        for child in self._grid.winfo_children():
            child.destroy()

    def _message(self, text):
        self._clear()
        tk.Label(self._grid, text=text, font=_VALUE_FONT, fg=FG_DIM,
                 bg=BG_PANEL, anchor=tk.W).grid(row=0, column=0, columnspan=2,
                                                sticky="w")
