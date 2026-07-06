"""Fittings library persistence + fit/doctrine CRUD + share import/export.

`FittingsStore` owns `fittings_library.json` in `app_dir()`: a flat store of
`Fit`s and `Doctrine`s plus the user-extensible tag vocabulary. Writes are
atomic (temp file + `os.replace`, mirroring `esi_auth._save_tokens`) so a crash
mid-write cannot corrupt the library.

Doctrine membership tags live on the doctrine↔fit link (`DoctrineMember.tags`),
so the same fit can be tagged differently in different doctrines. Deleting a fit
cascades — it is removed from every doctrine's member list.

`.fctdoc` share files are self-contained: they embed full copies of the
referenced fits, so importing into a clean store needs no external data. Import
de-dupes fits by `fit_content_hash` (identical parsed content reuses the local
fit) and remaps ids so incoming doctrine memberships point at the right local
fit.

Pure logic: no Tkinter, no network.
"""

from __future__ import annotations

import json
import os
import shutil
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
    """In-memory fit/doctrine library backed by an atomic JSON file."""

    def __init__(self, path: str):
        self.path = path
        self._fits: dict[str, Fit] = {}
        self._doctrines: dict[str, Doctrine] = {}
        self._tags: list[str] = list(DEFAULT_TAGS)
        # Optional duck-typed TypeCatalog (group_of/resolve_name), set by the GUI
        # after construction. Used to auto-tag fits that mount a Defender Launcher.
        self.catalog = None

    # ── Persistence ───────────────────────────────────────────────────────────

    def load(self) -> None:
        """Load the library from disk. A missing or unreadable file seeds an
        empty library with the default tag vocabulary."""
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

        self._fits = {
            fid: fit_from_dict(raw)
            for fid, raw in (data.get("fits") or {}).items()
        }
        self._doctrines = {
            did: doctrine_from_dict(raw)
            for did, raw in (data.get("doctrines") or {}).items()
        }
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
        """Atomically persist the library (temp file + fsync + os.replace)."""
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

    # ── Fit CRUD ──────────────────────────────────────────────────────────────

    def add_fit(self, fit: Fit) -> str:
        """Add a fit, assigning a fresh id and created/modified stamps if the
        fit doesn't already carry them. Returns the assigned id."""
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
        if fit.id not in self._fits:
            return False
        fit.modified = _now()
        self._fits[fit.id] = fit
        return True

    def delete_fit(self, fit_id: str) -> None:
        """Delete a fit and cascade-remove it from every doctrine's members."""
        self._fits.pop(fit_id, None)
        for doctrine in self._doctrines.values():
            doctrine.members = [
                m for m in doctrine.members if m.fit_id != fit_id
            ]

    def get_fit(self, fit_id: str) -> Fit | None:
        return self._fits.get(fit_id)

    def list_fits(self) -> list[Fit]:
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
        """
        fit = self._fits.get(fit_id)
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

        fit.esi_fitting_ids[character_id] = new_id
        self.update_fit(fit)
        self.save()
        return True

    # ── Doctrine CRUD ─────────────────────────────────────────────────────────

    def add_doctrine(self, name: str, description: str = "") -> str:
        """Create a new, empty doctrine. Returns the assigned id."""
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

    def update_doctrine(self, doctrine: Doctrine) -> bool:
        """Replace an existing doctrine (by id), refreshing its modified stamp.

        Returns True if the doctrine existed and was updated, False for an
        unknown id. (Additive: callers ignoring the return are unaffected.)
        """
        if doctrine.id not in self._doctrines:
            return False
        doctrine.modified = _now()
        self._doctrines[doctrine.id] = doctrine
        return True

    def delete_doctrine(self, doctrine_id: str) -> None:
        self._doctrines.pop(doctrine_id, None)

    def get_doctrine(self, doctrine_id: str) -> Doctrine | None:
        return self._doctrines.get(doctrine_id)

    def list_doctrines(self) -> list[Doctrine]:
        return list(self._doctrines.values())

    # ── Membership (per-doctrine tags live on the link) ───────────────────────

    def add_fit_to_doctrine(
        self, doctrine_id: str, fit_id: str, tags: list[str]
    ) -> None:
        """Add a fit to a doctrine with its own tag list. Tags are copied so
        the same fit can carry different tags in another doctrine."""
        doctrine = self._doctrines.get(doctrine_id)
        if doctrine is None:
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
        """Replace the tag list of the (doctrine, fit) membership."""
        doctrine = self._doctrines.get(doctrine_id)
        if doctrine is None:
            return
        for member in doctrine.members:
            if member.fit_id == fit_id:
                member.tags = list(tags)
        doctrine.modified = _now()

    def set_member_ideal(
        self, doctrine_id: str, fit_id: str,
        mode: str | None, ideal_min: int | None, ideal_max: int | None,
    ) -> None:
        """Set the per-fit ideal (mode/min/max) on a (doctrine, fit) membership."""
        doctrine = self._doctrines.get(doctrine_id)
        if doctrine is None:
            return
        for member in doctrine.members:
            if member.fit_id == fit_id:
                member.ideal_mode = mode
                member.ideal_min = ideal_min
                member.ideal_max = ideal_max
        doctrine.modified = _now()

    def set_doctrine_exemptions(
        self, doctrine_id: str, entries: list[dict] | None
    ) -> None:
        """Set the per-doctrine ideal-% exemption list.

        None = "use STANDARD_EXEMPTIONS" (omitted from JSON); [] = explicitly none;
        [...] = that explicit list. Copies the entries so external mutation cannot
        leak in. Round-trips via doctrine_to_dict/_from_dict.
        """
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
        doctrine = self._doctrines.get(doctrine_id)
        if doctrine is None:
            return
        doctrine.seed_target = seed_target
        doctrine.modified = _now()

    def remove_fit_from_doctrine(self, doctrine_id: str, fit_id: str) -> None:
        """Remove a fit from a doctrine's member list."""
        doctrine = self._doctrines.get(doctrine_id)
        if doctrine is None:
            return
        doctrine.members = [m for m in doctrine.members if m.fit_id != fit_id]
        doctrine.modified = _now()

    # ── Tag vocabulary ────────────────────────────────────────────────────────

    @property
    def tags(self) -> list[str]:
        return list(self._tags)

    def add_tag(self, name: str) -> None:
        """Add a new tag to the vocabulary (no-op if already present)."""
        if name and name not in self._tags:
            self._tags.append(name)

    def remove_tag(self, name: str) -> None:
        """Remove a tag from the vocabulary and strip it from every doctrine
        member that carries it."""
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
        the file can be imported into a clean store with no external data.
        """
        doctrines: list[dict] = []
        referenced_fit_ids: list[str] = []
        seen_fit_ids: set[str] = set()
        for did in doctrine_ids:
            doctrine = self._doctrines.get(did)
            if doctrine is None:
                continue
            doctrines.append(doctrine_to_dict(doctrine))
            for member in doctrine.members:
                if member.fit_id not in seen_fit_ids:
                    seen_fit_ids.add(member.fit_id)
                    referenced_fit_ids.append(member.fit_id)

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
                if local_fit_id is None:
                    continue
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
