"""Role Tracker preset helpers (pure: stdlib only, no Tk, no ``fc_gui``).

``BUILTIN_PRESETS``, ``LETTER_MAX``, ``preset_label``, ``parse_preset_fields`` and
``default_presets`` were moved verbatim out of ``fc_gui.py`` so that the
group-configuration items (``group_items_fleet``), which run in the boot hook
before any window exists, share the Fleet tab's own preset rules. ``fc_gui``
keeps its five private names (``_ROLE_BUILTIN_PRESETS``, ...) as aliases of
these same objects.

``CUSTOM_MAX``, ``clean_preset`` and ``label_key`` are what the items add: the
size of the custom row, one untrusted entry made clean (or dropped), and the
way two preset labels are compared.
"""

# The Role Tracker's BUILT-IN preset buttons (row 1), each
# ``(label, letter, title, cap)``. The owner can edit them in place (right-click
# a button); an edited set is persisted as ``config["default_role_presets"]``
# and ``default_presets`` decides at Fleet-tab build time which list wins.
# "Restore built-in" puts the matching tuple from HERE back.
BUILTIN_PRESETS = (
    ("C-Cyno", "c", "Cyno", None),
    ("D-Dictors", "d", "Dictors", None),
    ("F-Fax 3", "f", "FAX", 3),
    ("Z-Defenders-8", "z", "Defenders", 8),
    ("1-Dreads-10", "1", "Dreads", 10),
)

# Mirrors the role slot's key Entry (``width=2``): a preset key is 1-2 chars.
LETTER_MAX = 2

# The custom preset row (row 2) holds at most this many presets - the Fleet
# tab's own limit (``_MAX_CUSTOM_PRESETS``).
CUSTOM_MAX = 8


def preset_label(letter, title, cap) -> str:
    """A role preset's button label, built exactly the way
    ``_save_role_as_preset`` builds it: ``LETTER-Title`` plus ``-cap`` when a
    cap is set."""
    label = f"{letter.upper()}-{title}"
    if cap is not None:
        label += f"-{cap}"
    return label


def parse_preset_fields(letter, title, cap_text):
    """Validate the role-preset edit dialog's three fields.

    Returns ``((letter, title, cap), None)`` on success or ``(None, message)``
    naming the first problem: the key must be 1-2 non-blank characters, the
    role name non-blank, the cap blank (no cap) or a positive whole number.
    Surrounding whitespace is stripped from every field."""
    letter = str(letter or "").strip()
    title = str(title or "").strip()
    cap_text = str(cap_text or "").strip()
    if not letter:
        return None, "The key cannot be blank."
    if len(letter) > LETTER_MAX:
        return None, (f"The key must be at most {LETTER_MAX} "
                      "characters.")
    if not title:
        return None, "The role name cannot be blank."
    cap = None
    if cap_text:
        # ``str.isdecimal``, not ``str.isdigit``: the latter is also true for
        # characters such as a superscript two, which ``int`` then refuses with
        # a ValueError. ``int`` reads every decimal digit, ASCII or not.
        if not cap_text.isdecimal() or int(cap_text) <= 0:
            return None, "The cap must be blank or a positive whole number."
        cap = int(cap_text)
    return (letter, title, cap), None


def default_presets(config, builtin=BUILTIN_PRESETS) -> list:
    """The Role Tracker's built-in preset list at Fleet-tab build time.

    Uses ``config["default_role_presets"]`` (written only when the owner edits
    a built-in preset: a list of ``{label, letter, title, cap}`` dicts in the
    built-in order) when it is WELL-FORMED — a list of exactly ``len(builtin)``
    dicts, each with a non-blank string ``letter`` and ``title`` and a ``cap``
    that is None or a positive int. Anything else (missing key, non-list,
    wrong length, one bad entry) falls back to ``builtin`` WHOLESALE — never a
    half-merge. A blank/missing label is rebuilt from the other fields. Never
    raises: a damaged config must not stop the Fleet tab from building."""
    fallback = [tuple(p) for p in builtin]
    try:
        stored = config.get("default_role_presets") if isinstance(
            config, dict) else None
        if not isinstance(stored, list) or len(stored) != len(fallback):
            return fallback
        out = []
        for p in stored:
            if not isinstance(p, dict):
                return fallback
            letter, title, cap = p.get("letter"), p.get("title"), p.get("cap")
            if not isinstance(letter, str) or not letter.strip():
                return fallback
            if not isinstance(title, str) or not title.strip():
                return fallback
            if cap is not None and (isinstance(cap, bool)
                                    or not isinstance(cap, int) or cap <= 0):
                return fallback
            label = p.get("label")
            if not isinstance(label, str) or not label.strip():
                label = preset_label(letter, title, cap)
            out.append((label, letter, title, cap))
        return out
    except Exception:
        return fallback


def clean_preset(raw):
    """One untrusted preset entry as ``{label, letter, title, cap}``, or ``None``.

    ``raw`` must be a dict whose ``letter`` and ``title`` are text and whose
    ``letter`` / ``title`` / ``cap`` pass the edit dialog's own rule
    (``parse_preset_fields``, the cap handed over as the text the dialog would
    hold - so a missing or ``None`` cap is "no cap", and a bool, a float or
    anything else that is not a positive whole number is refused). The result
    is a NEW dict with exactly those four keys: the fields stripped, the cap
    ``None`` or a positive ``int``, and a blank, missing or non-text ``label``
    rebuilt by ``preset_label``. Total: never raises, never edits ``raw``."""
    try:
        if not isinstance(raw, dict):
            return None
        letter, title, cap = raw.get("letter"), raw.get("title"), raw.get("cap")
        if not isinstance(letter, str) or not isinstance(title, str):
            return None
        parsed, _error = parse_preset_fields(
            letter, title, "" if cap is None else str(cap))
        if parsed is None:
            return None
        letter, title, cap = parsed
        label = raw.get("label")
        label = label.strip() if isinstance(label, str) else ""
        if not label:
            label = preset_label(letter, title, cap)
        return {"label": label, "letter": letter, "title": title, "cap": cap}
    except Exception:
        return None


def label_key(label) -> str:
    """The key two preset labels are compared by: the label itself.

    Mirrors ``FCToolGUI._role_preset_label_taken`` (and the checks in
    ``_save_role_as_preset`` / ``_rename_custom_preset``), which compare labels
    with a plain ``==`` - exact, case-sensitive, not stripped. So ``C-Cyno``
    and ``c-cyno`` are two presets here exactly as they are on the Fleet tab.
    Anything that is not text has the key ``""``."""
    return label if isinstance(label, str) else ""
