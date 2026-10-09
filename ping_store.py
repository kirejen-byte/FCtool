"""The ``discord_ping`` block of the app config - its one reader and writer (pure: no Tk).

The Discord ping keeps everything it remembers in ONE top-level config block::

    "discord_ping": {
      "version": 1,
      "fleet_types": ["Stratop", "CTA"],        # three shared option lists
      "locations": ["Jita"],
      "comms": ["Op 1"],
      "now_text": "Now",                        # two texts
      "doctrine_text": "Check MOTD",
      "when_format": "time_countdown",          # one of WHEN_FORMATS
      "basic": [...],                           # the basic template; absent = the built-in one
      "default_picks": {"fleet_type": "CTA"},   # the picks used with no doctrine selected
      "doctrines": {
        "<doctrine id>": {"doc": [...],         # its own template; absent = use the basic one
                          "picks": {"comms": "Op 1"}}
      }
    }

A template is an opaque JSON list here (``motd_doc.doc_to_json`` output); this module never
looks inside one.

The rules every method keeps:

* **The store never holds the config.** Save Settings REPLACES the app's config dict; an
  object that kept the old dict, or any dict inside it, would then write into an orphan and
  the change would be lost. So :class:`PingStore` is given a ``get_config()`` callable, asks
  it for the dict on every call, and keeps nothing but that callable and ``save``.
* **Reads never write.** With the block absent or damaged every getter answers with its
  default and leaves the config exactly as it found it. Whatever a getter hands out is a
  fresh object, never a list that lives in the config.
* **The block is only made by a change.** It appears on the first call that changes
  something, as ``{"version": 1}`` plus what was written - every key is optional and the
  getters supply the rest. ``save()`` runs exactly once per such call. A call that would
  only restate what is already in force (the stored value again, or - where nothing is
  stored yet - the value already read back) writes nothing and does not save.
* **Junk found in the config is read around, never raised on.** A wrong-typed block, table,
  entry or value counts as absent. It is replaced only when a change has to write through
  it; junk beside the written key stays as it is.
* **A wrong name from the caller is a programmer error:** an unknown list kind, pick field,
  text key or When format, a non-list where a list is due, a non-text pick or text - each is
  a ``ValueError`` and changes nothing.

Picks are stored one field at a time. A doctrine answers a field from its own picks, else
from ``default_picks``; the answer is then checked against the lists as they are NOW, so a
pick that names a removed item falls back on the way out while the stored pick is left be.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

BLOCK = "discord_ping"
VERSION = 1
LIST_KINDS = ("fleet_types", "locations", "comms")
DEFAULT_FLEET_TYPES = ("Stratop", "CTA", "Home Defense", "Roam")
STAGING = "@staging"
TEXT_DEFAULTS = {"now_text": "Now", "doctrine_text": "Check MOTD"}
WHEN_FORMATS = ("time_countdown", "full", "datetime_countdown")
MAX_ITEMS = 50
MAX_CHARS = 80

PICK_FIELDS = ("fleet_type", "location", "doctrine", "comms")
DOCTRINE_MODES = ("text", "name")

_DEFAULT_WHEN_FORMAT = "time_countdown"
_DEFAULT_DOCTRINE_MODE = "text"


@dataclass(frozen=True)
class Picks:
    fleet_type: str          # a fleet_types item, or "" when the list is empty
    location: str            # STAGING or a locations item
    doctrine: str            # "text" | "name"
    comms: str               # a comms item, or "" when the list is empty


# -- cleaning -------------------------------------------------------------------------------

def _clean_text(value) -> str:
    """One stored line: stripped and cut to MAX_CHARS; anything that is not text is ``""``."""
    if not isinstance(value, str):
        return ""
    return value.strip()[:MAX_CHARS].rstrip()


def _clean_items(items) -> list:
    """A shared list: clean items, blanks dropped, one spelling per name, first MAX_ITEMS."""
    kept, seen = [], set()
    if not isinstance(items, (list, tuple)):
        return kept
    for item in items:
        text = _clean_text(item)
        folded = text.casefold()
        if not text or folded in seen:
            continue
        seen.add(folded)
        kept.append(text)
        if len(kept) == MAX_ITEMS:
            break
    return kept


def _require(name, allowed, what) -> None:
    if not isinstance(name, str) or name not in allowed:
        raise ValueError("unknown %s: %r" % (what, name))


def _doctrine_key(doctrine_id) -> Optional[str]:
    """The ``doctrines`` key for an id; ``None`` (or ``""``) = no doctrine selected."""
    if doctrine_id is None or doctrine_id == "":
        return None
    return str(doctrine_id)


# -- reading (never writes: every helper below only looks) ----------------------------------

def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _block(cfg) -> dict:
    """The stored block, or an empty stand-in when it is absent or not a dict."""
    return _as_dict(_as_dict(cfg).get(BLOCK))


def _entry(block: dict, key: Optional[str]) -> dict:
    """One doctrine's stored entry; empty when there is none (or it is junk)."""
    if key is None:
        return {}
    return _as_dict(_as_dict(block.get("doctrines")).get(key))


def _stored_picks(block: dict, key: Optional[str]) -> dict:
    """The dict a pick for ``key`` is stored in: ``default_picks`` when no doctrine."""
    if key is None:
        return _as_dict(block.get("default_picks"))
    return _as_dict(_entry(block, key).get("picks"))


def _items(block: dict, kind: str) -> list:
    stored = block.get(kind)
    fallback = list(DEFAULT_FLEET_TYPES) if kind == "fleet_types" else []
    if not isinstance(stored, list):
        return fallback
    items = _clean_items(stored)
    # A stored EMPTY list is the user's own choice and stays empty. A list that held only
    # junk is damage, not a choice.
    return items if items or not stored else fallback


def _picks(block: dict, key: Optional[str]) -> Picks:
    sources = (_stored_picks(block, key), _stored_picks(block, None))

    def stored(field: str) -> str:
        for source in sources:
            value = source.get(field)
            if isinstance(value, str):
                return _clean_text(value)
        return ""

    def from_list(field: str, kind: str) -> str:
        items = _items(block, kind)
        value = stored(field)
        if value in items:
            return value
        return items[0] if items else ""

    location = stored("location")
    if location != STAGING and location not in _items(block, "locations"):
        location = STAGING
    doctrine = stored("doctrine")
    if doctrine not in DOCTRINE_MODES:
        doctrine = _DEFAULT_DOCTRINE_MODE
    return Picks(fleet_type=from_list("fleet_type", "fleet_types"), location=location,
                 doctrine=doctrine, comms=from_list("comms", "comms"))


def _text(block: dict, key: str) -> str:
    return _clean_text(block.get(key)) or TEXT_DEFAULTS[key]


def _when_format(block: dict) -> str:
    stored = block.get("when_format")
    if isinstance(stored, str) and stored in WHEN_FORMATS:
        return stored
    return _DEFAULT_WHEN_FORMAT


def _own_template(block: dict, key: Optional[str]):
    """A doctrine's own template (the stored list itself), or ``None``."""
    doc = _entry(block, key).get("doc")
    return doc if isinstance(doc, list) else None


def _in_force(holder: dict, key: str, new, read_back) -> bool:
    """Would storing ``new`` under ``holder[key]`` only restate what is already there?

    A stored value is compared as stored. Where nothing is stored yet, ``new`` is compared
    with what a reader gets today - so choosing the default again does not create the block.
    """
    if key in holder:
        return holder[key] == new
    return read_back == new


# -- writing --------------------------------------------------------------------------------

def _open_block(cfg) -> dict:
    """The block of ``cfg``, made (or made anew over junk) for a change that needs it."""
    if not isinstance(cfg, dict):
        raise TypeError("get_config() must return the config dict to write to")
    block = cfg.get(BLOCK)
    if not isinstance(block, dict):
        block = cfg[BLOCK] = {}
    block.setdefault("version", VERSION)
    return block


def _open(parent: dict, key: str) -> dict:
    """``parent[key]`` as a dict to write into; anything else found there is replaced."""
    child = parent.get(key)
    if not isinstance(child, dict):
        child = parent[key] = {}
    return child


class PingStore:
    """Reads and writes the ``discord_ping`` block of whatever ``get_config()`` returns NOW.

    ``get_config()`` is called on every method call; the object keeps the two callables and
    nothing else, so it stays valid when the app swaps its config dict for a new one.
    ``save()`` is called once after every call that changed the config.
    """

    def __init__(self, get_config: Callable[[], dict], save: Callable[[], None]):
        self._get_config = get_config
        self._save = save

    # -- the three shared lists -------------------------------------------------------------

    def items(self, kind: str) -> list:
        _require(kind, LIST_KINDS, "list kind")
        return _items(_block(self._get_config()), kind)

    def set_items(self, kind: str, items) -> None:
        _require(kind, LIST_KINDS, "list kind")
        if not isinstance(items, (list, tuple)):
            raise ValueError("%s must be a list, not %r" % (kind, items))
        new = _clean_items(items)
        cfg = self._get_config()
        block = _block(cfg)
        if _in_force(block, kind, new, _items(block, kind)):
            return
        _open_block(cfg)[kind] = new
        self._save()

    # -- picks ------------------------------------------------------------------------------

    def picks(self, doctrine_id) -> Picks:
        return _picks(_block(self._get_config()), _doctrine_key(doctrine_id))

    def set_pick(self, doctrine_id, field: str, value: str) -> None:
        _require(field, PICK_FIELDS, "pick field")
        if not isinstance(value, str):
            raise ValueError("the %s pick must be text, not %r" % (field, value))
        new = _clean_text(value)
        if field == "doctrine":
            _require(new, DOCTRINE_MODES, "doctrine mode")
        key = _doctrine_key(doctrine_id)
        cfg = self._get_config()
        block = _block(cfg)
        if _in_force(_stored_picks(block, key), field, new, getattr(_picks(block, key), field)):
            return
        block = _open_block(cfg)
        if key is None:
            target = _open(block, "default_picks")
        else:
            target = _open(_open(_open(block, "doctrines"), key), "picks")
        target[field] = new
        self._save()

    # -- templates --------------------------------------------------------------------------

    def template(self, doctrine_id) -> "list | None":
        """Its own template, else the basic one, else ``None`` (= the built-in basic)."""
        block = _block(self._get_config())
        doc = _own_template(block, _doctrine_key(doctrine_id))
        if doc is None:
            doc = block.get("basic")
        return copy.deepcopy(doc) if isinstance(doc, list) else None

    def has_own_template(self, doctrine_id) -> bool:
        block = _block(self._get_config())
        return _own_template(block, _doctrine_key(doctrine_id)) is not None

    def set_template(self, doctrine_id, doc_json: list) -> None:
        """Store a doctrine's own template; with no doctrine, the basic template."""
        if not isinstance(doc_json, list):
            raise ValueError("a template must be a list, not %r" % (doc_json,))
        key = _doctrine_key(doctrine_id)
        cfg = self._get_config()
        block = _block(cfg)
        holder, slot = (block, "basic") if key is None else (_entry(block, key), "doc")
        if slot in holder and holder[slot] == doc_json:
            return
        target = _open_block(cfg)
        if key is not None:
            target = _open(_open(target, "doctrines"), key)
        target[slot] = copy.deepcopy(doc_json)
        self._save()

    def reset_template(self, doctrine_id) -> None:
        """Drop a doctrine's own template; with no doctrine, the basic template."""
        key = _doctrine_key(doctrine_id)
        cfg = self._get_config()
        block = _block(cfg)
        holder, slot = (block, "basic") if key is None else (_entry(block, key), "doc")
        if not isinstance(holder.get(slot), list):
            return
        _open_block(cfg)
        del holder[slot]            # a stored list was found, so holder is the live dict
        self._save()

    def make_basic(self, doc_json: list) -> None:
        self.set_template(None, doc_json)

    def inheriting_count(self, doctrine_ids: Iterable) -> int:
        """How many of these ids have no template of their own."""
        block = _block(self._get_config())
        return sum(1 for doctrine_id in doctrine_ids
                   if _own_template(block, _doctrine_key(doctrine_id)) is None)

    # -- the two texts and the When format --------------------------------------------------

    def text(self, key: str) -> str:
        _require(key, TEXT_DEFAULTS, "text key")
        return _text(_block(self._get_config()), key)

    def set_text(self, key: str, value: str) -> None:
        _require(key, TEXT_DEFAULTS, "text key")
        if not isinstance(value, str):
            raise ValueError("%s must be text, not %r" % (key, value))
        new = _clean_text(value)
        cfg = self._get_config()
        block = _block(cfg)
        if key in block:
            same = block[key] == new
        else:
            same = _text(block, key) == (new or TEXT_DEFAULTS[key])
        if same:
            return
        _open_block(cfg)[key] = new
        self._save()

    def when_format(self) -> str:
        return _when_format(_block(self._get_config()))

    def set_when_format(self, preset: str) -> None:
        _require(preset, WHEN_FORMATS, "When format")
        cfg = self._get_config()
        block = _block(cfg)
        if _in_force(block, "when_format", preset, _when_format(block)):
            return
        _open_block(cfg)["when_format"] = preset
        self._save()

    # -- doctrine lifecycle -----------------------------------------------------------------

    def copy_doctrine(self, src_id: str, dst_id: str) -> None:
        """Give ``dst_id`` a deep copy of ``src_id``'s entry (Duplicate); no source, no change."""
        src, dst = _doctrine_key(src_id), _doctrine_key(dst_id)
        cfg = self._get_config()
        doctrines = _as_dict(_block(cfg).get("doctrines"))
        source = doctrines.get(src) if src is not None else None
        if not isinstance(source, dict) or dst is None or dst == src:
            return
        if dst in doctrines and doctrines[dst] == source:
            return
        _open_block(cfg)
        doctrines[dst] = copy.deepcopy(source)      # a source was found: the live table
        self._save()

    def drop_doctrine(self, doctrine_id: str) -> None:
        """Forget a doctrine's entry (Delete); nothing stored, no change."""
        key = _doctrine_key(doctrine_id)
        cfg = self._get_config()
        doctrines = _as_dict(_block(cfg).get("doctrines"))
        if key is None or key not in doctrines:
            return
        _open_block(cfg)
        del doctrines[key]                          # the key was found: the live table
        self._save()
