import glob
import os
from datetime import datetime

import pytest

from chat_monitor import ChatMessage
import intel_monitor
from intel_monitor import (
    BARE_COUNT_PATTERN,
    CAMP_PATTERN,
    CHAT_LOG_SUFFIX_PATTERN,
    CLEAR_PATTERN,
    COUNT_PATTERN,
    EXPLICIT_PLUS_COUNT_PATTERN,
    HOSTILE_CONTEXT_PATTERN,
    INTEL_CHANNELS,
    SPIKE_PATTERN,
    _extract_system_name,
    discover_channels,
    parse_intel_message,
    scan_available_channels,
)


def _make_msg(text, sender="Scout Pilot", channel="I. Ftn Intel"):
    return ChatMessage(
        timestamp=datetime(2026, 4, 21, 12, 0, 0),
        sender=sender,
        message=text,
        channel=channel,
        raw_line=f"[ 2026.04.21 12:00:00 ] {sender} > {text}",
    )


# ── BARE_COUNT_PATTERN: current behavior ────────────────────────────────────

def test_bare_count_matches_plus_prefix():
    m = BARE_COUNT_PATTERN.search("+5")
    assert m is not None
    assert m.group(1) == "5"


def test_bare_count_matches_plus_suffix():
    m = BARE_COUNT_PATTERN.search("5+")
    assert m is not None
    assert m.group(1) == "5"


def test_bare_count_matches_lone_number_before_word():
    m = BARE_COUNT_PATTERN.search(" 5 hostiles")
    assert m is not None
    assert m.group(1) == "5"


def test_bare_count_does_not_match_clock_time():
    """Phase 2(b): clock-time digits must no longer leak through as pilot counts."""
    m = BARE_COUNT_PATTERN.search("13:45")
    assert m is None


def test_bare_count_does_not_match_ticket_number():
    """Phase 2(b): ticket-number digits must no longer leak through as pilot counts."""
    m = BARE_COUNT_PATTERN.search("ticket #1234")
    assert m is None


def test_bare_count_does_not_match_number_glued_to_letter():
    m = BARE_COUNT_PATTERN.search("500m isk")
    assert m is None


# ── EXPLICIT_PLUS_COUNT_PATTERN: Tier-1 always-accept forms ─────────────────

def test_explicit_plus_matches_plus_prefix():
    m = EXPLICIT_PLUS_COUNT_PATTERN.search("+5")
    assert m is not None
    assert (m.group(1) or m.group(2)) == "5"


def test_explicit_plus_matches_plus_suffix():
    m = EXPLICIT_PLUS_COUNT_PATTERN.search("5+")
    assert m is not None
    assert (m.group(1) or m.group(2)) == "5"


def test_explicit_plus_does_not_match_bare_number():
    assert EXPLICIT_PLUS_COUNT_PATTERN.search("13:45") is None
    assert EXPLICIT_PLUS_COUNT_PATTERN.search("ticket #1234") is None
    assert EXPLICIT_PLUS_COUNT_PATTERN.search("500m isk") is None


def test_explicit_plus_does_not_bleed_into_system_names():
    # "1DQ1-A" and similar shouldn't be treated as plus-count matches
    assert EXPLICIT_PLUS_COUNT_PATTERN.search("1DQ1-A") is None


# ── HOSTILE_CONTEXT_PATTERN: Tier-2 gating keyword set ─────────────────────

def test_hostile_context_pattern_matches_all_keywords():
    for kw in (
        "hostile", "hostiles", "red", "reds", "neut", "neuts",
        "enemy", "enemies", "clr", "pilot", "pilots",
        "dude", "dudes", "guy", "guys", "gang",
    ):
        assert HOSTILE_CONTEXT_PATTERN.search(kw) is not None, kw
    # case-insensitive
    assert HOSTILE_CONTEXT_PATTERN.search("HOSTILES") is not None
    assert HOSTILE_CONTEXT_PATTERN.search("Reds") is not None


def test_hostile_context_pattern_rejects_unrelated_words():
    assert HOSTILE_CONTEXT_PATTERN.search("status update") is None
    assert HOSTILE_CONTEXT_PATTERN.search("quiet tonight") is None
    assert HOSTILE_CONTEXT_PATTERN.search("13:45 timestamp") is None


# ── Other intel pattern sanity checks ───────────────────────────────────────

def test_count_pattern_matches_with_keyword():
    assert COUNT_PATTERN.search("5+ reds").group(1) == "5"
    assert COUNT_PATTERN.search("10 hostiles").group(1) == "10"
    assert COUNT_PATTERN.search("3 neuts").group(1) == "3"


def test_clear_pattern():
    assert CLEAR_PATTERN.search("clr") is not None
    assert CLEAR_PATTERN.search("nv") is not None
    assert CLEAR_PATTERN.search("clear") is not None
    assert CLEAR_PATTERN.search("unrelated") is None


def test_camp_pattern():
    assert CAMP_PATTERN.search("gate camp") is not None
    assert CAMP_PATTERN.search("bubbled") is not None
    assert CAMP_PATTERN.search("peaceful") is None


def test_spike_pattern():
    assert SPIKE_PATTERN.search("local spike") is not None
    assert SPIKE_PATTERN.search("clear and quiet") is None


# ── System name extraction ─────────────────────────────────────────────────

def test_extract_system_name_good_case():
    def resolver(name):
        if name == "Jita":
            return 30000142
        return None

    sysname, remaining = _extract_system_name("Jita 5 hostiles", resolver)
    assert sysname == "Jita"
    assert remaining == "5 hostiles"


def test_extract_system_name_no_match():
    def resolver(name):
        return None

    sysname, remaining = _extract_system_name("gibberish tokens here", resolver)
    assert sysname == ""
    assert remaining == "gibberish tokens here"


def test_extract_system_name_multiword():
    def resolver(name):
        if name == "Old Man Star":
            return 30002053
        return None

    sysname, remaining = _extract_system_name("Old Man Star clr", resolver)
    assert sysname == "Old Man Star"
    assert remaining == "clr"


def test_extract_system_name_strips_punctuation():
    def resolver(name):
        if name == "Jita":
            return 30000142
        return None

    sysname, _ = _extract_system_name("Jita!! hostiles", resolver)
    assert sysname == "Jita"


# ── Full parse_intel_message pipeline ──────────────────────────────────────

def test_parse_intel_message_hostile_report(mocker):
    def resolver(name):
        return 30000142 if name == "Jita" else None

    msg = _make_msg("Jita 5 hostiles")
    report = parse_intel_message(msg, resolver)

    assert report is not None
    assert report.system_name == "Jita"
    assert report.system_id == 30000142
    assert report.report_type == "hostile"
    assert report.pilot_count == 5
    assert report.reporter == "Scout Pilot"
    assert report.channel == "I. Ftn Intel"


def test_parse_intel_message_clear_report():
    def resolver(name):
        return 30000142 if name == "Jita" else None

    msg = _make_msg("Jita clr")
    report = parse_intel_message(msg, resolver)

    assert report is not None
    assert report.system_name == "Jita"
    assert report.report_type == "clear"


def test_parse_intel_message_with_dscan_url():
    def resolver(name):
        return 30000142 if name == "Jita" else None

    msg = _make_msg("Jita https://dscan.info/v/abcd1234")
    report = parse_intel_message(msg, resolver)

    assert report is not None
    assert report.system_name == "Jita"
    assert report.report_type == "dscan"
    assert "dscan.info" in report.dscan_url


def test_parse_intel_message_detects_camp():
    def resolver(name):
        return 30000142 if name == "Jita" else None

    msg = _make_msg("Jita gate camp")
    report = parse_intel_message(msg, resolver)

    assert report is not None
    assert report.has_camp is True


def test_parse_intel_message_info_when_no_system():
    def resolver(name):
        return None

    msg = _make_msg("some random chatter without any known system")
    report = parse_intel_message(msg, resolver)

    assert report is not None
    assert report.system_name == ""
    assert report.report_type == "info"


def test_parse_intel_message_empty_returns_none():
    def resolver(name):
        return None

    msg = _make_msg("")
    report = parse_intel_message(msg, resolver)
    assert report is None


def test_parse_intel_message_single_char_returns_none():
    def resolver(name):
        return None

    msg = _make_msg("o")
    report = parse_intel_message(msg, resolver)
    assert report is None


def test_parse_intel_message_does_not_call_network(mocker):
    def resolver(name):
        return 30000142 if name == "Jita" else None

    req_post_patch = mocker.patch("requests.post")
    req_get_patch = mocker.patch("requests.get")

    msg = _make_msg("Jita 3 hostiles")
    report = parse_intel_message(msg, resolver)

    assert report is not None
    req_post_patch.assert_not_called()
    req_get_patch.assert_not_called()


# ── Phase 2(b): pilot-count extraction — matches (hostile context present) ──

def _jita_resolver(name):
    return 30000142 if name == "Jita" else None


def test_pilot_count_explicit_plus_prefix():
    msg = _make_msg("Jita +5")
    report = parse_intel_message(msg, _jita_resolver)
    assert report is not None
    assert report.pilot_count == 5


def test_pilot_count_explicit_plus_suffix():
    msg = _make_msg("Jita 5+")
    report = parse_intel_message(msg, _jita_resolver)
    assert report is not None
    assert report.pilot_count == 5


def test_pilot_count_keyword_phrase_hostiles():
    msg = _make_msg("Jita 5 hostiles")
    report = parse_intel_message(msg, _jita_resolver)
    assert report is not None
    assert report.pilot_count == 5


def test_pilot_count_keyword_phrase_reds():
    msg = _make_msg("Jita 10 reds")
    report = parse_intel_message(msg, _jita_resolver)
    assert report is not None
    assert report.pilot_count == 10


def test_pilot_count_bare_digit_with_gang_context():
    """'gang of 8' — no count-keyword adjacency, but 'gang' licenses the bare digit."""
    msg = _make_msg("Jita gang of 8")
    report = parse_intel_message(msg, _jita_resolver)
    assert report is not None
    assert report.pilot_count == 8


def test_pilot_count_plus5s_suffix_acceptable():
    """'+5s jita' — ambiguous, but '+5' is a Tier-1 plus-form match. Acceptable."""
    msg = _make_msg("Jita +5s incoming")
    report = parse_intel_message(msg, _jita_resolver)
    assert report is not None
    assert report.pilot_count == 5


# ── Phase 2(b): pilot-count extraction — non-matches (no hostile context) ───

def test_pilot_count_clock_time_rejected():
    """'13:45 status X' used to produce pilot_count=13. Now must be None."""
    msg = _make_msg("Jita 13:45 status")
    report = parse_intel_message(msg, _jita_resolver)
    assert report is not None
    assert report.pilot_count is None


def test_pilot_count_ticket_number_rejected():
    msg = _make_msg("Jita ticket #1234")
    report = parse_intel_message(msg, _jita_resolver)
    assert report is not None
    assert report.pilot_count is None


def test_pilot_count_isk_amount_rejected():
    msg = _make_msg("Jita 500m isk bounty")
    report = parse_intel_message(msg, _jita_resolver)
    assert report is not None
    assert report.pilot_count is None


def test_pilot_count_bare_digit_without_hostile_context_rejected():
    """Bare '7' with no hostile-context keyword must stay None (no hallucination)."""
    msg = _make_msg("Jita check 7")
    report = parse_intel_message(msg, _jita_resolver)
    assert report is not None
    assert report.pilot_count is None


# ── Phase 2(b) proximity gate: keyword must be NEAR the digit ───────────────
# Earlier phase used a whole-message gate: any hostile keyword anywhere in the
# cleaned text licensed any bare digit anywhere else. Too loose. These tests
# lock in the tightened proximity-gated behavior.

def test_pilot_count_rejects_keyword_too_far_from_digit_reds_left():
    """'the reds left, 5 jumps to go' — `reds` and `5` are in different clauses.
    Previously produced pilot_count=5; with the proximity gate it must be None."""
    msg = _make_msg("Jita, the reds left, 5 jumps to go")
    report = parse_intel_message(msg, _jita_resolver)
    assert report is not None
    assert report.system_name == "Jita"
    assert report.pilot_count is None


def test_pilot_count_rejects_keyword_too_far_from_digit_hostiles_isk():
    """'hostiles in system, 500 isk bounty payout' — `hostiles` is separated from
    `500` by an unrelated clause. Must be None under the proximity gate."""
    msg = _make_msg("Jita hostiles in system, 500 isk bounty payout")
    report = parse_intel_message(msg, _jita_resolver)
    assert report is not None
    assert report.system_name == "Jita"
    assert report.pilot_count is None


def test_pilot_count_accepts_keyword_adjacent_to_digit():
    """Keyword directly adjacent — "clr 5" — still passes the proximity gate."""
    # COUNT_PATTERN doesn't match "clr 5" (clr isn't in its keyword set),
    # and EXPLICIT_PLUS doesn't match either — this exercises the bare-digit tier.
    msg = _make_msg("Jita clr 5")
    report = parse_intel_message(msg, _jita_resolver)
    assert report is not None
    assert report.pilot_count == 5


def test_pilot_count_accepts_gang_of_n():
    """'gang of 8' — small word gap; proximity gate still accepts."""
    msg = _make_msg("Jita gang of 8")
    report = parse_intel_message(msg, _jita_resolver)
    assert report is not None
    assert report.pilot_count == 8


def test_pilot_count_picks_first_valid_candidate_not_misleading_earlier_digit():
    """If an earlier bare digit has no nearby keyword but a later one does,
    only the later (licensed) one should be taken."""
    # "47 afk, 3 reds here" — "47" is far from any keyword; "3" is adjacent to "reds".
    # finditer walks left-to-right, so the loop should skip 47 and take 3.
    msg = _make_msg("Jita 47 afk some idle chatter filler words, 3 reds here")
    report = parse_intel_message(msg, _jita_resolver)
    assert report is not None
    # COUNT_PATTERN would match "3 reds" directly and return 3; either way,
    # the proximity gate must not return 47.
    assert report.pilot_count == 3


# ── CHAT_LOG_SUFFIX_PATTERN: filename suffix recognition ────────────────────

def test_suffix_pattern_matches_with_charid():
    m = CHAT_LOG_SUFFIX_PATTERN.search("Bean-Intel_20211115_043458_1694010657.txt")
    assert m is not None
    # Channel name is everything before the match.
    assert "Bean-Intel_20211115_043458_1694010657.txt"[: m.start()] == "Bean-Intel"


def test_suffix_pattern_matches_without_charid():
    fn = "super serious channel_20131011_002149.txt"
    m = CHAT_LOG_SUFFIX_PATTERN.search(fn)
    assert m is not None
    assert fn[: m.start()] == "super serious channel"


def test_suffix_pattern_special_chars_in_name():
    fn = "I. Delve & Q Intel_20231215_024930_90143494.txt"
    m = CHAT_LOG_SUFFIX_PATTERN.search(fn)
    assert m is not None
    assert fn[: m.start()] == "I. Delve & Q Intel"


def test_suffix_pattern_rejects_non_log_names():
    assert CHAT_LOG_SUFFIX_PATTERN.search("notes.txt") is None
    assert CHAT_LOG_SUFFIX_PATTERN.search("README.md") is None
    # Wrong digit widths (date must be 8, time must be 6).
    assert CHAT_LOG_SUFFIX_PATTERN.search("Channel_2023121_024930.txt") is None
    assert CHAT_LOG_SUFFIX_PATTERN.search("Channel_20231215_02493.txt") is None


# ── discover_channels: helpers ──────────────────────────────────────────────

# Realistic EVE chat-log header (UTF-16LE on disk). Only "Listener:" matters
# for the character filter; the rest mirrors what EVE writes.
def _eve_log_text(channel_name, listener):
    return (
        "﻿"  # BOM, as EVE writes
        "---------------------------------------------------------------\n"
        f"  Channel ID:      intel\n"
        f"  Channel Name:    {channel_name}\n"
        f"  Listener:        {listener}\n"
        "  Session started: 2026.06.14 12:00:00\n"
        "---------------------------------------------------------------\n"
        "[ 2026.06.14 12:00:05 ] Some Pilot > Jita clr\n"
    )


def _make_log(dir_path, filename, *, listener="Scout Pilot", channel="Chan",
              mtime=None):
    """Create a UTF-16LE EVE-style log file; optionally backdate its mtime."""
    path = dir_path / filename
    path.write_text(_eve_log_text(channel, listener), encoding="utf-16-le")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _now():
    return datetime.now().timestamp()


def _days_ago(days):
    return _now() - days * 86400


# ── discover_channels: core behavior ────────────────────────────────────────

def test_discover_channels_empty_or_missing_path(tmp_path):
    # Non-existent directory.
    assert discover_channels(str(tmp_path / "nope")) == []
    # Empty string.
    assert discover_channels("") == []
    # None.
    assert discover_channels(None) == []
    # Existing but empty directory.
    assert discover_channels(str(tmp_path)) == []


def test_discover_channels_extracts_name_with_charid(tmp_path):
    _make_log(tmp_path, "Bean-Intel_20211115_043458_1694010657.txt",
              channel="Bean-Intel", mtime=_now())
    result = discover_channels(str(tmp_path), max_age_days=None)
    assert [c["name"] for c in result] == ["Bean-Intel"]


def test_discover_channels_extracts_name_without_charid(tmp_path):
    _make_log(tmp_path, "super serious channel_20131011_002149.txt",
              channel="super serious channel", mtime=_now())
    result = discover_channels(str(tmp_path), max_age_days=None)
    assert [c["name"] for c in result] == ["super serious channel"]


def test_discover_channels_handles_special_character_names(tmp_path):
    # Spaces, '.', '&', brackets, '+'.
    _make_log(tmp_path, "I. Delve & Q Intel_20231215_024930_90143494.txt",
              channel="I. Delve & Q Intel", mtime=_now())
    _make_log(tmp_path, "[VG] Region Bookmarks Part 1_20231215_024930_90143494.txt",
              channel="[VG] Region Bookmarks Part 1", mtime=_now())
    _make_log(tmp_path, "wc.Vale+Tribute_20231215_024930.txt",
              channel="wc.Vale+Tribute", mtime=_now())
    names = [c["name"] for c in discover_channels(str(tmp_path), max_age_days=None)]
    assert names == [
        "[VG] Region Bookmarks Part 1",
        "I. Delve & Q Intel",
        "wc.Vale+Tribute",
    ]


def test_discover_channels_dedupes_across_sessions_and_charids(tmp_path):
    """Multiple sessions / different charids of the SAME channel collapse to one."""
    older = _days_ago(2)
    newest = _now()
    # Same channel name, three different session files (two charids + one old client).
    _make_log(tmp_path, "I. Delve & Q Intel_20231215_010000_90143494.txt",
              channel="I. Delve & Q Intel", mtime=older)
    _make_log(tmp_path, "I. Delve & Q Intel_20231216_020000_11112222.txt",
              channel="I. Delve & Q Intel", mtime=newest)
    _make_log(tmp_path, "I. Delve & Q Intel_20231214_030000.txt",
              channel="I. Delve & Q Intel", mtime=_days_ago(3))

    result = discover_channels(str(tmp_path), max_age_days=None)
    assert len(result) == 1
    entry = result[0]
    assert entry["name"] == "I. Delve & Q Intel"
    # Newest file wins for file_path / last_modified.
    assert entry["file_path"].endswith("I. Delve & Q Intel_20231216_020000_11112222.txt")
    assert entry["last_modified"] == pytest.approx(newest, abs=1)


def test_discover_channels_active_flag(tmp_path):
    _make_log(tmp_path, "Active Chan_20231215_010000_1.txt",
              channel="Active Chan", mtime=_now())
    _make_log(tmp_path, "Stale Chan_20231215_010000_2.txt",
              channel="Stale Chan", mtime=_days_ago(3))
    result = {c["name"]: c for c in discover_channels(str(tmp_path), max_age_days=None)}
    assert result["Active Chan"]["active"] is True
    assert result["Stale Chan"]["active"] is False


def test_discover_channels_max_age_filtering(tmp_path):
    # Recent channel — kept; very old channel — dropped at default 30 days.
    _make_log(tmp_path, "Recent_20231215_010000_1.txt",
              channel="Recent", mtime=_days_ago(5))
    _make_log(tmp_path, "Ancient_20131011_002149.txt",
              channel="Ancient", mtime=_days_ago(400))

    default_names = [c["name"] for c in discover_channels(str(tmp_path))]
    assert default_names == ["Recent"]  # Ancient excluded by default max_age_days=30

    # With max_age_days=None, both are returned.
    all_names = [c["name"] for c in discover_channels(str(tmp_path), max_age_days=None)]
    assert all_names == ["Ancient", "Recent"]

    # Tight bound excludes the 5-day-old one too.
    tight = [c["name"] for c in discover_channels(str(tmp_path), max_age_days=3)]
    assert tight == []


def test_discover_channels_skips_malformed_filenames(tmp_path):
    # Valid log.
    _make_log(tmp_path, "Good Chan_20231215_010000_1.txt",
              channel="Good Chan", mtime=_now())
    # Assorted junk that must be ignored.
    (tmp_path / "notes.txt").write_text("just notes", encoding="utf-8")
    (tmp_path / "README.md").write_text("readme", encoding="utf-8")
    (tmp_path / "Channel_2023121_024930.txt").write_text("bad date", encoding="utf-8")
    (tmp_path / "Channel_20231215_02493.txt").write_text("bad time", encoding="utf-8")
    (tmp_path / "_20231215_010000.txt").write_text("empty name", encoding="utf-8")

    result = discover_channels(str(tmp_path), max_age_days=None)
    assert [c["name"] for c in result] == ["Good Chan"]


def test_discover_channels_sorted_case_insensitive(tmp_path):
    _make_log(tmp_path, "zeta_20231215_010000_1.txt", channel="zeta", mtime=_now())
    _make_log(tmp_path, "Alpha_20231215_010000_2.txt", channel="Alpha", mtime=_now())
    _make_log(tmp_path, "beta_20231215_010000_3.txt", channel="beta", mtime=_now())
    names = [c["name"] for c in discover_channels(str(tmp_path), max_age_days=None)]
    assert names == ["Alpha", "beta", "zeta"]


def test_discover_channels_return_shape(tmp_path):
    mtime = _now()
    _make_log(tmp_path, "Shape Chan_20231215_010000_1.txt",
              channel="Shape Chan", mtime=mtime)
    [entry] = discover_channels(str(tmp_path), max_age_days=None)
    assert set(entry.keys()) == {"name", "active", "last_modified", "file_path"}
    assert isinstance(entry["name"], str)
    assert isinstance(entry["active"], bool)
    assert isinstance(entry["last_modified"], float)
    assert isinstance(entry["file_path"], str)
    assert entry["last_modified"] == pytest.approx(mtime, abs=1)


def test_discover_channels_no_intel_name_filtering(tmp_path):
    """Helper is decision-neutral: a non-intel channel is still returned."""
    _make_log(tmp_path, "Random Corp Chat_20231215_010000_1.txt",
              channel="Random Corp Chat", mtime=_now())
    names = [c["name"] for c in discover_channels(str(tmp_path), max_age_days=None)]
    assert names == ["Random Corp Chat"]


# ── discover_channels: tracked_character (header-based) narrowing ────────────

def test_discover_channels_tracked_character_filters_by_listener(tmp_path):
    _make_log(tmp_path, "Chan A_20231215_010000_1.txt",
              channel="Chan A", listener="Scout Pilot", mtime=_now())
    _make_log(tmp_path, "Chan B_20231215_010000_2.txt",
              channel="Chan B", listener="Other Pilot", mtime=_now())

    names = [
        c["name"]
        for c in discover_channels(str(tmp_path), tracked_character="Scout Pilot",
                                   max_age_days=None)
    ]
    assert names == ["Chan A"]


def test_discover_channels_tracked_character_uses_newest_file_header(tmp_path):
    """Only the newest file per channel is consulted for the Listener match."""
    # Newest session for this channel belongs to "Scout Pilot".
    _make_log(tmp_path, "Shared_20231216_020000_1.txt",
              channel="Shared", listener="Scout Pilot", mtime=_now())
    # Older session belonged to someone else — should not affect the decision.
    _make_log(tmp_path, "Shared_20231215_010000_2.txt",
              channel="Shared", listener="Other Pilot", mtime=_days_ago(2))

    names = [
        c["name"]
        for c in discover_channels(str(tmp_path), tracked_character="Scout Pilot",
                                   max_age_days=None)
    ]
    assert names == ["Shared"]


def test_discover_channels_tracked_character_no_match(tmp_path):
    _make_log(tmp_path, "Chan A_20231215_010000_1.txt",
              channel="Chan A", listener="Scout Pilot", mtime=_now())
    result = discover_channels(str(tmp_path), tracked_character="Nobody Here",
                               max_age_days=None)
    assert result == []


# ── scan_available_channels: backward-compat + generalization ───────────────

def test_intel_channels_seed_is_empty():
    # The first-run seed is intentionally empty so a fresh install is
    # group-neutral; users add their own channels in the GUI/web layers.
    assert INTEL_CHANNELS == set()


def test_scan_available_channels_defaults_to_intel_channels(tmp_path):
    # Create a log for a channel that is NOT in the (empty) default seed.
    _make_log(tmp_path, "I. Ftn Intel_20231215_010000_1.txt",
              channel="I. Ftn Intel", mtime=_now())
    # With no channels argument, the default is the empty INTEL_CHANNELS seed,
    # so nothing is scanned regardless of which logs exist on disk.
    result = scan_available_channels(str(tmp_path))
    assert result == []
    # Each entry still has the unchanged return shape when channels are present.
    for entry in result:
        assert set(entry.keys()) == {"name", "active", "file_path"}


def test_scan_available_channels_custom_channels(tmp_path):
    _make_log(tmp_path, "My Custom Chan_20231215_010000_1.txt",
              channel="My Custom Chan", mtime=_now())
    result = scan_available_channels(str(tmp_path), channels={"My Custom Chan"})
    assert [c["name"] for c in result] == ["My Custom Chan"]
    assert result[0]["active"] is True


def test_scan_available_channels_character_filter(tmp_path):
    _make_log(tmp_path, "My Custom Chan_20231215_010000_1.txt",
              channel="My Custom Chan", listener="Scout Pilot", mtime=_now())
    # Matching listener → active.
    match = scan_available_channels(str(tmp_path), tracked_character="Scout Pilot",
                                    channels={"My Custom Chan"})
    assert match[0]["active"] is True
    assert match[0]["file_path"] is not None
    # Non-matching listener → file filtered out, channel inactive with no path.
    nomatch = scan_available_channels(str(tmp_path), tracked_character="Nobody",
                                      channels={"My Custom Chan"})
    assert nomatch[0]["active"] is False
    assert nomatch[0]["file_path"] is None


def test_scan_available_channels_case_insensitive_match(tmp_path, monkeypatch):
    """On a case-sensitive filesystem (Linux), a channel name configured in a
    different case than the on-disk filename must still match. The on-disk file
    is "Ftn Intel_..." but the configured channel is the lowercase "ftn intel"."""
    _make_log(tmp_path, "Ftn Intel_20990101_120000_123.txt",
              channel="Ftn Intel", mtime=_now())

    # Force CASE-SENSITIVE globbing to simulate Linux's filesystem, so this test
    # discriminates the fix on Windows (whose native glob is case-insensitive).
    # The old per-channel glob ("ftn intel*.txt") would not match the on-disk
    # "Ftn Intel_...txt" under fnmatchcase and the test would fail; the new code
    # globs "*.txt" once and lowercases the basename prefix, so it still matches.
    import fnmatch
    _real_glob = glob.glob
    def _cs_glob(pattern):
        d = os.path.dirname(pattern)
        pat = os.path.basename(pattern)
        return [p for p in _real_glob(os.path.join(d, "*"))
                if fnmatch.fnmatchcase(os.path.basename(p), pat)]
    monkeypatch.setattr(intel_monitor.glob, "glob", _cs_glob)

    result = scan_available_channels(str(tmp_path), channels=["ftn intel"])
    by_name = {c["name"]: c for c in result}
    assert by_name["ftn intel"]["active"] is True
    assert by_name["ftn intel"]["file_path"] is not None
