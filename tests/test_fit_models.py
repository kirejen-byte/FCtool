from fit_models import (
    ParsedModule, DroneStack, CargoStack, ParsedFit, DEFAULT_TAGS, fit_content_hash,
)


def _fit():
    return ParsedFit(
        ship_type_id=12015, ship_name="Muninn",
        modules=[
            ParsedModule(2048, "Damage Control II", "low"),
            ParsedModule(2185, "720mm Howitzer Artillery II", "high",
                         charge_type_id=215, charge_name="Republic Fleet EMP M"),
        ],
        drones=[DroneStack(12058, "Hobgoblin II", 5)],
        cargo=[CargoStack(215, "Republic Fleet EMP M", 1000)],
        subsystems=[],
    )


def test_default_tags_are_the_seven():
    assert DEFAULT_TAGS == ["DPS", "Links", "Logistics",
                            "Support - EWAR", "Support - Webs", "Defenders", "Tackle", "Special"]


def _doctrine(exemptions="__unset__"):
    from fit_models import Doctrine
    kwargs = {}
    if exemptions != "__unset__":
        kwargs["exemptions"] = exemptions
    return Doctrine(id="d1", name="D", description="", members=[],
                    created="c", modified="m", **kwargs)


def test_doctrine_exemptions_defaults_none():
    assert _doctrine().exemptions is None


def test_doctrine_to_dict_omits_none_exemptions():
    from fit_models import doctrine_to_dict
    d = doctrine_to_dict(_doctrine(None))
    assert "exemptions" not in d


def test_doctrine_to_dict_serializes_list_exemptions():
    from fit_models import doctrine_to_dict
    entries = [{"kind": "capital"},
               {"kind": "group", "id": 833, "name": "Force Recon Ship"},
               {"kind": "type", "id": 671, "name": "Erebus"}]
    d = doctrine_to_dict(_doctrine(entries))
    assert d["exemptions"] == entries


def test_doctrine_to_dict_serializes_empty_list_exemptions():
    from fit_models import doctrine_to_dict
    d = doctrine_to_dict(_doctrine([]))
    assert d["exemptions"] == []


def test_doctrine_from_dict_reads_exemptions():
    from fit_models import doctrine_from_dict
    entries = [{"kind": "type", "id": 671, "name": "Erebus"}]
    doc = doctrine_from_dict({"id": "d1", "name": "D", "exemptions": entries})
    assert doc.exemptions == entries


def test_doctrine_from_dict_missing_exemptions_is_none():
    from fit_models import doctrine_from_dict
    doc = doctrine_from_dict({"id": "d1", "name": "D"})
    assert doc.exemptions is None


def test_doctrine_roundtrip_preserves_exemptions():
    from fit_models import doctrine_to_dict, doctrine_from_dict
    entries = [{"kind": "group", "id": 30, "name": "Titan"}]
    doc = doctrine_from_dict(doctrine_to_dict(_doctrine(entries)))
    assert doc.exemptions == entries


def test_content_hash_is_order_independent_and_stable():
    a = _fit()
    b = _fit()
    b.modules.reverse()                          # same contents, different order
    assert fit_content_hash(a) == fit_content_hash(b)


def test_content_hash_changes_with_contents():
    a = _fit()
    c = _fit()
    c.modules.append(ParsedModule(2048, "Damage Control II", "low"))
    assert fit_content_hash(a) != fit_content_hash(c)


from fit_models import (
    DoctrineMember, doctrine_member_to_dict, doctrine_member_from_dict,
)


def test_doctrine_member_ideal_round_trips():
    m = DoctrineMember(fit_id="f1", tags=["DPS"], order=0,
                       ideal_mode="percent", ideal_min=50, ideal_max=60)
    d = doctrine_member_to_dict(m)
    assert d["ideal_mode"] == "percent" and d["ideal_min"] == 50 and d["ideal_max"] == 60
    assert doctrine_member_from_dict(d) == m


def test_doctrine_member_ideal_absent_defaults_to_none():
    m = doctrine_member_from_dict({"fit_id": "f1", "tags": ["DPS"], "order": 0})
    assert (m.ideal_mode, m.ideal_min, m.ideal_max) == (None, None, None)


def test_doctrine_member_to_dict_omits_none_ideal():
    m = DoctrineMember(fit_id="f1", tags=["DPS"], order=0)
    d = doctrine_member_to_dict(m)
    assert "ideal_mode" not in d and "ideal_min" not in d and "ideal_max" not in d


def test_defenders_in_default_tags():
    assert "Defenders" in DEFAULT_TAGS
