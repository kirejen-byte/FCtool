"""Unit tests for the Tk-free intel-filter list helpers in ``fc_gui``.

These exercise the pure ``add_filter_item`` / ``add_coalition_item`` /
``remove_filter_item`` functions used by the Intelligence-tab filter panel to
mutate the location/parties lists. No Tk window is created — only the
module-level helpers are imported. Repo root is on sys.path via
tests/conftest.py.
"""

import pytest

from fc_gui import (
    add_filter_item,
    add_coalition_item,
    remove_filter_item,
    rename_coalition_in_selection,
    remove_coalition_from_selection,
)


# ── add_filter_item ─────────────────────────────────────────────────────────

def test_add_filter_item_appends_and_normalizes():
    out, added = add_filter_item([], {"id": 42, "name": "Delve"})
    assert added is True
    assert out == [{"id": 42, "name": "Delve"}]


def test_add_filter_item_coerces_string_id_and_strips_name():
    out, added = add_filter_item([], {"id": "7", "name": "  Jita  "})
    assert added is True
    assert out == [{"id": 7, "name": "Jita"}]


def test_add_filter_item_dedupes_by_id():
    existing = [{"id": 5, "name": "A"}]
    out, added = add_filter_item(existing, {"id": 5, "name": "A-again"})
    assert added is False
    assert out == [{"id": 5, "name": "A"}]  # unchanged, not duplicated


def test_add_filter_item_does_not_mutate_input():
    existing = [{"id": 1, "name": "A"}]
    out, added = add_filter_item(existing, {"id": 2, "name": "B"})
    assert added is True
    assert existing == [{"id": 1, "name": "A"}]  # original untouched
    assert len(out) == 2


def test_add_filter_item_rejects_missing_id():
    out, added = add_filter_item([{"id": 1, "name": "A"}], {"name": "no id"})
    assert added is False
    assert out == [{"id": 1, "name": "A"}]


def test_add_filter_item_rejects_non_numeric_id():
    out, added = add_filter_item([], {"id": "not-a-number", "name": "X"})
    assert added is False
    assert out == []


def test_add_filter_item_handles_none_items():
    out, added = add_filter_item(None, {"id": 9, "name": "Z"})
    assert added is True
    assert out == [{"id": 9, "name": "Z"}]


def test_add_filter_item_missing_name_becomes_empty():
    out, added = add_filter_item([], {"id": 3})
    assert added is True
    assert out == [{"id": 3, "name": ""}]


# ── add_coalition_item ──────────────────────────────────────────────────────

def test_add_coalition_item_appends():
    out, added = add_coalition_item([], "Imperium")
    assert added is True
    assert out == ["Imperium"]


def test_add_coalition_item_strips_whitespace():
    out, added = add_coalition_item([], "  Winter Coalition  ")
    assert added is True
    assert out == ["Winter Coalition"]


def test_add_coalition_item_dedupes():
    out, added = add_coalition_item(["Imperium"], "Imperium")
    assert added is False
    assert out == ["Imperium"]


def test_add_coalition_item_rejects_blank():
    out, added = add_coalition_item(["Imperium"], "   ")
    assert added is False
    assert out == ["Imperium"]


def test_add_coalition_item_does_not_mutate_input():
    existing = ["A"]
    out, added = add_coalition_item(existing, "B")
    assert added is True
    assert existing == ["A"]
    assert out == ["A", "B"]


# ── remove_filter_item ──────────────────────────────────────────────────────

def test_remove_filter_item_removes_index():
    items = [{"id": 1}, {"id": 2}, {"id": 3}]
    out = remove_filter_item(items, 1)
    assert out == [{"id": 1}, {"id": 3}]


def test_remove_filter_item_works_on_coalition_name_strings():
    out = remove_filter_item(["Imperium", "Winter Coalition"], 0)
    assert out == ["Winter Coalition"]


def test_remove_filter_item_out_of_range_is_noop():
    items = [{"id": 1}]
    assert remove_filter_item(items, 5) == [{"id": 1}]
    assert remove_filter_item(items, -1) == [{"id": 1}]


def test_remove_filter_item_does_not_mutate_input():
    items = [{"id": 1}, {"id": 2}]
    out = remove_filter_item(items, 0)
    assert items == [{"id": 1}, {"id": 2}]  # original untouched
    assert out == [{"id": 2}]


def test_remove_filter_item_handles_none():
    assert remove_filter_item(None, 0) == []


# ── rename_coalition_in_selection ───────────────────────────────────────────

def test_rename_coalition_in_selection_replaces_match():
    out = rename_coalition_in_selection(["Imperium", "PanFam"], "Imperium",
                                        "The Imperium")
    assert out == ["The Imperium", "PanFam"]


def test_rename_coalition_in_selection_preserves_order():
    out = rename_coalition_in_selection(["A", "B", "C"], "B", "Z")
    assert out == ["A", "Z", "C"]


def test_rename_coalition_in_selection_absent_name_is_noop():
    out = rename_coalition_in_selection(["A", "B"], "Missing", "Z")
    assert out == ["A", "B"]


def test_rename_coalition_in_selection_collapses_onto_existing_target():
    # Renaming "A" -> "B" when "B" already selected must not duplicate "B".
    out = rename_coalition_in_selection(["A", "B"], "A", "B")
    assert out == ["B"]


def test_rename_coalition_in_selection_is_case_sensitive():
    # Coalition keys are case-sensitive dict keys; "imperium" != "Imperium".
    out = rename_coalition_in_selection(["Imperium"], "imperium", "X")
    assert out == ["Imperium"]


def test_rename_coalition_in_selection_case_only_swap():
    # A case-only rename of the SAME coalition must update the selected
    # reference's casing while leaving other entries untouched.
    out = rename_coalition_in_selection(["Imperium", "x"], "Imperium",
                                        "imperium")
    assert out == ["imperium", "x"]


def test_rename_coalition_in_selection_does_not_mutate_input():
    src = ["A", "B"]
    out = rename_coalition_in_selection(src, "A", "Z")
    assert src == ["A", "B"]
    assert out == ["Z", "B"]


def test_rename_coalition_in_selection_handles_none():
    assert rename_coalition_in_selection(None, "A", "B") == []


# ── remove_coalition_from_selection ─────────────────────────────────────────

def test_remove_coalition_from_selection_drops_match():
    out = remove_coalition_from_selection(["Imperium", "PanFam"], "Imperium")
    assert out == ["PanFam"]


def test_remove_coalition_from_selection_absent_name_is_noop():
    out = remove_coalition_from_selection(["A", "B"], "Missing")
    assert out == ["A", "B"]


def test_remove_coalition_from_selection_removes_all_occurrences():
    out = remove_coalition_from_selection(["A", "A", "B"], "A")
    assert out == ["B"]


def test_remove_coalition_from_selection_does_not_mutate_input():
    src = ["A", "B"]
    out = remove_coalition_from_selection(src, "A")
    assert src == ["A", "B"]
    assert out == ["B"]


def test_remove_coalition_from_selection_handles_none():
    assert remove_coalition_from_selection(None, "A") == []
