"""
Tests for cyno_check.CynoChecker — fully offline.

ALL HTTP is mocked: tests inject a FakeFetcher (mimicking the _HttpFetcher
public surface) into CynoChecker, and an autouse fixture patches
requests.get/requests.post/requests.Session so any accidental real network call
fails the test loudly. ship_classes.get_group_id (the only ESI-touching call in
the qualify path) is monkeypatched to a static hull->group map.

Covered: loss pagination + 6-month cutoff; cyno detection positives (cyno in a
high slot on a Force Recon) and negatives (combat Falcon w/o cyno; cyno only in
cargo / a low slot; non-cyno hull); hull-class mapping; latest-loss selection;
battle-inferred association (enemy-set seeding from the loss attackers; both
friendly-inference branches read from the related endpoint's INLINE camelCase
team-summary data; own-corp/alliance + NPC exclusion; MAX_BATTLES bound;
hour-floored timestamp + previous-hour fallback; related-battle cache; fallback
to the stats aggregate when battles yield nothing; graceful partial on a
related-endpoint error; an END-TO-END run over a real captured zKill response);
stats-aggregate association parsing (topLists primary + topAllTime fallback) with
self-exclusion; cache round-trip + is_stale; graceful handling of network errors
and missing fields.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

import ship_classes
import cyno_check
from cyno_check import (
    CynoChecker,
    CynoCache,
    victim_has_high_slot_cyno,
    six_month_cutoff,
    _parse_km_time,
)

# ── Constants used across tests ─────────────────────────────────────────────
RAPIER = 11969        # Force Recon (group 833)
FALCON = 11957        # Force Recon (group 833)
PHOBOS = 12021        # HIC (group 894)
TENGU = 29984         # Strategic Cruiser (group 963)
PURIFIER = 17740      # Stealth Bomber (group 834)
BUZZARD = 11192       # Covert Ops (group 830)
RIFTER = 587          # Frigate (group 25) — NOT a cyno hull

NORMAL_CYNO = 21096
COVERT_CYNO = 28646
SOME_MODULE = 3520    # arbitrary non-cyno module

# Hull type_id -> group_id map for the qualify path (mocks ship_classes ESI).
_GROUP_MAP = {
    RAPIER: 833, FALCON: 833,
    PHOBOS: 894,
    TENGU: 963,
    PURIFIER: 834,
    BUZZARD: 830,
    RIFTER: 25,
}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Guard the wire: any real requests.* call fails the test."""
    def _boom(*a, **k):
        raise AssertionError("network call attempted in a unit test")

    monkeypatch.setattr(cyno_check.requests, "get", _boom)
    monkeypatch.setattr(cyno_check.requests, "post", _boom)
    monkeypatch.setattr(cyno_check.requests, "Session", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("requests.Session constructed in a unit test")))


@pytest.fixture(autouse=True)
def _static_groups(monkeypatch):
    """Resolve hull type_id -> group via a static map (no ESI)."""
    monkeypatch.setattr(ship_classes, "get_group_id",
                        lambda tid: _GROUP_MAP.get(tid))
    # cyno_check imported cyno_loss_hull_class by name; it calls ship_classes.get_group_id
    # internally, so patching the module attribute above is sufficient.


# ── Fake HTTP fetcher ───────────────────────────────────────────────────────

class FakeFetcher:
    """Mimics cyno_check._HttpFetcher's public surface with canned data.

    losses_by_group: {group_id: [page1_entries, page2_entries, ...]}
        each entry = {"killmail_id": int, "zkb": {"hash": str}}
        A page value of None simulates a fetch failure for that page.
    killmails: {killmail_id: <esi killmail dict>}   (None => fetch failure)
    stats: dict | None
    affiliation: dict | None  (the character's own corp/alliance)
    names: {id: name}
    related: {(system_id, "YYYYMMDDHH00"): <team-summary dict>}
        Keyed by the HOUR-FLOORED battle timestamp (zKill's related endpoint
        rejects non-hour-aligned values). The value is the modern team-summary
        response ({"summary": {"teamA": {...}, "teamB": {...}}}); a value of None
        simulates a related-endpoint fetch failure for that key.
    """

    def __init__(self, losses_by_group=None, killmails=None, stats=None,
                 affiliation=None, names=None, related=None):
        self.losses_by_group = losses_by_group or {}
        self.killmails = killmails or {}
        self.stats = stats
        self.affiliation = affiliation
        self.names = names or {}
        self.related = related or {}
        self.zkill_blocked = False
        self.last_status = 200
        # Call logs for assertions
        self.loss_calls = []
        self.km_calls = []
        self.related_calls = []

    def zkill_losses_page(self, character_id, group_id, page):
        self.loss_calls.append((character_id, group_id, page))
        pages = self.losses_by_group.get(group_id, [])
        if page - 1 < len(pages):
            return pages[page - 1]
        return []  # no more pages

    def zkill_stats(self, character_id):
        return self.stats

    def zkill_related(self, solar_system_id, ts_hour):
        self.related_calls.append((solar_system_id, ts_hour))
        return self.related.get((solar_system_id, ts_hour))

    def esi_killmail(self, killmail_id, killmail_hash):
        self.km_calls.append((killmail_id, killmail_hash))
        return self.killmails.get(killmail_id)

    def esi_affiliation(self, character_id):
        return self.affiliation

    def esi_names(self, ids):
        return [{"id": i, "name": self.names[i], "category": "x"}
                for i in ids if i in self.names]


def _km(ship_type_id, items=None, when=None, system_id=30000142, attackers=None):
    """Build a minimal ESI killmail dict (optionally with attackers[])."""
    if when is None:
        when = datetime.now(timezone.utc)
    km = {
        "killmail_time": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "solar_system_id": system_id,
        "victim": {"ship_type_id": ship_type_id, "items": items or []},
    }
    if attackers is not None:
        km["attackers"] = attackers
    return km


def _party(character_id=1, corporation_id=None, alliance_id=None,
           faction_id=None):
    """Build an attacker/victim party dict.

    Omitting ``character_id`` (passing None) marks the party as an NPC/structure
    so cyno_check excludes it from the friendly/enemy tallies.
    """
    p = {}
    if character_id is not None:
        p["character_id"] = character_id
    if corporation_id is not None:
        p["corporation_id"] = corporation_id
    if alliance_id is not None:
        p["alliance_id"] = alliance_id
    if faction_id is not None:
        p["faction_id"] = faction_id
    return p


def _inline(character_id=1, corporation_id=None, alliance_id=None,
            faction_id=None, corporation_name=None, alliance_name=None):
    """Build an INLINE related-battle party dict (camelCase, as zKill returns).

    These are the parties carried inline on each related kill's ``victim`` /
    ``involved`` — DISTINCT from the snake_case ESI ``_party`` used on the loss
    killmail. Omitting ``character_id`` (None) marks the party as an NPC/structure
    so cyno_check excludes it from the friendly/enemy tallies.
    """
    p = {}
    if character_id is not None:
        p["characterID"] = character_id
    if corporation_id is not None:
        p["corporationID"] = corporation_id
    if alliance_id is not None:
        p["allianceID"] = alliance_id
    if faction_id is not None:
        p["factionID"] = faction_id
    if corporation_name is not None:
        p["corporationName"] = corporation_name
    if alliance_name is not None:
        p["allianceName"] = alliance_name
    return p


def _battle_kill(kill_id, victim_party, attacker_parties, ship_type_id=RIFTER):
    """Build ONE related-battle kill object: the victim plus an ``involved`` list
    that includes EVERY participant (the victim flagged ``isVictim`` + attackers),
    matching the real zKill team-summary shape."""
    victim = dict(victim_party)
    victim["shipTypeID"] = ship_type_id
    victim["isVictim"] = True
    involved = [dict(victim)]
    for a in attacker_parties:
        ap = dict(a)
        ap["isVictim"] = False
        involved.append(ap)
    return {
        "killID": kill_id,
        "victim": victim,
        "involved": involved,
    }


def _battle(*kills, team="teamA"):
    """Wrap kill objects into a zKill 'related' team-summary response.

    ``{"summary": {"<team>": {"kills": {"<killID>": <kill>, ...}}}}``.
    """
    kills_dict = {str(k.get("killID", i)): k for i, k in enumerate(kills)}
    return {"summary": {team: {"kills": kills_dict}}}


def _loss_stub(killmail_id, hash="h"):
    """A zKill character-LOSSES list entry (still {killmail_id, zkb:{hash}})."""
    return {"killmail_id": killmail_id, "zkb": {"hash": hash}}


def _hi(type_id, slot=27):
    return {"item_type_id": type_id, "flag": slot, "quantity_dropped": 1}


def _cargo(type_id):
    return {"item_type_id": type_id, "flag": 5}  # Cargo


def _low(type_id):
    return {"item_type_id": type_id, "flag": 11}  # LoSlot0


def _checker(fetcher, tmp_path):
    cache = CynoCache(path=str(tmp_path / "cyno_cache.json"))
    return CynoChecker(fetcher=fetcher, cache=cache)


# ════════════════════════════════════════════════════════════════════════════
# victim_has_high_slot_cyno — detection unit
# ════════════════════════════════════════════════════════════════════════════

def test_detect_cyno_in_high_slot():
    assert victim_has_high_slot_cyno([_hi(NORMAL_CYNO, 27)]) is True
    assert victim_has_high_slot_cyno([_hi(COVERT_CYNO, 34)]) is True


def test_detect_cyno_in_cargo_is_false():
    assert victim_has_high_slot_cyno([_cargo(NORMAL_CYNO)]) is False


def test_detect_cyno_in_low_slot_is_false():
    assert victim_has_high_slot_cyno([_low(COVERT_CYNO)]) is False


def test_detect_no_cyno_is_false():
    assert victim_has_high_slot_cyno([_hi(SOME_MODULE, 27), _low(SOME_MODULE)]) is False


def test_detect_handles_empty_and_none():
    assert victim_has_high_slot_cyno([]) is False
    assert victim_has_high_slot_cyno(None) is False


def test_detect_cyno_in_nested_container():
    """A cyno fitted in a high slot inside a nested container still counts."""
    items = [{
        "item_type_id": 11489,  # a container-ish module
        "flag": 27,
        "items": [_hi(COVERT_CYNO, 28)],
    }]
    assert victim_has_high_slot_cyno(items) is True


def test_detect_bool_flag_not_treated_as_int():
    # flag True == 1 in Python; must NOT be read as a high slot.
    assert victim_has_high_slot_cyno([{"item_type_id": NORMAL_CYNO, "flag": True}]) is False


def test_detect_string_high_slot_flag():
    assert victim_has_high_slot_cyno(
        [{"item_type_id": NORMAL_CYNO, "flag": "HiSlot3"}]) is True


# ════════════════════════════════════════════════════════════════════════════
# Qualify + aggregate via analyze_character
# ════════════════════════════════════════════════════════════════════════════

def test_qualifying_force_recon_with_high_slot_cyno(tmp_path):
    f = FakeFetcher(
        losses_by_group={833: [[{"killmail_id": 1, "zkb": {"hash": "h1"}}]]},
        killmails={1: _km(RAPIER, items=[_hi(NORMAL_CYNO, 27)])},
        stats=None,
    )
    res = _checker(f, tmp_path).analyze_character(100)
    assert res["total"] == 1
    assert res["breakdown"] == {"Force Recon": 1}
    assert res["latest"]["killmail_id"] == 1
    assert res["latest"]["url"] == "https://zkillboard.com/kill/1/"


def test_combat_falcon_without_cyno_does_not_qualify(tmp_path):
    """A Force Recon hull with NO cyno fitted (combat fit) must not count."""
    f = FakeFetcher(
        losses_by_group={833: [[{"killmail_id": 2, "zkb": {"hash": "h2"}}]]},
        killmails={2: _km(FALCON, items=[_hi(SOME_MODULE, 27), _low(SOME_MODULE)])},
    )
    res = _checker(f, tmp_path).analyze_character(100)
    assert res["total"] == 0
    assert res["breakdown"] == {}
    assert res["latest"] is None


def test_cyno_only_in_cargo_does_not_qualify(tmp_path):
    f = FakeFetcher(
        losses_by_group={833: [[{"killmail_id": 3, "zkb": {"hash": "h3"}}]]},
        killmails={3: _km(RAPIER, items=[_cargo(NORMAL_CYNO)])},
    )
    res = _checker(f, tmp_path).analyze_character(100)
    assert res["total"] == 0


def test_non_cyno_hull_with_cyno_module_does_not_qualify(tmp_path):
    """Even a high-slot cyno on a non-cyno hull (Rifter, group 25) is excluded
    because the hull group isn't one of the five."""
    f = FakeFetcher(
        # zKill would never return a Rifter under groupID 833, but prove the
        # hull-class gate independently by routing it through that group's page.
        losses_by_group={833: [[{"killmail_id": 4, "zkb": {"hash": "h4"}}]]},
        killmails={4: _km(RIFTER, items=[_hi(NORMAL_CYNO, 27)])},
    )
    res = _checker(f, tmp_path).analyze_character(100)
    assert res["total"] == 0


def test_hull_class_mapping_across_groups(tmp_path):
    """One qualifying loss in each of the five groups -> correct labels."""
    now = datetime.now(timezone.utc)
    losses = {
        894: [[{"killmail_id": 10, "zkb": {"hash": "h"}}]],
        833: [[{"killmail_id": 11, "zkb": {"hash": "h"}}]],
        963: [[{"killmail_id": 12, "zkb": {"hash": "h"}}]],
        834: [[{"killmail_id": 13, "zkb": {"hash": "h"}}]],
        830: [[{"killmail_id": 14, "zkb": {"hash": "h"}}]],
    }
    kms = {
        10: _km(PHOBOS, items=[_hi(NORMAL_CYNO)], when=now - timedelta(days=1)),
        11: _km(RAPIER, items=[_hi(NORMAL_CYNO)], when=now - timedelta(days=2)),
        12: _km(TENGU, items=[_hi(COVERT_CYNO)], when=now - timedelta(days=3)),
        13: _km(PURIFIER, items=[_hi(COVERT_CYNO)], when=now - timedelta(days=4)),
        14: _km(BUZZARD, items=[_hi(COVERT_CYNO)], when=now - timedelta(days=5)),
    }
    f = FakeFetcher(losses_by_group=losses, killmails=kms)
    res = _checker(f, tmp_path).analyze_character(100)
    assert res["total"] == 5
    assert res["breakdown"] == {
        "HIC": 1, "Force Recon": 1, "Strategic Cruiser": 1,
        "Stealth Bomber": 1, "Covert Ops": 1,
    }
    # Latest = the most recent (PHOBOS, 1 day ago) = killmail 10.
    assert res["latest"]["killmail_id"] == 10


def test_latest_selection_picks_most_recent_time(tmp_path):
    now = datetime.now(timezone.utc)
    f = FakeFetcher(
        losses_by_group={833: [[
            {"killmail_id": 20, "zkb": {"hash": "h"}},
            {"killmail_id": 21, "zkb": {"hash": "h"}},
        ]]},
        killmails={
            20: _km(RAPIER, items=[_hi(NORMAL_CYNO)], when=now - timedelta(days=10)),
            21: _km(RAPIER, items=[_hi(NORMAL_CYNO)], when=now - timedelta(days=1)),
        },
    )
    res = _checker(f, tmp_path).analyze_character(100)
    assert res["total"] == 2
    assert res["latest"]["killmail_id"] == 21  # newer one wins


# ════════════════════════════════════════════════════════════════════════════
# 6-month cutoff + pagination
# ════════════════════════════════════════════════════════════════════════════

def test_cutoff_excludes_old_and_stops_pagination(tmp_path):
    """Newest-first mails: once we cross the 6-month cutoff we stop, and the
    next page is never requested."""
    now = datetime.now(timezone.utc)
    page1 = [
        {"killmail_id": 30, "zkb": {"hash": "h"}},  # 1 day ago — qualifies
        {"killmail_id": 31, "zkb": {"hash": "h"}},  # 200 days ago — too old, stop
        {"killmail_id": 32, "zkb": {"hash": "h"}},  # would-be qualifier after cutoff
    ]
    page2 = [{"killmail_id": 33, "zkb": {"hash": "h"}}]  # must NOT be fetched
    f = FakeFetcher(
        losses_by_group={833: [page1, page2]},
        killmails={
            30: _km(RAPIER, items=[_hi(NORMAL_CYNO)], when=now - timedelta(days=1)),
            31: _km(RAPIER, items=[_hi(NORMAL_CYNO)], when=now - timedelta(days=200)),
            32: _km(RAPIER, items=[_hi(NORMAL_CYNO)], when=now - timedelta(days=1)),
            33: _km(RAPIER, items=[_hi(NORMAL_CYNO)], when=now - timedelta(days=1)),
        },
    )
    checker = _checker(f, tmp_path)
    res = checker.analyze_character(100)
    assert res["total"] == 1  # only mail 30 counted; 31 stops the scan
    # Page 2 for group 833 was never requested.
    assert (100, 833, 2) not in f.loss_calls
    # Killmail 33 (on page 2) was never fetched.
    assert 33 not in [c[0] for c in f.km_calls]


def test_short_page_stops_without_requesting_next(tmp_path):
    """A page shorter than ZKILL_PAGE_SIZE is treated as the last page."""
    now = datetime.now(timezone.utc)
    f = FakeFetcher(
        losses_by_group={833: [[{"killmail_id": 40, "zkb": {"hash": "h"}}]]},
        killmails={40: _km(RAPIER, items=[_hi(NORMAL_CYNO)], when=now)},
    )
    checker = _checker(f, tmp_path)
    checker.analyze_character(100)
    # Only page 1 of group 833 requested (short page => stop).
    assert (100, 833, 1) in f.loss_calls
    assert (100, 833, 2) not in f.loss_calls


def test_six_month_cutoff_helper():
    fixed = datetime(2026, 6, 15, tzinfo=timezone.utc)
    cut = six_month_cutoff(fixed)
    assert cut == fixed - timedelta(days=182)
    assert cut.tzinfo is not None


# ════════════════════════════════════════════════════════════════════════════
# Association parsing + self-exclusion
# ════════════════════════════════════════════════════════════════════════════

def _stats_topalltime(alliances, corps):
    """Build a stats payload with a populated topAllTime (topLists empty),
    matching the confirmed real-world shape."""
    return {
        "shipsDestroyed": 5000,
        "topLists": [
            {"type": "alliance", "title": "Top Alliances", "values": []},
            {"type": "corporation", "title": "Top Corporations", "values": []},
        ],
        "topAllTime": [
            {"type": "alliance", "data": [
                {"kills": k, "allianceID": a} for a, k in alliances]},
            {"type": "corporation", "data": [
                {"kills": k, "corporationID": c} for c, k in corps]},
        ],
    }


def test_association_from_topalltime_picks_top_alliance(tmp_path):
    # Own alliance = 999 (must be excluded even though it has the most kills).
    stats = _stats_topalltime(
        alliances=[(999, 1000), (1354830081, 800), (131511956, 300)],
        corps=[(427125032, 500)],
    )
    f = FakeFetcher(
        losses_by_group={},
        stats=stats,
        affiliation={"character_id": 100, "corporation_id": 427125032,
                     "alliance_id": 999},
        names={1354830081: "Pandemic Legion"},
    )
    res = _checker(f, tmp_path).analyze_character(100)
    assoc = res["association"]
    assert assoc["kind"] == "alliance"
    assert assoc["id"] == 1354830081           # own alliance 999 excluded
    assert assoc["name"] == "Pandemic Legion"
    assert assoc["count"] == 800
    assert assoc["sample_total"] == 5000


def test_association_falls_back_to_corporation(tmp_path):
    """No alliance data (or only own alliance) -> use the top corporation,
    excluding the character's own corp."""
    stats = {
        "shipsDestroyed": 10,
        "topAllTime": [
            {"type": "alliance", "data": []},
            {"type": "corporation", "data": [
                {"kills": 50, "corporationID": 5555},   # own corp -> excluded
                {"kills": 40, "corporationID": 6666},   # winner
            ]},
        ],
    }
    f = FakeFetcher(
        losses_by_group={},
        stats=stats,
        affiliation={"character_id": 100, "corporation_id": 5555, "alliance_id": None},
        names={6666: "Some Corp"},
    )
    assoc = _checker(f, tmp_path).analyze_character(100)["association"]
    assert assoc["kind"] == "corporation"
    assert assoc["id"] == 6666
    assert assoc["name"] == "Some Corp"
    assert assoc["count"] == 40


def test_association_prefers_populated_toplists(tmp_path):
    """If topLists IS populated, it takes precedence over topAllTime."""
    stats = {
        "shipsDestroyed": 7,
        "topLists": [
            {"type": "alliance", "title": "Top Alliances", "values": [
                {"allianceID": 12345, "kills": 99, "name": "FromTopLists"},
            ]},
        ],
        "topAllTime": [
            {"type": "alliance", "data": [{"kills": 1000, "allianceID": 67890}]},
        ],
    }
    f = FakeFetcher(
        losses_by_group={},
        stats=stats,
        affiliation={"character_id": 100, "corporation_id": 1, "alliance_id": 2},
        names={12345: "FromTopLists"},
    )
    assoc = _checker(f, tmp_path).analyze_character(100)["association"]
    assert assoc["id"] == 12345         # topLists wins over topAllTime
    assert assoc["count"] == 99


def test_association_unknown_when_no_stats(tmp_path):
    f = FakeFetcher(losses_by_group={}, stats=None,
                    affiliation={"corporation_id": 1, "alliance_id": 2})
    assoc = _checker(f, tmp_path).analyze_character(100)["association"]
    assert assoc == {"kind": "unknown"}


def test_association_unknown_when_only_own_alliance(tmp_path):
    """If the only alliance present is the character's own, and there are no
    other corps, degrade to unknown rather than self-associate."""
    stats = {
        "shipsDestroyed": 3,
        "topAllTime": [
            {"type": "alliance", "data": [{"kills": 100, "allianceID": 4242}]},
            {"type": "corporation", "data": [{"kills": 100, "corporationID": 7777}]},
        ],
    }
    f = FakeFetcher(
        losses_by_group={},
        stats=stats,
        affiliation={"corporation_id": 7777, "alliance_id": 4242},  # both are own
        names={},
    )
    assoc = _checker(f, tmp_path).analyze_character(100)["association"]
    assert assoc == {"kind": "unknown"}


def test_association_handles_missing_affiliation(tmp_path):
    """If affiliation lookup fails (None), we can't self-exclude but still pick
    the top alliance without crashing."""
    stats = _stats_topalltime(alliances=[(111, 900)], corps=[(222, 100)])
    f = FakeFetcher(losses_by_group={}, stats=stats, affiliation=None,
                    names={111: "Alliance 111"})
    assoc = _checker(f, tmp_path).analyze_character(100)["association"]
    assert assoc["kind"] == "alliance"
    assert assoc["id"] == 111


# ════════════════════════════════════════════════════════════════════════════
# Battle-inferred association (PRIMARY) + stats fallback
# ════════════════════════════════════════════════════════════════════════════
#
# Convention for these tests:
#   * The pilot (victim) flies a cyno Rapier and dies in system SYS at time T.
#   * The loss killmail (ESI, snake_case) carries attackers[] -> these seed the
#     ENEMY set E.
#   * related[(SYS, T_HOUR)] returns the surrounding battle as the modern zKill
#     team-summary response ({"summary": {"teamA": {"kills": {...}}}}); each kill
#     carries its victim + attackers INLINE in camelCase (with inline names), so
#     there is NO per-killmail ESI fetch and no /universe/names/ call.
#   * We assert the inferred "Flies with" entity is the pilot's blue, never the
#     enemy / the pilot's own corp/alliance / an NPC.

SYS = 30000142
ENEMY_ALLY = 700001          # the alliance that killed the pilot
FRIEND_ALLY = 800001         # the pilot's blue alliance (what we want to infer)
FRIEND_ALLY_2 = 800002
PILOT_ALLY = 900001          # the pilot's OWN alliance (must be excluded)
PILOT_CORP = 900002          # the pilot's OWN corp (must be excluded)


def _km_hour(when):
    """The YYYYMMDDHH00 (hour-floored) related-battle key cyno_check derives from
    a killmail time."""
    floored = when.astimezone(timezone.utc).replace(
        minute=0, second=0, microsecond=0)
    return floored.strftime("%Y%m%d%H00")


def _prev_hour(when):
    """The previous-hour YYYYMMDDHH00 key (the boundary fallback)."""
    floored = when.astimezone(timezone.utc).replace(
        minute=0, second=0, microsecond=0)
    return (floored - timedelta(hours=1)).strftime("%Y%m%d%H00")


def _pilot_loss(when, attackers, hash="lh"):
    """A qualifying cyno-Rapier loss for the pilot, with the given attackers."""
    return _km(RAPIER, items=[_hi(NORMAL_CYNO)], when=when, system_id=SYS,
               attackers=attackers)


def test_enemy_seed_and_friendly_from_killing_an_enemy(tmp_path):
    """Branch A: a battle kill whose VICTIM is in the enemy set means that kill's
    ATTACKERS fought the enemy -> they are the pilot's friends. Read from the
    inline team-summary data (camelCase), no per-killmail fetch."""
    when = datetime(2026, 3, 15, 14, 23, 9, tzinfo=timezone.utc)
    loss = _pilot_loss(when, attackers=[_party(1, alliance_id=ENEMY_ALLY)])
    # Battle: an ENEMY ship dies to the pilot's blue (FRIEND_ALLY).
    battle = _battle(_battle_kill(
        50,
        victim_party=_inline(2, alliance_id=ENEMY_ALLY),
        attacker_parties=[_inline(3, alliance_id=FRIEND_ALLY,
                                  alliance_name="Blue Alliance")],
    ))
    f = FakeFetcher(
        losses_by_group={833: [[_loss_stub(1, "lh")]]},
        killmails={1: loss},
        related={(SYS, _km_hour(when)): battle},
        affiliation={"corporation_id": PILOT_CORP, "alliance_id": PILOT_ALLY},
        stats=None,
    )
    assoc = _checker(f, tmp_path).analyze_character(100)["association"]
    assert assoc["basis"] == "battles"
    assert assoc["kind"] == "alliance"
    assert assoc["id"] == FRIEND_ALLY
    assert assoc["name"] == "Blue Alliance"     # inline allianceName, no ESI
    assert assoc["count"] == 1
    assert assoc["sample_total"] == 1     # one battle scanned
    # The related endpoint was queried with the loss's system + HOUR-FLOORED time.
    assert (SYS, _km_hour(when)) in f.related_calls
    # No /universe/names/ fetch happened (FakeFetcher.names is empty anyway).


def test_friendly_from_being_killed_by_enemy(tmp_path):
    """Branch B: a battle kill whose ATTACKERS include the enemy and whose VICTIM
    is NOT an enemy means the victim was killed BY the enemy -> the victim is the
    pilot's friend."""
    when = datetime(2026, 3, 15, 14, 23, 9, tzinfo=timezone.utc)
    loss = _pilot_loss(when, attackers=[_party(1, alliance_id=ENEMY_ALLY)])
    # Battle: the pilot's blue (FRIEND_ALLY) dies to the enemy.
    battle = _battle(_battle_kill(
        60,
        victim_party=_inline(4, alliance_id=FRIEND_ALLY,
                             alliance_name="Blue Alliance"),
        attacker_parties=[_inline(5, alliance_id=ENEMY_ALLY)],
    ))
    f = FakeFetcher(
        losses_by_group={833: [[_loss_stub(1, "lh")]]},
        killmails={1: loss},
        related={(SYS, _km_hour(when)): battle},
        affiliation={"corporation_id": PILOT_CORP, "alliance_id": PILOT_ALLY},
        stats=None,
    )
    assoc = _checker(f, tmp_path).analyze_character(100)["association"]
    assert assoc["basis"] == "battles"
    assert assoc["id"] == FRIEND_ALLY
    assert assoc["name"] == "Blue Alliance"
    assert assoc["count"] == 1


def test_excludes_own_and_npc_keeps_blue(tmp_path):
    """The pilot's own corp/alliance and NPCs (no characterID) never enter the
    friendly tally; only the genuine blue is reported."""
    when = datetime(2026, 3, 15, 14, 23, 9, tzinfo=timezone.utc)
    loss = _pilot_loss(when, attackers=[
        _party(1, alliance_id=ENEMY_ALLY),
        _party(None, faction_id=500001),       # NPC attacker on the loss — ignored
    ])
    # Enemy ship dies; on the killing side: the pilot's own alliance, an NPC, and
    # the real blue. Only the blue should be tallied.
    battle = _battle(_battle_kill(
        70,
        victim_party=_inline(2, alliance_id=ENEMY_ALLY),
        attacker_parties=[
            _inline(10, alliance_id=PILOT_ALLY),          # own alliance -> excluded
            _inline(11, corporation_id=PILOT_CORP),       # own corp -> excluded
            _inline(None, faction_id=500001),             # NPC -> excluded
            _inline(12, alliance_id=FRIEND_ALLY,
                    alliance_name="Blue Alliance"),        # the blue -> counted
        ],
    ))
    f = FakeFetcher(
        losses_by_group={833: [[_loss_stub(1, "lh")]]},
        killmails={1: loss},
        related={(SYS, _km_hour(when)): battle},
        affiliation={"corporation_id": PILOT_CORP, "alliance_id": PILOT_ALLY},
        stats=None,
    )
    assoc = _checker(f, tmp_path).analyze_character(100)["association"]
    assert assoc["basis"] == "battles"
    assert assoc["id"] == FRIEND_ALLY
    assert assoc["count"] == 1


def test_corp_only_friend_uses_corporation_kind(tmp_path):
    """A blue with no alliance (corp only) is reported as kind 'corporation'."""
    when = datetime(2026, 3, 15, 14, 23, 9, tzinfo=timezone.utc)
    loss = _pilot_loss(when, attackers=[_party(1, alliance_id=ENEMY_ALLY)])
    battle = _battle(_battle_kill(
        80,
        victim_party=_inline(2, alliance_id=ENEMY_ALLY),
        attacker_parties=[_inline(3, corporation_id=808080,
                                  corporation_name="Blue Corp")],   # no alliance
    ))
    f = FakeFetcher(
        losses_by_group={833: [[_loss_stub(1, "lh")]]},
        killmails={1: loss},
        related={(SYS, _km_hour(when)): battle},
        affiliation={"corporation_id": PILOT_CORP, "alliance_id": PILOT_ALLY},
        stats=None,
    )
    assoc = _checker(f, tmp_path).analyze_character(100)["association"]
    assert assoc["basis"] == "battles"
    assert assoc["kind"] == "corporation"
    assert assoc["id"] == 808080
    assert assoc["name"] == "Blue Corp"     # inline corporationName


def test_alliance_preferred_over_corp_when_both_tied(tmp_path):
    """When a corp-only blue and an alliance blue tie on count, the alliance is
    chosen (prefer alliance)."""
    when = datetime(2026, 3, 15, 14, 23, 9, tzinfo=timezone.utc)
    loss = _pilot_loss(when, attackers=[_party(1, alliance_id=ENEMY_ALLY)])
    battle = _battle(_battle_kill(
        90,
        victim_party=_inline(2, alliance_id=ENEMY_ALLY),
        attacker_parties=[
            _inline(3, corporation_id=808080,
                    corporation_name="Blue Corp"),          # corp-only blue (1)
            _inline(4, alliance_id=FRIEND_ALLY,
                    alliance_name="Blue Alliance"),          # alliance blue (1)
        ],
    ))
    f = FakeFetcher(
        losses_by_group={833: [[_loss_stub(1, "lh")]]},
        killmails={1: loss},
        related={(SYS, _km_hour(when)): battle},
        affiliation={"corporation_id": PILOT_CORP, "alliance_id": PILOT_ALLY},
        stats=None,
    )
    assoc = _checker(f, tmp_path).analyze_character(100)["association"]
    assert assoc["kind"] == "alliance"
    assert assoc["id"] == FRIEND_ALLY


def test_max_battles_bound(tmp_path):
    """Only the MAX_BATTLES most-recent losses are turned into battle fetches."""
    base = datetime(2026, 3, 15, 14, 30, 0, tzinfo=timezone.utc)
    n_losses = cyno_check.MAX_BATTLES + 3
    stubs, kms, related = [], {}, {}
    for i in range(n_losses):
        # Each loss in a DISTINCT hour so each maps to its own related key (and
        # the floored hour is non-empty, so the prev-hour fallback never fires).
        when = base - timedelta(hours=i)
        km_id = 1000 + i
        stubs.append(_loss_stub(km_id, f"h{i}"))
        kms[km_id] = _pilot_loss(when, attackers=[_party(1, alliance_id=ENEMY_ALLY)],
                                 hash=f"h{i}")
        # Every battle yields the same blue, so any scanned battle would score.
        related[(SYS, _km_hour(when))] = _battle(_battle_kill(
            2000 + i,
            victim_party=_inline(2, alliance_id=ENEMY_ALLY),
            attacker_parties=[_inline(3, alliance_id=FRIEND_ALLY,
                                      alliance_name="Blue Alliance")],
        ))
    f = FakeFetcher(
        losses_by_group={833: [stubs]},
        killmails=kms,
        related=related,
        affiliation={"corporation_id": PILOT_CORP, "alliance_id": PILOT_ALLY},
        stats=None,
    )
    assoc = _checker(f, tmp_path).analyze_character(100)["association"]
    # At most MAX_BATTLES related fetches, despite more qualifying losses. (Each
    # floored hour is non-empty, so no previous-hour fallback fetch is issued.)
    assert len(f.related_calls) == cyno_check.MAX_BATTLES
    assert assoc["sample_total"] == cyno_check.MAX_BATTLES
    assert assoc["id"] == FRIEND_ALLY


def test_all_battle_kills_scanned_no_cap(tmp_path):
    """A single battle scans ALL of its kills in ONE fetch with NO per-battle cap
    and NO per-killmail ESI re-fetch.

    Build a battle with more kills than the old MAX_KILLS_PER_BATTLE (40) cap, all
    feeding the SAME blue alliance. The whole battle is read from ONE related
    response (no per-killmail ESI re-fetch). Under the PER-BATTLE PRESENCE metric
    the blue's ``count``/``battle_count`` is 1 (present in this one battle),
    regardless of how many kills it appears across — the raw-observation count is
    an internal tiebreak only and is NOT surfaced as ``count`` anymore."""
    when = datetime(2026, 3, 15, 14, 23, 9, tzinfo=timezone.utc)
    loss = _pilot_loss(when, attackers=[_party(1, alliance_id=ENEMY_ALLY)])
    n_kills = 55          # > the former MAX_KILLS_PER_BATTLE (40)
    kills = []
    for i in range(n_kills):
        kills.append(_battle_kill(
            3000 + i,
            victim_party=_inline(2, alliance_id=ENEMY_ALLY),
            attacker_parties=[_inline(3000 + i, alliance_id=FRIEND_ALLY,
                                      alliance_name="Blue Alliance")],
        ))
    battle = _battle(*kills)
    f = FakeFetcher(
        losses_by_group={833: [[_loss_stub(1, "lh")]]},
        killmails={1: loss},
        related={(SYS, _km_hour(when)): battle},
        affiliation={"corporation_id": PILOT_CORP, "alliance_id": PILOT_ALLY},
        stats=None,
    )
    res = _checker(f, tmp_path).analyze_character(100)
    assoc = res["association"]
    # Exactly ONE related fetch (the whole battle is in one response).
    assert len(f.related_calls) == 1
    # Every kill was scanned in that single fetch (no per-killmail ESI re-fetch).
    assert assoc["basis"] == "battles"
    assert assoc["id"] == FRIEND_ALLY
    # PER-BATTLE PRESENCE: one battle present -> count == battle_count == 1,
    # NOT the 55 raw kill-observations (which only break ties internally).
    assert assoc["count"] == 1
    assert assoc["battle_count"] == 1
    assert assoc["sample_total"] == 1
    # No per-killmail ESI fetches in the battle path (only the loss km was fetched).
    assert f.km_calls == [(1, "lh")]


def test_prev_hour_fallback_when_floored_hour_empty(tmp_path):
    """If the floored-hour battle is empty/missing, the scan also queries the
    PREVIOUS hour and uses that battle (boundary robustness)."""
    # A loss right after the top of the hour: the fight may be filed under the
    # previous hour's battle.
    when = datetime(2026, 3, 15, 15, 2, 9, tzinfo=timezone.utc)
    loss = _pilot_loss(when, attackers=[_party(1, alliance_id=ENEMY_ALLY)])
    prev_battle = _battle(_battle_kill(
        50,
        victim_party=_inline(2, alliance_id=ENEMY_ALLY),
        attacker_parties=[_inline(3, alliance_id=FRIEND_ALLY,
                                  alliance_name="Blue Alliance")],
    ))
    f = FakeFetcher(
        losses_by_group={833: [[_loss_stub(1, "lh")]]},
        killmails={1: loss},
        related={
            # Floored hour (1500) returns an EMPTY battle (no kills) ...
            (SYS, _km_hour(when)): _battle(),
            # ... but the previous hour (1400) holds the real fight.
            (SYS, _prev_hour(when)): prev_battle,
        },
        affiliation={"corporation_id": PILOT_CORP, "alliance_id": PILOT_ALLY},
        stats=None,
    )
    assoc = _checker(f, tmp_path).analyze_character(100)["association"]
    assert assoc["basis"] == "battles"
    assert assoc["id"] == FRIEND_ALLY
    assert assoc["name"] == "Blue Alliance"
    # Both hours were queried (floored first, then the previous-hour fallback).
    assert (SYS, _km_hour(when)) in f.related_calls
    assert (SYS, _prev_hour(when)) in f.related_calls


def test_floored_hour_hit_skips_prev_hour(tmp_path):
    """When the floored hour already has a usable battle, the previous hour is
    NOT queried."""
    when = datetime(2026, 3, 15, 15, 47, 9, tzinfo=timezone.utc)
    loss = _pilot_loss(when, attackers=[_party(1, alliance_id=ENEMY_ALLY)])
    battle = _battle(_battle_kill(
        50,
        victim_party=_inline(2, alliance_id=ENEMY_ALLY),
        attacker_parties=[_inline(3, alliance_id=FRIEND_ALLY,
                                  alliance_name="Blue Alliance")],
    ))
    f = FakeFetcher(
        losses_by_group={833: [[_loss_stub(1, "lh")]]},
        killmails={1: loss},
        related={(SYS, _km_hour(when)): battle},   # only the floored hour seeded
        affiliation={"corporation_id": PILOT_CORP, "alliance_id": PILOT_ALLY},
        stats=None,
    )
    res = _checker(f, tmp_path).analyze_character(100)
    assoc = res["association"]
    assert assoc["basis"] == "battles"
    assert assoc["id"] == FRIEND_ALLY
    # The previous-hour key was never requested (floored hour sufficed) ...
    assert (SYS, _prev_hour(when)) not in f.related_calls
    # ... and the run is NOT marked partial (the missing prev hour wasn't fetched).
    assert "partial" not in res["status"]


def test_related_cache_round_trip(tmp_path):
    """CynoCache stores related battles (team-summary dicts) forever and survives
    a reload, keyed by the hour-floored timestamp."""
    path = str(tmp_path / "cyno_cache.json")
    cache = CynoCache(path=path)
    cache.load()
    battle = _battle(_battle_kill(
        111,
        victim_party=_inline(2, alliance_id=ENEMY_ALLY),
        attacker_parties=[_inline(3, alliance_id=FRIEND_ALLY)],
    ))
    cache.put_related(SYS, "202603151400", battle)
    cache.save()

    cache2 = CynoCache(path=path)
    cache2.load()
    assert cache2.get_related(SYS, "202603151400") == battle
    assert cache2.get_related(SYS, "999912312300") is None  # miss -> None


def test_related_cache_avoids_refetch(tmp_path):
    """A cached related battle is reused; the related endpoint isn't re-hit."""
    when = datetime(2026, 3, 15, 14, 23, 9, tzinfo=timezone.utc)
    loss = _pilot_loss(when, attackers=[_party(1, alliance_id=ENEMY_ALLY)])
    battle = _battle(_battle_kill(
        50,
        victim_party=_inline(2, alliance_id=ENEMY_ALLY),
        attacker_parties=[_inline(3, alliance_id=FRIEND_ALLY,
                                  alliance_name="Blue Alliance")],
    ))
    f = FakeFetcher(
        losses_by_group={833: [[_loss_stub(1, "lh")]]},
        killmails={1: loss},
        related={(SYS, _km_hour(when)): battle},
        affiliation={"corporation_id": PILOT_CORP, "alliance_id": PILOT_ALLY},
        stats=None,
    )
    checker = _checker(f, tmp_path)
    checker.analyze_character(100)
    # Pre-seed a fresh checker that shares the same on-disk cache, but force a
    # live (non-cached) run so the association recomputes from the cached battle.
    checker2 = CynoChecker(fetcher=f, cache=checker.cache)
    f.related_calls.clear()
    checker2.analyze_character(100, use_cache=False)
    assert f.related_calls == []     # served entirely from the related cache


def test_falls_back_to_stats_when_battles_empty(tmp_path):
    """Solo gank (loss has NO attackers / no enemy set) -> battle scan yields
    nothing -> association falls back to the stats aggregate (basis='stats')."""
    when = datetime(2026, 3, 15, 14, 23, 9, tzinfo=timezone.utc)
    # Loss with no attackers -> empty enemy set -> no battle scanned.
    loss = _km(RAPIER, items=[_hi(NORMAL_CYNO)], when=when, system_id=SYS)
    stats = _stats_topalltime(alliances=[(FRIEND_ALLY, 500)], corps=[(222, 50)])
    f = FakeFetcher(
        losses_by_group={833: [[_loss_stub(1, "lh")]]},
        killmails={1: loss},
        related={},
        stats=stats,
        affiliation={"corporation_id": PILOT_CORP, "alliance_id": PILOT_ALLY},
        names={FRIEND_ALLY: "Blue Alliance"},
    )
    assoc = _checker(f, tmp_path).analyze_character(100)["association"]
    assert assoc["basis"] == "stats"
    assert assoc["kind"] == "alliance"
    assert assoc["id"] == FRIEND_ALLY
    # No battle could be anchored, so the related endpoint was never queried.
    assert f.related_calls == []


def test_related_error_marks_partial_and_falls_back(tmp_path):
    """A related-endpoint failure (None on BOTH the floored and previous hour)
    marks the run partial and falls back to the stats aggregate rather than
    crashing or returning unknown."""
    when = datetime(2026, 3, 15, 14, 23, 9, tzinfo=timezone.utc)
    loss = _pilot_loss(when, attackers=[_party(1, alliance_id=ENEMY_ALLY)])
    stats = _stats_topalltime(alliances=[(FRIEND_ALLY, 500)], corps=[(222, 50)])
    f = FakeFetcher(
        losses_by_group={833: [[_loss_stub(1, "lh")]]},
        killmails={1: loss},
        related={
            (SYS, _km_hour(when)): None,    # floored-hour fetch fails
            (SYS, _prev_hour(when)): None,  # previous-hour fallback also fails
        },
        stats=stats,
        affiliation={"corporation_id": PILOT_CORP, "alliance_id": PILOT_ALLY},
        names={FRIEND_ALLY: "Blue Alliance"},
    )
    res = _checker(f, tmp_path).analyze_character(100)
    assoc = res["association"]
    assert assoc["basis"] == "stats"
    assert assoc["id"] == FRIEND_ALLY
    assert "partial" in res["status"]
    # A degraded/partial run is not cached.
    assert _checker(f, tmp_path).cache.get_result(100) is None


def test_battles_yield_only_enemy_falls_back_to_stats(tmp_path):
    """If the only thing the battle shows is the enemy (e.g. enemy ganks an NPC),
    there is no friendly tally -> fall back to stats."""
    when = datetime(2026, 3, 15, 14, 23, 9, tzinfo=timezone.utc)
    loss = _pilot_loss(when, attackers=[_party(1, alliance_id=ENEMY_ALLY)])
    # Battle kill: enemy kills an NPC (no characterID). Victim entity is None,
    # attackers are all enemy -> nothing friendly is added.
    battle = _battle(_battle_kill(
        55,
        victim_party=_inline(None, faction_id=500001),
        attacker_parties=[_inline(9, alliance_id=ENEMY_ALLY)],
    ))
    stats = _stats_topalltime(alliances=[(FRIEND_ALLY, 500)], corps=[(222, 50)])
    f = FakeFetcher(
        losses_by_group={833: [[_loss_stub(1, "lh")]]},
        killmails={1: loss},
        related={(SYS, _km_hour(when)): battle},
        stats=stats,
        affiliation={"corporation_id": PILOT_CORP, "alliance_id": PILOT_ALLY},
        names={FRIEND_ALLY: "Blue Alliance"},
    )
    assoc = _checker(f, tmp_path).analyze_character(100)["association"]
    assert assoc["basis"] == "stats"
    assert assoc["id"] == FRIEND_ALLY


def test_odd_related_shape_degrades_to_stats(tmp_path):
    """A related response of an unexpected shape (e.g. a bare list, or summary
    without team kills) must not crash; it yields no battle and falls back to
    stats."""
    when = datetime(2026, 3, 15, 14, 23, 9, tzinfo=timezone.utc)
    loss = _pilot_loss(when, attackers=[_party(1, alliance_id=ENEMY_ALLY)])
    stats = _stats_topalltime(alliances=[(FRIEND_ALLY, 500)], corps=[(222, 50)])
    f = FakeFetcher(
        losses_by_group={833: [[_loss_stub(1, "lh")]]},
        killmails={1: loss},
        related={
            # Legacy-list shape AND a malformed summary both seeded; neither hour
            # yields kills, so the scan degrades.
            (SYS, _km_hour(when)): [{"killmail_id": 9, "zkb": {"hash": "x"}}],
            (SYS, _prev_hour(when)): {"summary": {"teamA": {"kills": "nope"}}},
        },
        stats=stats,
        affiliation={"corporation_id": PILOT_CORP, "alliance_id": PILOT_ALLY},
        names={FRIEND_ALLY: "Blue Alliance"},
    )
    res = _checker(f, tmp_path).analyze_character(100)
    assoc = res["association"]
    assert assoc["basis"] == "stats"     # degraded cleanly, no crash
    assert assoc["id"] == FRIEND_ALLY


def test_battle_progress_lines_emitted(tmp_path):
    """The scan emits 'Reading battle i/n ...' progress lines."""
    when = datetime(2026, 3, 15, 14, 23, 9, tzinfo=timezone.utc)
    loss = _pilot_loss(when, attackers=[_party(1, alliance_id=ENEMY_ALLY)])
    battle = _battle(_battle_kill(
        50,
        victim_party=_inline(2, alliance_id=ENEMY_ALLY),
        attacker_parties=[_inline(3, alliance_id=FRIEND_ALLY,
                                  alliance_name="Blue Alliance")],
    ))
    f = FakeFetcher(
        losses_by_group={833: [[_loss_stub(1, "lh")]]},
        killmails={1: loss},
        related={(SYS, _km_hour(when)): battle},
        affiliation={"corporation_id": PILOT_CORP, "alliance_id": PILOT_ALLY},
        stats=None,
    )
    msgs = []
    _checker(f, tmp_path).analyze_character(100, progress=msgs.append)
    assert any("Reading battle" in m for m in msgs)


# ════════════════════════════════════════════════════════════════════════════
# PER-BATTLE PRESENCE metric: distinct-battle presence, not raw observations
# ════════════════════════════════════════════════════════════════════════════
#
# Each loss is in a DISTINCT hour so it maps to its own related-battle key, and
# the floored hour is always populated (so the previous-hour fallback never
# fires). Branch A of the side-inference is exercised: an ENEMY ship is the
# victim, so that kill's friendly attackers are tallied.

def _enemy_killed_by(*friendly_parties, kill_id):
    """A battle kill where an ENEMY ship is the victim and the given friendly
    parties are the attackers (Branch A: they fought the enemy -> friendly)."""
    return _battle_kill(
        kill_id,
        victim_party=_inline(2, alliance_id=ENEMY_ALLY),
        attacker_parties=list(friendly_parties),
    )


def _multi_battle_fetcher(battles, *, base=None, stats=None,
                          affiliation=None):
    """Build a FakeFetcher driving one cyno loss per entry in ``battles``.

    ``battles`` is a list of (kill_objects) tuples; each becomes its OWN loss in
    its OWN distinct hour (so a separate related key) with the same enemy anchor.
    Returns the fetcher; the caller runs analyze_character(100).
    """
    if base is None:
        base = datetime(2026, 3, 15, 18, 30, 0, tzinfo=timezone.utc)
    if affiliation is None:
        affiliation = {"corporation_id": PILOT_CORP, "alliance_id": PILOT_ALLY}
    stubs, kms, related = [], {}, {}
    for i, kills in enumerate(battles):
        when = base - timedelta(hours=i)
        km_id = 5000 + i
        stubs.append(_loss_stub(km_id, f"h{i}"))
        kms[km_id] = _pilot_loss(
            when, attackers=[_party(1, alliance_id=ENEMY_ALLY)], hash=f"h{i}")
        related[(SYS, _km_hour(when))] = _battle(*kills)
    return FakeFetcher(
        losses_by_group={833: [stubs]},
        killmails=kms,
        related=related,
        affiliation=affiliation,
        stats=stats,
    )


def test_more_battles_beats_one_big_battle(tmp_path):
    """THE inflation fix.

    Entity X (FRIEND_ALLY_2) appears in ONE battle with MANY friendly kills;
    entity Y (FRIEND_ALLY) appears in MORE battles with FEW kills each. Under the
    OLD raw-observation metric X (many kills in one fight) out-tallies Y and would
    be (wrongly) reported. Under the PER-BATTLE PRESENCE metric Y — present in
    more DISTINCT battles — wins.

    Raw observations here: X = 20 (one battle), Y = 3 (one per battle, 3 battles).
    Old metric would pick X (20 > 3); new metric picks Y (battle_count 3 > 1).
    """
    # Battle 0: ONE battle where X racks up 20 friendly kill-observations.
    big_battle_kills = [
        _enemy_killed_by(
            _inline(3000 + j, alliance_id=FRIEND_ALLY_2,
                    alliance_name="Big Fleet Alliance"),
            kill_id=6000 + j,
        )
        for j in range(20)
    ]
    # Battles 1..3: Y present in each with just ONE friendly kill-observation.
    y_battle = [_enemy_killed_by(
        _inline(40, alliance_id=FRIEND_ALLY, alliance_name="Consistent Blue"),
        kill_id=7000,
    )]
    battles = [big_battle_kills, list(y_battle), list(y_battle), list(y_battle)]
    f = _multi_battle_fetcher(battles, stats=None)
    assoc = _checker(f, tmp_path).analyze_character(100)["association"]
    assert assoc["basis"] == "battles"
    # Y (more distinct battles) wins, NOT X (one big battle).
    assert assoc["id"] == FRIEND_ALLY
    assert assoc["name"] == "Consistent Blue"
    assert assoc["battle_count"] == 3
    assert assoc["count"] == 3            # count mirrors battle_count
    assert assoc["sample_total"] == 4
    # X is the runner-up (present in only its single big battle).
    runner_ids = [r["id"] for r in assoc["runners_up"]]
    assert FRIEND_ALLY_2 in runner_ids
    x_runner = next(r for r in assoc["runners_up"] if r["id"] == FRIEND_ALLY_2)
    assert x_runner["battle_count"] == 1


def test_obs_breaks_tie_when_battle_count_equal(tmp_path):
    """When two blues are present in the SAME number of distinct battles, raw
    observations (summed over kills) break the tie."""
    # Both X and Y present in 2 battles each, but in battle 0 X has 5 kill-obs
    # while Y has 1 -> X's total obs (6) > Y's (2). Same kind (both alliances),
    # so the alliance tiebreak is neutral and obs decides.
    b0 = [
        _enemy_killed_by(
            _inline(3000 + j, alliance_id=FRIEND_ALLY_2,
                    alliance_name="Higher Obs"),
            kill_id=6100 + j,
        ) for j in range(5)
    ] + [_enemy_killed_by(
        _inline(40, alliance_id=FRIEND_ALLY, alliance_name="Lower Obs"),
        kill_id=6200,
    )]
    b1 = [
        _enemy_killed_by(
            _inline(3001, alliance_id=FRIEND_ALLY_2, alliance_name="Higher Obs"),
            kill_id=6300),
        _enemy_killed_by(
            _inline(41, alliance_id=FRIEND_ALLY, alliance_name="Lower Obs"),
            kill_id=6400),
    ]
    f = _multi_battle_fetcher([b0, b1], stats=None)
    assoc = _checker(f, tmp_path).analyze_character(100)["association"]
    assert assoc["battle_count"] == 2          # both present in 2 battles
    assert assoc["id"] == FRIEND_ALLY_2        # higher raw obs wins the tie
    assert assoc["name"] == "Higher Obs"
    # The other blue (same battle_count, fewer obs) is the runner-up.
    assert assoc["runners_up"][0]["id"] == FRIEND_ALLY
    assert assoc["runners_up"][0]["battle_count"] == 2


def test_alliance_preferred_over_corp_when_battle_count_tied(tmp_path):
    """An alliance and a corp-only blue tie on battle_count -> the ALLIANCE wins
    (alliance-over-bare-corp tiebreak), even if the corp has equal/more raw
    observations."""
    blue_corp_id = 808080
    # Both present in 2 battles. The corp even has MORE raw obs in battle 0, but
    # the alliance still wins on the is-alliance tiebreak (which precedes obs).
    b0 = [
        _enemy_killed_by(
            _inline(3000 + j, corporation_id=blue_corp_id,
                    corporation_name="Blue Corp"),
            kill_id=6500 + j,
        ) for j in range(4)
    ] + [_enemy_killed_by(
        _inline(40, alliance_id=FRIEND_ALLY, alliance_name="Blue Alliance"),
        kill_id=6600,
    )]
    b1 = [
        _enemy_killed_by(
            _inline(3100, corporation_id=blue_corp_id,
                    corporation_name="Blue Corp"),
            kill_id=6700),
        _enemy_killed_by(
            _inline(41, alliance_id=FRIEND_ALLY, alliance_name="Blue Alliance"),
            kill_id=6800),
    ]
    f = _multi_battle_fetcher([b0, b1], stats=None)
    assoc = _checker(f, tmp_path).analyze_character(100)["association"]
    assert assoc["battle_count"] == 2
    assert assoc["kind"] == "alliance"
    assert assoc["id"] == FRIEND_ALLY          # alliance beats the corp tie
    # The corp-only blue (equal battle_count) is the runner-up.
    assert assoc["runners_up"][0]["id"] == blue_corp_id
    assert assoc["runners_up"][0]["kind"] == "corporation"


def test_confident_true_on_strict_majority(tmp_path):
    """The top entity present in a STRICT majority of scanned battles -> confident.
    Here Y is present in 3 of 3 scanned battles (3*2 > 3)."""
    y_battle = [_enemy_killed_by(
        _inline(40, alliance_id=FRIEND_ALLY, alliance_name="Blue"), kill_id=7100)]
    f = _multi_battle_fetcher([list(y_battle)] * 3, stats=None)
    assoc = _checker(f, tmp_path).analyze_character(100)["association"]
    assert assoc["sample_total"] == 3
    assert assoc["battle_count"] == 3
    assert assoc["confident"] is True


def test_not_confident_without_strict_majority(tmp_path):
    """The top entity present in only a MINORITY (or exactly half) of scanned
    battles -> NOT confident. Here the top blue is present in 3 of 8 battles
    (3*2 == 6, not > 8), so confident is False."""
    # 3 battles feature FRIEND_ALLY; the other 5 feature FRIEND_ALLY_2 once each
    # but each of THOSE is a distinct entity present in exactly 1 battle, so the
    # top (FRIEND_ALLY, present in 3) is still the winner but not a majority of 8.
    a = [_enemy_killed_by(
        _inline(40, alliance_id=FRIEND_ALLY, alliance_name="Blue"), kill_id=7200)]
    others = []
    for i in range(5):
        others.append([_enemy_killed_by(
            _inline(50 + i, alliance_id=900100 + i,
                    alliance_name=f"One-Off {i}"), kill_id=7300 + i)])
    battles = [list(a), list(a), list(a)] + others
    f = _multi_battle_fetcher(battles, stats=None)
    res = _checker(f, tmp_path).analyze_character(100)
    assoc = res["association"]
    assert assoc["sample_total"] == 8
    assert assoc["id"] == FRIEND_ALLY
    assert assoc["battle_count"] == 3
    # 3 of 8 is not a strict majority -> not confident.
    assert assoc["confident"] is False


def test_runners_up_ordered_and_capped(tmp_path):
    """runners_up holds up to the next 2 entities by the SAME ranking, ordered."""
    # Top: A present in 4 battles; B in 3; C in 2; D in 1. runners_up should be
    # [B, C] (the next two), in that order; D is dropped (cap of 2).
    A = lambda kid: _enemy_killed_by(
        _inline(40, alliance_id=FRIEND_ALLY, alliance_name="A"), kill_id=kid)
    B = lambda kid: _enemy_killed_by(
        _inline(41, alliance_id=FRIEND_ALLY_2, alliance_name="B"), kill_id=kid)
    C = lambda kid: _enemy_killed_by(
        _inline(42, alliance_id=810003, alliance_name="C"), kill_id=kid)
    D = lambda kid: _enemy_killed_by(
        _inline(43, alliance_id=810004, alliance_name="D"), kill_id=kid)
    # Build 4 battles; each battle includes whichever entities should be "present"
    # so the presence counts are A=4, B=3, C=2, D=1.
    battles = [
        [A(8000), B(8001), C(8002), D(8003)],   # battle 0: A,B,C,D
        [A(8010), B(8011), C(8012)],            # battle 1: A,B,C
        [A(8020), B(8021)],                     # battle 2: A,B
        [A(8030)],                              # battle 3: A
    ]
    f = _multi_battle_fetcher(battles, stats=None)
    assoc = _checker(f, tmp_path).analyze_character(100)["association"]
    assert assoc["id"] == FRIEND_ALLY          # A, present in all 4
    assert assoc["battle_count"] == 4
    ru = assoc["runners_up"]
    assert len(ru) == 2                          # capped at 2
    assert [r["id"] for r in ru] == [FRIEND_ALLY_2, 810003]   # B then C
    assert [r["battle_count"] for r in ru] == [3, 2]
    # Each runner-up carries the documented shape.
    assert set(ru[0].keys()) == {"name", "id", "kind", "battle_count"}
    assert ru[0]["name"] == "B"


def test_stats_fallback_exposes_new_fields_defensively(tmp_path):
    """The stats fallback path sets the new fields so a GUI reading them won't
    break: confident=True, runners_up=[], battle_count=None."""
    when = datetime(2026, 3, 15, 14, 23, 9, tzinfo=timezone.utc)
    # Solo gank: loss has no attackers -> empty enemy set -> stats fallback.
    loss = _km(RAPIER, items=[_hi(NORMAL_CYNO)], when=when, system_id=SYS)
    stats = _stats_topalltime(alliances=[(FRIEND_ALLY, 500)], corps=[(222, 50)])
    f = FakeFetcher(
        losses_by_group={833: [[_loss_stub(1, "lh")]]},
        killmails={1: loss},
        related={},
        stats=stats,
        affiliation={"corporation_id": PILOT_CORP, "alliance_id": PILOT_ALLY},
        names={FRIEND_ALLY: "Blue Alliance"},
    )
    assoc = _checker(f, tmp_path).analyze_character(100)["association"]
    assert assoc["basis"] == "stats"
    assert assoc["confident"] is True
    assert assoc["runners_up"] == []
    assert assoc["battle_count"] is None
    # Existing stats fields still present.
    assert assoc["kind"] == "alliance"
    assert assoc["id"] == FRIEND_ALLY
    assert assoc["count"] == 500


# ════════════════════════════════════════════════════════════════════════════
# END-TO-END over a real captured zKill 'related' response
# ════════════════════════════════════════════════════════════════════════════

_FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "cyno",
    "related_30002019_202602110100.json")

# Ground-truth facts from the captured Calypso Yaken battle (F-NMX6, 30002019):
#   * Calypso's Falcon (Force Recon, type 11957) loss — victim characterID below.
#   * Killed by TEST (498125261) + Fraternity (99003581 / 99013541).
#   * The friendly side that fought the killers is The Initiative. (1900696668).
CALYPSO_ID = 2112710733
CALYPSO_CORP = 98823198            # his corp (no alliance) — must be excluded
INITIATIVE = 1900696668            # the inferred association
TEST_ALLY = 498125261
FRAT_A = 99003581
FRAT_B = 99013541
F_NMX6 = 30002019


def test_end_to_end_real_related_resolves_initiative(tmp_path):
    """Load the REAL captured related response, point a qualifying Falcon loss at
    Calypso's killmail, and assert the battle-inferred association resolves to
    The Initiative. (1900696668) with basis 'battles' — entirely from the inline
    team-summary data (no ESI battle fetch, no /universe/names/)."""
    with open(_FIXTURE, encoding="utf-8") as fh:
        battle = json.load(fh)

    # Calypso's Falcon loss as an ESI killmail (snake_case): a high-slot cyno so
    # it qualifies, and the three killers as attackers seeding the enemy set.
    when = datetime(2026, 2, 11, 1, 46, 0, tzinfo=timezone.utc)
    loss = {
        "killmail_time": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "solar_system_id": F_NMX6,
        "victim": {
            "character_id": CALYPSO_ID,
            "corporation_id": CALYPSO_CORP,
            "ship_type_id": FALCON,
            "items": [_hi(NORMAL_CYNO)],
        },
        "attackers": [
            {"character_id": 11, "alliance_id": FRAT_A},
            {"character_id": 12, "alliance_id": FRAT_B},
            {"character_id": 13, "alliance_id": TEST_ALLY},
        ],
    }

    # The Falcon is group 833 (Force Recon) per the static group map.
    f = FakeFetcher(
        losses_by_group={833: [[_loss_stub(133279149, "lh")]]},
        killmails={133279149: loss},
        related={(F_NMX6, "202602110100"): battle},   # the floored hour key
        # Calypso's own corp (no alliance), so INITIATIVE is never self-excluded.
        affiliation={"corporation_id": CALYPSO_CORP, "alliance_id": None},
        stats=None,
    )
    res = _checker(f, tmp_path).analyze_character(100)
    assert res["total"] == 1
    assoc = res["association"]
    assert assoc["basis"] == "battles"
    assert assoc["kind"] == "alliance"
    assert assoc["id"] == INITIATIVE
    assert assoc["name"] == "The Initiative."     # inline allianceName
    assert assoc["sample_total"] == 1
    # Per-battle presence: Initiative is present in the single scanned battle, so
    # battle_count == 1 and that is a strict majority of 1 -> confident.
    assert assoc["battle_count"] == 1
    assert assoc["confident"] is True
    # The floored hour was queried; the related response carried everything inline
    # so only the loss killmail itself was ESI-fetched.
    assert (F_NMX6, "202602110100") in f.related_calls
    assert f.km_calls == [(133279149, "lh")]


# ════════════════════════════════════════════════════════════════════════════
# Cache round-trip + is_stale
# ════════════════════════════════════════════════════════════════════════════

def test_cache_round_trip(tmp_path):
    path = str(tmp_path / "cyno_cache.json")
    cache = CynoCache(path=path)
    cache.load()
    result = {"total": 3, "breakdown": {"HIC": 3}, "latest": None,
              "association": {"kind": "unknown"}, "losses": [], "status": "ok"}
    cache.put_result(12345, result)
    cache.put_killmail(999, {"killmail_time": "2026-01-01T00:00:00Z"})
    cache.save()

    cache2 = CynoCache(path=path)
    cache2.load()
    entry = cache2.get_result(12345)
    assert entry is not None
    assert entry["result"]["total"] == 3
    assert cache2.get_killmail(999) == {"killmail_time": "2026-01-01T00:00:00Z"}


def test_corrupt_cache_is_discarded_and_logged(tmp_path, caplog):
    """A corrupt cache file is discarded (empty caches) but the discard is now
    LOGGED at WARNING rather than swallowed silently."""
    import logging

    path = str(tmp_path / "cyno_cache.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{ this is not valid json ]")
    cache = CynoCache(path=path)
    with caplog.at_level(logging.WARNING, logger="cyno_check"):
        cache.load()
    # Behavior preserved: corrupt file => empty caches, no crash.
    assert cache.results == {}
    assert cache.killmails == {}
    assert cache.related == {}
    assert cache.get_result(123) is None
    # New behavior: the discard emitted a WARNING log line.
    assert any(rec.levelno == logging.WARNING
               and "cyno cache load failed" in rec.getMessage()
               for rec in caplog.records)


def test_cache_is_stale_when_missing(tmp_path):
    cache = CynoCache(path=str(tmp_path / "c.json"))
    cache.load()
    assert cache.is_stale(42) is True


def test_cache_is_stale_when_old(tmp_path):
    path = str(tmp_path / "c.json")
    cache = CynoCache(path=path)
    cache.load()
    cache.results["42"] = {
        "result": {"total": 0},
        "fetched_at": (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(),
    }
    assert cache.is_stale(42, max_age_hours=2) is True


def test_cache_not_stale_when_fresh(tmp_path):
    path = str(tmp_path / "c.json")
    cache = CynoCache(path=path)
    cache.load()
    cache.results["42"] = {
        "result": {"total": 0},
        "fetched_at": (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),
    }
    assert cache.is_stale(42, max_age_hours=2) is False


def test_analyze_uses_cache_on_second_call(tmp_path):
    """A fresh cached result short-circuits a second analyze (no loss calls)."""
    f = FakeFetcher(
        losses_by_group={833: [[{"killmail_id": 1, "zkb": {"hash": "h1"}}]]},
        killmails={1: _km(RAPIER, items=[_hi(NORMAL_CYNO)])},
        stats=None,
    )
    checker = _checker(f, tmp_path)
    res1 = checker.analyze_character(100)
    assert res1["total"] == 1
    calls_after_first = len(f.loss_calls)

    # Reuse the SAME cache object (already populated + fresh).
    res2 = checker.analyze_character(100)
    assert res2["total"] == 1
    # No additional zKill loss calls were made on the cached path.
    assert len(f.loss_calls) == calls_after_first


def test_partial_results_not_cached(tmp_path):
    """A run with a failed page (partial) must not be persisted, so a later
    healthy run can replace it."""
    # page value None => zkill_losses_page returns None => partial.
    f = FakeFetcher(losses_by_group={833: [None]}, killmails={}, stats=None)
    checker = _checker(f, tmp_path)
    res = checker.analyze_character(100)
    assert "partial" in res["status"]
    # Nothing was cached for character 100.
    assert checker.cache.get_result(100) is None


# ════════════════════════════════════════════════════════════════════════════
# Graceful degradation: network errors + missing fields
# ════════════════════════════════════════════════════════════════════════════

def test_invalid_character_id_returns_empty(tmp_path):
    f = FakeFetcher()
    checker = _checker(f, tmp_path)
    for bad in (0, None, -5):
        res = checker.analyze_character(bad)
        assert res["total"] == 0
        assert res["losses"] == []
        assert res["association"] == {"kind": "unknown"}
        assert "invalid" in res["status"]


def test_killmail_fetch_failure_is_partial_not_crash(tmp_path):
    """If an ESI killmail can't be fetched, the run is partial but doesn't crash
    and other mails still count."""
    now = datetime.now(timezone.utc)
    f = FakeFetcher(
        losses_by_group={833: [[
            {"killmail_id": 50, "zkb": {"hash": "h"}},   # fetch fails (None)
            {"killmail_id": 51, "zkb": {"hash": "h"}},   # ok, qualifies
        ]]},
        killmails={51: _km(RAPIER, items=[_hi(NORMAL_CYNO)], when=now)},  # 50 absent
    )
    res = _checker(f, tmp_path).analyze_character(100)
    assert res["total"] == 1
    assert "partial" in res["status"]


def test_entries_missing_hash_are_skipped(tmp_path):
    """Loss entries lacking a zkb hash or killmail_id are skipped silently."""
    now = datetime.now(timezone.utc)
    f = FakeFetcher(
        losses_by_group={833: [[
            {"killmail_id": 60},                         # no zkb/hash
            {"zkb": {"hash": "h"}},                      # no killmail_id
            {"killmail_id": 61, "zkb": {"hash": "h"}},   # valid
        ]]},
        killmails={61: _km(RAPIER, items=[_hi(NORMAL_CYNO)], when=now)},
    )
    res = _checker(f, tmp_path).analyze_character(100)
    assert res["total"] == 1


def test_killmail_missing_victim_or_items(tmp_path):
    """Killmails with no victim / no items don't crash and don't qualify."""
    now = datetime.now(timezone.utc)
    f = FakeFetcher(
        losses_by_group={833: [[
            {"killmail_id": 70, "zkb": {"hash": "h"}},
            {"killmail_id": 71, "zkb": {"hash": "h"}},
        ]]},
        killmails={
            70: {"killmail_time": now.strftime("%Y-%m-%dT%H:%M:%SZ")},  # no victim
            71: {"killmail_time": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "victim": {"ship_type_id": RAPIER}},  # victim, but no items key
        },
    )
    res = _checker(f, tmp_path).analyze_character(100)
    assert res["total"] == 0
    # Did not raise; produced a coherent empty result.
    assert res["breakdown"] == {}


def test_killmail_without_time_still_handled(tmp_path):
    """A qualifying killmail with a missing killmail_time is counted (cutoff
    can't exclude it) and sorts last."""
    now = datetime.now(timezone.utc)
    km_no_time = {
        "solar_system_id": 30000142,
        "victim": {"ship_type_id": RAPIER, "items": [_hi(NORMAL_CYNO)]},
    }
    f = FakeFetcher(
        losses_by_group={833: [[
            {"killmail_id": 80, "zkb": {"hash": "h"}},   # no time
            {"killmail_id": 81, "zkb": {"hash": "h"}},   # has time
        ]]},
        killmails={
            80: km_no_time,
            81: _km(RAPIER, items=[_hi(NORMAL_CYNO)], when=now),
        },
    )
    res = _checker(f, tmp_path).analyze_character(100)
    assert res["total"] == 2
    # The timed mail (81) is the latest; the untimed one sorts last.
    assert res["latest"]["killmail_id"] == 81


def test_no_losses_clean_status(tmp_path):
    f = FakeFetcher(losses_by_group={}, stats=None, affiliation=None)
    res = _checker(f, tmp_path).analyze_character(100)
    assert res["total"] == 0
    assert res["breakdown"] == {}
    assert res["latest"] is None
    assert "No cyno-ship losses" in res["status"]


def test_progress_callback_receives_lines(tmp_path):
    msgs = []
    f = FakeFetcher(losses_by_group={}, stats=None, affiliation=None)
    _checker(f, tmp_path).analyze_character(100, progress=msgs.append)
    assert any("Scanning losses" in m for m in msgs)
    assert any("Done" == m for m in msgs)


def test_progress_callback_exceptions_swallowed(tmp_path):
    def bad(_):
        raise RuntimeError("gui blew up")
    f = FakeFetcher(losses_by_group={}, stats=None, affiliation=None)
    # Must not propagate the callback's exception.
    res = _checker(f, tmp_path).analyze_character(100, progress=bad)
    assert res["total"] == 0


def test_parse_km_time_variants():
    assert _parse_km_time("2026-06-15T12:00:00Z").year == 2026
    assert _parse_km_time(None) is None
    assert _parse_km_time("not-a-date") is None
    assert _parse_km_time(12345) is None
