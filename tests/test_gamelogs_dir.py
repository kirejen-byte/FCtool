"""Tests for eve_paths.gamelogs_dir_for — swap trailing Chatlogs -> Gamelogs."""
import os

import eve_paths


def test_swaps_trailing_chatlogs_to_gamelogs():
    src = os.path.join("C:", os.sep, "Users", "x", "Documents", "EVE", "logs", "Chatlogs")
    got = eve_paths.gamelogs_dir_for(src)
    assert os.path.basename(got) == "Gamelogs"
    assert os.path.dirname(got) == os.path.dirname(src)


def test_trailing_slash_is_safe():
    src = os.path.join("D:", os.sep, "EVE", "logs", "Chatlogs") + os.sep
    got = eve_paths.gamelogs_dir_for(src)
    assert os.path.basename(os.path.normpath(got)) == "Gamelogs"
    assert os.path.normpath(os.path.dirname(os.path.normpath(got))) == \
        os.path.normpath(os.path.join("D:", os.sep, "EVE", "logs"))


def test_non_chatlogs_input_returned_unchanged():
    src = os.path.join("E:", os.sep, "somewhere", "else")
    assert eve_paths.gamelogs_dir_for(src) == src


def test_blank_input_returned_unchanged():
    assert eve_paths.gamelogs_dir_for("") == ""
    assert eve_paths.gamelogs_dir_for(None) is None
