"""Dual-dialect EVE clipboard parser for friendly-infrastructure imports.

Parses OS-clipboard text captured from the in-game structure browser into a
flat list of `ParsedEntry` records. Three markup/line shapes are recognized,
plus a plain-text fallback:

- **Formatted paste** ("Copy Selected With Formatting"): `<font>`-wrapped
  ``<a href="showinfo:TYPEID//STRUCTUREID">label</a>`` links; the `<font>`
  scaffolding around/between links is ignored.
- **URL dialect** (a second in-game copy path; also what SMT's own importer
  parses): ``<url=showinfo:TYPEID//STRUCTUREID>label</url>``.
- **SMT plain line form**: ``STRUCTUREID From --> To`` (one per line). Always
  a gate — there is no type_id in this line shape.
- **Plain copy** (bare Ctrl-C, no markup): display names only, separated by
  runs of 2+ whitespace or newlines. Used only when no markup links (either
  dialect) are found at all — the leftovers in a markup paste are `<font>`
  scaffolding, not real plain entries.

Pure stdlib, Tk-free, network-free. `categorize()` / `TYPE_CATEGORY` are the
single source of truth for structure category classification shared by the
rest of the infra_* feature (infra_store, infra_scan, infra_overlay).
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

# ── Categories & type map (single source of truth) ────────────────────────────
CATEGORIES = ("citadel", "engineering", "refinery", "gate", "flex", "npc", "unknown")

TYPE_CATEGORY = {
    35832: "citadel", 35833: "citadel", 35834: "citadel",            # Astrahus, Fortizar, Keepstar
    47512: "citadel", 47513: "citadel", 47514: "citadel",            # faction Fortizars
    47515: "citadel", 47516: "citadel",
    35825: "engineering", 35826: "engineering", 35827: "engineering",# Raitaru, Azbel, Sotiyo
    35835: "refinery", 35836: "refinery", 81826: "refinery",         # Athanor, Tatara, Metenox
    35841: "gate",                                                   # Ansiblex
    35840: "flex", 37534: "flex",                                    # Pharolux, Tenebrex
}


def categorize(type_id: int | None, structure_id: int | None) -> str:
    if structure_id is not None and structure_id < 1_000_000_000:
        return "npc"                       # NPC stations: ids ~6.0e7 (fixture-proven)
    return TYPE_CATEGORY.get(type_id or 0, "unknown")


# ── Public dataclasses ──────────────────────────────────────────────────────
@dataclass
class ParsedEntry:
    name: str
    type_id: int | None = None
    structure_id: int | None = None
    category: str = "unknown"            # categorize() applied
    system_name: str = ""                # leading "SYS - " prefix if present, else ""
    gate_to_system_name: str | None = None   # for "A » B - label" names: system_name=A, gate_to=B


@dataclass
class ParseResult:
    entries: list[ParsedEntry] = field(default_factory=list)
    total_links: int = 0                 # markup links seen (either dialect)
    plain_names: int = 0                 # entries produced by the no-markup fallback
    unparsed: list[str] = field(default_factory=list)   # chunks we could not classify


# ── Regexes ──────────────────────────────────────────────────────────────────
RE_A_HREF = re.compile(r'<a\s+href="showinfo:(\d+)//(\d+)"[^>]*>(.*?)</a>', re.I | re.S)
RE_URL = re.compile(r'<url=showinfo:(\d+)//(\d+)>(.*?)</url>', re.I | re.S)
RE_SMT = re.compile(r'^[ \t]*(\d{9,})[ \t]+(.+?)[ \t]+-->[ \t]+(.+?)[ \t]*$', re.M)
PLAIN_SPLIT = re.compile(r'\s{2,}|\r?\n')

_UNPARSED_CAP = 50


def _split_gate_name(name: str) -> tuple[str, str | None]:
    """Extract (system_name, gate_to_system_name) from an unescaped label.

    Gate labels read "A » B - LABEL": system_name=A, gate_to=B (the text
    between » and the first " - " that follows it — NOT the first " - " in
    the whole label, which for something like "AAA-11 » BBB-22 - GATE EAST"
    would wrongly land inside "BBB-22"). Non-gate labels use the text before
    the first " - " in the whole label as system_name, no gate_to.
    """
    if "»" in name:
        a, _, rest = name.partition("»")
        rest = rest.strip()
        b = rest.split(" - ", 1)[0].strip() if " - " in rest else rest
        return a.strip(), b
    if " - " in name:
        return name.split(" - ", 1)[0].strip(), None
    return "", None


def _entry_from(type_id: int | None, structure_id: int | None, label: str) -> ParsedEntry:
    name = html.unescape(label).strip()
    system, gate_to = _split_gate_name(name)
    return ParsedEntry(name=name, type_id=type_id, structure_id=structure_id,
                       category=categorize(type_id, structure_id),
                       system_name=system, gate_to_system_name=gate_to)


def _dedupe(entries: list[ParsedEntry]) -> list[ParsedEntry]:
    """Keep the first entry per structure_id. Entries with structure_id=None
    (name-only imports — plain-copy fallback) are never deduped against each
    other; None is not a real identity."""
    seen: set[int] = set()
    out: list[ParsedEntry] = []
    for e in entries:
        if e.structure_id is not None:
            if e.structure_id in seen:
                continue
            seen.add(e.structure_id)
        out.append(e)
    return out


def parse_clipboard(text: str) -> ParseResult:
    entries: list[ParsedEntry] = []

    href_matches = list(RE_A_HREF.finditer(text))
    url_matches = list(RE_URL.finditer(text))
    total_links = len(href_matches) + len(url_matches)

    for m in (*href_matches, *url_matches):
        type_id, structure_id, label = int(m.group(1)), int(m.group(2)), m.group(3)
        entries.append(_entry_from(type_id, structure_id, label))

    for m in RE_SMT.finditer(text):
        structure_id = int(m.group(1))
        origin, dest = m.group(2).strip(), m.group(3).strip()
        entries.append(ParsedEntry(name=f"{origin} » {dest}", type_id=None,
                                   structure_id=structure_id, category="gate",
                                   system_name=origin, gate_to_system_name=dest))

    plain_names = 0
    unparsed: list[str] = []
    if total_links == 0:
        for chunk in PLAIN_SPLIT.split(text):
            chunk = chunk.strip()
            if not chunk:
                continue
            if " - " in chunk or "»" in chunk:
                entries.append(_entry_from(None, None, chunk))
                plain_names += 1
            elif len(unparsed) < _UNPARSED_CAP:
                unparsed.append(chunk)

    return ParseResult(entries=_dedupe(entries), total_links=total_links,
                       plain_names=plain_names, unparsed=unparsed)
