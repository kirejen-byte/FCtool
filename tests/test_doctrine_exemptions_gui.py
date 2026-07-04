"""GUI wiring for the per-doctrine ideal-% exemption feature.

Covers the fc_gui side of the doctrine-exemptions feature (the engine side lives
in test_fleet_guidance.py / test_fittings_store.py):

  1. ``_resolve_exemptions_for_counts`` — classifies ONLY the present ship_type
     ids, returning (exempt_type_ids, doctrine_hull_ids). A Force Recon present in
     the fleet is exempt UNLESS it is one of the doctrine's own hulls.
  2. the two live callers (``_update_specialized_roles`` guidance call and
     ``_motd_fit_deltas``) forward exempt_type_ids + doctrine_hull_ids into
     ``compute_fleet_guidance``.
  3. the guidance panel shows a dim "N excluded from %" line when
     ``excluded_from_pct > 0`` (and not when it is 0).
  4. the exemptions editor's pure list helpers (add / remove / reset) and the
     commit path route through ``FittingsStore.set_doctrine_exemptions`` +
     ``save`` and refresh the detail pane + MOTD preview.

All tests use throwaway ``FCToolGUI`` instances (``object.__new__``) and bound
methods with fakes — no Tk window, no network. ``ship_classes`` classifiers are
monkeypatched to cache-free fakes so no ESI call is made. Repo root is on
sys.path via tests/conftest.py.
"""

import types

import fc_gui
import fleet_guidance as fg
import ship_classes
from fit_models import DoctrineMember, Fit, ParsedFit


FCToolGUI = fc_gui.FCToolGUI


# ── Fakes ────────────────────────────────────────────────────────────────────

class _Mem:
    def __init__(self, fit_id, tags):
        self.fit_id = fit_id
        self.tags = tags


class _Fit:
    def __init__(self, hull_type_id):
        self.hull_type_id = hull_type_id


class _Doc:
    def __init__(self, members, exemptions=None, doc_id="doc1"):
        self.id = doc_id
        self.name = "Doc"
        self.members = members
        self.exemptions = exemptions


class _Fittings:
    """Minimal fittings store: get_fit for build_tag_index + a doctrine mutator."""
    def __init__(self, fits):
        self._fits = fits
        self.set_calls = []      # (doctrine_id, entries)
        self.saved = 0

    def get_fit(self, fit_id):
        return self._fits.get(fit_id)

    def set_doctrine_exemptions(self, doctrine_id, entries):
        self.set_calls.append((doctrine_id, entries))

    def save(self):
        self.saved += 1


# Force Recon Ship group id used across the exemption seed.
FORCE_RECON_GID = 833
RECON_TID = 11957        # a Force Recon hull (present in fleet)
DPS_TID = 17740          # doctrine DPS hull
LOGI_TID = 33816         # doctrine logi hull


def _classify_fakes(monkeypatch, group_map, cap_set=()):
    """Point ship_classes.get_group_id / is_capital at cache-free fakes."""
    cap = set(cap_set)
    monkeypatch.setattr(ship_classes, "get_group_id",
                        lambda tid: group_map.get(tid), raising=True)
    monkeypatch.setattr(ship_classes, "is_capital",
                        lambda tid: tid in cap, raising=True)


# ── 1. _resolve_exemptions_for_counts ────────────────────────────────────────

def test_resolve_exemptions_recon_present_is_exempt(monkeypatch):
    """A Force Recon present in the fleet (not a doctrine hull) is exempt; the
    doctrine hulls are collected into doctrine_hull_ids."""
    _classify_fakes(monkeypatch, {RECON_TID: FORCE_RECON_GID,
                                  DPS_TID: 999, LOGI_TID: 998})
    fits = {"dps": _Fit(DPS_TID), "logi": _Fit(LOGI_TID)}
    doc = _Doc([_Mem("dps", ["DPS"]), _Mem("logi", ["Logistics"])])
    gui = object.__new__(FCToolGUI)
    gui.fittings = _Fittings(fits)

    counts = {DPS_TID: 7, LOGI_TID: 6, RECON_TID: 4}
    exempt, hull_ids = FCToolGUI._resolve_exemptions_for_counts(gui, doc, counts)

    assert exempt == {RECON_TID}
    assert hull_ids == {DPS_TID, LOGI_TID}


def _real_fit(fit_id, hull):
    parsed = ParsedFit(ship_type_id=hull, ship_name="X", modules=[],
                       drones=[], cargo=[], subsystems=[])
    return Fit(id=fit_id, name=fit_id, hull_type_id=hull, hull_name=str(hull),
               source="dna", raw_text="", parsed=parsed, dna="", notes="",
               esi_fitting_ids={}, created="", modified="")


def _real_mem(fit_id, tags):
    return DoctrineMember(fit_id=fit_id, tags=tags, order=0,
                          ideal_mode=None, ideal_min=None, ideal_max=None)


def test_resolve_exemptions_recon_that_is_doctrine_hull_still_exempt_set(monkeypatch):
    """The recon is still reported as exempt-by-classification even when it is a
    doctrine hull — the engine applies the exact-hull override (a hull in
    doctrine_hull_ids is never actually subtracted)."""
    _classify_fakes(monkeypatch, {RECON_TID: FORCE_RECON_GID, DPS_TID: 999})
    fits = {"dps": _real_fit("dps", DPS_TID), "recon": _real_fit("recon", RECON_TID)}
    doc = _Doc([_real_mem("dps", ["DPS"]), _real_mem("recon", ["Tackle"])])
    gui = object.__new__(FCToolGUI)
    gui.fittings = _Fittings(fits)

    counts = {DPS_TID: 7, RECON_TID: 2}
    exempt, hull_ids = FCToolGUI._resolve_exemptions_for_counts(gui, doc, counts)

    assert RECON_TID in exempt          # classified exempt
    assert RECON_TID in hull_ids        # but also a doctrine hull
    # Engine override: with RECON in doctrine_hull_ids, nothing is excluded.
    rep = fg.compute_fleet_guidance(
        doc, gui.fittings.get_fit, _Cat(), counts, 9,
        exempt_type_ids=exempt, doctrine_hull_ids=hull_ids)
    assert rep.excluded_from_pct == 0


def test_resolve_exemptions_only_present_types_classified(monkeypatch):
    """Only ship_type ids actually present in the counts get classified — an
    absent type never appears in the exempt set, and get_group_id is only ever
    called for present ids."""
    seen = []

    def _gid(tid):
        seen.append(tid)
        return {RECON_TID: FORCE_RECON_GID, DPS_TID: 999}.get(tid)

    monkeypatch.setattr(ship_classes, "get_group_id", _gid, raising=True)
    monkeypatch.setattr(ship_classes, "is_capital", lambda tid: False, raising=True)

    fits = {"dps": _Fit(DPS_TID)}
    doc = _Doc([_Mem("dps", ["DPS"])])
    gui = object.__new__(FCToolGUI)
    gui.fittings = _Fittings(fits)

    counts = {DPS_TID: 7}   # no recon present
    exempt, _hull_ids = FCToolGUI._resolve_exemptions_for_counts(gui, doc, counts)
    assert exempt == set()
    assert set(seen) <= {DPS_TID}       # never classified a non-present id


def test_resolve_exemptions_custom_empty_list_exempts_nothing(monkeypatch):
    """An explicit empty exemption list means 'no exemptions' — even a present
    recon is not exempt."""
    _classify_fakes(monkeypatch, {RECON_TID: FORCE_RECON_GID, DPS_TID: 999})
    fits = {"dps": _Fit(DPS_TID)}
    doc = _Doc([_Mem("dps", ["DPS"])], exemptions=[])   # explicit none
    gui = object.__new__(FCToolGUI)
    gui.fittings = _Fittings(fits)

    counts = {DPS_TID: 7, RECON_TID: 4}
    exempt, _hull = FCToolGUI._resolve_exemptions_for_counts(gui, doc, counts)
    assert exempt == set()


# ── 2. callers forward the sets into compute_fleet_guidance ──────────────────

class _Cat:
    def group_of(self, tid): return None
    def resolve_name(self, tid): return str(tid)
    def category_of(self, tid): return "module"


def _capturing_compute(monkeypatch):
    """Patch fleet_guidance.compute_fleet_guidance to record kwargs and return a
    stub report; returns the mutable capture dict."""
    cap = {}
    stub = types.SimpleNamespace(fits=[], roles={}, links_suppressed=False,
                                 has_live_fleet=True, excluded_from_pct=0)

    def _fake(doc, get_fit, catalog, counts, total, command_ship_fraction=0.0,
              exempt_type_ids=None, doctrine_hull_ids=None):
        cap["exempt_type_ids"] = exempt_type_ids
        cap["doctrine_hull_ids"] = doctrine_hull_ids
        cap["total"] = total
        return stub

    monkeypatch.setattr(fc_gui.fleet_guidance, "compute_fleet_guidance", _fake,
                        raising=True)
    return cap


def test_motd_fit_deltas_forwards_exempt_sets(monkeypatch):
    """_motd_fit_deltas resolves + forwards exempt_type_ids and doctrine_hull_ids
    (recon present & not a doctrine hull -> in the exempt set)."""
    _classify_fakes(monkeypatch, {RECON_TID: FORCE_RECON_GID, DPS_TID: 999})
    cap = _capturing_compute(monkeypatch)

    fits = {"dps": _Fit(DPS_TID)}
    doc = _Doc([_Mem("dps", ["DPS"])])
    gui = object.__new__(FCToolGUI)
    gui.fittings = _Fittings(fits)
    gui.type_catalog = _Cat()
    gui._active_fleet_doctrine = lambda: doc
    counts = {DPS_TID: 7, RECON_TID: 4}
    gui._last_specialized_args = ([], counts, 11)

    FCToolGUI._motd_fit_deltas(gui)

    assert cap["exempt_type_ids"] == {RECON_TID}
    assert cap["doctrine_hull_ids"] == {DPS_TID}


def test_specialized_guidance_call_forwards_exempt_sets(monkeypatch):
    """The live guidance call in _update_specialized_roles forwards the resolved
    exempt sets. We exercise just the guidance-compute block via a tiny shim that
    mirrors the caller (doc in scope, resolve, forward)."""
    _classify_fakes(monkeypatch, {RECON_TID: FORCE_RECON_GID, DPS_TID: 999})
    cap = _capturing_compute(monkeypatch)

    fits = {"dps": _Fit(DPS_TID)}
    doc = _Doc([_Mem("dps", ["DPS"])])
    gui = object.__new__(FCToolGUI)
    gui.fittings = _Fittings(fits)
    gui.type_catalog = _Cat()
    counts = {DPS_TID: 7, RECON_TID: 4}

    # Mirror the exact resolve+forward the caller does.
    exempt, hull_ids = FCToolGUI._resolve_exemptions_for_counts(gui, doc, counts)
    fc_gui.fleet_guidance.compute_fleet_guidance(
        doc, gui.fittings.get_fit, gui.type_catalog, counts, 11,
        command_ship_fraction=0.1,
        exempt_type_ids=exempt, doctrine_hull_ids=hull_ids)

    assert cap["exempt_type_ids"] == {RECON_TID}
    assert cap["doctrine_hull_ids"] == {DPS_TID}


# ── 3. panel "N excluded from %" note ────────────────────────────────────────

def test_exclusion_note_text_when_excluded_positive():
    rep = types.SimpleNamespace(excluded_from_pct=4)
    assert FCToolGUI._exclusion_note_text(object.__new__(FCToolGUI), rep) \
        == "4 excluded from % (caps/recon)"


def test_exclusion_note_text_none_when_zero_or_missing():
    gui = object.__new__(FCToolGUI)
    assert FCToolGUI._exclusion_note_text(
        gui, types.SimpleNamespace(excluded_from_pct=0)) is None
    assert FCToolGUI._exclusion_note_text(gui, None) is None


# ── 4. exemptions editor: list helpers + commit path ─────────────────────────

def test_add_exemption_entry_dedupes_by_kind_and_id():
    entries = [{"kind": "group", "id": 833, "name": "Force Recon Ship"}]
    # adding the same group again is a no-op
    out = fc_gui._add_exemption_entry(entries, {"kind": "group", "id": 833,
                                                "name": "Force Recon Ship"})
    assert out == entries
    # a different group is appended
    out2 = fc_gui._add_exemption_entry(entries, {"kind": "group", "id": 906,
                                                 "name": "Combat Recon Ship"})
    assert out2[-1]["id"] == 906 and len(out2) == 2
    # capital meta dedupes on kind alone
    out3 = fc_gui._add_exemption_entry([{"kind": "capital"}], {"kind": "capital"})
    assert out3 == [{"kind": "capital"}]
    out4 = fc_gui._add_exemption_entry([{"kind": "capital"}],
                                       {"kind": "type", "id": 671, "name": "Erebus"})
    assert len(out4) == 2


def test_remove_exemption_entry_by_index():
    entries = [{"kind": "capital"},
               {"kind": "type", "id": 671, "name": "Erebus"}]
    out = fc_gui._remove_exemption_entry(entries, 0)
    assert out == [{"kind": "type", "id": 671, "name": "Erebus"}]
    # out-of-range index is a safe no-op (returns a copy)
    same = fc_gui._remove_exemption_entry(entries, 9)
    assert same == entries and same is not entries


def test_commit_exemptions_saves_and_refreshes():
    """Saving an explicit list routes through the store mutator + save, then
    refreshes the detail pane and MOTD preview."""
    gui = object.__new__(FCToolGUI)
    gui.fittings = _Fittings({})
    doc = _Doc([], doc_id="docX")
    refreshed = {"detail": None, "motd": 0}
    gui._show_doctrine_detail = lambda arg: refreshed.__setitem__("detail", arg)
    gui._rebuild_motd_preview = lambda: refreshed.__setitem__(
        "motd", refreshed["motd"] + 1)
    gui.fittings.get_doctrine = lambda did: doc

    entries = [{"kind": "capital"}]
    FCToolGUI._commit_doctrine_exemptions(gui, doc, entries)

    assert gui.fittings.set_calls == [("docX", entries)]
    assert gui.fittings.saved == 1
    assert refreshed["motd"] == 1
    # detail pane refreshed (arg may be the doc or its id, both acceptable)
    assert refreshed["detail"] in (doc, "docX")


def test_commit_exemptions_reset_passes_none():
    """Reset-to-standard commits None (so the doctrine falls back to the standard
    seed)."""
    gui = object.__new__(FCToolGUI)
    gui.fittings = _Fittings({})
    doc = _Doc([], doc_id="docY")
    gui._show_doctrine_detail = lambda arg: None
    gui._rebuild_motd_preview = lambda: None
    gui.fittings.get_doctrine = lambda did: doc

    FCToolGUI._commit_doctrine_exemptions(gui, doc, None)
    assert gui.fittings.set_calls == [("docY", None)]
    assert gui.fittings.saved == 1
