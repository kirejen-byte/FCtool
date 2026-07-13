"""EVE overview YAML <-> canonical pack.

Real exports are plain YAML (no python tags) in list-of-pairs style; the
loader still tolerates !!python/tuple defensively (legacy files in the wild).
Emit choices (UTF-8, no BOM, CRLF, block style, alphabetical top-level keys)
match the client's own emission, pinned by the golden export SecFCTool.yaml
(G1 DONE 2026-07-12) and locked by tests/test_overview_golden.py.
Facts: R §A.2, §A.10, R §2 addendum.
"""
from __future__ import annotations

import os

import yaml

import overview_schema as osch


class _EveSafeLoader(yaml.SafeLoader):
    pass


def _construct_tuple_as_list(loader, node):
    return list(loader.construct_sequence(node, deep=True))


_EveSafeLoader.add_constructor(
    "tag:yaml.org,2002:python/tuple", _construct_tuple_as_list)


def loads(text: str) -> osch.OverviewPack:
    try:
        wire = yaml.load(text, Loader=_EveSafeLoader)
    except yaml.YAMLError as e:
        raise ValueError(f"invalid YAML: {e}") from e
    if not isinstance(wire, dict):
        raise ValueError("not an overview settings file (top level is not a map)")
    pack = osch.from_wire(wire)
    for p in pack.presets or []:
        p.name = osch.unescape_markup(p.name)
    for t in pack.tabs or []:
        t.name = osch.unescape_markup(t.name)
        t.overview_preset = osch.unescape_markup(t.overview_preset)
        t.bracket_preset = osch.unescape_markup(t.bracket_preset)
    return pack


def load_file(path: str) -> osch.OverviewPack:
    try:
        with open(path, "rb") as f:
            text = f.read().decode("utf-8-sig")
    except OSError as e:
        raise ValueError(f"cannot read {path}: {e}") from e
    try:
        return loads(text)
    except ValueError as e:
        raise ValueError(f"{os.path.basename(path)}: {e}") from e


def emit(pack: osch.OverviewPack) -> str:
    return yaml.safe_dump(
        osch.to_wire(pack),
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=True,           # top level only; sections are lists (order kept)
        width=100000,             # never wrap long markup names
    )


def write_file(pack: osch.OverviewPack, path: str) -> None:
    # CRLF: the client's own export is CRLF-only (golden, 2026-07-12) —
    # match its byte style. emit() produces LF, so this translation is total.
    data = emit(pack).replace("\n", "\r\n").encode("utf-8")
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:      # bytes mode => exact newline control
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
