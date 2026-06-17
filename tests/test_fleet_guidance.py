# tests/test_fleet_guidance.py
from fit_models import ParsedFit, ParsedModule
import fleet_guidance as fg


class _Cat:
    """Catalog stub: group_of from a dict; resolve_name passthrough."""
    def __init__(self, groups=None, names=None):
        self._g = groups or {}
        self._n = names or {}
    def group_of(self, tid): return self._g.get(tid)
    def resolve_name(self, tid): return self._n.get(tid, str(tid))
    def category_of(self, tid): return "module"


def _parsed(ship_type_id, module_type_ids, names=None):
    names = names or {}
    return ParsedFit(
        ship_type_id=ship_type_id, ship_name="X",
        modules=[ParsedModule(t, names.get(t, str(t)), "high") for t in module_type_ids],
        drones=[], cargo=[], subsystems=[],
    )


def test_count_command_bursts_counts_group_1770_modules():
    parsed = _parsed(29984, [42529, 42530, 9999])  # 2 bursts + 1 other
    cat = _Cat(groups={42529: 1770, 42530: 1770, 9999: 500})
    assert fg.count_command_bursts(parsed, cat) == 2

def test_count_command_bursts_name_fallback_when_group_missing():
    parsed = _parsed(29984, [1, 2], names={1: "Shield Command Burst II", 2: "Damage Control II"})
    cat = _Cat(groups={})  # no group info
    assert fg.count_command_bursts(parsed, cat) == 1

def test_has_defender_launcher_by_type_id():
    parsed = _parsed(587, [44102, 1])
    assert fg.has_defender_launcher(parsed, _Cat()) is True

def test_has_defender_launcher_by_name():
    parsed = _parsed(587, [55555], names={55555: "Defender Launcher I"})
    assert fg.has_defender_launcher(parsed, _Cat(names={55555: "Defender Launcher I"})) is True

def test_has_defender_launcher_absent():
    parsed = _parsed(587, [1, 2])
    assert fg.has_defender_launcher(parsed, _Cat()) is False


def test_links_ideal_range_claymore_3_bursts():
    cat = _Cat(groups={42529: 1770, 42530: 1770, 88261: 1770})
    claymore = _parsed(22468, [42529, 42530, 88261])  # 3 bursts
    assert fg.links_ideal_range([claymore], cat) == (3, 6)

def test_links_ideal_range_averages_multiple_fits():
    cat = _Cat(groups={1: 1770, 2: 1770})
    a = _parsed(22468, [1, 2])          # 2 bursts
    b = _parsed(22468, [1, 2, 1, 2])    # 4 bursts  -> avg 3
    assert fg.links_ideal_range([a, b], cat) == (3, 6)

def test_links_ideal_range_none_when_no_bursts():
    cat = _Cat(groups={})
    assert fg.links_ideal_range([_parsed(22468, [1])], cat) is None

def test_links_ideal_range_none_when_no_fits():
    assert fg.links_ideal_range([], _Cat()) is None


from fit_models import DoctrineMember

def _mem(tags, mode=None, mn=None, mx=None):
    return DoctrineMember(fit_id="f", tags=tags, order=0,
                          ideal_mode=mode, ideal_min=mn, ideal_max=mx)

def test_resolve_uses_tag_default_when_unset():
    e = fg.resolve_composition_ideal(_mem(["DPS"]), links_range=None)
    assert (e.mode, e.min, e.max) == ("percent", 50, 60)

def test_resolve_logistics_default():
    e = fg.resolve_composition_ideal(_mem(["Logistics"]), links_range=None)
    assert (e.mode, e.min, e.max) == ("percent", 25, 35)

def test_resolve_links_uses_computed_range():
    e = fg.resolve_composition_ideal(_mem(["Links"]), links_range=(3, 6))
    assert (e.mode, e.min, e.max) == ("count", 3, 6)

def test_resolve_links_none_when_no_range():
    assert fg.resolve_composition_ideal(_mem(["Links"]), links_range=None) is None

def test_resolve_off_returns_none():
    assert fg.resolve_composition_ideal(_mem(["DPS"], mode="off"), links_range=None) is None

def test_resolve_explicit_override():
    e = fg.resolve_composition_ideal(_mem(["DPS"], mode="count", mn=4, mx=8), links_range=None)
    assert (e.mode, e.min, e.max) == ("count", 4, 8)

def test_resolve_first_role_wins_defenders_excluded():
    # A DPS+Defenders fit resolves its composition ideal as DPS (Defenders is overlay).
    e = fg.resolve_composition_ideal(_mem(["Defenders", "DPS"]), links_range=None)
    assert (e.mode, e.min, e.max) == ("percent", 50, 60)

def test_resolve_no_composition_tag_returns_none():
    assert fg.resolve_composition_ideal(_mem(["Tackle"]), links_range=None) is None

def test_percent_to_pilots_rounds():
    assert fg.percent_to_pilots(50, 20) == 10
    assert fg.percent_to_pilots(55, 21) == 12   # 11.55 -> 12

def test_compute_delta_under_in_over():
    assert fg.compute_delta(7, 10, 12) == ("under", 3)
    assert fg.compute_delta(11, 10, 12) == ("in", 0)
    assert fg.compute_delta(14, 10, 12) == ("over", -2)
    assert fg.compute_delta(5, 8, None) == ("under", 3)   # no upper bound
    assert fg.compute_delta(20, 8, None) == ("in", 0)
