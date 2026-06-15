"""Unit tests for ``intel_filter`` — the Tk-free, network-free criteria filter.

These exercise the pure matching helpers in isolation: party expansion,
location/parties sub-checks, the AND/OR combination matrix, malformed-input
tolerance, and the default coalition seed. No Tk, no network, no file IO. Repo
root is placed on sys.path by tests/conftest.py.
"""

import pytest

from intel_filter import (
    expand_parties,
    location_matches,
    parties_match,
    matches,
    build_default_coalitions,
    DEFAULT_COALITIONS_SEED,
)


# Triumvirate.'s real alliance id (used only as a sample value for the
# migration-time injection path; the seed itself never contains it).
TRIUMVIRATE_ID = 933731581
INITIATIVE_ID = 1900696668


def _sample_coalitions():
    """A small, explicit coalition map for expansion tests."""
    return {
        "Imperium": {
            "alliances": [
                {"id": 1354830081, "name": "Goonswarm Federation"},
                {"id": 99003214, "name": "Brave Collective"},
            ],
            "corporations": [
                {"id": 98000001, "name": "Some Goon Corp"},
            ],
        },
        "Winter Coalition": {
            "alliances": [{"id": 99003581, "name": "Fraternity."}],
            "corporations": [],
        },
    }


# ── expand_parties ─────────────────────────────────────────────────────────

def test_expand_direct_ids_only():
    parties = {
        "alliances": [{"id": 111, "name": "A"}, {"id": 222, "name": "B"}],
        "corporations": [{"id": 333, "name": "C"}],
        "coalitions": [],
    }
    a, c = expand_parties(parties, {})
    assert a == {111, 222}
    assert c == {333}


def test_expand_coalition_expansion_merges_alliances_and_corps():
    parties = {
        "alliances": [{"id": 111, "name": "A"}],
        "corporations": [],
        "coalitions": ["Imperium"],
    }
    a, c = expand_parties(parties, _sample_coalitions())
    # 111 direct + Imperium's two alliances.
    assert a == {111, 1354830081, 99003214}
    # Imperium contributes a corp id even though parties listed no corps.
    assert c == {98000001}


def test_expand_multiple_coalitions_union():
    parties = {"coalitions": ["Imperium", "Winter Coalition"]}
    a, c = expand_parties(parties, _sample_coalitions())
    assert a == {1354830081, 99003214, 99003581}
    assert c == {98000001}


def test_expand_unknown_coalition_is_skipped():
    parties = {
        "alliances": [{"id": 111, "name": "A"}],
        "coalitions": ["Imperium", "Nonexistent Bloc"],
    }
    a, c = expand_parties(parties, _sample_coalitions())
    # Only Imperium expands; the unknown name contributes nothing.
    assert a == {111, 1354830081, 99003214}
    assert c == {98000001}


def test_expand_malformed_items_are_skipped():
    parties = {
        "alliances": [
            {"id": 111, "name": "ok"},
            {"name": "no id"},          # missing id -> skip
            {"id": None},               # None id -> skip
            {"id": "not-an-int"},       # unparseable -> skip
            {"id": True},               # bool -> skip (not a real id)
            "just a string",            # non-dict -> skip
            42,                          # non-dict -> skip
        ],
        "corporations": [{"id": "777"}],  # numeric string -> coerced to 777
    }
    a, c = expand_parties(parties, {})
    assert a == {111}
    assert c == {777}


def test_expand_missing_keys_and_non_dict_inputs():
    # Missing list keys.
    assert expand_parties({}, {}) == (set(), set())
    # parties not a dict.
    assert expand_parties(None, {}) == (set(), set())
    assert expand_parties("nope", {}) == (set(), set())
    # coalitions not a dict but a coalition name is requested -> no crash.
    assert expand_parties({"coalitions": ["Imperium"]}, None) == (set(), set())


# ── location_matches ───────────────────────────────────────────────────────

def test_location_anywhere_true():
    loc = {"anywhere": True, "systems": [], "regions": []}
    assert location_matches(30000142, 10000002, loc) is True
    # anywhere wins even when ids would not otherwise match.
    assert location_matches(999, 888, loc) is True


def test_location_system_hit():
    loc = {
        "anywhere": False,
        "systems": [{"id": 30000142, "name": "Jita"}],
        "regions": [],
    }
    assert location_matches(30000142, 10000002, loc) is True


def test_location_region_hit():
    loc = {
        "anywhere": False,
        "systems": [],
        "regions": [{"id": 10000002, "name": "The Forge"}],
    }
    # System does not match, but region does.
    assert location_matches(30000999, 10000002, loc) is True


def test_location_anywhere_false_empty_lists_is_true():
    # No constraint configured -> never silently drop everything.
    loc = {"anywhere": False, "systems": [], "regions": []}
    assert location_matches(30000142, 10000002, loc) is True
    assert location_matches(None, None, loc) is True


def test_location_no_match_returns_false():
    loc = {
        "anywhere": False,
        "systems": [{"id": 30000142, "name": "Jita"}],
        "regions": [{"id": 10000002, "name": "The Forge"}],
    }
    assert location_matches(30000999, 10000999, loc) is False


def test_location_none_ids_with_constraint_does_not_match():
    loc = {"anywhere": False, "systems": [{"id": 30000142}], "regions": []}
    assert location_matches(None, None, loc) is False


def test_location_non_dict_is_no_constraint():
    assert location_matches(1, 2, None) is True
    assert location_matches(1, 2, "garbage") is True


# ── parties_match ──────────────────────────────────────────────────────────

def test_parties_anyone_true():
    parties = {"anyone": True, "alliances": [], "corporations": [], "coalitions": []}
    assert parties_match([], [], parties, {}) is True
    # anyone wins even with non-matching involved parties.
    assert parties_match([555], [666], parties, {}) is True


def test_parties_alliance_hit():
    parties = {
        "anyone": False,
        "alliances": [{"id": 111, "name": "A"}],
        "corporations": [],
        "coalitions": [],
    }
    assert parties_match([111, 999], [], parties, {}) is True


def test_parties_corp_hit():
    parties = {
        "anyone": False,
        "alliances": [],
        "corporations": [{"id": 333, "name": "C"}],
        "coalitions": [],
    }
    assert parties_match([], [333], parties, {}) is True


def test_parties_coalition_expansion_hit():
    parties = {"anyone": False, "coalitions": ["Imperium"]}
    # Involved alliance is one of Imperium's members.
    assert parties_match([99003214], [], parties, _sample_coalitions()) is True
    # Involved corp matches Imperium's seeded corp id.
    assert parties_match([], [98000001], parties, _sample_coalitions()) is True


def test_parties_anyone_false_empty_expansion_is_true():
    # anyone False but nothing selected -> no constraint -> True.
    parties = {"anyone": False, "alliances": [], "corporations": [], "coalitions": []}
    assert parties_match([555], [666], parties, {}) is True
    assert parties_match([], [], parties, {}) is True


def test_parties_no_match_returns_false():
    parties = {
        "anyone": False,
        "alliances": [{"id": 111, "name": "A"}],
        "corporations": [{"id": 333, "name": "C"}],
        "coalitions": [],
    }
    assert parties_match([999], [888], parties, {}) is False


def test_parties_accepts_list_or_set():
    parties = {"anyone": False, "alliances": [{"id": 111}]}
    assert parties_match({111, 222}, set(), parties, {}) is True
    assert parties_match([111, 222], [], parties, {}) is True


def test_parties_non_dict_is_no_constraint():
    assert parties_match([1], [2], None, {}) is True
    assert parties_match([1], [2], "garbage", {}) is True


# ── matches() AND/OR matrix ────────────────────────────────────────────────
#
# Construct a filter where location matches ONLY system 100 and parties match
# ONLY alliance 200. Then drive all four (loc, par) truth combinations and
# assert AND vs OR results.

def _matrix_filter(combine):
    return {
        "combine": combine,
        "location": {
            "anywhere": False,
            "systems": [{"id": 100, "name": "S"}],
            "regions": [],
        },
        "parties": {
            "anyone": False,
            "alliances": [{"id": 200, "name": "A"}],
            "corporations": [],
            "coalitions": [],
        },
    }


@pytest.mark.parametrize(
    "system_id, alliance, loc_expected, par_expected",
    [
        (100, 200, True, True),    # both match
        (100, 999, True, False),   # location only
        (999, 200, False, True),   # parties only
        (999, 999, False, False),  # neither
    ],
)
def test_matches_and_matrix(system_id, alliance, loc_expected, par_expected):
    f = _matrix_filter("AND")
    expected = loc_expected and par_expected
    assert matches(system_id, None, [alliance], [], f, {}) is expected


@pytest.mark.parametrize(
    "system_id, alliance, loc_expected, par_expected",
    [
        (100, 200, True, True),    # both match
        (100, 999, True, False),   # location only
        (999, 200, False, True),   # parties only
        (999, 999, False, False),  # neither
    ],
)
def test_matches_or_matrix(system_id, alliance, loc_expected, par_expected):
    f = _matrix_filter("OR")
    expected = loc_expected or par_expected
    assert matches(system_id, None, [alliance], [], f, {}) is expected


def test_matches_defaults_to_and_when_combine_missing():
    f = _matrix_filter("AND")
    f.pop("combine")  # no combine key -> AND default
    # location-only: AND should be False.
    assert matches(100, None, [999], [], f, {}) is False
    # both: AND True.
    assert matches(100, None, [200], [], f, {}) is True


def test_matches_unknown_combine_treated_as_and():
    f = _matrix_filter("xor-nonsense")
    # Treated as AND: parties-only should fail.
    assert matches(999, None, [200], [], f, {}) is False
    assert matches(100, None, [200], [], f, {}) is True


def test_matches_combine_is_case_sensitive_or():
    # Only exact "OR" triggers OR; lowercase "or" falls back to AND.
    f = _matrix_filter("or")
    # location-only under AND -> False.
    assert matches(100, None, [999], [], f, {}) is False


def test_matches_empty_filter_passes_everything():
    # No constraints at all -> both sub-checks True -> AND True.
    assert matches(30000142, 10000002, [1], [2], {}, {}) is True


def test_matches_region_path_via_or():
    f = {
        "combine": "OR",
        "location": {"anywhere": False, "systems": [], "regions": [{"id": 10000002}]},
        "parties": {"anyone": False, "alliances": [{"id": 200}], "coalitions": []},
    }
    # Region matches even though parties don't (OR).
    assert matches(99999, 10000002, [999], [], f, {}) is True


# ── malformed / None inputs must not raise ─────────────────────────────────

def test_matches_accepts_none_ids_and_none_involved():
    f = _matrix_filter("AND")
    # Should not raise; None ids simply don't match the constrained filter.
    assert matches(None, None, None, None, f, {}) is False


def test_matches_non_dict_filter_does_not_raise():
    assert matches(1, 2, [3], [4], None, {}) is True
    assert matches(1, 2, [3], [4], "garbage", None) is True


def test_matches_non_dict_coalitions_does_not_raise():
    parties = {"anyone": False, "coalitions": ["Imperium"]}
    f = {"combine": "AND", "location": {"anywhere": True}, "parties": parties}
    # coalitions is None: expansion yields nothing -> parties is "no
    # constraint" -> True; location anywhere -> True; AND -> True.
    assert matches(1, 2, [1], [2], f, None) is True


def test_matches_involved_as_sets():
    f = _matrix_filter("AND")
    assert matches(100, None, {200}, set(), f, {}) is True


def test_submatchers_do_not_raise_on_garbage():
    # Sanity: each public sub-helper tolerates junk without raising.
    assert expand_parties(12345, [1, 2, 3]) == (set(), set())
    assert location_matches("x", "y", 999) is True
    assert parties_match(object(), object(), {"anyone": False}, {}) is True


@pytest.mark.parametrize("inf", [float("inf"), float("-inf")])
def test_non_finite_ids_are_ignored_without_raising(inf):
    # json.loads accepts a bare Infinity/-Infinity literal, so a hand-edited
    # config.json can carry a non-finite id. int(float('inf')) raises
    # OverflowError, which must be swallowed by the int-coercion helpers rather
    # than propagating and breaking the "never raises on malformed input"
    # contract. The non-finite id must simply be ignored (match nothing).

    # _collect_ids path (party alliance list with an infinite id).
    parties = {
        "anyone": False,
        "alliances": [{"id": inf}, {"id": 200, "name": "ok"}],
        "corporations": [],
        "coalitions": [],
    }
    a, c = expand_parties(parties, {})
    assert a == {200}  # inf dropped, finite id kept
    assert c == set()

    # _as_int_set path (involved-ids list with an infinite id). Constrain on
    # alliance 200 only; an involved list of [inf] must not match and must not
    # raise.
    assert parties_match([inf], [inf], parties, {}) is False
    # A finite hit still works even when an infinite id is alongside it.
    assert parties_match([inf, 200], [], parties, {}) is True

    # location_matches feeds its configured systems/regions through
    # _collect_ids; an infinite configured id must be ignored. With only an
    # infinite system id and no other constraint, the section collapses to "no
    # constraint" -> True; pairing it with a finite system id keeps matching
    # deterministic.
    loc = {
        "anywhere": False,
        "systems": [{"id": inf}, {"id": 30000142, "name": "Jita"}],
        "regions": [],
    }
    assert location_matches(30000142, None, loc) is True   # finite id matches
    assert location_matches(30000999, None, loc) is False  # inf ignored, no match

    # Full matches() pipeline must not raise with non-finite ids on both the
    # config side and the involved side.
    f = {
        "combine": "AND",
        "location": loc,
        "parties": parties,
    }
    assert matches(30000142, None, [200], [], f, {}) is True
    assert matches(30000999, None, [inf], [inf], f, {}) is False


# ── build_default_coalitions / seed integrity ──────────────────────────────

def test_seed_has_expected_coalitions_and_counts():
    assert set(DEFAULT_COALITIONS_SEED.keys()) == {
        "Imperium",
        "Winter Coalition",
        "The Initiative.",
    }
    assert len(DEFAULT_COALITIONS_SEED["Imperium"]["alliances"]) == 11
    assert len(DEFAULT_COALITIONS_SEED["Winter Coalition"]["alliances"]) == 11
    assert len(DEFAULT_COALITIONS_SEED["The Initiative."]["alliances"]) == 1
    # Corporations are empty for every seeded coalition.
    for entry in DEFAULT_COALITIONS_SEED.values():
        assert entry["corporations"] == []


def test_seed_imperium_excludes_initiative():
    imp_ids = {a["id"] for a in DEFAULT_COALITIONS_SEED["Imperium"]["alliances"]}
    assert INITIATIVE_ID not in imp_ids


def test_seed_initiative_includes_initiative_id():
    init_ids = {a["id"] for a in DEFAULT_COALITIONS_SEED["The Initiative."]["alliances"]}
    assert init_ids == {INITIATIVE_ID}


def test_build_without_triumvirate_matches_seed_membership():
    built = build_default_coalitions()
    init_ids = {a["id"] for a in built["The Initiative."]["alliances"]}
    assert init_ids == {INITIATIVE_ID}
    # Imperium still excludes Initiative.
    imp_ids = {a["id"] for a in built["Imperium"]["alliances"]}
    assert INITIATIVE_ID not in imp_ids


def test_build_with_triumvirate_appends_entry():
    built = build_default_coalitions(triumvirate_id=TRIUMVIRATE_ID)
    alliances = built["The Initiative."]["alliances"]
    ids = {a["id"] for a in alliances}
    assert ids == {INITIATIVE_ID, TRIUMVIRATE_ID}
    # Default name applied.
    tri = next(a for a in alliances if a["id"] == TRIUMVIRATE_ID)
    assert tri["name"] == "Triumvirate."


def test_build_with_triumvirate_custom_name():
    built = build_default_coalitions(triumvirate_id=42, triumvirate_name="Tri")
    tri = next(
        a for a in built["The Initiative."]["alliances"] if a["id"] == 42
    )
    assert tri["name"] == "Tri"


def test_build_returns_deep_copy_seed_not_mutated():
    before_len = len(DEFAULT_COALITIONS_SEED["The Initiative."]["alliances"])
    built = build_default_coalitions(triumvirate_id=TRIUMVIRATE_ID)
    # Mutating the built copy must not touch the seed.
    built["The Initiative."]["alliances"].append({"id": 1, "name": "x"})
    built["Imperium"]["alliances"].clear()
    assert len(DEFAULT_COALITIONS_SEED["The Initiative."]["alliances"]) == before_len
    assert len(DEFAULT_COALITIONS_SEED["Imperium"]["alliances"]) == 11


def test_build_deep_copy_independent_across_calls():
    a = build_default_coalitions()
    b = build_default_coalitions()
    a["Imperium"]["alliances"][0]["name"] = "MUTATED"
    assert b["Imperium"]["alliances"][0]["name"] != "MUTATED"


def test_expanded_seed_works_with_parties_match():
    # End-to-end: a real seeded coalition name expands and matches an involved
    # alliance id via parties_match.
    coalitions = build_default_coalitions()
    parties = {"anyone": False, "coalitions": ["Imperium"]}
    # Goonswarm Federation is in Imperium.
    assert parties_match([1354830081], [], parties, coalitions) is True
    # A Winter Coalition alliance is NOT in Imperium.
    assert parties_match([99003581], [], parties, coalitions) is False
