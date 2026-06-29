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


def _kinds(spans):
    return {s.kind for s in spans}


def test_count_keyword_tier(monkeypatch):
    import intel_stream
    monkeypatch.setattr(intel_stream, "resolve_name", lambda n: None)
    spans = annotate("10 hostiles inbound")
    cs = [s for s in spans if s.kind == "count"]
    assert len(cs) == 1
    assert cs[0].payload == {"count": 10}
    assert cs[0].value == "10"


def test_count_explicit_plus(monkeypatch):
    import intel_stream
    monkeypatch.setattr(intel_stream, "resolve_name", lambda n: None)
    spans = annotate("gang +5 coming")
    cs = [s for s in spans if s.kind == "count"]
    assert len(cs) == 1
    assert cs[0].payload == {"count": 5}


def test_bare_count_only_with_hostile_context(monkeypatch):
    import intel_stream
    monkeypatch.setattr(intel_stream, "resolve_name", lambda n: None)
    # bare digit near a hostile keyword -> count
    assert any(s.kind == "count" and s.payload == {"count": 8}
               for s in annotate("gang of 8 here"))
    # bare digit with no hostile context -> NOT a count
    assert [s for s in annotate("be there in 5 jumps") if s.kind == "count"] == []


def test_dscan_url(monkeypatch):
    import intel_stream
    monkeypatch.setattr(intel_stream, "resolve_name", lambda n: None)
    url = "https://dscan.info/v/abc123"
    spans = annotate(f"scan {url} fresh")
    ds = [s for s in spans if s.kind == "dscan_url"]
    assert len(ds) == 1
    assert ds[0].payload == {"url": url}
    assert ds[0].value == url


def test_clear_camp_spike_cyno(monkeypatch):
    import intel_stream
    monkeypatch.setattr(intel_stream, "resolve_name", lambda n: None)
    assert any(s.kind == "clear" for s in annotate("clr"))
    assert any(s.kind == "camp" for s in annotate("gate camp up"))
    assert any(s.kind == "spike" for s in annotate("spike in local"))
    assert any(s.kind == "cyno" for s in annotate("cyno lit"))


def test_combined_line(monkeypatch):
    import intel_stream
    monkeypatch.setattr(intel_stream, "resolve_name",
                        lambda n: 30002187 if n.lower() == "amamake" else None)
    spans = annotate("Amamake 5 reds camp cyno")
    kinds = _kinds(spans)
    assert {"system", "count", "camp", "cyno"} <= kinds
    # non-overlapping: no two spans share any offset
    spans = sorted(spans, key=lambda s: s.start)
    for a, b in zip(spans, spans[1:]):
        assert a.end <= b.start


def test_plain_chatter_no_false_positives(monkeypatch):
    import intel_stream
    monkeypatch.setattr(intel_stream, "resolve_name", lambda n: None)
    assert annotate("anyone want to run abyssals later") == []
