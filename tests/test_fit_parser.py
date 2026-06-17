from pathlib import Path
from fit_parser import parse_eft, detect_and_parse, FitParseError
from fit_models import SLOT_HIGH, SLOT_MED, SLOT_LOW

FIX = Path(__file__).parent / "fixtures" / "fittings"


class FakeCatalog:
    NAMES = {
        "muninn": 12015, "damage control ii": 2048,
        "720mm howitzer artillery ii": 2185, "republic fleet emp m": 215,
        "hobgoblin ii": 12058, "gyrostabilizer ii": 519,
        "tracking computer ii": 1978, "1600mm steel plates ii": 20353,
        "energized adaptive nano membrane ii": 11269,
        "50mn quad lif restrained microwarpdrive": 5945,
        "republic fleet phased plasma m": 216, "nanite repair paste": 28668,
        "medium ancillary current router ii": 31794,
        "medium projectile burst aerator ii": 31796,
        "republic fleet titanium sabot m": 12625,
    }
    SLOTS = {2048: "low", 2185: "high", 519: "low", 1978: "med", 20353: "low",
             11269: "low", 5945: "med", 31794: "rig", 31796: "rig"}
    CATS = {12058: "drone", 215: "charge", 216: "charge", 28668: "other", 12625: "charge"}

    def resolve_id(self, name): return self.NAMES.get(name.strip().lower())
    def resolve_name(self, tid): return next((n for n, i in self.NAMES.items() if i == tid), None)
    def slot_of(self, tid): return self.SLOTS.get(tid)
    def category_of(self, tid): return self.CATS.get(tid, "module")


def test_parse_muninn_header_and_racks():
    res = parse_eft((FIX / "muninn.eft").read_text(encoding="utf-8"), FakeCatalog())
    fit = res.fit
    assert fit.ship_type_id == 12015
    assert fit.name_hint == "Arty Muninn"               # parser exposes the fit name from header
    highs = [m for m in fit.modules if m.slot == SLOT_HIGH]
    assert len(highs) == 5                               # 5 guns, 6th high is empty
    gun = highs[0]
    assert gun.charge_type_id == 215                     # loaded charge parsed


def test_drone_vs_cargo_disambiguated_by_category():
    res = parse_eft((FIX / "muninn.eft").read_text(encoding="utf-8"), FakeCatalog())
    fit = res.fit
    assert any(d.type_id == 12058 and d.quantity == 5 for d in fit.drones)   # Hobgoblin -> drone bay
    assert any(c.type_id == 216 for c in fit.cargo)                          # ammo -> cargo
    assert all(c.type_id != 12058 for c in fit.cargo)                        # drone not in cargo


def test_blank_run_and_crlf_normalization():
    raw = (FIX / "muninn.eft").read_text(encoding="utf-8").replace("\n", "\r\n")
    res = parse_eft("﻿" + raw + "\n\n\n", FakeCatalog())   # BOM + trailing blanks
    assert res.fit.ship_type_id == 12015


def test_unknown_item_is_warned_not_fatal():
    raw = "[Muninn, Test]\nNonexistent Module XYZ\n"
    res = parse_eft(raw, FakeCatalog())
    assert any("Nonexistent Module" in w for w in res.warnings)
    assert res.fit.ship_type_id == 12015


def test_bad_header_raises():
    import pytest
    with pytest.raises(FitParseError):
        parse_eft("no header here\nDamage Control II\n", FakeCatalog())


def test_detect_picks_eft_vs_dna():
    eft = detect_and_parse((FIX / "muninn.eft").read_text(encoding="utf-8"), FakeCatalog())
    assert eft.fit.ship_type_id == 12015


from fit_parser import parse_dna


def test_parse_dna_ship_and_items():
    # Muninn(12015) + 1x DCII(2048) + 5x guns(2185) + 5x Hobgoblin(12058) + 1000x ammo(215)
    dna = "12015:2185;5:2048;1:12058;5:215;1000::"
    res = parse_dna(dna, FakeCatalog())
    assert res.fit.ship_type_id == 12015
    assert sum(1 for m in res.fit.modules if m.type_id == 2185) == 5  # stacked expands to 5
    assert any(d.type_id == 12058 and d.quantity == 5 for d in res.fit.drones)
    assert any(c.type_id == 215 and c.quantity == 1000 for c in res.fit.cargo)


def test_parse_dna_accepts_per_instance_and_stacked():
    a = parse_dna("12015:2185;5::", FakeCatalog())
    b = parse_dna("12015:2185;1:2185;1:2185;1:2185;1:2185;1::", FakeCatalog())
    assert len([m for m in a.fit.modules if m.type_id == 2185]) == 5
    assert len([m for m in b.fit.modules if m.type_id == 2185]) == 5


def test_parse_dna_strips_offline_underscore():
    res = parse_dna("12015:2185_;1::", FakeCatalog())
    assert res.fit.modules[0].type_id == 2185
    assert res.fit.modules[0].offline is True
