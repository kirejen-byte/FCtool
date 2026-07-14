# ui_theme.py
"""Shared dark palette — the single source of truth for FCTool's house colors.

Containment-safe leaf module: it imports NOTHING (stdlib or otherwise) and MUST
never import ``fc_gui`` or any feature module. That is what lets both ``fc_gui``
and every standalone window module (fleet templates, infra manager, overview
manager/editor, markup editor, ...) share ONE palette without the copy-paste
value drift that previously shipped two different dark themes in one app
(navy ``#1a1a2e``/cyan here vs a stray gray ``#1a1a1a`` set — see
OPTIMIZATION_REVIEW.md finding D1).

fc_gui re-exports these names at module scope, so ``fc_gui.BG_DARK`` (and every
sibling) still resolves for existing callers and tests. Window modules import the
names they need directly from here.

Palette = the canonical FCTool navy scheme.
"""
from __future__ import annotations

# ── Backgrounds ────────────────────────────────────────────────────────────────
BG_DARK = "#1a1a2e"       # window / dialog base
BG_PANEL = "#16213e"      # framed panels, bars, menus
BG_ENTRY = "#0f3460"      # entry / text / listbox fields

# ── Foregrounds ─────────────────────────────────────────────────────────────────
FG_TEXT = "#e0e0e0"       # primary body text
FG_DIM = "#888899"        # muted / secondary text
FG_ACCENT = "#00d4ff"     # cyan accent (links, selection, headers)
FG_GREEN = "#00ff88"      # ok / in-position / online
FG_RED = "#ff4444"        # error / dead / offline
FG_ORANGE = "#ff8c00"     # warning / partial
FG_YELLOW = "#ffdd00"     # attention / needs-action
FG_WHITE = "#ffffff"      # emphasis / default markup color
FG_MAGENTA = "#ff66ff"    # rare highlight
FG_UNDER = "#ff6666"      # underline / soft-alert accent

# ── Lines ────────────────────────────────────────────────────────────────────────
BORDER_COLOR = "#2a2a4a"  # rules, separators, frame borders

__all__ = [
    "BG_DARK", "BG_PANEL", "BG_ENTRY",
    "FG_TEXT", "FG_DIM", "FG_ACCENT", "FG_GREEN", "FG_RED", "FG_ORANGE",
    "FG_YELLOW", "FG_WHITE", "FG_MAGENTA", "FG_UNDER",
    "BORDER_COLOR",
]
