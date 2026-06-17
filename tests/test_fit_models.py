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
                            "Support - EWAR", "Support - Webs", "Tackle", "Special"]


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
