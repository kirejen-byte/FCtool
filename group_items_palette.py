"""Group configuration: the colour-palette item (spec sections 6.1 and 11).

``palette.colors``  the eight role colours of ``ui_palette.json``

A group file carries ``{"name": ..., "roles": {window, panel, field, text, muted, accent,
border, selection}}``. An import replaces the palette file whole - after the palette has
passed every contrast rule (``palette.validate``); one that fails a rule is never written,
and its failing rules are what the importer is told. The palette takes effect at the next
start (``palette.install``, from the boot hook): nothing is re-themed here.

Rules of this module
--------------------
* EXPORT is the palette the app runs with: the stored one when the file passes the rules,
  the stock one otherwise (no file, a file the app refuses). Stock is exported too - an
  importer gets the builder's look, not the builder's differences.
* The exported ``name`` is the name of the preset the colours equal, else ``"Custom"`` -
  never the name stored in the builder's file, which is free text someone typed.
* "The same palette" is judged on the colours IN EFFECT, not on the file: importing the
  stock palette into a setup with no palette file writes nothing, and so does a file that
  differs from the stored one only in its name.
* A payload with no colour in it is nothing to apply - it never means "back to stock"
  (absent means untouched).
* The name a file carries is clamped by ``palette.clean_name`` before it is written, as
  ``palette.save`` clamps it: the palette file can never outgrow what ``palette.load`` reads.
* ``validate`` builds a clean payload from scratch and never raises; ``apply`` checks what
  it is given once more, so a payload that skipped validation still cannot write a palette
  that fails a rule.

Pure: stdlib, ``palette`` and ``group_items`` only. No Tk, no ``ui_theme``, no ``fc_gui`` -
this module runs in the boot hook, BEFORE the palette is installed onto ``ui_theme``.
"""

import palette
from group_items import ABSENT, Item, ItemResult, Record

FILE_VERSION = 1                        # the "version" palette.save writes

_ROW = "colors"                         # the one tickable row: a palette travels whole
_NO_COLOURS = "no colours in it"


def _key_set(keys) -> set:
    if isinstance(keys, str):
        return {keys}
    try:
        return {key for key in keys if isinstance(key, str)}
    except TypeError:
        return set()


def _roles_in_effect(stored):
    """The eight role colours the app runs with for a stored palette file (the parsed
    ``ui_palette.json``, or ``None``): the file's when it passes every rule, else the stock
    ones - exactly what ``palette.load`` decides at boot. Always a new dict. Total."""
    if isinstance(stored, dict):
        roles, _problems = palette.validate(stored.get("roles"))
        if roles is not None:
            return roles
    return dict(palette.DEFAULT)


class _PaletteItem(Item):
    """``{"name": str, "roles": {the eight roles: "#rrggbb"}}`` - one row, all or nothing.

    A palette is tuned as a whole (every text role against every background): half of
    one group's palette on half of another's is a palette nobody checked by eye. The
    builder changes single colours in the palette editor, on the draft.
    """

    id = "palette.colors"
    label = "Colour palette"
    kind = "value"
    files = ("palette",)
    paths = (("palette", ()),)          # the whole file
    order = 10

    def _clean(self, payload):
        """-> ``(clean payload | ABSENT, problems)``. Total.

        ``ABSENT`` without a rule sentence for a payload that holds no colour at all;
        ``ABSENT`` with the failing rules for one that does and may not be applied."""
        if not isinstance(payload, dict):
            return ABSENT, [f"{self.label}: not a palette"]
        raw = payload.get("roles")
        if not isinstance(raw, dict) or not any(role in raw for role in palette.ROLES):
            return ABSENT, [f"{self.label}: {_NO_COLOURS}"]
        roles, problems = palette.validate(raw)
        if roles is None:
            return ABSENT, [f"{self.label}: {sentence}" for sentence in problems]
        name = palette.clean_name(payload.get("name")) or palette.preset_name(roles)
        return {"name": name, "roles": roles}, []

    def export(self, state, ctx):
        roles = _roles_in_effect(getattr(state, "palette", None))
        return {"name": palette.preset_name(roles), "roles": roles}

    def validate(self, payload, ctx):
        return self._clean(payload)

    def records(self, payload) -> list:
        clean, _problems = self._clean(payload)
        return [] if clean is ABSENT else [Record(_ROW, "Colours", clean["name"])]

    def summary(self, payload) -> str:
        clean, _problems = self._clean(payload)
        return "" if clean is ABSENT else clean["name"]

    def lines(self, payload) -> list:
        clean, _problems = self._clean(payload)
        if clean is ABSENT:
            return []
        return [("Palette", clean["name"])] + [
            (palette.ROLE_LABELS[role], clean["roles"][role]) for role in palette.ROLES]

    def subset(self, payload, keys):
        clean, _problems = self._clean(payload)
        return clean if clean is not ABSENT and _ROW in _key_set(keys) else ABSENT

    def apply(self, state, payload, mode, conflict, ctx) -> ItemResult:
        result = ItemResult(self.id)
        clean, problems = self._clean(payload)      # checked again, whatever the caller did
        if clean is ABSENT:
            raw = payload.get("roles") if isinstance(payload, dict) else None
            if isinstance(raw, dict) and any(role in raw for role in palette.ROLES):
                # There IS a palette in it, and it fails a rule: say which.
                result.status = "skipped"
                result.skipped.append(_ROW)
                result.warnings.extend(problems)
            return result
        current = _roles_in_effect(state.palette)
        if current == clean["roles"]:
            result.unchanged.append(_ROW)
            return result
        state.palette = {"version": FILE_VERSION, "name": clean["name"],
                         "roles": dict(clean["roles"])}
        result.updated.append(_ROW)
        result.notes.append(f"Palette: {palette.preset_name(current)} -> {clean['name']}")
        return result.settle()


ITEMS = (_PaletteItem(),)
