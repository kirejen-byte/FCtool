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


from intel_stream import annotate


def test_system_at_start(monkeypatch):
    import intel_stream
    monkeypatch.setattr(intel_stream, "resolve_name",
                        lambda n: 30000142 if n.lower() == "jita" else None)
    spans = annotate("Jita 5 reds")
    sys_spans = [s for s in spans if s.kind == "system"]
    assert len(sys_spans) == 1
    s = sys_spans[0]
    assert (s.start, s.end, s.value) == (0, 4, "Jita")
    assert s.payload == {"system_id": 30000142}


def test_system_in_middle(monkeypatch):
    import intel_stream
    monkeypatch.setattr(intel_stream, "resolve_name",
                        lambda n: 30002187 if n.lower() == "amamake" else None)
    spans = annotate("watch out Amamake gate")
    sys_spans = [s for s in spans if s.kind == "system"]
    assert len(sys_spans) == 1
    assert sys_spans[0].value == "Amamake"
    assert "watch out Amamake gate"[sys_spans[0].start:sys_spans[0].end] == "Amamake"


def test_system_at_end(monkeypatch):
    import intel_stream
    monkeypatch.setattr(intel_stream, "resolve_name",
                        lambda n: 30002510 if n.lower() == "rancer" else None)
    spans = annotate("inbound to Rancer")
    sys_spans = [s for s in spans if s.kind == "system"]
    assert len(sys_spans) == 1
    assert sys_spans[0].value == "Rancer"


def test_multiword_old_man_star(monkeypatch):
    import intel_stream
    names = {"old man star": 30002780, "man": 30009999}
    monkeypatch.setattr(intel_stream, "resolve_name",
                        lambda n: names.get(n.lower()))
    spans = annotate("camp in Old Man Star now")
    sys_spans = [s for s in spans if s.kind == "system"]
    assert len(sys_spans) == 1
    assert sys_spans[0].value == "Old Man Star"
    assert sys_spans[0].payload == {"system_id": 30002780}


def test_longest_match_preference(monkeypatch):
    import intel_stream
    names = {"old man star": 30002780, "old": 30001111}
    monkeypatch.setattr(intel_stream, "resolve_name",
                        lambda n: names.get(n.lower()))
    spans = annotate("Old Man Star")
    sys_spans = [s for s in spans if s.kind == "system"]
    assert len(sys_spans) == 1
    assert sys_spans[0].value == "Old Man Star"


def test_sub_three_char_single_word_rejected(monkeypatch):
    import intel_stream
    # "EC" resolves but is a 2-char single-word token -> ignored as noise.
    monkeypatch.setattr(intel_stream, "resolve_name",
                        lambda n: 30000001 if n.lower() == "ec" else None)
    spans = annotate("EC reds")
    assert [s for s in spans if s.kind == "system"] == []


def test_no_system_when_nothing_resolves(monkeypatch):
    import intel_stream
    monkeypatch.setattr(intel_stream, "resolve_name", lambda n: None)
    assert [s for s in annotate("random chatter here") if s.kind == "system"] == []
