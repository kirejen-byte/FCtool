"""The group-configuration file (``*.fctg``): envelope, limits, checksum, read / write.

A group file is one JSON document::

    {"format": "fctool-group-config", "format_version": 1, "app_version": "6.8.0",
     "bundle": {"id": "<uuid4 hex>", "revision": 3, "name": "...", "tag": "ABCD",
                "author": "", "notes": "", "created": "2026-10-08T18:00:00Z"},
     "categories": {"<category>": {"<item key>": <payload>}},
     "checksum": "sha256:<hex of the canonical categories JSON>"}

``bundle.tag`` is the group's short tag (at most ``MAX_TAG`` characters); the key is left out
of the file when the tag is blank. :func:`clean_tag` is the one owner of the tag rule - every
place that takes a tag, in this module or outside it, cleans it with that function.

This module owns the ENVELOPE only. It never looks inside an item payload: unknown categories
and item keys are kept as they are, and the item engine reports them.

The file is untrusted input. Reading either returns ``(Bundle, notes)`` or raises
:class:`BundleError`, whose ``code`` is one of ``not_a_bundle``, ``doctrine_file``, ``too_big``,
``too_deep``, ``newer_format``, ``bad_envelope`` and whose ``str()`` is the message to show.
Nothing else escapes a reader. ``notes`` lists what was tolerated, at most ``MAX_READ_NOTES``
entries. A note is one of exactly three kinds:

* ``"checksum"`` - the checksum is missing or different: the file was edited after it was
  exported. A warning, never a refusal. Always first when present.
* ``"dropped:<category>"`` - that category was not a dict and was left out.
* ``"more:<n>"`` - ``n`` further notes did not fit. Always last when present.

A consumer compares whole strings or uses ``note.partition(":")`` - never a position in the
list, and never a prefix test (a category may be NAMED "checksum" or "more").

The writers (:func:`dumps`, :func:`write_file`) raise the same :class:`BundleError` for a
bundle whose file a reader would refuse as ``too_deep`` or ``bad_envelope``.

Pure: stdlib and ``app_io`` only. No Tk, no config, no network.
"""

import hashlib
import json
import math
import os
import re
import unicodedata
import uuid
from dataclasses import dataclass

from app_io import atomic_write_json


FORMAT = "fctool-group-config"
FORMAT_VERSION = 1
EXTENSION = ".fctg"

MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_DEPTH = 16
MAX_NAME = 80
MAX_TAG = 10
MAX_READ_NOTES = 50      # reader notes returned for one file
MAX_NOTES_TEXT = 500     # the `notes` text field of the bundle header
_MAX_STAMP = 40          # `created` and `app_version`
_MAX_REVISION = 999_999_999

CATEGORIES = ("palette", "fleet", "intel", "jump", "map", "fittings", "settings")
CATEGORY_LABELS = {
    "palette": "Colour palette",
    "fleet": "Fleet management",
    "intel": "Intelligence",
    "jump": "Jump range",
    "map": "Map",
    "fittings": "Fittings",
    "settings": "Settings",
}

# User-facing text. ASCII on purpose: a caller may log it.
_MESSAGES = {
    "not_a_bundle": "This is not a group configuration file.",
    "doctrine_file": "This is a doctrine file - use the doctrine import on the Fittings tab.",
    "too_big": "This file is too large to be a group configuration file.",
    "too_deep": "This file is nested too deeply to be a group configuration file.",
    "newer_format": "This file was made by a newer FCTool - update FCTool to import it.",
    "bad_envelope": "This group configuration file is damaged and cannot be read.",
}
_UNREADABLE = "This file could not be read."

_BOM = chr(0xFEFF)       # spelled by code point: this source file stays pure ASCII
# Removed from display text: C0 controls, DEL, and the surrogate code points (a surrogate in
# a str is always a lone one, and it cannot be encoded as UTF-8).
_REMOVED_CHARS = dict.fromkeys(list(range(32)) + [127] + list(range(0xD800, 0xE000)))
_BUNDLE_ID = re.compile(r"[0-9a-f]{32}")
_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
_LETTER_OR_DIGIT = re.compile(r"[A-Za-z0-9]")
_NOT_VERSION_CHAR = re.compile(r"[^0-9A-Za-z.+-]+")
_MAX_SHOWN_VERSION = 20

# Tag rule (see clean_tag). Removed: control, format (zero-width, bidi), line- and
# paragraph-separator characters. Visible: a letter, number, punctuation mark or symbol ...
_TAG_REMOVED_CATEGORIES = ("Cc", "Cf", "Zl", "Zp")
_TAG_VISIBLE_CLASSES = "LNPS"
# ... other than these, which Unicode files as letters / symbols but which draw as nothing:
# Hangul fillers (U+3164, U+115F, U+1160, U+FFA0) and the blank braille pattern (U+2800).
_BLANK_LOOKING = frozenset(map(chr, (0x3164, 0x115F, 0x1160, 0xFFA0, 0x2800)))
_TAG_SCAN = 1000         # characters of a raw tag looked at before the clip to MAX_TAG

# A file name whose part before the first dot is one of these names a DEVICE on Windows.
_DEVICE_NAMES = frozenset(["CON", "PRN", "AUX", "NUL"]
                          + [f"COM{n}" for n in range(1, 10)]
                          + [f"LPT{n}" for n in range(1, 10)])


class BundleError(Exception):
    """A group file was refused. ``code`` says why; ``str(e)`` is the user message."""

    def __init__(self, code, message=""):
        self.code = code
        super().__init__(message or _MESSAGES.get(code) or _MESSAGES["not_a_bundle"])


@dataclass(frozen=True)
class BundleMeta:
    id: str
    revision: int
    name: str
    author: str
    notes: str
    created: str
    app_version: str
    tag: str = ""             # the group tag; last, so the seven-argument form still builds


@dataclass
class Bundle:
    meta: BundleMeta
    categories: dict          # categories[category][item_key] = payload


def new_bundle_id() -> str:
    return uuid.uuid4().hex


def clean_text(value, max_len=MAX_NAME) -> str:
    """``value`` as display text: control characters and lone surrogates removed, stripped,
    clipped to ``max_len``.

    Total - never raises. ``None`` is blank (a JSON ``null`` must not become the word "None").
    The clip is followed by a right-strip, so the result is always stripped and cleaning it
    again changes nothing. The result always encodes as UTF-8.
    """
    if value is None:
        return ""
    try:
        text = str(value)
    except Exception:
        return ""
    try:
        limit = max(0, int(max_len))
    except (TypeError, ValueError, OverflowError):
        limit = MAX_NAME
    return text.translate(_REMOVED_CHARS).strip()[:limit].rstrip()


def _text_or_number(value):
    """``value`` when it is text or a plain number, else None.

    Header text shows what the file SAYS, never the Python repr of a list, dict or bool: a str
    passes, so does an int (not a bool) or a finite float, and anything else is blank.
    """
    if isinstance(value, str):
        return value
    if type(value) is int or (type(value) is float and math.isfinite(value)):
        return value
    return None


def clean_tag(value) -> str:
    """``value`` as a group tag - the ONE owner of the tag rule.

    The tag is drawn in the title bar as "by <tag>" and can come from someone else's file, so
    beyond :func:`clean_text` it loses every control, format (zero-width, bidi override) and
    line / paragraph separator character, and a tag with nothing visible left - only combining
    marks, or only blank-looking letters - is no tag at all. At most ``MAX_TAG`` characters.

    Total - never raises. Cleaning a clean tag changes nothing. Not text or a plain number
    (``None``, a bool, a list ...) is ``""``.
    """
    text = clean_text(_text_or_number(value), _TAG_SCAN)
    text = "".join(ch for ch in text
                   if unicodedata.category(ch) not in _TAG_REMOVED_CATEGORIES)
    text = clean_text(text, MAX_TAG)      # again: the removal may have uncovered whitespace
    if not any(unicodedata.category(ch)[0] in _TAG_VISIBLE_CLASSES and ch not in _BLANK_LOOKING
               for ch in text):
        return ""
    return text


def checksum(categories) -> str:
    canonical = json.dumps(categories, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _clean_revision(value) -> int:
    # bool is an int subclass; True is not a revision number. The upper bound keeps a
    # hand-made huge integer out of file names and arithmetic.
    return value if type(value) is int and 1 <= value <= _MAX_REVISION else 1


def _clean_meta(get) -> BundleMeta:
    """Build a clean BundleMeta from ``get(field)``. Coerces; never refuses."""
    bundle_id = get("id")
    if not (isinstance(bundle_id, str) and _BUNDLE_ID.fullmatch(bundle_id)):
        bundle_id = ""

    def text(field, max_len):
        return clean_text(_text_or_number(get(field)), max_len)

    return BundleMeta(
        id=bundle_id,
        revision=_clean_revision(get("revision")),
        name=text("name", MAX_NAME),
        author=text("author", MAX_NAME),
        notes=text("notes", MAX_NOTES_TEXT),
        created=text("created", _MAX_STAMP),
        app_version=text("app_version", _MAX_STAMP),
        tag=clean_tag(get("tag")),
    )


def to_envelope(bundle) -> dict:
    """The file's dict, keys in file order. Meta is written clean, so a saved file re-reads
    to the same meta; ``categories`` is referenced, not copied."""
    meta = _clean_meta(lambda field: getattr(bundle.meta, field, None))
    header = {"id": meta.id, "revision": meta.revision, "name": meta.name}
    if meta.tag:
        header["tag"] = meta.tag            # right after `name`; no key at all when blank
    header.update(author=meta.author, notes=meta.notes, created=meta.created)
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "app_version": meta.app_version,
        "bundle": header,
        "categories": bundle.categories,
        "checksum": checksum(bundle.categories),
    }


def _too_deep(data) -> bool:
    """True when containers nest deeper than MAX_DEPTH.

    Iterative (it must not itself recurse), and it holds one iterator per level rather than
    every pending child, so its memory is bounded by the depth limit and not by the file.
    """
    stack = [iter((data,))]
    while stack:
        for node in stack[-1]:
            if isinstance(node, dict):
                children = node.values()
            elif isinstance(node, (list, tuple)):
                children = node
            else:
                continue
            if len(stack) > MAX_DEPTH:
                return True
            stack.append(iter(children))
            break
        else:
            stack.pop()
    return False


def _newer_format_error(data):
    """The ``newer_format`` refusal when ``data`` is a file of this format from a newer
    build, else None. Reads the top level only."""
    if not isinstance(data, dict):
        return None
    file_format = data.get("format")
    if not (isinstance(file_format, str) and file_format == FORMAT):
        return None
    version = data.get("format_version")
    if type(version) is not int or version <= FORMAT_VERSION:
        return None
    # The version shown is the FILE's word inside OUR sentence: version characters only (so
    # ASCII, and no room for a sentence of the file's own), and only a few of them.
    made_by = clean_text(_text_or_number(data.get("app_version")), _MAX_STAMP)
    made_by = _NOT_VERSION_CHAR.sub("", made_by)[:_MAX_SHOWN_VERSION]
    if not _LETTER_OR_DIGIT.search(made_by):
        return BundleError("newer_format")
    return BundleError(
        "newer_format",
        f"This file was made by a newer FCTool ({made_by}) - update FCTool to import it.")


def from_envelope(data):
    """Parsed JSON -> ``(Bundle, notes)``, or :class:`BundleError`.

    The input is not modified. The returned ``categories`` dict and each category dict in it
    are new objects; the item payloads below them are shared with ``data``.
    """
    # Judged before anything below the top level, the depth limit included: a newer format
    # may have reshaped the whole body, and "update FCTool" is the answer that helps.
    newer = _newer_format_error(data)
    if newer is not None:
        raise newer
    if _too_deep(data):
        raise BundleError("too_deep")
    if not isinstance(data, dict):
        raise BundleError("not_a_bundle")
    if "format" not in data and "doctrines" in data and "fits" in data:
        raise BundleError("doctrine_file")
    file_format = data.get("format")
    if not (isinstance(file_format, str) and file_format == FORMAT):
        raise BundleError("not_a_bundle")

    version = data.get("format_version")
    if type(version) is not int or version < 1:
        raise BundleError("bad_envelope")

    raw_categories = data.get("categories")
    if not isinstance(raw_categories, dict):
        raise BundleError("bad_envelope")

    # The checksum covers `categories` exactly as the file carries it.
    try:
        intact = data.get("checksum") == checksum(raw_categories)
    except (TypeError, ValueError, RecursionError):
        intact = False
    notes = [] if intact else ["checksum"]

    # At most MAX_READ_NOTES notes: a file with a million junk categories must not turn into
    # a million strings. The checksum note is already in, so it is never the one squeezed out.
    room = max(1, MAX_READ_NOTES - len(notes))
    dropped = []
    overflow = 0
    categories = {}
    for category, items in raw_categories.items():
        if isinstance(items, dict):
            categories[category] = dict(items)
        elif len(dropped) < room:
            dropped.append(f"dropped:{clean_text(category)}")
        else:
            overflow += 1
    if overflow:
        dropped[-1] = f"more:{overflow + 1}"    # the note it replaces is one of "the rest"
    notes.extend(dropped)

    raw_meta = data.get("bundle")
    if not isinstance(raw_meta, dict):
        raw_meta = {}
    meta = _clean_meta(lambda field: data.get(field) if field == "app_version"
                       else raw_meta.get(field))
    return Bundle(meta=meta, categories=categories), notes


def _envelope_to_write(bundle) -> dict:
    """:func:`to_envelope`, or the :class:`BundleError` a reader would raise for that file.

    A writer never produces a file its own reader refuses. Judged before the envelope is
    built, so the checksum never has to walk a payload that is too deep for it.
    """
    categories = getattr(bundle, "categories", None)
    if not isinstance(categories, dict):
        raise BundleError("bad_envelope")
    # In the file `categories` sits one level down, inside the envelope's own dict.
    if _too_deep({"categories": categories}):
        raise BundleError("too_deep")
    return to_envelope(bundle)


def dumps(bundle) -> str:
    """The file's text. Raises :class:`BundleError` (``bad_envelope``, ``too_deep``) for a
    bundle whose file a reader would refuse."""
    return json.dumps(_envelope_to_write(bundle), indent=1, ensure_ascii=True)


def _parse(source):
    """Text (or the file's bytes) -> parsed JSON, or :class:`BundleError`."""
    if isinstance(source, (bytes, bytearray)):
        try:
            source = bytes(source).decode("utf-8-sig")
        except UnicodeDecodeError:
            raise BundleError("not_a_bundle") from None
    if not isinstance(source, str):
        raise BundleError("not_a_bundle")
    if source.startswith(_BOM):
        source = source[1:]
    try:
        return json.loads(source)
    except RecursionError:
        raise BundleError("too_deep") from None
    except ValueError:
        # JSONDecodeError, and the plain ValueError int() raises for an oversized literal.
        raise BundleError("not_a_bundle") from None


def loads(text):
    try:
        return from_envelope(_parse(text))
    except MemoryError:
        # A file inside the byte limit can still parse into more objects than fit.
        raise BundleError("too_big") from None


def read_file(path):
    """Read a group file. Only :class:`BundleError` escapes - an unreadable or missing file is
    ``not_a_bundle`` with its own message, so a caller needs one ``except``.

    ``path`` is a path (``str``, ``bytes`` or ``os.PathLike``) and nothing else: ``open()``
    would take an int as a file DESCRIPTOR, read it and close it under its owner.
    """
    if not isinstance(path, (str, bytes, os.PathLike)):
        raise BundleError("not_a_bundle", _UNREADABLE)      # before anything is touched
    limit = MAX_FILE_BYTES
    try:
        size = os.path.getsize(path)
    except (OSError, TypeError, ValueError):
        raise BundleError("not_a_bundle", _UNREADABLE) from None
    if size > limit:
        raise BundleError("too_big")        # refused before the file is opened
    try:
        try:
            with open(path, "rb") as f:
                raw = f.read(limit + 1)
        except (OSError, TypeError, ValueError):
            raise BundleError("not_a_bundle", _UNREADABLE) from None
        if len(raw) > limit:                # it grew between the size check and the read
            raise BundleError("too_big")
        return from_envelope(_parse(raw))
    except MemoryError:
        raise BundleError("too_big") from None


def write_file(path, bundle) -> None:
    """Write atomically. Failures propagate; the target is then left as it was.

    Raises :class:`BundleError` (``bad_envelope``, ``too_deep``), before any file is made, for
    a bundle whose file a reader would refuse.
    """
    atomic_write_json(path, _envelope_to_write(bundle), indent=1)


def default_filename(meta) -> str:
    # No dash or dot at either end: a leading dot hides the file, Windows trims a trailing one.
    stem = _UNSAFE_FILENAME.sub("-", clean_text(getattr(meta, "name", ""))).strip("-.")
    if not _LETTER_OR_DIGIT.search(stem):
        stem = "group-config"               # blank, or punctuation only ("...", "_")
    elif stem.split(".", 1)[0].upper() in _DEVICE_NAMES:
        stem = "group-" + stem              # "con.pack" would name the console, not a file
    revision = _clean_revision(getattr(meta, "revision", 1))
    return f"{stem}-r{revision}{EXTENSION}"
