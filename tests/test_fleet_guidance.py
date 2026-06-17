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
