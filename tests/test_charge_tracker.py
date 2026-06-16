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
