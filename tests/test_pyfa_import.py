import sqlite3
from pyfa_import import list_pyfa_fits, read_pyfa_fit


def _make_db(tmp_path):
    p = str(tmp_path / "saveddata.db")
    con = sqlite3.connect(p)
    con.executescript("""
        CREATE TABLE fits(ID INTEGER PRIMARY KEY, shipID INTEGER, name TEXT);
        CREATE TABLE modules(ID INTEGER PRIMARY KEY, fitID INTEGER, itemID INTEGER,
                             chargeID INTEGER, state INTEGER, position INTEGER);
        CREATE TABLE cargo(ID INTEGER PRIMARY KEY, fitID INTEGER, itemID INTEGER, amount INTEGER);
        CREATE TABLE drones(groupID INTEGER PRIMARY KEY, fitID INTEGER, itemID INTEGER, amount INTEGER);
        INSERT INTO fits VALUES (1, 12015, 'Arty Muninn');
        INSERT INTO modules VALUES (1, 1, 2048, NULL, 1, 0);
        INSERT INTO modules VALUES (2, 1, 2185, 215, 1, 8);
        INSERT INTO modules VALUES (3, 1, NULL, NULL, 0, 9);     -- empty slot (itemID NULL)
        INSERT INTO cargo VALUES (1, 1, 216, 1000);
        INSERT INTO drones VALUES (1, 1, 12058, 5);
    """)
    con.commit(); con.close()
    return p


class Cat:
    def resolve_name(self, tid): return {12015:"Muninn",2048:"DCII",2185:"Gun",215:"EMP",216:"Plasma",12058:"Hob"}.get(tid)
    def slot_of(self, tid): return {2048:"low",2185:"high"}.get(tid)
    def category_of(self, tid): return {12058:"drone",215:"charge",216:"charge"}.get(tid,"module")


def test_list_fits(tmp_path):
    fits = list_pyfa_fits(_make_db(tmp_path))
    assert fits == [{"fit_id": 1, "ship_type_id": 12015, "name": "Arty Muninn"}]


def test_read_fit_builds_parsedfit(tmp_path):
    fit = read_pyfa_fit(_make_db(tmp_path), 1, Cat())
    assert fit.ship_type_id == 12015
    assert any(m.type_id == 2185 and m.charge_type_id == 215 for m in fit.modules)
    assert any(d.type_id == 12058 and d.quantity == 5 for d in fit.drones)
    assert any(c.type_id == 216 and c.quantity == 1000 for c in fit.cargo)
    assert all(m.type_id is not None for m in fit.modules)        # NULL itemID row skipped


def test_offline_only_for_pyfa_offline_state(tmp_path):
    # pyfa State enum: OFFLINE=-1, ONLINE=0, ACTIVE=1, OVERHEATED=2.
    # Only state == -1 is offline; online/active modules must NOT be flagged offline.
    p = str(tmp_path / "states.db"); con = sqlite3.connect(p)
    con.executescript("""
        CREATE TABLE fits(ID INTEGER PRIMARY KEY, shipID INTEGER, name TEXT);
        CREATE TABLE modules(ID INTEGER PRIMARY KEY, fitID INTEGER, itemID INTEGER,
                             chargeID INTEGER, state INTEGER, position INTEGER);
        INSERT INTO fits VALUES (1, 12015, 'States');
        INSERT INTO modules VALUES (1, 1, 2048, NULL, 0, 0);   -- ONLINE  -> not offline
        INSERT INTO modules VALUES (2, 1, 2185, NULL, -1, 1);  -- OFFLINE -> offline
        INSERT INTO modules VALUES (3, 1, 2048, NULL, 1, 2);   -- ACTIVE  -> not offline
    """)
    con.commit(); con.close()
    fit = read_pyfa_fit(p, 1, Cat())
    offline_flags = [m.offline for m in fit.modules]  # ordered by position
    assert offline_flags == [False, True, False], offline_flags


def test_null_state_is_not_offline(tmp_path):
    # a module with NULL state must default to online (not offline).
    p = str(tmp_path / "nullstate.db"); con = sqlite3.connect(p)
    con.executescript("""
        CREATE TABLE fits(ID INTEGER PRIMARY KEY, shipID INTEGER, name TEXT);
        CREATE TABLE modules(ID INTEGER PRIMARY KEY, fitID INTEGER, itemID INTEGER,
                             chargeID INTEGER, state INTEGER, position INTEGER);
        INSERT INTO fits VALUES (1, 12015, 'NullState');
        INSERT INTO modules VALUES (1, 1, 2048, NULL, NULL, 0);
    """)
    con.commit(); con.close()
    fit = read_pyfa_fit(p, 1, Cat())
    assert fit.modules[0].offline is False


def test_missing_optional_column_is_tolerated(tmp_path):
    # a DB without chargeID should still read (older pyfa schema)
    p = str(tmp_path / "old.db"); con = sqlite3.connect(p)
    con.executescript("CREATE TABLE fits(ID INTEGER PRIMARY KEY, shipID INTEGER, name TEXT);"
                      "CREATE TABLE modules(ID INTEGER PRIMARY KEY, fitID INTEGER, itemID INTEGER, position INTEGER);"
                      "INSERT INTO fits VALUES(1,12015,'X'); INSERT INTO modules VALUES(1,1,2048,0);")
    con.commit(); con.close()
    fit = read_pyfa_fit(p, 1, Cat())
    assert fit.ship_type_id == 12015
