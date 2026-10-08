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
  copy; (b) refuse a filter that would match every party, BEFORE anything is written;
  (c) write. A skipped filter therefore leaves the setup exactly as it was. The test in
  (b) is the app's own - ``intel_filter.expand_parties`` yields no id - so a filter that
  names only coalitions without a single usable member is refused as well.
* A ``location`` / ``parties`` section that lists something but has no usable match-all
  flag (``anywhere`` / ``anyone``) is a RESTRICTION: the flag reads as off, with a problem
  line. It never comes to mean "anywhere" / "anyone".
* EXPORT mirrors what the filter panel effectively uses: ``config.json`` can hold a
  ``min_pilots`` / ``max_jumps`` outside the spinbox range, and the alert gate reads the
  spinbox, which holds the nearest bound. Such a whole number is exported as that bound,
  never as the standard value. IMPORT never clamps: a file's out-of-range number takes
  the standard value, with a problem line.
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

FLAG_SEEDED = "coalitions_seeded"               # boot flag (exact name)
EVERY_PARTY = "the filter would match every party"

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


def _clean_coalitions(raw, label):
    """``{name: definition}`` from an untrusted dict. -> ``(coalitions, problems)``. Total.

    Names are cleaned and 1-80 characters. ``North`` and ``north`` are ONE name, as they
    are to the app's coalition manager (``same_name``): the first one counts and keeps its
    spelling. A definition must be a dict that carries at least one of its two lists. At
    most ``MAX_COALITIONS`` survive.
    """
    if not isinstance(raw, dict):
        return {}, [f"{label}: not a set of coalitions"]
    clean, taken = {}, set()
    bad_names = bad_definitions = repeats = extra = lost = over = 0
    for key, value in raw.items():
        name = _name(key, MAX_COALITION_NAME)
        if name is None:
            bad_names += 1
        elif _fold(name) in taken:
            repeats += 1
        elif not isinstance(value, dict) or not any(part in value for part in _MEMBER_KEYS):
            bad_definitions += 1
        elif len(clean) >= MAX_COALITIONS:
            extra += 1
        else:
            taken.add(_fold(name))
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
    non-empty list reads as off, with a problem line. The block shares nothing with ``raw``
    or with ``DEFAULT_FILTER``.

    ``clamp`` is for EXPORT only - reading this setup's own block: a whole number outside
    its range becomes the nearest bound, without a problem line, because that is the number
    the filter panel's spinbox (and so the alert gate) holds for it. A file's number is
    never clamped.
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

    def match_all(source, part, key, lists, where):
        """The section's match-all flag, read AFTER its lists."""
        if type(source.get(key)) is bool:
            return source[key]
        if any(lists):                          # something is listed: a restriction
            odd(part, f"'{key}' was {'not a yes or no' if key in source else 'missing'}"
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
        kept, seen, bad, extra = [], set(), 0, 0
        for entry in source[key]:
            name = _name(entry, MAX_COALITION_NAME)
            if name is None:
                bad += 1
            elif _fold(name) in seen:           # one coalition, named twice
                continue
            elif len(kept) >= MAX_COALITIONS:
                extra += 1
            else:
                seen.add(_fold(name))
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
            "anywhere": match_all(location, "location", "anywhere", (systems, regions),
                                  "Anywhere"),
            "systems": systems,
            "regions": regions,
        },
        "parties": {
            "anyone": match_all(parties, "parties", "anyone",
                                (alliances, corporations, coalitions), "Anyone"),
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
        """This setup's filter FOR EXPORT: ``(block, definitions, names without a
        definition)``, or ``None`` when there is no filter block. A stored number outside
        the panel's range travels as the number the panel effectively uses."""
        config = _view(state)
        block, _ = _clean_filter(config.get(_FILTER), self.label, clamp=True)
        if block is None:
            return None
        local, _ = _clean_coalitions(config.get(_COALITIONS), self.label)
        named = block["parties"]["coalitions"]
        definitions = {name: local[name] for name in named if name in local}
        block["parties"]["coalitions"] = [name for name in named if name in definitions]
        return block, definitions, [name for name in named if name not in definitions]

    def export(self, state, ctx):
        stored = self._stored(state)
        if stored is None:
            return ABSENT
        return {_FILTER_ROW: stored[0], _COALITIONS: stored[1]}

    def export_notes(self, state, ctx) -> list:
        """Lines for the builder: the coalitions the filter names that have no definition in
        this setup. They are left out of the exported filter (the app ignores them too)."""
        stored = self._stored(state)
        if stored is None:
            return []
        return [f"{self.label}: coalition '{name}' has no definition here and is left out"
                for name in stored[2]]

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

        # (b) The guard, before any write. The app expands the parties to ids and, with
        # "anyone" off and not one id, treats the filter as "no constraint": it would alert
        # on everybody. A coalition NAME without a usable member is no party.
        alliance_ids, corp_ids = intel_filter.expand_parties(parties, working)
        if not parties["anyone"] and not (alliance_ids or corp_ids):
            return ItemResult(self.id, status="skipped", skipped=[_FILTER_ROW],
                              warnings=result.warnings + [EVERY_PARTY])

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
        id="intel.coalitions", label="Coalitions", order=10),
    _FilterItem(
        id="intel.filter", label="Intel filter", order=20),
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
