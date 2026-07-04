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

# --- REAL 2026 client lines (verbatim shapes from the user's death log) ---
# The 2026 client wraps the attacker name in a nested <color=..> tag INSIDE the
# <b>…</b>, which the pre-fix DAMAGE_IN_RE attacker clause (`<b>([^<]+)</b>`)
# could not span — it matched 0% of real lines and silently suppressed the flash.
REAL_2026_NPC = ("[ 2026.07.03 18:22:11 ] (combat) <color=0xffcc0000><b>62</b> "
                 "<color=0x77ffffff><font size=10>from</font> "
                 "<b><color=0xffffffff>Corpum Dark Priest</b> - Wrecks")
REAL_2026_PLAYER = ("[ 2026.07.03 18:22:11 ] (combat) <color=0xffcc0000><b>333</b> "
                    "<color=0x77ffffff><font size=10>from</font> "
                    "<b><color=0xffffffff>Enemy Pilot[CORP](Ishtar)</b> - Warden II - Hits")
REAL_2026_OUT = ("[ 2026.07.03 18:22:13 ] (combat) <color=0xff00ff00><b>612</b> "
                 "<color=0x77ffffff><font size=10>to</font> "
                 "<b><color=0xffffffff>Victim[RED](Drake)</b> - Occult L - Penetrates")
REAL_2026_MISS = ("[ 2026.07.03 18:22:20 ] (combat) <color=0xffcc0000>"
                  "Corpum Dark Priest misses you completely")


def test_real_2026_npc_line_extracts_amount_and_nested_attacker():
    # dmg from the leading <b>N</b>; attacker spans the nested <color=..> tag.
    assert gm.parse_damage_line(REAL_2026_NPC) == (62, "Corpum Dark Priest")


def test_real_2026_player_line_extracts_nested_attacker():
    assert gm.parse_damage_line(REAL_2026_PLAYER) == (
        333, "Enemy Pilot[CORP](Ishtar)")


def test_real_2026_outgoing_is_rejected():
    # 'to' must never match — this is our outgoing damage, not incoming.
    assert gm.parse_damage_line(REAL_2026_OUT) is None


def test_real_2026_miss_is_not_a_damage_event():
    # 'X misses you completely' carries no <b>N</b> amount → not a damage event.
    assert gm.parse_damage_line(REAL_2026_MISS) is None


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
