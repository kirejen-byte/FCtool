"""Unit tests for the Tk-free intel-channel helpers used by the GUI settings.

These exercise normalize_tracked_channels() and filter_suggestion_channels()
in isolation — no Tk, no network — so the tracked-list normalization and the
picker's suggestion noise-filter are verifiable on their own. They mirror the
style of test_range_staging.py.
"""

import importlib.util

# fc_gui imports cleanly at module scope (Tk is only touched inside the class),
# so a normal import works once the repo root is on sys.path (conftest.py).
spec = importlib.util.find_spec("fc_gui")
assert spec is not None, "fc_gui must be importable (check tests/conftest.py)"

from fc_gui import (  # noqa: E402
    normalize_tracked_channels,
    filter_suggestion_channels,
)


# ── normalize_tracked_channels ────────────────────────────────────────────

def test_normalize_passthrough_order_preserved():
    names = ["I. Ftn Intel", "I. Aridia Intel", "Bean-Intel"]
    assert normalize_tracked_channels(names) == names


def test_normalize_strips_whitespace():
    assert normalize_tracked_channels(["  I. OR Intel  "]) == ["I. OR Intel"]


def test_normalize_drops_blanks():
    assert normalize_tracked_channels(["A", "", "   ", "B"]) == ["A", "B"]


def test_normalize_dedupes_case_insensitively_keeping_first():
    # First-seen casing/position wins; later case-variant duplicates drop.
    out = normalize_tracked_channels(["Delve Intel", "delve intel", "DELVE INTEL"])
    assert out == ["Delve Intel"]


def test_normalize_empty_without_seed_is_empty():
    assert normalize_tracked_channels([]) == []
    assert normalize_tracked_channels(None) == []


def test_normalize_empty_falls_back_to_seed():
    seed = ["I. Ftn Intel", "I. OR Intel"]
    assert normalize_tracked_channels([], seed=seed) == seed
    assert normalize_tracked_channels(None, seed=seed) == seed
    # Whitespace-only input is "empty" after cleaning -> seed.
    assert normalize_tracked_channels(["  ", ""], seed=seed) == seed


def test_normalize_seed_itself_is_normalized():
    # A non-empty result never uses the seed; but when seed is used it is
    # cleaned/de-duped the same way.
    seed = ["  Dupe ", "dupe", "Real"]
    assert normalize_tracked_channels([], seed=seed) == ["Dupe", "Real"]


def test_normalize_does_not_mutate_input():
    src = ["A", "a", "B"]
    normalize_tracked_channels(src)
    assert src == ["A", "a", "B"]


# ── filter_suggestion_channels ────────────────────────────────────────────

def test_filter_hides_exact_system_channels_case_insensitive():
    src = ["Local", "corp", "ALLIANCE", "Fleet", "rookie help", "Delve Intel"]
    assert filter_suggestion_channels(src) == ["Delve Intel"]


def test_filter_hides_private_chat_prefix():
    src = ["Private Chat (Bob)", "private chat foo", "I. Ftn Intel"]
    assert filter_suggestion_channels(src) == ["I. Ftn Intel"]


def test_filter_keeps_real_intel_channels():
    src = ["I. Ftn Intel", "Bean-Intel", "Querious Intel"]
    assert filter_suggestion_channels(src) == src


def test_filter_dedupes_case_insensitively_and_preserves_order():
    src = ["Bean-Intel", "bean-intel", "Aridia Intel"]
    assert filter_suggestion_channels(src) == ["Bean-Intel", "Aridia Intel"]


def test_filter_strips_and_drops_blanks():
    src = ["  Delve Intel  ", "", "   "]
    assert filter_suggestion_channels(src) == ["Delve Intel"]


def test_filter_substring_of_system_name_is_not_hidden():
    # Only EXACT matches (and the Private Chat prefix) are hidden; a channel
    # that merely contains "Local" is a legitimate suggestion.
    src = ["Local Intel", "Corp Defense", "Fleetcomms"]
    assert filter_suggestion_channels(src) == src


def test_filter_empty_input():
    assert filter_suggestion_channels([]) == []
    assert filter_suggestion_channels(None) == []
