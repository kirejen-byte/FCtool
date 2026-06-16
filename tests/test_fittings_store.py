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
