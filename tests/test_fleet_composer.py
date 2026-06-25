# tests/test_fleet_composer.py
from fleet_composer import Move, ComposeResult, build_tag_index


class _FakeFit:
    def __init__(self, fit_id, hull_type_id):
        self.id = fit_id
        self.hull_type_id = hull_type_id


class _FakeMember:
    def __init__(self, fit_id, tags):
        self.fit_id = fit_id
        self.tags = tags


class _FakeDoctrine:
    def __init__(self, members):
        self.members = members


class _FakeFittings:
    def __init__(self, fits):
        self._fits = {f.id: f for f in fits}

    def get_fit(self, fit_id):
        return self._fits.get(fit_id)


def test_build_tag_index_unions_tags_per_hull():
    fits = [_FakeFit("f-dam", 22474), _FakeFit("f-guard", 11987)]
    doctrine = _FakeDoctrine([
        _FakeMember("f-dam", ["Links"]),
        _FakeMember("f-dam", ["DPS"]),       # same hull, second member → union
        _FakeMember("f-guard", ["Logistics"]),
    ])
    idx = build_tag_index(doctrine, _FakeFittings(fits))
    assert idx[22474] == {"Links", "DPS"}
    assert idx[11987] == {"Logistics"}


def test_build_tag_index_empty_without_doctrine():
    assert build_tag_index(None, _FakeFittings([])) == {}


def test_move_defaults_to_executable():
    m = Move(pilot_id=1, pilot_name="X", target_wing_name="W",
             target_squad_name="S", target_role="squad_member")
    assert m.skip_reason is None
