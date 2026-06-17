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
