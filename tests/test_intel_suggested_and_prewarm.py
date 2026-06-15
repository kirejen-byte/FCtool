"""Unit tests for the Tk-free helpers behind the Suggested intel-channels
panel and the staging-only prewarm.

These exercise:
  * compute_intel_channel_suggestions() — the intel/intelligence predicate
    minus the tracked set (Suggested panel source);
  * extract_staging_system_names() — the prewarm system extractor (configured
    staging system + friendly/hostile staging lists, name-or-{id,name} forms);
  * the _add_intel_channel_by_name() 1-click add path, driven through a tiny
    Tk-free fake that binds the REAL method but stubs the widget refreshes —
    proving a 1-click add moves a channel into tracked (persisted) and out of
    the recomputed suggestions.

No Tk, no network, no display required (mirrors test_intel_channels_config.py).
"""

import types

from fc_gui import (  # noqa: E402
    compute_intel_channel_suggestions,
    extract_staging_system_names,
    normalize_tracked_channels,
    FCToolGUI,
)


# ── compute_intel_channel_suggestions (Suggested-panel source) ─────────────

def test_suggestions_keep_only_intel_named_minus_tracked():
    discovered = ["Delve Intel", "Local", "Querious Intelligence",
                  "Corp", "I. Aridia Intel", "Fleet"]
    tracked = ["I. Aridia Intel"]
    out = compute_intel_channel_suggestions(discovered, tracked)
    # "intel"/"intelligence" names survive; the already-tracked one drops out.
    assert out == ["Delve Intel", "Querious Intelligence"]


def test_suggestions_tracked_match_is_case_insensitive():
    discovered = ["DELVE INTEL", "Other Intel"]
    tracked = ["delve intel"]
    assert compute_intel_channel_suggestions(discovered, tracked) == ["Other Intel"]


def test_suggestions_dedupe_case_insensitively_first_wins():
    discovered = ["Delve Intel", "delve intel", "DELVE INTEL"]
    assert compute_intel_channel_suggestions(discovered, []) == ["Delve Intel"]


def test_suggestions_skip_blanks_and_non_intel():
    discovered = ["", "   ", "Jita", "Home Intel"]
    assert compute_intel_channel_suggestions(discovered, []) == ["Home Intel"]


def test_suggestions_empty_inputs():
    assert compute_intel_channel_suggestions([], []) == []
    assert compute_intel_channel_suggestions(None, None) == []


# ── extract_staging_system_names (prewarm system pool) ─────────────────────

def test_prewarm_uses_staging_and_staging_lists_not_hardcoded():
    staging = "1DQ1-A"
    friendly = ["T5ZI-S", {"id": 30000142, "name": "Jita"}]
    hostile = ["319-3D"]
    out = extract_staging_system_names(staging, friendly, hostile)
    assert out == ["1DQ1-A", "T5ZI-S", "Jita", "319-3D"]
    # None of the old hard-coded Querious/Delve seeds leak in.
    for old in ("C-N4OD", "6RCQ-V", "F7C-H0", "CL6-ZG", "HPS5-C",
                "Korasen", "Y-2ANO", "NOL-M9"):
        assert old not in out


def test_prewarm_dedupes_and_skips_blanks_across_sources():
    staging = "1DQ1-A"
    friendly = ["1dq1-a", "  ", {"name": ""}, {"name": "T5ZI-S"}]
    hostile = ["T5ZI-S", "", None]
    out = extract_staging_system_names(staging, friendly, hostile)
    # Case-insensitive dedupe, blanks dropped, first-seen casing kept.
    assert out == ["1DQ1-A", "T5ZI-S"]


def test_prewarm_empty_when_nothing_configured():
    assert extract_staging_system_names("", [], []) == []
    assert extract_staging_system_names(None) == []


# ── 1-click add path (_add_intel_channel_by_name) ──────────────────────────

def _make_fake_gui(tracked):
    """A Tk-free stand-in that binds the REAL add method but stubs the widget
    refreshes, so we can assert the data-model side-effects of a 1-click add."""
    fake = types.SimpleNamespace()
    fake._tracked_intel_channels = list(tracked)
    fake._intel_suggested_channels = []
    fake.saved = []          # records each persist call
    fake.statuses = []       # records each status (text, fg)
    fake.rebuilt = 0
    fake.listbox_refreshed = 0

    fake._save_tracked_intel_channels = lambda: fake.saved.append(
        list(fake._tracked_intel_channels))
    fake._refresh_intel_channels_listbox = lambda: setattr(
        fake, "listbox_refreshed", fake.listbox_refreshed + 1)
    fake._rebuild_intel_channel_checkboxes = lambda: setattr(
        fake, "rebuilt", fake.rebuilt + 1)

    class _Status:
        def config(self, text="", fg=None):
            fake.statuses.append((text, fg))
    fake._intel_channels_status = _Status()

    # Recompute suggestions from a fixed discovered pool, minus tracked —
    # mirrors _refresh_intel_channel_suggestions' data side (no widgets).
    def _refresh_suggestions(discovered=None):
        pool = list(discovered) if discovered is not None else list(
            fake._intel_suggested_channels)
        fake._intel_suggested_channels = compute_intel_channel_suggestions(
            pool, fake._tracked_intel_channels)
    fake._refresh_intel_channel_suggestions = _refresh_suggestions

    # Bind the REAL methods under test.
    fake._add_intel_channel_by_name = types.MethodType(
        FCToolGUI._add_intel_channel_by_name, fake)
    return fake


def test_one_click_add_moves_channel_into_tracked_and_persists():
    fake = _make_fake_gui(tracked=["I. Aridia Intel"])
    # Seed the suggestion pool from a discovered set.
    fake._refresh_intel_channel_suggestions(
        discovered=["Delve Intel", "I. Aridia Intel"])
    assert fake._intel_suggested_channels == ["Delve Intel"]

    fake._add_intel_channel_by_name("Delve Intel")

    # Tracked now contains the added channel (de-duped, persisted).
    assert "Delve Intel" in fake._tracked_intel_channels
    assert fake.saved and fake.saved[-1] == fake._tracked_intel_channels
    assert fake.rebuilt == 1
    assert fake.listbox_refreshed == 1
    # ...and it has dropped OUT of the recomputed suggestions.
    assert "Delve Intel" not in fake._intel_suggested_channels
    assert fake._intel_suggested_channels == []
    # Status reflects the successful add.
    assert fake.statuses[-1][0] == "Added Delve Intel"


def test_one_click_add_existing_is_noop_with_notice():
    fake = _make_fake_gui(tracked=["Delve Intel"])
    fake._add_intel_channel_by_name("delve intel")  # case-variant duplicate
    # No persist / rebuild happened; tracked unchanged (one entry).
    assert fake.saved == []
    assert fake.rebuilt == 0
    assert normalize_tracked_channels(fake._tracked_intel_channels) == [
        "Delve Intel"]
    assert fake.statuses[-1][0] == "delve intel already tracked"


def test_one_click_add_blank_is_silent_noop():
    fake = _make_fake_gui(tracked=["Delve Intel"])
    fake._add_intel_channel_by_name("   ")
    assert fake.saved == []
    assert fake.rebuilt == 0
    assert fake.statuses == []  # blank returns before any status set
