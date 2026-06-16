import pytest

import jump_range
from jump_range import (
    JumpRangeChecker,
    get_stargate_route,
    search_system,
)


# Known EVE system IDs used as fixtures (stable — these never change)
JITA_ID = 30000142
AMARR_ID = 30002187
RENS_ID = 30002510


def test_ship_ranges_at_jdc_5():
    checker_dread = JumpRangeChecker(ship_type="Dreadnought", jdc_level=5)
    checker_blops = JumpRangeChecker(ship_type="Black Ops", jdc_level=5)
    checker_jf = JumpRangeChecker(ship_type="Jump Freighter", jdc_level=5)

    assert checker_dread.jump_range == pytest.approx(7.0, abs=0.01)
    assert checker_blops.jump_range == pytest.approx(8.0, abs=0.01)
    assert checker_jf.jump_range == pytest.approx(10.0, abs=0.01)


def test_ship_ranges_contains_all_expected_classes():
    expected = {
        "Dreadnought", "Carrier", "Force Auxiliary",
        "Supercarrier", "Titan", "Black Ops",
        "Jump Freighter", "Rorqual",
    }
    assert expected.issubset(set(JumpRangeChecker.SHIP_RANGES.keys()))


def test_jdc_scaling_lower_level_reduces_range():
    checker_jdc5 = JumpRangeChecker(ship_type="Dreadnought", jdc_level=5)
    checker_jdc1 = JumpRangeChecker(ship_type="Dreadnought", jdc_level=1)
    assert checker_jdc1.jump_range < checker_jdc5.jump_range


def _install_fake_stargate_graph(mocker):
    """
    Replace the real stargate adjacency graph with a tiny fabricated one
    so route tests never hit disk or the network.

    Topology (minimal, just enough to satisfy the assertions):
        JITA -- HOP_A -- HOP_B -- AMARR
    That's a 4-node chain (length 4). With an Ansiblex JITA|AMARR the
    route collapses to [JITA, AMARR] (length 2), which is strictly
    shorter — the invariant test_ansiblex_shortens_route relies on.
    """
    HOP_A = 30009001
    HOP_B = 30009002
    fake_graph = {
        JITA_ID: {HOP_A},
        HOP_A: {JITA_ID, HOP_B},
        HOP_B: {HOP_A, AMARR_ID},
        AMARR_ID: {HOP_B},
    }
    # Short-circuit the loader so it never reads disk or touches the network,
    # regardless of whether stargate_jumps.json happens to exist.
    mocker.patch("jump_range._load_stargate_graph", lambda: None)
    # Swap the module-level graph the BFS actually walks.
    mocker.patch.dict(jump_range._stargate_graph, fake_graph, clear=True)
    # Clear the route disk cache for the keys these tests would populate so
    # the BFS actually runs against our fabricated graph instead of returning
    # a previously-cached real route.
    for key in (
        f"{JITA_ID}:{AMARR_ID}",
        f"{JITA_ID}:{JITA_ID}",
        f"{JITA_ID}:{AMARR_ID}:{JITA_ID}|{AMARR_ID}",
    ):
        jump_range._route_disk_cache.pop(key, None)


def test_get_stargate_route_jita_to_amarr(mocker):
    _install_fake_stargate_graph(mocker)
    route = get_stargate_route(JITA_ID, AMARR_ID)
    assert route is not None
    assert len(route) > 0
    assert route[0] == JITA_ID
    assert route[-1] == AMARR_ID


def test_get_stargate_route_same_system_returns_single_hop():
    route = get_stargate_route(JITA_ID, JITA_ID)
    assert route == [JITA_ID]


def test_ansiblex_shortens_route(mocker):
    _install_fake_stargate_graph(mocker)
    normal = get_stargate_route(JITA_ID, AMARR_ID)
    with_ansiblex = get_stargate_route(
        JITA_ID, AMARR_ID,
        connections=[f"{JITA_ID}|{AMARR_ID}"],
    )
    assert normal is not None
    assert with_ansiblex is not None
    assert len(with_ansiblex) < len(normal)
    assert with_ansiblex[0] == JITA_ID
    assert with_ansiblex[-1] == AMARR_ID


def test_search_system_exact_match(mocker):
    mocker.patch("jump_range.system_coords.resolve_name", return_value=None)
    fake_resp = mocker.MagicMock()
    fake_resp.ok = True
    fake_resp.json.return_value = {
        "systems": [
            {"id": 30002544, "name": "Uphallant"},
            {"id": 30099999, "name": "Uphallanter"},
        ]
    }
    mocker.patch("jump_range.requests.post", return_value=fake_resp)
    mocker.patch("jump_range.rate_limit")

    # Clear cache for this lookup
    jump_range._system_name_cache.pop("uphallant", None)

    result = search_system("Uphallant")
    assert result == 30002544


def test_search_system_ambiguous_falls_back_to_first(mocker):
    mocker.patch("jump_range.system_coords.resolve_name", return_value=None)
    fake_resp = mocker.MagicMock()
    fake_resp.ok = True
    fake_resp.json.return_value = {
        "systems": [
            {"id": 30099998, "name": "FooSystemA"},
            {"id": 30099999, "name": "FooSystemB"},
        ]
    }
    mocker.patch("jump_range.requests.post", return_value=fake_resp)
    mocker.patch("jump_range.rate_limit")

    jump_range._system_name_cache.pop("nonexistentquery", None)

    result = search_system("NonExistentQuery")
    assert result == 30099998


def test_search_system_no_results(mocker):
    mocker.patch("jump_range.system_coords.resolve_name", return_value=None)
    fake_resp = mocker.MagicMock()
    fake_resp.ok = True
    fake_resp.json.return_value = {"systems": []}
    mocker.patch("jump_range.requests.post", return_value=fake_resp)
    mocker.patch("jump_range.rate_limit")

    jump_range._system_name_cache.pop("bogussystem123", None)

    result = search_system("BogusSystem123")
    assert result is None


def test_search_system_uses_cache_without_network(mocker):
    jump_range._system_name_cache["cachedsystem"] = 12345
    post_mock = mocker.patch("jump_range.requests.post")

    result = search_system("CachedSystem")
    assert result == 12345
    post_mock.assert_not_called()


def test_ly_constant_matches_ccp_official_value():
    # CCP's official in-game light year is exactly 9,460,000,000,000,000.0 m.
    # Not the physical 9.4607e15, not the old wrong 9.4605e15.
    assert jump_range.LY_IN_METERS == 9_460_000_000_000_000.0


def test_calculate_ly_distance_uses_ly_constant(mocker):
    # Two points exactly 9.46e15 m apart on the x-axis must read as 1.00 ly.
    mocker.patch(
        "jump_range.system_coords.get_position",
        side_effect=lambda sid: {
            1: {"x": 0.0, "y": 0.0, "z": 0.0},
            2: {"x": 9.46e15, "y": 0.0, "z": 0.0},
        }.get(sid),
    )
    dist = jump_range.calculate_ly_distance(1, 2)
    assert dist == pytest.approx(1.0, abs=1e-9)


def test_search_system_prefers_local_table_no_network(mocker):
    mocker.patch("jump_range.system_coords.resolve_name", return_value=30000142)
    post = mocker.patch("jump_range.requests.post")
    assert jump_range.search_system("Jita") == 30000142
    post.assert_not_called()  # local hit must not touch ESI


def test_search_system_falls_back_to_esi_when_not_local(mocker):
    mocker.patch("jump_range.system_coords.resolve_name", return_value=None)
    fake = mocker.MagicMock()
    fake.ok = True
    fake.json.return_value = {"systems": [{"id": 30009999, "name": "Weirdspace"}]}
    mocker.patch("jump_range.requests.post", return_value=fake)
    mocker.patch("jump_range.rate_limit")
    jump_range._system_name_cache.pop("weirdspace", None)
    assert jump_range.search_system("Weirdspace") == 30009999


def test_calculate_ly_distance_falls_back_to_esi_position(mocker):
    # Origin known locally, destination only available via ESI get_system_info.
    mocker.patch(
        "jump_range.system_coords.get_position",
        side_effect=lambda sid: {"x": 0.0, "y": 0.0, "z": 0.0} if sid == 1 else None,
    )
    mocker.patch(
        "jump_range.get_system_info",
        return_value={"position": {"x": 9.46e15, "y": 0.0, "z": 0.0}},
    )
    assert jump_range.calculate_ly_distance(1, 2) == pytest.approx(1.0, abs=1e-9)


def test_check_range_can_skip_route(mocker):
    mocker.patch("jump_range.search_system", side_effect=lambda n: {"a": 1, "b": 2}[n.lower()])
    mocker.patch("jump_range.calculate_ly_distance", return_value=3.0)
    mocker.patch("jump_range.system_coords.is_legal_jump_destination", return_value=True)
    route = mocker.patch("jump_range.get_stargate_route")
    checker = jump_range.JumpRangeChecker(ship_type="Dreadnought", jdc_level=5)

    result = checker.check_range("A", "B", include_route=False)
    assert result["in_range"] is True          # 3.0 <= 7.0
    assert result["legal_destination"] is True
    assert result["reachable"] is True
    assert result["gate_jumps"] is None
    route.assert_not_called()                  # route skipped


def test_range_to_targets_marks_range_and_legality(mocker):
    mocker.patch("jump_range.calculate_ly_distance",
                 side_effect=lambda o, t: {10: 3.0, 11: 9.0, 12: None}[t])
    mocker.patch("jump_range.system_coords.is_legal_jump_destination",
                 side_effect=lambda sid: sid != 11)
    checker = jump_range.JumpRangeChecker(ship_type="Dreadnought", jdc_level=5)  # 7 ly

    rows = checker.range_to_targets(1, [10, 11, 12])
    by_id = {r["system_id"]: r for r in rows}
    assert by_id[10]["in_range"] is True and by_id[10]["legal_destination"] is True
    assert by_id[11]["in_range"] is False and by_id[11]["legal_destination"] is False
    assert by_id[12]["distance_ly"] is None and by_id[12]["in_range"] is False
