import pytest
from intel_stream import Span


def test_span_is_frozen_with_fields():
    s = Span(start=0, end=5, kind="system", value="Jita", payload={"system_id": 30000142})
    assert s.start == 0
    assert s.end == 5
    assert s.kind == "system"
    assert s.value == "Jita"
    assert s.payload == {"system_id": 30000142}


def test_span_is_immutable():
    s = Span(start=0, end=1, kind="cyno", value="cyno", payload={})
    with pytest.raises(Exception):
        s.start = 9  # frozen dataclass -> FrozenInstanceError


def test_cyno_pattern_matches_variants():
    from intel_monitor import CYNO_PATTERN
    assert CYNO_PATTERN.search("cyno up") is not None
    assert CYNO_PATTERN.search("CYNOSURAL field") is not None
    assert CYNO_PATTERN.search("cynosural") is not None
    assert CYNO_PATTERN.search("synonym") is None
