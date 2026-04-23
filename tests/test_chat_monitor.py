import json
import os
from datetime import datetime

from chat_monitor import (
    ChatLogFile,
    ChatMessage,
    ChatMonitor,
    HEADER_CHANNEL_PATTERN,
    HEADER_LISTENER_PATTERN,
    MESSAGE_PATTERN,
)


def _write_utf16le(path, text):
    with open(path, "wb") as f:
        f.write(text.encode("utf-16-le"))


def _append_utf16le(path, text):
    with open(path, "ab") as f:
        f.write(text.encode("utf-16-le"))


def _append_raw(path, raw_bytes):
    with open(path, "ab") as f:
        f.write(raw_bytes)


_FULL_HEADER = (
    "\ufeffChannel ID:      fleet_1213112261803\r\n"
    "Channel Name:    Fleet\r\n"
    "Listener:        Securitas Protector\r\n"
    "Session started: 2026.03.25 20:38:13\r\n"
    "\r\n"
)


def test_parse_header_extracts_channel_and_listener(tmp_path):
    header = (
        "\ufeffChannel ID:      fleet_1213112261803\r\n"
        "Channel Name:    Fleet\r\n"
        "Listener:        Securitas Protector\r\n"
        "Session started: 2026.03.25 20:38:13\r\n"
    )
    fp = tmp_path / "fleet_123.txt"
    _write_utf16le(fp, header)

    log_file = ChatLogFile(str(fp))
    with open(fp, "r", encoding="utf-16-le", errors="replace") as f:
        content = f.read()
    log_file._parse_header(content.split("\n"))

    assert log_file.channel_name == "Fleet"
    assert log_file.listener == "Securitas Protector"
    assert log_file._header_parsed is True


def test_header_patterns_match_directly():
    assert HEADER_CHANNEL_PATTERN.search("Channel Name:    I. Ftn Intel").group(1).strip() == "I. Ftn Intel"
    assert HEADER_LISTENER_PATTERN.search("Listener:        Dave the Pilot").group(1).strip() == "Dave the Pilot"


def test_message_pattern_parses_ascii_line():
    line = "\ufeff[ 2026.03.25 20:38:22 ] Cylic Mithuza > you gotta collect them"
    m = MESSAGE_PATTERN.match(line)
    assert m is not None
    ts = datetime.strptime(m.group(1), "%Y.%m.%d %H:%M:%S")
    assert ts.year == 2026 and ts.month == 3 and ts.day == 25
    assert m.group(2).strip() == "Cylic Mithuza"
    assert m.group(3).strip() == "you gotta collect them"


def test_message_pattern_parses_non_ascii_message():
    line = "\ufeff[ 2026.03.25 20:38:22 ] Müller Groß > ich heiße Straße — it's great"
    m = MESSAGE_PATTERN.match(line)
    assert m is not None
    assert m.group(2).strip() == "Müller Groß"
    assert "Straße" in m.group(3)
    assert "it's great" in m.group(3)


def test_message_pattern_handles_apostrophe_and_punctuation():
    line = "\ufeff[ 2026.03.25 20:38:22 ] Pilot X > Who's on? reply ASAP!"
    m = MESSAGE_PATTERN.match(line)
    assert m is not None
    assert m.group(2).strip() == "Pilot X"
    assert m.group(3).strip() == "Who's on? reply ASAP!"


def test_message_pattern_returns_none_on_malformed_line():
    malformed = [
        "not a chat line",
        "",
        "[ 2026.03.25 ] broken",
        "random text without brackets",
        "[ 2026.03.25 20:38:22 ] no_arrow_here message",
    ]
    for line in malformed:
        m = MESSAGE_PATTERN.match(line)
        assert m is None, f"expected None for {line!r}"


def test_read_new_lines_parses_utf16_log_file(tmp_path):
    content = (
        "\ufeffChannel ID:      fleet_1213112261803\r\n"
        "Channel Name:    Fleet\r\n"
        "Listener:        Securitas Protector\r\n"
        "Session started: 2026.03.25 20:38:13\r\n"
        "\r\n"
        "\ufeff[ 2026.03.25 20:38:22 ] Cylic Mithuza > you gotta collect them\r\n"
        "\ufeff[ 2026.03.25 20:38:30 ] Securitas Protector > on grid\r\n"
    )
    fp = tmp_path / "fleet_abc.txt"
    _write_utf16le(fp, content)

    log_file = ChatLogFile(str(fp))
    msgs = log_file.read_new_lines()

    assert len(msgs) == 2
    assert msgs[0].sender == "Cylic Mithuza"
    assert msgs[0].message == "you gotta collect them"
    assert msgs[0].channel == "Fleet"
    assert msgs[1].sender == "Securitas Protector"
    assert msgs[1].message == "on grid"


# ---------------------------------------------------------------------------
# Phase 2a: binary tailing, partial buffering, rotation, dedupe, persistence
# ---------------------------------------------------------------------------

def test_read_new_lines_binary_mode_three_initial_lines(tmp_path):
    content = _FULL_HEADER + (
        "\ufeff[ 2026.03.25 20:38:22 ] Alpha Pilot > first\r\n"
        "\ufeff[ 2026.03.25 20:38:23 ] Bravo Pilot > second\r\n"
        "\ufeff[ 2026.03.25 20:38:24 ] Charlie Pilot > third\r\n"
    )
    fp = tmp_path / "fleet_bin.txt"
    _write_utf16le(fp, content)

    log_file = ChatLogFile(str(fp))
    msgs = log_file.read_new_lines()

    assert len(msgs) == 3
    assert [m.sender for m in msgs] == ["Alpha Pilot", "Bravo Pilot", "Charlie Pilot"]
    assert [m.message for m in msgs] == ["first", "second", "third"]
    assert log_file.channel_name == "Fleet"


def test_read_new_lines_second_poll_returns_only_new_lines(tmp_path):
    fp = tmp_path / "fleet_incremental.txt"
    _write_utf16le(fp, _FULL_HEADER + "\ufeff[ 2026.03.25 20:38:22 ] Alpha > hi\r\n")

    log_file = ChatLogFile(str(fp))
    first = log_file.read_new_lines()
    assert len(first) == 1

    # Append 5 new lines.
    for i in range(5):
        _append_utf16le(fp, f"\ufeff[ 2026.03.25 20:38:{30 + i:02d} ] P{i} > msg{i}\r\n")

    second = log_file.read_new_lines()
    assert len(second) == 5
    assert [m.sender for m in second] == [f"P{i}" for i in range(5)]


def test_partial_line_buffers_until_newline(tmp_path):
    fp = tmp_path / "fleet_partial.txt"
    _write_utf16le(fp, _FULL_HEADER)

    log_file = ChatLogFile(str(fp))
    log_file.read_new_lines()  # consume header

    full_line = "\ufeff[ 2026.03.25 20:38:22 ] Alpha > hello world\r\n"
    encoded = full_line.encode("utf-16-le")

    # Write all but the final 4 bytes (including the CRLF terminator).
    _append_raw(fp, encoded[:-4])

    partial_msgs = log_file.read_new_lines()
    assert partial_msgs == []  # no complete line yet
    assert log_file._partial != ""  # something held back

    # Now write the remaining bytes - the line should complete.
    _append_raw(fp, encoded[-4:])
    completed = log_file.read_new_lines()
    assert len(completed) == 1
    assert completed[0].sender == "Alpha"
    assert completed[0].message == "hello world"


def test_partial_line_with_odd_byte_write(tmp_path):
    """Simulate EVE writing an odd number of bytes mid-UTF-16 code unit."""
    fp = tmp_path / "fleet_oddbyte.txt"
    _write_utf16le(fp, _FULL_HEADER)

    log_file = ChatLogFile(str(fp))
    log_file.read_new_lines()

    full_line = "\ufeff[ 2026.03.25 20:38:22 ] Delta > testing\r\n"
    encoded = full_line.encode("utf-16-le")

    # Write everything except the last byte - leaves us mid-codepoint.
    _append_raw(fp, encoded[:-1])
    partial_msgs = log_file.read_new_lines()
    assert partial_msgs == []

    # Complete it.
    _append_raw(fp, encoded[-1:])
    completed = log_file.read_new_lines()
    assert len(completed) == 1
    assert completed[0].message == "testing"


def test_truncation_detection_resets_and_reads_new_content(tmp_path):
    fp = tmp_path / "fleet_rotate.txt"
    _write_utf16le(fp, _FULL_HEADER + (
        "\ufeff[ 2026.03.25 20:38:22 ] Alpha > one\r\n"
        "\ufeff[ 2026.03.25 20:38:23 ] Alpha > two\r\n"
    ))
    log_file = ChatLogFile(str(fp))
    first = log_file.read_new_lines()
    assert len(first) == 2

    # Rotation: truncate to zero, then write new header + 2 new lines.
    with open(fp, "wb"):
        pass  # truncate

    new_content = _FULL_HEADER + (
        "\ufeff[ 2026.03.25 20:40:00 ] Echo > rotated1\r\n"
        "\ufeff[ 2026.03.25 20:40:01 ] Echo > rotated2\r\n"
    )
    _write_utf16le(fp, new_content)

    second = log_file.read_new_lines()
    assert len(second) == 2
    assert [m.message for m in second] == ["rotated1", "rotated2"]


def test_dedupe_suppresses_duplicate_across_files(tmp_path):
    """Same (channel, ts, sender, message) must only be emitted once by the monitor."""
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()

    shared_line = "\ufeff[ 2026.03.25 20:38:22 ] Alpha > dup content\r\n"
    content = _FULL_HEADER + shared_line
    fp_a = logs_dir / "fleet_aaa.txt"
    fp_b = logs_dir / "fleet_bbb.txt"
    _write_utf16le(fp_a, content)
    _write_utf16le(fp_b, content)

    state_file = tmp_path / "state.json"
    monitor = ChatMonitor(
        str(logs_dir),
        channel_filter="fleet_",
        state_path=str(state_file),
    )

    # Force both files to be tracked from byte 0 so we actually read their content
    # (discovery defaults unknown files to EOF).
    monitor._discover_files()
    for lf in monitor._tracked_files.values():
        lf._last_pos = 0
        lf._header_parsed = False

    msgs = monitor.poll()
    # Only one logical message should survive dedupe.
    assert len(msgs) == 1
    assert msgs[0].message == "dup content"


def test_state_file_roundtrip_resumes_from_stored_position(tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    fp = logs_dir / "fleet_persist.txt"
    _write_utf16le(fp, _FULL_HEADER + (
        "\ufeff[ 2026.03.25 20:38:22 ] Alpha > one\r\n"
        "\ufeff[ 2026.03.25 20:38:23 ] Alpha > two\r\n"
    ))

    state_file = tmp_path / "state.json"

    monitor1 = ChatMonitor(
        str(logs_dir),
        channel_filter="fleet_",
        state_path=str(state_file),
    )
    # On first discovery the file is unknown -> seeks to EOF.
    monitor1.poll()

    # Append new lines after "startup" captured position.
    _append_utf16le(fp, "\ufeff[ 2026.03.25 20:38:30 ] Alpha > three\r\n")
    emitted = monitor1.poll()
    assert len(emitted) == 1
    assert emitted[0].message == "three"

    # Force state flush and confirm state file exists.
    monitor1._save_state()
    assert state_file.exists()
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    key = os.path.abspath(str(fp))
    assert key in saved
    assert saved[key]["last_pos"] > 0

    # Append more lines; spin up a fresh monitor and confirm it resumes.
    _append_utf16le(fp, "\ufeff[ 2026.03.25 20:38:31 ] Alpha > four\r\n")
    _append_utf16le(fp, "\ufeff[ 2026.03.25 20:38:32 ] Alpha > five\r\n")

    monitor2 = ChatMonitor(
        str(logs_dir),
        channel_filter="fleet_",
        state_path=str(state_file),
    )
    resumed = monitor2.poll()
    assert [m.message for m in resumed] == ["four", "five"]


def test_dedupe_ttl_eviction(tmp_path):
    """Entries older than TTL should be evicted from the seen set."""
    monitor = ChatMonitor(
        str(tmp_path),
        state_path=str(tmp_path / "state.json"),
        dedupe_ttl=0.1,
    )
    msg = ChatMessage(
        timestamp=datetime(2026, 3, 25, 20, 38, 22),
        sender="Alpha",
        message="hello",
        channel="Fleet",
        raw_line="",
    )
    assert monitor._is_duplicate(msg) is False
    assert monitor._is_duplicate(msg) is True  # immediate duplicate
    # Force the stored timestamp into the past and evict.
    key = ChatMonitor._dedupe_key(msg)
    monitor._seen[key] = monitor._seen[key] - 10.0
    monitor._evict_dedupe()
    assert key not in monitor._seen
    assert monitor._is_duplicate(msg) is False
