"""Generate inv_groups.json + inv_categories.json from Fuzzwork SDE CSVs.

Run manually when the SDE updates:  python tools/gen_inv_groups.py
Outputs land at repo root and are bundled via FCTool.spec datas.
"""
import csv
import io
import json
import os

import requests

# NOTE: the /csv/ segment is REQUIRED — the bare /dump/latest/<file>.csv
# 404s (verified live 2026-07-12; same gotcha documented in
# tools/gen_fit_types.py:55-56 and the jump-range research).
BASE = "https://www.fuzzwork.co.uk/dump/latest/csv/"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _fetch_csv(name):
    r = requests.get(BASE + name, timeout=60)
    r.raise_for_status()
    # utf-8-sig: Fuzzwork CSVs start with a UTF-8 BOM; plain utf-8 glues
    # U+FEFF onto the first header name and breaks DictReader key lookups
    # (verified live; same handling as tools/gen_fit_types.py:107-108).
    return csv.DictReader(io.StringIO(r.content.decode("utf-8-sig")))


def main():
    cats = {}
    for row in _fetch_csv("invCategories.csv"):
        cats[row["categoryID"]] = row["categoryName"]

    groups = {}
    for row in _fetch_csv("invGroups.csv"):
        groups[row["groupID"]] = {
            "name": row["groupName"],
            "cat": int(row["categoryID"]),
            "pub": row.get("published", "1") in ("1", "True", "true"),
        }

    for name, data in (("inv_groups.json", groups), ("inv_categories.json", cats)):
        path = os.path.join(ROOT, name)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        print(f"wrote {path} ({len(data)} entries)")


if __name__ == "__main__":
    main()
