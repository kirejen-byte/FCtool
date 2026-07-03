"""Tests for gamelog_monitor: pure parse_damage_line + UTF-8 tailing smoke."""
import os

import pytest

import gamelog_monitor as gm

# --- verbatim sample lines (English client, PELD-verified shapes) ---
INCOMING = ("[ 2026.07.03 18:22:11 ] (combat) <color=0xffcc0000><b>547</b>"
            "<color=0xffffffff><font size=10> from</font> "
            "<b>Bad Guy[HOST](Muninn)</b> - Scourge Fury Heavy Missile - Hits")
INCOMING_SMARTBOMB = ("[ 2026.07.03 18:22:12 ] (combat) <color=0xffcc0000><b>88</b>"
                      "<color=0xffffffff><font size=10> from</font> "
                      "<b>Camper[HOST](Bhaalgorn)</b> - Large EMP Smartbomb - Smashes")
OUTGOING = ("[ 2026.07.03 18:22:13 ] (combat) <color=0xff00ff00><b>612</b>"
            "<color=0xffffffff><font size=10> to</font> "
            "<b>Victim[RED](Drake)</b> - Occult L - Penetrates")
MINING = ("[ 2026.07.03 18:22:14 ] (mining) <color=0xffffffff>"
          "<font size=12>You mined some Veldspar</font>")
NOTIFY = ("[ 2026.07.03 18:22:15 ] (notify) <color=0xffaa00aa>"
          "<font size=12>You are now cloaked</font>")
MALFORMED = "[ garbage ] (combat) <b>not a number</b> from <b>x</b>"


def test_parse_incoming_damage_extracts_amount_and_attacker():
    got = gm.parse_damage_line(INCOMING)
    assert got == (547, "Bad Guy[HOST](Muninn)")


def test_parse_incoming_smartbomb_variant():
    assert gm.parse_damage_line(INCOMING_SMARTBOMB) == (88, "Camper[HOST](Bhaalgorn)")


def test_outgoing_damage_is_not_matched():
    assert gm.parse_damage_line(OUTGOING) is None


def test_non_combat_lines_are_not_matched():
    assert gm.parse_damage_line(MINING) is None
    assert gm.parse_damage_line(NOTIFY) is None


def test_malformed_line_is_safe():
    assert gm.parse_damage_line(MALFORMED) is None
    assert gm.parse_damage_line("") is None


def test_tail_reads_utf8_appends_and_emits_events(tmp_path):
    events = []
    path = tmp_path / "20260703_182200_91000001.txt"
    header = ("------------------------------------------------------------\n"
              "  Gamelog\n"
              "  Listener: Kirejen\n"
              "  Session Started: 2026.07.03 18:22:00\n"
              "------------------------------------------------------------\n")
    path.write_text(header, encoding="utf-8")
    mon = gm.GamelogMonitor(on_event=events.append,
                            state_path=str(tmp_path / "gamelog_monitor_state.json"))
    mon.seed_file(str(path))                    # seed to EOF: existing lines not replayed
    with open(path, "a", encoding="utf-8") as f:
        f.write(INCOMING + "\n")
        f.write(OUTGOING + "\n")                # ignored (outgoing)
    mon.poll_file(str(path))                    # one tailing pass
    assert len(events) == 1
    ev = events[0]
    assert ev.character_name == "Kirejen"
    assert ev.amount == 547 and ev.attacker == "Bad Guy[HOST](Muninn)"
    assert ev.timestamp == "2026.07.03 18:22:11"


def test_utf8_multibyte_does_not_break_position_math(tmp_path):
    # A non-ASCII attacker name (é = 2 UTF-8 bytes) must not corrupt the byte
    # position (no *2 / &~1 alignment in the UTF-8 path).
    path = tmp_path / "20260703_190000_91000002.txt"
    path.write_text("  Gamelog\n  Listener: Kirejen\n", encoding="utf-8")
    mon = gm.GamelogMonitor(on_event=lambda e: None,
                            state_path=str(tmp_path / "s.json"))
    mon.seed_file(str(path))
    line = INCOMING.replace("Bad Guy", "Björn")
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    seen = []
    mon._on_event = seen.append
    mon.poll_file(str(path))
    assert seen and seen[0].attacker.startswith("Björn")
