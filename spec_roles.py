"""Custom Specialized Role categories - the model (pure: stdlib only, no Tk, no ``fc_gui``).

The Fleet tab's ten built-in Specialized Role sections stay as they are. A user (or a group
file) may ADD up to ``MAX_CUSTOM`` categories of their own, stored as a list under
``config[CONFIG_KEY]``::

    {"title": "Booshers", "type_ids": [101, 202], "tags": ["Support"], "threshold": 3}

A fleet member belongs to a category when its ship type id is one of ``type_ids``, or the
active doctrine tags its hull with one of ``tags``. ``threshold`` is an optional minimum
(``None`` = none), shown as ``n / threshold``.

This module is the one owner of that shape. It is read at Fleet-tab build time and by the
group-configuration item before any window exists, so every function is TOTAL: garbage in
gives a clean value, ``None`` or an empty list - never an exception, never a wrong-typed
value. A list read from the config always goes through :func:`normalize`.

The rules it mirrors:

* hull membership is whole-number set membership, as in ``_update_specialized_roles``
  (``stid in ALL_LOGISTICS`` ...) - and ``True`` is not type id 1;
* a tag matches exactly and case-sensitively, as in ``fleet_composer.pilot_matches``
  (``v in tag_index.get(ship_type_id, set())``); a stored tag keeps the bounds of a doctrine
  member tag (``fittings_store._share_member_tags``): stripped, and one over
  ``MAX_TAG_LEN`` characters is dropped whole - cut short it would be a tag nobody chose;
* two titles are the same when they are equal ignoring case (``str.lower``), the way fleet
  template names are compared (``fleet_template_store``);
* a title follows the project's visible-label rule (``group_bundle.clean_tag``): invisible
  characters are removed and a title with nothing to see is no title. Tags do NOT - a tag
  must equal a doctrine tag character for character.
"""

import unicodedata

CONFIG_KEY = "specialized_roles_custom"
MAX_CUSTOM = 12          # categories kept
TITLE_MAX = 28           # a longer title is clipped
MAX_TYPE_IDS = 200       # hulls per category
MAX_TAGS = 12            # tags per category
MAX_TAG_LEN = 40         # a longer tag is dropped whole (fittings_store.MAX_SHARE_TAG_LEN)
MAX_THRESHOLD = 500      # the largest minimum

_MAX_TYPE_ID = 2 ** 31 - 1      # EVE type ids are 32-bit; a larger number is no hull
_MAX_COUNT = 1_000_000          # count_text never prints a longer number than this

# Removed from display text - the same set as ``group_bundle.clean_text``: C0 controls (so
# the text is one line), DEL, and the surrogate code points (a surrogate in a str is always
# a lone one, and it cannot be written as UTF-8).
_REMOVED_CHARS = dict.fromkeys(list(range(32)) + [127] + list(range(0xD800, 0xE000)))

# Title rule (see _clean_title) - the same three values as ``group_bundle.clean_tag``.
# Removed: control, format (zero-width, bidi), line- and paragraph-separator characters.
# Visible: a letter, number, punctuation mark or symbol ...
_TITLE_REMOVED_CATEGORIES = ("Cc", "Cf", "Zl", "Zp")
_TITLE_VISIBLE_CLASSES = "LNPS"
# ... other than these, which Unicode files as letters / symbols but which draw as nothing:
# Hangul fillers (U+3164, U+115F, U+1160, U+FFA0) and the blank braille pattern (U+2800).
_BLANK_LOOKING = frozenset(map(chr, (0x3164, 0x115F, 0x1160, 0xFFA0, 0x2800)))
_TITLE_SCAN = 1000       # characters of a raw title looked at before the clip to TITLE_MAX


def _clean_text(value) -> str:
    """``value`` as one line of display text, stripped. Anything that is not text is blank."""
    if not isinstance(value, str):
        return ""
    return value.translate(_REMOVED_CHARS).strip()


def _clean_title(value) -> str:
    """``value`` as a section title: the visible-label rule of ``group_bundle.clean_tag``,
    with ``TITLE_MAX`` in place of its length limit.

    A title is drawn as a section header and can come from someone else's file, so beyond
    :func:`_clean_text` it loses every control, format (zero-width, bidi override) and
    line / paragraph separator character BEFORE the clip, and a title with nothing visible
    left - only combining marks, or only blank-looking letters - is no title at all (``""``).
    Cleaning a clean title changes nothing."""
    text = _clean_text(value)[:_TITLE_SCAN]
    text = "".join(ch for ch in text
                   if unicodedata.category(ch) not in _TITLE_REMOVED_CATEGORIES)
    # Strip again (the removal may have uncovered a blank), clip, and strip the clip's end.
    text = text.strip()[:TITLE_MAX].rstrip()
    if not any(unicodedata.category(ch)[0] in _TITLE_VISIBLE_CLASSES
               and ch not in _BLANK_LOOKING for ch in text):
        return ""
    return text


def _clean_type_ids(raw) -> list:
    """The first ``MAX_TYPE_IDS`` different type ids of ``raw``, sorted.

    A type id is a positive ``int`` - not a bool, not a float, not digits in a string.
    ``raw`` must be a list or a tuple (a stray string is not a list of ids)."""
    if not isinstance(raw, (list, tuple)):
        return []
    seen = set()
    for value in raw:
        if type(value) is int and 0 < value <= _MAX_TYPE_ID and value not in seen:
            seen.add(value)
            if len(seen) >= MAX_TYPE_IDS:
                break
    return sorted(seen)


def _clean_tags(raw) -> list:
    """The first ``MAX_TAGS`` different clean tags of ``raw``, in their own order.

    Text only, cleaned and stripped; a blank one, one over ``MAX_TAG_LEN`` characters and a
    repeat (exact - ``Logi`` and ``logi`` are two tags) are left out. ``raw`` must be a list
    or a tuple (a stray string would otherwise become one tag per character)."""
    if not isinstance(raw, (list, tuple)):
        return []
    tags = []
    for value in raw:
        tag = _clean_text(value)
        if not tag or len(tag) > MAX_TAG_LEN or tag in tags:
            continue
        tags.append(tag)
        if len(tags) >= MAX_TAGS:
            break
    return tags


def _clean_threshold(value):
    """``value`` when it is an ``int`` (not a bool) from 1 to ``MAX_THRESHOLD``, else None."""
    if type(value) is int and 1 <= value <= MAX_THRESHOLD:
        return value
    return None


def _count(n) -> int:
    """``n`` as a head count: a positive ``int`` (not a bool), else 0."""
    return min(n, _MAX_COUNT) if type(n) is int and n > 0 else 0


def title_key(title) -> str:
    """The key two category titles are compared by: the cleaned title, lower-cased.

    ``normalize`` drops a later category whose key equals an earlier one; anything else
    that asks "is this title taken?" (the editor, the group-file merge) must ask with this
    same key. Anything that is not text has the key ``""``.

    It takes a TITLE, never a key: lower-casing can lengthen a string, so a key fed back in
    may clip differently."""
    try:
        return _clean_title(title).lower()
    except Exception:
        return ""


def clean_category(raw):
    """One untrusted category as ``{title, type_ids, tags, threshold}``, or ``None``.

    * ``title`` - text, one line, stripped, invisible characters removed, clipped to
      ``TITLE_MAX``; blank or nothing visible => ``None``.
    * ``type_ids`` - the first ``MAX_TYPE_IDS`` different positive ints, sorted.
    * ``tags`` - the first ``MAX_TAGS`` different clean tags of at most ``MAX_TAG_LEN``
      characters, in their own order.
    * ``threshold`` - an int from 1 to ``MAX_THRESHOLD``; anything else is ``None``.

    A category with no type id and no tag can match nobody and is ``None``. The result is a
    NEW dict with exactly those four keys and new lists; ``type_ids`` and ``tags`` may be
    given as a list or a tuple. Total: never raises, never edits ``raw``; cleaning a clean
    category changes nothing."""
    try:
        if not isinstance(raw, dict):
            return None
        title = _clean_title(raw.get("title"))
        if not title:
            return None
        type_ids = _clean_type_ids(raw.get("type_ids"))
        tags = _clean_tags(raw.get("tags"))
        if not type_ids and not tags:
            return None
        return {"title": title, "type_ids": type_ids, "tags": tags,
                "threshold": _clean_threshold(raw.get("threshold"))}
    except Exception:
        return None


def normalize(raw) -> list:
    """The stored list as clean categories: what ``config[CONFIG_KEY]`` MEANS.

    Not a list => ``[]``. Each entry goes through :func:`clean_category`; one it refuses is
    left out, and so is a later entry whose title equals an earlier one ignoring case
    (:func:`title_key`). At most ``MAX_CUSTOM`` are kept - the first ones; a refused entry
    does not use up a place. Total: never raises."""
    out = []
    if not isinstance(raw, list):
        return out
    seen = set()
    try:
        for entry in raw:
            if len(out) >= MAX_CUSTOM:
                break
            category = clean_category(entry)
            if category is None:
                continue
            key = title_key(category["title"])
            if key in seen:
                continue
            seen.add(key)
            out.append(category)
    except Exception:
        pass
    return out


def _hull_matches(pilot, type_id_of, type_ids) -> bool:
    try:
        value = type_id_of(pilot)
        return type(value) is int and value in type_ids
    except Exception:
        return False


def _tag_matches(pilot, tags_of, tags) -> bool:
    try:
        held = tags_of(pilot)
        if held is None or isinstance(held, (str, bytes)):
            return False            # a bare string is not a collection of tags
        return any(isinstance(tag, str) and tag in tags for tag in held)
    except Exception:
        return False


def members(category, pilots, *, type_id_of, tags_of) -> list:
    """The ``pilots`` that belong to ``category``, in their own order.

    A pilot belongs when ``type_id_of(pilot)`` is one of the category's ``type_ids``, or
    ``tags_of(pilot)`` (any collection of tags, or ``None``) holds one of its ``tags`` -
    exact, case-sensitive. Each pilot is listed once, and it is the object handed in, not a
    copy. The two accessors are independent: one that raises, or answers with the wrong
    type (a bool or text for a type id, a bare string for the tags), is no match for THAT
    rule and that pilot only. ``category`` is read by the rules of :func:`clean_category`
    whether or not it was cleaned; its title plays no part.

    Total: never raises. Not a dict, nothing to match, or ``pilots`` not iterable => ``[]``.
    """
    out = []
    try:
        if not isinstance(category, dict):
            return out
        type_ids = frozenset(_clean_type_ids(category.get("type_ids")))
        tags = frozenset(_clean_tags(category.get("tags")))
        if not type_ids and not tags:
            return out
        for pilot in pilots:
            if ((type_ids and _hull_matches(pilot, type_id_of, type_ids))
                    or (tags and _tag_matches(pilot, tags_of, tags))):
                out.append(pilot)
    except Exception:
        pass
    return out


def count_text(n, threshold) -> str:
    """The section's count: ``"3"`` without a threshold, ``"3 / 5"`` with one.

    ``threshold`` counts only when it is a clean one (an int 1 to ``MAX_THRESHOLD``); ``n``
    that is not a positive int reads as 0. Total: never raises."""
    count, minimum = _count(n), _clean_threshold(threshold)
    return str(count) if minimum is None else f"{count} / {minimum}"


def met(n, threshold):
    """Whether the category has its minimum: ``None`` without a (clean) threshold, else
    ``n >= threshold``. ``n`` that is not a positive int reads as 0. Total: never raises."""
    minimum = _clean_threshold(threshold)
    if minimum is None:
        return None
    return _count(n) >= minimum
