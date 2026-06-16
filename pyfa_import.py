"""Read fits from a local pyfa `saveddata.db` (read-only, defensive).

pyfa stores all fits in one SQLite file (`~/.pyfa/saveddata.db`); the schema is
internal and has been migrated 49+ times, so we open it **read-only / immutable**
and never `SELECT *` — `PRAGMA table_info` tells us which columns actually exist
so an older schema (e.g. missing `chargeID`) still reads. Type IDs only are
stored, so a `catalog` resolves names/slots/categories.

`find_pyfa_db` locates the DB; `list_pyfa_fits` lists fits by ship + name;
`read_pyfa_fit` builds a `ParsedFit`. Any failure raises `PyfaImportError` so the
GUI can fall back to importing a pyfa EFT-text export.

No Tkinter, no network.
"""

from __future__ import annotations

import os
import sqlite3

from fit_models import CargoStack, DroneStack, ParsedFit, ParsedModule


class PyfaImportError(Exception):
    """Raised when a pyfa database cannot be read (unreadable schema, missing
    table, locked file, etc.). The GUI offers EFT-text import as a fallback."""


def _connect(db_path: str) -> sqlite3.Connection:
    """Open a pyfa DB read-only and immutable so we never mutate the user's
    file and don't take a write lock on a DB pyfa may have open."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    """Return the set of column names present in `table` (empty if absent)."""
    try:
        rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.Error:
        return set()
    return {row["name"] for row in rows}


def find_pyfa_db(start_dir: str | None = None) -> str | None:
    """Locate a pyfa `saveddata.db`.

    With `start_dir`, look for `saveddata.db` directly inside it. Otherwise fall
    back to the default `~/.pyfa/saveddata.db` (the same path on Windows via
    `%USERPROFILE%\\.pyfa`). Returns the path if it exists, else None.
    """
    if start_dir:
        candidate = os.path.join(start_dir, "saveddata.db")
        return candidate if os.path.exists(candidate) else None
    default = os.path.join(os.path.expanduser("~"), ".pyfa", "saveddata.db")
    return default if os.path.exists(default) else None


def list_pyfa_fits(db_path: str) -> list[dict]:
    """List fits as `[{fit_id, ship_type_id, name}]`, ordered by name."""
    try:
        con = _connect(db_path)
        try:
            rows = con.execute(
                "SELECT ID, shipID, name FROM fits ORDER BY name"
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error as exc:
        raise PyfaImportError(f"Could not list pyfa fits: {exc}") from exc

    return [
        {
            "fit_id": row["ID"],
            "ship_type_id": row["shipID"],
            "name": row["name"],
        }
        for row in rows
    ]


def read_pyfa_fit(db_path: str, fit_id: int, catalog) -> ParsedFit:
    """Build a `ParsedFit` from a single pyfa fit.

    Reads modules (skipping rows with a NULL `itemID` — pyfa's empty slots),
    cargo, and drones, resolving names/slots/categories via `catalog`. Optional
    columns (`chargeID`, `amount`, …) are only selected when present so an older
    pyfa schema still imports. Raises `PyfaImportError` on any DB failure.
    """
    try:
        con = _connect(db_path)
        try:
            return _read_fit(con, fit_id, catalog)
        finally:
            con.close()
    except sqlite3.Error as exc:
        raise PyfaImportError(f"Could not read pyfa fit {fit_id}: {exc}") from exc


def _read_fit(con: sqlite3.Connection, fit_id: int, catalog) -> ParsedFit:
    fit_row = con.execute(
        "SELECT ID, shipID, name FROM fits WHERE ID = ?", (fit_id,)
    ).fetchone()
    if fit_row is None:
        raise PyfaImportError(f"pyfa fit {fit_id} not found")

    ship_type_id = fit_row["shipID"]
    ship_name = catalog.resolve_name(ship_type_id) or ""

    modules = _read_modules(con, fit_id, catalog)
    drones = _read_drones(con, fit_id, catalog)
    cargo = _read_cargo(con, fit_id, catalog)
    subsystems = [m.type_id for m in modules if m.slot == "subsystem"]
    modules = [m for m in modules if m.slot != "subsystem"]

    return ParsedFit(
        ship_type_id=ship_type_id,
        ship_name=ship_name,
        modules=modules,
        drones=drones,
        cargo=cargo,
        subsystems=subsystems,
    )


def _read_modules(con, fit_id, catalog) -> list[ParsedModule]:
    cols = _columns(con, "modules")
    if "itemID" not in cols or "fitID" not in cols:
        return []
    has_charge = "chargeID" in cols
    has_state = "state" in cols
    select_cols = ["itemID"]
    if has_charge:
        select_cols.append("chargeID")
    if has_state:
        select_cols.append("state")
    order = " ORDER BY position" if "position" in cols else ""
    sql = (
        f"SELECT {', '.join(select_cols)} FROM modules "
        f"WHERE fitID = ? AND itemID IS NOT NULL{order}"
    )
    rows = con.execute(sql, (fit_id,)).fetchall()

    modules: list[ParsedModule] = []
    for row in rows:
        type_id = row["itemID"]
        if type_id is None:
            continue
        name = catalog.resolve_name(type_id) or str(type_id)
        slot = catalog.slot_of(type_id) or ""
        charge_type_id = None
        charge_name = None
        if has_charge:
            cid = row["chargeID"]
            if cid is not None:
                charge_type_id = cid
                charge_name = catalog.resolve_name(cid)
        # pyfa module state: a value of 0 means offline (online states are > 0).
        offline = bool(has_state and row["state"] == 0)
        modules.append(
            ParsedModule(
                type_id=type_id,
                name=name,
                slot=slot,
                charge_type_id=charge_type_id,
                charge_name=charge_name,
                offline=offline,
            )
        )
    return modules


def _read_drones(con, fit_id, catalog) -> list[DroneStack]:
    cols = _columns(con, "drones")
    if "itemID" not in cols or "fitID" not in cols:
        return []
    has_amount = "amount" in cols
    select_cols = ["itemID"] + (["amount"] if has_amount else [])
    sql = (
        f"SELECT {', '.join(select_cols)} FROM drones "
        f"WHERE fitID = ? AND itemID IS NOT NULL"
    )
    rows = con.execute(sql, (fit_id,)).fetchall()
    drones: list[DroneStack] = []
    for row in rows:
        type_id = row["itemID"]
        if type_id is None:
            continue
        amount = row["amount"] if has_amount and row["amount"] is not None else 0
        name = catalog.resolve_name(type_id) or str(type_id)
        drones.append(DroneStack(type_id, name, amount))
    return drones


def _read_cargo(con, fit_id, catalog) -> list[CargoStack]:
    cols = _columns(con, "cargo")
    if "itemID" not in cols or "fitID" not in cols:
        return []
    has_amount = "amount" in cols
    select_cols = ["itemID"] + (["amount"] if has_amount else [])
    sql = (
        f"SELECT {', '.join(select_cols)} FROM cargo "
        f"WHERE fitID = ? AND itemID IS NOT NULL"
    )
    rows = con.execute(sql, (fit_id,)).fetchall()
    cargo: list[CargoStack] = []
    for row in rows:
        type_id = row["itemID"]
        if type_id is None:
            continue
        amount = row["amount"] if has_amount and row["amount"] is not None else 0
        name = catalog.resolve_name(type_id) or str(type_id)
        cargo.append(CargoStack(type_id, name, amount))
    return cargo
