"""Group configuration: the four Intelligence items (spec section 6.3).

``intel.coalitions``       coalition definitions, merged by name
``intel.filter``           the intel filter block, with the coalitions it names
``intel.alerts``           alert mute, ping on/off and the ping sound (by catalogue key)
``intel.fusion_channels``  which channels feed Intelligence Fusion (the ``Intel`` tag)

Rules of this module
--------------------
* Every item writes ONLY the paths it declares (spec 6.7): the allowlist test is built from
  ``paths``. ``channel_store.save`` stays the only writer of the channels block; it rebinds
  that one top-level block (sibling keys inside it are kept) and touches nothing else - the
  legacy ``intel_channels`` block in particular is left exactly as it was.
* The filter block this module writes is ALWAYS complete and well-typed: every key of
  ``DEFAULT_FILTER``, ``location`` and ``parties`` as dicts, every list entry an
  ``{"id": int, "name": str}`` dict. The intel panel reads those shapes without checking
  and would stop the app at startup on anything else.
* A coalitions key that is absent (or is not a dict) is first seeded exactly as the app
  seeds it - ``intel_filter.build_default_coalitions()`` - and the boot flag
  ``coalitions_seeded`` is raised, so that the app still runs its own follow-up for a
  fresh seed. The seed is written only together with something that needs it. That
  follow-up adds one alliance to ``SEED_FOLLOWUP_COALITION``. The flag is withheld only
  when the file's OWN definition of that coalition was written (added or replaced), so that
  a group's definition is never altered afterwards; when the app's seed is what stays
  stored under that name (the file does not define it, defines it with the same members,
  or "keep mine" kept the seed's), the flag is raised as for any fresh seed.
* Two coalition names are ONE coalition when the app's coalition manager says so
  (``same_name``: equal once lower-cased). A file's coalition lands on the importer's
  coalition of that name and the importer's spelling of the key is kept; a filter is
  written with the importer's spelling too. Of two such names in one file the first counts.
* "Keep mine" keeps a DEFINITION. A stored value that is not a dict is nothing to keep: the
  file's definition replaces it under either choice.
* ``intel.filter`` works in a fixed order: (a) resolve the coalitions the filter names, on a
  copy; (b) refuse a filter that would match every party or every place, BEFORE anything
  is written; (c) write. A skipped filter therefore leaves the setup exactly as it was.
  The party test in (b) is the app's own - ``intel_filter.expand_parties`` yields no id -
  so a filter that names only coalitions without a single usable member is refused as
  well. The place test is its twin: ``anywhere`` off and not one usable system or region.
* A ``location`` / ``parties`` section that lists something but has no usable match-all
  flag (``anywhere`` / ``anyone``) is a RESTRICTION: the flag reads as off, with a problem
  line. It never comes to mean "anywhere" / "anyone" - also when every listed entry is
  unusable: the guard in (b) then skips the filter.
* ``export_notes(state, ctx=None)`` (coalitions and filter) lists what an EXPORT of this
  setup leaves out; ``reconcile(payloads)`` settles a file that defines one coalition
  twice. Both are hooks the engine calls; neither writes anything.
* EXPORT mirrors what the filter panel effectively uses: ``config.json`` can hold a
  ``min_pilots`` / ``max_jumps`` outside the spinbox range, and the alert gate reads the
  spinbox, which holds the nearest bound. Such a whole number is exported as that bound,
  never as the standard value. IMPORT never clamps: a file's out-of-range number takes
  the standard value, with a problem line. In the same way a section the panel stored
  with its match-all flag OFF and nothing listed - which the app reads as "anywhere" /
  "anyone" - is exported with the flag on, and an export note says so; the import guards
  are unchanged, so a hand-made file in that shape is still skipped.
* Nothing here needs a character or the network (spec 9.1).
* ``validate`` builds a clean payload from scratch and never raises; ``apply`` cleans what
  it is given once more, so a payload that skipped validation still cannot write a
  wrong-typed value. A value that is not in the payload is never read or written.
* ``SOUND_KEYS`` and ``DEFAULT_FILTER`` MIRROR ``fc_gui`` (its alert-sound catalogue and the
  block its intel-filter migration seeds); so do the number limits (the filter panel's two
  spinboxes), ``same_name`` (the coalition manager's name check) and
  ``SEED_FOLLOWUP_COALITION``. ``fc_gui`` is not importable here (the purity rule below);
  ``tests/test_group_items_intel.py`` holds the mirrors equal.

Pure: stdlib, ``group_bundle``, ``group_items``, ``channel_store`` and ``intel_filter``
only. No Tk, no ``ui_theme``, no ``fc_gui`` - this module runs in the boot hook.
"""

import copy

import channel_store
import intel_filter
from group_bundle import MAX_NAME, clean_text
from group_items import (ABSENT, MAX_LIST, ConfigFieldsItem, Field, Item, ItemResult, Record,
                         set_path, value_text)

# -- mirrors (guarded by tests) -------------------------------------------------
SOUND_KEYS = ("sonar", "klaxon", "blip", "horn", "comms", "bell")   # fc_gui _ALERT_SOUNDS
DEFAULT_FILTER = {                              # fc_gui _migrate_intel_filter_config
    "combine": "AND",
    "location": {"anywhere": True, "systems": [], "regions": []},
    "parties": {"anyone": True, "alliances": [], "corporations": [], "coalitions": []},
    "min_pilots": 25,
    "max_jumps": 0,
    "capitals": {"alert": True, "bypass_filter": False},
    "security": {"highsec": True, "lowsec": True},
}
MIN_MIN_PILOTS = 1                              # fc_gui: the Min Pilots spinbox, 1-500
MAX_MIN_PILOTS = 500
MAX_MAX_JUMPS = 200                             # fc_gui: the Max Jumps spinbox, 0-200
SEED_FOLLOWUP_COALITION = "The Initiative."     # fc_gui _apply_triumvirate_resolution


def _fold(name) -> str:
    """A coalition name as the app's coalition manager compares it (fc_gui
    ``_cm_name_exists``): lower-cased - ``lower()``, not ``casefold()``."""
    return name.lower()


def same_name(a, b) -> bool:
    """Whether two coalition names are one name to the app's coalition manager."""
    return _fold(a) == _fold(b)


# -- limits ---------------------------------------------------------------------
MAX_COALITIONS = 200                            # spec section 5: 200 coalitions x 2,000 ids
MAX_COALITION_IDS = 2_000
MAX_COALITION_NAME = MAX_NAME
MAX_ENTITY_NAME = 100                           # an alliance, corporation, system or region
MAX_FILTER_LIST = MAX_LIST                      # spec section 5: 500 per list
MAX_CHANNELS = MAX_LIST                         # spec section 5: 500 channels
MAX_CHANNEL_NAME = 100
_MAX_ID = 2 ** 63 - 1                           # every EVE id fits a signed 64-bit integer
_ANY_LENGTH = 1_000_000
MAX_NOTES = 50                                  # export notes of one item; then "and <n> more"
_SHOWN_NAME = 40                                # characters of an over-long name a note shows

FLAG_SEEDED = "coalitions_seeded"               # boot flag (exact name)
EVERY_PARTY = "the filter would match every party"
NO_LOCATION = "the filter's location list has no usable entry"
COALITIONS_ID, FILTER_ID = "intel.coalitions", "intel.filter"

_COALITIONS = "coalitions"                      # the config key AND the payload key
_FILTER = "intel_filter"
_MEMBER_KEYS = ("alliances", "corporations")
_FILTER_ROW = "filter"                          # record key of the filter block
_DEF_ROW = "coalition:"                         # record key prefix of a named definition
_SEED_NOTE = "Coalitions: set up with FCTool's standard coalitions first"


# -- small helpers --------------------------------------------------------------

def _view(state) -> dict:
    """The config for reading; a missing or unreadable one reads as empty."""
    config = getattr(state, "config", None)
    return config if isinstance(config, dict) else {}


def _writable(state) -> dict:
    """The config FOR WRITING, created when the file is missing. Call it only once there is
    something to write: an apply that changes nothing must leave a missing config missing."""
    if state.config is None:
        state.config = {}
    if not isinstance(state.config, dict):
        raise TypeError("config is not readable")
    return state.config


def _count(n, one, many) -> str:
    return f"{n} {one if n == 1 else many}"


def _key_set(keys) -> set:
    if isinstance(keys, str):
        return {keys}
    try:
        return {key for key in keys if isinstance(key, str)}
    except TypeError:
        return set()


def _identical(a, b) -> bool:
    """Deep equality that also compares types: ``1`` is not ``True``, ``25`` is not ``25.0``."""
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_identical(a[key], b[key]) for key in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(map(_identical, a, b))
    return a == b


def _name(raw, max_len):
    """A clean name of 1..``max_len`` characters, or ``None``. A longer one is refused, never
    clipped: the clipped text would name something else."""
    if not isinstance(raw, str):
        return None
    text = clean_text(raw, _ANY_LENGTH)
    return text if 0 < len(text) <= max_len else None


def _entities(raw, cap):
    """``[{"id", "name"}, ...]`` from an untrusted list. -> ``(entries, dropped, extra)``.

    An entry needs a positive whole-number id (a bool, a float or text is not one); its name
    is cleaned and clipped, and blank when it is missing or not text. One entry per id - the
    first one counts. ``dropped`` counts entries without a usable id, ``extra`` those past
    ``cap``. Anything that is not a list reads as empty. Total.
    """
    entries, seen, dropped, extra = [], set(), 0, 0
    for entry in raw if isinstance(raw, list) else ():
        entity_id = entry.get("id") if isinstance(entry, dict) else None
        if type(entity_id) is not int or not 0 < entity_id <= _MAX_ID:
            dropped += 1
            continue
        if entity_id in seen:
            continue
        if len(entries) >= cap:
            extra += 1
            continue
        seen.add(entity_id)
        name = entry.get("name")
        entries.append({"id": entity_id,
                        "name": clean_text(name, MAX_ENTITY_NAME) if isinstance(name, str) else ""})
    return entries, dropped, extra


# -- coalitions (shared by intel.coalitions and intel.filter) -------------------

def _members(raw):
    """A coalition dict -> ``(definition, members without a usable id, members over the
    limit)``."""
    definition, lost, over = {}, 0, 0
    for key in _MEMBER_KEYS:
        definition[key], dropped, extra = _entities(raw.get(key), MAX_COALITION_IDS)
        lost += dropped
        over += extra
    return definition, lost, over


def _twin(coalitions, name):
    """The key ``name`` goes by in ``coalitions``: its exact spelling when that is a key,
    else the first key that is the same name to the app (``same_name``), else ``None``."""
    if name in coalitions:
        return name
    folded = _fold(name)
    for key in coalitions:
        if isinstance(key, str) and _fold(key) == folded:
            return key
    return None


def clean_coalition(raw):
    """``{"alliances": [...], "corporations": [...]}`` from an untrusted coalition, or
    ``None`` when ``raw`` is not a dict.

    Each list holds at most 2,000 ``{"id": positive int, "name": text of at most 100
    characters}`` entries; an entry with a bad id is dropped, a list that is missing or of
    the wrong type is empty, and every other key is left out. Total; ``raw`` is not modified.
    """
    return _members(raw)[0] if isinstance(raw, dict) else None


def _long_name(raw):
    """``raw`` as a note shows it when it is a coalition name over the limit (its first 40
    characters and ``...``), else ``None``."""
    if not isinstance(raw, str):
        return None
    text = clean_text(raw, _ANY_LENGTH)
    return f"{text[:_SHOWN_NAME]}..." if len(text) > MAX_COALITION_NAME else None


def _capped(lines) -> list:
    """At most ``MAX_NOTES`` lines; what is over becomes one last ``"and <n> more"``."""
    lines = list(lines)
    if len(lines) <= MAX_NOTES:
        return lines
    return lines[:MAX_NOTES] + [f"and {len(lines) - MAX_NOTES} more"]


def _clean_coalitions(raw, label, fold=True, left=None):
    """``{name: definition}`` from an untrusted dict. -> ``(coalitions, problems)``. Total.

    Names are cleaned and 1-80 characters. ``North`` and ``north`` are ONE name, as they
    are to the app's coalition manager (``same_name``): the first one counts and keeps its
    spelling. A definition must be a dict that carries at least one of its two lists. At
    most ``MAX_COALITIONS`` survive.

    ``fold=False`` is for reading THIS setup's own keys the way the app's matcher reads
    them - exactly: ``North`` and ``north`` are then two keys, and only an exact repeat
    (two keys that clean to one name) is dropped. ``left``, when given a list, receives
    what an export note names: ``("long", shown name)`` for a name over the limit and
    ``("twin", name, the first name of that spelling)`` for a repeat.
    """
    if not isinstance(raw, dict):
        return {}, [f"{label}: not a set of coalitions"]
    key_of = _fold if fold else str
    clean, taken = {}, {}                           # taken: compare key -> the name kept
    bad_names = bad_definitions = repeats = extra = lost = over = 0
    for key, value in raw.items():
        name = _name(key, MAX_COALITION_NAME)
        if name is None:
            bad_names += 1
            if left is not None and _long_name(key) is not None:
                left.append(("long", _long_name(key)))
        elif key_of(name) in taken:
            repeats += 1
            if left is not None:
                left.append(("twin", name, taken[key_of(name)]))
        elif not isinstance(value, dict) or not any(part in value for part in _MEMBER_KEYS):
            bad_definitions += 1
        elif len(clean) >= MAX_COALITIONS:
            extra += 1
        else:
            taken[key_of(name)] = name
            clean[name], dropped, past = _members(value)
            lost += dropped
            over += past
    problems = []
    if bad_names:
        problems.append(_count(bad_names, "coalition", "coalitions")
                        + f" without a usable name (1-{MAX_COALITION_NAME} characters) left out")
    if bad_definitions:
        problems.append(_count(bad_definitions, "coalition", "coalitions")
                        + " without a usable member list left out")
    if repeats:
        problems.append(_count(repeats, "repeated coalition", "repeated coalitions")
                        + " left out (the first one counts; capitals do not make a new name)")
    if extra:
        problems.append(f"only the first {MAX_COALITIONS} coalitions are kept ({extra} left out)")
    if lost:
        problems.append(_count(lost, "coalition member", "coalition members")
                        + " without a usable id left out")
    if over:
        problems.append(_count(over, "coalition member", "coalition members")
                        + f" over the limit of {MAX_COALITION_IDS} left out")
    return clean, [f"{label}: {line}" for line in problems]


def _working_coalitions(config):
    """The coalitions to merge into, as a copy. -> ``(coalitions, seeded)``.

    ``seeded`` is True when the setup has no coalitions dict: the copy is then the app's own
    seed, and whoever writes it raises the ``coalitions_seeded`` flag.
    """
    stored = config.get(_COALITIONS)
    if isinstance(stored, dict):
        return copy.deepcopy(stored), False
    return intel_filter.build_default_coalitions(), True


def _member_key(definition):
    return tuple(sorted((entry["id"], entry["name"]) for entry in definition[key])
                 for key in _MEMBER_KEYS)


def _same_definition(mine, theirs) -> bool:
    """Same members, whatever their order. ``mine`` is this setup's (untrusted) value."""
    clean = clean_coalition(mine)
    return clean is not None and _member_key(clean) == _member_key(theirs)


def _merge_coalition(working, name, definition, conflict):
    """Land one definition in ``working``. -> ``(outcome, key)``.

    ``outcome`` is ``added`` | ``unchanged`` | ``updated`` | ``skipped`` (a differing
    definition the importer keeps); ``key`` is the name the coalition goes by in
    ``working`` - the importer's spelling when the importer has that coalition. "Keep mine"
    keeps a definition only: a stored value that is not a dict is replaced either way.
    """
    key = _twin(working, name)
    if key is None:
        working[name] = copy.deepcopy(definition)
        return "added", name
    if _same_definition(working[key], definition):
        return "unchanged", key
    if conflict == "mine" and isinstance(working[key], dict):
        return "skipped", key
    working[key] = copy.deepcopy(definition)        # in place: position and spelling are kept
    return "updated", key


def _wrote_followup(outcome, key) -> bool:
    """Whether this merge WROTE the file's definition of the coalition the app's seed
    follow-up extends. ``unchanged`` and ``skipped`` wrote nothing: what is stored under
    that name is then the app's own seed, and the follow-up belongs to it."""
    return outcome in ("added", "updated") and same_name(key, SEED_FOLLOWUP_COALITION)


def _record(result, key, name, outcome) -> None:
    getattr(result, outcome).append(key)
    if outcome == "updated":
        result.notes.append(f"Coalition '{name}': replaced by the group's definition")
    elif outcome == "skipped":
        result.notes.append(f"Coalition '{name}': yours differs and is kept")


def _members_text(definition) -> str:
    clean = clean_coalition(definition) or {key: [] for key in _MEMBER_KEYS}
    return (f"{_count(len(clean['alliances']), 'alliance', 'alliances')}, "
            f"{_count(len(clean['corporations']), 'corporation', 'corporations')}")


# -- intel.coalitions -----------------------------------------------------------

class _CoalitionsItem(Item):
    """``{name: {"alliances": [...], "corporations": [...]}}`` - merge only. A coalition the
    importer lacks is added; one with the same name (``same_name``) and other members is
    replaced ("theirs") or left alone ("mine"). Nothing is ever removed."""

    kind = "collection"
    files = ("config",)
    paths = (("config", (_COALITIONS,)),)
    conflicts = True

    def export(self, state, ctx):
        stored = _view(state).get(_COALITIONS)
        if not isinstance(stored, dict):
            return ABSENT
        return _clean_coalitions(stored, self.label)[0] or ABSENT

    def export_notes(self, state, ctx=None) -> list:
        """Lines for the builder: the coalitions of this setup that ``export`` leaves out
        because of their NAME - a key that is the same name as an earlier one to the app's
        manager (an old config can hold such twins; another setup cannot), and a name over
        80 characters. Never raises; at most ``MAX_NOTES`` lines and ``"and <n> more"``."""
        try:
            stored = _view(state).get(_COALITIONS)
            if not isinstance(stored, dict):
                return []
            left = []
            _clean_coalitions(stored, self.label, left=left)
            lines = []
            for kind, name, *first in left:
                if kind == "twin":
                    lines.append(f"{self.label}: '{name}' is left out - FCTool reads it as "
                                 f"the same name as '{first[0]}'")
                else:
                    lines.append(f"{self.label}: '{name}' has a name over "
                                 f"{MAX_COALITION_NAME} characters and is left out")
            return _capped(lines)
        except Exception:
            return []

    def validate(self, payload, ctx):
        clean, problems = _clean_coalitions(payload, self.label)
        return (clean or ABSENT), problems

    @staticmethod
    def _rows(payload):
        if not isinstance(payload, dict):
            return []
        return [(name, definition) for name, definition in payload.items()
                if isinstance(name, str) and isinstance(definition, dict)]

    def records(self, payload) -> list:
        return [Record(name, name, _members_text(definition))
                for name, definition in self._rows(payload)]

    def summary(self, payload) -> str:
        return _count(len(self._rows(payload)), "coalition", "coalitions")

    def lines(self, payload) -> list:
        return [(name, _members_text(definition)) for name, definition in self._rows(payload)]

    def apply(self, state, payload, mode, conflict, ctx) -> ItemResult:
        result = ItemResult(self.id)
        incoming, _ = _clean_coalitions(payload, self.label)
        if not incoming:
            return result
        working, seeded = _working_coalitions(_view(state))
        followup_is_theirs = False
        for name, definition in incoming.items():
            outcome, key = _merge_coalition(working, name, definition, conflict)
            _record(result, name, key, outcome)
            followup_is_theirs = followup_is_theirs or _wrote_followup(outcome, key)
        if seeded or result.added or result.updated:
            set_path(_writable(state), (_COALITIONS,), working)
        if seeded:
            if not followup_is_theirs:
                result.flags[FLAG_SEEDED] = True
            result.notes.append(_SEED_NOTE)
            result.status = "applied"           # the seed alone is a change
        return result.settle()


# -- intel.filter ---------------------------------------------------------------

def _clean_filter(raw, label, clamp=False):
    """A complete filter block from an untrusted one. -> ``(block | None, problems)``. Total.

    ``None`` when ``raw`` is not a dict or holds none of the filter's keys - there is no
    filter to share. Otherwise EVERY key of ``DEFAULT_FILTER`` is in the block: a missing one
    silently takes its default, a wrong-typed or out-of-range one takes its default with a
    problem line. One exception, so that a list can never come to mean "everything": a
    match-all flag (``anywhere`` / ``anyone``) that is missing or not a bool BESIDE a
    non-empty list reads as off, with a problem line. It is the RAW list that counts -
    something was listed, even when not one entry of it survives the cleaning (the app
    itself reads a text id, for one). The block shares nothing with ``raw`` or with
    ``DEFAULT_FILTER``.

    ``clamp`` is for EXPORT only - reading this setup's own block the way the app uses it:
    a whole number outside its range becomes the nearest bound, without a problem line,
    because that is the number the filter panel's spinbox (and so the alert gate) holds for
    it; and the coalition names are de-duplicated by their EXACT spelling, because the
    app's matcher looks a name up as it is written (``North`` and ``north`` are two keys of
    this setup until ``_FilterItem._stored`` has seen which of them is one). A file's
    number is never clamped and a file's names are one name per manager name.
    """
    if not isinstance(raw, dict) or not any(key in raw for key in DEFAULT_FILTER):
        return None, [f"{label}: no filter in it"]
    problems = []

    def odd(where, reason="is not usable; the standard value is used"):
        problems.append(f"{label} - {where}: {reason}")

    def section(key, where):
        if key not in raw:
            return {}
        if isinstance(raw[key], dict):
            return raw[key]
        odd(where)
        return {}

    def flag(source, part, key, where):
        if key in source:
            if type(source[key]) is bool:
                return source[key]
            odd(where)
        return DEFAULT_FILTER[part][key]

    def match_all(source, part, title, key, list_keys, where):
        """The section's match-all flag. ``list_keys`` name the section's lists in ``raw``."""
        if type(source.get(key)) is bool:
            return source[key]
        if any(isinstance(source.get(name), list) and source[name] for name in list_keys):
            # Something is listed, usable or not: a restriction.
            odd(title, f"'{key}' was {'not a yes or no' if key in source else 'missing'}"
                       " - read as a restricted list")
            return False
        if key in source:
            odd(where)
        return DEFAULT_FILTER[part][key]

    def whole(key, lo, hi, where):
        if key in raw:
            if type(raw[key]) is int:           # no bool, no float
                if lo <= raw[key] <= hi:
                    return raw[key]
                if clamp:
                    return max(lo, min(hi, raw[key]))
            odd(where)
        return DEFAULT_FILTER[key]

    def entities(source, key, where):
        if key not in source:
            return []
        if not isinstance(source[key], list):
            odd(where, "is not a list; it is left empty")
            return []
        entries, dropped, extra = _entities(source[key], MAX_FILTER_LIST)
        if dropped:
            odd(where, _count(dropped, "entry", "entries") + " without a usable id left out")
        if extra:
            odd(where, f"only the first {MAX_FILTER_LIST} are kept ({extra} left out)")
        return entries

    def names(source, key, where):
        if key not in source:
            return []
        if not isinstance(source[key], list):
            odd(where, "is not a list; it is left empty")
            return []
        key_of = str if clamp else _fold        # this setup's own names: exact (see above)
        kept, seen, bad, extra = [], set(), 0, 0
        for entry in source[key]:
            name = _name(entry, MAX_COALITION_NAME)
            if name is None:
                bad += 1
            elif key_of(name) in seen:          # one coalition, named twice
                continue
            elif len(kept) >= MAX_COALITIONS:
                extra += 1
            else:
                seen.add(key_of(name))
                kept.append(name)
        if bad:
            odd(where, _count(bad, "entry that is not a name",
                              "entries that are not names") + " left out")
        if extra:
            odd(where, f"only the first {MAX_COALITIONS} are kept ({extra} left out)")
        return kept

    combine = DEFAULT_FILTER["combine"]
    if "combine" in raw:
        if isinstance(raw["combine"], str) and raw["combine"] in ("AND", "OR"):
            combine = str(raw["combine"])
        else:
            odd("Combine", "is neither AND nor OR; AND is used")
    location, parties = section("location", "Location"), section("parties", "Parties")
    capitals, security = section("capitals", "Capitals"), section("security", "Empire kills")
    systems = entities(location, "systems", "Systems")
    regions = entities(location, "regions", "Regions")
    alliances = entities(parties, "alliances", "Alliances")
    corporations = entities(parties, "corporations", "Corporations")
    coalitions = names(parties, "coalitions", "Coalitions")
    block = {                                   # the app's own key order
        "combine": combine,
        "location": {
            "anywhere": match_all(location, "location", "Location", "anywhere",
                                  ("systems", "regions"), "Anywhere"),
            "systems": systems,
            "regions": regions,
        },
        "parties": {
            "anyone": match_all(parties, "parties", "Parties", "anyone",
                                ("alliances", "corporations", "coalitions"), "Anyone"),
            "alliances": alliances,
            "corporations": corporations,
            "coalitions": coalitions,
        },
        "min_pilots": whole("min_pilots", MIN_MIN_PILOTS, MAX_MIN_PILOTS, "Minimum pilots"),
        "max_jumps": whole("max_jumps", 0, MAX_MAX_JUMPS, "Maximum jumps"),
        "capitals": {
            "alert": flag(capitals, "capitals", "alert", "Capital alerts"),
            "bypass_filter": flag(capitals, "capitals", "bypass_filter",
                                  "Capitals bypass the filter"),
        },
        "security": {
            "highsec": flag(security, "security", "highsec", "Highsec kills"),
            "lowsec": flag(security, "security", "lowsec", "Lowsec kills"),
        },
    }
    return block, problems


def _no_party(parties, coalitions) -> bool:
    """The guard's question, asked of any parties block: does the app find not one alliance
    or corporation id in it? Total: a block the app's own expansion cannot read (a coalition
    name that is a list, say) is not "no party" - nothing is decided on it."""
    try:
        return not any(intel_filter.expand_parties(parties, coalitions))
    except Exception:
        return False


def _entity_names(*lists) -> list:
    return [entry["name"] or str(entry["id"]) for entries in lists for entry in entries]


def _filter_lines(block) -> list:
    location, parties = block["location"], block["parties"]
    places = _entity_names(location["systems"], location["regions"])
    who = _entity_names(parties["alliances"], parties["corporations"]) + parties["coalitions"]
    return [
        ("Combine location and parties with", block["combine"]),
        ("Location", "anywhere" if location["anywhere"] else value_text(places)),
        ("Parties", "anyone" if parties["anyone"] else value_text(who)),
        ("Minimum pilots", value_text(block["min_pilots"])),
        ("Maximum jumps", value_text(block["max_jumps"])),
        ("Capital alerts", value_text(block["capitals"]["alert"])),
        ("Capitals bypass the filter", value_text(block["capitals"]["bypass_filter"])),
        ("Highsec kills", value_text(block["security"]["highsec"])),
        ("Lowsec kills", value_text(block["security"]["lowsec"])),
    ]


def _filter_summary(block) -> str:
    location, parties = block["location"], block["parties"]
    where = "anywhere" if location["anywhere"] else _count(
        len(location["systems"]) + len(location["regions"]), "place", "places")
    who = "anyone" if parties["anyone"] else _count(
        len(parties["alliances"]) + len(parties["corporations"]) + len(parties["coalitions"]),
        "party", "parties")
    return f"{where}, {who}, {block['min_pilots']}+ pilots"


class _FilterItem(Item):
    """``{"filter": {...the whole block...}, "coalitions": {name: definition}}``.

    The block replaces the importer's whole; ``coalitions`` carries the definition of every
    coalition the block names, so that the filter means the same thing on the other side.
    The conflict choice is about those definitions only. Rows: ``filter`` (required - the
    definitions are nothing without it) and one ``coalition:<name>`` per definition.
    """

    kind = "value"
    files = ("config",)
    paths = (("config", (_FILTER,)), ("config", (_COALITIONS,)))
    conflicts = True

    def _clean(self, payload):
        """-> ``(clean payload | ABSENT, problems)``. Total."""
        if not isinstance(payload, dict):
            return ABSENT, [f"{self.label}: not an intel filter"]
        block, problems = _clean_filter(payload.get(_FILTER_ROW), self.label)
        if block is None:
            return ABSENT, problems
        definitions = {}
        if _COALITIONS in payload:
            every, lines = _clean_coalitions(payload[_COALITIONS], self.label)
            problems.extend(lines)
            named = []
            for name in block["parties"]["coalitions"]:
                key = _twin(every, name)
                if key is not None:             # only what the filter names, and the
                    definitions[key] = every[key]   # filter in the definition's spelling
                named.append(name if key is None else key)
            block["parties"]["coalitions"] = named
        return {_FILTER_ROW: block, _COALITIONS: definitions}, problems

    def _stored(self, state):
        """This setup's filter FOR EXPORT: ``(block, definitions, notes)``, or ``None`` when
        there is no filter block. A stored number outside the panel's range travels as the
        number the panel effectively uses.

        The coalition names are resolved the way the app's matcher resolves them: a name
        counts when it is EXACTLY a key of this setup's coalitions (a spelling that is no
        key - left behind by a rename that only changed capitals, say - is dead to the app
        too). Of the names that do count, one per manager name travels - the first: another
        setup cannot hold ``North`` and ``north`` side by side. ``notes`` says what was left
        out: a name without a definition, the second of two such twins, a name over the
        limit. It also says what will READ differently on the other side: a name that means
        my second twin (importers get the first, the one the Coalitions item exports), and
        a match-all flag exported as on (``_widen``)."""
        config = _view(state)
        raw = config.get(_FILTER)
        block, _ = _clean_filter(raw, self.label, clamp=True)
        if block is None:
            return None
        parties = raw.get("parties")
        raw_names = parties.get("coalitions") if isinstance(parties, dict) else None
        notes = [f"{self.label}: coalition '{shown}' has a name over {MAX_COALITION_NAME} "
                 f"characters and is left out"
                 for shown in map(_long_name, raw_names if isinstance(raw_names, list) else ())
                 if shown is not None]
        local, _ = _clean_coalitions(config.get(_COALITIONS), self.label, fold=False)
        # What the Coalitions item exports: my FIRST key of each manager name.
        travels, _ = _clean_coalitions(config.get(_COALITIONS), self.label)
        first = {_fold(key): key for key in travels}
        definitions, taken = {}, {}                 # taken: manager name -> the name kept
        for name in block["parties"]["coalitions"]:
            if name not in local:
                notes.append(f"{self.label}: coalition '{name}' has no definition here "
                             f"and is left out")
            elif _fold(name) in taken:
                notes.append(f"{self.label}: coalition '{name}' is left out - FCTool reads "
                             f"it as the same name as '{taken[_fold(name)]}'")
            else:
                taken[_fold(name)] = name
                definitions[name] = local[name]
                twin = first.get(_fold(name))
                if (twin is not None and twin != name
                        and not _same_definition(travels[twin], local[name])):
                    # In one file the Coalitions entry wins (``reconcile``).
                    notes.append(f"{self.label}: coalition '{name}' shares its name with "
                                 f"'{twin}' in this setup - importers will get '{twin}'")
        block["parties"]["coalitions"] = list(definitions)
        notes.extend(self._widen(block, definitions, raw, config.get(_COALITIONS)))
        return block, definitions, notes

    def _widen(self, block, definitions, raw, stored_coalitions) -> list:
        """EXPORT only, like the number clamp: a section whose match-all flag is off while
        nothing is listed in it. The panel can store that ("Anywhere" / "Anyone" unticked,
        nothing added) and the app then matches every place / everybody - while an importer's
        guard would skip such a file. So the flag travels as ``True``, what the filter
        means, with one note per section. Changes ``block``; returns the notes.

        Never a wider filter than the exporter's own: a section that only the APP can read
        an entry of (a text id, say - the cleaner drops it) is a restriction to the app and
        stays off. For the parties both views are asked the guard's question
        (``intel_filter.expand_parties`` yields no id): the block as it travels and the
        block as it is stored."""
        notes = []
        location, parties = block["location"], block["parties"]
        raw_location, raw_parties = raw.get("location"), raw.get("parties")
        listed = isinstance(raw_location, dict) and any(
            isinstance(raw_location.get(key), list) and raw_location[key]
            for key in ("systems", "regions"))
        if not location["anywhere"] and not listed \
                and not (location["systems"] or location["regions"]):
            location["anywhere"] = True
            notes.append(f"{self.label}: Location has 'Anywhere' off but nothing listed - "
                         f"FCTool treats that as anywhere, and it is exported that way")
        if not parties["anyone"] and _no_party(parties, definitions) \
                and _no_party(raw_parties, stored_coalitions):
            parties["anyone"] = True
            notes.append(f"{self.label}: Parties has 'Anyone' off but nothing listed - "
                         f"FCTool treats that as anyone, and it is exported that way")
        return notes

    def export(self, state, ctx):
        stored = self._stored(state)
        if stored is None:
            return ABSENT
        return {_FILTER_ROW: stored[0], _COALITIONS: stored[1]}

    def export_notes(self, state, ctx=None) -> list:
        """Lines for the builder: the coalitions this setup's filter names that ``export``
        leaves out (see ``_stored``). Never raises; at most ``MAX_NOTES`` lines and
        ``"and <n> more"``."""
        try:
            stored = self._stored(state)
            return [] if stored is None else _capped(stored[2])
        except Exception:
            return []

    def validate(self, payload, ctx):
        return self._clean(payload)

    def records(self, payload) -> list:
        clean, _ = self._clean(payload)
        if clean is ABSENT:
            return []
        return [Record(_FILTER_ROW, "Filter", _filter_summary(clean[_FILTER_ROW]))] + [
            Record(_DEF_ROW + name, f"Coalition: {name}", _members_text(definition))
            for name, definition in clean[_COALITIONS].items()]

    def summary(self, payload) -> str:
        clean, _ = self._clean(payload)
        return "" if clean is ABSENT else _filter_summary(clean[_FILTER_ROW])

    def lines(self, payload) -> list:
        clean, _ = self._clean(payload)
        if clean is ABSENT:
            return []
        return _filter_lines(clean[_FILTER_ROW]) + [
            (f"Coalition: {name}", _members_text(definition))
            for name, definition in clean[_COALITIONS].items()]

    def subset(self, payload, keys):
        wanted = _key_set(keys)
        clean, _ = self._clean(payload)
        if clean is ABSENT or _FILTER_ROW not in wanted:
            return ABSENT
        return {_FILTER_ROW: clean[_FILTER_ROW],
                _COALITIONS: {name: definition for name, definition in clean[_COALITIONS].items()
                              if _DEF_ROW + name in wanted}}

    def apply(self, state, payload, mode, conflict, ctx) -> ItemResult:
        result = ItemResult(self.id)
        clean, _ = self._clean(payload)
        if clean is ABSENT:
            return result
        block, definitions = clean[_FILTER_ROW], clean[_COALITIONS]
        config = _view(state)
        parties = block["parties"]

        # (a) Resolve every coalition the filter names - on a copy: nothing is written yet.
        # A name resolves the way the app's manager compares names, and the filter is
        # written with the spelling this setup has.
        working, seeded = _working_coalitions(config)
        kept, followup_is_theirs = [], False
        for name in parties["coalitions"]:
            if name in definitions:
                outcome, key = _merge_coalition(working, name, definitions[name], conflict)
                _record(result, _DEF_ROW + name, key, outcome)
                followup_is_theirs = followup_is_theirs or _wrote_followup(outcome, key)
            else:
                key = _twin(working, name)
            if key is None or not isinstance(working[key], dict):
                result.warnings.append(
                    f"{self.label}: coalition '{name}' is not defined in the file or in your "
                    f"setup; it is left out of the filter")
            elif key not in kept:
                kept.append(key)
        parties["coalitions"] = kept

        # (b) The guards, before any write. The app expands the parties to ids and, with
        # "anyone" off and not one id, treats the filter as "no constraint": it would alert
        # on everybody. A coalition NAME without a usable member is no party. The same for
        # the location: "anywhere" off with no system and no region is "anywhere" to the app.
        refused = []
        location = block["location"]
        if not location["anywhere"] and not (location["systems"] or location["regions"]):
            refused.append(NO_LOCATION)
        alliance_ids, corp_ids = intel_filter.expand_parties(parties, working)
        if not parties["anyone"] and not (alliance_ids or corp_ids):
            refused.append(EVERY_PARTY)
        if refused:
            return ItemResult(self.id, status="skipped", skipped=[_FILTER_ROW],
                              warnings=result.warnings + refused)

        # (c) Write: the coalitions first (only when a kept name needs them), then the block.
        if result.added or result.updated or (seeded and kept):
            set_path(_writable(state), (_COALITIONS,), working)
            if seeded:
                if not followup_is_theirs:
                    result.flags[FLAG_SEEDED] = True
                result.notes.append(_SEED_NOTE)
                result.status = "applied"       # the seed alone is a change
        if _identical(config.get(_FILTER, ABSENT), block):
            result.unchanged.append(_FILTER_ROW)
        else:
            set_path(_writable(state), (_FILTER,), block)
            result.updated.append(_FILTER_ROW)
            result.notes.append("Filter: replaced by the group's")
        return result.settle()


# -- one file, two definitions of one coalition ---------------------------------

def reconcile(payloads) -> list:
    """The file-level pass over the clean payloads of ONE file (``{item id: payload}``).

    A file can define a coalition twice: in the Coalitions item and among the definitions
    the filter item carries for the names it uses. When the two differ (the same name to
    the app's manager, other members) the file would never settle - each item would
    overwrite the other's definition on every apply. The Coalitions entry is the one that
    counts: the filter item's own copy is taken out of its payload, with one line each,
    ``"<name>: defined twice in the file - the Coalitions entry is used"`` (the name as the
    Coalitions item spells it). The filter still NAMES the coalition; applied after the
    Coalitions item, it finds that definition in the setup.

    Changes ``payloads`` in place and returns the problem lines. Total: either id may be
    absent, anything that is not in a clean payload's shape is left exactly as it is, never
    raises.
    """
    try:
        coalitions, wrapped = payloads.get(COALITIONS_ID), payloads.get(FILTER_ID)
        if not isinstance(coalitions, dict) or not isinstance(wrapped, dict):
            return []
        embedded = wrapped.get(_COALITIONS)
        if not isinstance(embedded, dict):
            return []
        stay, lines = {}, []
        for name, definition in embedded.items():
            key = _twin(coalitions, name) if isinstance(name, str) else None
            theirs = clean_coalition(definition)
            if (key is None or theirs is None or clean_coalition(coalitions[key]) is None
                    or _same_definition(coalitions[key], theirs)):
                stay[name] = definition
            else:
                lines.append(f"{key}: defined twice in the file - the Coalitions entry is used")
        if lines:
            payloads[FILTER_ID] = {**wrapped, _COALITIONS: stay}
        return lines
    except Exception:
        return []


# -- intel.fusion_channels ------------------------------------------------------

class _FusionChannelsItem(Item):
    """``[channel name, ...]`` - the channels that feed Intelligence Fusion.

    merge: every named channel exists and carries the ``Intel`` tag; its other tags stay.
    replace: the same, and every OTHER channel loses the tag - the channel itself stays in
    the list with its remaining tags. No channel is ever deleted. A legacy setup (only
    ``intel_channels.tracked``) is read through ``channel_store.load`` first, so its
    channels are kept.
    """

    kind = "collection"
    files = ("config",)
    paths = (("config", (channel_store.CONFIG_KEY,)),)
    modes = ("merge", "replace")

    def _clean(self, payload):
        """-> ``(names, problems)``. Total."""
        if not isinstance(payload, list):
            return [], [f"{self.label}: not a list of channels"]
        names, seen = [], set()
        junk = long_names = repeats = extra = 0
        for entry in payload:
            name = clean_text(entry, _ANY_LENGTH) if isinstance(entry, str) else ""
            if not name:
                junk += 1
            elif len(name) > MAX_CHANNEL_NAME:  # clipping it would name another channel
                long_names += 1
            elif name.casefold() in seen:
                repeats += 1
            elif len(names) >= MAX_CHANNELS:
                extra += 1
            else:
                seen.add(name.casefold())
                names.append(name)
        problems = []
        if junk:
            problems.append(_count(junk, "entry that is not a channel name",
                                   "entries that are not channel names") + " left out")
        if long_names:
            problems.append(_count(long_names, "channel name", "channel names")
                            + f" longer than {MAX_CHANNEL_NAME} characters left out")
        if repeats:
            problems.append(_count(repeats, "repeated channel", "repeated channels")
                            + " left out (the first one counts)")
        if extra:
            problems.append(f"only the first {MAX_CHANNELS} channels are kept ({extra} left out)")
        return names, [f"{self.label}: {line}" for line in problems]

    def export(self, state, ctx):
        config = getattr(state, "config", None)
        if not isinstance(config, dict):
            return ABSENT
        try:
            names = channel_store.load(config).names(channel_store.TAG_INTEL)
        except Exception:
            return ABSENT
        return self._clean(names)[0] or ABSENT

    def validate(self, payload, ctx):
        names, problems = self._clean(payload)
        return (names or ABSENT), problems      # an empty list is "no list", never "clear mine"

    @staticmethod
    def _names(payload):
        if not isinstance(payload, list):
            return []
        return [name for name in payload if isinstance(name, str)]

    def records(self, payload) -> list:
        return [Record(name, name) for name in self._names(payload)]

    def summary(self, payload) -> str:
        return _count(len(self._names(payload)), "channel", "channels")

    def lines(self, payload) -> list:
        names = self._names(payload)
        return [("Channels", ", ".join(names))] if names else []

    def subset(self, payload, keys):
        wanted = _key_set(keys)
        return [name for name in self._names(payload) if name in wanted] or ABSENT

    def apply(self, state, payload, mode, conflict, ctx) -> ItemResult:
        result = ItemResult(self.id)
        names, _ = self._clean(payload)
        if not names:
            return result
        intel = channel_store.TAG_INTEL
        registry = channel_store.load(_view(state))
        for name in names:
            before = registry.get(name)
            outcome = registry.merge(name, (intel,))
            if outcome == "added":
                result.added.append(name)
            elif outcome == "updated":
                result.updated.append(name)
                if before is not None and before.name != name:
                    result.notes.append(f"'{before.name}' is now spelled '{name}'")
            else:
                result.unchanged.append(name)
        if mode == "replace":
            wanted = {name.casefold() for name in names}
            for channel in registry.tagged(intel):
                if channel.name.casefold() in wanted:
                    continue
                registry.set_tags(channel.name, [tag for tag in channel.tags
                                                 if tag.casefold() != intel.casefold()])
                result.removed.append(channel.name)
            if result.removed:
                result.notes.append(
                    _count(len(result.removed), "channel no longer feeds",
                           "channels no longer feed")
                    + " Intelligence Fusion (still in your channel list)")
        if result.added or result.updated or result.removed:
            channel_store.save(_writable(state), registry)
        return result.settle()


# -- the items ------------------------------------------------------------------

ITEMS = (
    _CoalitionsItem(
        id=COALITIONS_ID, label="Coalitions", order=10),
    _FilterItem(
        id=FILTER_ID, label="Intel filter", order=20),
    ConfigFieldsItem(
        id="intel.alerts", label="Alert mute and ping", order=30,
        fields=(
            Field("muted", "Mute alerts", "bool", ("intel_alerts_muted",)),
            Field("sound_enabled", "Ping on intel", "bool", ("intel_sound_enabled",)),
            Field("sound", "Ping sound", "enum", ("intel_alert_sound",), choices=SOUND_KEYS),
        )),
    _FusionChannelsItem(
        id="intel.fusion_channels", label="Intelligence Fusion channels", order=40),
)
