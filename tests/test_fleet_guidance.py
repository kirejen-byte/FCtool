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


from fit_models import Fit

def _fit(fit_id, hull, parsed):
    return Fit(id=fit_id, name=fit_id, hull_type_id=hull, hull_name=str(hull),
               source="dna", raw_text="", parsed=parsed, dna="", notes="",
               esi_fitting_ids={}, created="", modified="")

class _Doc:
    def __init__(self, members): self.members = members; self.name = "Doc"

def _build():
    cat = _Cat(groups={42529: 1770, 42530: 1770, 88261: 1770})
    # Retribution(17740) DPS, Deacon(33816) Logi, Claymore(22468) Links 3 bursts
    fits = {
        "ret": _fit("ret", 17740, _parsed(17740, [])),
        "dea": _fit("dea", 33816, _parsed(33816, [])),
        "cla": _fit("cla", 22468, _parsed(22468, [42529, 42530, 88261])),
    }
    doc = _Doc([
        _mem2("ret", ["DPS"]), _mem2("dea", ["Logistics"]), _mem2("cla", ["Links"]),
    ])
    return doc, (lambda fid: fits.get(fid)), cat

def _mem2(fit_id, tags, mode=None, mn=None, mx=None):
    return DoctrineMember(fit_id=fit_id, tags=tags, order=0,
                          ideal_mode=mode, ideal_min=mn, ideal_max=mx)

def test_compute_guidance_basic_targets_and_deltas():
    doc, get_fit, cat = _build()
    # fleet of 20: 7 Retribution(17740), 6 Deacon(33816), 2 Claymore(22468)
    counts = {17740: 7, 33816: 6, 22468: 2}
    rep = fg.compute_fleet_guidance(doc, get_fit, cat, counts, 20, command_ship_fraction=0.1)
    by = {f.fit_id: f for f in rep.fits}
    # DPS 50-60% of 20 = 10-12; current 7 -> under +3
    assert by["ret"].target_min == 10 and by["ret"].target_max == 12
    assert by["ret"].status == "under" and by["ret"].delta == 3
    # Logi 25-35% of 20 = 5-7; current 6 -> in
    assert by["dea"].status == "in" and by["dea"].delta == 0
    # Links 3-6; current 2 -> under +1
    assert by["cla"].target_min == 3 and by["cla"].target_max == 6
    assert by["cla"].status == "under" and by["cla"].delta == 1
    assert rep.links_suppressed is False and rep.has_live_fleet is True

def test_compute_guidance_links_suppressed_above_threshold():
    doc, get_fit, cat = _build()
    counts = {17740: 7, 33816: 6, 22468: 2}
    rep = fg.compute_fleet_guidance(doc, get_fit, cat, counts, 20, command_ship_fraction=0.5)
    assert rep.links_suppressed is True
    assert all(f.role != "Links" for f in rep.fits)

def test_compute_guidance_defenders_overlay():
    cat = _Cat(groups={})
    ret_parsed = _parsed(17740, [44102])  # Retribution with a defender launcher
    fits = {"ret": _fit("ret", 17740, ret_parsed)}
    doc = _Doc([_mem2("ret", ["DPS", "Defenders"])])
    counts = {17740: 6}
    rep = fg.compute_fleet_guidance(doc, (lambda fid: fits.get(fid)), cat, counts, 12,
                                    command_ship_fraction=0.0)
    d = rep.roles["Defenders"]
    assert d.target_min == 8 and d.target_max is None
    assert d.current == 6 and d.status == "under" and d.delta == 2
    # The same fit still carries its DPS composition guidance.
    assert any(f.role == "DPS" for f in rep.fits)

def test_compute_guidance_no_live_fleet_unknown():
    doc, get_fit, cat = _build()
    rep = fg.compute_fleet_guidance(doc, get_fit, cat, {}, None, command_ship_fraction=0.0)
    assert rep.has_live_fleet is False
    assert all(f.current is None and f.status == "unknown" for f in rep.fits)


def test_defenders_overlay_no_live_fleet_unknown():
    # With no live fleet, the Defenders overlay reports the static target but an
    # unknown current (the hull computation is skipped, output is unchanged).
    cat = _Cat(groups={})
    fits = {"ret": _fit("ret", 17740, _parsed(17740, [44102]))}
    doc = _Doc([_mem2("ret", ["DPS", "Defenders"])])
    rep = fg.compute_fleet_guidance(doc, (lambda fid: fits.get(fid)), cat, {}, None,
                                    command_ship_fraction=0.0)
    d = rep.roles["Defenders"]
    assert d.target_min == 8 and d.target_max is None
    assert d.current is None and d.status == "unknown" and d.delta == 0


def test_resolve_percent_blank_min_falls_through_to_dps_default():
    # mode="percent" with a blank (None) min must NOT be treated as an authoritative
    # override; it falls through to the DPS tag default and does not raise.
    e = fg.resolve_composition_ideal(_mem(["DPS"], mode="percent", mn=None), links_range=None)
    assert (e.mode, e.min, e.max, e.role) == ("percent", 50, 60, "DPS")

def test_resolve_count_blank_min_falls_through_to_webs_default():
    # mode="count" with a blank (None) min falls through to the Support - Webs default.
    e = fg.resolve_composition_ideal(_mem(["Support - Webs"], mode="count", mn=None), links_range=None)
    assert (e.mode, e.min, e.max, e.role) == ("count", 2, 6, "Support - Webs")

def test_compute_guidance_percent_blank_min_uses_dps_default_live():
    # End-to-end: a DPS member with ideal_mode="percent" but a blank (None) min must
    # not raise against a live fleet; it uses the default 50-60% targets.
    cat = _Cat(groups={})
    fits = {"ret": _fit("ret", 17740, _parsed(17740, []))}
    doc = _Doc([_mem2("ret", ["DPS"], mode="percent", mn=None)])
    counts = {17740: 7}
    rep = fg.compute_fleet_guidance(doc, (lambda fid: fits.get(fid)), cat, counts, 20,
                                    command_ship_fraction=0.0)
    by = {f.fit_id: f for f in rep.fits}
    assert by["ret"].mode == "percent" and by["ret"].role == "DPS"
    # 50-60% of 20 = 10-12 (via percent_to_pilots), current 7 -> under +3
    assert by["ret"].target_min == fg.percent_to_pilots(50, 20) == 10
    assert by["ret"].target_max == fg.percent_to_pilots(60, 20) == 12
    assert by["ret"].status == "under" and by["ret"].delta == 3
