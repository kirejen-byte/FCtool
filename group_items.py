"""Group configuration: the item engine and the generic item classes.

A group file (``group_bundle``) carries ``categories[category][item_key] = payload``. An
**item** is the unit that knows one such payload: how to read it out of a setup (``export``),
how to rebuild a clean one from an untrusted file (``validate``), how to describe it
(``records`` / ``summary`` / ``lines``), how to keep a part of it (``subset``) and how to land
it (``apply``). This module holds

* the data the items work on - :class:`State` (the five JSON files, parsed), :class:`Ctx`
  (system table, id and clock - injected, so tests never touch the real ones),
  :class:`Options` (what is ticked, plus mode and conflict choices), :class:`ItemResult`;
* the :class:`Item` base class and two generic items, :class:`ConfigFieldsItem` (a few typed
  ``config.json`` fields) and :class:`NameListItem` (a list of names at one config path);
* the engine: ``register`` / ``registry`` / ``get_item``, ``export_all``,
  ``collect_export_notes``, ``build_bundle``, ``validate_bundle``, ``apply_bundle``,
  ``preview``. ``registry()`` loads the item modules (``group_items_*``) on its first call.

Two rules hold everywhere (spec section 4): **absent means untouched** - a field, record or
item that is not in the payload is not read, compared or written - and **an import never
deletes**, except for the ``replace`` mode of a list, which the importer chooses.

Contracts the item modules rely on
----------------------------------
* ``validate`` is TOTAL: the payload comes from a file anyone can edit. It never raises and
  returns ``(clean payload | ABSENT, problems)``. The clean payload is built from scratch:
  known keys only, types checked (a ``bool`` or a ``float`` is never an ``int``), strings
  cleaned and clipped. ``export``, ``records``, ``summary``, ``lines`` and ``subset`` are
  total too.
* ``apply`` may raise, or return a ``failed`` result: the engine restores the files the item
  names in ``files`` and reports the item as ``failed``. It must still never write a
  wrong-typed value, and it must treat a missing file (``None`` in the state) as "create it
  from the app's defaults". The engine does not take its word for it: a result with an
  unknown status, a declared file that is no longer a ``dict`` and an ``UNREADABLE`` file
  that was written are rolled back the same way.
* ``apply`` gets its OWN copy of the payload: whatever it does to it stays with it.
* ``paths`` lists EVERYTHING an item may write, as ``(file_key, path_tuple)`` pairs; an empty
  path tuple means the whole file - never allowed for ``config.json``. The write-allowlist
  test (``tests/test_group_registry.py``) holds the union of all of them against the list
  in spec section 6.7.
* ``register`` refuses a malformed item (no files, an unknown category, a ``paths`` entry of
  the wrong shape or for a file the item does not declare, ``files`` / ``paths`` / ``modes``
  / ``sections`` / ``never_saved`` that is not a tuple, an empty field or list path) and an
  id that another item holds - at import of its module.
* ``apply_bundle`` MUTATES the state it is given and returns that same object. Hand it
  ``state.copy()`` to keep the original - ``preview`` does exactly that.
* The payloads handed to ``apply_bundle`` are the output of ``validate_bundle``.
* A rule that spans two items of one module lives in that module's ``reconcile(payloads)``
  (see ``validate_bundle``); what an export leaves out is said by ``export_notes``.
* The lines an item says are bounded by the engine (``MAX_PROBLEMS``, ``MAX_RESULT_LINES``):
  an item need not count them.

Pure: stdlib, ``group_bundle`` and ``system_coords`` only. No Tk, no ``ui_theme``, no
``fc_gui`` - this module runs in the boot hook, before the palette is installed. The one
exception is ``_load_item_modules``, which imports the item modules (``group_items_*``).
"""

import contextlib
import math
import re
import sys
import threading
import uuid
from copy import deepcopy
from dataclasses import dataclass, field as _dc_field
from datetime import datetime, timezone

import system_coords
from group_bundle import CATEGORIES, CATEGORY_LABELS, MAX_NAME, Bundle, clean_text, has_visible


class _Sentinel:
    """A unique marker with a readable repr. Copying it gives the same object back."""

    __slots__ = ("_name",)

    def __init__(self, name):
        self._name = name

    def __repr__(self):
        return self._name

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def __reduce__(self):
        return self._name           # pickles as this module's global of that name


ABSENT = _Sentinel("ABSENT")            # no payload / nothing to share / field left out
UNREADABLE = _Sentinel("UNREADABLE")    # a file that exists and could not be parsed

FILE_NAMES = {
    "config": "config.json",
    "fittings": "fittings_library.json",
    "templates": "fleet_templates.json",
    "infra": "infrastructure.json",
    "palette": "ui_palette.json",
}

KINDS = ("value", "collection")
FIELD_KINDS = ("bool", "int", "str", "enum", "system", "names")
STATUSES = ("applied", "unchanged", "skipped", "failed")
CONFLICTS = ("theirs", "mine")
MAX_LIST = 500                          # spec section 5: 500 systems / channels per list
MAX_PROBLEMS = 50                       # problem lines of one validate_bundle, then "and <n> more"
MAX_RESULT_LINES = 50                   # warnings / notes of one ItemResult, and the export
#                                         notes of one item; then "and <n> more"
NOT_SUPPORTED = "not supported by this version: "

_NOTE_CLIP = 60                         # one side of an "<old> -> <new>" note
_SHOWN_NAMES = 5                        # unknown names quoted in one problem line
_MAX_JSON_DEPTH = 24
_MORE_LINE = re.compile(r"and ([0-9]{1,12}) more")     # the line that ends a bounded list
_ITEM_MODULE_PREFIX = "group_items_"    # the name every item module starts with


# ── State and context ───────────────────────────────────────────────────────

@dataclass
class State:
    """The five files, parsed. Each value is a ``dict``, ``None`` (the file does not exist)
    or ``UNREADABLE`` (it exists and must never be written)."""

    config: object
    fittings: object = None
    templates: object = None
    infra: object = None
    palette: object = None

    def copy(self):
        return State(**{key: deepcopy(getattr(self, key)) for key in FILE_NAMES})

    def readable(self, key) -> bool:
        """True when an item may work on this file: it parsed, or it is missing and can be
        created. False for ``UNREADABLE``, for any other non-dict value and for an unknown key."""
        if not isinstance(key, str) or key not in FILE_NAMES:
            return False
        value = getattr(self, key)
        return value is None or isinstance(value, dict)


@dataclass(frozen=True)
class Ctx:
    """What an item may ask the outside world. Every member is a callable.

    ``system_ids(name) -> (canonical_name, system_id, region_id) | None``;
    ``region_known(region_id) -> bool``; ``new_id() -> str``; ``now_iso() -> str``.
    """

    system_ids: object
    region_known: object
    new_id: object
    now_iso: object


def _system_ids(name):
    if not isinstance(name, str):
        return None
    text = name.strip()
    if not text:
        return None
    try:
        system_id = system_coords.resolve_name(text)
        if type(system_id) is not int:
            return None
        canonical = system_coords.get_name(system_id)
        region_id = system_coords.get_region_id(system_id)
    except Exception:
        return None
    if not isinstance(canonical, str) or not canonical:
        return None
    return canonical, system_id, region_id if type(region_id) is int else 0


_known_regions = None                   # the region ids of the system table, once it answered


def _region_ids() -> frozenset:
    """Every region id the bundled system table assigns to at least one system. Read once:
    a table that gave nothing (not shipped, not loadable yet) is asked again next time."""
    global _known_regions
    if _known_regions is None:
        regions = set()
        for system_id in system_coords.get_kspace_name_to_id().values():
            region_id = system_coords.get_region_id(system_id)
            if type(region_id) is int and region_id > 0:
                regions.add(region_id)
        if not regions:
            return frozenset()
        _known_regions = frozenset(regions)
    return _known_regions


def _region_known(region_id) -> bool:
    # A region is known when a SYSTEM of the bundled table is in it. (Not "when the gate
    # table gives it a neighbour": five regions have no gate to another region, and a
    # scan region is none the worse for that.) With no system table shipped every region
    # reads as unknown: the item drops it, which is the safe side.
    if type(region_id) is not int:
        return False
    try:
        return region_id in _region_ids()
    except Exception:
        return False


def _new_id() -> str:
    return uuid.uuid4().hex             # the id shape both stores use


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_ctx() -> Ctx:
    """The real context: the bundled system table (offline, no ESI), ``uuid4`` and UTC now."""
    return Ctx(system_ids=_system_ids, region_known=_region_known,
               new_id=_new_id, now_iso=_now_iso)


# ── Small records ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Field:
    """One typed ``config.json`` value of a :class:`ConfigFieldsItem`.

    ``kind``: ``bool`` | ``int`` (``lo``..``hi`` inclusive) | ``str`` (``max_len``,
    ``allow_blank``) | ``enum`` (``choices``) | ``system`` (a system name, stored canonical) |
    ``names`` (a list of system names, at most ``hi`` or ``MAX_LIST``; an EMPTY list is
    "not present" - never exported, validated away, never applied). ``path`` is the tuple
    of config keys the value lives at.
    """

    key: str
    label: str
    kind: str
    path: tuple
    choices: tuple = ()
    lo: object = None
    hi: object = None
    max_len: int = 120
    allow_blank: bool = False


@dataclass(frozen=True)
class Record:
    """One tickable row of an item: a field of a value item, a record of a collection."""

    key: str
    label: str
    detail: str = ""


@dataclass(frozen=True)
class Attention:
    """A Settings section the importer must look at (``section`` = its TOC title)."""

    section: str
    item_id: str
    reason: str


def _as_str(value) -> str:
    return value if isinstance(value, str) else ""


def _text_list(values) -> list:
    """Strings as they are, ``None`` left out, anything else as its text."""
    if not isinstance(values, (list, tuple)):
        return []
    out = []
    for value in values:
        if isinstance(value, str):
            out.append(value)
        elif value is not None:
            out.append(clean_text(value, 200))
    return out


def _json_safe(value, _depth=0):
    """``value`` as plain JSON data: string keys, lists, finite numbers. Total."""
    if value is None or type(value) in (bool, int, str):
        return value
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        return str(value)
    if _depth >= _MAX_JSON_DEPTH:
        return None
    if isinstance(value, dict):
        return {key if type(key) is str else clean_text(key, 200): _json_safe(item, _depth + 1)
                for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item, _depth + 1) for item in value]
    return clean_text(value, 200)


_RESULT_LISTS = ("added", "updated", "unchanged", "removed", "skipped", "notes", "warnings")
_LINE_LISTS = ("notes", "warnings")     # the two lists of LINES; the others hold record keys


def _bounded(lines, limit=MAX_RESULT_LINES):
    """``lines`` cut to ``limit`` lines plus one last line ``"and <n> more"``.

    Returns the SAME object when there is nothing to cut - also for a list that was bounded
    before, whose last line is that count. Bounding a bounded list to which lines were added
    since keeps the count right (the old count line is added up, not counted as one line).
    Anything that is not a list or tuple is returned as it is. Total.
    """
    if not isinstance(lines, (list, tuple)) or len(lines) <= limit:
        return lines
    tail = lines[limit:]
    if len(tail) == 1 and isinstance(tail[0], str) and _MORE_LINE.fullmatch(tail[0]):
        return lines
    more = 0
    for line in tail:
        counted = _MORE_LINE.fullmatch(line) if isinstance(line, str) else None
        more += int(counted.group(1)) if counted else 1
    return list(lines[:limit]) + [f"and {more} more"]


@dataclass
class ItemResult:
    """What one item did (or, in a preview, would do).

    ``added`` / ``updated`` / ``unchanged`` / ``removed`` / ``skipped`` hold field or record
    keys; ``notes`` and ``warnings`` are lines for the importer; ``attention`` holds
    :class:`Attention`; ``tasks`` holds JSON dicts for work after the restart; ``flags`` are
    boot flags (``coalitions_seeded``, ``motd_disarm``).

    ``notes`` and ``warnings`` are BOUNDED - ``MAX_RESULT_LINES`` lines, then one line
    ``"and <n> more"`` - by ``settle``, by the engine for every result an item returns, and
    by ``to_dict`` / ``from_dict``: results are stored in the import report and in the
    history, and one hand-made file can make an item say twenty thousand lines.
    """

    item_id: str
    status: str = "unchanged"
    added: list = _dc_field(default_factory=list)
    updated: list = _dc_field(default_factory=list)
    unchanged: list = _dc_field(default_factory=list)
    removed: list = _dc_field(default_factory=list)
    skipped: list = _dc_field(default_factory=list)
    notes: list = _dc_field(default_factory=list)
    warnings: list = _dc_field(default_factory=list)
    attention: list = _dc_field(default_factory=list)
    tasks: list = _dc_field(default_factory=list)
    flags: dict = _dc_field(default_factory=dict)

    def bound(self):
        """Cut ``notes`` and ``warnings`` to ``MAX_RESULT_LINES`` lines each, the line after
        them being ``"and <n> more"``. Bounding twice changes nothing. Returns ``self``."""
        for name in _LINE_LISTS:
            lines = getattr(self, name)
            cut = _bounded(lines)
            if cut is not lines:
                setattr(self, name, cut)
        return self

    def settle(self):
        """Set ``status`` to ``applied`` when anything was added, updated or removed (a
        ``failed`` result stays failed); otherwise leave it. Also bounds ``notes`` and
        ``warnings`` (``bound``). Returns ``self``."""
        if self.status != "failed" and (self.added or self.updated or self.removed):
            self.status = "applied"
        return self.bound()

    def to_dict(self) -> dict:
        """JSON-safe, whatever an item put in the lists; ``notes`` and ``warnings`` bounded
        (the result itself is not changed)."""
        data = {"item_id": _as_str(self.item_id),
                "status": self.status if self.status in STATUSES else "unchanged"}
        for name in _RESULT_LISTS:
            lines = getattr(self, name)
            data[name] = _text_list(_bounded(lines) if name in _LINE_LISTS else lines)
        data["attention"] = [
            {"section": _as_str(entry.section), "item_id": _as_str(entry.item_id),
             "reason": _as_str(entry.reason)}
            for entry in (self.attention if isinstance(self.attention, (list, tuple)) else ())
            if isinstance(entry, Attention)]
        data["tasks"] = [_json_safe(task) for task in
                         (self.tasks if isinstance(self.tasks, (list, tuple)) else ())
                         if isinstance(task, dict)]
        data["flags"] = _json_safe(self.flags) if isinstance(self.flags, dict) else {}
        return data

    @classmethod
    def from_dict(cls, data):
        """Tolerant: anything that is not a result dict reads as an empty ``unchanged``."""
        if not isinstance(data, dict):
            return cls(item_id="")
        status = data.get("status")
        result = cls(item_id=_as_str(data.get("item_id")),
                     status=status if isinstance(status, str) and status in STATUSES
                     else "unchanged")
        for name in _RESULT_LISTS:
            lines = data.get(name)
            setattr(result, name, _text_list(_bounded(lines) if name in _LINE_LISTS else lines))
        raw_attention = data.get("attention")
        for entry in raw_attention if isinstance(raw_attention, (list, tuple)) else ():
            if isinstance(entry, Attention):
                result.attention.append(entry)
            elif isinstance(entry, dict):
                section, reason = _as_str(entry.get("section")), _as_str(entry.get("reason"))
                if section or reason:
                    result.attention.append(
                        Attention(section, _as_str(entry.get("item_id")), reason))
        raw_tasks = data.get("tasks")
        result.tasks = [_json_safe(task) for task in
                        (raw_tasks if isinstance(raw_tasks, (list, tuple)) else ())
                        if isinstance(task, dict)]
        raw_flags = data.get("flags")
        result.flags = _json_safe(raw_flags) if isinstance(raw_flags, dict) else {}
        return result


@dataclass
class Options:
    """What the builder or the importer ticked.

    ``selected[item_id]`` is ``True`` (the whole item) or a list of field / record keys;
    an item that is not in ``selected`` is left alone. ``modes[item_id]`` is one of the
    item's ``modes``, ``conflicts[item_id]`` is ``theirs`` or ``mine``; both fall back to the
    item's default. ``apply_tag`` says whether an import may set the group tag - the engine
    ignores it, ``group_apply`` reads it.
    """

    selected: dict = _dc_field(default_factory=dict)
    modes: dict = _dc_field(default_factory=dict)
    conflicts: dict = _dc_field(default_factory=dict)
    apply_tag: bool = True

    @classmethod
    def everything(cls, payloads):
        """Every item that has a payload, whole."""
        if not isinstance(payloads, dict):
            return cls()
        return cls(selected={item_id: True for item_id, payload in payloads.items()
                             if isinstance(item_id, str) and payload is not ABSENT})

    def to_json(self) -> dict:
        clean = Options.from_json({"selected": self.selected, "modes": self.modes,
                                   "conflicts": self.conflicts, "apply_tag": self.apply_tag})
        return {"selected": clean.selected, "modes": clean.modes,
                "conflicts": clean.conflicts, "apply_tag": clean.apply_tag}

    @classmethod
    def from_json(cls, data):
        """Tolerant: garbage reads as empty options; a missing or non-bool ``apply_tag``
        reads as ``True``. The result shares nothing with ``data``."""
        if not isinstance(data, dict):
            return cls()
        selected = {}
        raw = data.get("selected")
        for item_id, value in (raw.items() if isinstance(raw, dict) else ()):
            if not isinstance(item_id, str):
                continue
            if value is True:
                selected[item_id] = True
            elif isinstance(value, (list, tuple, set, frozenset)):
                keys = [key for key in value if isinstance(key, str)]
                selected[item_id] = keys if isinstance(value, (list, tuple)) else sorted(keys)

        def _words(raw_map):
            if not isinstance(raw_map, dict):
                return {}
            return {key: value for key, value in raw_map.items()
                    if isinstance(key, str) and isinstance(value, str)}

        return cls(selected=selected, modes=_words(data.get("modes")),
                   conflicts=_words(data.get("conflicts")),
                   apply_tag=data.get("apply_tag") is not False)


def _options(options) -> Options:
    if isinstance(options, Options):
        return Options.from_json({"selected": options.selected, "modes": options.modes,
                                  "conflicts": options.conflicts,
                                  "apply_tag": options.apply_tag})
    return Options.from_json(options)


# ── Paths into a config dict ────────────────────────────────────────────────

def get_path(d, path, default=None):
    """``d[path[0]][path[1]]...`` or ``default``. Only dicts are walked. Total."""
    try:
        for key in path:
            if not isinstance(d, dict) or key not in d:
                return default
            d = d[key]
    except TypeError:               # a path that is not iterable, or an unhashable key
        return default
    return d


def set_path(d, path, value) -> None:
    """Set ``d[path...] = value``. Missing dicts on the way are created and a non-dict
    intermediate is replaced by ``{}``. A non-dict root or an empty path is a programming
    error and raises (the engine then reports the item as failed and rolls it back)."""
    if not isinstance(d, dict):
        raise TypeError("set_path needs a dict to write into")
    keys = tuple(path)
    if not keys:
        raise ValueError("set_path needs a non-empty path")
    for key in keys[:-1]:
        child = d.get(key)
        if not isinstance(child, dict):
            child = d[key] = {}
        d = child
    d[keys[-1]] = value


def _is_path(path) -> bool:
    """True for a tuple of non-empty strings - what a config path must be. A string is not
    one: ``("intel_alerts_muted")`` without its comma would be walked letter by letter."""
    return isinstance(path, tuple) and all(isinstance(key, str) and key for key in path)


def del_path(d, path) -> None:
    """Remove the leaf key at ``path``. A missing path is silent; parents are kept."""
    try:
        keys = tuple(path)
        for key in keys[:-1]:
            if not isinstance(d, dict) or key not in d:
                return
            d = d[key]
        if keys and isinstance(d, dict):
            d.pop(keys[-1], None)
    except TypeError:
        return


def _config_of(state) -> dict:
    """The state's config dict FOR WRITING, created when the file is missing. Call it only
    once there is something to write: a no-op apply must leave a missing config missing."""
    if state.config is None:
        state.config = {}
    if not isinstance(state.config, dict):
        raise TypeError("config is not readable")
    return state.config


def _config_view(state) -> dict:
    """The state's config dict for reading; a missing or unreadable one reads as empty."""
    return state.config if isinstance(state.config, dict) else {}


# ── Display text ────────────────────────────────────────────────────────────

def _scalar_text(value) -> str:
    if value is ABSENT or value is None:
        return "(not set)"
    if value is True:
        return "on"
    if value is False:
        return "off"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return clean_text(value, 1_000_000) or "(blank)"
    return "(unreadable)"


def value_text(value, limit=None) -> str:
    """A payload or config value as one line of display text (``on`` / ``off``, a number,
    the text, a comma-separated list). Total. ``limit`` clips with ``...``."""
    if isinstance(value, (list, tuple)):
        text = ", ".join(_scalar_text(entry) for entry in value) or "(none)"
    else:
        text = _scalar_text(value)
    if type(limit) is int and limit > 3 and len(text) > limit:
        text = text[:limit - 3].rstrip() + "..."
    return text


def _key_set(keys) -> set:
    if isinstance(keys, str):
        return {keys}
    try:
        return {key for key in keys if isinstance(key, str)}
    except TypeError:
        return set()


def _count(n, one, many) -> str:
    return f"{n} {one if n == 1 else many}"


# ── Name lists (shared by the `names` field kind and NameListItem) ──────────

def _clean_names(values, resolve, max_items):
    """Resolve a list of names. -> ``(names, problems)``.

    Non-strings and blanks are left out, names ``resolve`` does not know are left out,
    duplicates (ignoring case) keep their first spelling, and at most ``max_items`` survive.
    ``problems`` holds at most three lines however long the list is.
    """
    names, seen, unknown, junk, extra = [], set(), [], 0, 0
    for entry in values:
        if not isinstance(entry, str):
            junk += 1
            continue
        try:
            name = resolve(entry)
        except Exception:
            name = None
        if not isinstance(name, str) or not name:
            shown = clean_text(entry, 40)
            if shown:
                unknown.append(shown)
            else:
                junk += 1
            continue
        key = name.lower()
        if key in seen:
            continue
        if len(names) >= max_items:
            extra += 1
            continue
        seen.add(key)
        names.append(name)
    problems = []
    if unknown:
        more = len(unknown) - _SHOWN_NAMES
        shown = ", ".join(unknown[:_SHOWN_NAMES]) + (f" and {more} more" if more > 0 else "")
        problems.append(f"{_count(len(unknown), 'unknown name', 'unknown names')} "
                        f"left out ({shown})")
    if junk:
        problems.append(f"{_count(junk, 'entry that is not a name', 'entries that are not names')}"
                        f" left out")
    if extra:
        problems.append(f"only the first {max_items} names are kept ({extra} left out)")
    return names, problems


# ── Item ────────────────────────────────────────────────────────────────────

class Item:
    """One shareable unit. Subclass it, or build a generic one with keyword arguments.

    Attributes (class attributes; any of them may be passed to the constructor):

    ``id``          ``"<category>.<key>"`` - also where the payload sits in the file
    ``label``       the name shown in the window
    ``kind``        ``value`` (a few fields) or ``collection`` (a list of records)
    ``files``       the ``FILE_NAMES`` keys the item reads or writes - what the engine
                    snapshots before ``apply`` and restores when it raises
    ``paths``       everything the item may write: ``(file_key, path_tuple)`` pairs
    ``order``       position inside the category
    ``sections``    Settings TOC titles the item may raise attention on
    ``modes``       ``("merge",)`` or ``("merge", "replace")``; the first is the default
    ``conflicts``   True when a same-named record offers theirs / mine
    ``never_saved`` human lines: what this item leaves out of a file on purpose
    ``partial_ok``  True when the item copes with an unreadable SIDE file itself; an
                    unreadable ``config.json`` skips every item that names it regardless
    """

    id = ""
    label = ""
    kind = "value"
    files = ()
    paths = ()
    order = 0
    sections = ()
    modes = ("merge",)
    conflicts = False
    never_saved = ()
    partial_ok = False

    def __init__(self, **attrs):
        for name, value in attrs.items():
            if name.startswith("_") or not hasattr(type(self), name):
                raise TypeError(f"{type(self).__name__} has no attribute {name!r}")
            setattr(self, name, value)

    def __repr__(self):
        return f"<{type(self).__name__} {self.id}>"

    @property
    def category(self) -> str:
        return self.id.partition(".")[0] if isinstance(self.id, str) and "." in self.id else ""

    @property
    def key(self) -> str:
        return self.id.partition(".")[2] if isinstance(self.id, str) else ""

    # -- what a subclass provides --------------------------------------------

    def export(self, state, ctx):
        """The payload this setup has to share, or ``ABSENT``. Total."""
        return ABSENT

    def export_notes(self, state, ctx=None) -> list:
        """Lines for the BUILDER of a file: what of this setup ``export`` leaves out on
        purpose and the builder would not expect - a record that breaks a limit, a name
        that cannot travel. ``[]`` when everything travels (the default). Total.
        ``collect_export_notes`` asks every item and bounds what each one says."""
        return []

    def validate(self, payload, ctx):
        """``(clean payload | ABSENT, problems)`` from an untrusted payload. Total."""
        return ABSENT, []

    def records(self, payload) -> list:
        """The tickable rows of a payload: its fields (value) or its records (collection)."""
        return []

    def summary(self, payload) -> str:
        """One short phrase for the tree and the preview, e.g. ``"3 systems"``."""
        return ""

    def lines(self, payload) -> list:
        """``(label, value)`` pairs: everything the payload holds, for "What will be saved"."""
        return []

    def subset(self, payload, keys):
        """The part of ``payload`` whose record keys are in ``keys``, or ``ABSENT``.

        This default covers three shapes: a value item's dict of fields; a collection's dict
        whose keys are exactly its record keys (``{name: definition}``); and a collection's
        list that ``records`` describes one-to-one, in order. Any other shape - a wrapper
        dict such as ``{"version": 1, "templates": [...]}`` - gives ``ABSENT``, so that
        nothing is ever applied by accident: such an item overrides this method.
        """
        wanted = _key_set(keys)
        if not wanted or not isinstance(payload, (dict, list)):
            return ABSENT
        if isinstance(payload, dict) and self.kind != "collection":
            return {key: value for key, value in payload.items() if key in wanted} or ABSENT
        try:
            row_keys = [row.key for row in self.records(payload)]
        except Exception:
            return ABSENT
        if isinstance(payload, dict):
            if len(row_keys) != len(payload) or set(row_keys) != set(payload):
                return ABSENT
            return {key: value for key, value in payload.items() if key in wanted} or ABSENT
        if len(row_keys) != len(payload):
            return ABSENT
        return [entry for entry, key in zip(payload, row_keys) if key in wanted] or ABSENT

    def apply(self, state, payload, mode, conflict, ctx) -> ItemResult:
        """Land a validated payload in ``state`` and say what changed."""
        raise NotImplementedError(f"{type(self).__name__}.apply")


class ConfigFieldsItem(Item):
    """A value item made of typed ``config.json`` fields.

    Build one with ``ConfigFieldsItem(id=..., label=..., order=..., fields=(Field(...), ...))``;
    ``paths`` is derived from the fields unless given. A field that is missing from a payload
    is never read or written - and an EMPTY ``names`` list counts as missing, so that a
    setup with nothing in a list can never blank the importer's (an import never deletes).
    ``apply`` cleans each value again, so a wrong-typed value is skipped rather than written
    even when a caller forgot to validate.

    Override points: ``resolve_name(field, name, ctx)`` (how ``system`` and ``names`` values
    are resolved - default: the system table, giving the canonical name);
    ``keep_unknown_systems`` (True: ``validate`` keeps an unknown ``system`` value, with its
    problem line, so that ``apply`` runs, puts the field in ``skipped`` and the subclass can
    add an :class:`Attention`); and ``_clean_field(fld, value, ctx, keep_unknown=False)``,
    the subclass SEAM for a rule of the item's own: ``export``, ``validate`` and ``apply``
    all clean a value through that one method, so an override that returns
    ``(ABSENT, [reason])`` for a value it will not take - and calls ``super()`` for
    everything else - refuses it in all three places. Its name and signature are part of
    this class's contract (``group_items_fittings.MotdSettingsItem`` overrides it).

    A ``str`` value needs at least one VISIBLE character (``group_bundle.has_visible``):
    text made only of zero-width, bidi or separator characters is blank - refused, or
    stored as ``""`` when the field allows a blank. A file must not be able to set an
    x-up trigger word nobody can see.
    """

    kind = "value"
    files = ("config",)
    fields = ()
    keep_unknown_systems = False

    def __init__(self, **attrs):
        super().__init__(**attrs)
        self.fields = tuple(self.fields)
        for fld in self.fields:
            if not isinstance(fld, Field) or fld.kind not in FIELD_KINDS:
                raise ValueError(f"{self.id}: not a usable Field: {fld!r}")
            if not _is_path(fld.path) or not fld.path:
                raise ValueError(f"{self.id}: the path of {fld.key!r} must be a non-empty "
                                 f"tuple of config keys: {fld.path!r}")
        if "paths" not in attrs and not type(self).paths:
            self.paths = tuple(("config", tuple(fld.path)) for fld in self.fields)

    def resolve_name(self, field, name, ctx):
        """``name`` as it should be stored, or ``None`` when it is not known."""
        hit = ctx.system_ids(name)
        return hit[0] if isinstance(hit, tuple) and hit else None

    def _resolve(self, fld, name, ctx):
        try:
            resolved = self.resolve_name(fld, name, ctx)
        except Exception:
            return None
        return resolved if isinstance(resolved, str) and resolved else None

    def _clean_field(self, fld, value, ctx, keep_unknown=False):
        """One value -> ``(clean | ABSENT, [reason, ...])``. Total.

        THE SUBCLASS SEAM (see the class docstring): ``export``, ``validate`` and ``apply``
        clean every value through this method and nothing else, so an item with a rule of
        its own overrides it - keeping this name and this signature - and calls ``super()``
        for the values it has nothing to say about. A reason is a sentence tail for
        ``"<item> - <field>: <reason>"`` and never quotes a value of unbounded size."""
        kind = fld.kind
        if kind == "bool":
            if type(value) is bool:
                return value, []
            return ABSENT, ["is not on or off"]
        if kind == "int":
            if type(value) is not int:          # bool and float (NaN, Infinity) end here
                return ABSENT, ["is not a whole number"]
            if (type(fld.lo) is int and value < fld.lo) or (type(fld.hi) is int and value > fld.hi):
                # the value is NOT quoted: a file can carry a number thousands of digits long
                return ABSENT, ["not a whole number in range"]
            return value, []
        if kind == "str":
            if not isinstance(value, str):
                return ABSENT, ["is not text"]
            text = clean_text(value, fld.max_len)
            if not has_visible(text):           # nothing a reader can see: blank
                if not fld.allow_blank:
                    return ABSENT, ["is blank"]
                return "", []
            return text, []
        if kind == "enum":
            if isinstance(value, str) and any(value == choice for choice in fld.choices
                                              if isinstance(choice, str)):
                return str(value), []
            return ABSENT, ["is not one of the known choices"]
        if kind == "system":
            name = clean_text(value, MAX_NAME) if isinstance(value, str) else ""
            if not name:
                return ABSENT, ["is not a system name"]
            canonical = self._resolve(fld, name, ctx)
            if canonical:
                return canonical, []
            reason = [f"'{name}' is not a known system"]
            return (name if keep_unknown else ABSENT), reason
        if kind == "names":
            if not isinstance(value, list):
                return ABSENT, ["is not a list of names"]
            cap = fld.hi if type(fld.hi) is int and fld.hi > 0 else MAX_LIST
            names, problems = _clean_names(value, lambda n: self._resolve(fld, n, ctx), cap)
            if not names:                       # an empty list is "not present" (no problem
                return ABSENT, problems         # line); garbage must not blank a list either
            return sorted(names, key=lambda n: (n.lower(), n)), problems
        return ABSENT, ["is not supported"]

    def _line(self, fld, reason) -> str:
        return f"{self.label} - {fld.label}: {reason}"

    def export(self, state, ctx):
        config = getattr(state, "config", None)
        if not isinstance(config, dict):
            return ABSENT
        payload = {}
        for fld in self.fields:
            raw = get_path(config, fld.path, ABSENT)
            if raw is ABSENT:
                continue
            if fld.kind == "names" and not (isinstance(raw, list)
                                            and all(isinstance(n, str) for n in raw)):
                continue
            value, _ = self._clean_field(fld, raw, ctx)
            if value is not ABSENT:
                payload[fld.key] = value
        return payload or ABSENT

    def validate(self, payload, ctx):
        if not isinstance(payload, dict):
            return ABSENT, [f"{self.label}: not a settings block"]
        clean, problems = {}, []
        for fld in self.fields:
            if fld.key not in payload:
                continue
            value, reasons = self._clean_field(fld, payload[fld.key], ctx,
                                               keep_unknown=self.keep_unknown_systems)
            problems.extend(self._line(fld, reason) for reason in reasons)
            if value is not ABSENT:
                clean[fld.key] = value
        return (clean or ABSENT), problems

    def _present(self, payload):
        """The fields a payload carries. An empty ``names`` list is not one of them."""
        if not isinstance(payload, dict):
            return []
        return [fld for fld in self.fields if fld.key in payload
                and not (fld.kind == "names" and isinstance(payload[fld.key], list)
                         and not payload[fld.key])]

    def records(self, payload) -> list:
        return [Record(fld.key, fld.label, value_text(payload[fld.key]))
                for fld in self._present(payload)]

    def lines(self, payload) -> list:
        return [(fld.label, value_text(payload[fld.key])) for fld in self._present(payload)]

    def summary(self, payload) -> str:
        present = self._present(payload)
        if len(present) == 1:
            return value_text(payload[present[0].key], 80)
        return _count(len(present), "setting", "settings")

    def subset(self, payload, keys):
        wanted = _key_set(keys)
        return {fld.key: payload[fld.key] for fld in self._present(payload)
                if fld.key in wanted} or ABSENT

    @staticmethod
    def _same(fld, current, value) -> bool:
        if fld.kind == "names":                 # a set of names: order is not a change
            return (isinstance(current, list) and all(isinstance(n, str) for n in current)
                    and set(current) == set(value))
        return type(current) is type(value) and current == value

    def apply(self, state, payload, mode, conflict, ctx) -> ItemResult:
        result = ItemResult(self.id)
        present = self._present(payload)
        if not present:
            return result
        config = _config_view(state)
        for fld in present:
            value, reasons = self._clean_field(fld, payload[fld.key], ctx)
            if value is ABSENT:
                result.skipped.append(fld.key)
                result.warnings.extend(self._line(fld, reason) for reason in reasons)
                continue
            current = get_path(config, fld.path, ABSENT)
            if self._same(fld, current, value):
                result.unchanged.append(fld.key)
                continue
            config = _config_of(state)
            set_path(config, fld.path, list(value) if isinstance(value, list) else value)
            result.updated.append(fld.key)
            result.notes.append(f"{fld.label}: {value_text(current, _NOTE_CLIP)} -> "
                                f"{value_text(value, _NOTE_CLIP)}")
        if result.updated:
            result.status = "applied"
        elif result.skipped:
            result.status = "skipped"
        return result


class NameListItem(Item):
    """A collection item: a list of names at one ``config.json`` path.

    Build one with ``NameListItem(id=..., label=..., order=..., path=("a", "b"))``; ``paths``
    is derived unless given. ``modes`` stays ``("merge",)`` unless the item sets
    ``("merge", "replace")``. Subclass and override ``resolve(name, ctx)`` to check names
    against a table (return the spelling to store, or ``None`` to leave the name out).

    merge: missing names are appended; a name already there (ignoring case) keeps its
    spelling. replace: the list becomes the file's, in the file's order and spelling; names
    that left go to ``removed``. A stored value that is not a list counts as empty, and
    non-strings in a stored list are ignored - both with a warning. An empty or unusable
    payload changes nothing.
    """

    kind = "collection"
    files = ("config",)
    path = ()
    max_items = MAX_LIST
    max_len = MAX_NAME
    unit = ("name", "names")

    def __init__(self, **attrs):
        super().__init__(**attrs)
        if not _is_path(self.path):
            raise ValueError(f"{self.id}: path must be a tuple of config keys: {self.path!r}")
        if "paths" not in attrs and not type(self).paths:
            self.paths = (("config", self.path),)

    def resolve(self, name, ctx):
        """``name`` as it should be stored, or ``None`` to leave it out."""
        return clean_text(name, self.max_len) or None

    def _clean(self, payload, ctx):
        if not isinstance(payload, list):
            return [], [f"{self.label}: not a list of {self.unit[1]}"]
        cap = self.max_items if type(self.max_items) is int and self.max_items > 0 else MAX_LIST
        names, problems = _clean_names(payload, lambda name: self.resolve(name, ctx), cap)
        return names, [f"{self.label}: {line}" for line in problems]

    def export(self, state, ctx):
        stored = get_path(getattr(state, "config", None), self.path)
        if not isinstance(stored, list):
            return ABSENT
        return self._clean(stored, ctx)[0] or ABSENT

    def validate(self, payload, ctx):
        names, problems = self._clean(payload, ctx)
        return (names or ABSENT), problems

    def _names(self, payload):
        if not isinstance(payload, list):
            return []
        return [name for name in payload if isinstance(name, str)]

    def records(self, payload) -> list:
        return [Record(name, name) for name in self._names(payload)]

    def summary(self, payload) -> str:
        return _count(len(self._names(payload)), self.unit[0], self.unit[1])

    def lines(self, payload) -> list:
        names = self._names(payload)
        return [(self.unit[1].capitalize(), ", ".join(names))] if names else []

    def subset(self, payload, keys):
        wanted = _key_set(keys)
        return [name for name in self._names(payload) if name in wanted] or ABSENT

    def apply(self, state, payload, mode, conflict, ctx) -> ItemResult:
        result = ItemResult(self.id)
        incoming, _ = self._clean(payload, ctx)
        if not incoming:
            return result
        stored = get_path(_config_view(state), self.path)
        if stored is None:
            current = []
        elif isinstance(stored, list):
            current = [name for name in stored if isinstance(name, str)]
            if len(current) != len(stored):
                odd = _count(len(stored) - len(current), "entry that is not a name",
                             "entries that are not names")
                result.warnings.append(f"{self.label}: your saved list held {odd}")
        else:
            current = []
            result.warnings.append(f"{self.label}: your saved list could not be read "
                                   f"and counts as empty")
        spelling = {}
        for name in current:
            spelling.setdefault(name.lower(), name)

        if mode == "replace":
            new = list(incoming)
            arriving = {name.lower() for name in incoming}
            for name in incoming:
                mine = spelling.get(name.lower())
                if mine is None:
                    result.added.append(name)
                elif mine == name:
                    result.unchanged.append(name)
                else:
                    result.updated.append(name)
            for key, name in spelling.items():
                if key not in arriving:
                    result.removed.append(name)
        else:
            new = list(current)
            for name in incoming:
                mine = spelling.get(name.lower())
                if mine is None:
                    new.append(name)
                    spelling[name.lower()] = name
                    result.added.append(name)
                else:
                    result.unchanged.append(mine)

        if new != current or result.added or result.updated or result.removed:
            set_path(_config_of(state), self.path, new)
            result.status = "applied"
        return result


# ── Registry ────────────────────────────────────────────────────────────────

_REGISTRY = {}                          # item id -> Item
_modules_loaded = False                 # the "loaded once" latch of _load_item_modules
_load_lock = threading.RLock()          # held for the whole load; re-entrant on purpose
LOAD_FAILURES = []                      # (module name, exception type name), one per module
#                                         that failed to import or to register


def _load_item_modules() -> None:
    """Import the item modules and register their ``ITEMS`` - once per process.

    Every import is a PLAIN ``import`` statement, written out one module at a time: a frozen
    build ships only what PyInstaller's module graph can see, and a module that is imported
    by name from a string is invisible to it - the frozen app would start with an EMPTY
    registry while every source-tree test stays green. The statements sit in this function
    and not at the top of the file because every item module imports this one.

    A module that fails is recorded in ``LOAD_FAILURES`` and the others still load.

    The whole load runs under ``_load_lock``: a second thread that asks for the registry
    while the first one is still importing WAITS, and then sees all of it (it used to get
    the items registered so far). The lock is re-entrant and the latch is set before the
    first import, so an item module that calls ``registry()`` while it is being imported
    gets what is registered at that moment instead of waiting for itself.

    A test that resets ``_modules_loaded`` gets a clean second load: the failure list is
    emptied first, and registering the very same items again is not a clash of ids
    (``register``).
    """
    global _modules_loaded
    with _load_lock:
        if _modules_loaded:
            return
        _modules_loaded = True
        del LOAD_FAILURES[:]
        # ==== ITEM MODULES - BEGIN ==========================================
        # One block per item module, each of exactly this form. Plain import statements
        # ONLY - never a module name held in a string, never a loop over names
        # (tests/test_group_items.py::test_module_is_pure and
        # tests/test_group_registry.py hold this). A new item module needs a block here:
        # the registry test compares this list with the group_items_*.py files on disk.
        try:
            import group_items_palette as m
            register(m.ITEMS)
        except Exception as exc:    # recorded, never raised: the boot hook must not die
            LOAD_FAILURES.append(("group_items_palette", type(exc).__name__))
        try:
            import group_items_fleet as m
            register(m.ITEMS)
        except Exception as exc:
            LOAD_FAILURES.append(("group_items_fleet", type(exc).__name__))
        try:
            import group_items_intel as m
            register(m.ITEMS)
        except Exception as exc:
            LOAD_FAILURES.append(("group_items_intel", type(exc).__name__))
        try:
            import group_items_map as m
            register(m.ITEMS)
        except Exception as exc:
            LOAD_FAILURES.append(("group_items_map", type(exc).__name__))
        try:
            import group_items_fittings as m
            register(m.ITEMS)
        except Exception as exc:
            LOAD_FAILURES.append(("group_items_fittings", type(exc).__name__))
        try:
            import group_items_settings as m
            register(m.ITEMS)
        except Exception as exc:
            LOAD_FAILURES.append(("group_items_settings", type(exc).__name__))
        # ==== ITEM MODULES - END ============================================


_TUPLE_ATTRIBUTES = ("files", "paths", "modes", "sections", "never_saved")


def _check_item(item) -> None:
    """Refuse a malformed item. A programming error in an item module - it surfaces when
    that module is imported, long before a file is applied."""
    if not isinstance(item, Item):
        raise ValueError(f"not an Item: {item!r}")
    if not (isinstance(item.id, str) and item.category and item.key and "." not in item.key):
        raise ValueError(f"item id must be '<category>.<key>': {item.id!r}")
    if item.category not in CATEGORIES:
        raise ValueError(f"{item.id}: the category must be one of {CATEGORIES}")
    if item.kind not in KINDS:
        raise ValueError(f"{item.id}: kind must be one of {KINDS}")
    # Tuples, each of them: a generator for `files` is empty the second time it is walked
    # (nothing would be snapshotted before `apply`), and ("merge") without its comma is the
    # string "merge" - an item with the modes 'm', 'e', 'r', 'g', 'e'.
    for name in _TUPLE_ATTRIBUTES:
        if not isinstance(getattr(item, name), tuple):
            raise ValueError(f"{item.id}: {name} must be a tuple: {getattr(item, name)!r}")
    files, paths = item.files, item.paths
    if not files or not all(isinstance(key, str) and key in FILE_NAMES for key in files):
        raise ValueError(f"{item.id}: files must name at least one FILE_NAMES key: "
                         f"{item.files!r}")
    for entry in paths:
        if not (isinstance(entry, tuple) and len(entry) == 2 and isinstance(entry[0], str)
                and entry[0] in files and _is_path(entry[1])):
            raise ValueError(f"{item.id}: a paths entry must be (one of the item's files, "
                             f"tuple of keys): {entry!r}")
        if entry[0] == "config" and not entry[1]:
            # An empty path is "the whole file" - fine for a file that IS one item (the
            # palette), never for config.json: it would put every setting in reach.
            raise ValueError(f"{item.id}: an item may not declare all of config.json")
    if not item.modes or not all(isinstance(mode, str) and mode for mode in item.modes):
        raise ValueError(f"{item.id}: modes must name at least one mode")
    if not all(isinstance(line, str) for line in item.sections + item.never_saved):
        raise ValueError(f"{item.id}: sections and never_saved hold text")
    # The paths of the generic items, checked HERE and not only when they are built: a
    # subclass may set its path after the base constructor has run (the staging lists do).
    if isinstance(item, ConfigFieldsItem):
        for fld in item.fields if isinstance(item.fields, tuple) else (None,):
            if not (isinstance(fld, Field) and _is_path(fld.path) and fld.path):
                raise ValueError(f"{item.id}: every field needs a non-empty path: {fld!r}")
    if isinstance(item, NameListItem) and not (_is_path(item.path) and item.path):
        raise ValueError(f"{item.id}: path must be a non-empty tuple of config keys: "
                         f"{item.path!r}")


def register(items) -> None:
    """Add items to the registry.

    A malformed item is a programming error: ``ValueError``, and nothing of this call is
    registered. So is an id that is TAKEN - by another item of this call, or by a
    different item registered before: two item modules that pick the same id must not
    silently replace one another. Registering the very same item again is not a clash and
    changes nothing (a repeated load of the item modules hands back the same objects).
    """
    items = list(items)
    seen = {}
    for item in items:
        _check_item(item)
        taken = seen.get(item.id, _REGISTRY.get(item.id))
        if taken is not None and taken is not item:
            raise ValueError(f"{item.id}: an item with this id is registered already")
        seen[item.id] = item
    _REGISTRY.update(seen)


def _sort_key(item):
    category = item.category
    index = CATEGORIES.index(category) if category in CATEGORIES else len(CATEGORIES)
    return index, item.order if type(item.order) is int else 0, item.id


def registry() -> tuple:
    """Every registered item, sorted by category (``group_bundle.CATEGORIES`` order), then
    ``order``. This is the order items are exported, validated and applied in."""
    _load_item_modules()
    return tuple(sorted(_REGISTRY.values(), key=_sort_key))


def get_item(item_id):
    """The item with that id, or ``None``."""
    _load_item_modules()
    return _REGISTRY.get(item_id) if isinstance(item_id, str) else None


@contextlib.contextmanager
def isolated_registry(items=()):
    """For tests: a registry that holds only ``items`` (the item modules are not loaded
    inside the block); the previous registry, latch and ``LOAD_FAILURES`` are put back on
    exit - also when the block reset the latch to run the real load."""
    global _modules_loaded
    saved, saved_loaded, saved_failures = dict(_REGISTRY), _modules_loaded, list(LOAD_FAILURES)
    _REGISTRY.clear()
    _modules_loaded = True
    try:
        register(items)
        yield
    finally:
        _REGISTRY.clear()
        _REGISTRY.update(saved)
        _modules_loaded = saved_loaded
        LOAD_FAILURES[:] = saved_failures


# ── Engine ──────────────────────────────────────────────────────────────────

def _selection(item, payload, chosen):
    """The part of ``payload`` the options ask for, or ``ABSENT``. May raise (``subset``,
    or the copy of a payload nested too deep to copy).

    ``subset`` is handed a COPY: "the caller's payloads are never changed" must not depend
    on every item's ``subset`` being careful with what it is given."""
    if payload is ABSENT:
        return ABSENT
    if chosen is True:
        return payload
    if isinstance(chosen, (list, tuple, set, frozenset)):
        return item.subset(deepcopy(payload), chosen)
    return ABSENT


def collect_export_notes(state, ctx=None, items=None) -> list:
    """What the builder of a file should know before saving it: the ``export_notes`` lines
    of ``items`` (items or item ids, in the order given; ``None`` = every registered item,
    in registry order), as one flat list.

    Never raises. An item whose ``export_notes`` raises contributes the one line
    ``"<label>: could not be checked"``; one that returns anything but a list of lines
    contributes the lines that are text (blank ones are left out). Each item's lines are
    bounded on their own: ``MAX_RESULT_LINES`` of them, then ``"and <n> more"``.
    """
    ctx = default_ctx() if ctx is None else ctx
    if items is None:
        chosen = registry()
    elif isinstance(items, (list, tuple)):
        chosen = [entry if isinstance(entry, Item) else get_item(entry) for entry in items]
    else:
        chosen = ()
    notes = []
    for item in chosen:
        if not isinstance(item, Item):
            continue
        try:
            lines = item.export_notes(state, ctx)
            if not isinstance(lines, (list, tuple)):
                lines = ()
            lines = [line for line in lines if isinstance(line, str) and line.strip()]
        except Exception:
            lines = [f"{clean_text(item.label) or clean_text(item.id)}: could not be checked"]
        notes.extend(_bounded(lines))
    return notes


def export_all(state, ctx=None) -> dict:
    """``{item_id: payload}`` for every item that has something to share, in registry order.
    Payloads are copies: editing one never touches ``state``. An item whose export raises is
    left out."""
    ctx = default_ctx() if ctx is None else ctx
    payloads = {}
    for item in registry():
        try:
            payload = item.export(state, ctx)
        except Exception:
            continue
        if payload is not ABSENT and payload is not None:
            payloads[item.id] = deepcopy(payload)
    return payloads


def build_bundle(payloads, options, meta) -> Bundle:
    """The file for a selection: only registered, selected items, and of those only the
    selected fields and records. A category with no item is left out. The bundle holds
    copies of the payloads."""
    options = _options(options)
    categories = {}
    for item in registry() if isinstance(payloads, dict) else ():
        if item.id not in payloads:
            continue
        try:
            payload = _selection(item, payloads[item.id], options.selected.get(item.id))
        except Exception:
            continue
        if payload is ABSENT or payload is None:
            continue
        categories.setdefault(item.category, {})[item.key] = deepcopy(payload)
    return Bundle(meta=meta, categories=categories)


def validate_bundle(bundle, ctx=None):
    """``({item_id: clean payload}, problems)`` for a bundle read from a file. Total.

    A category this build does not know and an item id that is not registered each give one
    problem line starting with ``NOT_SUPPORTED``, followed by that category or item id.
    Every other payload goes through its item's ``validate``; ``ABSENT`` results are left
    out. The bundle is not modified.

    After every payload has been validated comes the RECONCILE pass, for rules that span
    items: each item module that has an item registered and defines a module-level
    ``reconcile(payloads) -> [problem lines]`` is called, in registry order, with the dict
    of clean payloads (item id -> payload). It may change a payload in place or delete a
    key (that item is then not in the result, as if the file had not carried it). A
    ``reconcile`` that raises is contained: one problem line, and the payloads are exactly
    what they were before it was called. It cannot add an item: after each call only
    registered item ids with a payload are kept.

    The problems are BOUNDED, whatever the file holds: the first ``MAX_PROBLEMS`` lines,
    then one last line ``"and <n> more"``. What explains the file comes first - the
    validators' lines, then the reconcile lines - and the ``NOT_SUPPORTED`` lines last, so
    that a file with fifty unknown keys cannot crowd every real explanation out.
    """
    ctx = default_ctx() if ctx is None else ctx
    categories = getattr(bundle, "categories", None)
    if not isinstance(categories, dict):
        return {}, []
    items = {item.id: item for item in registry()}
    raw, unknown, more = {}, [], 0
    for category, entries in categories.items():
        if not isinstance(category, str) or category not in CATEGORIES:
            if len(unknown) < MAX_PROBLEMS:
                unknown.append(NOT_SUPPORTED + clean_text(category))
            else:
                more += 1
            continue
        if not isinstance(entries, dict):
            continue
        for key, payload in entries.items():
            item_id = f"{category}.{key}" if isinstance(key, str) else None
            if item_id in items:
                raw[item_id] = payload
            elif len(unknown) < MAX_PROBLEMS:
                unknown.append(NOT_SUPPORTED + clean_text(f"{category}.{key}"))
            else:                               # counted, not built: 100,000 unknown keys
                more += 1                       # must not cost 100,000 lines
    problems = []

    def say(lines) -> int:
        """Add the lines that are text while there is room; -> how many did not fit."""
        left_out = 0
        for line in lines if isinstance(lines, (list, tuple)) else ():
            if not isinstance(line, str) or not line:
                continue
            if len(problems) < MAX_PROBLEMS:
                problems.append(line)
            else:
                left_out += 1
        return left_out

    payloads = {}
    for item_id, item in items.items():         # registry order
        if item_id not in raw:
            continue
        try:
            clean, lines = item.validate(raw[item_id], ctx)
        except Exception as exc:                # validators are total; this is the backstop
            clean, lines = ABSENT, [f"{item.label}: could not be checked "
                                    f"({type(exc).__name__})"]
        more += say(lines)
        if clean is not ABSENT and clean is not None:
            payloads[item_id] = clean
    for label, reconcile in _reconcilers() if payloads else ():
        payloads, lines = _reconciled(payloads, label, reconcile, items)
        more += say(lines)
    more += say(unknown)
    if more:
        problems.append(f"and {more} more")
    return payloads, problems


def _reconcilers() -> list:
    """``[(label, reconcile)]``: the module-level ``reconcile`` hook of every loaded item
    module (``group_items_*``) that has at least one of its ``ITEMS`` in the registry, in
    the registry order of that item. ``label`` names the module to the importer: the
    category of that first item.

    The modules are found in ``sys.modules`` - where a plain ``import`` in
    ``_load_item_modules`` put them - and never imported from here, by name or otherwise.
    A module whose items are not registered (an isolated registry in a test) is not asked.
    """
    owners = {}
    for name, module in list(sys.modules.items()):
        if module is None or not isinstance(name, str) \
                or not name.startswith(_ITEM_MODULE_PREFIX):
            continue
        hook, own = getattr(module, "reconcile", None), getattr(module, "ITEMS", None)
        if not callable(hook) or not isinstance(own, (tuple, list)):
            continue
        for item in own:
            owners.setdefault(id(item), (name, hook))
    found, seen = [], set()
    for item in registry():
        name, hook = owners.get(id(item), (None, None))
        if hook is not None and name not in seen:
            seen.add(name)
            found.append((CATEGORY_LABELS.get(item.category, item.category), hook))
    return found


def _reconciled(payloads, label, reconcile, items):
    """Run one ``reconcile`` hook. -> ``(payloads, lines)``.

    The hook gets the real dict and may edit it. When it raises, the dict it half-edited
    is thrown away and the snapshot taken before the call is returned with one problem
    line. Afterwards only registered ids with a payload are kept, in registry order."""
    try:
        before = deepcopy(payloads)             # nested edits must be undone too
    except Exception as exc:                    # cannot be put back: the hook is not run
        return payloads, [f"{label}: could not be cross-checked ({type(exc).__name__})"]
    try:
        lines = reconcile(payloads)
    except Exception as exc:
        return before, [f"{label}: could not be cross-checked ({type(exc).__name__})"]
    kept = {item_id: payloads[item_id] for item_id in items
            if item_id in payloads and payloads[item_id] is not ABSENT
            and payloads[item_id] is not None}
    return kept, lines


def apply_bundle(state, payloads, options, ctx=None):
    """Apply the selected payloads to ``state``. -> ``(state, [ItemResult, ...])``.

    MUTATES ``state`` and returns that same object - pass ``state.copy()`` to keep the
    original. ``payloads`` is the output of ``validate_bundle``.

    Items run in registry order, whatever the order of ``payloads``. An item runs only when
    it is in ``options.selected`` AND has a payload; a selection list is narrowed with
    ``item.subset`` first. Everything else is not in the results and is left untouched.
    The mode defaults to ``item.modes[0]`` (also for a mode the item does not offer), the
    conflict choice to ``theirs``.

    An item that names an unreadable file is ``skipped`` with "<file name> could not be
    read" - unless it sets ``partial_ok`` and ``config.json`` itself is readable.

    Each item gets a deep copy of its payload - ``subset`` is handed a copy too, so
    ``payloads`` is never changed - and is not trusted with what it leaves behind. It is
    rolled back alone - the files it names in ``files`` are put back exactly as they were
    before it ran, nothing else is touched - and reported as ``failed`` when it

    * raises, or returns no ``ItemResult``                    -> the exception type;
    * returns a ``failed`` result                             -> the item's own warnings;
    * returns a status that is not one of ``STATUSES``        -> ``"bad status"``;
    * leaves a declared file that is no longer a ``dict`` (a missing file may stay missing),
      or writes one that was ``UNREADABLE``                   -> ``"bad file state"``.

    Nothing else such a result claimed (change lists, tasks, flags) is reported. A file that
    cannot be snapshotted (``RecursionError``: nested too deep to copy) fails the items that
    name it, before they run. The items before and after a failed one are not affected.

    The ``notes`` and ``warnings`` of every result are bounded (``ItemResult.bound``),
    whether or not the item settled it.
    """
    return _apply(state, payloads, options, ctx)


def _failed(item, *reasons) -> ItemResult:
    return ItemResult(item.id, status="failed", warnings=list(reasons))


def _distrust(result, before, state) -> list:
    """Why the engine does not accept what ``apply`` returned - warning lines - or ``[]``.
    ``before`` is the pre-item snapshot of the item's files."""
    if not isinstance(result.status, str) or result.status not in STATUSES:
        return ["bad status"]
    if result.status == "failed":
        return _text_list(result.warnings) or ["failed"]
    for key, prior in before.items():
        now = getattr(state, key)
        if prior is None:
            intact = now is None or isinstance(now, dict)       # still missing, or created
        elif isinstance(prior, dict):
            intact = isinstance(now, dict)
        else:
            intact = prior is not UNREADABLE or now is UNREADABLE   # never written over
        if not intact:
            return ["bad file state"]
    return []


def _apply(state, payloads, options, ctx, broken=None):
    """``apply_bundle``. With ``broken`` (an exception type name: the state could not even
    be copied) nothing runs and every item that would have run is ``failed`` with it."""
    options = _options(options)
    ctx = default_ctx() if ctx is None else ctx
    results = []
    if not isinstance(payloads, dict):
        return state, results
    for item in registry():
        if item.id not in payloads or item.id not in options.selected:
            continue
        try:
            payload = _selection(item, payloads[item.id], options.selected[item.id])
        except Exception as exc:
            results.append(_failed(item, type(exc).__name__))
            continue
        if payload is ABSENT or payload is None:
            continue
        if broken is not None:
            results.append(_failed(item, broken))
            continue

        unreadable = [key for key in item.files if not state.readable(key)]
        if unreadable and ("config" in unreadable or not item.partial_ok):
            results.append(ItemResult(item.id, status="skipped", warnings=[
                f"{FILE_NAMES.get(key, key)} could not be read" for key in unreadable]))
            continue

        mode = options.modes.get(item.id)
        if not isinstance(mode, str) or mode not in item.modes:
            mode = item.modes[0]
        conflict = options.conflicts.get(item.id)
        if not isinstance(conflict, str) or conflict not in CONFLICTS:
            conflict = CONFLICTS[0]

        try:                                    # nothing has run yet: nothing to roll back
            before = {key: deepcopy(getattr(state, key)) for key in item.files}
            own_payload = deepcopy(payload)
        except Exception as exc:                # RecursionError: nested too deep to copy
            results.append(_failed(item, type(exc).__name__))
            continue
        try:
            result = item.apply(state, own_payload, mode, conflict, ctx)
            if not isinstance(result, ItemResult):
                raise TypeError("apply returned no ItemResult")
            doubts = _distrust(result, before, state)
        except Exception as exc:
            doubts = [type(exc).__name__]
        if doubts:
            for key, value in before.items():   # ONLY the declared files, EXACTLY as before
                setattr(state, key, value)
            result = _failed(item, *doubts)
        result.item_id = item.id
        results.append(result.bound())          # an item need not have settled its result
    return state, results


def preview(state, payloads, options, ctx=None) -> list:
    """What ``apply_bundle`` would report, computed by running it on a copy. ``state`` is
    never modified. Never raises: a state that cannot be copied (``RecursionError`` - a file
    nested too deep) gives one ``failed`` result per item that would have run."""
    try:
        twin = state.copy()
    except Exception as exc:
        return _apply(state, payloads, options, ctx, broken=type(exc).__name__)[1]
    return _apply(twin, payloads, options, ctx)[1]
