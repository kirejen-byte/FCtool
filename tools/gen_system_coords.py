"""
Generate system_coords.json — the bundled New Eden coordinate table.

Downloads the Fuzzwork SDE mapSolarSystems.csv, keeps only K-space systems
(30,000,000-30,999,999), and writes a compact id->record JSON to the repo root.

Run manually to refresh after a CCP expansion that adds/moves systems:
    py -3.13 tools/gen_system_coords.py
Coordinates are part of the static universe and change very rarely.
"""
import csv
import io
import json
import os

import requests

# The /csv/ subdirectory is REQUIRED -- the bare .../latest/mapSolarSystems.csv 404s.
CSV_URL = "https://www.fuzzwork.co.uk/dump/latest/csv/mapSolarSystems.csv"
OUT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "system_coords.json")

K_SPACE_MIN = 30_000_000
K_SPACE_MAX = 30_999_999


def main() -> None:
    print(f"Downloading {CSV_URL} ...")
    resp = requests.get(CSV_URL, timeout=60)
    resp.raise_for_status()
    # The CSV is UTF-8 with a BOM; utf-8-sig strips it so the first column key
    # is "regionID" and not a BOM-prefixed variant.
    text = resp.content.decode("utf-8-sig")

    reader = csv.DictReader(io.StringIO(text))
    table: dict[str, dict] = {}
    for row in reader:
        sid = int(row["solarSystemID"])
        if not (K_SPACE_MIN <= sid <= K_SPACE_MAX):
            continue
        table[str(sid)] = {
            "name": row["solarSystemName"],
            "x": float(row["x"]),
            "y": float(row["y"]),
            "z": float(row["z"]),
            "region_id": int(row["regionID"]),
            "security": float(row["security"]),  # full-precision true-sec
        }

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(table, f, separators=(",", ":"))
    print(f"Wrote {len(table)} K-space systems to {OUT_PATH}")
    jita = table.get("30000142")
    assert jita and jita["security"] >= 0.45, "Jita missing or not highsec -- bad data"
    print(f"Sanity OK: Jita security={jita['security']}")


if __name__ == "__main__":
    main()
