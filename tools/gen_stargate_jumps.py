"""
Generate stargate_jumps.json - the bundled New Eden stargate graph.

Reads the Fuzzwork SDE mapSolarSystemJumps.csv (one row per gate direction),
checks it, and writes {"<system id>": [neighbour ids, ...]} to the repo root.

Readers that depend on this exact shape (string system-id keys, list of int
neighbour ids, NO metadata key - every key is int()-ed):
    jump_range._load_stargate_graph   (routing, gate-jump balls)
    map_data.load_map_model           (the map's gate lines)

Output is deterministic: keys in numeric order, each neighbour list sorted,
json's default separators on one line, so a refresh diffs readably.

Run manually after a CCP patch that adds or removes stargates:
    py -3.12 tools/gen_stargate_jumps.py --download
    py -3.12 tools/gen_stargate_jumps.py --csv path/to/mapSolarSystemJumps.csv --out stargate_jumps.json
Exit status: 0 written, 2 a sanity gate failed (nothing written).
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys

# The /csv/ subdirectory is REQUIRED -- the bare .../latest/<file>.csv 404s.
CSV_URL = "https://www.fuzzwork.co.uk/dump/latest/csv/mapSolarSystemJumps.csv"
OUT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "stargate_jumps.json")

K_SPACE_MIN = 30_000_000
K_SPACE_MAX = 30_999_999
# Floor on undirected gate links. 6,888 in the 2025 bundle, 6,991 at SDE build
# 3693310 (2026-11-12). A truncated or reformatted download lands far below.
MIN_LINKS = 6_800


def parse_jumps(text: str) -> dict[int, set[int]]:
    """Directed adjacency from the CSV text, exactly as listed (no symmetrising,
    so check_graph can see a one-way row)."""
    graph: dict[int, set[int]] = {}
    for row in csv.DictReader(io.StringIO(text.lstrip(chr(0xFEFF)))):
        a = int(row["fromSolarSystemID"])
        b = int(row["toSolarSystemID"])
        graph.setdefault(a, set()).add(b)
    return graph


def links(graph: dict[int, set[int]]) -> set[tuple[int, int]]:
    """Undirected links as (low id, high id) pairs."""
    return {(min(a, b), max(a, b)) for a, nbrs in graph.items() for b in nbrs}


def check_graph(graph: dict[int, set[int]], min_links: int = MIN_LINKS) -> list[str]:
    """Every sanity-gate failure, as text. Empty list = the graph is usable."""
    errors: list[str] = []
    for a, nbrs in graph.items():
        if not (K_SPACE_MIN <= a <= K_SPACE_MAX):
            errors.append(f"system {a} is outside K-space")
        if a in nbrs:
            errors.append(f"self-link on {a}")
        for b in nbrs:
            if b not in graph:
                errors.append(f"neighbour {b} of {a} is not a key")
            elif a not in graph[b]:
                errors.append(f"one-way link {a} -> {b}")
    n = len(links(graph))
    if n < min_links:
        errors.append(f"only {n} links (floor {min_links})")
    return errors


def serialize(graph: dict[int, set[int]]) -> str:
    """The file text: numeric key order, sorted neighbour lists, one line."""
    return json.dumps({str(a): sorted(graph[a]) for a in sorted(graph)})


def _read_existing(path: str) -> dict[int, set[int]] | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return {int(k): set(v) for k, v in json.load(f).items()}
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _download() -> str:
    import requests  # repo dependency; imported here so tests need no network stack
    print(f"Downloading {CSV_URL} ...")
    resp = requests.get(CSV_URL, timeout=120)
    resp.raise_for_status()
    return resp.content.decode("utf-8-sig")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", help="local mapSolarSystemJumps.csv")
    src.add_argument("--download", action="store_true", help="fetch the CSV from Fuzzwork")
    ap.add_argument("--out", default=OUT_PATH, help="output path (default: repo root)")
    ap.add_argument("--min-links", type=int, default=MIN_LINKS)
    args = ap.parse_args(argv)

    if args.csv:
        with open(args.csv, "r", encoding="utf-8-sig") as f:
            text = f.read()
    else:
        text = _download()

    graph = parse_jumps(text)
    errors = check_graph(graph, args.min_links)
    if errors:
        for e in errors[:20]:
            print(f"FATAL: {e}")
        print(f"{len(errors)} sanity failure(s); {args.out} left unchanged")
        return 2

    new_links = links(graph)
    old = _read_existing(args.out)
    if old is not None:
        old_links = links(old)
        print(f"before: {len(old)} systems, {len(old_links)} links")
        for a, b in sorted(old_links - new_links):
            print(f"  REMOVED {a} - {b}")
        print(f"  {len(new_links - old_links)} added, {len(old_links - new_links)} removed")

    with open(args.out, "w", encoding="utf-8", newline="") as f:
        f.write(serialize(graph))
    print(f"Wrote {len(graph)} systems, {len(new_links)} links to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
