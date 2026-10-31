"""Build the baked EWAR cycle-time table inside ``ewar_cycles.py``.

The preview's EWAR icons expire on a window derived from the offending
module's own cycle time (design A1/A1b).  A runtime read of the bundled dogma
blob is not an option: ``fit_dogma.json.gz`` is megabytes unpacked and the
preview tick must pay nothing for it.  So the lookup is a literal dict baked
into ``ewar_cycles.py`` at development time -- a few KB that imports for free.

This is a DEV TOOL.  No runtime module imports it, and a test pins that.

Inputs, both tracked in the repository root:

  ``fit_dogma.json.gz``  gzipped JSON; ``types`` maps a type-id string to a
                         dict whose ``a`` key is a FLAT list
                         ``[attr_id, value, attr_id, value, ...]``.
                         **Attribute 73 is the module's duration in ms.**
  ``fit_types.json``     maps a type-id string to a dict whose ``n`` key is
                         the type name -- the name the gamelog writes after
                         the final " - " of a combat line.

FILTER: every type whose name contains one of :data:`NAME_NEEDLES`, that has
an attribute-73 value.  Types without one are skipped and reported: ECM
scripts and the ECM Jammer Burst Projector carry no duration, and the eight
Abyssal neut/nos types carry no stats at all because theirs are rolled per
item.  Those eight are exactly why ``ewar_cycles`` keeps a size-word fallback.

Measured on SDE build 3494416 (2026-10-30): 151 entries kept, 13 skipped.

OUTPUT: the tool rewrites only the region of ``ewar_cycles.py`` between
:data:`BEGIN_MARKER` and :data:`END_MARKER`, so the hand-written resolver
below the table survives a regeneration.  If the file does not exist yet a
skeleton carrying just the table is written, and the resolver is added by
hand underneath.

Usage::

    py -3.12 tools/build_ewar_cycles.py          # rewrite ewar_cycles.py
    py -3.12 tools/build_ewar_cycles.py --check  # non-zero exit if stale
"""
from __future__ import annotations

import argparse
import datetime
import gzip
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOGMA_PATH = os.path.join(ROOT, "fit_dogma.json.gz")
TYPES_PATH = os.path.join(ROOT, "fit_types.json")
OUT_PATH = os.path.join(ROOT, "ewar_cycles.py")

#: Attribute id of ``duration`` -- a module's cycle time, in milliseconds.
DURATION_ATTR = 73

#: A type is in scope when its name contains one of these.  Mirrors the five
#: detectable effects of the design's section 5.1: ECM covers the jam icon,
#: the two energy families cover neut and nos, and the burst projector is the
#: structure module that logs as "Standup Energy Neutralization Burst
#: Projector".  Tackle is deliberately absent -- it logs once at activation
#: and gets a flat window (design D3), so its cycle time is never consulted.
NAME_NEEDLES = ("ECM", "Energy Neutralizer", "Energy Nosferatu", "Neutralization Burst")

BEGIN_MARKER = "# --- BEGIN GENERATED TABLE: tools/build_ewar_cycles.py ---"
END_MARKER = "# --- END GENERATED TABLE ---"

SKELETON_HEAD = '''"""Per-module EWAR cycle times (generated) and the window resolver."""
'''


def load_inputs(dogma_path: str = DOGMA_PATH, types_path: str = TYPES_PATH):
    """Return ``(dogma_dict, types_dict)`` read from the two bundled files."""
    with gzip.open(dogma_path, "rt", encoding="utf-8") as fh:
        dogma = json.load(fh)
    with open(types_path, "r", encoding="utf-8") as fh:
        types = json.load(fh)
    return dogma, types


def duration_ms(type_entry) -> int | None:
    """Pull attribute 73 out of a dogma type entry's FLAT ``a`` list."""
    if not isinstance(type_entry, dict):
        return None
    flat = type_entry.get("a") or []
    for i in range(0, len(flat) - 1, 2):
        if flat[i] == DURATION_ATTR:
            value = flat[i + 1]
            try:
                ms = int(round(float(value)))
            except (TypeError, ValueError):
                return None
            return ms if ms > 0 else None
    return None


def collect(dogma, types):
    """Return ``(table, skipped)``.

    ``table`` maps module name -> cycle ms, sorted by name; ``skipped`` lists
    ``(name, reason)`` for in-scope types that carry no usable duration.
    """
    dogma_types = dogma.get("types") or {}
    table: dict[str, int] = {}
    skipped: list[tuple[str, str]] = []
    for type_id, info in types.items():
        name = (info or {}).get("n") or ""
        if not any(needle in name for needle in NAME_NEEDLES):
            continue
        entry = dogma_types.get(type_id)
        ms = duration_ms(entry)
        if ms is None:
            skipped.append((name, "no dogma entry" if entry is None else "no attribute 73"))
            continue
        previous = table.get(name)
        if previous is not None and previous != ms:
            raise SystemExit(
                "two types share the name %r with different cycles (%d vs %d)"
                % (name, previous, ms)
            )
        table[name] = ms
    return dict(sorted(table.items())), sorted(skipped)


def render_block(table, skipped, sde_build) -> str:
    """Render the generated region, markers included, as source text."""
    today = datetime.date.today().isoformat()
    lines = [BEGIN_MARKER]
    lines.append("# Generated %s by tools/build_ewar_cycles.py from" % today)
    lines.append("# fit_dogma.json.gz (SDE build %s, attribute 73) + fit_types.json." % sde_build)
    lines.append("# %d modules kept; %d in-scope types carry no duration and rely on" % (len(table), len(skipped)))
    lines.append("# the size-word fallback below:")
    for name, reason in skipped:
        lines.append("#   %s  (%s)" % (name, reason))
    lines.append("# DO NOT EDIT THIS REGION BY HAND -- re-run the tool instead.")
    lines.append("MODULE_CYCLE_MS: dict[str, int] = {")
    for name, ms in table.items():
        lines.append("    %s: %d," % (_py_str(name), ms))
    lines.append("}")
    lines.append(END_MARKER)
    return "\n".join(lines) + "\n"


def _py_str(text: str) -> str:
    """Repr a name as an ASCII-only Python string literal."""
    literal = repr(text)
    literal.encode("ascii")  # the SDE names in scope are ASCII; prove it
    return literal


def splice(existing: str, block: str) -> str:
    """Put ``block`` where the markers are, keeping everything around them."""
    if not existing:
        return SKELETON_HEAD + "\n\n" + block
    begin = existing.find(BEGIN_MARKER)
    end = existing.find(END_MARKER)
    if begin < 0 or end < 0:
        raise SystemExit(
            "%s exists but carries no generated-table markers; add\n  %s\n  %s\n"
            "around the table, or delete the file and re-run."
            % (os.path.basename(OUT_PATH), BEGIN_MARKER, END_MARKER)
        )
    tail = existing[end + len(END_MARKER):].lstrip("\n")
    return existing[:begin] + block + ("\n" + tail if tail else "")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="exit non-zero when the baked table is stale")
    args = parser.parse_args(argv)

    dogma, types = load_inputs()
    table, skipped = collect(dogma, types)
    if not table:
        raise SystemExit("no modules matched -- the filter or the inputs are wrong")
    block = render_block(table, skipped, dogma.get("v"))

    existing = ""
    if os.path.exists(OUT_PATH):
        with open(OUT_PATH, "r", encoding="utf-8", newline="") as fh:
            # Normalise to "\n" here and write with newline="\r\n" below, so a
            # file that is already CRLF does not grow a second carriage
            # return on every line.  The repository is CRLF throughout.
            existing = fh.read().replace("\r\n", "\n")
    updated = splice(existing, block)

    if args.check:
        # Compare the TABLE only: the header carries a generation date, and a
        # date is not staleness.
        stale = _table_of(existing) != table
        print("%s: %s (%d modules)"
              % (os.path.basename(OUT_PATH), "STALE" if stale else "up to date", len(table)))
        return 1 if stale else 0

    if updated != existing:
        with open(OUT_PATH, "w", encoding="utf-8", newline="\r\n") as fh:
            fh.write(updated)
        print("wrote %s: %d modules, %d skipped" % (OUT_PATH, len(table), len(skipped)))
    else:
        print("%s already up to date (%d modules)" % (OUT_PATH, len(table)))
    return 0


def _table_of(source: str) -> dict:
    """Read MODULE_CYCLE_MS back out of generated source, without importing."""
    import ast

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    for node in ast.walk(tree):
        target = None
        if isinstance(node, ast.AnnAssign):
            target = node.target
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == "MODULE_CYCLE_MS" and node.value:
            try:
                return ast.literal_eval(node.value)
            except ValueError:
                return {}
    return {}


if __name__ == "__main__":
    sys.exit(main())
