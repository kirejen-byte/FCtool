"""Tests for intel_monitor.read_channel_id — extracting a channel's numeric
ID from its EVE chat-log header.

EVE writes one UTF-16LE log per channel-session, beginning with a BOM and a
header block. For a PLAYER channel the header carries
``Channel ID:  -84651075`` (a NEGATIVE integer) alongside ``Channel Name:``.
``read_channel_id`` returns that raw id as a STRING (leading minus preserved),
reusing discover_channels' case-insensitive filename-prefix matching.
"""
import os

from intel_monitor import read_channel_id


# Realistic EVE chat-log header for a PLAYER channel (UTF-16LE on disk, BOM
# first). The ``Channel ID`` is a negative integer for player channels.
def _player_log_text(channel_id, channel_name, listener="Someone"):
    return (
        "﻿"  # BOM, as EVE writes
        "---------------------------------------------------------------\n"
        f"  Channel ID:      {channel_id}\n"
        f"  Channel Name:    {channel_name}\n"
        f"  Listener:        {listener}\n"
        "  Session started: 2026.06.16 12:00:00\n"
        "---------------------------------------------------------------\n"
        "[ 2026.06.16 12:01:00 ] Someone > hi\n"
    )


def _make_log(dir_path, filename, *, channel_id, channel_name,
              listener="Someone", mtime=None):
    path = dir_path / filename
    path.write_text(
        _player_log_text(channel_id, channel_name, listener),
        encoding="utf-16-le",
    )
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_read_channel_id_returns_raw_negative_id_string(tmp_path):
    _make_log(
        tmp_path, "Cap Chain Alpha_20260616_120000_123.txt",
        channel_id="-84651075", channel_name="Cap Chain Alpha",
    )
    assert read_channel_id(str(tmp_path), "Cap Chain Alpha") == "-84651075"


def test_read_channel_id_unknown_channel_returns_none(tmp_path):
    _make_log(
        tmp_path, "Cap Chain Alpha_20260616_120000_123.txt",
        channel_id="-84651075", channel_name="Cap Chain Alpha",
    )
    assert read_channel_id(str(tmp_path), "No Such Channel") is None


def test_read_channel_id_is_case_insensitive(tmp_path):
    _make_log(
        tmp_path, "Cap Chain Alpha_20260616_120000_123.txt",
        channel_id="-84651075", channel_name="Cap Chain Alpha",
    )
    assert read_channel_id(str(tmp_path), "cap chain alpha") == "-84651075"
    assert read_channel_id(str(tmp_path), "CAP CHAIN ALPHA") == "-84651075"


def test_read_channel_id_picks_newest_matching_header(tmp_path):
    # Two sessions of the same channel; the newest file's id must win.
    _make_log(
        tmp_path, "Cap Chain Alpha_20260101_120000_1.txt",
        channel_id="-11111111", channel_name="Cap Chain Alpha",
        mtime=1000,
    )
    _make_log(
        tmp_path, "Cap Chain Alpha_20260616_120000_2.txt",
        channel_id="-84651075", channel_name="Cap Chain Alpha",
        mtime=2000,
    )
    assert read_channel_id(str(tmp_path), "Cap Chain Alpha") == "-84651075"


def test_read_channel_id_missing_dir_returns_none(tmp_path):
    assert read_channel_id(str(tmp_path / "nope"), "Cap Chain Alpha") is None
