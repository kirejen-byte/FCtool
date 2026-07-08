"""EFT / DNA fit parsing + format detection.

Turns pasted EVE fit text into a `ParsedFit`. Two input formats:

- **EFT** — `[Hull, Fit Name]` header followed by blank-line-delimited racks of
  module lines (`Module Name[, Charge Name][ /offline]`), then drones/cargo as
  `Item Name xN`. Drones and cargo are syntactically identical, so they are
  disambiguated by the catalog's category lookup, not by syntax. A bare line
  (no ` xN`) is likewise routed by category — combat boosters, loose charges,
  drones and fighters are emitted without a quantity but are not fitting
  modules, so they go to cargo/drones rather than into a slot.
- **DNA** — a flat `shipID:typeID[_];qty:…::` bag of items; slot position is not
  encoded, so items route to slots via the catalog.

Parsing is permissive: an unresolved item becomes a non-fatal `warning`; only a
missing or invalid header raises `FitParseError`. Pure logic, Tk-free,
network-free — all type resolution goes through the injected `catalog`.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from fit_models import (
    CargoStack,
    DroneStack,
    ParsedFit,
    ParsedModule,
    SLOT_HIGH,
    SLOT_LOW,
    SLOT_MED,
    SLOT_RIG,
    SLOT_SUBSYSTEM,
)


class ParseResult(NamedTuple):
    """The single public return type of all three parsers."""

    fit: ParsedFit
    warnings: list[str]


class FitParseError(ValueError):
    """Raised when a fit cannot be parsed at all (e.g. missing/invalid header)."""

    def __init__(self, message: str, line: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.line = line


# ── Regexes ───────────────────────────────────────────────────────────────────
_HEADER_RE = re.compile(r"^\[(?P<hull>[^,]+),\s*(?P<name>.+)\]$")
_EMPTY_SLOT_RE = re.compile(r"^\[empty\b.*\bslot\]$", re.IGNORECASE)
_ITEM_QTY_RE = re.compile(r"^(?P<name>.+?)\s+x(?P<qty>\d+)$")
_OFFLINE_RE = re.compile(r"\s*/offline\s*$", re.IGNORECASE)
_MUTATION_REF_RE = re.compile(r"\s*\[\d+\]\s*$")  # trailing mutaplasmid ref, e.g. " [1]"
_DNA_RE = re.compile(r"^\d+(:[\d;_]*)*::")

# Rack order EFT exporters emit blocks in, used only as a fallback when the
# catalog has no slot for a module type.
_RACK_ORDER = [SLOT_LOW, SLOT_MED, SLOT_HIGH, SLOT_RIG, SLOT_SUBSYSTEM]


def _normalize_blocks(text: str) -> list[list[str]]:
    """Normalize raw fit text and split it into blank-line-delimited blocks.

    Strips a leading BOM, converts CRLF/CR to LF, rstrips each line, then groups
    consecutive non-blank lines into blocks. Any run of one or more blank lines
    is a single boundary.
    """
    text = text.lstrip("﻿")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks: list[list[str]] = []
    current: list[str] = []
    for raw in text.split("\n"):
        line = raw.rstrip()
        if line:
            current.append(line)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def _parse_module_line(line: str, catalog) -> tuple[ParsedModule | None, str | None]:
    """Parse one module line into a ParsedModule, or return a warning string.

    Returns ``(module, None)`` on success or ``(None, warning)`` when the module
    name cannot be resolved.
    """
    offline = bool(_OFFLINE_RE.search(line))
    line = _OFFLINE_RE.sub("", line)
    line = _MUTATION_REF_RE.sub("", line).strip()

    module_name = line
    charge_type_id: int | None = None
    charge_name: str | None = None

    # Split on the FIRST comma only if the right side resolves to a charge.
    if "," in line:
        left, right = line.split(",", 1)
        right = right.strip()
        right_id = catalog.resolve_id(right)
        if right_id is not None and catalog.category_of(right_id) == "charge":
            module_name = left.strip()
            charge_type_id = right_id
            charge_name = right

    type_id = catalog.resolve_id(module_name)
    if type_id is None:
        return None, f"Unknown module: {module_name}"

    slot = catalog.slot_of(type_id)
    return (
        ParsedModule(
            type_id=type_id,
            name=module_name,
            slot=slot or "",
            charge_type_id=charge_type_id,
            charge_name=charge_name,
            offline=offline,
        ),
        None,
    )


def parse_eft(text: str, catalog) -> ParseResult:
    """Parse EFT fit text into a `ParseResult` (fit + non-fatal warnings)."""
    blocks = _normalize_blocks(text)
    if not blocks:
        raise FitParseError("Empty fit text")

    header_line = blocks[0][0]
    header = _HEADER_RE.match(header_line)
    if header is None:
        raise FitParseError(f"Invalid or missing fit header: {header_line!r}", line=1)

    hull_name = header.group("hull").strip()
    name_hint = header.group("name").strip()
    ship_type_id = catalog.resolve_id(hull_name)
    if ship_type_id is None:
        raise FitParseError(f"Unknown hull: {hull_name}", line=1)

    modules: list[ParsedModule] = []
    drones: list[DroneStack] = []
    cargo: list[CargoStack] = []
    subsystems: list[int] = []
    warnings: list[str] = []

    # The first block's remaining lines plus every later block. Track the block
    # index so a slot-less module can fall back to the EFT rack order.
    rack_index = 0
    for block_idx, block in enumerate(blocks):
        lines = block[1:] if block_idx == 0 else block
        produced_module = False
        for line in lines:
            if _EMPTY_SLOT_RE.match(line):
                # Empty slots count toward the rack but carry no module.
                continue

            item_match = _ITEM_QTY_RE.match(line)
            if item_match:
                name = item_match.group("name").strip()
                qty = int(item_match.group("qty"))
                type_id = catalog.resolve_id(name)
                if type_id is None:
                    warnings.append(f"Unknown item: {name}")
                    continue
                category = catalog.category_of(type_id)
                if category == "drone":
                    drones.append(DroneStack(type_id, name, qty))
                elif category == "fighter":
                    # Fighters land in cargo for v1 (no fighter bay model yet).
                    cargo.append(CargoStack(type_id, name, qty))
                else:
                    cargo.append(CargoStack(type_id, name, qty))
                continue

            module, warning = _parse_module_line(line, catalog)
            if warning is not None:
                warnings.append(warning)
                continue
            if module is None:
                continue

            # Route the resolved line by catalog category, mirroring the ESI
            # import path (`fit_dna.esi_items_to_parsed`) and `parse_dna`. A bare
            # line (no ` xN`) is not necessarily a fitting module: EFT/pyfa emit
            # combat boosters (SDE category "Implant" -> "other"), and loose
            # charges/drones/fighters, as bare lines too. Classifying by category
            # keeps a booster in cargo instead of forcing it into a fitting slot
            # (or, when it fails to resolve, surfacing a misleading "Unknown
            # module"). Only genuine modules/subsystems occupy a rack.
            category = catalog.category_of(module.type_id)
            if category == "subsystem":
                subsystems.append(module.type_id)
                produced_module = True
            elif category == "drone":
                drones.append(DroneStack(module.type_id, module.name, 1))
            elif category in ("charge", "fighter", "other"):
                cargo.append(CargoStack(module.type_id, module.name, 1))
            else:
                # A genuine fitting module (or an unclassifiable type kept as a
                # module for back-compat). Slot-less modules fall back to the
                # EFT rack order.
                if not module.slot:
                    fallback = (
                        _RACK_ORDER[rack_index]
                        if rack_index < len(_RACK_ORDER)
                        else SLOT_LOW
                    )
                    module.slot = fallback
                modules.append(module)
                produced_module = True
        if produced_module:
            rack_index += 1

    fit = ParsedFit(
        ship_type_id=ship_type_id,
        ship_name=hull_name,
        modules=modules,
        drones=drones,
        cargo=cargo,
        subsystems=subsystems,
        name_hint=name_hint,
    )
    return ParseResult(fit=fit, warnings=warnings)


def parse_dna(text: str, catalog) -> ParseResult:
    """Parse fitting-DNA text into a `ParseResult`.

    DNA is a flat ``shipID[:subsystemID×5]:typeID[_];qty:…::`` bag of items;
    slot position is not encoded, so each item is routed to a slot/drone/cargo
    bucket via the catalog. Stacked (``typeID;N``) and per-instance forms are
    both accepted; a trailing ``_`` on a module marks it offline. DNA does not
    bind charges to modules, so charges land in cargo.
    """
    cleaned = text.lstrip("﻿").strip()
    cleaned = cleaned.rstrip(":")  # drop the terminating "::"
    tokens = [t for t in cleaned.split(":") if t != ""]
    if not tokens:
        raise FitParseError("Empty DNA string")

    try:
        ship_type_id = int(tokens[0])
    except ValueError as exc:
        raise FitParseError(f"Invalid DNA ship id: {tokens[0]!r}") from exc

    modules: list[ParsedModule] = []
    drones: list[DroneStack] = []
    cargo: list[CargoStack] = []
    subsystems: list[int] = []
    warnings: list[str] = []

    for token in tokens[1:]:
        offline = False
        if ";" in token:
            id_part, qty_part = token.split(";", 1)
            try:
                qty = int(qty_part)
            except ValueError:
                warnings.append(f"Invalid DNA quantity: {token!r}")
                continue
        else:
            # A bare id with no quantity — a T3 subsystem reference.
            id_part = token
            qty = 1
        if id_part.endswith("_"):
            offline = True
            id_part = id_part[:-1]
        try:
            type_id = int(id_part)
        except ValueError:
            warnings.append(f"Invalid DNA type id: {token!r}")
            continue

        category = catalog.category_of(type_id)
        name = catalog.resolve_name(type_id) or str(type_id)

        if category == "subsystem":
            subsystems.append(type_id)
        elif category == "drone":
            drones.append(DroneStack(type_id, name, qty))
        elif category == "fighter":
            cargo.append(CargoStack(type_id, name, qty))
        elif category in ("charge", "other"):
            cargo.append(CargoStack(type_id, name, qty))
        else:
            # A module — expand the stack into individual instances.
            slot = catalog.slot_of(type_id) or ""
            for _ in range(qty):
                modules.append(
                    ParsedModule(
                        type_id=type_id,
                        name=name,
                        slot=slot,
                        offline=offline,
                    )
                )

    ship_name = catalog.resolve_name(ship_type_id) or str(ship_type_id)
    fit = ParsedFit(
        ship_type_id=ship_type_id,
        ship_name=ship_name,
        modules=modules,
        drones=drones,
        cargo=cargo,
        subsystems=subsystems,
    )
    return ParseResult(fit=fit, warnings=warnings)


def detect_and_parse(text: str, catalog) -> ParseResult:
    """Auto-detect EFT vs DNA and parse accordingly."""
    stripped = text.lstrip("﻿").strip()
    if "[" not in stripped and _DNA_RE.match(stripped):
        return parse_dna(text, catalog)
    return parse_eft(text, catalog)
