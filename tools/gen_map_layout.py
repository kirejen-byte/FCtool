"""Generate map_layout.json (2D star-map layout) from CCP's new-SDE JSONL export.

Source of truth: mapSolarSystems `position2D` (the in-game 2D map's schematic
layout, redistributable under the CCP Developer License). Systems missing
position2D get an affine-fitted fallback from their raw 3D x/-z projection.

Render mapping (CCP map-data guide): x_render = position2D.x,
y_render = -position2D.y. Output coordinates are ALREADY render-space.

Usage:
  py -3.13 tools/gen_map_layout.py --sde-zip tools/_cache/sde.zip --out map_layout.json
  py -3.13 tools/gen_map_layout.py --download --out map_layout.json
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
import zipfile
from pathlib import Path

SDE_ZIP_URL = "https://developers.eveonline.com/static-data/eve-online-static-data-latest-jsonl.zip"
SDE_BUILD_URL = "https://developers.eveonline.com/static-data/tranquility/latest.jsonl"
CACHE_DIR = Path(__file__).resolve().parent / "_cache"
KSPACE_MIN, KSPACE_MAX = 30_000_000, 30_999_999


def _iter_jsonl(lines):
    for line in lines:
        line = line.strip()
        if line:
            yield json.loads(line)


def extract_systems(lines) -> dict[int, dict]:
    """K-space systems: id -> {x2d,y2d (render-space, may be None), x3d, z3d, region_id, name}."""
    out: dict[int, dict] = {}
    for rec in _iter_jsonl(lines):
        sid = rec.get("_key")
        if not isinstance(sid, int) or not (KSPACE_MIN <= sid <= KSPACE_MAX):
            continue
        pos = rec.get("position") or {}
        p2d = rec.get("position2D")
        out[sid] = {
            "x2d": float(p2d["x"]) if p2d else None,
            "y2d": -float(p2d["y"]) if p2d else None,  # render-space flip
            "x3d": float(pos.get("x", 0.0)),
            "z3d": float(pos.get("z", 0.0)),
            "region_id": int(rec.get("regionID", 0)),
            "name": (rec.get("name") or {}).get("en", str(sid)),
        }
    return out


def extract_regions(lines) -> dict[int, str]:
    return {rec["_key"]: (rec.get("name") or {}).get("en", str(rec["_key"]))
            for rec in _iter_jsonl(lines) if isinstance(rec.get("_key"), int)}


def _bbox_affine(src: list[float], dst: list[float]):
    """Linear map [min(src),max(src)] -> [min(dst),max(dst)]; identity-safe."""
    s0, s1, d0, d1 = min(src), max(src), min(dst), max(dst)
    scale = (d1 - d0) / (s1 - s0) if s1 != s0 else 1.0
    return lambda v: d0 + (v - s0) * scale


def apply_fallback(systems: dict[int, dict]) -> list[int]:
    """Fill x2d/y2d for systems lacking position2D. Returns sorted fallback ids."""
    donors = [s for s in systems.values() if s["x2d"] is not None]
    missing = sorted(sid for sid, s in systems.items() if s["x2d"] is None)
    if not missing:
        return []
    if not donors:
        raise SystemExit("FATAL: no system carries position2D — SDE format changed?")
    fx = _bbox_affine([d["x3d"] for d in donors], [d["x2d"] for d in donors])
    fy = _bbox_affine([-d["z3d"] for d in donors], [d["y2d"] for d in donors])
    for sid in missing:
        s = systems[sid]
        s["x2d"], s["y2d"] = fx(s["x3d"]), fy(-s["z3d"])
    return missing


def nudge_anchors(anchors: dict[int, list[float]], min_dist: float, rounds: int = 10) -> None:
    """Push apart region label anchors closer than min_dist (in place, deterministic)."""
    ids = sorted(anchors)
    for _ in range(rounds):
        moved = False
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                ax, ay = anchors[a]
                bx, by = anchors[b]
                dx, dy = bx - ax, by - ay
                d = (dx * dx + dy * dy) ** 0.5
                if d >= min_dist:
                    continue
                if d < 1e-9:
                    dx, dy, d = 1.0, 0.0, 1.0  # coincident: split along +x
                push = (min_dist - d) / 2.0
                ux, uy = dx / d, dy / d
                anchors[a] = [ax - ux * push, ay - uy * push]
                anchors[b] = [bx + ux * push, by + uy * push]
                moved = True
        if not moved:
            return


def build_layout(systems: dict[int, dict], regions: dict[int, str]) -> dict:
    fallback = apply_fallback(systems)
    by_region: dict[int, list[tuple[float, float]]] = {}
    for s in systems.values():
        by_region.setdefault(s["region_id"], []).append((s["x2d"], s["y2d"]))
    anchors = {rid: [sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)]
               for rid, pts in by_region.items()}
    xs = [s["x2d"] for s in systems.values()]
    ys = [s["y2d"] for s in systems.values()]
    diag = ((max(xs) - min(xs)) ** 2 + (max(ys) - min(ys)) ** 2) ** 0.5 if len(xs) > 1 else 1.0
    nudge_anchors(anchors, min_dist=diag * 0.02)
    return {
        "generated": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "sde_build": "unknown",
        "systems": {str(sid): {"x": round(s["x2d"], 3), "y": round(s["y2d"], 3)}
                    for sid, s in sorted(systems.items())},
        "regions": {str(rid): {"name": regions.get(rid, str(rid)),
                               "lx": round(a[0], 3), "ly": round(a[1], 3)}
                    for rid, a in sorted(anchors.items())},
        "fallback": fallback,
    }


def _read_zip_dataset(zpath: Path, needle: str) -> list[str]:
    with zipfile.ZipFile(zpath) as z:
        names = [n for n in z.namelist() if needle.lower() in n.lower() and n.lower().endswith(".jsonl")]
        if not names:
            raise SystemExit(f"FATAL: no *{needle}*.jsonl inside {zpath}")
        with z.open(names[0]) as f:
            return f.read().decode("utf-8").splitlines()


def _download(force: bool) -> tuple[Path, str]:
    import requests  # repo dependency; import here so tests need no network stack
    CACHE_DIR.mkdir(exist_ok=True)
    zpath = CACHE_DIR / "sde_latest_jsonl.zip"
    if force or not zpath.exists():
        print(f"downloading {SDE_ZIP_URL} …")
        r = requests.get(SDE_ZIP_URL, timeout=300)
        r.raise_for_status()
        zpath.write_bytes(r.content)
    build = "unknown"
    try:
        meta = requests.get(SDE_BUILD_URL, timeout=30)
        if meta.ok and meta.text.strip():
            build = str(json.loads(meta.text.strip().splitlines()[0]).get("buildNumber", "unknown"))
    except Exception as exc:  # build tag is cosmetic; never fail generation on it
        print(f"warn: build lookup failed: {exc}")
    return zpath, build


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--sde-zip", type=Path, help="path to SDE jsonl zip")
    src.add_argument("--download", action="store_true", help="fetch latest SDE zip (cached)")
    ap.add_argument("--out", type=Path, default=Path("map_layout.json"))
    ap.add_argument("--build", default=None, help="override sde_build tag")
    ap.add_argument("--force", action="store_true", help="re-download even if cached")
    args = ap.parse_args(argv)

    build = args.build or "unknown"
    zpath = args.sde_zip
    if args.download:
        zpath, dl_build = _download(args.force)
        build = args.build or dl_build

    systems = extract_systems(_read_zip_dataset(zpath, "mapSolarSystems"))
    regions = extract_regions(_read_zip_dataset(zpath, "mapRegions"))
    layout = build_layout(systems, regions)
    layout["sde_build"] = build
    args.out.write_text(json.dumps(layout, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {args.out}: {len(layout['systems'])} systems, "
          f"{len(layout['regions'])} regions, {len(layout['fallback'])} fallback")
    return 0


if __name__ == "__main__":
    sys.exit(main())
