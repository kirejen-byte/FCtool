"""Group configuration: the six Fittings items (spec section 6.5).

==========================  =====  ==========================================================
item                        order  what it carries
==========================  =====  ==========================================================
``fittings.tag_categories``    10  the tag vocabulary (``fittings_library.json`` -> ``tags``)
``fittings.doctrines``         20  doctrines with every fit and refit they name
``fittings.fits``              30  the library fits no doctrine names
``fittings.seed``              40  the default seed target (``market.seed_target``, 1-9,999)
``fittings.motds``             50  MOTD templates (``fittings.saved_motds``)
``fittings.motd_settings``     60  MOTD length limit and remembered logi channel
==========================  =====  ==========================================================

The three library items never parse a fit or a doctrine themselves. They run the library
through the store, in memory: ``FittingsStore.load_data`` reads ``state.fittings``,
``validate_share`` / ``merge_share`` do the checking and the merging, ``dump`` gives the
result back. No file is opened here - ``group_state`` reads and writes the files. A library
written by a newer build is never touched: the item is skipped.

What never travels (owner privacy rule; scrubbed on export AND again on import):

* ``esi_fitting_ids`` - which of the exporter's characters have a fit saved in game;
* which refit a slot happened to have active - a slot travels on its default, ``refits[0]``;
* a MOTD template's ``fc`` (the FC's character name), a pinned-FC pill (it becomes "follow
  the selected FC"), ``legacy_fits`` and every pill kind this build does not know;
* a pill whose params hold ``<``, ``>`` or a control character, or a fit pill whose ``dna``
  is not a DNA (or holds a number of more than nine digits): the app writes pill params into
  the MOTD markup as they are (free text is escaped, pills are not), so such a pill would be
  markup in the importer's fleet MOTD. How MANY units a DNA names is not bounded here - a
  capital fit carries fuel and ammunition by the hundred thousand: what the app builds for a
  DNA it renders is bounded by the parser (``fit_parser.parse_dna``);
* a library fit or doctrine with ``<``, ``>`` or a control character in a name, or a fit
  whose stored DNA is not one. Pills are NOT the only way file text reaches the markup: the
  app labels the fit links of a tag line with the fit's hull name (else its name), gives a
  refit-following pill the active fit's name, and falls back to the stored DNA when it
  cannot rebuild one - none of them escaped. ``fittings_store.validate_share`` is what
  closes that (it drops such a fit or doctrine whole); the items here only call it;
* ``fittings.motd_link`` and ``fittings.motd_recent_tokens`` - never read, never written.

An export carries only what an import would accept: the doctrines and the fits items run
their own payload through ``validate_share``, the templates through the scrub, and
``export_notes`` names what that left out.

A ``char`` pill - a character link the author placed in the MOTD text by hand - is content
and travels as written; the own-name sweep that runs before a file is saved covers the
exporter's own characters.

A file never carries a MOTD template in the old (pre-composer) format: the export migrates
the exporter's own, and one found in a file is left out, never migrated.

Pure: stdlib, ``fittings_store``, ``motd_doc``, ``group_bundle`` and ``group_items`` only.
No Tk, no ``ui_theme``, no ``fc_gui`` - this module runs in the boot hook.
"""

import json
import re

import fittings_store
import motd_doc
from fit_models import fit_to_dict
from fittings_store import FittingsStore
from group_bundle import MAX_NAME, clean_text
from group_items import (ABSENT, ConfigFieldsItem, Field, Item, ItemResult, Record, get_path,
                         set_path)


MAX_TAGS = 100                      # tag vocabulary entries in one file
MAX_TAG_LEN = 40
MAX_MOTDS = 500                     # spec section 5; over it the item is refused
MAX_MOTD_DOC_CHARS = 20_000         # the JSON text of one template's document, as written
MOTD_DETAIL_CHARS = 60              # Record.detail: the first line of a template

NEWER_FORMAT = "made with a newer fittings format"
# The importer's library cannot be loaded, merged into or written back by the store.
LIBRARY_BROKEN = "your fittings library could not be read - nothing was changed"
_NOT_A_PAYLOAD = "not a fittings payload"
_NEVER_IN_GAME = "Which of your characters have a fit saved in game"

# The in-memory store is never saved; an empty path makes an accidental save() fail.
_NO_PATH = ""


def _count(n, one, many) -> str:
    return f"{n} {one if n == 1 else many}"


def _key_set(keys) -> set:
    if isinstance(keys, str):
        return {keys}
    try:
        return {key for key in keys if isinstance(key, str)}
    except TypeError:
        return set()


def _dicts(values) -> list:
    """The dict entries of a list; anything else reads as no entries."""
    return [entry for entry in values if isinstance(entry, dict)] \
        if isinstance(values, list) else []


def _name_of(entry) -> str:
    name = entry.get("name")
    return name if isinstance(name, str) else ""


def _named_rows(entries, name_of=_name_of) -> list:
    """``[(record key, entry)]`` for a list of named dicts. The key is the name; a name
    used again gets ``#2``, ``#3``, ... so that every row can be ticked on its own."""
    rows, used, seen = [], set(), {}
    for entry in entries:
        name = name_of(entry)
        number = seen.get(name, 0) + 1
        key = name if number == 1 else f"{name}#{number}"
        while key in used:
            number += 1
            key = f"{name}#{number}"
        seen[name] = number
        used.add(key)
        rows.append((key, entry))
    return rows


def _skipped(result, reason) -> ItemResult:
    result.status = "skipped"
    result.warnings.append(reason)
    return result


# ── The library, in memory ──────────────────────────────────────────────────

def _open_library(state):
    """``state.fittings`` as an in-memory store. -> ``(store, "")`` or ``(None, reason)``.

    A missing library (``None``) is an empty one with the default tags. A library stored
    with a newer ``schema_version`` is refused with ``NEWER_FORMAT``; one the store cannot
    load is refused with ``LIBRARY_BROKEN``. Total. ``state.fittings`` is not modified.

    A library that LOADS can still be one the store cannot merge into or write back (a
    doctrine name that is a list, exemptions that are not objects): every ``apply`` below
    does its store work inside a ``try`` and answers such a library with ``LIBRARY_BROKEN``
    as well, before ``state`` is touched."""
    data = getattr(state, "fittings", None)
    if data is not None and not isinstance(data, dict):
        return None, LIBRARY_BROKEN
    if data is not None:
        version = data.get("schema_version")
        if type(version) is int and version > fittings_store.SCHEMA_VERSION:
            return None, NEWER_FORMAT
    store = FittingsStore(_NO_PATH)
    try:
        store.load_data(data)
    except Exception:
        return None, LIBRARY_BROKEN
    return store, ""


def _write_library(state, store, keys, stamp=False) -> None:
    """Put the store's result back into ``state.fittings``: only ``keys`` of an existing
    library (everything else in the file stays as it is), the whole ``dump()`` when there
    was no library - a created library is the store's whole file, ``schema_version``
    included, whichever item creates it. ``stamp`` also writes the schema version into an
    existing library, as the store's own save would - needed when doctrines are written,
    because loading them applied the store's one-time migrations.

    ``dump()`` runs before anything is assigned: when it raises, ``state`` is as it was."""
    dumped = store.dump()
    if not isinstance(state.fittings, dict):
        state.fittings = dumped
        return
    for key in keys:
        state.fittings[key] = dumped[key]
    if stamp:
        state.fittings["schema_version"] = dumped["schema_version"]


def _file_keys(state, section) -> list:
    """The ids a library section holds, in file order (the store keeps them as its keys)."""
    entries = state.fittings.get(section) if isinstance(state.fittings, dict) else None
    return [key for key in entries if isinstance(key, str)] if isinstance(entries, dict) else []


def _scrub_fit(fit) -> dict:
    return dict(fit, esi_fitting_ids={})


def _scrub_doctrine(doctrine) -> dict:
    """A doctrine as it travels: every slot with refits is on its default."""
    members = []
    for member in _dicts(doctrine.get("members")):
        member = dict(member)
        refits = member.get("refits")
        if isinstance(refits, list) and refits:
            member["fit_id"] = refits[0]
        members.append(member)
    return dict(doctrine, members=members)


def _slot_fit_ids(member) -> list:
    """Every fit id a member dict names: its active fit and its refits, once each (the
    active fit first, then the refits in their order). Linear: a slot can name thousands."""
    ids, seen = [], set()
    refits = member.get("refits")
    for value in (member.get("fit_id"), *(refits if isinstance(refits, list) else ())):
        if isinstance(value, str) and value not in seen:
            seen.add(value)
            ids.append(value)
    return ids


def _share_of(schema_version, doctrines, fits) -> dict:
    """The doctrines payload for ``doctrines``, with only the fits they name."""
    named = {fit_id for doctrine in doctrines
             for member in _dicts(doctrine.get("members"))
             for fit_id in _slot_fit_ids(member)}
    kept = [fit for fit in fits
            if isinstance(fit.get("id"), str) and fit["id"] in named]
    return {"schema_version": schema_version, "fits": kept, "doctrines": list(doctrines)}


def _doctrine_detail(doctrine) -> str:
    members = _dicts(doctrine.get("members"))
    refits = sum(max(0, len(_slot_fit_ids(member)) - 1) for member in members
                 if isinstance(member.get("refits"), list) and member["refits"])
    return f"{_count(len(members), 'fit', 'fits')}, {_count(refits, 'refit', 'refits')}"


def _fits_note(merged) -> list:
    added, reused = len(merged.fits_added), len(merged.fits_reused)
    if not (added or reused):
        return []
    return [f"{_count(added, 'fit', 'fits')} added, {reused} already in your library"]


_STAMP = ("fittings", ("schema_version",))


class _LibraryItem(Item):
    """Shared by the three items that live in ``fittings_library.json``.

    Each of them declares ``_STAMP`` (``schema_version``) in its ``paths``: any of the three
    creates a missing library whole (``_write_library``), version stamp included. In a
    library that is there, only the doctrines item writes the stamp."""

    kind = "collection"
    files = ("fittings",)


# ── fittings.tag_categories ─────────────────────────────────────────────────

def _clean_tags(values, label):
    """A tag list from an untrusted payload. -> ``(tags, problems)``. Names are cleaned,
    legacy names renamed, repeats dropped, the list capped. A name over ``MAX_TAG_LEN`` is
    dropped whole, as the store drops a member tag: cut short it would be a tag nobody
    chose.

    Only the first ``MAX_TAGS`` ENTRIES are looked at (a list FCTool wrote never has
    more): the bound comes before any per-entry work."""
    if not isinstance(values, list):
        return [], [f"{label}: not a list of tags"]
    problems = []
    if len(values) > MAX_TAGS:
        problems.append(f"{label}: only the first {MAX_TAGS} tags are kept "
                        f"({len(values) - MAX_TAGS} left out)")
        values = values[:MAX_TAGS]
    names, junk, long = [], 0, 0
    for value in values:
        name = clean_text(value, len(value)) if isinstance(value, str) else ""
        if len(name) > MAX_TAG_LEN:
            long += 1
        elif name:
            names.append(name)
        else:
            junk += 1
    names = fittings_store._migrate_tag_list(names)
    if long:
        problems.append(f"{label}: {_count(long, 'tag', 'tags')} longer than {MAX_TAG_LEN} "
                        f"characters left out")
    if junk:
        odd = _count(junk, "entry that is not a tag", "entries that are not tags")
        problems.append(f"{label}: {odd} left out")
    return names, problems


class TagCategoriesItem(_LibraryItem):
    """The tag vocabulary. Import = union: missing tags are appended in file order."""

    id = "fittings.tag_categories"
    label = "Tag categories"
    order = 10
    paths = (("fittings", ("tags",)), _STAMP)

    def export(self, state, ctx):
        if getattr(state, "fittings", None) is None:
            return ABSENT
        store, _reason = _open_library(state)
        if store is None:
            return ABSENT
        try:
            return _clean_tags(store.tags, self.label)[0] or ABSENT
        except Exception:
            return ABSENT

    def validate(self, payload, ctx):
        tags, problems = _clean_tags(payload, self.label)
        return (tags or ABSENT), problems

    def _tags(self, payload) -> list:
        return [tag for tag in payload if isinstance(tag, str)] \
            if isinstance(payload, list) else []

    def records(self, payload) -> list:
        return [Record(tag, tag) for tag in self._tags(payload)]

    def summary(self, payload) -> str:
        return _count(len(self._tags(payload)), "tag", "tags")

    def lines(self, payload) -> list:
        tags = self._tags(payload)
        return [("Tags", ", ".join(tags))] if tags else []

    def subset(self, payload, keys):
        wanted = _key_set(keys)
        return [tag for tag in self._tags(payload) if tag in wanted] or ABSENT

    def apply(self, state, payload, mode, conflict, ctx) -> ItemResult:
        result = ItemResult(self.id)
        tags, _problems = _clean_tags(payload, self.label)
        if not tags:
            return result
        store, reason = _open_library(state)
        if store is None:
            return _skipped(result, reason)
        try:
            have = set(store.tags)
            for tag in tags:
                if tag in have:
                    result.unchanged.append(tag)
                else:
                    store.add_tag(tag)
                    have.add(tag)
                    result.added.append(tag)
            if result.added:
                _write_library(state, store, ("tags",))
        except Exception:
            return _skipped(ItemResult(self.id), LIBRARY_BROKEN)
        return result.settle()


# ── fittings.doctrines ──────────────────────────────────────────────────────

class DoctrinesItem(_LibraryItem):
    """Doctrines with their fits, refits, tags, seed targets, ideals and exemptions.

    The payload is ``.fctdoc``-shaped: ``{"schema_version", "fits": [...], "doctrines":
    [...]}``, ``fits`` being every fit a member or a refit names. Import goes through
    ``FittingsStore.merge_share``, which owns every rule: fits are reused by content; a
    doctrine the importer already has BY NAME is replaced in place ("theirs") or left alone
    with its fits ("mine"); one that differs only in the importer's active refit is
    unchanged; a name used twice in one file keeps its first doctrine. A doctrine added or
    updated sets the boot flag ``motd_disarm``, as a new MOTD template does: the tag lines
    of the importer's MOTD are built from the library.
    """

    id = "fittings.doctrines"
    label = "Doctrines"
    order = 20
    paths = (("fittings", ("fits",)), ("fittings", ("doctrines",)), ("fittings", ("tags",)),
             _STAMP)
    conflicts = True
    never_saved = (_NEVER_IN_GAME,)

    def _exported(self, state):
        """-> ``(payload | ABSENT, notes)``: what this setup has to share, and what was
        left out of it. Total."""
        try:
            if getattr(state, "fittings", None) is None:
                return ABSENT, []
            store, _reason = _open_library(state)
            if store is None:
                return ABSENT, []
            # export_doctrines is the frozen .fctdoc exporter: called, never changed. The
            # scrubbing happens on the dict it returns (its `exported_at` stamp is left
            # out too: the same setup must export the same payload twice).
            shared = store.export_doctrines(_file_keys(state, "doctrines"))
            doctrines = [_scrub_doctrine(d) for d in _dicts(shared.get("doctrines"))]
            if not doctrines:
                return ABSENT, []
            # ... and then through the import's own check: what is saved is exactly what
            # an importer will accept, and what is not is said (`export_notes`).
            return self.validate({"schema_version": shared.get("schema_version"),
                                  "fits": [_scrub_fit(f) for f in _dicts(shared.get("fits"))],
                                  "doctrines": doctrines}, None)
        except Exception:
            return ABSENT, []

    def export(self, state, ctx):
        return self._exported(state)[0]

    def export_notes(self, state, ctx=None) -> list:
        """Lines for the builder: the fits and doctrines of this setup that are left out
        of the file (a name with ``<`` or ``>`` in it, ...), as an import would say it."""
        return self._exported(state)[1]

    def validate(self, payload, ctx):
        clean, problems = fittings_store.validate_share(payload)
        lines = [f"{self.label}: {problem}" for problem in problems]
        if clean is None or not clean["doctrines"]:
            return ABSENT, lines
        # Only the fits the doctrines name belong to this item.
        return _share_of(clean["schema_version"], clean["doctrines"], clean["fits"]), lines

    def _rows(self, payload) -> list:
        return _named_rows(_dicts(payload.get("doctrines"))) if isinstance(payload, dict) else []

    def records(self, payload) -> list:
        return [Record(key, _name_of(doctrine), _doctrine_detail(doctrine))
                for key, doctrine in self._rows(payload)]

    def summary(self, payload) -> str:
        fits = _dicts(payload.get("fits")) if isinstance(payload, dict) else []
        return (f"{_count(len(self._rows(payload)), 'doctrine', 'doctrines')}, "
                f"{_count(len(fits), 'fit', 'fits')}")

    def lines(self, payload) -> list:
        return [(_name_of(doctrine), _doctrine_detail(doctrine))
                for _key, doctrine in self._rows(payload)]

    def subset(self, payload, keys):
        wanted = _key_set(keys)
        kept = [doctrine for key, doctrine in self._rows(payload) if key in wanted]
        if not kept:
            return ABSENT
        return _share_of(payload.get("schema_version"), kept, _dicts(payload.get("fits")))

    def apply(self, state, payload, mode, conflict, ctx) -> ItemResult:
        result = ItemResult(self.id)
        doctrines = _dicts(payload.get("doctrines")) if isinstance(payload, dict) else []
        if not doctrines:
            return result
        store, reason = _open_library(state)
        if store is None:
            return _skipped(result, reason)
        # A payload the store refuses whole (a newer format, too many fits) is the file's
        # fault and is said as such; checked here so that it cannot be mistaken for ...
        clean, problems = fittings_store.validate_share(payload)
        if clean is None:
            return _skipped(result, problems[0] if problems else _NOT_A_PAYLOAD)
        try:
            # Every conflict rule is the store's: under "mine" it leaves a doctrine the
            # importer has exactly as it is and declines the fits only such doctrines name.
            # `clean` is what validate_share just returned: not validated a second time.
            merged = store.merge_share(clean, on_conflict=conflict, validated=True)
            changed = bool(merged.doctrines_added or merged.doctrines_updated
                           or merged.fits_added or merged.tags_added)
            if changed:
                _write_library(state, store, ("fits", "doctrines", "tags"), stamp=True)
        except Exception:
            # ... a library the store cannot merge into or write back. Nothing was written.
            return _skipped(result, LIBRARY_BROKEN)
        result.added.extend(merged.doctrines_added)
        result.updated.extend(merged.doctrines_updated)
        result.unchanged.extend(merged.doctrines_unchanged)
        result.skipped.extend(merged.doctrines_skipped)
        result.notes.extend(_fits_note(merged))
        if merged.tags_added:
            result.notes.append("added to your tag list: " + ", ".join(merged.tags_added))
        result.warnings.extend(merged.members_dropped)
        if merged.orphaned_fits:
            result.warnings.append("no longer used, kept in your library: "
                                   + ", ".join(merged.orphaned_fits))
        if merged.doctrines_added or merged.doctrines_updated:
            # A new or changed doctrine changes what the importer's MOTD says (its tag
            # lines are built from the library): as for a template, the next start begins
            # with Auto-update MOTD off for the session.
            result.flags["motd_disarm"] = True
        if changed:
            result.status = "applied"
        return result


# ── fittings.fits ───────────────────────────────────────────────────────────

class FitsItem(_LibraryItem):
    """The library fits no doctrine names, ``{"schema_version", "fits": [...]}``. A fit
    whose content the importer already has is that fit (their name is kept)."""

    id = "fittings.fits"
    label = "Fits outside doctrines"
    order = 30
    paths = (("fittings", ("fits",)), _STAMP)
    never_saved = (_NEVER_IN_GAME,)

    def _exported(self, state):
        """-> ``(payload | ABSENT, notes)``, as ``DoctrinesItem._exported``. Total."""
        try:
            if getattr(state, "fittings", None) is None:
                return ABSENT, []
            store, _reason = _open_library(state)
            if store is None:
                return ABSENT, []
            named = set()
            for doctrine in store.list_doctrines():
                named.update(fittings_store.doctrine_fit_ids(doctrine, True))
            fits = [_scrub_fit(fit_to_dict(store.get_fit(fit_id)))
                    for fit_id in _file_keys(state, "fits")
                    if fit_id not in named and store.get_fit(fit_id) is not None]
            if not fits:
                return ABSENT, []
            return self.validate(
                {"schema_version": fittings_store.SCHEMA_VERSION, "fits": fits}, None)
        except Exception:
            return ABSENT, []

    def export(self, state, ctx):
        return self._exported(state)[0]

    def export_notes(self, state, ctx=None) -> list:
        """Lines for the builder: the fits of this setup that are left out of the file."""
        return self._exported(state)[1]

    def validate(self, payload, ctx):
        if not isinstance(payload, dict):
            return ABSENT, [f"{self.label}: not a fittings payload"]
        clean, problems = fittings_store.validate_share(
            {"schema_version": payload.get("schema_version"), "fits": payload.get("fits")})
        lines = [f"{self.label}: {problem}" for problem in problems]
        if clean is None or not clean["fits"]:
            return ABSENT, lines
        return {"schema_version": clean["schema_version"], "fits": clean["fits"]}, lines

    def _rows(self, payload) -> list:
        return _named_rows(_dicts(payload.get("fits"))) if isinstance(payload, dict) else []

    def records(self, payload) -> list:
        rows = []
        for key, fit in self._rows(payload):
            hull = fit.get("hull_name")
            rows.append(Record(key, _name_of(fit),
                               clean_text(hull) if isinstance(hull, str) else ""))
        return rows

    def summary(self, payload) -> str:
        return _count(len(self._rows(payload)), "fit", "fits")

    def lines(self, payload) -> list:
        return [(record.label, record.detail) for record in self.records(payload)]

    def subset(self, payload, keys):
        wanted = _key_set(keys)
        kept = [fit for key, fit in self._rows(payload) if key in wanted]
        if not kept:
            return ABSENT
        return {"schema_version": payload.get("schema_version"), "fits": kept}

    def apply(self, state, payload, mode, conflict, ctx) -> ItemResult:
        result = ItemResult(self.id)
        fits = _dicts(payload.get("fits")) if isinstance(payload, dict) else []
        if not fits:
            return result
        store, reason = _open_library(state)
        if store is None:
            return _skipped(result, reason)
        clean, problems = fittings_store.validate_share(      # see DoctrinesItem.apply
            {"schema_version": payload.get("schema_version"), "fits": fits})
        if clean is None:
            return _skipped(result, problems[0] if problems else _NOT_A_PAYLOAD)
        try:
            merged = store.merge_share(clean, validated=True)
            if merged.fits_added:
                _write_library(state, store, ("fits",))
        except Exception:
            return _skipped(result, LIBRARY_BROKEN)
        result.added.extend(merged.fits_added)
        result.unchanged.extend(merged.fits_reused)
        if merged.fits_reused:
            result.notes.append(f"{len(merged.fits_reused)} already in your library")
        return result.settle()


# ── MOTD templates: the scrub ───────────────────────────────────────────────

# The params of each pill kind that travel, with their types. Anything else on a pill is
# left behind - which is what unpins an FC: an fc_line keeps nothing and is rebuilt as
# "follow the selected FC".
TOKEN_PARAMS = {
    "char": (("id", int), ("name", str)),
    "fit": (("dna", str), ("name", str)),
    "system": (("name", str),),
    "channel": (("name", str),),
    "fc_line": (),
    "staging_line": (("name", str),),
    "doctrine_line": (),
    "tag_line": (("tag", str),),
    "channel_line": (("label", str), ("name", str)),
    "doctrine_block": (),
}
_INLINE_KINDS = ("char", "fit", "system", "channel")      # pills that sit inside a text line
_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}")
_STYLE_FLAGS = ("bold", "italic", "underline")
_MAX_FONT_SIZE = 100

# What a pill param may not hold. The app's resolver escapes free text, but writes a pill's
# params as they are - ``<url=fitting:{dna}>{name}</url>``, ``<url=showinfo:1377//{id}>
# {name}</url>``, ``{label}: <url=joinChannel:...>{name}</url>`` - into the MOTD the
# importer pushes to their fleet. ``<``, ``>`` and every control character (category Cc:
# U+0000-001F, U+007F-009F) are refused; a DNA is digits and its three separators.
_MARKUP = re.compile(r"[<>\x00-\x1f\x7f-\x9f]")
_DNA = re.compile(r"[0-9:;_]+")
# Free text is escaped, so it carries no markup - but a control character (other than the
# newline and the tab a text is made of) has no place in a MOTD either: removed, and said.
_TEXT_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

# No number in a DNA - a type id or a quantity - may have more than this many digits: a
# longer one is not a number a fit holds. (The quantities themselves are NOT bounded: the
# app's parser decides what it builds for them.)
_MAX_DNA_DIGITS = 9
_DNA_NUMBER = re.compile(r"[0-9_]+")
# A character id the game could have issued (and a number every JSON reader keeps exact).
_MAX_CHAR_ID = 2 ** 53


def _dna_numbers_ok(dna) -> bool:
    """Whether every number of a DNA string (already known to be digits and separators)
    has at most ``_MAX_DNA_DIGITS`` digits. The parser reads ``1_000`` as 1000, so a ``_``
    inside a number is skipped, not a separator."""
    return all(len(number.replace("_", "")) <= _MAX_DNA_DIGITS
               for number in _DNA_NUMBER.findall(dna))


def _has_markup(value) -> bool:
    """Whether any string in ``value`` - a pill's params: every value at any depth, and
    every key - holds a character of ``_MARKUP``. Not recursive: any nesting is walked."""
    pending = [value]
    while pending:
        value = pending.pop()
        if isinstance(value, str):
            if _MARKUP.search(value):
                return True
        elif isinstance(value, dict):
            pending.extend(value)
            pending.extend(value.values())
        elif isinstance(value, (list, tuple)):
            pending.extend(value)
    return False


def _pill_may_travel(kind, params) -> bool:
    """False for a pill that would put markup into the MOTD: any param string with a
    ``_MARKUP`` character (also in a param this build would leave behind - the pill is not
    what its author's build made), or a fit pill carrying a ``dna`` that is not one, or
    one with a number of more than nine digits in it (``_dna_numbers_ok``)."""
    if _has_markup(params):
        return False
    if kind == "fit" and isinstance(params, dict) and "dna" in params:
        dna = params["dna"]
        return (isinstance(dna, str) and _DNA.fullmatch(dna) is not None
                and _dna_numbers_ok(dna))
    return True


def _clean_params(kind, params) -> dict:
    if kind == "fc_line":
        return {"source": "selected"}
    if not isinstance(params, dict):
        return {}
    clean = {}
    for key, wanted in TOKEN_PARAMS.get(kind, ()):
        value = params.get(key)
        if type(value) is not wanted:           # a bool is not an id
            continue
        if kind == "char" and key == "id" and not 1 <= value <= _MAX_CHAR_ID:
            continue                            # nor is 0, a negative or a 400-digit number
        clean[key] = value
    return clean


def _clean_style(style) -> dict:
    if not isinstance(style, dict):
        return {}
    clean = {}
    color = style.get("color")
    if isinstance(color, str) and _HEX_COLOR.fullmatch(color):
        clean["color"] = color
    for flag in _STYLE_FLAGS:
        if style.get(flag) is True:
            clean[flag] = True
    size = style.get("size")
    if type(size) is int and 0 < size <= _MAX_FONT_SIZE:
        clean["size"] = size
    return clean


def _clean_doc(raw):
    """A document's JSON runs, rebuilt from scratch. -> ``(runs, marked, controls)``: text
    runs with their known style keys, pills of a kind this build knows with their known
    params. Everything else is left out; ``marked`` counts the known pills left out because
    they carried markup (``_pill_may_travel``), ``controls`` says whether a control
    character was removed from a text run (``_TEXT_CONTROL``). The runs have been through
    ``doc_from_json`` -> ``doc_to_json``."""
    runs, marked, controls = [], 0, False
    for run in raw:
        if not isinstance(run, dict):
            continue
        if run.get("t") == "token":
            kind = run.get("kind")
            if isinstance(kind, str) and kind in motd_doc.TOKEN_KINDS:
                params = run.get("params")
                if not _pill_may_travel(kind, params):
                    marked += 1
                    continue
                runs.append({"t": "token", "kind": kind,
                             "params": _clean_params(kind, params)})
        elif run.get("t") == "text":
            text = run.get("text")
            if isinstance(text, str) and text:
                shown = _TEXT_CONTROL.sub("", text)
                controls = controls or shown != text
                if shown:
                    runs.append({"t": "text", "text": shown,
                                 "style": _clean_style(run.get("style"))})
    return motd_doc.doc_to_json(motd_doc.doc_from_json(runs)), marked, controls


def _doctrine_name(value) -> str:
    """A doctrine name exactly as the store keeps one that arrives in a share
    (``fittings_store._share_doctrine``): stripped and nothing else - not clipped, no
    character removed. A template cleaned any other way would stop matching the doctrine
    it travels with."""
    return value.strip()


_OLD_FORMAT = "old-format template left out"
_NOTHING_LEFT = "nothing left after cleaning"


def _scrub_motd(entry, legacy):
    """-> ``(template | None, [what to tell the importer about it])``; see ``scrub_motd``.
    The lines start with the template's ``"<doctrine> / <name>"``. An entry refused without
    a line of its own is simply "not a usable template"."""
    if not isinstance(entry, dict):
        return None, []
    name, doctrine = entry.get("name"), entry.get("doctrine")
    if not isinstance(name, str) or not isinstance(doctrine, str):
        return None, []
    name, doctrine = clean_text(name, MAX_NAME), _doctrine_name(doctrine)
    # A doctrine name the store would not keep (`validate_share` drops the doctrine) names
    # nothing - and is never cut short, or it would name some other doctrine.
    if not name or not doctrine or len(doctrine) > fittings_store.MAX_SHARE_NAME:
        return None, []
    label = clean_text(f"{doctrine} / {name}", fittings_store.MAX_SHARE_NAME + MAX_NAME + 3)
    version = entry.get("version")
    if (type(version) is int and version == 2) or "doc" in entry:
        # The composer's own rule: an entry that carries a `doc` is a v2 entry.
        raw = entry.get("doc")
        if not isinstance(raw, list):
            return None, []
    elif not legacy:
        # Never migrated out of a file: FCTool does not write one (the export migrates),
        # and the app's markup parser is quadratic on "<" without ">" - a hand-made header
        # of 80,000 of them held the import preview, and the next start, for 37 seconds.
        return None, [f"{label}: {_OLD_FORMAT}"]
    else:
        # The exporter's own template, saved before the pill composer: migrated exactly as
        # the app does when it loads one, legacy tag names renamed with the store's table.
        raw = motd_doc.doc_to_json(motd_doc.from_legacy_fields(
            entry, tag_renames=fittings_store._TAG_RENAMES))
    doc, marked, controls = _clean_doc(raw)
    if not doc:
        # Under "theirs" an empty document would blank the importer's same-named template.
        return None, [f"{label}: {_NOTHING_LEFT}"]
    if len(json.dumps(doc, ensure_ascii=False)) > MAX_MOTD_DOC_CHARS:
        return None, []
    said = []
    if marked == 1:
        said.append(f"{label}: 1 pill with markup in it left out")
    elif marked:
        said.append(f"{label}: {marked} pills with markup in them left out")
    if controls:
        said.append(f"{label}: control characters removed")
    return {"name": name, "doctrine": doctrine, "version": 2, "doc": doc}, said


def _scrub(entry, legacy):
    try:
        return _scrub_motd(entry, legacy)
    except Exception:
        return None, []


def scrub_motd(entry, legacy=False):
    """A saved MOTD template as it may travel, or ``None`` when it cannot.

    -> ``{"name", "doctrine", "version": 2, "doc"}``: no ``fc``, no ``legacy_fits``; a
    pinned-FC pill follows the selected FC; pills of an unknown kind are gone, and so are
    pills that carry markup (``_pill_may_travel``). ``name`` is cleaned and clipped to
    ``MAX_NAME``; ``doctrine`` is only stripped, as the store strips a doctrine's name, so
    the two always match.

    ``legacy`` is for the EXPORT only: a template of the exporter's saved before the pill
    composer is migrated to a document. Without it (reading a file) such an entry is
    refused - a file written by FCTool never carries one.

    ``None`` for anything that is not a template; a blank name or doctrine; a doctrine name
    over ``fittings_store.MAX_SHARE_NAME``; an old-format entry (``legacy`` off, or one that
    cannot be migrated); a document with nothing left after cleaning; a document over
    ``MAX_MOTD_DOC_CHARS`` of JSON text as it is written (not ASCII-escaped).

    Total: any input, never raises, never edits ``entry``. Idempotent."""
    return _scrub(entry, legacy)[0]


def _motd_key(entry) -> str:
    return f"{entry.get('doctrine')} / {entry.get('name')}"


def _motd_first_line(doc) -> str:
    """The first line of the document that says something: free text, with the names of
    the pills that sit inside it. Lines made of line pills alone say nothing here."""
    lines, current = [], []
    for run in doc if isinstance(doc, list) else ():
        if not isinstance(run, dict):
            continue
        if run.get("t") == "text" and isinstance(run.get("text"), str):
            parts = run["text"].replace("\t", " ").split("\n")
            current.append(parts[0])
            for part in parts[1:]:
                lines.append("".join(current))
                current = [part]
        elif run.get("t") == "token" and run.get("kind") in _INLINE_KINDS:
            params = run.get("params")
            name = params.get("name") if isinstance(params, dict) else None
            if isinstance(name, str):
                current.append(name)
    lines.append("".join(current))
    for line in lines:
        shown = clean_text(line, MOTD_DETAIL_CHARS)
        if shown:
            return shown
    return ""


def _named_characters(doc) -> int:
    """How many different characters the document links by hand (``char`` pills)."""
    named = set()
    for run in doc if isinstance(doc, list) else ():
        if isinstance(run, dict) and run.get("t") == "token" and run.get("kind") == "char":
            params = run.get("params")
            params = params if isinstance(params, dict) else {}
            char_id, name = params.get("id"), params.get("name")
            named.add(("id", char_id) if type(char_id) is int
                      else ("name", name if isinstance(name, str) else ""))
    return len(named)


def _motd_detail(doc) -> str:
    """``Record.detail`` of a template: its first line of text, and - so that the export
    list shows it before the file is written - whether it names characters."""
    line = _motd_first_line(doc)
    named = _named_characters(doc)
    if not named:
        return line
    names = "names " + _count(named, "character", "characters")
    return f"{line} - {names}" if line else names


def _is_template(entry) -> bool:
    return (isinstance(entry, dict) and isinstance(entry.get("name"), str)
            and isinstance(entry.get("doctrine"), str) and isinstance(entry.get("doc"), list))


class _ChosenTemplates(list):
    """What ``MotdsItem.subset`` returns: the ticked templates - a plain list to everyone
    (a file gets just the templates) - that also remembers, in ``row_keys``, the record key
    each one had in the whole payload. ``apply`` reports under those keys."""

    def __init__(self, *args):
        super().__init__(*args)
        self.row_keys = []


def _library_doctrine_names(state):
    """The doctrine names in ``state.fittings``, or ``None`` when that cannot be told
    (an unreadable library). A missing library has none."""
    library = getattr(state, "fittings", None)
    if library is None:
        return set()
    if not isinstance(library, dict):
        return None
    doctrines = library.get("doctrines")
    if not isinstance(doctrines, dict):
        return set()
    return {entry["name"] for entry in doctrines.values()
            if isinstance(entry, dict) and isinstance(entry.get("name"), str)}


class MotdsItem(Item):
    """MOTD templates, ``[{"name", "doctrine", "version": 2, "doc"}]``.

    A template is known by ``(doctrine, name)``. Import: a new one is appended after the
    importer's own with a blank ``fc``; one they already have gets the group's document
    ("theirs": its own ``fc`` stays, its ``legacy_fits`` - the fallback fit links of the
    document that is replaced - go), or is left alone ("mine", read as the store reads it:
    blanks and case aside). Only ``fittings.saved_motds`` is written. Any template added or
    updated sets the boot flag ``motd_disarm``: the next start begins with Auto-update MOTD
    off for the session, so a template nobody has read yet is not pushed on a cold launch.

    The record key is ``"<doctrine> / <name>"``; two templates that spell the same key
    (``"A / B"`` + ``"C"`` and ``"A"`` + ``"B / C"``) get ``#2``, ``#3`` as doctrines do.
    Templates in the old format are migrated by ``export`` only (``scrub_motd``).
    """

    id = "fittings.motds"
    label = "MOTD templates"
    kind = "collection"
    files = ("config",)
    paths = (("config", ("fittings", "saved_motds")),)
    order = 50
    conflicts = True
    never_saved = ("The FC name", "A pinned FC", "Your recent pills",
                   "The Auto-update MOTD switch")

    _PATH = ("fittings", "saved_motds")

    def _clean(self, values, legacy=False):
        """-> ``(templates, problems)``: scrubbed, one per ``(doctrine, name)``. A template
        that has something of its own to say gets a line; the rest are counted."""
        templates, seen, unusable, twins, said = [], set(), 0, 0, []
        for value in values:
            entry, lines = _scrub(value, legacy)
            said.extend(lines)
            if entry is None:
                unusable += 0 if lines else 1
                continue
            key = (entry["doctrine"], entry["name"])
            if key in seen:
                twins += 1
                continue
            seen.add(key)
            templates.append(entry)
        problems = [f"{self.label}: {line}" for line in said]
        if unusable:
            odd = _count(unusable, "entry that is not a usable template",
                         "entries that are not usable templates")
            problems.append(f"{self.label}: {odd} left out")
        if twins:
            odd = _count(twins, "template with a name used twice",
                         "templates with a name used twice")
            problems.append(f"{self.label}: {odd} left out")
        return templates, problems

    def _exported(self, state):
        """-> ``(templates, notes)``: this setup's templates as they travel, and what the
        scrub left out of them."""
        saved = get_path(getattr(state, "config", None), self._PATH)
        if not isinstance(saved, list):
            return [], []
        # The one place an old-format template is migrated: the exporter's own.
        templates, notes = self._clean(saved, legacy=True)
        if len(templates) > MAX_MOTDS:
            notes.append(f"{self.label}: only the first {MAX_MOTDS} templates are saved "
                         f"({len(templates) - MAX_MOTDS} left out)")
        return templates[:MAX_MOTDS], notes

    def export(self, state, ctx):
        return self._exported(state)[0] or ABSENT

    def export_notes(self, state, ctx=None) -> list:
        """Lines for the builder: the templates and pills of this setup that are left out
        of the file (a pill with markup in it, a template with nothing left, ...)."""
        try:
            return self._exported(state)[1]
        except Exception:
            return []

    def validate(self, payload, ctx):
        if not isinstance(payload, list):
            return ABSENT, [f"{self.label}: not a list of MOTD templates"]
        if len(payload) > MAX_MOTDS:
            return ABSENT, [f"{self.label}: too many MOTD templates"]
        templates, problems = self._clean(payload)
        return (templates or ABSENT), problems

    def _templates(self, payload) -> list:
        return [entry for entry in payload if _is_template(entry)] \
            if isinstance(payload, list) else []

    def _rows(self, payload) -> list:
        """``[(record key, template)]``, every key its own."""
        return _named_rows(self._templates(payload), _motd_key)

    def records(self, payload) -> list:
        return [Record(key, _motd_key(entry), _motd_detail(entry["doc"]))
                for key, entry in self._rows(payload)]

    def summary(self, payload) -> str:
        return _count(len(self._templates(payload)), "MOTD template", "MOTD templates")

    def lines(self, payload) -> list:
        return [(record.label, record.detail) for record in self.records(payload)]

    def subset(self, payload, keys):
        wanted = _key_set(keys)
        chosen = _ChosenTemplates()
        for key, entry in self._rows(payload):
            if key in wanted:
                chosen.append(entry)
                chosen.row_keys.append(key)
        return chosen or ABSENT

    def _result_rows(self, payload, incoming) -> list:
        """``[(key, template)]`` for ``apply``: the keys ``records`` gave the templates in
        the WHOLE payload when ``subset`` narrowed it (a ``#2`` twin stays ``#2`` when its
        first is not ticked), else the keys of ``incoming`` as it stands. A remembered key
        is used only when every template has one that is its own key, with or without a
        ``#<n>``, and no two are the same."""
        rows = self._rows(incoming)
        hints = getattr(payload, "row_keys", None)
        if not isinstance(hints, list) or len(hints) != len(payload):
            return rows
        by_identity = {}
        for raw, hint in zip(payload, hints):
            if _is_template(raw) and isinstance(hint, str):
                by_identity.setdefault((raw["doctrine"], raw["name"]), hint)
        kept, used = [], set()
        for _key, entry in rows:
            hint = by_identity.get((entry["doctrine"], entry["name"]))
            base = _motd_key(entry)
            if hint is None or hint in used or not (
                    hint == base or (hint.startswith(base + "#")
                                     and hint[len(base) + 1:].isdigit())):
                return rows
            used.add(hint)
            kept.append((hint, entry))
        return kept

    def apply(self, state, payload, mode, conflict, ctx) -> ItemResult:
        result = ItemResult(self.id)
        # Scrubbed again (a caller may not have validated); never migrated from a file.
        incoming, _problems = self._clean(payload if isinstance(payload, list) else [])
        if not incoming:
            return result
        keep_mine = isinstance(conflict, str) and conflict.strip().lower() == "mine"
        config = state.config if isinstance(state.config, dict) else {}
        stored = get_path(config, self._PATH)
        saved = list(stored) if isinstance(stored, list) else []
        if stored is not None and not isinstance(stored, list):
            result.warnings.append(f"{self.label}: your saved templates could not be read "
                                   f"and count as none")
        # Entries that are not templates are never read: they stay where they are.
        mine = {}
        for index, entry in enumerate(saved):
            if isinstance(entry, dict):
                doctrine, name = entry.get("doctrine") or "", entry.get("name")
                if isinstance(doctrine, str) and isinstance(name, str):
                    mine.setdefault((doctrine, name), index)

        landed = []
        for key, entry in self._result_rows(payload, incoming):    # the keys `records` shows
            index = mine.get((entry["doctrine"], entry["name"]))
            if index is None:
                # Key order as the composer writes a template.
                saved.append({"fc": "", "doc": entry["doc"], "version": 2,
                              "name": entry["name"], "doctrine": entry["doctrine"]})
                mine[(entry["doctrine"], entry["name"])] = len(saved) - 1
                result.added.append(key)
            elif keep_mine:
                result.skipped.append(key)
                continue
            else:
                local = saved[index]
                version = local.get("version")
                if (type(version) is int and version == 2 and local.get("doc") == entry["doc"]
                        and not local.get("legacy_fits")):
                    result.unchanged.append(key)
                    continue
                local = saved[index] = dict(local)      # its own `fc` and the rest stay
                local["doc"] = entry["doc"]
                local["version"] = 2
                # The old document's fallback fit links would render under the new one.
                local.pop("legacy_fits", None)
                result.updated.append(key)
            landed.append(entry)

        if not landed:
            return result
        if state.config is None:
            state.config = {}
        set_path(state.config, self._PATH, saved)
        result.flags["motd_disarm"] = True
        known = _library_doctrine_names(state)
        for entry in landed if known is not None else ():
            if entry["doctrine"] not in known:
                result.warnings.append(f"'{entry['name']}' is for the doctrine "
                                       f"'{entry['doctrine']}', which is not in your library")
        return result.settle()


# ── fittings.motd_settings ──────────────────────────────────────────────────

class MotdSettingsItem(ConfigFieldsItem):
    """The MOTD length limit and the remembered logi channel.

    The channel name ends up in a channel pill, and a pill's params are written into the
    MOTD markup as they are: a name with ``<``, ``>`` or a control character is refused
    whole (never cleaned into something else) - by ``validate``, by ``apply`` and by
    ``export``, which all clean a value through this one method."""

    def _clean_field(self, fld, value, ctx, keep_unknown=False):
        if fld.key == "logi_channel" and isinstance(value, str) and _MARKUP.search(value):
            return ABSENT, ["has characters a channel name cannot have"]    # never quoted
        return super()._clean_field(fld, value, ctx, keep_unknown=keep_unknown)


# ── The items ───────────────────────────────────────────────────────────────

ITEMS = (
    TagCategoriesItem(),
    DoctrinesItem(),
    FitsItem(),
    ConfigFieldsItem(
        id="fittings.seed", label="Default seed target", order=40,
        # From 1: the app reads a global target of 0 or less as 20 and rewrites it.
        fields=(Field("seed_target", "Seed target", "int", ("market", "seed_target"),
                      lo=1, hi=9999),)),
    MotdsItem(),
    MotdSettingsItem(
        id="fittings.motd_settings", label="MOTD settings", order=60,
        fields=(Field("motd_budget", "MOTD length limit", "int", ("fittings", "motd_budget"),
                      lo=200, hi=10000),
                Field("logi_channel", "Logi channel", "str", ("fittings", "logi_channel"),
                      max_len=100))),
)
