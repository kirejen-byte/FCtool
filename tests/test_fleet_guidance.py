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
    assert fg.resolve_composition_ideal(_mem(["Special"]), links_range=None) is None

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


# ── Doctrine exemptions (ideal-% denominator) ────────────────────────────────

def test_standard_exemptions_seed_contents():
    ids = {e["id"] for e in fg.STANDARD_EXEMPTIONS}
    # Force Recon (833) + granular capital groups.
    assert ids == {833, 30, 659, 547, 485, 1538, 4594}
    assert all(e["kind"] == "group" for e in fg.STANDARD_EXEMPTIONS)
    assert all(isinstance(e["name"], str) and e["name"] for e in fg.STANDARD_EXEMPTIONS)


def test_effective_exemptions_none_uses_standard():
    doc = _Doc([])
    doc.exemptions = None
    assert fg.effective_exemptions(doc) is fg.STANDARD_EXEMPTIONS


def test_effective_exemptions_empty_list_is_explicit_none():
    doc = _Doc([])
    doc.exemptions = []
    assert fg.effective_exemptions(doc) == []


def test_effective_exemptions_explicit_list_passthrough():
    doc = _Doc([])
    custom = [{"kind": "type", "id": 123, "name": "Widget"}]
    doc.exemptions = custom
    assert fg.effective_exemptions(doc) is custom


def test_effective_exemptions_missing_attr_treated_as_none():
    doc = _Doc([])  # no exemptions attr at all
    assert fg.effective_exemptions(doc) is fg.STANDARD_EXEMPTIONS


def test_is_exempt_type_capital_kind():
    exemptions = [{"kind": "capital"}]
    is_cap = lambda tid: tid == 999
    group_of = lambda tid: None
    assert fg.is_exempt_type(999, exemptions, group_of, is_cap) is True
    assert fg.is_exempt_type(111, exemptions, group_of, is_cap) is False


def test_is_exempt_type_group_kind():
    exemptions = [{"kind": "group", "id": 833, "name": "Force Recon Ship"}]
    group_of = lambda tid: 833 if tid == 11957 else 500
    is_cap = lambda tid: False
    assert fg.is_exempt_type(11957, exemptions, group_of, is_cap) is True
    assert fg.is_exempt_type(22222, exemptions, group_of, is_cap) is False


def test_is_exempt_type_type_kind():
    exemptions = [{"kind": "type", "id": 671, "name": "Erebus"}]
    group_of = lambda tid: None
    is_cap = lambda tid: False
    assert fg.is_exempt_type(671, exemptions, group_of, is_cap) is True
    assert fg.is_exempt_type(672, exemptions, group_of, is_cap) is False


def test_is_exempt_type_empty_exemptions_never_matches():
    assert fg.is_exempt_type(671, [], (lambda t: 1), (lambda t: True)) is False


def test_is_exempt_type_multiple_kinds_any_match():
    exemptions = [
        {"kind": "capital"},
        {"kind": "group", "id": 833, "name": "Force Recon Ship"},
        {"kind": "type", "id": 671, "name": "Erebus"},
    ]
    group_of = lambda tid: 833 if tid == 11957 else None
    is_cap = lambda tid: tid == 23773
    assert fg.is_exempt_type(23773, exemptions, group_of, is_cap) is True   # capital
    assert fg.is_exempt_type(11957, exemptions, group_of, is_cap) is True   # group
    assert fg.is_exempt_type(671, exemptions, group_of, is_cap) is True     # type
    assert fg.is_exempt_type(555, exemptions, group_of, is_cap) is False


def test_compute_guidance_exempt_present_ship_reduces_denominator():
    # Fleet of 20 but 4 are exempt (present in fleet, NOT a doctrine hull) -> adj 16.
    doc, get_fit, cat = _build()
    counts = {17740: 7, 33816: 6, 22468: 2, 11957: 4}  # 4 Force Recons (exempt)
    rep = fg.compute_fleet_guidance(
        doc, get_fit, cat, counts, 20, command_ship_fraction=0.1,
        exempt_type_ids={11957}, doctrine_hull_ids={17740, 33816, 22468})
    by = {f.fit_id: f for f in rep.fits}
    # DPS 50-60% of adjusted 16 = 8-10 (was 10-12 at 20). current 7 -> under +1.
    assert by["ret"].target_min == 8 and by["ret"].target_max == 10
    assert by["ret"].current == 7  # numerator unchanged (role sum)
    assert by["ret"].status == "under" and by["ret"].delta == 1
    assert rep.excluded_from_pct == 4


def test_compute_guidance_exempt_doctrine_hull_still_counted():
    # A doctrine hull that is also exempt-by-group must STILL count in the denominator
    # (exact-hull-id override): it is a doctrine ship, so not excluded.
    doc, get_fit, cat = _build()
    # Say the DPS hull (17740) is exempt by type-id resolution, but it's a doctrine hull.
    counts = {17740: 7, 33816: 6, 22468: 2}
    rep = fg.compute_fleet_guidance(
        doc, get_fit, cat, counts, 20, command_ship_fraction=0.1,
        exempt_type_ids={17740}, doctrine_hull_ids={17740, 33816, 22468})
    by = {f.fit_id: f for f in rep.fits}
    # Denominator stays 20 (17740 excluded from the exemption because it's a doctrine hull).
    assert by["ret"].target_min == 10 and by["ret"].target_max == 12
    assert rep.excluded_from_pct == 0


def test_compute_guidance_adj_total_clamps_at_one():
    # If exemptions would zero out the denominator, it clamps to 1 (never 0/negative).
    doc, get_fit, cat = _build()
    counts = {17740: 7, 33816: 6, 22468: 2, 11957: 20}
    rep = fg.compute_fleet_guidance(
        doc, get_fit, cat, counts, 20, command_ship_fraction=0.1,
        exempt_type_ids={11957}, doctrine_hull_ids={17740, 33816, 22468})
    # All 20 excluded -> adj clamps to 1. 50% of 1 = 1 (ceil), etc.
    by = {f.fit_id: f for f in rep.fits}
    assert by["ret"].target_min == fg.percent_to_pilots(50, 1) == 1
    assert rep.excluded_from_pct == 20


def test_compute_guidance_no_exemptions_denominator_unchanged():
    doc, get_fit, cat = _build()
    counts = {17740: 7, 33816: 6, 22468: 2}
    rep = fg.compute_fleet_guidance(doc, get_fit, cat, counts, 20, command_ship_fraction=0.1)
    by = {f.fit_id: f for f in rep.fits}
    assert by["ret"].target_min == 10 and by["ret"].target_max == 12
    assert rep.excluded_from_pct == 0


def test_compute_guidance_excluded_count_only_counts_present_ships():
    # An exempt type id that is NOT present in the fleet contributes 0 to excluded.
    doc, get_fit, cat = _build()
    counts = {17740: 7, 33816: 6, 22468: 2}  # no 11957 present
    rep = fg.compute_fleet_guidance(
        doc, get_fit, cat, counts, 20, command_ship_fraction=0.1,
        exempt_type_ids={11957}, doctrine_hull_ids={17740, 33816, 22468})
    assert rep.excluded_from_pct == 0
    by = {f.fit_id: f for f in rep.fits}
    assert by["ret"].target_min == 10 and by["ret"].target_max == 12


def test_compute_guidance_multi_fit_role_shares_role_level_delta():
    # Two Logistics fits (Basilisk + Scimitar). Logi default 25-35%. Fleet of 100.
    # 10 Basilisk + 11 Scimitar = 21 logi total (21%). Min is 25 -> role is +4 short,
    # and that +4 must appear on BOTH fits (not per-hull deltas).
    cat = _Cat(groups={})
    BASI, SCIM = 11985, 11978
    fits = {
        "basi": _fit("basi", BASI, _parsed(BASI, [])),
        "scim": _fit("scim", SCIM, _parsed(SCIM, [])),
    }
    doc = _Doc([_mem2("basi", ["Logistics"]), _mem2("scim", ["Logistics"])])
    counts = {BASI: 10, SCIM: 11}   # 21 logi total
    rep = fg.compute_fleet_guidance(doc, (lambda fid: fits.get(fid)), cat, counts, 100,
                                    command_ship_fraction=0.0)
    by = {f.fit_id: f for f in rep.fits}
    # Role-level target 25-35 of 100; current = 21 (the SUM, not per-hull); +4 on each.
    assert by["basi"].target_min == 25 and by["basi"].target_max == 35
    assert by["scim"].target_min == 25 and by["scim"].target_max == 35
    assert by["basi"].current == 21 and by["scim"].current == 21
    assert by["basi"].delta == 4 and by["scim"].delta == 4
    assert by["basi"].status == "under" and by["scim"].status == "under"
    # Role rollup is NOT double-counted (would be 50-70 / 42 under the old summing bug).
    rr = rep.roles["Logistics"]
    assert (rr.target_min, rr.target_max, rr.current, rr.delta) == (25, 35, 21, 4)


def test_compute_guidance_multi_fit_role_explicit_override_wins():
    # An explicit override on one Logi fit represents the whole role (25-30%).
    cat = _Cat(groups={})
    BASI, SCIM = 11985, 11978
    fits = {"basi": _fit("basi", BASI, _parsed(BASI, [])),
            "scim": _fit("scim", SCIM, _parsed(SCIM, []))}
    doc = _Doc([_mem2("basi", ["Logistics"], mode="percent", mn=25, mx=30),
                _mem2("scim", ["Logistics"])])
    counts = {BASI: 10, SCIM: 11}   # 21 of 100
    rep = fg.compute_fleet_guidance(doc, (lambda fid: fits.get(fid)), cat, counts, 100,
                                    command_ship_fraction=0.0)
    by = {f.fit_id: f for f in rep.fits}
    assert by["basi"].target_min == 25 and by["basi"].target_max == 30
    assert by["scim"].target_min == 25 and by["scim"].target_max == 30  # role-level
    assert by["basi"].delta == 4 and by["scim"].delta == 4


def test_compute_guidance_small_fleet_percent_min_not_rounded_to_zero():
    # 2-person fleet, Logistics 25-35%. Old banker's round made 25% of 2 = 0 ("in
    # range" with 0 logi -> no feedback). Ceil -> min 1, so 0 logi shows +1.
    cat = _Cat(groups={})
    SCIM = 11978
    fits = {"scim": _fit("scim", SCIM, _parsed(SCIM, []))}
    doc = _Doc([_mem2("scim", ["Logistics"])])
    counts = {17740: 2}   # 2 DPS-ish hulls, ZERO logistics in the fleet
    rep = fg.compute_fleet_guidance(doc, (lambda fid: fits.get(fid)), cat, counts, 2,
                                    command_ship_fraction=0.0)
    by = {f.fit_id: f for f in rep.fits}
    assert by["scim"].target_min == 1            # ceil(0.25*2)=1, not 0
    assert by["scim"].current == 0
    assert by["scim"].status == "under" and by["scim"].delta == 1   # now shows +1


def test_percent_to_pilots_ceils_small_fractions():
    assert fg.percent_to_pilots(25, 2) == 1     # 0.5 -> 1 (was 0 under banker's round)
    assert fg.percent_to_pilots(50, 20) == 10   # exact, unchanged
    assert fg.percent_to_pilots(55, 21) == 12   # 11.55 -> 12


def test_resolve_tackle_explicit_count_ideal():
    e = fg.resolve_composition_ideal(_mem(["Tackle"], mode="count", mn=3, mx=8),
                                     links_range=None)
    assert (e.mode, e.min, e.max, e.role) == ("count", 3, 8, "Tackle")


def test_resolve_tackle_without_explicit_ideal_is_none():
    # Tackle is a composition role but has no static default, so an unset Tackle
    # fit yields no guidance (explicit-only).
    assert fg.resolve_composition_ideal(_mem(["Tackle"]), links_range=None) is None


def test_compute_guidance_tackle_role_delta_count():
    # A Raptor (tackle frigate) tagged Tackle with explicit 3-8 count; 2 in fleet -> +1.
    cat = _Cat(groups={})
    RAPTOR = 11178
    fits = {"rap": _fit("rap", RAPTOR, _parsed(RAPTOR, []))}
    doc = _Doc([_mem2("rap", ["Tackle"], mode="count", mn=3, mx=8)])
    counts = {RAPTOR: 2}
    rep = fg.compute_fleet_guidance(doc, (lambda fid: fits.get(fid)), cat, counts, 50,
                                    command_ship_fraction=0.0)
    by = {f.fit_id: f for f in rep.fits}
    assert by["rap"].role == "Tackle" and by["rap"].mode == "count"
    assert by["rap"].target_min == 3 and by["rap"].target_max == 8
    assert by["rap"].current == 2 and by["rap"].status == "under" and by["rap"].delta == 1
    assert "Tackle" in rep.roles and rep.roles["Tackle"].delta == 1
