"""Group configuration: the Jump Range and Map items (spec section 6.4).

``jump.friendly_stagings`` / ``jump.hostile_stagings``
    The two staging-system lists under ``config["jump_range"]``. Names are checked against
    the system table (``ctx.system_ids``) and stored in its canonical spelling; a name the
    table does not know - a wormhole system, a typo - is left out and reported. A system
    lives in ONE of the two lists, so landing a name in one list takes it out of the other
    (the Jump Range tab's own rule, ``staging_lists.mutate_staging_lists``): each item
    therefore declares BOTH paths. The hostile item runs after the friendly one, so a system
    that a file names in both lists ends hostile - and ``reconcile`` (the engine's pass over
    one file's payloads) takes such a name out of the friendly payload beforehand, so that a
    second apply of the same file changes nothing.

``map.projection``
    The map's threat / friendly projection settings: five typed fields under
    ``config["map"]``. It is in the ``map`` category, which runs after ``jump``.

Nothing else under ``jump_range`` or ``map`` is read or written - in particular not the map
camera and not the other map layers.

Pure: stdlib, ``group_items``, ``map_overlays`` and ``staging_lists`` only. No Tk, no
``ui_theme``, no ``fc_gui`` - this module runs in the boot hook.
"""

import re

import map_overlays
from group_items import ConfigFieldsItem, Field, ItemResult, NameListItem, get_path, set_path
from staging_lists import mutate_staging_lists


STAGING_PATHS = {
    "friendly": ("jump_range", "friendly_staging_systems"),
    "hostile": ("jump_range", "hostile_staging_systems"),
}
_OTHER = {"friendly": "hostile", "hostile": "friendly"}

# The map stores a threat class as its menu label minus the trailing "(6.0 ly)"; this is
# the pattern map_tab strips it with (map_tab itself is a Tk module and cannot be imported).
_LY_SUFFIX = re.compile(r"\s*\([^()]*ly\)\s*$")


def threat_classes() -> tuple:
    """The threat ship classes the map offers, as it stores them in ``map.threat_ship``:
    the labels of ``map_overlays.threat_options()`` without their ``" (x.x ly)"`` tail.
    Total: an unusable option table gives ``()``, and then no class is accepted.
    """
    # An empty ship table on purpose: the class names do not depend on the ranges, and the
    # default table lives in jump_range, which would pull the network stack into the boot.
    try:
        options = list(map_overlays.threat_options({}))
    except Exception:
        return ()
    classes = []
    for option in options:
        label = option[0] if isinstance(option, (tuple, list)) and option else None
        if not isinstance(label, str):
            continue
        name = _LY_SUFFIX.sub("", label).strip()
        if name and name not in classes:
            classes.append(name)
    return tuple(classes)


def _key(name) -> str:
    """How ``mutate_staging_lists`` compares two names."""
    return name.strip().lower()


def _stored_names(stored):
    """A stored staging list as ``(names, readable, dropped)``.

    A missing list is empty. Anything that is not a list is not readable and counts as
    empty. Entries that are not non-blank strings are dropped and counted.
    """
    if stored is None:
        return [], True, 0
    if not isinstance(stored, list):
        return [], False, 0
    names = [name for name in stored if isinstance(name, str) and name.strip()]
    return names, True, len(stored) - len(names)


class StagingListItem(NameListItem):
    """One of the two staging lists. Build it with ``target="friendly"`` or ``"hostile"``;
    ``path`` follows from the target.

    merge: a name that is not in the target list is added through
    ``mutate_staging_lists(..., "add", ...)``, which also takes it out of the other list; a
    name that is already there (and only there) is left exactly as it is - spelling and
    position. replace: the target list becomes the file's names in file order, each of them
    is taken out of the other list, and the names that left the target are ``removed``.
    Every name that came out of the other list gives a note,
    ``"<name> moved from <other> to <target>"``, and is ``added`` when the target did not
    hold it and ``updated`` when it did (it sat in both lists) - in both modes.

    A stored list that is not a list counts as empty, and entries that are not names are
    dropped - with a warning. The target list is rewritten without such entries even when
    the file adds no name to it; the other list is rewritten only when a name actually left
    it.

    The payload is cleaned again here. A name that no longer resolves (the system table
    failed to load) is left out with a warning, and when no name resolves at all nothing is
    written and the item is ``skipped``.
    """

    modes = ("merge", "replace")
    unit = ("system", "systems")
    paths = tuple(("config", path) for path in STAGING_PATHS.values())
    target = "friendly"

    def __init__(self, **attrs):
        super().__init__(**attrs)
        if self.target not in STAGING_PATHS:
            raise ValueError(f"{self.id}: target must be one of {tuple(STAGING_PATHS)}")
        self.path = STAGING_PATHS[self.target]

    def resolve(self, name, ctx):
        """The system's canonical name, or ``None`` for a name the table does not know."""
        hit = ctx.system_ids(name)
        return hit[0] if isinstance(hit, tuple) and hit else None

    def _damage_line(self, which, readable, dropped) -> str:
        """The warning for a damaged stored list, or ``""``. ``which`` is ``""`` for this
        item's own list and ``"hostile "`` / ``"friendly "`` for the other one."""
        if not readable:
            return (f"{self.label}: your saved {which}list could not be read "
                    f"and counts as empty")
        if dropped:
            odd = "1 entry that is not a name" if dropped == 1 else \
                f"{dropped} entries that are not names"
            return f"{self.label}: your saved {which}list held {odd}"
        return ""

    def apply(self, state, payload, mode, conflict, ctx) -> ItemResult:
        result = ItemResult(self.id)
        incoming, problems = self._clean(payload, ctx)
        result.warnings.extend(problems)        # what this second cleaning left out
        if not incoming:                        # nothing usable never blanks a list
            if problems:                        # (an empty list is simply nothing to do)
                result.status = "skipped"
            return result
        target, other = self.target, _OTHER[self.target]

        config = state.config if isinstance(state.config, dict) else {}
        before, damage = {}, {}
        for kind, path in STAGING_PATHS.items():
            names, readable, dropped = _stored_names(get_path(config, path))
            before[kind] = names
            damage[kind] = self._damage_line("" if kind == target else f"{kind} ",
                                             readable, dropped)
        if damage[target]:
            result.warnings.append(damage[target])

        spelling = {}                           # compare key -> my spelling in the target
        for name in before[target]:
            spelling.setdefault(_key(name), name)
        in_other = {_key(name) for name in before[other]}
        friendly, hostile = list(before["friendly"]), list(before["hostile"])

        if mode == "replace":
            for name in incoming:
                mine = spelling.get(_key(name))
                if mine is None:
                    result.added.append(name)
                elif mine == name and _key(name) not in in_other:
                    result.unchanged.append(name)
                else:                           # another spelling, or it sat in both lists
                    result.updated.append(name)
                if _key(name) in in_other:
                    friendly, hostile = mutate_staging_lists(
                        friendly, hostile, "remove", name, other)
            arriving = {_key(name) for name in incoming}
            result.removed.extend(name for key, name in spelling.items()
                                  if key not in arriving)
            after = {"friendly": friendly, "hostile": hostile}
            after[target] = list(incoming)
        else:
            for name in incoming:
                key = _key(name)
                if key in spelling and key not in in_other:
                    result.unchanged.append(name)       # "add" would move it to the end
                    continue
                friendly, hostile = mutate_staging_lists(
                    friendly, hostile, "add", name, target)
                (result.updated if key in spelling else result.added).append(name)
            after = {"friendly": friendly, "hostile": hostile}

        result.notes.extend(f"{name} moved from {other} to {target}"
                            for name in incoming if _key(name) in in_other)

        for kind in (target, other):
            if after[kind] == before[kind] and not (kind == target and damage[target]):
                continue                        # an untouched damaged OTHER list stays as it is
            if state.config is None:            # a missing config.json: created on first write
                state.config = {}
            set_path(state.config, STAGING_PATHS[kind], after[kind])
            result.status = "applied"
            if kind == other and damage[other]:
                result.warnings.append(damage[other])
        return result


FRIENDLY_ID, HOSTILE_ID = "jump.friendly_stagings", "jump.hostile_stagings"
# What `reconcile` works on: the engine hands it - and snapshots for its undo - only these
# payloads (group_items.validate_bundle), not every payload of the file.
RECONCILES = (FRIENDLY_ID, HOSTILE_ID)


def reconcile(payloads) -> list:
    """The file-level pass over the clean payloads of ONE file (``{item id: payload}``).

    A file that names a system in BOTH staging lists would never settle: on every apply the
    friendly item pulls the system back and the hostile item moves it again. So such a name
    is taken out of the friendly payload - it counts as hostile (spec 6.4) - with one line
    each, ``"<name> is in both staging lists - kept as hostile"``; a friendly payload emptied
    that way is removed from ``payloads``. Names are compared the way the lists themselves
    compare them (``_key``); the line carries the hostile list's spelling.

    Changes ``payloads`` in place and returns the problem lines. Total: either id may be
    absent, anything that is not a list of names is left exactly as it is, never raises.
    """
    try:
        friendly, hostile = payloads.get(FRIENDLY_ID), payloads.get(HOSTILE_ID)
        if not isinstance(friendly, list) or not isinstance(hostile, list):
            return []
        kept_as = {}                            # compare key -> the hostile list's spelling
        for name in hostile:
            if isinstance(name, str):
                kept_as.setdefault(_key(name), name)
        stay, lines = [], []
        for name in friendly:
            if isinstance(name, str) and _key(name) in kept_as:
                line = f"{kept_as[_key(name)]} is in both staging lists - kept as hostile"
                if line not in lines:
                    lines.append(line)
            else:
                stay.append(name)
        if not lines:
            return []
        if stay:
            payloads[FRIENDLY_ID] = stay
        else:
            del payloads[FRIENDLY_ID]
        return lines
    except Exception:
        return []


ITEMS = (
    StagingListItem(id=FRIENDLY_ID, label="Friendly staging systems",
                    order=10, target="friendly"),
    StagingListItem(id=HOSTILE_ID, label="Hostile staging systems",
                    order=20, target="hostile"),
    ConfigFieldsItem(
        id="map.projection", label="Threat and friendly projection", order=10,
        never_saved=("The map camera and the other map layers",),
        fields=(
            Field("threat_ship", "Threat ship class", "enum", ("map", "threat_ship"),
                  choices=threat_classes()),
            Field("show_threat", "Show threat overlay", "bool", ("map", "layers", "threat")),
            Field("friendly_enabled", "Show friendly projection", "bool",
                  ("map", "threat_friendly_enabled")),
            Field("hostile_excluded", "Hostile stagings left out of the threat overlay",
                  "names", ("map", "threat_staging_excluded")),
            Field("friendly_excluded", "Friendly stagings left out of the projection",
                  "names", ("map", "threat_friendly_excluded")),
        )),
)
