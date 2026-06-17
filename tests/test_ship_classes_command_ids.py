from ship_classes import COMMAND_SHIPS, COMMAND_DESTROYERS

# Canonical SDE type_ids (verified 2026-06-16 vs everef.net).
EXPECTED_COMMAND_SHIPS = {
    22448,  # Absolution
    22474,  # Damnation
    22470,  # Nighthawk
    22446,  # Vulture
    22466,  # Astarte
    22442,  # Eos
    22444,  # Sleipnir
    22468,  # Claymore
}
EXPECTED_COMMAND_DESTROYERS = {
    37480,  # Bifrost
    37481,  # Pontifex
    37482,  # Stork
    37483,  # Magus
}
# Battleship IDs that were wrongly present before the fix.
BATTLESHIP_IDS = {24690, 24692, 24688}


def test_command_ships_are_canonical():
    assert COMMAND_SHIPS == EXPECTED_COMMAND_SHIPS


def test_command_ships_have_no_battleship_ids():
    assert COMMAND_SHIPS.isdisjoint(BATTLESHIP_IDS)


def test_command_ships_include_previously_missing_hulls():
    for missing in (22446, 22474, 22442):  # Vulture, Damnation, Eos
        assert missing in COMMAND_SHIPS


def test_command_destroyers_are_canonical():
    assert COMMAND_DESTROYERS == EXPECTED_COMMAND_DESTROYERS
