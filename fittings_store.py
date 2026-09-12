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
import shutil
import threading
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

SCHEMA_VERSION = 1

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
        consistent with existing behavior."""
        with self._lock:
            if not os.path.exists(self.path):
                self._fits = {}
                self._doctrines = {}
                self._tags = list(DEFAULT_TAGS)
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
            payload = {
                "schema_version": SCHEMA_VERSION,
                "fits": {fid: fit_to_dict(fit) for fid, fit in self._fits.items()},
                "doctrines": {
                    did: doctrine_to_dict(d) for did, d in self._doctrines.items()
                },
                "tags": list(self._tags),
            }
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
    ) -> None:
        """Add a fit to a doctrine with its own tag list. Tags are copied so
        the same fit can carry different tags in another doctrine.

        Silently no-ops (today's style — the return value is None and callers
        don't check) when `fit_id` is already a REFIT of some slot in this
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
                return
            for member in doctrine.members:
                if fit_id in member.refits:
                    return
            tags = list(tags or [])
            if self.catalog is not None and "Defenders" not in tags:
                fit = self.get_fit(fit_id)
                if fit is not None and fleet_guidance.has_defender_launcher(fit.parsed, self.catalog):
                    tags.append("Defenders")
            doctrine.members.append(
                DoctrineMember(fit_id=fit_id, tags=list(tags), order=len(doctrine.members))
            )
            doctrine.modified = _now()

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
        """
        with self._lock:
            doctrine = self._doctrines.get(doctrine_id)
            if doctrine is None:
                return False
            member = self._slot_of(doctrine, fit_id)
            if member is None or not member.refits:
                return False
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
            for raw_doctrine in payload.get("doctrines") or []:
                incoming = doctrine_from_dict(raw_doctrine)
                new_id = uuid4().hex
                incoming.id = new_id
                incoming.name = self._unique_name(incoming.name, existing_names)
                existing_names.add(incoming.name)
                # Remap member fit ids; drop members whose fit didn't travel.
                remapped: list[DoctrineMember] = []
                for member in incoming.members:
                    local_fit_id = id_map.get(member.fit_id)
                    # Refits are remapped the same way and the ones whose fit did
                    # not travel are dropped. If the ACTIVE fit is among those,
                    # the slot survives on its first surviving refit — the ship
                    # is still in the doctrine, just on a different variant. Only
                    # when NOTHING survived is the member dropped (today's rule).
                    # A single survivor collapses back to a plain member.
                    local_refits = [
                        id_map[f] for f in member.refits if f in id_map
                    ]
                    if local_fit_id is None:
                        if not local_refits:
                            continue
                        local_fit_id = local_refits[0]
                    if local_refits and local_fit_id not in local_refits:
                        local_refits.insert(0, local_fit_id)   # normalise (§ _normalise_slot)
                    if len(local_refits) <= 1:
                        local_refits = []
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

    @staticmethod
    def _unique_name(name: str, existing: set[str]) -> str:
        """Return `name`, or `name (2)`, `name (3)`, … if it collides."""
        if name not in existing:
            return name
        suffix = 2
        while f"{name} ({suffix})" in existing:
            suffix += 1
        return f"{name} ({suffix})"
