from fittings_store import FittingsStore
from fit_models import Fit, ParsedFit


def _fit(name="Arty Muninn"):
    parsed = ParsedFit(12015, "Muninn", [], [], [], [])
    return Fit(id="", name=name, hull_type_id=12015, hull_name="Muninn",
               source="eft", raw_text="[Muninn, %s]" % name, parsed=parsed,
               dna="12015::", notes="", esi_fitting_ids={}, created="", modified="")


def test_add_get_list_persist(tmp_path):
    path = str(tmp_path / "lib.json")
    s = FittingsStore(path); s.load()
    fid = s.add_fit(_fit())
    assert s.get_fit(fid).name == "Arty Muninn"
    assert len(s.list_fits()) == 1
    s.save()
    s2 = FittingsStore(path); s2.load()                 # survives reload
    assert s2.get_fit(fid).name == "Arty Muninn"


def test_update_and_delete(tmp_path):
    s = FittingsStore(str(tmp_path / "lib.json")); s.load()
    fid = s.add_fit(_fit())
    f = s.get_fit(fid); f.name = "Renamed"; s.update_fit(f)
    assert s.get_fit(fid).name == "Renamed"
    s.delete_fit(fid)
    assert s.get_fit(fid) is None


def test_default_tags_seeded(tmp_path):
    s = FittingsStore(str(tmp_path / "lib.json")); s.load()
    assert "DPS" in s.tags and "Logistics" in s.tags


def test_membership_tags_are_per_doctrine(tmp_path):
    s = FittingsStore(str(tmp_path / "lib.json")); s.load()
    fid = s.add_fit(_fit())
    d1 = s.add_doctrine("Shield HACs")
    d2 = s.add_doctrine("Roam")
    s.add_fit_to_doctrine(d1, fid, ["DPS"])
    s.add_fit_to_doctrine(d2, fid, ["Special"])
    m1 = next(m for m in s.get_doctrine(d1).members if m.fit_id == fid)
    m2 = next(m for m in s.get_doctrine(d2).members if m.fit_id == fid)
    assert m1.tags == ["DPS"] and m2.tags == ["Special"]   # same fit, different tags


def test_set_tags_and_remove_member(tmp_path):
    s = FittingsStore(str(tmp_path / "lib.json")); s.load()
    fid = s.add_fit(_fit()); d = s.add_doctrine("D")
    s.add_fit_to_doctrine(d, fid, ["DPS"])
    s.set_member_tags(d, fid, ["DPS", "Links"])
    assert sorted(next(m for m in s.get_doctrine(d).members).tags) == ["DPS", "Links"]
    s.remove_fit_from_doctrine(d, fid)
    assert s.get_doctrine(d).members == []


def test_delete_fit_cascades_membership(tmp_path):
    s = FittingsStore(str(tmp_path / "lib.json")); s.load()
    fid = s.add_fit(_fit()); d = s.add_doctrine("D")
    s.add_fit_to_doctrine(d, fid, ["DPS"])
    s.delete_fit(fid)
    assert all(m.fit_id != fid for m in s.get_doctrine(d).members)


def test_export_then_import_into_clean_store_is_self_contained(tmp_path):
    src = FittingsStore(str(tmp_path / "a.json")); src.load()
    fid = src.add_fit(_fit("Arty Muninn")); d = src.add_doctrine("Shield HACs")
    src.add_fit_to_doctrine(d, fid, ["DPS"])
    payload = src.export_doctrines([d])

    dst = FittingsStore(str(tmp_path / "b.json")); dst.load()
    summary = dst.import_share(payload)
    assert summary.fits_added == 1 and summary.doctrines_added == 1
    nd = dst.list_doctrines()[0]
    assert nd.name == "Shield HACs"
    member = nd.members[0]
    assert dst.get_fit(member.fit_id).name == "Arty Muninn"   # full fit travelled in the file
    assert member.tags == ["DPS"]                             # tags preserved


def test_import_dedupes_identical_fit_by_content_hash(tmp_path):
    dst = FittingsStore(str(tmp_path / "b.json")); dst.load()
    existing = dst.add_fit(_fit("Existing Name"))
    src = FittingsStore(str(tmp_path / "a.json")); src.load()
    fid = src.add_fit(_fit("Shared Hull Fit")); d = src.add_doctrine("D")
    src.add_fit_to_doctrine(d, fid, ["DPS"])
    summary = dst.import_share(src.export_doctrines([d]))
    assert summary.fits_added == 0 and summary.fits_reused == 1   # same parsed content -> reused
    assert dst.get_doctrine(dst.list_doctrines()[0].id).members[0].fit_id == existing


def test_push_fit_creates_and_records_id(tmp_path):
    s = FittingsStore(str(tmp_path / "lib.json")); s.load()
    fid = s.add_fit(_fit())

    class FakeAuth:
        def __init__(self): self.created = []; self.deleted = []
        def create_fitting(self, cid, body): self.created.append((cid, body)); return 4242
        def delete_fitting(self, cid, fitid): self.deleted.append((cid, fitid)); return True
    auth = FakeAuth()

    assert s.push_fit_to_character(fid, 100, auth) is True
    assert s.get_fit(fid).esi_fitting_ids == {100: 4242}      # id recorded
    assert auth.deleted == []                                  # nothing to delete first time


def test_push_fit_deletes_prior_id_then_recreates(tmp_path):
    s = FittingsStore(str(tmp_path / "lib.json")); s.load()
    fid = s.add_fit(_fit())
    f = s.get_fit(fid); f.esi_fitting_ids = {100: 1111}; s.update_fit(f)

    class FakeAuth:
        def __init__(self): self.deleted = []
        def create_fitting(self, cid, body): return 5555
        def delete_fitting(self, cid, fitid): self.deleted.append((cid, fitid)); return True
    auth = FakeAuth()

    assert s.push_fit_to_character(fid, 100, auth) is True
    assert auth.deleted == [(100, 1111)]                      # old id deleted (edit = delete+recreate)
    assert s.get_fit(fid).esi_fitting_ids == {100: 5555}


from fit_models import ParsedFit, ParsedModule, Fit


class _StubCat:
    """Minimal catalog: group_of/resolve_name for defender detection."""
    def group_of(self, tid):
        return None
    def resolve_name(self, tid):
        return str(tid)


def _store_with_fit(tmp_path, hull, module_type_ids):
    store = FittingsStore(str(tmp_path / "lib.json"))
    store.load()
    store.catalog = _StubCat()
    parsed = ParsedFit(ship_type_id=hull, ship_name="X",
                       modules=[ParsedModule(t, str(t), "high") for t in module_type_ids],
                       drones=[], cargo=[], subsystems=[])
    fit = Fit(id="", name="X", hull_type_id=hull, hull_name="X", source="dna",
              raw_text="", parsed=parsed, dna="", notes="", esi_fitting_ids={},
              created="", modified="")
    fid = store.add_fit(fit)
    return store, fid


def test_add_fit_to_doctrine_auto_tags_defender(tmp_path):
    store, fid = _store_with_fit(tmp_path, 17740, [44102])  # has defender launcher
    did = store.add_doctrine("D")
    store.add_fit_to_doctrine(did, fid, ["DPS"])
    mem = next(m for m in store.get_doctrine(did).members if m.fit_id == fid)
    assert "Defenders" in mem.tags and "DPS" in mem.tags


def test_add_fit_to_doctrine_no_defender_no_autotag(tmp_path):
    store, fid = _store_with_fit(tmp_path, 17740, [1, 2])
    did = store.add_doctrine("D")
    store.add_fit_to_doctrine(did, fid, ["DPS"])
    mem = next(m for m in store.get_doctrine(did).members if m.fit_id == fid)
    assert "Defenders" not in mem.tags


def test_set_member_ideal_persists(tmp_path):
    store, fid = _store_with_fit(tmp_path, 17740, [])
    did = store.add_doctrine("D")
    store.add_fit_to_doctrine(did, fid, ["DPS"])
    store.set_member_ideal(did, fid, "percent", 45, 55)
    store.save()
    store2 = FittingsStore(str(tmp_path / "lib.json")); store2.load()
    mem = next(m for m in store2.get_doctrine(did).members if m.fit_id == fid)
    assert (mem.ideal_mode, mem.ideal_min, mem.ideal_max) == ("percent", 45, 55)


def test_doctrine_exemptions_none_omitted_from_json(tmp_path):
    import json
    from fit_models import doctrine_to_dict
    store, fid = _store_with_fit(tmp_path, 17740, [])
    did = store.add_doctrine("D")
    doc = store.get_doctrine(did)
    assert doc.exemptions is None
    # None must NOT be serialized (omitted key).
    assert "exemptions" not in doctrine_to_dict(doc)


def test_set_doctrine_exemptions_list_persists(tmp_path):
    store, fid = _store_with_fit(tmp_path, 17740, [])
    did = store.add_doctrine("D")
    entries = [{"kind": "type", "id": 671, "name": "Erebus"},
               {"kind": "group", "id": 833, "name": "Force Recon Ship"}]
    store.set_doctrine_exemptions(did, entries)
    store.save()
    store2 = FittingsStore(str(tmp_path / "lib.json")); store2.load()
    assert store2.get_doctrine(did).exemptions == entries


def test_set_doctrine_exemptions_empty_list_persists(tmp_path):
    # [] means "explicitly no exemptions" and must round-trip as [], not None.
    store, fid = _store_with_fit(tmp_path, 17740, [])
    did = store.add_doctrine("D")
    store.set_doctrine_exemptions(did, [])
    store.save()
    store2 = FittingsStore(str(tmp_path / "lib.json")); store2.load()
    assert store2.get_doctrine(did).exemptions == []


def test_set_doctrine_exemptions_none_resets(tmp_path):
    # Setting back to None means "use STANDARD_EXEMPTIONS" and is omitted from JSON.
    store, fid = _store_with_fit(tmp_path, 17740, [])
    did = store.add_doctrine("D")
    store.set_doctrine_exemptions(did, [{"kind": "capital"}])
    store.set_doctrine_exemptions(did, None)
    store.save()
    store2 = FittingsStore(str(tmp_path / "lib.json")); store2.load()
    assert store2.get_doctrine(did).exemptions is None


def test_share_import_preserves_per_fit_ideals(tmp_path):
    src = FittingsStore(str(tmp_path / "a.json")); src.load()
    fid = src.add_fit(_fit("Arty Muninn")); did = src.add_doctrine("Shield HACs")
    src.add_fit_to_doctrine(did, fid, ["DPS"])
    src.set_member_ideal(did, fid, "percent", 55, 65)
    payload = src.export_doctrines([did])

    dst = FittingsStore(str(tmp_path / "b.json")); dst.load()
    summary = dst.import_share(payload)
    member = dst.list_doctrines()[0].members[0]
    assert (member.ideal_mode, member.ideal_min, member.ideal_max) == ("percent", 55, 65)


# ── Corrupt-file recovery (ITEM 2) ──────────────────────────────────────────

def test_corrupt_library_is_backed_up_and_degrades_to_empty(tmp_path):
    path = tmp_path / "lib.json"
    original_bytes = b'{ this is not valid json'
    path.write_bytes(original_bytes)

    s = FittingsStore(str(path))
    s.load()                                      # (a) must not raise

    # (b) degrades to an empty store with default tags
    assert s.list_fits() == []
    assert s.list_doctrines() == []
    assert "DPS" in s.tags

    # (c) a "<path>.corrupt" copy of the original bytes is left behind
    backup = tmp_path / "lib.json.corrupt"
    assert backup.exists()
    assert backup.read_bytes() == original_bytes


def test_corrupt_backup_then_save_does_not_destroy_recoverable_data(tmp_path):
    # After a corrupt load + save(), the original is recoverable from .corrupt
    # even though the live file was overwritten with an empty store.
    path = tmp_path / "lib.json"
    original_bytes = b'\x00\x01 totally bogus'
    path.write_bytes(original_bytes)

    s = FittingsStore(str(path))
    s.load()
    s.save()                                      # would have clobbered the original

    assert (tmp_path / "lib.json.corrupt").read_bytes() == original_bytes


def test_corrupt_backup_overwrites_previous_corrupt(tmp_path):
    path = tmp_path / "lib.json"
    backup = tmp_path / "lib.json.corrupt"
    backup.write_bytes(b"stale previous corrupt")

    path.write_bytes(b"new corrupt contents")
    FittingsStore(str(path)).load()

    assert backup.read_bytes() == b"new corrupt contents"


def test_invalid_utf8_library_is_backed_up_and_degrades_to_empty(tmp_path):
    # UnicodeDecodeError case: the bytes are not valid UTF-8.
    path = tmp_path / "lib.json"
    original_bytes = b'\xff\xfe invalid utf8 \x80\x81'
    path.write_bytes(original_bytes)

    s = FittingsStore(str(path))
    s.load()                                      # (a) must not raise

    # (b) degrades to an empty store with default tags
    assert s.list_fits() == []
    assert s.list_doctrines() == []
    assert "DPS" in s.tags

    # (c) a "<path>.corrupt" copy of the original bytes is left behind
    backup = tmp_path / "lib.json.corrupt"
    assert backup.exists()
    assert backup.read_bytes() == original_bytes


# ── rename_tag (ITEM 6a) ────────────────────────────────────────────────────

def test_rename_tag_updates_vocabulary_and_cascades_to_members(tmp_path):
    s = FittingsStore(str(tmp_path / "lib.json")); s.load()
    fid = s.add_fit(_fit())
    d1 = s.add_doctrine("D1"); d2 = s.add_doctrine("D2")
    s.add_fit_to_doctrine(d1, fid, ["DPS", "Links"])
    s.add_fit_to_doctrine(d2, fid, ["DPS"])

    assert s.rename_tag("DPS", "Damage") is True

    # vocabulary renamed (in place), old gone
    assert "Damage" in s.tags and "DPS" not in s.tags
    # cascade across every member that carried it
    m1 = next(m for m in s.get_doctrine(d1).members if m.fit_id == fid)
    m2 = next(m for m in s.get_doctrine(d2).members if m.fit_id == fid)
    assert "Damage" in m1.tags and "DPS" not in m1.tags
    assert "Links" in m1.tags                      # other tags untouched
    assert m2.tags == ["Damage"]

    # persisted: a reload sees the renamed tag and cascaded membership
    s2 = FittingsStore(str(tmp_path / "lib.json")); s2.load()
    assert "Damage" in s2.tags and "DPS" not in s2.tags
    rm = next(m for m in s2.get_doctrine(d1).members if m.fit_id == fid)
    assert "Damage" in rm.tags and "DPS" not in rm.tags


def test_rename_tag_absent_old_is_noop_false(tmp_path):
    s = FittingsStore(str(tmp_path / "lib.json")); s.load()
    before = list(s.tags)
    assert s.rename_tag("NotARealTag", "Whatever") is False
    assert s.tags == before


def test_rename_tag_to_existing_name_is_noop_false(tmp_path):
    s = FittingsStore(str(tmp_path / "lib.json")); s.load()
    # Both DPS and Logistics are default tags.
    before = list(s.tags)
    assert s.rename_tag("DPS", "Logistics") is False
    assert s.tags == before


# ── update_* return values (ITEM 6b) ────────────────────────────────────────

def test_update_fit_returns_true_for_known_false_for_unknown(tmp_path):
    s = FittingsStore(str(tmp_path / "lib.json")); s.load()
    fid = s.add_fit(_fit())
    known = s.get_fit(fid)
    assert s.update_fit(known) is True

    ghost = _fit("Ghost"); ghost.id = "does-not-exist"
    assert s.update_fit(ghost) is False
    assert s.get_fit("does-not-exist") is None     # unknown id not inserted


def test_update_doctrine_returns_true_for_known_false_for_unknown(tmp_path):
    s = FittingsStore(str(tmp_path / "lib.json")); s.load()
    did = s.add_doctrine("D")
    d = s.get_doctrine(did)
    assert s.update_doctrine(d) is True

    d.id = "nope"
    assert s.update_doctrine(d) is False
    assert s.get_doctrine("nope") is None
