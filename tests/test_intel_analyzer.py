from intel_paste import LocalScan
from intel_analyzer import LocalScanResult, analyze_local_scan


class _FakeAuth:
    """Stand-in for ESIAuth — overrides batch helpers only."""

    def __init__(self, name_to_id, affiliations):
        self._name_to_id = name_to_id
        self._affiliations = affiliations

    def resolve_names_to_ids(self, names):
        return {n: self._name_to_id[n] for n in names if n in self._name_to_id}

    def get_affiliations(self, ids):
        return [self._affiliations[i] for i in ids if i in self._affiliations]


def test_analyze_local_scan_classifies_pilots():
    scan = LocalScan(pilot_names=["Alice", "Bob", "Carol"])
    auth = _FakeAuth(
        name_to_id={"Alice": 1, "Bob": 2, "Carol": 3},
        affiliations={
            1: {"character_id": 1, "corporation_id": 100, "alliance_id": 200},
            2: {"character_id": 2, "corporation_id": 101, "alliance_id": 201},
            3: {"character_id": 3, "corporation_id": 102, "alliance_id": None},
        },
    )
    result = analyze_local_scan(
        scan, auth=auth,
        friendly_ids={200, 101},  # Alice's alliance, Bob's corp
        own_character_ids=set(),
    )
    assert isinstance(result, LocalScanResult)
    assert result.friendly_count == 2
    assert result.hostile_count == 1
    assert result.unresolved_names == []
    assert result.total == 3


def test_analyze_local_scan_buckets_unresolved():
    scan = LocalScan(pilot_names=["Alice", "GhostName"])
    auth = _FakeAuth(
        name_to_id={"Alice": 1},
        affiliations={1: {"character_id": 1, "corporation_id": 100, "alliance_id": 200}},
    )
    result = analyze_local_scan(scan, auth=auth, friendly_ids=set(), own_character_ids=set())
    assert result.unresolved_names == ["GhostName"]
    assert result.hostile_count == 1
    assert result.total == 2


def test_analyze_local_scan_own_chars_count_friendly():
    scan = LocalScan(pilot_names=["Me"])
    auth = _FakeAuth(
        name_to_id={"Me": 42},
        affiliations={42: {"character_id": 42, "corporation_id": 1, "alliance_id": 2}},
    )
    result = analyze_local_scan(
        scan, auth=auth,
        friendly_ids=set(),
        own_character_ids={42},
    )
    assert result.friendly_count == 1
    assert result.hostile_count == 0


def test_analyze_local_scan_top_hostile_affiliations():
    scan = LocalScan(pilot_names=["A", "B", "C", "D"])
    auth = _FakeAuth(
        name_to_id={"A": 1, "B": 2, "C": 3, "D": 4},
        affiliations={
            1: {"character_id": 1, "corporation_id": 10, "alliance_id": 100},
            2: {"character_id": 2, "corporation_id": 11, "alliance_id": 100},
            3: {"character_id": 3, "corporation_id": 12, "alliance_id": 101},
            4: {"character_id": 4, "corporation_id": 13, "alliance_id": None},
        },
    )
    result = analyze_local_scan(scan, auth=auth, friendly_ids=set(), own_character_ids=set())
    counts = dict(result.top_hostile_alliances)
    assert counts[100] == 2
    assert counts[101] == 1
