import os

import pytest

from intel_paste import (
    DScan,
    FleetComposition,
    FleetSummary,
    LocalScan,
    detect_and_parse,
)

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "intel")


def _read(name: str) -> str:
    with open(os.path.join(FIXTURE_DIR, name), encoding="utf-8") as f:
        return f.read()


def test_dataclasses_exist():
    assert LocalScan is not None
    assert DScan is not None
    assert FleetComposition is not None
    assert FleetSummary is not None


def test_parse_local_scan_basic():
    from intel_paste import parse_local_scan
    text = "Securitas Protector\nTyreece Arkan\nNessa Volkov\n"
    result = parse_local_scan(text)
    assert isinstance(result, LocalScan)
    assert result.pilot_names == ["Securitas Protector", "Tyreece Arkan", "Nessa Volkov"]


def test_parse_local_scan_skips_blank_lines():
    from intel_paste import parse_local_scan
    text = "Alice\n\n\nBob\n"
    result = parse_local_scan(text)
    assert result.pilot_names == ["Alice", "Bob"]


def test_parse_local_scan_rejects_lines_with_digits():
    from intel_paste import parse_local_scan
    text = "Alice\nRandomEnemy123\nBob\n"
    result = parse_local_scan(text)
    assert result.pilot_names == ["Alice", "Bob"]


def test_parse_local_scan_keeps_apostrophes_and_hyphens():
    from intel_paste import parse_local_scan
    text = "O'Reilly\nJean-Luc\n"
    result = parse_local_scan(text)
    assert result.pilot_names == ["O'Reilly", "Jean-Luc"]


def test_parse_local_scan_from_fixture():
    from intel_paste import parse_local_scan
    result = parse_local_scan(_read("local_scan.txt"))
    assert "Securitas Protector" in result.pilot_names
    assert "RandomEnemy123" not in result.pilot_names  # has digits
