from charge_tracker import ChargeTracker
from command_bursts import SHIELD, ARMOR, SKIRMISH, INFORMATION


def test_record_returns_true_then_ignores_empty():
    t = ChargeTracker()
    assert t.record("Pilot A", "Active Shielding Charge") is True
    assert t.record("Pilot A", "x up please") is False           # no charge -> ignored
    snap = dict(t.snapshot())
    assert snap["Pilot A"] == {(SHIELD, "Active Shielding Charge")}


def test_record_overwrites_prior_set():
    t = ChargeTracker()
    t.record("Pilot A", "Active Shielding Charge  Shield Extension Charge")
    t.record("Pilot A", "Rapid Repair Charge")  # new post -> replace
    snap = dict(t.snapshot())
    assert snap["Pilot A"] == {(ARMOR, "Rapid Repair Charge")}


def test_record_no_change_returns_false():
    t = ChargeTracker()
    t.record("Pilot A", "Active Shielding Charge")
    assert t.record("Pilot A", "Active Shielding Charge") is False


def test_record_ignores_blank_sender():
    t = ChargeTracker()
    assert t.record("", "Active Shielding Charge") is False


def test_coverage_union_across_pilots():
    t = ChargeTracker()
    t.record("A", "Active Shielding Charge  Shield Extension Charge")
    t.record("B", "Shield Harmonizing Charge")
    cov = t.coverage()
    assert cov[SHIELD].full is True
    assert cov[SHIELD].missing == []
    assert cov[ARMOR].full is False
    assert set(cov[ARMOR].missing) == {
        "Armor Energizing Charge", "Armor Reinforcement Charge", "Rapid Repair Charge",
    }


def test_clear_empties_state():
    t = ChargeTracker()
    t.record("A", "Active Shielding Charge")
    t.clear()
    assert t.snapshot() == []
    assert t.coverage()[SHIELD].full is False


def test_coverage_redundancy_single_pilot_full_is_one():
    t = ChargeTracker()
    t.record("A", "Active Shielding Charge  Shield Extension Charge  Shield Harmonizing Charge")
    cov = t.coverage()
    assert cov[SHIELD].full is True
    assert cov[SHIELD].redundancy == 1


def test_coverage_redundancy_double():
    t = ChargeTracker()
    full = "Active Shielding Charge  Shield Extension Charge  Shield Harmonizing Charge"
    t.record("A", full)
    t.record("B", full)
    assert t.coverage()[SHIELD].redundancy == 2


def test_coverage_redundancy_triple():
    t = ChargeTracker()
    full = "Active Shielding Charge  Shield Extension Charge  Shield Harmonizing Charge"
    t.record("A", full)
    t.record("B", full)
    t.record("C", full)
    assert t.coverage()[SHIELD].redundancy == 3


def test_coverage_redundancy_is_min_across_charges():
    t = ChargeTracker()
    t.record("A", "Active Shielding Charge  Shield Extension Charge  Shield Harmonizing Charge")
    t.record("B", "Active Shielding Charge  Shield Extension Charge")  # missing Harmonizing
    cov = t.coverage()
    assert cov[SHIELD].full is True       # all 3 present (A covers Harmonizing)
    assert cov[SHIELD].redundancy == 1    # Harmonizing covered by only A -> min is 1


def test_coverage_redundancy_zero_when_not_full():
    t = ChargeTracker()
    t.record("A", "Active Shielding Charge  Shield Extension Charge")  # missing Harmonizing
    cov = t.coverage()
    assert cov[SHIELD].full is False
    assert cov[SHIELD].redundancy == 0


def test_remove_pilot_removes_and_returns_true():
    t = ChargeTracker()
    t.record("A", "Active Shielding Charge")
    t.record("B", "Shield Extension Charge")
    assert t.remove_pilot("A") is True
    snap = dict(t.snapshot())
    assert "A" not in snap
    assert snap["B"] == {(SHIELD, "Shield Extension Charge")}


def test_remove_pilot_unknown_returns_false():
    t = ChargeTracker()
    t.record("A", "Active Shielding Charge")
    assert t.remove_pilot("Ghost") is False
    assert dict(t.snapshot()).keys() == {"A"}


def test_remove_pilot_updates_coverage():
    t = ChargeTracker()
    full = "Active Shielding Charge  Shield Extension Charge  Shield Harmonizing Charge"
    t.record("A", full)
    assert t.coverage()[SHIELD].full is True
    assert t.remove_pilot("A") is True
    assert t.coverage()[SHIELD].full is False
