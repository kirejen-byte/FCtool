"""Group configuration: the Fleet Management items (spec section 6.2).

``fleet.builtin_role_presets``
    The five built-in preset buttons as edited, ``config["default_role_presets"]``. They
    travel as a whole: exactly five clean presets, each with a label of its own, or nothing.
    A payload equal to the shipped five REMOVES the key - the app's own form of "unedited".
    It is applied BEFORE the custom row.

``fleet.role_presets``
    The custom Role Tracker presets, ``config["custom_role_presets"]``: at most
    ``role_presets.CUSTOM_MAX`` entries ``{label, letter, title, cap}``. Every entry passes
    the Fleet tab's own field rule (``role_presets.clean_preset``); two entries are the same
    preset when their labels are equal the way the Fleet tab compares them
    (``role_presets.label_key``).

One label, one button: the Fleet tab never lets a label sit on both rows
(``FCToolGUI._role_preset_label_taken``). An incoming CUSTOM preset whose label a built-in
button in effect already uses is skipped; incoming BUILT-IN buttons always land (an import
never deletes or renames a custom preset of mine) and a label of theirs that my custom row
uses is reported.

``fleet.templates``
    The fleet templates in ``fleet_templates.json`` -> ``templates``. A template travels
    WITHOUT its pilots: ``scrub_template`` nulls every slot ``character`` /
    ``character_id``, drops every ``character`` rule, nulls ``doctrine_id`` and removes the
    ``id`` - on export, again in ``validate``, and once more in ``apply``, so that no caller
    can land a named pilot by skipping a step. An export also leaves out every rule whose
    VALUE is a pilot's name my own file knows (a ``character`` rule retyped in the editor
    keeps the name as its value). ``cached_characters`` is read for that one comparison
    only; it and ``ui`` never go into a group file and are never written.

``fleet.specialized_roles``
    The custom Specialized Role categories, ``config["specialized_roles_custom"]``: at most
    ``spec_roles.MAX_CUSTOM`` entries ``{title, type_ids, tags, threshold}``, shown on the
    Fleet tab after the ten built-in sections. ``spec_roles`` owns the shape: every entry
    passes ``spec_roles.clean_category``, and two entries are the same category when their
    titles have one ``spec_roles.title_key`` (equal ignoring case and invisible characters).
    A category is hull type ids, doctrine tags and a number, but its title (28 characters)
    and its tags (40 each) are free text, and this item cannot know whether one holds a
    pilot's name; the Create window's whole-file own-name sweep is the guard.

Rules of this module
--------------------
* Every item writes ONLY the path it declares (spec 6.7). The templates item creates a
  missing ``fleet_templates.json`` from the app's defaults; in an existing one it rebinds
  the ``templates`` list and nothing else.
* ``validate`` builds a clean payload from scratch and never raises; ``apply`` cleans what
  it is given once more. Nothing here needs a character or the network (spec 9.1).
* ``export_notes(state, ctx=None)`` (custom presets, templates and custom categories) lists
  what an EXPORT of this setup leaves out - a hook the engine calls for the builder; it
  writes nothing and names no pilot.
* ``ROLE_VALUES``, ``CONDITION_TYPES``, ``PACING_RANGES`` and ``LIVE_NAME_LEN`` MIRROR
  ``fleet_template_window`` (a Tk module) and ``fleet_esi`` (the network stack); neither is
  importable here. ``tests/test_group_items_fleet.py`` holds the mirrors equal.

Pure: stdlib, ``group_bundle``, ``group_items``, ``fleet_template_store``, ``role_presets``
and ``spec_roles`` only. No Tk, no ``ui_theme``, no ``fc_gui`` - this module runs in the
boot hook.
"""

from dataclasses import asdict

import fleet_template_store as fts
import role_presets
import spec_roles
from group_bundle import MAX_NAME, clean_tag, clean_text
from group_items import ABSENT, Item, ItemResult, Record, del_path, get_path, set_path, value_text

# -- mirrors (guarded by tests) -------------------------------------------------
ROLE_VALUES = ("squad_member", "squad_commander", "wing_commander", "fleet_commander")
CONDITION_TYPES = ("ship_type", "ship_class", "character", "doctrine_tag",
                   "capital", "subcap", "default")
PACING_RANGES = {                               # the template window's Settings spinboxes
    "sync_active_s": (5, 120),
    "sync_idle_s": (5, 300),
    "move_spacing_ms": (100, 5000),
    "burst_cap": (1, 100),
    "settle_s": (1, 30),
    "bulk_apply_threshold": (1, 100),
}
LIVE_NAME_LEN = 10                              # fleet_esi: ESI clamps wing / squad names

# -- limits ---------------------------------------------------------------------
MAX_TEMPLATES = 200                             # spec section 5
MAX_WINGS = 50
MAX_SQUADS = 50                                 # per wing
MAX_SLOTS = 256                                 # per squad
MAX_RULES = 500                                 # per template
_MAX_LABEL = 200                                # a preset's button label
_MAX_PRIORITY = 1_000_000
MAX_NOTES = 50                                  # export notes of one item; then "and <n> more"

_PRESETS_PATH = ("custom_role_presets",)
_BUILTIN_PATH = ("default_role_presets",)
_CATEGORIES_PATH = (spec_roles.CONFIG_KEY,)     # "specialized_roles_custom"
_DEFAULT_ROLE = ROLE_VALUES[0]
_PRESET_KEYS = ("label", "letter", "title", "cap")
_CONTENT_KEYS = ("wings", "rules", "settings")  # a template apart from name, id and doctrine


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


def _name(value) -> str:
    """A name as display text (cleaned, at most 80 characters); not text -> blank."""
    return clean_text(value, MAX_NAME) if isinstance(value, str) else ""


# -- role presets ---------------------------------------------------------------

def _capped(lines) -> list:
    """At most ``MAX_NOTES`` lines; what is over becomes one last ``"and <n> more"``."""
    lines = list(lines)
    if len(lines) <= MAX_NOTES:
        return lines
    return lines[:MAX_NOTES] + [f"and {len(lines) - MAX_NOTES} more"]


def _refusal(entry) -> str:
    """Why the edit dialog's field rule refuses ``entry`` (its text fields cleaned), in the
    dialog's own words: ``"the key must be at most 2 characters"``. For an export note."""
    try:
        letter, title, cap = entry.get("letter"), entry.get("title"), entry.get("cap")
        if not isinstance(letter, str) or not isinstance(title, str):
            return "the key or the role name is not text"
        _parsed, message = role_presets.parse_preset_fields(
            letter, title, "" if cap is None else str(cap))
        if isinstance(message, str) and message.strip():
            reason = message.strip().rstrip(".")
            return reason[:1].lower() + reason[1:]
    except Exception:
        pass
    return "not a usable preset"


def _judge(raw):
    """One preset entry -> ``(clean preset | None, why it is refused)``.

    The text fields lose their control characters and are clipped before the Fleet tab's
    own rule (``role_presets.clean_preset``) judges the entry. Cleaning a clean preset
    gives it back unchanged."""
    if not isinstance(raw, dict):
        return None, "not a preset"
    entry = {"cap": raw.get("cap")}
    for key, limit in (("label", _MAX_LABEL), ("letter", 16), ("title", MAX_NAME)):
        value = raw.get(key)
        entry[key] = clean_text(value, limit) if isinstance(value, str) else value
    preset = role_presets.clean_preset(entry)
    if preset is None:
        return None, _refusal(entry)
    if len(preset["label"]) > _MAX_LABEL:
        return None, f"a label longer than {_MAX_LABEL} characters"
    return preset, ""


def _preset_from(raw):
    """One preset entry from a FILE -> a clean preset, or ``None`` (see ``_judge``)."""
    return _judge(raw)[0]


def _preset_detail(preset) -> str:
    text = f"key {preset['letter']}, {preset['title']}"
    return text if preset["cap"] is None else f"{text}, cap {preset['cap']}"


# "Is this label in use on the other row" - the two halves of the Fleet tab's own check
# (``FCToolGUI._role_preset_label_taken``): labels are compared exactly
# (``role_presets.label_key``), against the built-in buttons IN EFFECT and against the
# ``label`` of every dict entry of the custom row.

def _builtin_label_keys(state) -> set:
    """The label keys of the built-in buttons in effect: the stored five when they are
    well-formed, else the shipped five (``role_presets.default_presets``)."""
    return {role_presets.label_key(row[0])
            for row in role_presets.default_presets(_view(state))}


def _custom_label_keys(state) -> set:
    """The label keys on my custom row; a row that is not a list holds none."""
    stored = get_path(_view(state), _PRESETS_PATH)
    if not isinstance(stored, list):
        return set()
    keys = {role_presets.label_key(entry.get("label"))
            for entry in stored if isinstance(entry, dict)}
    keys.discard("")                            # the key of a label that is not text
    return keys


def _stored_label(entry) -> str:
    """The label an entry of my custom row is known by when it is not a usable preset: the
    label as stored, or ``""`` when that is blank or not text."""
    label = role_presets.label_key(entry.get("label"))
    return label if label.strip() else ""


class _RolePresetsItem(Item):
    """The custom preset row.

    merge: a preset whose label is already there is replaced in place (``theirs``) or left
    (``mine``); a new one is appended, in file order, while the row holds fewer than
    ``CUSTOM_MAX`` - the rest are ``skipped`` with one warning. replace: the row becomes the
    file's; labels that left are ``removed``.

    An import never deletes a preset of mine: EVERY dict entry of the stored row is kept
    exactly as it is, in its place, extra keys included - also one the field rule refuses
    (the Fleet tab's own "save as preset" writes a three-character key or a cap of 0). Such
    an entry is matched by its label as stored, and by nothing when it has none; it fills
    a place of the row like any other. In replace mode it is ``removed`` like any other (one
    without a label is not listed). Only entries that are not dicts (they crash the Fleet
    tab) are dropped - with a warning, and only when the row is written anyway. A stored
    value that is not a list counts as empty.

    In BOTH modes an incoming preset whose label a built-in button in effect already uses
    is ``skipped`` with a warning - the built-in item runs first (``order``), so that is the
    row the import leaves behind. When nothing else arrives, nothing is written: a replace
    made only of such presets does not blank the row.
    """

    id = "fleet.role_presets"
    label = "Custom role presets"
    kind = "collection"
    files = ("config",)
    paths = (("config", _PRESETS_PATH),)
    order = 20                                  # after the built-in row (order 10)
    modes = ("merge", "replace")
    conflicts = True

    def _clean(self, payload):
        """-> ``(presets, problems)``; at most ``CUSTOM_MAX``, one per label. Total."""
        if not isinstance(payload, list):
            return [], [f"{self.label}: not a list of presets"]
        presets, seen, bad, twice = [], set(), 0, 0
        for raw in payload:
            preset = _preset_from(raw)
            if preset is None:
                bad += 1
                continue
            key = role_presets.label_key(preset["label"])
            if key in seen:
                twice += 1
                continue
            seen.add(key)
            presets.append(preset)
        problems = []
        if bad:
            odd = _count(bad, "entry that is not a usable preset",
                         "entries that are not usable presets")
            problems.append(f"{self.label}: {odd} left out")
        if twice:
            again = _count(twice, "preset with a label used earlier in the file",
                           "presets with a label used earlier in the file")
            problems.append(f"{self.label}: {again} left out")
        extra = len(presets) - role_presets.CUSTOM_MAX
        if extra > 0:
            presets = presets[:role_presets.CUSTOM_MAX]
            problems.append(f"{self.label}: only the first {role_presets.CUSTOM_MAX} presets "
                            f"are kept ({extra} left out)")
        return presets, problems

    def export(self, state, ctx):
        stored = get_path(getattr(state, "config", None), _PRESETS_PATH)
        if not isinstance(stored, list):
            return ABSENT
        return self._clean(stored)[0] or ABSENT

    def export_notes(self, state, ctx=None) -> list:
        """Lines for the builder, ``"<label>: left out (<reason>)"``: the presets of my row
        that ``export`` leaves out. Most often one the edit dialog's field rule refuses -
        the Fleet tab's own "save as preset" is laxer (a three-character key, a cap of 0);
        also a label used earlier in the row and whatever is past the first ``CUSTOM_MAX``.
        The walk is ``_clean``'s own, entry by entry. Entries that are not dicts are no
        presets and get no line. Never raises; at most ``MAX_NOTES`` lines and
        ``"and <n> more"``."""
        try:
            stored = get_path(_view(state), _PRESETS_PATH)
            if not isinstance(stored, list):
                return []
            lines, seen = [], set()
            for raw in stored:
                if not isinstance(raw, dict):
                    continue
                preset, why = _judge(raw)
                if preset is None:
                    shown = _name(raw.get("label")) or "a preset without a label"
                else:
                    shown, key = preset["label"], role_presets.label_key(preset["label"])
                    if key in seen:
                        why = "its label is used earlier in the row"
                    elif len(seen) >= role_presets.CUSTOM_MAX:
                        why = f"only the first {role_presets.CUSTOM_MAX} presets are kept"
                    else:
                        seen.add(key)
                        continue
                lines.append(f"{shown}: left out ({why})")
            return _capped(lines)
        except Exception:
            return []

    def validate(self, payload, ctx):
        presets, problems = self._clean(payload)
        return (presets or ABSENT), problems

    def records(self, payload) -> list:
        return [Record(preset["label"], preset["label"], _preset_detail(preset))
                for preset in self._clean(payload)[0]]

    def summary(self, payload) -> str:
        return _count(len(self._clean(payload)[0]), "preset", "presets")

    def lines(self, payload) -> list:
        return [(preset["label"], _preset_detail(preset)) for preset in self._clean(payload)[0]]

    def subset(self, payload, keys):
        wanted = _key_set(keys)
        return [preset for preset in self._clean(payload)[0]
                if preset["label"] in wanted] or ABSENT

    def apply(self, state, payload, mode, conflict, ctx) -> ItemResult:
        result = ItemResult(self.id)
        incoming, _ = self._clean(payload)
        if not incoming:                        # nothing usable never blanks the row
            return result
        taken, free = _builtin_label_keys(state), []
        for preset in incoming:
            if role_presets.label_key(preset["label"]) in taken:
                result.skipped.append(preset["label"])
                result.warnings.append(f"{preset['label']}: skipped (a built-in preset "
                                       f"already uses that name)")
            else:
                free.append(preset)
        incoming = free
        if not incoming:                        # ... and neither does nothing that may land
            return result

        stored = get_path(_view(state), _PRESETS_PATH)
        new, cleaned = [], []                   # my entries as stored / as they are compared
        if isinstance(stored, list):
            for entry in stored:
                if isinstance(entry, dict):     # a preset of mine, whatever its fields
                    new.append(entry)
                    cleaned.append(role_presets.clean_preset(entry)
                                   or {"label": _stored_label(entry)})
            dropped = len(stored) - len(new)
            if dropped:
                odd = _count(dropped, "entry that is not a preset",
                             "entries that are not presets")
                result.warnings.append(f"{self.label}: your saved list held {odd}")
        elif stored is not None:
            result.warnings.append(f"{self.label}: your saved list could not be read "
                                   f"and counts as empty")
        first = {}                              # label key -> index of my first such preset
        for index, preset in enumerate(cleaned):
            if preset["label"]:                 # no label: no file preset is "the same one"
                first.setdefault(role_presets.label_key(preset["label"]), index)

        if mode == "replace":
            arriving = set()
            for preset in incoming:
                key = role_presets.label_key(preset["label"])
                arriving.add(key)
                index = first.get(key)
                if index is None:
                    result.added.append(preset["label"])
                elif cleaned[index] == preset:
                    result.unchanged.append(preset["label"])
                else:
                    result.updated.append(preset["label"])
            result.removed.extend(
                preset["label"] for preset in cleaned
                if preset["label"] and role_presets.label_key(preset["label"]) not in arriving)
            new = [dict(preset) for preset in incoming]
            changed = new != stored
        else:
            full = False
            for preset in incoming:
                key = role_presets.label_key(preset["label"])
                index = first.get(key)
                if index is not None:
                    if cleaned[index] == preset:
                        result.unchanged.append(preset["label"])
                    elif conflict == "mine":
                        result.skipped.append(preset["label"])
                    else:
                        new[index], cleaned[index] = dict(preset), preset
                        result.updated.append(preset["label"])
                elif len(new) < role_presets.CUSTOM_MAX:
                    first[key] = len(new)
                    new.append(dict(preset))
                    cleaned.append(preset)
                    result.added.append(preset["label"])
                else:
                    result.skipped.append(preset["label"])
                    full = True
            if full:
                result.warnings.append(
                    f"the custom preset row holds {role_presets.CUSTOM_MAX}")
            changed = bool(result.added or result.updated)

        if changed:
            set_path(_writable(state), _PRESETS_PATH, new)
            result.status = "applied"
        return result


class _BuiltinPresetsItem(Item):
    """The five built-in preset buttons, as one value: all five or nothing.

    The payload is compared with the five buttons IN EFFECT (``role_presets.default_presets``
    - a damaged or missing key reads as the shipped five), so an import that would change
    nothing writes nothing. Otherwise a payload equal to the shipped five removes the key
    and any other payload is stored whole.

    Five that share a label among themselves are no payload at all (the Fleet tab cannot
    produce them). A label of theirs that my CUSTOM row already uses does not stop them: the
    five land, my custom preset stays as it is, and a warning names the label.
    """

    id = "fleet.builtin_role_presets"
    label = "Built-in role presets"
    kind = "value"
    files = ("config",)
    paths = (("config", _BUILTIN_PATH),)
    order = 10                                  # before the custom row (order 20)

    ROW = "buttons"                             # the one tickable row

    def _clean(self, payload):
        """-> ``(five clean presets | None, problems)``. Total."""
        wanted = len(role_presets.BUILTIN_PRESETS)
        if not isinstance(payload, list) or len(payload) != wanted:
            return None, [f"{self.label}: not a list of exactly {wanted} presets"]
        presets = [_preset_from(raw) for raw in payload]
        bad = sum(1 for preset in presets if preset is None)
        if bad:
            odd = _count(bad, "entry is not a usable preset", "entries are not usable presets")
            return None, [f"{self.label}: {odd}"]
        seen = set()
        for preset in presets:
            key = role_presets.label_key(preset["label"])
            if key in seen:
                return None, [f"built-in presets: two share the name {preset['label']}"]
            seen.add(key)
        return presets, []

    def export(self, state, ctx):
        config = getattr(state, "config", None)
        if config is not None and not isinstance(config, dict):
            return ABSENT                       # unreadable: the buttons in effect are unknown
        rows = role_presets.default_presets(config)
        presets, _ = self._clean([dict(zip(_PRESET_KEYS, row)) for row in rows])
        return presets if presets is not None else ABSENT

    def validate(self, payload, ctx):
        presets, problems = self._clean(payload)
        return (presets if presets is not None else ABSENT), problems

    def records(self, payload) -> list:
        presets, _ = self._clean(payload)
        if presets is None:
            return []
        return [Record(self.ROW, "The five built-in buttons",
                       ", ".join(preset["label"] for preset in presets))]

    def summary(self, payload) -> str:
        presets, _ = self._clean(payload)
        if presets is None:
            return ""
        return value_text([preset["label"] for preset in presets], 80)

    def lines(self, payload) -> list:
        presets, _ = self._clean(payload)
        return [(preset["label"], _preset_detail(preset)) for preset in presets or ()]

    def subset(self, payload, keys):
        presets, _ = self._clean(payload)
        if presets is None or self.ROW not in _key_set(keys):
            return ABSENT
        return presets

    def apply(self, state, payload, mode, conflict, ctx) -> ItemResult:
        result = ItemResult(self.id)
        presets, problems = self._clean(payload)
        if presets is None:
            result.status = "skipped"
            result.skipped.append(self.ROW)
            result.warnings.extend(problems)
            return result
        new = [tuple(preset[key] for key in _PRESET_KEYS) for preset in presets]
        current = role_presets.default_presets(_view(state))
        if new == current:
            result.unchanged.append(self.ROW)
            return result
        if new == [tuple(row) for row in role_presets.BUILTIN_PRESETS]:
            del_path(_writable(state), _BUILTIN_PATH)
        else:
            set_path(_writable(state), _BUILTIN_PATH, [dict(preset) for preset in presets])
        result.updated.append(self.ROW)
        for old, fresh in zip(current, new):
            if old != fresh:
                result.notes.append(f"{old[0]} -> {fresh[0]}" if old[0] != fresh[0]
                                    else f"{fresh[0]}: key, role name or cap changed")
        mine = _custom_label_keys(state)        # never deleted, never renamed: reported
        result.warnings.extend(
            f"{preset['label']} is now on both preset rows - rename or delete your custom one"
            for preset in presets if role_presets.label_key(preset["label"]) in mine)
        return result.settle()


# -- fleet templates: one template ----------------------------------------------

def _new_stats() -> dict:
    return {"junk": 0, "roles": 0, "kinds": 0, "pacing": 0}


def _entries(value, stats) -> list:
    """The dict entries of a list. Anything else is left out and counted."""
    if value is None:
        return []
    if not isinstance(value, list):
        stats["junk"] += 1
        return []
    entries = [entry for entry in value if isinstance(entry, dict)]
    stats["junk"] += len(value) - len(entries)
    return entries


def _sane_slot(raw) -> dict:
    character_id = raw.get("character_id")
    return {"character": _name(raw.get("character")) or None,
            "tag": _name(raw.get("tag")) or None,
            "role": _name(raw.get("role", _DEFAULT_ROLE)),
            "character_id": character_id if type(character_id) is int else None}


def _sane_squad(raw, stats) -> dict:
    size = raw.get("max_size")
    return {"name": _name(raw.get("name")),
            "max_size": size if type(size) is int and 0 < size <= MAX_SLOTS else None,
            "slots": [_sane_slot(slot) for slot in _entries(raw.get("slots"), stats)]}


def _sane_rule(raw, stats):
    condition, action = raw.get("condition"), raw.get("action")
    if not isinstance(condition, dict) or not isinstance(action, dict):
        stats["junk"] += 1
        return None
    wing, squad = action.get("wing_name"), action.get("squad_name")
    if not all(value is None or isinstance(value, str) for value in (wing, squad)):
        stats["junk"] += 1                      # a target that is not a name: no guessing
        return None
    priority = raw.get("priority")
    return {
        "priority": priority if type(priority) is int and abs(priority) <= _MAX_PRIORITY else 0,
        "condition": {"type": _name(condition.get("type", "ship_type")),
                      "value": _name(condition.get("value"))},
        # A blank target is "anywhere", stored as None - the template editor's own form
        # (``wing or None``); a rule that points at the wing "" shows as broken.
        "action": {"role": _name(action.get("role", _DEFAULT_ROLE)),
                   "wing_name": _name(wing) or None,
                   "squad_name": _name(squad) or None},
    }


def _sane_settings(raw, stats) -> dict:
    defaults = asdict(fts.RebalanceSettings())  # the store's six settings, in its order
    if raw is None:
        return defaults
    if not isinstance(raw, dict):
        stats["junk"] += 1
        return defaults
    settings = {}
    for key, default in defaults.items():
        value = raw.get(key, default)
        low, high = PACING_RANGES.get(key, (None, None))
        if type(value) is int and (low is None or low <= value <= high):
            settings[key] = value
        else:
            settings[key] = default
            stats["pacing"] += 1
    return settings


def _sane(raw, stats) -> dict:
    """An untrusted template dict rebuilt from scratch in the shape the store reads: known
    keys only, lists where it expects lists, text cleaned, numbers typed. No judgement on
    roles, rule kinds, names or sizes - the callers make those. Never raises."""
    doctrine = raw.get("doctrine_id")
    return {
        "name": _name(raw.get("name")),
        "doctrine_id": doctrine if isinstance(doctrine, str) else None,
        "wings": [{"name": _name(wing.get("name")),
                   "squads": [_sane_squad(squad, stats)
                              for squad in _entries(wing.get("squads"), stats)]}
                  for wing in _entries(raw.get("wings"), stats)],
        "rules": [rule for rule in (_sane_rule(entry, stats)
                                    for entry in _entries(raw.get("rules"), stats))
                  if rule is not None],
        "settings": _sane_settings(raw.get("settings"), stats),
    }


def _normal(raw, version, stats, scrub) -> dict:
    """One template as the store would save it after loading it from a ``version`` file,
    without its ``id``: the round trip ``template_from_dict`` -> migrations ->
    ``template_to_dict``. With ``scrub`` it carries no pilot and no doctrine."""
    template = fts.template_from_dict(_sane(raw, stats))
    if version < 2:
        fts._migrate_template_v1(template)
    fts._migrate_template_tags(template)
    if scrub:
        template.doctrine_id = None
        for wing in template.wings:
            for squad in wing.squads:
                for slot in squad.slots:
                    slot.character = None
                    slot.character_id = None
        template.rules = [rule for rule in template.rules
                          if rule.condition.type != "character"]
    data = fts.template_to_dict(template)
    del data["id"]
    return data


def scrub_template(d) -> dict:
    """A copy of template ``d`` that is safe to pass around.

    The ``id`` is removed and ``doctrine_id`` is ``None``; every slot's ``character`` and
    ``character_id`` are ``None``; rules whose condition type is ``character`` are gone; and
    every key outside the store's schema is gone - the copy is the result of the store's own
    round trip. ``d`` is not modified. Total: anything that is not a template dict gives an
    empty, nameless template. Scrubbing a scrubbed template changes nothing.
    """
    return _normal(d if isinstance(d, dict) else {}, fts.SCHEMA_VERSION, _new_stats(), True)


def _oversize(raw) -> str:
    """Why the STRUCTURE of a raw template is too large to take, or ``""``.

    Wings, squads and slots only - no migration adds any. The rules are judged in
    ``_clean_template``, on the normalised template: the store's version 1 migration turns
    slot tags into rules, so the list in the file is not the list that lands."""
    wings = raw.get("wings")
    if not isinstance(wings, list):
        return ""
    if len(wings) > MAX_WINGS:
        return f"more than {MAX_WINGS} wings"
    for wing in wings:
        squads = wing.get("squads") if isinstance(wing, dict) else None
        if not isinstance(squads, list):
            continue
        if len(squads) > MAX_SQUADS:
            return f"more than {MAX_SQUADS} squads in a wing"
        for squad in squads:
            slots = squad.get("slots") if isinstance(squad, dict) else None
            if isinstance(slots, list) and len(slots) > MAX_SLOTS:
                return f"more than {MAX_SLOTS} slots in a squad"
    return ""


def _live_name(name) -> bool:
    """Whether a wing or squad name is one a live fleet can carry: the game keeps its first
    ``LIVE_NAME_LEN`` characters, and at least one of those must be visible. Zero-width and
    bidi marks, a soft hyphen and blank-looking letters are no name
    (``group_bundle.clean_tag`` owns that rule). The name itself is not rewritten."""
    return bool(clean_tag(name[:LIVE_NAME_LEN]))


def _clean_template(raw, version, problems):
    """One template dict from a file -> a clean, scrubbed template, or ``None``.

    Dropped (with a line in ``problems``): no clean name of 1-80 characters; larger than
    the limits; a wing or squad without a usable name (``_live_name`` - the live-fleet
    Apply cannot create one and fails half way through). Repaired (with a line): a role
    outside ``ROLE_VALUES`` becomes a squad member; a rule of a kind outside
    ``CONDITION_TYPES`` is left out.

    The rule limits - how many, and their priorities - are judged on the template AS IT
    LANDS, after the store's migrations: cleaning a clean template gives it back unchanged,
    so what ``validate`` shows is what ``apply`` writes.
    """
    name = _name(raw.get("name"))
    if not name:
        problems.append("a template without a name was left out")
        return None
    large = _oversize(raw)
    if large:
        problems.append(f"{name}: left out ({large})")
        return None
    stats = _new_stats()
    data = _normal(raw, version, stats, scrub=True)
    if not all(_live_name(wing["name"])
               and all(_live_name(squad["name"]) for squad in wing["squads"])
               for wing in data["wings"]):
        problems.append(f"{name}: left out (a wing or squad without a name)")
        return None
    for wing in data["wings"]:
        for squad in wing["squads"]:
            for slot in squad["slots"]:
                if slot["role"] not in ROLE_VALUES:
                    slot["role"] = _DEFAULT_ROLE
                    stats["roles"] += 1
    rules = []
    for rule in data["rules"]:
        if rule["condition"]["type"] not in CONDITION_TYPES:
            stats["kinds"] += 1
            continue
        if rule["action"]["role"] not in ROLE_VALUES:
            rule["action"]["role"] = _DEFAULT_ROLE
            stats["roles"] += 1
        rules.append(rule)
    data["rules"] = rules
    if len(rules) > MAX_RULES:
        problems.append(f"{name}: left out (more than {MAX_RULES} rules)")
        return None
    if any(abs(rule["priority"]) > _MAX_PRIORITY for rule in rules):
        # Only a migrated rule can be here (the file's own are reset on the way in): it is
        # numbered one above the highest priority in the file.
        problems.append(f"{name}: left out (a rule priority above {_MAX_PRIORITY})")
        return None
    if stats["roles"]:
        problems.append(f"{name}: {_count(stats['roles'], 'unknown role', 'unknown roles')} "
                        f"changed to squad member")
    if stats["kinds"]:
        kinds = _count(stats["kinds"], "rule of an unknown kind", "rules of an unknown kind")
        problems.append(f"{name}: {kinds} left out")
    if stats["junk"]:
        junk = _count(stats["junk"], "entry that could not be read",
                      "entries that could not be read")
        problems.append(f"{name}: {junk} left out")
    if stats["pacing"]:
        pacing = _count(stats["pacing"], "pacing setting", "pacing settings")
        problems.append(f"{name}: {pacing} reset to the default")
    return data


def _file_version(data) -> int:
    """The format version of a template file or payload, read the way the store reads it:
    a missing or unusable value is version 1."""
    version = data.get("version", 1)
    return version if type(version) is int else 1


def _named_slots(template) -> int:
    """How many slots of a (sane) template name a pilot."""
    return sum(1 for wing in template["wings"] for squad in wing["squads"]
               for slot in squad["slots"]
               if slot["character"] is not None or slot["character_id"] is not None)


def _known_pilots(data) -> set:
    """The pilot names MY templates file knows, case-folded: the saved character list (the
    store writes ``{"name", "character_id"}`` rows; older files hold bare names), every
    named slot and the value of every ``character`` rule. Read to keep those names OUT of an
    export - nothing of this goes into a file. Total: anything unreadable knows no name."""
    names, stats = set(), _new_stats()

    def know(value):
        text = _name(value).casefold()
        if text:
            names.add(text)

    cached = data.get("cached_characters")
    for entry in cached if isinstance(cached, list) else ():
        know(entry.get("name") if isinstance(entry, dict) else entry)
    for raw in _entries(data.get("templates"), stats):
        for wing in _entries(raw.get("wings"), stats):
            for squad in _entries(wing.get("squads"), stats):
                for slot in _entries(squad.get("slots"), stats):
                    know(slot.get("character"))
        for rule in _entries(raw.get("rules"), stats):
            condition = rule.get("condition")
            if isinstance(condition, dict) and condition.get("type") == "character":
                know(condition.get("value"))
    return names


def _same_live_name(names) -> list:
    """``(first, later)`` pairs of names that are equal in their first ten characters."""
    seen, pairs = {}, []
    for name in names:
        key = name[:LIVE_NAME_LEN]
        if key in seen:
            pairs.append((seen[key], name))
        else:
            seen[key] = name
    return pairs


def _live_name_warnings(template) -> list:
    pairs = _same_live_name([wing["name"] for wing in template["wings"]])
    for wing in template["wings"]:
        pairs.extend(_same_live_name([squad["name"] for squad in wing["squads"]]))
    lines = []
    for first, later in pairs:
        line = (f"{template['name']}: '{first}' and '{later}' become the same name "
                f"in a live fleet")
        if line not in lines:
            lines.append(line)
    return lines


def _shape_text(template) -> str:
    wings = template["wings"]
    squads = sum(len(wing["squads"]) for wing in wings)
    return (f"{_count(len(wings), 'wing', 'wings')}, {_count(squads, 'squad', 'squads')}, "
            f"{_count(len(template['rules']), 'rule', 'rules')}")


# -- fleet templates: the item --------------------------------------------------

class _TemplatesItem(Item):
    """The fleet templates, without their pilots.

    merge only. A template is matched by NAME with my FIRST template of that name (the one
    the template selector picks). ``theirs``: its content is replaced; its ``id`` stays, so
    everything that points at it keeps working, and so does its ``doctrine_id`` (a link to
    MY library that a file never carries). ``mine``: skipped. Content that is already the
    same is ``unchanged`` whatever the choice. A template I do not have is appended with a
    new id. Everything else in the file - other templates, entries the store cannot read,
    ``cached_characters``, ``ui``, ``version`` - is left exactly as it is.
    """

    id = "fleet.templates"
    label = "Fleet templates"
    kind = "collection"
    files = ("templates",)
    paths = (("templates", ("templates",)),)
    order = 30
    conflicts = True
    never_saved = ("Named pilots in slots", "Rules that name a character",
                   "Your saved character list")

    def _clean_list(self, raw, version, left=None):
        """A list of raw templates -> ``(clean templates, problems)``, one per name.
        ``left``, when given a list, receives the reason line of every template that is
        left out whole (``"<name>: left out (<reason>)"``), without the item's label."""
        templates, problems, seen, junk = [], [], set(), 0
        for entry in raw:
            if not isinstance(entry, dict):
                junk += 1
                continue
            lines = []
            data = _clean_template(entry, version, lines)
            problems.extend(f"{self.label} - {line}" for line in lines)
            if data is None:
                if left is not None:
                    left.extend(lines)
                continue
            if data["name"] in seen:
                problems.append(f"{self.label} - {data['name']}: listed twice, "
                                f"the first is kept")
                if left is not None:
                    left.append(f"{data['name']}: left out (another template has this name)")
                continue
            seen.add(data["name"])
            templates.append(data)
        if junk:
            odd = _count(junk, "entry that is not a template", "entries that are not templates")
            problems.append(f"{self.label}: {odd} left out")
        return templates, problems

    def _clean(self, payload):
        """An untrusted payload -> ``(clean templates, problems)``. Total."""
        if not isinstance(payload, dict):
            return [], [f"{self.label}: not a fleet-template block"]
        version = _file_version(payload)
        if version > fts.SCHEMA_VERSION:
            return [], [f"{self.label}: made with a newer fleet-template format"]
        raw = payload.get("templates")
        if not isinstance(raw, list):
            return [], [f"{self.label}: holds no list of templates"]
        if len(raw) > MAX_TEMPLATES:
            return [], [f"{self.label}: more than {MAX_TEMPLATES} templates"]
        return self._clean_list(raw, version)

    @staticmethod
    def _payload(templates):
        return {"version": fts.SCHEMA_VERSION, "templates": templates} if templates else ABSENT

    def _exported(self, state):
        """My templates as they travel -> ``(templates, notes)``. ``notes`` are the lines of
        ``export_notes``: what was left out, never WHO (a note names a template)."""
        data = getattr(state, "templates", None)
        if not isinstance(data, dict):          # missing or unreadable
            return [], []
        version, raw = _file_version(data), data.get("templates")
        if version > fts.SCHEMA_VERSION or not isinstance(raw, list):
            return [], []
        notes = []
        templates = self._clean_list(raw, version, notes)[0]
        over = len(templates) - MAX_TEMPLATES
        if over > 0:                            # a file holds no more: ``_clean`` refuses it
            templates = templates[:MAX_TEMPLATES]
            notes.append(f"{over} template(s) left out (at most {MAX_TEMPLATES} per file)")
        # Decision D4, one step further than ``scrub_template``: a ``character`` rule whose
        # type was changed afterwards still holds the pilot's name as its value.
        pilots = _known_pilots(data)
        if pilots:
            for template in templates:
                kept = [rule for rule in template["rules"]
                        if rule["condition"]["value"].casefold() not in pilots]
                gone = len(template["rules"]) - len(kept)
                if gone:
                    notes.append(f"{template['name']}: {gone} rule(s) naming a pilot left out")
                template["rules"] = kept
        return templates, notes

    def export(self, state, ctx):
        return self._payload(self._exported(state)[0])

    def export_notes(self, state, ctx=None) -> list:
        """Lines for the builder: ``"<template>: left out (<reason>)"`` for a template of
        mine that does not travel (it breaks a limit, a wing or squad of it has no visible
        name, or an earlier template of mine has its name); ``"<n> template(s) left out (at
        most 200 per file)"`` for those past ``MAX_TEMPLATES``; and ``"<template>: <n>
        rule(s) naming a pilot left out"`` for rules whose VALUE is a pilot my file knows.
        (Rules of the ``character`` kind are not counted: ``never_saved`` says they never
        travel.) Never raises; at most ``MAX_NOTES`` lines and ``"and <n> more"``."""
        try:
            return _capped(self._exported(state)[1])
        except Exception:
            return []

    def validate(self, payload, ctx):
        templates, problems = self._clean(payload)
        return self._payload(templates), problems

    def records(self, payload) -> list:
        return [Record(template["name"], template["name"], _shape_text(template))
                for template in self._clean(payload)[0]]

    def summary(self, payload) -> str:
        return _count(len(self._clean(payload)[0]), "template", "templates")

    def lines(self, payload) -> list:
        return [(template["name"], _shape_text(template))
                for template in self._clean(payload)[0]]

    def subset(self, payload, keys):
        wanted = _key_set(keys)
        return self._payload([template for template in self._clean(payload)[0]
                              if template["name"] in wanted])

    def apply(self, state, payload, mode, conflict, ctx) -> ItemResult:
        result = ItemResult(self.id)
        incoming, _ = self._clean(payload)      # scrubbed here too, whatever the caller did
        if not incoming:
            return result
        data = state.templates
        if data is not None and not isinstance(data, dict):
            raise TypeError("fleet_templates.json is not readable")
        version = fts.SCHEMA_VERSION if data is None else _file_version(data)
        if version > fts.SCHEMA_VERSION:
            result.status = "skipped"
            result.skipped.extend(template["name"] for template in incoming)
            result.warnings.append(f"{self.label}: your fleet templates were saved by a newer "
                                   f"FCTool and are left as they are")
            return result

        stored = None if data is None else data.get("templates")
        if stored is not None and not isinstance(stored, list):
            result.warnings.append(f"{self.label}: your saved templates could not be read "
                                   f"and count as empty")
            stored = None
        new = list(stored or ())
        first = {}                              # name -> index of my first template of it
        for index, entry in enumerate(new):
            if isinstance(entry, dict):
                first.setdefault(_name(entry.get("name")), index)

        for template in incoming:
            name = template["name"]
            index = first.get(name)
            if index is None:
                first[name] = len(new)
                new.append({"id": ctx.new_id(), **template})
                result.added.append(name)
                result.warnings.extend(_live_name_warnings(template))
                continue
            mine = new[index]
            view = _normal(mine, version, _new_stats(), scrub=False)
            # The same as stored, or the same once the store has loaded it (an old-format
            # file is migrated on load; what an earlier import wrote into it is not).
            if (all(mine.get(key) == template[key] for key in _CONTENT_KEYS)
                    or all(view[key] == template[key] for key in _CONTENT_KEYS)):
                result.unchanged.append(name)
                continue
            if conflict == "mine":
                result.skipped.append(name)
                continue
            my_id, doctrine = mine.get("id"), mine.get("doctrine_id")
            new[index] = {**template,
                          "id": my_id if isinstance(my_id, str) and my_id else ctx.new_id(),
                          "doctrine_id": doctrine if isinstance(doctrine, str) else None}
            new[index] = {key: new[index][key]
                          for key in ("id", "name", "doctrine_id", *_CONTENT_KEYS)}
            result.updated.append(name)
            named = _named_slots(view)
            if named:
                result.warnings.append(
                    f"{name}: {named} named-pilot slot(s) replaced by open slots")
            result.warnings.extend(_live_name_warnings(template))

        if result.added or result.updated:
            if state.templates is None:         # a missing file: the app's empty store
                state.templates = {"version": fts.SCHEMA_VERSION, "templates": [],
                                   "cached_characters": [], "ui": {}}
            state.templates["templates"] = new
        return result.settle()


# -- custom Specialized Role categories -----------------------------------------

# What ``spec_roles.normalize`` makes of one entry of a list (``_judged``).
_KEPT, _UNUSABLE, _TWICE, _OVER = "kept", "unusable", "twice", "over"


def _judged(raw) -> list:
    """Every entry of a list of categories, judged the way ``spec_roles.normalize`` reads
    the list: ``[(index, entry, category | None, title key, verdict)]``.

    ``_KEPT`` - one of the categories the list MEANS (the entries ``normalize`` returns, in
    its order: the ``category`` of the kept rows IS ``normalize(raw)``); ``_UNUSABLE`` -
    ``spec_roles.clean_category`` refuses it (``category`` is ``None``, the key ``""``);
    ``_TWICE`` - its title has the key of an earlier kept one; ``_OVER`` - a usable category
    past the first ``spec_roles.MAX_CUSTOM``. An unusable entry uses up no place. Total for
    a list (``spec_roles`` is)."""
    rows, seen = [], set()
    for index, entry in enumerate(raw):
        category = spec_roles.clean_category(entry)
        if category is None:
            rows.append((index, entry, None, "", _UNUSABLE))
            continue
        key = spec_roles.title_key(category["title"])
        if key in seen:
            verdict = _TWICE
        elif len(seen) >= spec_roles.MAX_CUSTOM:
            verdict = _OVER
        else:
            seen.add(key)
            verdict = _KEPT
        rows.append((index, entry, category, key, verdict))
    return rows


def _stored_title(entry) -> str:
    """The title of an entry ``spec_roles`` refuses, as a section header would show it
    (cleaned by ``spec_roles``' own title rule, through a probe category), or ``""`` when
    it has none."""
    if not isinstance(entry, dict):
        return ""
    probe = spec_roles.clean_category({"title": entry.get("title"), "type_ids": [1]})
    return probe["title"] if probe else ""


def _category_detail(category) -> str:
    """``"3 hulls, 2 tags, min 5"`` - a part with nothing to say is left out."""
    parts = []
    if category["type_ids"]:
        parts.append(_count(len(category["type_ids"]), "hull", "hulls"))
    if category["tags"]:
        parts.append(_count(len(category["tags"]), "tag", "tags"))
    if category["threshold"] is not None:
        parts.append(f"min {category['threshold']}")
    return ", ".join(parts)


class _SpecRolesItem(Item):
    """The custom Specialized Role categories.

    A category of the file and one of mine are THE SAME when their titles have one
    ``spec_roles.title_key``. "Mine" are the categories my stored list MEANS - what
    ``spec_roles.normalize`` reads out of it, which is what the Fleet tab shows.

    merge: a category I have is replaced in its place (``theirs``) or left (``mine``); equal
    content is ``unchanged`` whatever the choice. A new one is appended, in file order,
    while I have fewer than ``MAX_CUSTOM``; each of the rest is ``skipped`` with a warning.
    replace: the list becomes the file's; categories of mine that are not in the file are
    ``removed``.

    An import never deletes (spec 4): a merge leaves EVERY entry of my stored list exactly
    as it is, in its place - an entry ``spec_roles`` ignores, extra keys, whatever is past
    the twelfth - except the one entry a ``theirs`` conflict replaces. (Unlike the preset
    row nothing needs dropping: the Fleet tab reads this list through ``normalize`` and an
    odd entry cannot hurt it.) A stored value that is not a list counts as empty, with a
    warning. A payload with nothing usable changes nothing.
    """

    id = "fleet.specialized_roles"
    label = "Custom Specialized Role categories"
    kind = "collection"
    files = ("config",)
    paths = (("config", _CATEGORIES_PATH),)
    order = 40
    modes = ("merge", "replace")
    conflicts = True

    def _clean(self, payload):
        """-> ``(categories, problems)``: ``spec_roles.normalize(payload)`` and why the rest
        was left out. At most ``MAX_CUSTOM``, one per title key. Total."""
        if not isinstance(payload, list):
            return [], [f"{self.label}: not a list of categories"]
        categories, first, twice, bad, over = [], {}, [], 0, 0
        for _index, _entry, category, key, verdict in _judged(payload):
            if verdict == _KEPT:
                first[key] = category["title"]
                categories.append(category)
            elif verdict == _TWICE:
                if first[key] not in twice:     # at most MAX_CUSTOM different lines
                    twice.append(first[key])
            elif verdict == _OVER:
                over += 1
            else:
                bad += 1
        problems = [f"{self.label} - {title}: listed twice, the first is kept"
                    for title in twice]
        if bad:
            odd = _count(bad, "entry that is not a usable category",
                         "entries that are not usable categories")
            problems.append(f"{self.label}: {odd} left out")
        if over:
            problems.append(f"{self.label}: only the first {spec_roles.MAX_CUSTOM} categories "
                            f"are kept ({over} left out)")
        return categories, problems

    def export(self, state, ctx):
        stored = get_path(getattr(state, "config", None), _CATEGORIES_PATH)
        return spec_roles.normalize(stored) or ABSENT

    def export_notes(self, state, ctx=None) -> list:
        """Lines for the builder, ``"<title>: left out (<reason>)"``: the categories of my
        list that ``export`` leaves out - one ``spec_roles`` cannot use (no visible title,
        or neither a hull nor a tag), one whose title is used earlier in the list, and
        whatever is past the first ``MAX_CUSTOM``. Entries that are not dicts are no
        categories and get no line. Never raises; at most ``MAX_NOTES`` lines and
        ``"and <n> more"``."""
        try:
            stored = get_path(_view(state), _CATEGORIES_PATH)
            if not isinstance(stored, list):
                return []
            lines = []
            for _index, entry, category, _key, verdict in _judged(stored):
                if verdict == _KEPT or not isinstance(entry, dict):
                    continue
                if verdict == _TWICE:
                    shown, why = category["title"], "its title is used earlier in the list"
                elif verdict == _OVER:
                    shown = category["title"]
                    why = f"only the first {spec_roles.MAX_CUSTOM} categories are kept"
                else:
                    shown = _stored_title(entry)
                    why = ("it names no hull and no tag" if shown
                           else "it has no visible title")
                    shown = shown or "a category without a title"
                lines.append(f"{shown}: left out ({why})")
            return _capped(lines)
        except Exception:
            return []

    def validate(self, payload, ctx):
        categories, problems = self._clean(payload)
        return (categories or ABSENT), problems

    def records(self, payload) -> list:
        return [Record(category["title"], category["title"], _category_detail(category))
                for category in self._clean(payload)[0]]

    def summary(self, payload) -> str:
        return _count(len(self._clean(payload)[0]), "category", "categories")

    def lines(self, payload) -> list:
        return [(category["title"], _category_detail(category))
                for category in self._clean(payload)[0]]

    def subset(self, payload, keys):
        wanted = _key_set(keys)
        return [category for category in self._clean(payload)[0]
                if category["title"] in wanted] or ABSENT

    def apply(self, state, payload, mode, conflict, ctx) -> ItemResult:
        result = ItemResult(self.id)
        incoming, _ = self._clean(payload)      # new dicts and lists: mine to store
        if not incoming:                        # nothing usable never blanks the list
            return result
        stored = get_path(_view(state), _CATEGORIES_PATH)
        if isinstance(stored, list):
            new = list(stored)                  # my entries AS STORED, every one of them
        else:
            new = []
            if stored is not None:
                result.warnings.append(f"{self.label}: your saved list could not be read "
                                       f"and counts as empty")
        mine = {}                               # title key -> (place in my list, as it reads)
        past = {}                               # usable categories past the twelfth, by key
        for index, _entry, category, key, verdict in _judged(new):
            if verdict == _KEPT:
                mine[key] = (index, category)
            elif verdict == _OVER:
                past.setdefault(key, category["title"])

        if mode == "replace":
            arriving = set()
            for category in incoming:
                title, key = category["title"], spec_roles.title_key(category["title"])
                arriving.add(key)
                held = mine.get(key)
                if held is None:
                    result.added.append(title)
                elif held[1] == category:
                    result.unchanged.append(title)
                else:
                    result.updated.append(title)
            result.removed.extend(category["title"] for key, (_index, category) in mine.items()
                                  if key not in arriving)
            # the categories past the twelfth are gone too, though the Fleet tab never showed them
            result.removed.extend(title for key, title in past.items()
                                  if key not in arriving and key not in mine)
            new = incoming
            changed = bool(result.added or result.updated or result.removed) or new != stored
        else:
            for category in incoming:
                title, key = category["title"], spec_roles.title_key(category["title"])
                held = mine.get(key)
                if held is not None:
                    if held[1] == category:
                        result.unchanged.append(title)
                    elif conflict == "mine":
                        result.skipped.append(title)
                    else:
                        new[held[0]] = category
                        mine[key] = (held[0], category)
                        result.updated.append(title)
                elif len(mine) < spec_roles.MAX_CUSTOM:
                    mine[key] = (len(new), category)
                    new.append(category)
                    result.added.append(title)
                else:
                    result.skipped.append(title)
                    result.warnings.append(f"{title}: skipped (at most "
                                           f"{spec_roles.MAX_CUSTOM} custom categories)")
            changed = bool(result.added or result.updated)

        if changed:
            set_path(_writable(state), _CATEGORIES_PATH, new)
            result.status = "applied"
        return result


ITEMS = (_BuiltinPresetsItem(), _RolePresetsItem(), _TemplatesItem(),
         _SpecRolesItem())                      # in ``order``
