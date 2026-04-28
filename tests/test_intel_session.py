from datetime import datetime, timedelta, timezone

from intel_paste import DScan, DScanRow, FleetSummary, FleetSummaryRow, LocalScan
from intel_session import IntelSession


def _dscan(types: list[str]) -> DScan:
    return DScan(rows=[
        DScanRow(type_id=i, item_name=f"Ship {i}", type_name=t, distance_au=1.0)
        for i, t in enumerate(types)
    ])


def test_add_local_scan_records_timestamp():
    s = IntelSession()
    s.add_local_scan("Jita", LocalScan(pilot_names=["Alice"]))
    assert len(s.local_scans) == 1
    assert s.local_scans[0].system == "Jita"


def test_add_dscan_records_timestamp():
    s = IntelSession()
    s.add_dscan("O-BDXB", _dscan(["Vulture"]))
    assert len(s.dscan_scans) == 1


def test_latest_fleet_paste_returns_most_recent():
    s = IntelSession()
    s.add_fleet_paste(FleetSummary(rows=[FleetSummaryRow("A", "Frigate", 1)]))
    s.add_fleet_paste(FleetSummary(rows=[FleetSummaryRow("B", "Frigate", 2)]))
    latest = s.latest_fleet_paste()
    assert latest is not None
    assert latest.parsed.rows[0].ship_name == "B"


def test_latest_fleet_paste_when_empty():
    s = IntelSession()
    assert s.latest_fleet_paste() is None


def test_prior_dscan_in_window():
    s = IntelSession()
    older = datetime.now(timezone.utc) - timedelta(minutes=5)
    s.dscan_scans.append(_make_entry(older, "O-BDXB", _dscan(["Sabre"])))
    prior = s.prior_dscan("O-BDXB", window_minutes=15)
    assert prior is not None
    assert prior.parsed.rows[0].type_name == "Sabre"


def test_prior_dscan_outside_window():
    s = IntelSession()
    older = datetime.now(timezone.utc) - timedelta(minutes=20)
    s.dscan_scans.append(_make_entry(older, "O-BDXB", _dscan(["Sabre"])))
    assert s.prior_dscan("O-BDXB", window_minutes=15) is None


def test_prior_dscan_different_system():
    s = IntelSession()
    older = datetime.now(timezone.utc) - timedelta(minutes=2)
    s.dscan_scans.append(_make_entry(older, "Jita", _dscan(["Sabre"])))
    assert s.prior_dscan("O-BDXB", window_minutes=15) is None


def test_prior_dscan_returns_most_recent_within_window():
    s = IntelSession()
    now = datetime.now(timezone.utc)
    s.dscan_scans.append(_make_entry(now - timedelta(minutes=10), "O-BDXB", _dscan(["A"])))
    s.dscan_scans.append(_make_entry(now - timedelta(minutes=2), "O-BDXB", _dscan(["B"])))
    prior = s.prior_dscan("O-BDXB", window_minutes=15)
    assert prior.parsed.rows[0].type_name == "B"


def test_clear_wipes_all():
    s = IntelSession()
    s.add_local_scan("Jita", LocalScan(pilot_names=["X"]))
    s.add_dscan("Jita", _dscan(["Y"]))
    s.add_fleet_paste(FleetSummary(rows=[]))
    s.clear()
    assert s.local_scans == []
    assert s.dscan_scans == []
    assert s.fleet_pastes == []


def _make_entry(ts, system, parsed):
    from intel_session import ScanEntry
    return ScanEntry(timestamp=ts, system=system, parsed=parsed)
