from datetime import datetime

import pytest

from chat_monitor import ChatMessage
from intel_monitor import (
    BARE_COUNT_PATTERN,
    CAMP_PATTERN,
    CLEAR_PATTERN,
    COUNT_PATTERN,
    EXPLICIT_PLUS_COUNT_PATTERN,
    HOSTILE_CONTEXT_PATTERN,
    SPIKE_PATTERN,
    _extract_system_name,
    parse_intel_message,
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
