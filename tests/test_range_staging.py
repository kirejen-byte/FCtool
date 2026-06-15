"""Unit tests for the Tk-free staging-list mutator used by the Jump Range tab.

These exercise mutate_staging_lists() in isolation — no Tk, no network — so the
add/remove/move/dedupe logic that backs the friendly/hostile staging UI is
verifiable on its own.
"""

import importlib.util
import os

# Import mutate_staging_lists from fc_gui without importing Tk-dependent state.
# fc_gui imports cleanly at module scope (Tk is only touched inside the class),
# so a normal import works here once the repo root is on sys.path (conftest.py).
spec = importlib.util.find_spec("fc_gui")
assert spec is not None, "fc_gui must be importable (check tests/conftest.py)"

from fc_gui import mutate_staging_lists  # noqa: E402


def test_add_to_empty_friendly():
    friendly, hostile = mutate_staging_lists([], [], "add", "NOL-M9", "friendly")
    assert friendly == ["NOL-M9"]
    assert hostile == []


def test_add_to_hostile():
    friendly, hostile = mutate_staging_lists([], [], "add", "B-9C24", "hostile")
    assert friendly == []
    assert hostile == ["B-9C24"]


def test_add_dedupes_case_insensitively():
    friendly, hostile = mutate_staging_lists(
        ["NOL-M9"], [], "add", "nol-m9", "friendly")
    # Only one entry, and the original casing position is not duplicated.
    assert [s.lower() for s in friendly] == ["nol-m9"]
    assert len(friendly) == 1
    assert hostile == []


def test_add_strips_whitespace():
    friendly, _ = mutate_staging_lists([], [], "add", "  6RCQ-V  ", "friendly")
    assert friendly == ["6RCQ-V"]


def test_blank_name_is_noop():
    friendly, hostile = mutate_staging_lists(
        ["A"], ["B"], "add", "   ", "friendly")
    assert friendly == ["A"]
    assert hostile == ["B"]


def test_move_from_hostile_to_friendly():
    # Adding a system as friendly when it is already hostile MOVES it.
    friendly, hostile = mutate_staging_lists(
        [], ["B-9C24"], "add", "B-9C24", "friendly")
    assert friendly == ["B-9C24"]
    assert hostile == []


def test_move_from_friendly_to_hostile_case_insensitive():
    friendly, hostile = mutate_staging_lists(
        ["NOL-M9", "6RCQ-V"], [], "add", "nol-m9", "hostile")
    # NOL-M9 removed from friendly (case-insensitively), appended to hostile.
    assert friendly == ["6RCQ-V"]
    assert hostile == ["nol-m9"]


def test_remove_from_friendly():
    friendly, hostile = mutate_staging_lists(
        ["NOL-M9", "6RCQ-V"], ["B-9C24"], "remove", "NOL-M9", "friendly")
    assert friendly == ["6RCQ-V"]
    assert hostile == ["B-9C24"]


def test_remove_is_case_insensitive():
    friendly, _ = mutate_staging_lists(
        ["NOL-M9"], [], "remove", "nol-m9", "friendly")
    assert friendly == []


def test_remove_only_targets_named_list():
    # Removing from friendly must not touch a same-named entry in hostile.
    friendly, hostile = mutate_staging_lists(
        ["X"], ["X"], "remove", "X", "friendly")
    assert friendly == []
    assert hostile == ["X"]


def test_inputs_are_not_mutated_in_place():
    src_f = ["A"]
    src_h = ["B"]
    mutate_staging_lists(src_f, src_h, "add", "C", "friendly")
    # Originals untouched — the function returns fresh lists.
    assert src_f == ["A"]
    assert src_h == ["B"]


def test_unknown_action_is_noop_returning_copies():
    friendly, hostile = mutate_staging_lists(
        ["A"], ["B"], "frobnicate", "C", "friendly")
    assert friendly == ["A"]
    assert hostile == ["B"]
