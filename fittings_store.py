"""Fittings library persistence + fit/doctrine CRUD + share import/export.

`FittingsStore` owns `fittings_library.json` in `app_dir()`: a flat store of
`Fit`s and `Doctrine`s plus the user-extensible tag vocabulary. Writes are
atomic (temp file + `os.replace`, mirroring `esi_auth._save_tokens`) so a crash
mid-write cannot corrupt the library.

Doctrine membership tags live on the doctrine↔fit link (`DoctrineMember.tags`),
so the same fit can be tagged differently in different doctrines. Deleting a fit
cascades — it is removed from every doctrine's member list.

A membership is a SLOT (one ship role: tags, order, ideal, seed target), and a
slot may carry several fits — its "refits" (`DoctrineMember.refits`, ordered;
`[0]` is the default, `fit_id` is the ACTIVE one, `[]` is a plain member).
**Design rule: the active refit IS `member.fit_id`; only the MOTD palette, the
market enumerators and the library membership views may enumerate `refits`.**
Everything else — guidance, the composer's tag index, fleet stats, MOTD role
lines, cycle roles, previews — reads `fit_id` and therefore sees exactly one fit
per slot, with no de-dupe logic anywhere; that is what keeps a slot from decaying
into N sibling members that every consumer must learn to collapse. Enumerate a
slot's fits through `slot_fit_ids`/`doctrine_fit_ids`, never by reaching into
`.refits` directly. Refits are same-hull only and each fit id appears at most
once per doctrine; both rules are enforced on mutation (`add_refit`), never on
load — a hand-edited library that breaks them is tolerated and normalised by the
next store mutation of that slot.

`.fctdoc` share files are self-contained: they embed full copies of the
referenced fits, so importing into a clean store needs no external data. Import
de-dupes fits by `fit_content_hash` (identical parsed content reuses the local
fit) and remaps ids so incoming doctrine memberships point at the right local
fit.

The group configuration has a second, stricter way in beside that one, and
never through it: `validate_share` (module level, total) turns any payload of
that shape into a clean one or a refusal, and `merge_share` merges a clean
payload BY NAME — a doctrine the library already has is updated in place (or
kept), where `import_share` always adds a suffixed copy. `export_doctrines` and
`import_share` themselves are frozen: `.fctdoc` files work exactly as before.

Thread-safe: every public method that touches `_fits`/`_doctrines`/`_tags` is
synchronized under an internal `threading.RLock` (mirrors `InfraStore`), so a
worker-thread ESI push (`push_fit_to_character`) racing a Tk-thread edit cannot
interleave mutations or corrupt an in-flight `save()`. The lock is released
around `push_fit_to_character`'s two ESI calls so a slow network round-trip
never blocks unrelated store reads.

Pure logic: no Tkinter, no network.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import threading
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import NamedTuple
from uuid import uuid4

import fit_dna
import fleet_guidance
from app_io import atomic_write_json
from app_log import get_logger
from fit_models import (
    DEFAULT_TAGS,
    Doctrine,
    DoctrineMember,
    Fit,
    doctrine_from_dict,
    doctrine_to_dict,
    fit_content_hash,
    fit_from_dict,
    fit_to_dict,
)

log = get_logger(__name__)

# Library (and .fctdoc share) format version, written by save()/export.
#   1 — original format.
#   2 (2026-09-22) — no shape change; marks the one-time Command Carrier
#       exemption migration as done (see FittingsStore.load / import_share).
SCHEMA_VERSION = 2

# Oldest stored version that no longer needs the Command Carrier migration.
_CC_EXEMPTIONS_MIGRATED_VERSION = 2


def _stored_schema_version(data: dict) -> int:
    """The ``schema_version`` a library/share payload claims; a missing or
    non-int value (incl. a bool) counts as 1, the original format."""
    version = data.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        return 1
    return version

# Load-time tag renames applied to legacy libraries (old tag -> new tag). Three
# default role tags were shortened: "Logistics" -> "Logi" (so preview labels read
# "Logi - Onyx" instead of "Logistics - Onyx"), "Support - Webs" -> "Webs", and
# "Support - EWAR" -> "EWAR". Existing data tagged the old way is rewritten on
# every load (idempotent + de-duping). This does NOT touch EVE ship-group/class
# names (e.g. type_catalog's group 832 "Logistics") — only the doctrine role tag
# vocabulary and the per-membership tags that carry it.
_TAG_RENAMES: dict[str, str] = {
    "Logistics": "Logi",
    "Support - Webs": "Webs",
    "Support - EWAR": "EWAR",
}

# ESI character-fittings field limits (see esi_auth.create_fitting). The body is
# pre-trimmed here so the GUI never hands ESI an over-length name/description.
_FITTING_NAME_MAX = 50
_FITTING_DESC_MAX = 500


def _migrate_tag_list(tags: list[str]) -> list[str]:
    """Apply ``_TAG_RENAMES`` to a tag list, de-duping while preserving order.

    Idempotent: an already-renamed tag maps to itself and is kept once. If both a
    legacy tag and its target are present, they collapse to a single entry at the
    legacy tag's original position (the target's later duplicate is dropped).
    """
    out: list[str] = []
    seen: set[str] = set()
    for t in tags:
        new_t = _TAG_RENAMES.get(t, t)
        if new_t in seen:
            continue
        seen.add(new_t)
        out.append(new_t)
    return out


def slot_fit_ids(member: DoctrineMember) -> list[str]:
    """Every fit the SLOT can fly, in refit order (default first).

    The ONE enumerator for "all fits this membership covers": a slot with refits
    yields them in order (`refits[0]` = default), a plain member yields just its
    single `fit_id`. Consumers that must cover every variant — the market
    scan/gap enumerators, the MOTD palette's doctrine-fits provider, the library
    membership views — go through here instead of reading `.refits`, so "plain
    member" needs no special case at any call site.

    Returns a fresh list; mutating it never touches the stored member.
    """
    return list(member.refits) if member.refits else [member.fit_id]


def doctrine_fit_ids(doctrine: Doctrine, include_refits: bool) -> list[str]:
    """The doctrine's fit ids in member order.

    ``include_refits=False`` is exactly today's ``[m.fit_id for m in
    doctrine.members]`` — same values, same order, duplicates and dangling ids
    included — so it is a drop-in for existing call sites. ``True`` expands each
    slot through :func:`slot_fit_ids` (default first, then the other refits).
    Neither form de-dupes: a doctrine that legitimately lists the same fit twice
    (or a hand-edited one that does) keeps both entries, and callers that need a
    set already build one.
    """
    out: list[str] = []
    for member in doctrine.members:
        if include_refits:
            out.extend(slot_fit_ids(member))
        else:
            out.append(member.fit_id)
    return out


class ImportSummary(NamedTuple):
    """Result of `import_share`: how many fits were newly added vs reused
    (content-hash de-dupe), and how many doctrines were added."""

    fits_added: int
    fits_reused: int
    doctrines_added: int


def _now() -> str:
    """An ISO-8601 UTC timestamp for created/modified stamps."""
    return datetime.now(timezone.utc).isoformat()


# ── Share validation + merge result (the group-configuration path) ────────────

# The most one payload may carry through `validate_share` / `merge_share`. Far
# above any real library; over either cap the payload is REFUSED, not trimmed.
MAX_SHARE_FITS = 5000
MAX_SHARE_DOCTRINES = 500
# Member tags: a longer tag, and any tag past the count, is dropped (reported).
MAX_SHARE_TAG_LEN = 40
MAX_SHARE_MEMBER_TAGS = 12
# ...and one payload may bring this many DIFFERENT member tags; a later one is
# dropped from every member that carries it. Without it the per-member bound
# still lets one file grow the vocabulary by tens of thousands of entries.
MAX_SHARE_TAGS = 100
# Text. A fit or doctrine NAME over the first is not a name: the entry is
# dropped. A fit's EFT text over the second: the fit is dropped. Notes and a
# description over it are cleared, never cut short. All reported.
MAX_SHARE_NAME = 200
MAX_SHARE_TEXT = 20_000
# Shape. A doctrine with more slots is dropped before any of them is built; a
# slot keeps its first refits (the rest are dropped, counted). Without them one
# file could list the same 5,000 refits in every one of 400 doctrines.
MAX_SHARE_SLOTS = 200
MAX_SHARE_REFITS = 50
# A drone / cargo stack of more units than this (or of fewer than none) is not
# a fit: the fit is dropped.
MAX_SHARE_QUANTITY = 1_000_000
# Numbers. A type / ship / group id is 1 .. MAX_SHARE_ID (a negative ship id
# renders `fitting:-5:...`); an ideal count, a seed target and a slot's order
# are 0 .. MAX_SHARE_COUNT (a 4,299-digit ideal put a 4,299-digit delta in the
# MOTD and made the fleet guidance raise). Outside it the fit / the doctrine is
# dropped.
MAX_SHARE_ID = 999_999_999
MAX_SHARE_COUNT = 1_000_000

# What a NAME may not hold on this path: a control character (category Cc:
# U+0000-001F, U+007F-009F - the tab and the line ends included). The app
# writes a fit's hull name and its name into the fleet MOTD as link LABELS;
# the renderer escapes every label (`motd_builder._label`), so a "<" or a ">"
# in a name is text and travels - a control character has no place in a
# one-line label, in a list row or in a problem line. The stored DNA is a link
# TARGET (written when the app cannot re-encode one): digits and its three
# separators, nothing else.
_SHARE_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_SHARE_DNA = re.compile(r"[0-9:;_]*")
# ... and no number in it - a type id, a quantity - has more than this many
# digits: a longer one is not a number a fit holds, and a stored DNA of 4,299
# nines would be written into the MOTD as it is. (How MANY units a DNA names
# is not bounded: the app's parser decides what it builds for them.)
MAX_SHARE_DNA_DIGITS = 9
_SHARE_DNA_NUMBER = re.compile(r"[0-9_]+")
# Notes are a TEXT - lines and tabs are what it is made of - but not Tk-only:
# `push_fit_to_character` sends `notes[:500]` to the game as the fitting's
# description. Any other control character clears them.
_SHARE_TEXT_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_SHARE_LABEL_MAX = 60               # of a name quoted in a problem line


class _TooManySlots(ValueError):
    """A doctrine over `MAX_SHARE_SLOTS` (raised before its slots are built)."""


@dataclass
class MergeResult:
    """What `merge_share` did, as NAMES (the import report lists them).

    Fits are named as the library calls them, once each: `fits_added` under
    the incoming name, `fits_reused` under the local name the content already
    had; a fit declined together with a skipped doctrine is in neither.
    Doctrines land in exactly one of the four `doctrines_*` lists.
    `members_dropped` lines read ``"<doctrine> / <fit>: <why>"`` and are
    recorded only for a doctrine that was written. `orphaned_fits` are local
    fits a replaced doctrine used before and nothing uses now (kept, never
    deleted). `tags_added` are member tags appended to the vocabulary.

    With per-doctrine ``resolutions`` (a group import): `aliases` maps an
    incoming doctrine name to ``(local name, keep_mine)`` for every choice
    that was honoured and does not land under the incoming name - "use the
    group's" onto a doctrine of another name (``False``), and every "keep
    mine" (``True``, the same name included); `resolution_fallbacks` holds
    one line per choice that no longer held and followed the default rule;
    `honoured` maps an incoming doctrine name to ``(action, local id)`` for
    EVERY choice that was applied as chosen (the final set, after every
    fallback round) - the local id is the library key of the doctrine the
    choice wrote, kept or (for "both") was paired with. A fallback is never
    in it. `merged` lists ``(incoming name, local name, MergeCounts)`` for
    every merge that added something - a merge is otherwise reported like a
    write: `doctrines_updated`, or `doctrines_unchanged` when it added
    nothing. Those four are plain attributes set in ``__post_init__``,
    deliberately NOT dataclass fields: the field list is pinned
    (`test_fittings_merge_share`), and a result without choices compares and
    converts exactly as before."""

    fits_added: list[str] = field(default_factory=list)
    fits_reused: list[str] = field(default_factory=list)
    doctrines_added: list[str] = field(default_factory=list)
    doctrines_updated: list[str] = field(default_factory=list)
    doctrines_skipped: list[str] = field(default_factory=list)
    doctrines_unchanged: list[str] = field(default_factory=list)
    members_dropped: list[str] = field(default_factory=list)
    orphaned_fits: list[str] = field(default_factory=list)
    tags_added: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.aliases: dict[str, tuple[str, bool]] = {}
        self.resolution_fallbacks: list[str] = []
        self.honoured: dict[str, tuple[str, str]] = {}
        # (incoming name, local name, MergeCounts) for every MERGE that added
        # something (a merge that adds nothing is `doctrines_unchanged`).
        self.merged: list[tuple[str, str, MergeCounts]] = []


class MergeCounts(NamedTuple):
    """What a merge (`FittingsStore._merge_into`) added to the importer's
    doctrine: fits added as refits of its own slots, slots only the group had,
    tags added to its own slots."""

    refits: int
    slots: int
    tags: int


# What a per-doctrine choice of `merge_share` may be.
_RESOLUTION_ACTIONS = ("theirs", "mine", "both", "merge")


_FIT_TEXT_FIELDS = ("name", "hull_name", "source", "raw_text", "dna", "notes",
                    "created", "modified")


def _share_text(value) -> str:
    """Stored text: a str as it is, a stored null as "". Anything else raises."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("not text")
    return value


def _share_int(value, optional: bool = False):
    """A real int (a JSON true/false is not a number here), or None where a
    stored null is legal. Anything else raises."""
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("not a number")
    return value


def _share_fit(raw) -> dict:
    """One incoming fit as a clean `fit_to_dict` dict; raises on anything this
    build could not store as a fit.

    `fit_from_dict` decides the shape; on top of it every field must have the
    type the library holds, because a merged fit is written to the library
    verbatim. Type IDS are not looked up — a hull or module no build knows
    travels as it is. In-game fitting ids never travel (and are dropped before
    parsing, so a garbage value there cannot cost the fit)."""
    if not isinstance(raw, dict):
        raise ValueError("not an object")
    fit = fit_from_dict({**raw, "esi_fitting_ids": {}})
    if not isinstance(fit.id, str) or not fit.id:
        raise ValueError("no id")
    for attr in _FIT_TEXT_FIELDS:
        setattr(fit, attr, _share_text(getattr(fit, attr)))
    _share_int(fit.hull_type_id)
    parsed = fit.parsed
    _share_int(parsed.ship_type_id)
    parsed.ship_name = _share_text(parsed.ship_name)
    if parsed.name_hint is not None:
        _share_text(parsed.name_hint)
    for module in parsed.modules:
        _share_int(module.type_id)
        module.name = _share_text(module.name)
        _share_int(module.charge_type_id, optional=True)
        if module.charge_name is not None:
            _share_text(module.charge_name)
        if not isinstance(module.slot, str) or not isinstance(module.offline, bool):
            raise ValueError("not a module")
    for stack in (*parsed.drones, *parsed.cargo):
        _share_int(stack.type_id)
        stack.name = _share_text(stack.name)
        _share_int(stack.quantity, optional=True)    # a stored null is real data
    for subsystem in parsed.subsystems:
        _share_int(subsystem)
    fit_content_hash(parsed)       # the merge de-dupes on it: it must compute
    return fit_to_dict(fit)


def _share_seed_target(value) -> int | None:
    """A seed target (0 = "don't seed" is a real value); garbage = inherit."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _share_exemption_ok(entry) -> bool:
    """A tagged-union exemption entry: a `kind`, and a well-typed id / name
    when it carries them (an id is ``1 .. MAX_SHARE_ID``)."""
    if not isinstance(entry, dict) or not isinstance(entry.get("kind"), str):
        return False
    eid, name = entry.get("id"), entry.get("name")
    if eid is not None and (isinstance(eid, bool) or not isinstance(eid, int)
                            or not 1 <= eid <= MAX_SHARE_ID):
        return False
    return name is None or isinstance(name, str)


_EXEMPTION_KEYS = ("kind", "id", "name")


def _share_member_tags(raw_tags) -> tuple[list[str], bool]:
    """One slot's tags as they may be stored, plus whether a bound cut any.

    Text only, stripped, blank ones dropped; legacy names renamed and
    duplicates folded AFTER stripping. A tag over `MAX_SHARE_TAG_LEN` is
    dropped whole (cut short it would be a tag nobody chose), and so is every
    tag past the first `MAX_SHARE_MEMBER_TAGS`. Not a list => no tags (the
    model's `list()` would turn a stray string into one tag per character)."""
    if not isinstance(raw_tags, list):
        return [], False
    stripped = [t.strip() for t in raw_tags if isinstance(t, str)]
    tags = _migrate_tag_list(
        [t for t in stripped if t and len(t) <= MAX_SHARE_TAG_LEN])
    cut = (any(len(t) > MAX_SHARE_TAG_LEN for t in stripped)
           or len(tags) > MAX_SHARE_MEMBER_TAGS)
    return tags[:MAX_SHARE_MEMBER_TAGS], cut


def _share_doctrine(
    raw, fit_ids: set[str], migrate_cc: bool
) -> tuple[dict, int, bool, int, int]:
    """One incoming doctrine as a clean `doctrine_to_dict` dict, plus how many
    of its slots were DROPPED because no fit of theirs is in the file, plus
    whether a tag bound cut any member tag (`_share_member_tags`), plus how
    many refits were over `MAX_SHARE_REFITS`, plus how many fits the slots
    that STAY named that are not in the file (a refit, or the active fit of
    a slot that goes on with another). Raises when it is not a doctrine
    (`doctrine_from_dict` refuses it, or it has no name) and, BEFORE any slot
    is built, `_TooManySlots` when it lists more than `MAX_SHARE_SLOTS`.

    The name is stored stripped. A slot keeps every fit that IS in the file:
    an unknown refit is dropped, an unknown active fit hands the slot to its
    first surviving refit, and a slot with nothing left is dropped. The
    store's own slot shape is restored on the way (`fit_id in refits`, no
    duplicates, one fit = a plain member). A slot keeps its first
    `MAX_SHARE_REFITS` refits; when the active fit was among the ones cut,
    the slot flies its first refit. Which refit is active, hulls and
    cross-slot collisions are `merge_share`'s business. Wrong-typed optional
    fields fall back to their defaults."""
    if not isinstance(raw, dict):
        raise ValueError("not an object")
    raw_members = raw.get("members")
    if isinstance(raw_members, list) and len(raw_members) > MAX_SHARE_SLOTS:
        raise _TooManySlots("too many slots")
    doctrine = doctrine_from_dict(raw)
    if not isinstance(doctrine.name, str) or not doctrine.name.strip():
        raise ValueError("no name")
    doctrine.name = doctrine.name.strip()
    for attr in ("id", "description", "created", "modified"):
        if not isinstance(getattr(doctrine, attr), str):
            setattr(doctrine, attr, "")
    doctrine.seed_target = _share_seed_target(doctrine.seed_target)

    exemptions = doctrine.exemptions
    if isinstance(exemptions, list):
        # Rebuilt from the keys an entry is made of: whatever else the file
        # hung on one (nested objects, NaN) never reaches the library.
        kept = [{key: e[key] for key in _EXEMPTION_KEYS if e.get(key) is not None}
                for e in exemptions if _share_exemption_ok(e)]
        # A list that was ALL garbage is no statement; [] means "exempt nothing".
        exemptions = kept if (kept or not exemptions) else None
    else:
        exemptions = None
    if migrate_cc:
        # A pre-v2 payload may carry a frozen copy of the pre-5120 default
        # list: upgrade it exactly as load() and import_share do.
        upgraded = fleet_guidance.upgrade_pre_command_carrier_exemptions(exemptions)
        if upgraded is not None:
            exemptions = upgraded
    doctrine.exemptions = exemptions

    members: list[DoctrineMember] = []
    missing = 0
    tags_cut = False
    refits_cut = 0
    refits_missing = 0
    for index, (member, raw_member) in enumerate(
            zip(doctrine.members, raw.get("members") or [])):
        # Read the tags off the RAW member (see _share_member_tags).
        member.tags, cut = _share_member_tags(raw_member.get("tags"))
        tags_cut = tags_cut or cut
        if isinstance(member.order, bool) or not isinstance(member.order, int):
            member.order = index
        if not isinstance(member.ideal_mode, str):
            member.ideal_mode = None
        for attr in ("ideal_min", "ideal_max"):
            value = getattr(member, attr)
            if isinstance(value, bool) or not isinstance(value, int):
                setattr(member, attr, None)
        member.seed_target = _share_seed_target(member.seed_target)

        known = [f for f in member.refits if isinstance(f, str) and f in fit_ids]
        refits = list(dict.fromkeys(known))
        active_known = isinstance(member.fit_id, str) and member.fit_id in fit_ids
        # What the slot names that is not in the file: its refit entries, and
        # an active fit that is not one of them.
        lost = len(member.refits) - len(known)
        if not active_known and member.fit_id not in member.refits:
            lost += 1
        if active_known:
            if refits and member.fit_id not in refits:
                refits.insert(0, member.fit_id)      # as _normalise_slot does
        elif refits:
            member.fit_id = refits[0]
        else:
            missing += 1                             # the SLOT is left out
            continue
        refits_missing += lost                       # the slot stays, with less
        if len(refits) > MAX_SHARE_REFITS:
            refits_cut += len(refits) - MAX_SHARE_REFITS
            del refits[MAX_SHARE_REFITS:]
            if member.fit_id not in refits:
                member.fit_id = refits[0]
        member.refits = refits if len(refits) > 1 else []
        members.append(member)
    doctrine.members = members
    return doctrine_to_dict(doctrine), missing, tags_cut, refits_cut, refits_missing


def validate_share(payload) -> tuple[dict | None, list[str]]:
    """Check a share payload (`.fctdoc`-shaped: ``{schema_version, fits[],
    doctrines[]}``, either list optional) before it may be merged.

    Returns ``(clean, problems)``. `clean` is None when the payload is refused
    as a whole — then `problems` is the one reason: ``"not a fittings
    payload"``, ``"made with a newer fittings format"``, ``"too many fits"`` or
    ``"too many doctrines"``. Otherwise `clean` is a NEW payload
    ``{"schema_version": SCHEMA_VERSION, "fits": [...], "doctrines": [...]}``
    holding only what survived, and `problems` says what did not:

    A record that is DROPPED is named ``"fit <n> (<name>)"`` / ``"doctrine <n>
    (<name>)"`` - its 1-based position and its name, clipped, control
    characters removed (`_share_label`; the position alone when it has no
    usable name) - and the line says WHAT about it is at fault:

    * a fit that is not a fit (or repeats an id) - ``"fit <n> (<name>): not a
      valid fit"``; every surviving fit has ``esi_fitting_ids == {}``;
    * a fit whose name is over `MAX_SHARE_NAME` or whose EFT text is over
      `MAX_SHARE_TEXT` - dropped, ``"...: name longer than ..."`` / ``"...:
      fit text longer than ..."``;
    * notes over `MAX_SHARE_TEXT`, or holding a control character other than
      the newline and the tab (`_SHARE_TEXT_CONTROL`: the app sends notes to
      the game as a fitting's description) - CLEARED (the fit stays), ``"fit
      <n> (<name>): notes dropped (...)"``;
    * a fit or a doctrine with any OTHER string over its bound - dropped,
      ``"...: a module name is too long"``, the string named as
      `_SHARE_STRING_NAMES` calls it: `dna` may be as long as
      `MAX_SHARE_TEXT`, every remaining string at any depth (hull, module,
      charge, drone and cargo names, slot, source, stamps, ids, ideal mode,
      exemption kind / name) at most `MAX_SHARE_NAME` (`_share_strings_fit`);
    * a fit or a doctrine with a control character in any of those NAME
      strings (its own name, a member tag and the hull name included; the EFT
      text, notes and a description are not names) - dropped, ``"...: its
      hull name holds a control character"`` (`_share_control`). A ``<`` or a
      ``>`` in a name is text and travels: the app writes fit and hull names
      into the MOTD as link labels, and the renderer escapes every label.
      Checked before the twin rule, so such a doctrine never claims its name;
    * a fit whose stored `dna` is not digits and ``:`` ``;`` ``_`` only -
      dropped, ``"...: its DNA is not a fit DNA"`` (the stored DNA can end up
      as a link's target) - or holds a number of more than
      `MAX_SHARE_DNA_DIGITS` digits, ``"...: its saved link holds a number of
      more than 9 digits"``;
    * a fit with a type id outside ``1 .. MAX_SHARE_ID`` (ship, module,
      charge, drone, cargo, subsystem; its own `hull_type_id` may be 0) -
      dropped, ``"...: a type id is out of range"``;
    * a fit with a drone / cargo quantity below 0 or over
      `MAX_SHARE_QUANTITY` - dropped, ``"...: a quantity is out of range"``;
    * a doctrine with more than `MAX_SHARE_SLOTS` slots - dropped before a
      slot is built, ``"doctrine <n> (<name>): more than 200 slots"``;
    * a doctrine that is not a doctrine, or whose name (stripped) is over
      `MAX_SHARE_NAME` - ``"...: not a valid doctrine"`` / ``"...: name
      longer than ..."``;
    * a doctrine with an ideal count, a seed target or a slot order outside
      ``0 .. MAX_SHARE_COUNT`` - dropped, ``"...: an ideal count is out of
      range"`` (a NEGATIVE seed target is garbage and reads as "inherit", as
      a wrong-typed one does; an exemption whose id is outside ``1 ..
      MAX_SHARE_ID`` is no exemption entry);
    * a doctrine whose name (stripped) an earlier one in the file already has
      - ``"doctrine <n> (<name>): name used by an earlier doctrine"``; the
      later one is DROPPED, so a merge by name is well-defined and can never
      land on some other doctrine of the importer's;

    What follows is said about a doctrine that STAYS, by its name:

    * a description over `MAX_SHARE_TEXT` — cleared (the doctrine stays),
      ``"<name>: description dropped ..."``;
    * slots with no fit in the file — dropped, ONE counted line per
      doctrine, ``"<name>: <n> slots naming a fit that is not in the file
      left out"`` (see `_share_doctrine`; a doctrine left with no member is
      kept);
    * refits (or an active fit) not in the file, of a slot that STAYS on
      another of its fits — dropped from the slot, ONE counted line per
      doctrine, ``"<name>: <n> refits naming a fit that is not in the file
      left out"``: no slot was left out, and the line does not say so;
    * refits past the first `MAX_SHARE_REFITS` of a slot — dropped, ONE
      counted line per doctrine;
    * member tags over a bound (`MAX_SHARE_TAG_LEN`, `MAX_SHARE_MEMBER_TAGS`)
      — dropped, one line per doctrine;
    * member tags past the first `MAX_SHARE_TAGS` DIFFERENT ones of the file,
      counted over the doctrines that are kept, in file order — each dropped
      whole from every member that carries it, ONE line for the payload.

    Text over a limit is never cut short: it is kept whole or not at all.

    Doctrine names and member tags are stored stripped, legacy tag names are
    renamed, exemption entries keep only `kind` / `id` / `name`, and a pre-v2
    payload's default exemption list is upgraded as `import_share` does.

    TOTAL: any input, never raises, never edits `payload`. Idempotent:
    ``validate_share(clean) == (clean, [])``."""
    try:
        return _validate_share(payload)
    except Exception:                                 # the contract, not a hope
        return None, ["not a fittings payload"]


# The fields of a fit / doctrine record that hold a TEXT (up to
# `MAX_SHARE_TEXT`). Every other string in one is a name, an id, a stamp or a
# tag, and is bounded by `MAX_SHARE_NAME`.
_SHARE_LONG_TEXT = frozenset(("raw_text", "notes", "description", "dna"))


def _share_strings_fit(record: dict) -> bool:
    """Whether every string VALUE of a cleaned fit / doctrine dict is within
    its bound: the record's own `_SHARE_LONG_TEXT` fields `MAX_SHARE_TEXT`,
    any other string at any depth `MAX_SHARE_NAME`. One walk instead of a
    check per field, so a string slot added to the models later is bounded
    without being named here."""
    pending = []
    for key, value in record.items():
        if isinstance(value, str):
            limit = MAX_SHARE_TEXT if key in _SHARE_LONG_TEXT else MAX_SHARE_NAME
            if len(value) > limit:
                return False
        else:
            pending.append(value)
    while pending:
        value = pending.pop()
        if isinstance(value, str):
            if len(value) > MAX_SHARE_NAME:
                return False
        elif isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, (list, tuple)):
            pending.extend(value)
    return True


def _share_control(record: dict) -> bool:
    """Whether a NAME string of a cleaned fit / doctrine dict holds a control
    character (`_SHARE_CONTROL`). The walk of `_share_strings_fit`: every
    string value at any depth except the record's own `_SHARE_LONG_TEXT`
    fields - the texts are not names (notes have a rule of their own), and
    `dna` is checked against its alphabet. A ``<`` or a ``>`` is no fault:
    the renderer escapes a label."""
    pending = [value for key, value in record.items()
               if key not in _SHARE_LONG_TEXT]
    while pending:
        value = pending.pop()
        if isinstance(value, str):
            if _SHARE_CONTROL.search(value):
                return True
        elif isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, (list, tuple)):
            pending.extend(value)
    return False


# What a problem line calls a string of a fit / doctrine record, by the keys
# that lead to it (an entry of a list goes by its list's key). A string that
# is not listed is "a text field".
_SHARE_STRING_NAMES = {
    ("id",): "its id",
    ("name",): "its name",
    ("hull_name",): "its hull name",
    ("source",): "its source",
    ("dna",): "its DNA",
    ("parsed", "ship_name"): "its ship name",
    ("parsed", "name_hint"): "the name in its fit text",
    ("parsed", "modules", "name"): "a module name",
    ("parsed", "modules", "slot"): "a module slot",
    ("parsed", "modules", "charge_name"): "a charge name",
    ("parsed", "drones", "name"): "a drone name",
    ("parsed", "cargo", "name"): "a cargo name",
    ("members", "tags"): "a tag",
    ("members", "ideal_mode"): "an ideal mode",
    ("exemptions", "kind"): "an exemption kind",
    ("exemptions", "name"): "an exemption name",
}


def _share_string_at_fault(record: dict, at_fault) -> str:
    """What a problem line calls the FIRST string of a cleaned fit / doctrine
    dict - in the record's own order - for which ``at_fault(top_key, text)`` is
    true: ``"a module name"``, ``"its hull name"``, ... (`_SHARE_STRING_NAMES`),
    or ``"a text field"``. A refusal says WHICH string it is about: "rename
    the fit" does not help when a module name is the one. Only called for a
    record already known to be refused, so its cost is not on the common path."""
    pending = [((key,), value) for key, value in reversed(list(record.items()))]
    while pending:
        path, value = pending.pop()
        if isinstance(value, str):
            if at_fault(path[0], value):
                return _SHARE_STRING_NAMES.get(path, "a text field")
        elif isinstance(value, dict):
            pending.extend((path + (key,), item)
                           for key, item in reversed(list(value.items())))
        elif isinstance(value, (list, tuple)):
            pending.extend((path, item) for item in reversed(value))
    return "a text field"


def _share_too_long(top_key: str, text: str) -> bool:
    limit = MAX_SHARE_TEXT if top_key in _SHARE_LONG_TEXT else MAX_SHARE_NAME
    return len(text) > limit


def _share_has_control(top_key: str, text: str) -> bool:
    return (top_key not in _SHARE_LONG_TEXT
            and _SHARE_CONTROL.search(text) is not None)


def _share_dna_numbers_ok(dna: str) -> bool:
    """Whether every number of a DNA string (already known to be digits and
    separators) has at most `MAX_SHARE_DNA_DIGITS` digits. The parser reads
    ``1_000`` as 1000, so a ``_`` inside a number is skipped, not a separator.
    The one owner of the rule: a stored DNA here, a fit pill's in the group
    items."""
    return all(len(number.replace("_", "")) <= MAX_SHARE_DNA_DIGITS
               for number in _SHARE_DNA_NUMBER.findall(dna))


def _share_shown(name) -> str:
    """A name as a problem line QUOTES it: at most `_SHARE_LABEL_MAX`
    characters, and neither a control character (category Cc) nor a FORMAT
    character (Cf: a bidi override, a zero-width space) among them - a quoted
    name must not be able to reorder or hide part of the line it sits in.
    ``""`` for anything that is not text."""
    if not isinstance(name, str):
        return ""
    shown = "".join(ch for ch in name[:4 * _SHARE_LABEL_MAX]
                    if unicodedata.category(ch) not in ("Cc", "Cf")).strip()
    return shown[:_SHARE_LABEL_MAX].rstrip()


def _share_label(kind: str, index: int, name) -> str:
    """``"fit 3 (Alpha Fit)"`` for a problem line: the record's position and
    its name as `_share_shown` quotes one. No usable name: the position
    alone."""
    shown = _share_shown(name)
    return f"{kind} {index} ({shown})" if shown else f"{kind} {index}"


def _share_raw_label(kind: str, index: int, raw) -> str:
    """`_share_label` for a record that never became one: its name is read
    off the raw entry, when that is an object at all."""
    return _share_label(kind, index, raw.get("name") if isinstance(raw, dict) else None)


def _share_quantities_ok(entry: dict) -> bool:
    """Whether every stack quantity of a cleaned fit dict is None (a stored
    null) or within ``0 .. MAX_SHARE_QUANTITY``."""
    parsed = entry["parsed"]
    for section in ("modules", "drones", "cargo"):
        for stack in parsed.get(section) or ():
            quantity = stack.get("quantity")
            if quantity is not None and not 0 <= quantity <= MAX_SHARE_QUANTITY:
                return False
    return True


def _share_ids_ok(entry: dict) -> bool:
    """Whether every type id of a cleaned fit dict is ``1 .. MAX_SHARE_ID``:
    the ship, each module and its charge (None = no charge), each drone and
    cargo stack, each subsystem. The fit's own `hull_type_id` may also be 0 -
    what a fit stored without the key loads as."""
    parsed = entry["parsed"]
    ids = [parsed["ship_type_id"], *(parsed.get("subsystems") or ())]
    for module in parsed.get("modules") or ():
        ids.append(module["type_id"])
        if module.get("charge_type_id") is not None:
            ids.append(module["charge_type_id"])
    for section in ("drones", "cargo"):
        ids.extend(stack["type_id"] for stack in parsed.get(section) or ())
    return (0 <= entry["hull_type_id"] <= MAX_SHARE_ID
            and all(1 <= type_id <= MAX_SHARE_ID for type_id in ids))


def _share_number_at_fault(entry: dict) -> str:
    """What is out of ``0 .. MAX_SHARE_COUNT`` in a cleaned doctrine dict:
    ``"a seed target"`` (the doctrine's or a slot's), ``"an ideal count"``,
    ``"a slot order"`` - or ``""``. A number that is not there (None: inherit,
    no ideal) is fine."""
    def out(value) -> bool:
        return value is not None and not 0 <= value <= MAX_SHARE_COUNT

    if out(entry.get("seed_target")):
        return "a seed target"
    for member in entry["members"]:
        if out(member.get("ideal_min")) or out(member.get("ideal_max")):
            return "an ideal count"
        if out(member.get("seed_target")):
            return "a seed target"
        if out(member["order"]):
            return "a slot order"
    return ""


def _counted(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _validate_share(payload) -> tuple[dict | None, list[str]]:
    if not isinstance(payload, dict):
        return None, ["not a fittings payload"]
    version = payload.get("schema_version")
    if (isinstance(version, int) and not isinstance(version, bool)
            and version > SCHEMA_VERSION):
        return None, ["made with a newer fittings format"]
    raw_fits = payload.get("fits")
    if not isinstance(raw_fits, list):
        raw_fits = []
    raw_doctrines = payload.get("doctrines")
    if not isinstance(raw_doctrines, list):
        raw_doctrines = []
    if len(raw_fits) > MAX_SHARE_FITS:
        return None, ["too many fits"]
    if len(raw_doctrines) > MAX_SHARE_DOCTRINES:
        return None, ["too many doctrines"]

    problems: list[str] = []
    fits: list[dict] = []
    fit_ids: set[str] = set()
    for index, raw in enumerate(raw_fits, 1):
        try:
            entry = _share_fit(raw)
            if entry["id"] in fit_ids:
                raise ValueError("repeated id")
        except Exception:
            problems.append(f"{_share_raw_label('fit', index, raw)}: not a valid fit")
            continue
        # Every line below names the fit (clipped, control characters removed)
        # and what about it is at fault - never a bare position.
        label = _share_label("fit", index, entry["name"])
        if len(entry["name"]) > MAX_SHARE_NAME:
            problems.append(
                f"{label}: name longer than {MAX_SHARE_NAME} characters")
            continue
        if len(entry["raw_text"]) > MAX_SHARE_TEXT:
            problems.append(
                f"{label}: fit text longer than {MAX_SHARE_TEXT} characters")
            continue
        notes_dropped = ""
        if len(entry["notes"]) > MAX_SHARE_TEXT:
            notes_dropped = f"longer than {MAX_SHARE_TEXT} characters"
        elif _SHARE_TEXT_CONTROL.search(entry["notes"]):
            notes_dropped = "they hold a control character"
        if notes_dropped:
            entry["notes"] = ""
        if not _share_strings_fit(entry):
            # Dropped whole; a slot that names it is reported below as naming
            # a fit that is not in the file.
            what = _share_string_at_fault(entry, _share_too_long)
            problems.append(f"{label}: {what} is too long")
            continue
        # Names (see `_SHARE_CONTROL`), then the stored DNA, then the numbers.
        if _share_control(entry):
            what = _share_string_at_fault(entry, _share_has_control)
            problems.append(f"{label}: {what} holds a control character")
            continue
        if _SHARE_DNA.fullmatch(entry["dna"]) is None:
            problems.append(f"{label}: its DNA is not a fit DNA")
            continue
        if not _share_dna_numbers_ok(entry["dna"]):
            problems.append(f"{label}: its saved link holds a number of more "
                            f"than {MAX_SHARE_DNA_DIGITS} digits")
            continue
        if not _share_ids_ok(entry):
            problems.append(f"{label}: a type id is out of range")
            continue
        if not _share_quantities_ok(entry):
            problems.append(f"{label}: a quantity is out of range")
            continue
        if notes_dropped:
            problems.append(f"{label}: notes dropped ({notes_dropped})")
        fit_ids.add(entry["id"])
        fits.append(entry)

    migrate_cc = _stored_schema_version(payload) < _CC_EXEMPTIONS_MIGRATED_VERSION
    doctrines: list[dict] = []
    seen_names: set[str] = set()
    seen_tags: set[str] = set()         # the file's different member tags
    tags_over = False
    for index, raw in enumerate(raw_doctrines, 1):
        try:
            entry, missing, tags_cut, refits_cut, refits_missing = \
                _share_doctrine(raw, fit_ids, migrate_cc)
        except _TooManySlots:
            problems.append(f"{_share_label('doctrine', index, raw.get('name'))}: "
                            f"more than {MAX_SHARE_SLOTS} slots")
            continue
        except Exception:
            problems.append(
                f"{_share_raw_label('doctrine', index, raw)}: not a valid doctrine")
            continue
        name = entry["name"]
        # A doctrine that is DROPPED is named by its position and its name
        # (clipped, control characters removed); one that stays, by its name.
        label = _share_label("doctrine", index, name)
        if len(name) > MAX_SHARE_NAME:
            # Before the twin check: an over-long name is no name to be a twin of.
            problems.append(
                f"{label}: name longer than {MAX_SHARE_NAME} characters")
            continue
        if _share_control(entry):
            # Also before the twin check, so that a doctrine dropped here
            # never claims its name.
            what = _share_string_at_fault(entry, _share_has_control)
            problems.append(f"{label}: {what} holds a control character")
            continue
        out_of_range = _share_number_at_fault(entry)
        if out_of_range:
            problems.append(f"{label}: {out_of_range} is out of range")
            continue
        if name in seen_names:
            # One doctrine per name: the merge matches by name, so the second
            # copy would overwrite the first. The earlier one stands.
            problems.append(f"{label}: name used by an earlier doctrine")
            continue
        description_over = len(entry["description"]) > MAX_SHARE_TEXT
        if description_over:
            entry["description"] = ""
        if not _share_strings_fit(entry):
            # Before the name is taken: a later doctrine of this name stands.
            what = _share_string_at_fault(entry, _share_too_long)
            problems.append(f"{label}: {what} is too long")
            continue
        seen_names.add(name)
        if description_over:
            problems.append(f"{name}: description dropped (longer than "
                            f"{MAX_SHARE_TEXT} characters)")
        if missing:
            # ONE line, counted: a line per slot was 900,000 lines for one
            # hand-made doctrine before the slot bound existed.
            problems.append(f"{name}: {_counted(missing, 'slot', 'slots')} naming "
                            "a fit that is not in the file left out")
        if refits_missing:
            # A slot that only lost a refit is still there: never "a slot".
            problems.append(
                f"{name}: {_counted(refits_missing, 'refit', 'refits')} naming "
                "a fit that is not in the file left out")
        if refits_cut:
            problems.append(
                f"{name}: {_counted(refits_cut, 'refit', 'refits')} over the "
                f"limit of {MAX_SHARE_REFITS} per slot left out")
        if tags_cut:
            problems.append(
                f"{name}: member tags dropped (at most {MAX_SHARE_MEMBER_TAGS} "
                f"per member, {MAX_SHARE_TAG_LEN} characters each)")
        for member in entry["members"]:
            kept: list[str] = []
            for tag in member["tags"]:
                if tag not in seen_tags:
                    if len(seen_tags) >= MAX_SHARE_TAGS:
                        tags_over = True
                        continue
                    seen_tags.add(tag)
                kept.append(tag)
            member["tags"] = kept
        doctrines.append(entry)
    if tags_over:
        problems.append(f"member tags dropped (at most {MAX_SHARE_TAGS} "
                        "different tags per file)")

    return {
        "schema_version": SCHEMA_VERSION,
        "fits": fits,
        "doctrines": doctrines,
    }, problems


class FittingsStore:
    """In-memory fit/doctrine library backed by an atomic JSON file.

    All public methods are thread-safe (`threading.RLock`); every mutator
    persists via the same lock `save()` uses, so concurrent callers cannot
    interleave writes or corrupt the store.
    """

    def __init__(self, path: str):
        self.path = path
        self._fits: dict[str, Fit] = {}
        self._doctrines: dict[str, Doctrine] = {}
        self._tags: list[str] = list(DEFAULT_TAGS)
        # Optional duck-typed TypeCatalog (group_of/resolve_name), set by the GUI
        # after construction. Used to auto-tag fits that mount a Defender Launcher.
        self.catalog = None
        self._lock = threading.RLock()
        # Monotonic revision counter bumped on every persisted change (see save()).
        # Lets callers cheaply detect "the library changed" without diffing it —
        # e.g. the preview-caption memo keys its doctrine/tag-index bundle on this
        # so it recomputes only when fits/doctrines change (OPTIMIZATION_REVIEW E3).
        self._rev = 0

    # ── Persistence ───────────────────────────────────────────────────────────

    def load(self) -> None:
        """Load the library from disk. A missing or unreadable file seeds an
        empty library with the default tag vocabulary.

        Each fit/doctrine entry is constructed independently: valid JSON with
        the wrong shape (e.g. a required field missing) raises out of
        ``fit_from_dict``/``doctrine_from_dict`` for THAT entry only, so it is
        skipped (logged via ``log.warning`` with the entry id and the error,
        plus a load-level summary line with the total skip count) rather than
        aborting the whole load — mirroring
        ``fleet_template_store.load()``'s per-entry discipline. A doctrine
        member left pointing at a skipped/missing fit id is NOT cleaned up
        here: every consumer already treats an unresolvable ``get_fit`` as a
        tolerated case (skip in `fleet_composer`/`fleet_guidance`, an explicit
        "(missing fit ...)" label in the GUI), so leaving it as-is is
        consistent with existing behavior.

        In-memory migrations run after the entries load (never a write): the
        tag renames every time, and — for a library stored below version 2 —
        the one-time Command Carrier exemption upgrade."""
        with self._lock:
            if not os.path.exists(self.path):
                self.load_data(None)
                return
            try:
                with open(self.path, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                # The file exists but is unreadable (corrupt JSON, locked, perms).
                # Copy it aside BEFORE degrading to an empty store, otherwise the
                # next save() would atomically overwrite the user's recoverable
                # fits/doctrines/tags. The sidecar has a fixed name (no timestamp)
                # and is overwritten on each corrupt load.
                backup = f"{self.path}.corrupt"
                try:
                    shutil.copy2(self.path, backup)
                    log.warning(
                        "Fittings library at %s is unreadable; backed up to %s "
                        "before resetting to an empty store.",
                        self.path,
                        backup,
                    )
                except OSError:
                    log.exception(
                        "Fittings library at %s is unreadable and could not be "
                        "backed up to %s; resetting to an empty store.",
                        self.path,
                        backup,
                    )
                self.load_data(None)
                return
            if data is None:
                # A file holding a bare JSON `null` is a wrong-shaped library,
                # not a missing one. None is load_data's "empty library" signal,
                # so passing it through would turn this file into a silent empty
                # store; it keeps failing the way every other non-object top
                # level ([], 5, "x") does, exactly as it did before load_data
                # existed — down to what it leaves behind in a store that was
                # already populated: the fits were reset before `data.get`
                # raised, the doctrines and tags had not been reached.
                self._fits = {}
                raise AttributeError("'NoneType' object has no attribute 'get'")
            self.load_data(data)

    def load_data(self, data: dict | None) -> None:
        """Load the library from an already-parsed payload — everything
        `load` does once the file has been read, and nothing that touches a
        file (`self.path` is only named in log lines).

        ``None`` is "no library": empty, with the default tag vocabulary.
        A dict is read exactly as `load` reads the file's JSON object:
        per-entry tolerance, then the in-memory migrations. `dump` is the
        inverse. The group-configuration import uses the pair to run a library
        through the store without a file of its own."""
        with self._lock:
            if data is None:
                self._fits = {}
                self._doctrines = {}
                self._tags = list(DEFAULT_TAGS)
                return

            self._fits = {}
            skipped_fits = 0
            for fid, raw in (data.get("fits") or {}).items():
                try:
                    self._fits[fid] = fit_from_dict(raw)
                except Exception as exc:
                    skipped_fits += 1
                    log.warning(
                        "Skipping malformed fit %r in %s: %s", fid, self.path, exc
                    )

            self._doctrines = {}
            skipped_doctrines = 0
            for did, raw in (data.get("doctrines") or {}).items():
                try:
                    self._doctrines[did] = doctrine_from_dict(raw)
                except Exception as exc:
                    skipped_doctrines += 1
                    log.warning(
                        "Skipping malformed doctrine %r in %s: %s", did, self.path, exc
                    )

            total_skipped = skipped_fits + skipped_doctrines
            if total_skipped:
                log.warning(
                    "Fittings library at %s: skipped %d malformed entr%s on load "
                    "(%d fit%s, %d doctrine%s) — see warnings above for details.",
                    self.path,
                    total_skipped,
                    "y" if total_skipped == 1 else "ies",
                    skipped_fits,
                    "" if skipped_fits == 1 else "s",
                    skipped_doctrines,
                    "" if skipped_doctrines == 1 else "s",
                )

            tags = data.get("tags")
            self._tags = list(tags) if tags else list(DEFAULT_TAGS)
            self._migrate_tags()
            if _stored_schema_version(data) < _CC_EXEMPTIONS_MIGRATED_VERSION:
                self._migrate_command_carrier_exemptions()

    def _migrate_command_carrier_exemptions(self) -> None:
        """Give every doctrine whose SAVED exemptions are still exactly the
        pre-5120 standard set the Command Carrier entry (owner, 2026-09-22:
        "exempt Command Carriers like other capitals").

        Runs only for a library stored below version 2. Purely in-memory, like
        ``_migrate_tags``: load() never writes. Until the next save() this
        re-runs on every launch to the same result — safe because every
        exemptions edit goes through a mutator followed by save(), and that
        save writes version 2 together with the user's list, so the migration
        can never re-run over a choice the user made (e.g. removing Command
        Carriers again). None, customised and malformed lists are untouched
        (see fleet_guidance.upgrade_pre_command_carrier_exemptions)."""
        upgraded_n = 0
        for doctrine in self._doctrines.values():
            upgraded = fleet_guidance.upgrade_pre_command_carrier_exemptions(
                doctrine.exemptions)
            if upgraded is not None:
                doctrine.exemptions = upgraded
                upgraded_n += 1
        if upgraded_n:
            log.info("Fittings library: added the Command Carrier exemption to "
                     "%d doctrine(s) still on the old default list.", upgraded_n)

    def _migrate_tags(self) -> None:
        """Rewrite legacy role tags (``_TAG_RENAMES``) across the loaded library.

        Applies to the custom tag vocabulary and to every doctrine membership's
        ``tags`` list (which is where per-fit doctrine tags AND composition/ideal
        role resolution live — see ``fleet_guidance._composition_role``). Purely
        in-memory: the rename persists on the next natural ``save()``. Idempotent
        and de-duping via ``_migrate_tag_list`` so running it every load is safe
        and never creates duplicate ``"Logi"`` entries.
        """
        self._tags = _migrate_tag_list(self._tags)
        for doctrine in self._doctrines.values():
            for member in doctrine.members:
                new_tags = _migrate_tag_list(member.tags)
                if new_tags != member.tags:
                    member.tags = new_tags

    def dump(self) -> dict:
        """The payload `save` writes, as a fresh structure: the library file's
        JSON object, keys in file order. A read — no revision bump, no write.
        `load_data` is the inverse."""
        with self._lock:
            return {
                "schema_version": SCHEMA_VERSION,
                "fits": {fid: fit_to_dict(fit) for fid, fit in self._fits.items()},
                "doctrines": {
                    did: doctrine_to_dict(d) for did, d in self._doctrines.items()
                },
                "tags": list(self._tags),
            }

    def save(self) -> None:
        """Atomically persist the library (temp file + fsync + os.replace).

        Held under ``self._lock`` for the whole read-payload + write, so a
        concurrent mutator cannot interleave with the snapshot being written
        and two concurrent savers cannot race on ``atomic_write_json``'s
        shared temp file (the lost-update hazard `InfraStore` already guards
        against).
        """
        with self._lock:
            # Bump BEFORE the write (not after success): the in-memory library has
            # already been mutated by the caller, so the revision must advance even
            # if persistence later fails — consumers memoise off in-memory state.
            self._rev += 1
            payload = self.dump()
            parent = os.path.dirname(self.path)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            try:
                # atomic_write_json writes <path>.tmp, fsyncs, then os.replace;
                # it cleans up the temp file and re-raises on any failure.
                atomic_write_json(self.path, payload, indent=2)
            except Exception:
                log.exception("Failed to save fittings library to %s", self.path)
                raise

    def revision(self) -> int:
        """Monotonic change counter (advances once per ``save()``).

        Read WITHOUT the lock on purpose: a single int read is atomic under the
        GIL, and callers use it only as a cheap change signal — a value that is
        stale by one merely defers one memo refresh, never corrupts anything. A
        locked read would let a long ``save()`` fsync briefly stall the Tk thread
        that polls this every ~250 ms."""
        return self._rev

    # ── Fit CRUD ──────────────────────────────────────────────────────────────

    def add_fit(self, fit: Fit) -> str:
        """Add a fit, assigning a fresh id and created/modified stamps if the
        fit doesn't already carry them. Returns the assigned id."""
        with self._lock:
            fid = fit.id or uuid4().hex
            fit.id = fid
            stamp = _now()
            if not fit.created:
                fit.created = stamp
            fit.modified = stamp
            self._fits[fid] = fit
            return fid

    def update_fit(self, fit: Fit) -> bool:
        """Replace an existing fit (by id), refreshing its modified stamp.

        Returns True if the fit existed and was updated, False for an unknown
        id. (Additive: callers that ignore the return value are unaffected.)
        """
        with self._lock:
            if fit.id not in self._fits:
                return False
            fit.modified = _now()
            self._fits[fit.id] = fit
            return True

    def delete_fit(self, fit_id: str) -> None:
        """Delete a fit and cascade-remove it from every doctrine's members.

        A plain member pointing at the deleted fit loses its slot, as it always
        has. A slot WITH refits loses only the deleted variant: the fit is
        dropped from `refits`, a deleted ACTIVE fit is replaced by the new
        `refits[0]`, and a slot left with one fit collapses back to a plain
        member. Deleting a refit therefore never removes a slot while another fit
        remains — the doctrine still wants that ship. The slot goes only when
        nothing is left to fly it (the active fit deleted and no refit survives).

        Normalises the hand-edited `fit_id ∉ refits` shape first, exactly as
        `remove_refit` does: without it a slot flying `a` with refits `[b, c]`
        would, on deleting `b`, collapse to one remaining fit and silently lose
        `c`. With it the slot lands on `[a, c]` — the same answer both mutators
        give.
        """
        with self._lock:
            self._fits.pop(fit_id, None)
            for doctrine in self._doctrines.values():
                survivors: list[DoctrineMember] = []
                for member in doctrine.members:
                    if not member.refits:
                        if member.fit_id != fit_id:
                            survivors.append(member)
                        continue
                    self._normalise_slot(member)
                    remaining = [f for f in member.refits if f != fit_id]
                    if member.fit_id == fit_id:
                        if not remaining:
                            continue          # nothing left to fly: slot goes
                        member.fit_id = remaining[0]
                    member.refits = remaining if len(remaining) > 1 else []
                    survivors.append(member)
                doctrine.members = survivors

    def delete_fits(self, fit_ids) -> None:
        """Delete several fits in ONE atomic batch, then persist ONCE.

        Each id is removed and cascaded out of every doctrine's member list
        exactly as ``delete_fit`` does — this method calls it per id — so
        doctrine-membership cleanup is byte-for-byte identical to a single
        delete, just applied N times under a single lock hold followed by ONE
        ``save()`` (never N saves; matches the GUI's per-fit single-delete
        semantics, batched). An empty or ``None`` iterable is a true no-op:
        nothing is mutated and the library is NOT re-written (no revision bump).

        The whole batch runs under ``self._lock`` (the same RLock ``delete_fit``
        and ``save`` re-enter), so a concurrent reader can never observe a
        half-applied batch. Unknown ids are tolerated (``delete_fit`` pops with
        a default), mirroring the single-delete contract.
        """
        ids = list(fit_ids or [])
        if not ids:
            return
        with self._lock:
            for fit_id in ids:
                self.delete_fit(fit_id)
            self.save()

    def get_fit(self, fit_id: str) -> Fit | None:
        with self._lock:
            return self._fits.get(fit_id)

    def list_fits(self) -> list[Fit]:
        with self._lock:
            return list(self._fits.values())

    # ── ESI push (service-layer wrapper; keeps ESI out of the GUI) ─────────────

    def push_fit_to_character(
        self, fit_id: str, character_id: int, esi_auth
    ) -> bool:
        """Save a fit to a character's in-game Fittings via ESI.

        ESI has no fitting-update endpoint, so editing is delete + recreate:
        if this fit already has a stored fitting id for `character_id`, that
        in-game fitting is deleted first. The POST body is built from the fit's
        parsed contents (name <= 50 from the fit name, description <= 500 from
        notes, ship_type_id, and `to_esi_items` for the slot-flagged items).

        On success the returned fitting id is recorded in
        `Fit.esi_fitting_ids[character_id]`, the fit is updated + the library
        saved, and True is returned. If the fit is unknown or ESI returns no
        id (failure), no state is mutated and False is returned.

        The two ESI calls happen OUTSIDE the store lock (they're network I/O
        on a worker thread) so a slow push never blocks unrelated store reads;
        only the final mutation + save is locked.
        """
        fit = self.get_fit(fit_id)
        if fit is None:
            return False

        prior_id = fit.esi_fitting_ids.get(character_id)
        if prior_id is not None:
            deleted = esi_auth.delete_fitting(character_id, prior_id)
            if not deleted:
                # ESI couldn't delete the old in-game fitting (already gone,
                # transient error, etc.). We proceed to recreate anyway, which
                # may leave a stale duplicate in-game; log so it's diagnosable.
                log.warning(
                    "delete_fitting failed for character %s, fitting %s "
                    "(fit %s); recreating may leave a duplicate in-game.",
                    character_id,
                    prior_id,
                    fit_id,
                )

        body = {
            "name": (fit.name or "")[:_FITTING_NAME_MAX],
            "description": (fit.notes or "")[:_FITTING_DESC_MAX],
            "ship_type_id": fit.parsed.ship_type_id,
            "items": fit_dna.to_esi_items(fit.parsed),
        }
        new_id = esi_auth.create_fitting(character_id, body)
        if new_id is None:
            return False

        with self._lock:
            fit.esi_fitting_ids[character_id] = new_id
            self.update_fit(fit)
            self.save()
        return True

    # ── Doctrine CRUD ─────────────────────────────────────────────────────────

    def add_doctrine(self, name: str, description: str = "") -> str:
        """Create a new, empty doctrine. Returns the assigned id."""
        with self._lock:
            did = uuid4().hex
            stamp = _now()
            self._doctrines[did] = Doctrine(
                id=did,
                name=name,
                description=description,
                members=[],
                created=stamp,
                modified=stamp,
            )
            return did

    def duplicate_doctrine(self, doctrine_id: str, name: str | None = None) -> str | None:
        """Copy an existing doctrine into a new one and return the new id.

        Deep-copies ``members`` (every `DoctrineMember`, incl. tags/order/
        ideal_*/seed_target), ``description``, ``exemptions`` (a deep-copied
        list, or None) and ``seed_target`` so editing the copy (e.g. swapping
        one or two members) can never mutate the source. Fits themselves are
        shared BY ID — that is the point: the library fit objects are not
        copied. Gets a fresh uuid and fresh created/modified stamps.

        ``name`` (stripped, non-empty) is used as the base when given,
        otherwise the base is "<source> (copy)". Either way the base is
        ALWAYS run through ``_unique_name`` against every existing doctrine
        name, so a caller-supplied name that collides gets the " (2)" suffix
        rather than creating a same-named twin (several fc_gui sites resolve
        doctrines BY NAME — `_active_fleet_doctrine`, doctrine combos, etc. —
        so a duplicate name would silently point guidance at the wrong copy).

        Returns None (and writes nothing) for an unknown ``doctrine_id``.
        Does NOT save — callers save, same as ``add_doctrine`` callers do.
        """
        with self._lock:
            src = self._doctrines.get(doctrine_id)
            if src is None:
                return None
            existing_names = {d.name for d in self._doctrines.values()}
            base = (name or "").strip() or f"{src.name} (copy)"
            new_name = self._unique_name(base, existing_names)
            did = uuid4().hex
            stamp = _now()
            self._doctrines[did] = Doctrine(
                id=did,
                name=new_name,
                description=src.description,
                members=copy.deepcopy(src.members),
                created=stamp,
                modified=stamp,
                exemptions=copy.deepcopy(src.exemptions),
                seed_target=src.seed_target,
            )
            return did

    def update_doctrine(self, doctrine: Doctrine) -> bool:
        """Replace an existing doctrine (by id), refreshing its modified stamp.

        Returns True if the doctrine existed and was updated, False for an
        unknown id. (Additive: callers ignoring the return are unaffected.)
        """
        with self._lock:
            if doctrine.id not in self._doctrines:
                return False
            doctrine.modified = _now()
            self._doctrines[doctrine.id] = doctrine
            return True

    def delete_doctrine(self, doctrine_id: str) -> None:
        with self._lock:
            self._doctrines.pop(doctrine_id, None)

    def get_doctrine(self, doctrine_id: str) -> Doctrine | None:
        with self._lock:
            return self._doctrines.get(doctrine_id)

    def list_doctrines(self) -> list[Doctrine]:
        with self._lock:
            return list(self._doctrines.values())

    # ── Membership (per-doctrine tags live on the link) ───────────────────────

    @staticmethod
    def _slot_of(doctrine: Doctrine, fit_id: str) -> DoctrineMember | None:
        """The slot flying `fit_id` — as its ACTIVE fit or as one of its refits.

        Actives are matched first (a full pass), so in the degenerate case where
        one id is a slot's active AND another slot's refit (only reachable by
        hand-editing the library — the store refuses to create it), the slot
        actually flying it wins. Returns the member object itself, so callers
        mutate the stored slot in place.
        """
        if not fit_id:
            return None
        for member in doctrine.members:
            if member.fit_id == fit_id:
                return member
        for member in doctrine.members:
            if fit_id in member.refits:
                return member
        return None

    @staticmethod
    def _normalise_slot(member: DoctrineMember) -> None:
        """Restore `fit_id in refits` on a slot that has refits but lost it.

        Only a hand-edited library can produce that shape (the loader tolerates
        it deliberately and never mutates). The active fit is what the slot is
        flying, so it is inserted at index 0 — it becomes the default as well.
        A plain member (no refits) and an already-consistent slot are untouched.

        Normalise ON MUTATION only: callers that are about to change the slot
        call this first so they never write a slot whose active fit is missing
        from its own refit list.
        """
        if member.refits and member.fit_id not in member.refits:
            member.refits.insert(0, member.fit_id)

    def find_member(self, doctrine_id: str, fit_id: str) -> DoctrineMember | None:
        """The doctrine's slot flying `fit_id`, matching its ACTIVE fit or any
        of its refits; None for an unknown doctrine or fit.

        THE membership lookup: `set_member_tags`, `set_member_ideal`,
        `set_member_seed_target` and `remove_fit_from_doctrine` all resolve
        through it, so a fit id captured before a refit swap (a menu built a
        moment ago, a dialog holding the id it opened with) still edits the right
        slot instead of silently missing.

        The returned member is the LIVE object — mutating it mutates the store
        (without stamping `modified` or saving; use the setters for that).
        """
        with self._lock:
            doctrine = self._doctrines.get(doctrine_id)
            if doctrine is None:
                return None
            return self._slot_of(doctrine, fit_id)

    def add_fit_to_doctrine(
        self, doctrine_id: str, fit_id: str, tags: list[str]
    ) -> bool:
        """Add a fit to a doctrine with its own tag list. Tags are copied so
        the same fit can carry different tags in another doctrine.

        Returns True when a member was added, False when the doctrine is unknown
        or the add was refused — so a caller can tell the user why nothing
        appeared instead of watching the row silently not arrive. (Additive: the
        method returned None before and every existing caller ignores the
        result, so nothing breaks.)

        It is refused when `fit_id` is already a REFIT of some slot in this
        doctrine: a fit belongs to at most one slot, and adding it as a second
        member would give the doctrine two rows for one ship whose refit chip
        and member row disagree. Adding a fit that is already another member's
        ACTIVE fit is still allowed, exactly as before — the Add-fit picker
        excludes it, and two members sharing a fit id is a pre-existing (if
        pointless) shape this change does not start rejecting.
        """
        with self._lock:
            doctrine = self._doctrines.get(doctrine_id)
            if doctrine is None:
                return False
            for member in doctrine.members:
                if fit_id in member.refits:
                    return False
            tags = list(tags or [])
            if self.catalog is not None and "Defenders" not in tags:
                fit = self.get_fit(fit_id)
                if fit is not None and fleet_guidance.has_defender_launcher(fit.parsed, self.catalog):
                    tags.append("Defenders")
            doctrine.members.append(
                DoctrineMember(fit_id=fit_id, tags=list(tags), order=len(doctrine.members))
            )
            doctrine.modified = _now()
            return True

    def set_member_tags(
        self, doctrine_id: str, fit_id: str, tags: list[str]
    ) -> None:
        """Replace the tag list of the (doctrine, fit) membership.

        Resolves the slot via `find_member`, so a refit id edits the slot that
        carries it (tags are slot-level: a refit does not change the ship's
        role). `modified` is stamped whenever the doctrine exists, matching the
        previous behaviour exactly, including for a fit that is not a member.
        """
        with self._lock:
            doctrine = self._doctrines.get(doctrine_id)
            if doctrine is None:
                return
            member = self._slot_of(doctrine, fit_id)
            if member is not None:
                member.tags = list(tags)
            doctrine.modified = _now()

    def set_member_ideal(
        self, doctrine_id: str, fit_id: str,
        mode: str | None, ideal_min: int | None, ideal_max: int | None,
    ) -> None:
        """Set the per-fit ideal (mode/min/max) on a (doctrine, fit) membership.

        Slot-level like the tags: resolved via `find_member`, so a refit id sets
        the ideal of the slot it belongs to (how many of this SHIP the FC wants
        does not depend on which refit it is flying).
        """
        with self._lock:
            doctrine = self._doctrines.get(doctrine_id)
            if doctrine is None:
                return
            member = self._slot_of(doctrine, fit_id)
            if member is not None:
                member.ideal_mode = mode
                member.ideal_min = ideal_min
                member.ideal_max = ideal_max
            doctrine.modified = _now()

    def set_member_seed_target(
        self, doctrine_id: str, fit_id: str, seed_target: int | None
    ) -> None:
        """Set the per-fit market seed target on a (doctrine, fit) membership.

        None = "inherit the doctrine seed target" (omitted from JSON); a positive
        int overrides it for THIS fit only — so a doctrine can seed e.g. 50
        stabbers, 20 scythes and 10 bifrosts. Mirrors ``set_member_tags`` /
        ``set_doctrine_seed_target``: mutates in memory + stamps ``modified``; the
        caller persists via ``save()`` like the sibling member setters.

        Slot-level: resolved via `find_member`, and the target covers EVERY refit
        of the slot (the market enumerators expand `slot_fit_ids` and apply the
        owning slot's resolved target to each variant).
        """
        with self._lock:
            doctrine = self._doctrines.get(doctrine_id)
            if doctrine is None:
                return
            member = self._slot_of(doctrine, fit_id)
            if member is not None:
                member.seed_target = seed_target
            doctrine.modified = _now()

    def set_doctrine_exemptions(
        self, doctrine_id: str, entries: list[dict] | None
    ) -> None:
        """Set the per-doctrine ideal-% exemption list.

        None = "use STANDARD_EXEMPTIONS" (omitted from JSON); [] = explicitly none;
        [...] = that explicit list. Copies the entries so external mutation cannot
        leak in. Round-trips via doctrine_to_dict/_from_dict.
        """
        with self._lock:
            doctrine = self._doctrines.get(doctrine_id)
            if doctrine is None:
                return
            doctrine.exemptions = None if entries is None else [dict(e) for e in entries]
            doctrine.modified = _now()

    def set_doctrine_seed_target(
        self, doctrine_id: str, seed_target: int | None
    ) -> None:
        """Set the per-doctrine market seed target (units of each fit to consider
        "fully seeded").

        None = "use the global config["market"]["seed_target"] default" (omitted
        from JSON); a positive int overrides it for this doctrine. Mirrors
        ``set_doctrine_exemptions``: mutates in memory + stamps ``modified``; the
        caller persists via ``save()`` like the sibling doctrine setters.
        """
        with self._lock:
            doctrine = self._doctrines.get(doctrine_id)
            if doctrine is None:
                return
            doctrine.seed_target = seed_target
            doctrine.modified = _now()

    def remove_fit_from_doctrine(self, doctrine_id: str, fit_id: str) -> None:
        """Remove a fit's SLOT from a doctrine's member list.

        Resolved via `find_member`, so any of the slot's fit ids (active or
        refit) removes the WHOLE slot — every refit with it. That is what the
        member row's red "Remove" button means: the doctrine no longer wants this
        ship. To drop a single variant and keep the slot, use `remove_refit`.

        `modified` is stamped whenever the doctrine exists, as before.
        """
        with self._lock:
            doctrine = self._doctrines.get(doctrine_id)
            if doctrine is None:
                return
            member = self._slot_of(doctrine, fit_id)
            if member is not None:
                doctrine.members = [m for m in doctrine.members if m is not member]
            doctrine.modified = _now()

    # ── Refits (alternate fits of one slot) ───────────────────────────────────
    #
    # All four mutators follow the membership family's contract: they mutate in
    # memory under `self._lock`, stamp `doctrine.modified`, and leave persistence
    # to the CALLER (`save()`), so a GUI action that touches several things still
    # writes the library once.

    def add_refit(
        self, doctrine_id: str, slot_fit_id: str, new_fit_id: str
    ) -> bool:
        """Add `new_fit_id` as a refit of the slot identified by `slot_fit_id`.

        `slot_fit_id` may be ANY of the slot's fit ids (its active fit or one of
        its existing refits). The first refit added to a plain member seeds
        ``refits = [active, new]`` — the fit already there becomes the default,
        and the slot keeps flying it.

        Returns False, mutating nothing, when: the doctrine, the slot or
        `new_fit_id` is unknown; the slot's ACTIVE fit is missing from the
        library (the same-hull rule cannot be checked against a fit that is not
        there, and guessing is worse than refusing); the new fit's hull differs
        from the active fit's (a refit is the same ship — a different hull is a
        different slot); or `new_fit_id` already appears anywhere in this
        doctrine, as another slot's active fit or as any slot's refit
        (uniqueness: one fit belongs to at most one slot). Adding a fit to itself
        falls under that last rule.

        Normalises first: a hand-edited slot whose `refits` somehow omits its own
        active fit gets it inserted at index 0 before the append, so the
        invariant `fit_id in refits` holds for everything this method writes.
        """
        with self._lock:
            doctrine = self._doctrines.get(doctrine_id)
            if doctrine is None:
                return False
            member = self._slot_of(doctrine, slot_fit_id)
            if member is None:
                return False
            new_fit = self._fits.get(new_fit_id)
            if new_fit is None:
                return False
            active_fit = self._fits.get(member.fit_id)
            if active_fit is None:
                return False
            if new_fit.hull_type_id != active_fit.hull_type_id:
                return False
            for other in doctrine.members:
                if other.fit_id == new_fit_id or new_fit_id in other.refits:
                    return False

            if not member.refits:
                member.refits = [member.fit_id, new_fit_id]
            else:
                self._normalise_slot(member)
                member.refits.append(new_fit_id)
            doctrine.modified = _now()
            return True

    def remove_refit(self, doctrine_id: str, fit_id: str) -> bool:
        """Drop ONE refit from its slot, keeping the slot.

        If the removed fit was the ACTIVE one, the slot falls back to the
        default (`refits[0]`) — or, when the default is what was removed, to the
        next remaining refit, which is the new `refits[0]` either way. When a
        single fit is left the slot collapses back to a plain member
        (``refits = []``): a one-entry refit list is the same thing spelled
        longer, and every "does this slot have refits" check is `if
        member.refits`.

        Returns False, mutating nothing, for an unknown doctrine or fit, or for a
        PLAIN member — removing the only fit a slot can fly is removing the slot,
        which is `remove_fit_from_doctrine`'s job, not a silent side effect of a
        refit-menu click.
        """
        with self._lock:
            doctrine = self._doctrines.get(doctrine_id)
            if doctrine is None:
                return False
            member = self._slot_of(doctrine, fit_id)
            if member is None or not member.refits:
                return False
            self._normalise_slot(member)
            was_active = member.fit_id == fit_id
            member.refits = [f for f in member.refits if f != fit_id]
            if was_active and member.refits:
                member.fit_id = member.refits[0]
            if len(member.refits) <= 1:
                member.refits = []
            doctrine.modified = _now()
            return True

    def set_active_refit(self, doctrine_id: str, fit_id: str) -> bool:
        """Make `fit_id` the slot's ACTIVE fit (the swap every consumer sees).

        Returns True without touching `modified` when the fit is ALREADY active:
        re-picking the current entry from a menu must not dirty the library (and
        must not make the caller save, bump `revision()` and invalidate every
        memo keyed on it). Returns False for an unknown doctrine or fit, or for a
        fit that is not a refit of any slot — a plain member has nothing to swap.
        """
        with self._lock:
            doctrine = self._doctrines.get(doctrine_id)
            if doctrine is None:
                return False
            member = self._slot_of(doctrine, fit_id)
            if member is None or not member.refits:
                return False
            if member.fit_id == fit_id:
                return True
            self._normalise_slot(member)
            member.fit_id = fit_id
            doctrine.modified = _now()
            return True

    def set_default_refit(self, doctrine_id: str, fit_id: str) -> bool:
        """Move `fit_id` to the head of its slot's refits (the DEFAULT).

        The default is what `reset_refits` returns the slot to; which refit is
        ACTIVE right now is left exactly as it was. Returns False for an unknown
        doctrine or fit, or for a plain member.

        Returns True without touching `modified` when the fit is ALREADY the
        default — same rationale as `set_active_refit`: re-picking the current
        entry from a menu must not dirty the library, and so must not make the
        caller save, bump `revision()` and miss every memo keyed on it.
        """
        with self._lock:
            doctrine = self._doctrines.get(doctrine_id)
            if doctrine is None:
                return False
            member = self._slot_of(doctrine, fit_id)
            if member is None or not member.refits:
                return False
            if member.refits[0] == fit_id:
                return True
            self._normalise_slot(member)
            if fit_id not in member.refits:
                return False
            member.refits = [fit_id] + [f for f in member.refits if f != fit_id]
            doctrine.modified = _now()
            return True

    def reset_refits(self, doctrine_id: str) -> int:
        """Return every slot to its DEFAULT refit; returns how many changed.

        The form-up reset. A return of 0 means nothing moved (no refits at all,
        or every slot was already on its default) and `modified` is NOT stamped,
        so the caller can skip the save. Deliberately does NOT normalise a
        hand-edited slot whose active fit is absent from its refits: such a slot
        counts as changed and lands on `refits[0]`, which is what "reset to the
        default" means there too.
        """
        with self._lock:
            doctrine = self._doctrines.get(doctrine_id)
            if doctrine is None:
                return 0
            changed = 0
            for member in doctrine.members:
                if not member.refits:
                    continue
                default = member.refits[0]
                if member.fit_id != default:
                    member.fit_id = default
                    changed += 1
            if changed:
                doctrine.modified = _now()
            return changed

    # ── Tag vocabulary ────────────────────────────────────────────────────────

    @property
    def tags(self) -> list[str]:
        with self._lock:
            return list(self._tags)

    def add_tag(self, name: str) -> None:
        """Add a new tag to the vocabulary (no-op if already present)."""
        with self._lock:
            if name and name not in self._tags:
                self._tags.append(name)

    def remove_tag(self, name: str) -> None:
        """Remove a tag from the vocabulary and strip it from every doctrine
        member that carries it."""
        with self._lock:
            if name in self._tags:
                self._tags.remove(name)
            for doctrine in self._doctrines.values():
                changed = False
                for member in doctrine.members:
                    if name in member.tags:
                        member.tags = [t for t in member.tags if t != name]
                        changed = True
                if changed:
                    doctrine.modified = _now()

    def rename_tag(self, old_name: str, new_name: str) -> bool:
        """Rename a tag in the vocabulary and cascade across every doctrine
        member that carries it, then persist.

        No-op (returns False) if `old_name` is not in the vocabulary or
        `new_name` already exists. Returns True on success.
        """
        with self._lock:
            if old_name not in self._tags or new_name in self._tags:
                return False
            # Rename in the vocabulary, preserving position.
            self._tags = [new_name if t == old_name else t for t in self._tags]
            # Cascade to every doctrine member carrying the old tag.
            for doctrine in self._doctrines.values():
                changed = False
                for member in doctrine.members:
                    if old_name in member.tags:
                        member.tags = [
                            new_name if t == old_name else t for t in member.tags
                        ]
                        changed = True
                if changed:
                    doctrine.modified = _now()
            self.save()
            return True

    # ── Share (.fctdoc) export / import ───────────────────────────────────────

    def export_doctrines(self, doctrine_ids: list[str]) -> dict:
        """Build a self-contained `.fctdoc` payload for the given doctrines.

        Embeds full copies of every fit referenced by any exported doctrine so
        the file can be imported into a clean store with no external data. The
        referenced-fit walk expands each slot through `slot_fit_ids`, so every
        REFIT travels too — a refit whose fit stayed behind would import as a
        chip the receiver cannot fly.
        """
        with self._lock:
            doctrines: list[dict] = []
            referenced_fit_ids: list[str] = []
            seen_fit_ids: set[str] = set()
            for did in doctrine_ids:
                doctrine = self._doctrines.get(did)
                if doctrine is None:
                    continue
                doctrines.append(doctrine_to_dict(doctrine))
                for member in doctrine.members:
                    for fid in slot_fit_ids(member):
                        if fid not in seen_fit_ids:
                            seen_fit_ids.add(fid)
                            referenced_fit_ids.append(fid)

            fits: list[dict] = []
            for fid in referenced_fit_ids:
                fit = self._fits.get(fid)
                if fit is not None:
                    fits.append(fit_to_dict(fit))

            return {
                "schema_version": SCHEMA_VERSION,
                "exported_at": _now(),
                "doctrines": doctrines,
                "fits": fits,
            }

    def import_share(self, payload: dict) -> ImportSummary:
        """Import a `.fctdoc` payload, de-duping fits by content hash.

        For each incoming fit: if a local fit has the same `fit_content_hash`,
        reuse the local id (counts as reused); otherwise add it with a fresh id
        (counts as added). An old-id → new/local-id map is built so each
        imported doctrine's member `fit_id`s are remapped to the right local
        fit. Doctrines are added with fresh ids; a name collision gets a numeric
        suffix (the GUI offers a rename). Tags are preserved.
        """
        with self._lock:
            fits_added = 0
            fits_reused = 0
            doctrines_added = 0

            # Index existing local fits by content hash for de-dupe.
            local_by_hash: dict[str, str] = {}
            for local_id, local_fit in self._fits.items():
                local_by_hash.setdefault(fit_content_hash(local_fit.parsed), local_id)

            # old (export) fit id -> local fit id
            id_map: dict[str, str] = {}
            for raw_fit in payload.get("fits") or []:
                incoming = fit_from_dict(raw_fit)
                old_id = incoming.id
                content = fit_content_hash(incoming.parsed)
                existing_id = local_by_hash.get(content)
                if existing_id is not None:
                    id_map[old_id] = existing_id
                    fits_reused += 1
                    continue
                # New fit: assign a fresh id and store.
                new_id = uuid4().hex
                incoming.id = new_id
                stamp = _now()
                if not incoming.created:
                    incoming.created = stamp
                incoming.modified = stamp
                self._fits[new_id] = incoming
                local_by_hash[content] = new_id
                id_map[old_id] = new_id
                fits_added += 1

            existing_names = {d.name for d in self._doctrines.values()}
            # A share exported by a pre-v2 build may carry a frozen copy of the
            # pre-5120 standard exemption list: upgrade it exactly as load()
            # does. A v2+ share's list is the exporter's deliberate choice.
            migrate_cc = (_stored_schema_version(payload)
                          < _CC_EXEMPTIONS_MIGRATED_VERSION)
            for raw_doctrine in payload.get("doctrines") or []:
                incoming = doctrine_from_dict(raw_doctrine)
                if migrate_cc:
                    upgraded = fleet_guidance.upgrade_pre_command_carrier_exemptions(
                        incoming.exemptions)
                    if upgraded is not None:
                        incoming.exemptions = upgraded
                new_id = uuid4().hex
                incoming.id = new_id
                incoming.name = self._unique_name(incoming.name, existing_names)
                existing_names.add(incoming.name)
                # Remap member fit ids; drop members whose fit didn't travel.
                #
                # This is the ONE place the store could mint a doctrine that
                # breaks its own uniqueness rule, because the content-hash
                # de-dupe above is name-blind: two DIFFERENT incoming fits with
                # identical modules ("AC" and "AC2", or one member's fit and
                # another's) map onto the SAME local id. Left alone that yields a
                # refit list carrying an id twice, or two slots flying one fit —
                # and `find_member`, which matches a single slot, would then edit
                # whichever row came first. So each remapped id is claimed at
                # most once per doctrine, first slot wins.
                remapped: list[DoctrineMember] = []
                claimed: set[str] = set()
                for member in incoming.members:
                    local_fit_id = id_map.get(member.fit_id)
                    # Refits are remapped the same way and the ones whose fit did
                    # not travel are dropped. If the ACTIVE fit is among those,
                    # the slot survives on its first surviving refit — the ship
                    # is still in the doctrine, just on a different variant. Only
                    # when NOTHING survived is the member dropped (today's rule).
                    # A single survivor collapses back to a plain member.
                    # `dict.fromkeys` de-dupes while keeping first-seen order.
                    local_refits = list(dict.fromkeys(
                        id_map[f] for f in member.refits if f in id_map
                    ))
                    # Anything an earlier slot of this doctrine already flies is
                    # not this slot's to carry.
                    local_refits = [f for f in local_refits if f not in claimed]
                    if local_fit_id in claimed:
                        local_fit_id = None
                    if local_fit_id is None:
                        if not local_refits:
                            continue
                        local_fit_id = local_refits[0]
                    if local_refits and local_fit_id not in local_refits:
                        local_refits.insert(0, local_fit_id)   # normalise (§ _normalise_slot)
                    if len(local_refits) <= 1:
                        local_refits = []
                    claimed.add(local_fit_id)
                    claimed.update(local_refits)
                    remapped.append(
                        DoctrineMember(
                            fit_id=local_fit_id,
                            # Rename legacy tags on import too, so a .fctdoc exported
                            # by an older build lands with the current tag vocabulary.
                            tags=_migrate_tag_list(member.tags),
                            order=len(remapped),
                            ideal_mode=member.ideal_mode,
                            ideal_min=member.ideal_min,
                            ideal_max=member.ideal_max,
                            # Carry the per-fit seed target across the import remap so
                            # a shared doctrine keeps its "50 stabbers / 20 scythes"
                            # seeding intent (field-drop guard).
                            seed_target=member.seed_target,
                            # Same field-drop guard: this explicit field-by-field
                            # reconstruction silently loses any DoctrineMember
                            # field not named here, so every new one must be
                            # added. Already remapped/pruned above.
                            refits=local_refits,
                        )
                    )
                incoming.members = remapped
                self._doctrines[new_id] = incoming
                doctrines_added += 1

            return ImportSummary(fits_added, fits_reused, doctrines_added)

    # ── Group-configuration merge (beside import_share, never through it) ─────

    def merge_share(
        self, payload: dict, *, on_conflict: str = "theirs",
        validated: bool = False, resolutions: dict | None = None,
    ) -> MergeResult:
        """Merge a share payload into the library BY NAME, all or nothing.

        The payload goes through `validate_share` first; one it refuses as a
        whole raises ``ValueError`` with the reason and nothing is touched.
        After that `_fits`, `_doctrines` and `_tags` are snapshotted, and
        restored before re-raising if anything below fails — the library is
        either merged or exactly as it was. Does NOT save: the caller does.

        ``validated=True`` is for a caller that hands over the CLEAN payload
        `validate_share` just gave it: the method then skips its own
        validation (a large file was validated three times on its way in).
        Anything else passed that way is still merged all or nothing — it
        raises and the library is as it was — but nothing here cleans it.

        * **Fits** — a fit whose content hash a local fit already has is that
          local fit (its id, its name, its in-game fitting ids); any other is
          added under a new id. Two incoming fits with one content are one.
          Under "mine", a fit that ONLY skipped doctrines name is declined
          with them — not added, not reported; a fit a written doctrine
          names, or no doctrine at all, is handled as always.
        * **Doctrines** — matched by exact name against the FIRST local
          doctrine of that name. No match: added under a new id. A match and
          ``on_conflict`` "mine" (blanks and case aside): skipped. A match
          otherwise ("theirs", the default, and any other value): replaced in
          place — the local `id` and `created` stay, so everything holding
          the doctrine by id or by name still finds it — unless nothing
          differs. Which refit of a slot is ACTIVE is the importer's own
          state and no difference (`_same_slots`).
        * **Slots** — fit ids are remapped. A fit an earlier slot of the same
          doctrine already flies is dropped from the later one; a refit on
          another hull than the slot's first surviving fit is dropped; on a
          doctrine that is WRITTEN the active fit is the first survivor (the
          default: a share does not carry which refit someone happened to
          have active); one survivor is a plain member; none drops the slot.
          `order` travels as it is.
        * **Tags** — a member tag the vocabulary lacks is appended to it
          (otherwise the next tag edit would strip it silently).

        * **Per-doctrine choices** (``resolutions``, a group import; ``None``
          or ``{}`` is everything above, exactly) - keyed by the INCOMING
          doctrine name, ``(action, local doctrine id)``: "theirs" writes the
          incoming doctrine into that local one in place, keeping its `id`,
          `created` AND name; "mine" skips it and declines the fits only
          skipped doctrines name; "both" adds it as a new doctrine; "merge"
          merges it into that local one (`_merge_into`). A choice
          that no longer holds - its local doctrine is gone, two choices or a
          same-named incoming doctrine claim one local doctrine, "both" onto a
          name the library has exactly, any other action - follows the rules
          above and leaves one line in
          `MergeResult.resolution_fallbacks`; it never refuses the merge.
          `MergeResult.aliases` says where honoured choices landed and
          `MergeResult.honoured` which choices were honoured.
        * **Merge** (a "merge" choice, or ``on_conflict`` "merge" for every
          exact-name match without a choice) - "keep mine, add what the group
          has that I don't", in MY doctrine: my id, created, name,
          exemptions, seed target and every slot value stay; the group's
          fits, slots and slot tags I lack are added (`_merge_into`); the
          description is filled only when mine is blank. Nothing is ever
          removed, and merging the same payload again adds nothing (the
          doctrine is then `doctrines_unchanged`).

        Slot drops, new tags and orphans are reported for doctrines this call
        WROTE; a skipped or unchanged doctrine reports nothing. See
        `MergeResult`."""
        if validated:
            clean = payload
        else:
            clean, problems = validate_share(payload)
            if clean is None:
                raise ValueError(
                    problems[0] if problems else "not a fittings payload")
        keep_mine = str(on_conflict).strip().lower() == "mine"
        merge_all = str(on_conflict).strip().lower() == "merge"
        result = MergeResult()
        with self._lock:
            snapshot = (copy.deepcopy(self._fits), copy.deepcopy(self._doctrines),
                        list(self._tags))
            try:
                stamp = _now()

                # Doctrines are matched by name, first local match.
                first_by_name: dict[str, Doctrine] = {}
                for local in self._doctrines.values():
                    first_by_name.setdefault(local.name, local)

                # The importer's per-doctrine choices that still hold:
                # incoming name -> (action, local doctrine).
                chosen = self._honoured_choices(
                    clean["doctrines"], resolutions, first_by_name, keep_mine,
                    result, merge_all=merge_all)

                # "Keep mine" declines a doctrine whole: the fits only such
                # doctrines name (as a slot's fit or a refit) stay out too,
                # or they would land in the library as orphans.
                declined: set[str] = set()         # payload fit ids
                if keep_mine or chosen:
                    wanted: set[str] = set()
                    for raw_doctrine in clean["doctrines"]:
                        choice = chosen.get(raw_doctrine["name"])
                        skip = (choice[0] == "mine" if choice is not None
                                else keep_mine
                                and raw_doctrine["name"] in first_by_name)
                        named = declined if skip else wanted
                        for raw_member in raw_doctrine["members"]:
                            named.add(raw_member["fit_id"])
                            named.update(raw_member.get("refits") or ())
                    declined -= wanted

                # Fits: content-hash de-dupe against the library and each other.
                local_by_hash: dict[str, str] = {}
                for local_id, local_fit in self._fits.items():
                    local_by_hash.setdefault(
                        fit_content_hash(local_fit.parsed), local_id)
                id_map: dict[str, str] = {}        # payload fit id -> local fit id
                names: dict[str, str] = {}         # payload fit id -> its own name
                reported: set[str] = set()         # local ids already in the result
                for raw_fit in clean["fits"]:
                    if raw_fit["id"] in declined:
                        continue
                    incoming = fit_from_dict(raw_fit)
                    names[incoming.id] = incoming.name
                    content = fit_content_hash(incoming.parsed)
                    local_id = local_by_hash.get(content)
                    if local_id is None:
                        local_id = uuid4().hex
                        id_map[incoming.id] = local_id
                        incoming.id = local_id
                        incoming.esi_fitting_ids = {}
                        incoming.created = incoming.modified = stamp
                        self._fits[local_id] = incoming
                        local_by_hash[content] = local_id
                        reported.add(local_id)
                        result.fits_added.append(incoming.name)
                        continue
                    id_map[incoming.id] = local_id
                    if local_id not in reported:
                        reported.add(local_id)
                        result.fits_reused.append(self._fits[local_id].name)

                # Doctrines.
                released: list[str] = []           # fits replaced doctrines flew
                vocabulary = set(self._tags)       # one build, not a scan per tag
                for raw_doctrine in clean["doctrines"]:
                    incoming = doctrine_from_dict(raw_doctrine)
                    choice = chosen.get(incoming.name)
                    if choice is None:
                        local = first_by_name.get(incoming.name)
                        skip = local is not None and keep_mine
                        merging = local is not None and merge_all
                    else:
                        local = None if choice[0] == "both" else choice[1]
                        skip = choice[0] == "mine"
                        merging = choice[0] == "merge"
                    if skip:
                        result.doctrines_skipped.append(incoming.name)
                        continue
                    dropped: list[str] = []
                    members = self._merge_slots(incoming, id_map, names, dropped)
                    if merging:
                        # Keep mine, add what the group has that I don't. A
                        # line names a fit as the GROUP's doctrine does.
                        shown_as: dict[str, str] = {}
                        for member in incoming.members:
                            for old_id in (member.refits or [member.fit_id]):
                                if old_id in id_map:
                                    shown_as.setdefault(id_map[old_id],
                                                        names.get(old_id, ""))
                        members, counts = self._merge_into(
                            local, members, name=incoming.name, dropped=dropped,
                            shown_as=shown_as)
                        mine = local.description
                        fill = (not (isinstance(mine, str) and mine.strip())
                                and bool(incoming.description.strip()))
                        if not (any(counts) or fill):
                            result.doctrines_unchanged.append(incoming.name)
                            continue
                        if fill:
                            local.description = incoming.description
                        local.members = members
                        local.modified = stamp
                        result.doctrines_updated.append(incoming.name)
                        result.merged.append((incoming.name, local.name, counts))
                    elif local is None:
                        incoming.id = uuid4().hex
                        incoming.members = members
                        incoming.created = incoming.modified = stamp
                        self._doctrines[incoming.id] = incoming
                        first_by_name[incoming.name] = incoming
                        result.doctrines_added.append(incoming.name)
                    elif (local.description == incoming.description
                          and self._same_slots(local.members, members)
                          and local.exemptions == incoming.exemptions
                          and local.seed_target == incoming.seed_target):
                        result.doctrines_unchanged.append(incoming.name)
                        continue
                    else:
                        released.extend(doctrine_fit_ids(local, True))
                        if choice is None:         # a choice keeps MY name
                            local.name = incoming.name
                        local.description = incoming.description
                        local.members = members
                        local.exemptions = incoming.exemptions
                        local.seed_target = incoming.seed_target
                        local.modified = stamp
                        result.doctrines_updated.append(incoming.name)
                    result.members_dropped.extend(dropped)
                    for member in members:
                        for tag in member.tags:
                            if tag not in vocabulary:
                                vocabulary.add(tag)
                                self._tags.append(tag)
                                result.tags_added.append(tag)

                if released:
                    in_use: set[str] = set()
                    for doctrine in self._doctrines.values():
                        in_use.update(doctrine_fit_ids(doctrine, True))
                    for fit_id in dict.fromkeys(released):
                        fit = self._fits.get(fit_id)
                        if fit is not None and fit_id not in in_use:
                            result.orphaned_fits.append(fit.name)
            except BaseException:
                self._fits, self._doctrines, self._tags = snapshot
                raise
        return result

    def _honoured_choices(
        self, doctrines: list, resolutions, first_by_name: dict,
        keep_mine: bool, result: MergeResult, *, merge_all: bool = False,
    ) -> dict:
        """`merge_share`'s per-doctrine choices that still hold, as
        ``{incoming name: (action, local Doctrine)}``; `result` gets the
        aliases and the `honoured` record of those, and one fallback line per
        choice that does not.

        Only a name of the payload's doctrines is looked at (a choice for a
        doctrine that is not being merged is no choice). A choice falls back
        when its action is not one this build takes, its local doctrine is
        gone, another "theirs" / "mine" / "merge" choice names the same local
        doctrine (a "both" never touches it, so it counts for nothing there),
        "both" would make a name twin, or - for "theirs" / "mine" / "merge" -
        an incoming doctrine of that local doctrine's exact name follows the
        default rule onto it (repeated until nothing changes: a fallback can
        claim in its turn). A "merge" onto a doctrine of another name leaves
        an alias like "theirs" does (``keep_mine`` False); one onto the same
        name leaves none. ``merge_all`` is ``on_conflict`` "merge": only the
        wording of a fallback line onto a same-named doctrine."""
        if not resolutions or not isinstance(resolutions, dict):
            return {}
        names = [raw["name"] for raw in doctrines]
        reasons: dict[str, str] = {}
        chosen: dict[str, tuple] = {}
        keys: dict[str, str] = {}               # incoming name -> the local key
        for name in names:
            if name not in resolutions:
                continue
            entry = resolutions[name]
            action, local_id = (entry if isinstance(entry, tuple) and len(entry) == 2
                                else (None, None))
            local = (self._doctrines.get(local_id)
                     if isinstance(local_id, str) else None)
            if action not in _RESOLUTION_ACTIONS:
                reasons[name] = "the choice for it could not be read"
            elif local is None:
                reasons[name] = ("the doctrine of yours chosen for it is no "
                                 "longer in your library")
            elif action == "both" and name in first_by_name:
                reasons[name] = "you have a doctrine of that name now"
            else:
                chosen[name] = (action, local)
                keys[name] = local_id
        # Only a choice that writes or keeps the local doctrine claims it.
        uses: dict[int, int] = {}
        for action, local in chosen.values():
            if action != "both":
                uses[id(local)] = uses.get(id(local), 0) + 1
        for name, (action, local) in list(chosen.items()):
            if action != "both" and uses[id(local)] > 1:
                del chosen[name]
                reasons[name] = (f"your {_share_shown(local.name)} was chosen "
                                 f"for two doctrines")
        while True:
            claimed = {id(first_by_name[name]) for name in names
                       if name not in chosen and name in first_by_name}
            taken = [name for name, (action, local) in chosen.items()
                     if action != "both" and id(local) in claimed]
            if not taken:
                break
            for name in taken:
                local = chosen.pop(name)[1]
                reasons[name] = (f"your {_share_shown(local.name)} is taken by "
                                 f"the group's doctrine of that name")
        for name in names:
            if name in reasons:
                if name not in first_by_name:
                    default = "added as a new doctrine"
                elif keep_mine:
                    default = "kept yours"
                elif merge_all:
                    default = "merged into yours"
                else:
                    default = "the group's version"
                result.resolution_fallbacks.append(
                    f"{_share_shown(name)}: {reasons[name]} - {default} instead")
        for name, (action, local) in chosen.items():
            result.honoured[name] = (action, keys[name])
            if action == "mine":
                result.aliases[name] = (local.name, True)
            elif action in ("theirs", "merge") and local.name != name:
                result.aliases[name] = (local.name, False)
        return chosen

    def _merge_slots(
        self, incoming: Doctrine, id_map: dict[str, str],
        names: dict[str, str], dropped: list[str],
    ) -> list[DoctrineMember]:
        """`incoming`'s slots remapped onto local fit ids, by `merge_share`'s
        slot rules; every fit left out for a reason is appended to `dropped`.

        `dataclasses.replace` carries every other slot field across, so a
        field added to `DoctrineMember` later travels without being named
        here (the field-drop hazard `import_share` documents)."""
        slots: list[DoctrineMember] = []
        claimed: set[str] = set()                  # local ids earlier slots fly
        for member in incoming.members:
            survivors: list[str] = []
            kept: set[str] = set()                 # `survivors`, for the lookup
            hull = None
            for old_id in (member.refits or [member.fit_id]):
                local_id = id_map.get(old_id)
                if local_id is None or local_id in kept:
                    continue                       # not in the file / its own twin
                label = f"{incoming.name} / {names.get(old_id, '')}"
                if local_id in claimed:
                    dropped.append(f"{label}: same fit as an earlier slot")
                    continue
                fit_hull = self._fits[local_id].hull_type_id
                if not survivors:
                    hull = fit_hull
                elif fit_hull != hull:
                    dropped.append(f"{label}: refit on another hull")
                    continue
                survivors.append(local_id)
                kept.add(local_id)
            if not survivors:
                continue
            claimed.update(survivors)
            slots.append(replace(
                member,
                fit_id=survivors[0],
                tags=_migrate_tag_list(member.tags),
                refits=survivors if len(survivors) > 1 else [],
            ))
        return slots

    def _merge_into(
        self, local: Doctrine, members: list[DoctrineMember], *,
        name: str = "", dropped: list[str] | None = None,
        shown_as: dict[str, str] | None = None,
    ) -> tuple[list[DoctrineMember], MergeCounts]:
        """`local`'s slots with what `members` (an incoming doctrine's slots,
        already remapped by `_merge_slots`) has that they lack, and how much
        that is. Reads only: `local` is not modified.

        "Keep mine, add what the group has that I don't" (spec section 6).
        Fits are compared BY CONTENT (`fit_content_hash`): one fit stored
        under two ids is one fit. Each group slot, in the group's order:

        * pairs with my FIRST still-unpaired slot of its hull that already
          flies one of its fits (any refit); failing that, when it brings a
          fit my doctrine does not fly yet, with my first still-unpaired slot
          of its hull. The pair keeps my active fit, my default
          (``refits[0]``) and every value of mine; the group's fits it lacks
          are appended as refits (a plain slot of mine becomes ``refits = [my
          fit, ...]``) and the group's tags it lacks are appended to its tags.
          So tags only ever land on a slot that shares or gains a fit.
        * a slot of the group with a new fit and no slot of mine left to pair
          with is appended after mine, with its own values and an `order`
          after my highest (kept in ``0 .. MAX_SHARE_COUNT``, the range
          `validate_share` takes); it counts as paired.
        * a fit another slot of my doctrine already flies is never added a
          second time - one its partner has is silently there, any other is
          dropped and said in `dropped` (``"<name> / <the group's fit name>:
          same fit as another slot"``). A group slot ALL of whose fits other
          slots of mine fly adds nothing - no tag, no slot - and pairs with
          nothing.

        A slot only I have is kept. A written slot of mine whose active fit is
        not among its refits (hand-edited) gets it first, as
        `_normalise_slot` does. Refits share the slot's hull: a paired slot's
        by the pairing, an appended slot's by `_merge_slots`. A second merge
        of the same slots pairs every group slot as the first did and adds
        nothing. `shown_as` maps a local fit id to the group's name for it."""
        fits = self._fits
        contents: dict[str, object] = {}

        def content(fit_id):
            if fit_id not in contents:
                fit = fits.get(fit_id)
                contents[fit_id] = (fit_content_hash(fit.parsed) if fit is not None
                                    else ("no such fit", fit_id))
            return contents[fit_id]

        def hull_of(fit_id):
            fit = fits.get(fit_id)
            return fit.hull_type_id if fit is not None else None

        def said(fit_id):
            if dropped is not None:
                label = (shown_as or {}).get(fit_id)
                if label is None:
                    label = fits[fit_id].name
                dropped.append(f"{name} / {label}: same fit as another slot")

        out = list(local.members)
        held: list[set] = []                     # per slot of mine: its contents
        flown: set = set()                       # every content my doctrine flies
        unpaired: dict = {}                      # hull -> my unpaired slots, in order
        for index, member in enumerate(out):
            held.append({content(fit_id) for fit_id in [member.fit_id, *member.refits]})
            flown |= held[index]
            unpaired.setdefault(hull_of(member.fit_id), []).append(index)
        order = max((member.order for member in out if type(member.order) is int),
                    default=-1) + 1
        added_slots: list[DoctrineMember] = []
        refits = tags = 0

        for member in members:
            ids = slot_fit_ids(member)
            wanted = {content(fit_id) for fit_id in ids}
            free = unpaired.get(hull_of(member.fit_id), [])
            partner = next((index for index in free if held[index] & wanted), None)
            new_fits: list[str] = []
            for fit_id in ids:
                key = content(fit_id)
                if partner is not None and key in held[partner]:
                    continue                     # its partner flies it already
                if key in flown:
                    if not any(content(new) == key for new in new_fits):
                        said(fit_id)
                    continue
                flown.add(key)
                new_fits.append(fit_id)
            if partner is None:
                if not new_fits:
                    continue                     # other slots of mine fly it all
                if not free:
                    added_slots.append(replace(
                        member,
                        fit_id=new_fits[0],
                        tags=list(member.tags),
                        refits=new_fits if len(new_fits) > 1 else [],
                        order=min(max(order, 0), MAX_SHARE_COUNT),
                    ))
                    order += 1
                    continue
                partner = free[0]
            free.remove(partner)
            current = out[partner]
            new_tags = [tag for tag in member.tags if tag not in current.tags]
            if not (new_fits or new_tags):
                continue
            base = list(current.refits)
            if base and current.fit_id not in base:
                base.insert(0, current.fit_id)   # as _normalise_slot does
            if new_fits and not base:
                base = [current.fit_id]
            out[partner] = replace(current, tags=list(current.tags) + new_tags,
                                   refits=base + new_fits)
            refits += len(new_fits)
            tags += len(new_tags)
        return out + added_slots, MergeCounts(refits, len(added_slots), tags)

    @staticmethod
    def _same_slots(mine: list[DoctrineMember], theirs: list[DoctrineMember]) -> bool:
        """Whether a local doctrine's slots already ARE the incoming ones.

        Which refit of a slot is active is not compared: a share does not
        carry it (`_merge_slots` always hands over the first), it is the
        importer's own state, so on its own it is no reason to rewrite the
        doctrine and reset it. Only a sound local slot gets that pass — one
        whose active fit is not among its refits differs, and the rewrite
        normalises it."""
        return len(mine) == len(theirs) and all(
            (replace(a, fit_id=b.fit_id) == b and a.fit_id in a.refits)
            if a.refits else a == b
            for a, b in zip(mine, theirs))

    @staticmethod
    def _unique_name(name: str, existing: set[str]) -> str:
        """Return `name`, or `name (2)`, `name (3)`, … if it collides."""
        if name not in existing:
            return name
        suffix = 2
        while f"{name} ({suffix})" in existing:
            suffix += 1
        return f"{name} ({suffix})"
