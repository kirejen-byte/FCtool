import intel_monitor
from intel_monitor import classify_standing, load_standings, load_standings_whitelist


class FakeAuth:
    def __init__(self, char_id, responses):
        self._character_id = char_id
        self._responses = responses

    def esi_get(self, path):
        return self._responses.get(path)


def _reset():
    intel_monitor._standings_whitelist = set()
    intel_monitor._standings_hostile = set()
    intel_monitor._standings_loaded = False


def test_classify_standing_buckets():
    friendly = {100, 200}
    hostile = {300}
    assert classify_standing({"character_id": 100}, friendly, hostile) == "friendly"
    assert classify_standing({"character_id": 999, "corporation_id": 200},
                             friendly, hostile) == "friendly"
    assert classify_standing({"character_id": 300}, friendly, hostile) == "hostile"
    assert classify_standing({"character_id": 999, "alliance_id": 300},
                             friendly, hostile) == "hostile"
    # resolved (has character_id) but in neither bucket -> neutral
    assert classify_standing({"character_id": 555}, friendly, hostile) == "neutral"
    # unresolved (no character_id) -> unknown
    assert classify_standing({}, friendly, hostile) == "unknown"
    assert classify_standing({"character_id": None}, friendly, hostile) == "unknown"


def test_load_standings_splits_friendly_and_hostile():
    _reset()
    auth = FakeAuth(1, {
        "/characters/1/contacts/": [
            {"contact_id": 100, "standing": 5.0},
            {"contact_id": 300, "standing": -5.0},
            {"contact_id": 400, "standing": 0.0},  # neutral -> neither set
        ],
        "/characters/1/": {"corporation_id": 10},
        "/corporations/10/contacts/": [
            {"contact_id": 101, "standing": 2.5},
            {"contact_id": 301, "standing": -2.5},
        ],
        "/corporations/10/": {"alliance_id": 20},
        "/alliances/20/contacts/": [
            {"contact_id": 302, "standing": -10.0},
        ],
    })
    friendly, hostile = load_standings(auth)
    assert 100 in friendly and 101 in friendly
    assert 20 in friendly  # own alliance implicitly friendly
    assert 300 in hostile and 301 in hostile and 302 in hostile
    assert 400 not in friendly and 400 not in hostile


def test_load_standings_whitelist_still_returns_friendly_only():
    _reset()
    auth = FakeAuth(1, {
        "/characters/1/contacts/": [
            {"contact_id": 100, "standing": 5.0},
            {"contact_id": 300, "standing": -5.0},
        ],
        "/characters/1/": {},
    })
    wl = load_standings_whitelist(auth)
    assert wl == {100}
    assert 300 not in wl
