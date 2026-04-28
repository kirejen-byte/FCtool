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
