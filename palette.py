# palette.py
"""Colour palette core — roles, presets, contrast rules, load / save / install.

A palette is EIGHT role colours ("big picture, not per button"); each role maps
onto one ``ui_theme`` constant (``THEME_MAP``). The status colours, white and
every other colour in the app are fixed. A palette is applied only at startup:
the boot hook reads ``ui_palette.json`` and ``install`` assigns the role colours
onto the ``ui_theme`` module BEFORE any consumer is imported, so every
``from ui_theme import X``, frozen dict, default argument and ttk style picks the
override up by itself. Nothing is re-themed live.

PURE: stdlib + ``app_io`` only. This module is reached from the boot hook and
from the group-import apply path, both of which run before ``ui_theme``'s
consumers exist — so it must NOT import ``ui_theme``, ``ui_helpers`` or
``tkinter``. That is why the stock colours (``DEFAULT``, ``STATUS``, ``WHITE``)
and the hex normaliser are typed here a second time; ``tests/test_palette.py``
pins each of them against the module it mirrors.

A palette that fails a contrast rule (``problems``) is never applied — ``load``,
``save`` and ``install`` each refuse it on their own: the fixed neon status
colours stay readable only on dark backgrounds, so the rules are what keep a
shared palette from making someone's FCTool unreadable.

Everything the boot hook or a validator reaches (``normalize_hex``,
``relative_luminance``, ``contrast``, ``problems``, ``validate``, ``load``,
``preset_name``, ``clean_name``) is TOTAL - garbage in, a clean value out,
never an exception.
``install`` is total over its ``roles`` argument only: a ``theme_module`` that is
``None`` or lacks attributes is the caller's error. ``save`` and ``reset`` are
user actions and report failure by raising.
"""
from __future__ import annotations

import json
import os

from app_io import atomic_write_json

# ── Roles ────────────────────────────────────────────────────────────────────
ROLES = ("window", "panel", "field", "text", "muted", "accent", "border",
         "selection")

ROLE_LABELS = {
    "window": "Window",
    "panel": "Panel",
    "field": "Field",
    "text": "Text",
    "muted": "Muted text",
    "accent": "Accent",
    "border": "Border",
    "selection": "Selection",
}

ROLE_HINTS = {
    "window": "Window and dialog base; text on accent fills",
    "panel": "Framed panels, bars, menus (the most-used colour)",
    "field": "Entries, lists, buttons, selected tab",
    "text": "Body text",
    "muted": "Secondary text",
    "accent": "Headers, links, selected-tab text",
    "border": "Rules, separators",
    "selection": "Hover and selection",
}

# role -> the ui_theme constant it overrides
THEME_MAP = {
    "window": "BG_DARK",
    "panel": "BG_PANEL",
    "field": "BG_ENTRY",
    "text": "FG_TEXT",
    "muted": "FG_DIM",
    "accent": "FG_ACCENT",
    "border": "BORDER_COLOR",
    "selection": "BG_SELECT",
}

# ── Fixed colours (not editable; mirrors of ui_theme's FG_* constants) ───────
STATUS = {
    "green": "#00ff88",
    "red": "#ff4444",
    "orange": "#ff8c00",
    "yellow": "#ffdd00",
    "magenta": "#ff66ff",
    "under": "#ff6666",
}
WHITE = "#ffffff"

_STATUS_LABELS = {
    "green": "Green",
    "red": "Red",
    "orange": "Orange",
    "yellow": "Yellow",
    "magenta": "Magenta",
    "under": "Soft red",
}

# ── Presets (every row passes every rule below) ──────────────────────────────
#                  window  panel   field   text    muted   accent  border  selection
_PRESET_ROWS = (
    ("Navy (stock)", "#1a1a2e #16213e #0f3460 #e0e0e0 #888899 #00d4ff #2a2a4a #1a5a90"),
    ("Graphite",     "#17181b #1f2125 #2b2f36 #e2e2e2 #8d929c #5ac8fa #33373e #35618c"),
    ("Deep Teal",    "#0f1b1e #12272b #143c44 #dfe8e8 #7f9a9c #2ee6c8 #1f3b40 #1b6a70"),
    ("Ember",        "#1c1410 #281b14 #43281a #ece2d8 #a08c7c #ffb347 #3d2a1e #7a4a1c"),
    ("Amethyst",     "#18142a #211a3c #34245e #e4e0f0 #948aaa #c79bff #302650 #56409a"),
    ("Verdant",      "#121c14 #16271a #1c3e24 #e0e8e0 #86988a #7ee081 #22382a #2c6a3a"),
    ("Steel",        "#141a22 #1a2330 #24344a #e0e4ea #8791a0 #f5c542 #2a3648 #33598a"),
)
PRESETS = {name: dict(zip(ROLES, row.split())) for name, row in _PRESET_ROWS}
# Its own dict: editing a preset in place must never re-tune the stock palette.
DEFAULT = dict(PRESETS["Navy (stock)"])

# ── Contrast rules (WCAG 2 contrast ratio) ───────────────────────────────────
TEXT_MIN = 4.5                 # Text on Window, Panel, Field
MUTED_MIN = 3.0                # Muted text on Window, Panel, Field
ACCENT_MIN = 3.0               # Accent on Window, Panel, Field
STATUS_MIN = 3.0               # each status colour on Window, Panel, Field
WINDOW_ON_ACCENT_MIN = 4.5     # dark text on accent buttons
WHITE_MIN = 4.5                # white on Selection, white on Field
SELECTION_VS_FIELD_MIN = 1.3   # a selected row must stand out from the list

FILE = "ui_palette.json"
# A real palette file is a few hundred bytes. ``load`` runs at boot on a file
# anyone may have dropped in, so it never PARSES more than this - and never
# reads more than one byte beyond it (the byte that shows the file is larger).
MAX_FILE_BYTES = 64 * 1024
# The palette's display name. ``save`` clamps it (``clean_name``), so a file
# this module writes is always far below ``MAX_FILE_BYTES``: without the clamp
# a long name made a file that ``load`` refused at the next start.
MAX_NAME = 80

_BACKGROUNDS = ("window", "panel", "field")
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_NOT_A_PALETTE = "not a palette"
# Removed from a name: every control character (C0, DEL, C1), the line and
# paragraph separators, and the surrogate code points (a surrogate in a str is
# always a lone one, and it cannot be encoded as UTF-8).
_NAME_REMOVED = dict.fromkeys(list(range(0x20)) + list(range(0x7F, 0xA0))
                              + [0x2028, 0x2029] + list(range(0xD800, 0xE000)))


# ── Colours ──────────────────────────────────────────────────────────────────
def normalize_hex(value):
    """``"#rgb"`` / ``"#rrggbb"`` (any case, surrounding space) -> lowercase
    ``"#rrggbb"``; anything else -> ``None``.

    The same answers as ``ui_helpers.normalize_hex_color(value, None)`` (parity
    is tested) — re-stated here because this module cannot import a Tk module.
    The digit check is an explicit set: ``int(x, 16)`` would also accept ``"_"``
    separators, a ``"0x"`` prefix and non-ASCII digits. Never raises.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    digits = text[1:]
    if (not text.startswith("#") or len(digits) not in (3, 6)
            or not all(ch in _HEX_DIGITS for ch in digits)):
        return None
    if len(digits) == 3:
        digits = "".join(ch * 2 for ch in digits)
    return "#" + digits.lower()


def relative_luminance(hex):
    """WCAG 2 relative luminance of a colour, 0.0 (black) .. 1.0 (white).

    Total: a value that is not a colour measures as 0.0.
    """
    colour = normalize_hex(hex)
    if colour is None:
        return 0.0
    linear = []
    for start in (1, 3, 5):
        channel = int(colour[start:start + 2], 16) / 255.0
        linear.append(channel / 12.92 if channel <= 0.03928
                      else ((channel + 0.055) / 1.055) ** 2.4)
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(a, b):
    """WCAG 2 contrast ratio of two colours, 1.0 (same) .. 21.0 (black/white).
    Symmetric."""
    la, lb = relative_luminance(a), relative_luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


# ── Validation ───────────────────────────────────────────────────────────────
def _resolve(roles):
    """A dict -> all eight roles: a missing role is filled from ``DEFAULT``, a
    present one is normalised (``None`` when its value is not a colour). Keys
    that are not roles are dropped."""
    return {role: (normalize_hex(roles[role]) if role in roles else DEFAULT[role])
            for role in ROLES}


def _measure(found, label, colour, word, other_label, other, minimum):
    """Append the rule's sentence when the pair is below ``minimum``. A pair with
    an unusable colour is not measured — the bad hex is reported on its own."""
    if colour is None or other is None:
        return
    ratio = contrast(colour, other)
    if ratio < minimum:
        # A ratio a hair under the minimum (2.997) must not round up to it:
        # "is 3.00 (needs 3.0)" reads as a pass that was refused.
        shown = min(round(ratio, 2), minimum - 0.01)
        found.append(f"{label} {word} {other_label} is {shown:.2f} "
                     f"(needs {minimum:.1f})")


def problems(roles):
    """Every reason ``roles`` may not be applied, one sentence each; ``[]`` for a
    usable palette.

    Missing roles are measured as their stock colour. A role whose value is not
    a colour gets its own sentence and the rules that need it are skipped. Each
    contrast sentence names both colours and the measured ratio, e.g.
    ``"Text on Field is 3.10 (needs 4.5)"`` — a failing ratio is never printed
    as the minimum itself (2.997 reads ``2.99``, not ``3.00``). The status rule
    reports only the WORST status colour per background. Total.
    """
    if not isinstance(roles, dict):
        return [_NOT_A_PALETTE]
    c = _resolve(roles)
    found = [f"{ROLE_LABELS[role]} is not a colour (needs #rrggbb)"
             for role in ROLES if c[role] is None]

    for role, minimum in (("text", TEXT_MIN), ("muted", MUTED_MIN),
                          ("accent", ACCENT_MIN)):
        for bg in _BACKGROUNDS:
            _measure(found, ROLE_LABELS[role], c[role], "on",
                     ROLE_LABELS[bg], c[bg], minimum)

    for bg in _BACKGROUNDS:
        if c[bg] is None:
            continue
        worst = min(STATUS, key=lambda name: contrast(STATUS[name], c[bg]))
        _measure(found, _STATUS_LABELS[worst] + " status colour", STATUS[worst],
                 "on", ROLE_LABELS[bg], c[bg], STATUS_MIN)

    _measure(found, ROLE_LABELS["window"], c["window"], "on",
             ROLE_LABELS["accent"], c["accent"], WINDOW_ON_ACCENT_MIN)

    for bg in ("selection", "field"):
        _measure(found, "White", WHITE, "on", ROLE_LABELS[bg], c[bg], WHITE_MIN)

    _measure(found, ROLE_LABELS["selection"], c["selection"], "against",
             ROLE_LABELS["field"], c["field"], SELECTION_VS_FIELD_MIN)
    return found


def validate(roles):
    """``(clean eight-role dict, [])`` for a usable palette, else
    ``(None, problems)``. The clean dict is new, normalised, and holds exactly
    the eight roles (missing ones filled from ``DEFAULT``). Total."""
    if not isinstance(roles, dict):
        return None, [_NOT_A_PALETTE]
    found = problems(roles)
    if found:
        return None, found
    return _resolve(roles), []


def preset_name(roles):
    """The name of the preset whose eight roles equal ``roles``, else
    ``"Custom"``. Total."""
    if not isinstance(roles, dict):
        return "Custom"
    view = {role: normalize_hex(roles.get(role)) for role in ROLES}
    for name, preset in PRESETS.items():
        if preset == view:
            return name
    return "Custom"


def clean_name(value):
    """A palette's display name as it is stored: ONE line, control characters
    removed, stripped, at most ``MAX_NAME`` characters. Anything that is not
    text is ``""``.

    The same answers as ``group_bundle.clean_text(value, MAX_NAME)`` for text
    (and stricter on C1 controls and line separators) - re-stated here because
    this module imports nothing of the project but ``app_io``. Total; cleaning
    a clean name changes nothing; the result always encodes as UTF-8.
    """
    if not isinstance(value, str):
        return ""
    return str.translate(value, _NAME_REMOVED).strip()[:MAX_NAME].rstrip()


# ── The file: {"version": 1, "name": str, "roles": {...}} ────────────────────
def load(app_dir):
    """Read the stored palette -> ``(roles, status)``.

    * no file                                   -> ``(None, "stock")``
    * unreadable, not a dict, or fails a rule   -> ``(None, "invalid")``
    * larger than ``MAX_FILE_BYTES``            -> ``(None, "invalid")``, unparsed
    * usable                                    -> ``(roles, "custom")``
    * usable but equal to ``DEFAULT``           -> ``(None, "stock")``

    ``None`` roles mean "leave ui_theme alone". Never raises: this runs at boot,
    before any window exists to show an error in.
    """
    try:
        # One bounded read (not a size check, then a read): the file cannot grow
        # in between. It asks for the limit PLUS ONE byte: a file of exactly
        # MAX_FILE_BYTES is still read whole, and that one extra byte is how a
        # larger file is told from it - nothing beyond it ever reaches memory.
        with open(os.path.join(app_dir, FILE), "rb") as f:
            raw = f.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            return None, "invalid"
        # utf-8-sig: a hand edit in an editor that adds a BOM still loads.
        data = json.loads(raw.decode("utf-8-sig"))
    except FileNotFoundError:
        return None, "stock"
    except Exception:
        return None, "invalid"
    if not isinstance(data, dict):
        return None, "invalid"
    roles, _found = validate(data.get("roles"))
    if roles is None:
        return None, "invalid"
    if roles == DEFAULT:
        return None, "stock"
    return roles, "custom"


def save(app_dir, roles, name=""):
    """Validate, then write ``ui_palette.json`` atomically. A palette that fails
    a rule raises ``ValueError`` (carrying the sentences) and writes nothing; an
    I/O failure propagates with the previous file intact.

    ``name`` is stored through ``clean_name``: one line of at most ``MAX_NAME``
    characters (``""`` when it is not text), so the file written here can
    never be larger than ``MAX_FILE_BYTES`` and be refused by ``load``."""
    clean, found = validate(roles)
    if clean is None:
        raise ValueError("palette not saved: " + "; ".join(found))
    atomic_write_json(
        os.path.join(app_dir, FILE),
        {"version": 1, "name": clean_name(name), "roles": clean},
        indent=1)


def reset(app_dir):
    """Back to stock: delete ``ui_palette.json``. Silent when it is not there."""
    try:
        os.remove(os.path.join(app_dir, FILE))
    except FileNotFoundError:
        pass


# ── Install ──────────────────────────────────────────────────────────────────
def install(theme_module, roles):
    """Assign the role colours onto ``theme_module`` (``ui_theme``); returns the
    constant names set, in ``ROLES`` order. ``roles`` ``None`` -> ``[]``.

    Must run BEFORE anything imports from the theme module by value. All or
    nothing: ``roles`` that are not eight colours set NOTHING and return ``[]``
    — a half-installed theme, or a non-string constant, would break every
    consumer. So do eight good colours that fail a contrast rule
    (``problems``): this is the last gate before the theme, and it does not
    rely on the caller having come through ``load`` / ``validate``.

    Total over ``roles``. A ``theme_module`` that is ``None`` or lacks
    attributes is the caller's error and is not guarded.
    """
    if not isinstance(roles, dict):
        return []
    values = [(THEME_MAP[role], normalize_hex(roles.get(role))) for role in ROLES]
    if any(value is None for _name, value in values):
        return []
    if problems(roles):
        return []
    for name, value in values:
        setattr(theme_module, name, value)
    return [name for name, _value in values]
