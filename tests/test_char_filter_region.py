"""Unit tests for the Characters-tab role+region item filter.

Filtering by a capability AND a region must intersect at *two* layers:

  1. the panel layer — a character panel is shown when it has ANY matching
     asset in the selected region (``FCToolGUI._char_matches_filter``), and
  2. the item layer — within a shown panel, only the asset entries actually in
     that region are rendered (``fc_gui._filter_cap_entries``).

The bug this pins: a character qualifying at the panel layer used to have ALL
their matching assets rendered, including ones in other regions (e.g. a
Dreadnought pilot filtered to Molden Heath still showed their dreads
everywhere). These tests exercise the Tk-free helper and the ``self``-agnostic
``_char_matches_filter`` bound method with a throwaway instance — no Tk window,
no network. Repo root is on sys.path via tests/conftest.py.
"""

import fc_gui
from fc_gui import _filter_cap_entries


# Region name constants (EVE nullsec/lowsec regions used in the bug report).
MH = "Molden Heath"
HEIM = "Heimatar"


def _dread(location, region):
    """Build an asset-cap entry shaped like the real renderer input."""
    return {"ship": "Naglfar", "location": location, "region": region}


# ── _filter_cap_entries (pure item-layer predicate) ─────────────────────────

def test_filter_cap_entries_intersects_region():
    """Only entries in the selected region survive; empty region keeps all;
    non-dict entries are dropped when a region filter is active."""
    entries = [
        _dread("Bosena - Home", MH),
        _dread("Rens VI - Moon 8", HEIM),
        _dread("Teonusude - Keepstar", MH),
        "legacy-string-entry",  # non-dict; must be dropped when filtering
    ]

    # Filtered to Molden Heath: only the two MH dreads remain, in order.
    only_mh = _filter_cap_entries(entries, MH)
    assert only_mh == [
        _dread("Bosena - Home", MH),
        _dread("Teonusude - Keepstar", MH),
    ]
    # The out-of-region dread and the non-dict entry are both gone.
    assert _dread("Rens VI - Moon 8", HEIM) not in only_mh
    assert "legacy-string-entry" not in only_mh

    # Empty / falsy region == no filtering: the list is returned unchanged
    # (including the non-dict entry), but as a distinct list object.
    passthrough = _filter_cap_entries(entries, "")
    assert passthrough == entries
    assert passthrough is not entries


def test_filter_cap_entries_no_match_empty():
    """A region with no matching entries yields an empty list."""
    entries = [
        _dread("Rens VI - Moon 8", HEIM),
        _dread("Hek VIII - Moon 12", HEIM),
    ]
    assert _filter_cap_entries(entries, MH) == []
    # Nothing at all also yields empty (and does not raise).
    assert _filter_cap_entries([], MH) == []


def test_filter_cap_entries_drops_non_dicts_only_when_filtering():
    """Non-dict entries pass through untouched with no region, but are
    dropped as soon as a region filter is applied."""
    entries = ["a", _dread("Bosena - Home", MH), 123, None]
    assert _filter_cap_entries(entries, "") == entries
    assert _filter_cap_entries(entries, MH) == [_dread("Bosena - Home", MH)]


# ── _char_matches_filter (panel-layer predicate — unchanged behavior) ────────

def _matches(info, cap_key, region):
    """Call the self-agnostic bound method against a throwaway instance."""
    return fc_gui.FCToolGUI._char_matches_filter(object(), info, cap_key, region)


def test_char_matches_filter_panel_matches_when_any_entry_in_region():
    """The two-layer contract: a panel still MATCHES when it has any asset in
    the region, even though most of that character's assets are elsewhere.
    The item layer (tested above) is what trims the out-of-region ones."""
    info = {
        "dreads": [
            _dread("Bosena - Home", MH),        # one in-region asset ...
            _dread("Rens VI - Moon 8", HEIM),   # ... amid several elsewhere
            _dread("Hek VIII - Moon 12", HEIM),
        ]
    }

    # Panel layer: matches on Molden Heath because ONE dread is there.
    assert _matches(info, "dreads", MH) is True
    # Item layer: only that one MH dread would render for this panel.
    assert _filter_cap_entries(info["dreads"], MH) == [_dread("Bosena - Home", MH)]

    # No region selected: panel matches purely on having the capability.
    assert _matches(info, "dreads", "") is True

    # A region where this character has no dreads: panel does NOT match.
    assert _matches(info, "dreads", "Delve") is False


def test_char_matches_filter_no_entries_never_matches():
    """A capability the character lacks never matches, region or not."""
    info = {"dreads": []}
    assert _matches(info, "dreads", "") is False
    assert _matches(info, "dreads", MH) is False
