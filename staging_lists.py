"""Staging-system list helpers (pure: stdlib only, no Tk, no ``fc_gui``).

Moved verbatim out of ``fc_gui.py`` so that the group-configuration items
(``group_items_map``), which run in the boot hook before any window exists, can
share the Jump Range tab's own add / move / remove rule. ``fc_gui`` imports both
names back, so ``fc_gui.mutate_staging_lists`` and
``fc_gui.extract_staging_system_names`` are these same objects.
"""


def mutate_staging_lists(friendly: list[str], hostile: list[str],
                         action: str, name: str,
                         target: str = "friendly") -> tuple[list[str], list[str]]:
    """Pure, Tk-free mutator for the two staging-system lists.

    Returns NEW (friendly, hostile) lists; inputs are never mutated in place so
    this is safe to unit-test and to call from the UI.

    action:
      - "add":    add ``name`` to ``target`` ("friendly"/"hostile"). De-dupes
                  case-insensitively within the target list. If ``name`` already
                  exists (case-insensitively) in the OTHER list it is MOVED:
                  removed from the other list and appended to the target.
      - "remove": remove ``name`` (case-insensitive) from ``target`` only.

    ``name`` is normalized by stripping surrounding whitespace; the stripped
    display text is what gets stored (canonicalization beyond this is the
    caller's job and must not block list mutation). Blank names are ignored.
    """
    # Work on copies so callers' lists are never mutated.
    friendly = list(friendly)
    hostile = list(hostile)

    clean = (name or "").strip()
    if not clean:
        return friendly, hostile

    lower = clean.lower()

    def _without(lst: list[str]) -> list[str]:
        return [s for s in lst if s.strip().lower() != lower]

    if action == "remove":
        if target == "hostile":
            hostile = _without(hostile)
        else:
            friendly = _without(friendly)
        return friendly, hostile

    if action == "add":
        # Remove any existing copy from BOTH lists (handles de-dupe within the
        # target list and a move from the other list), then append to target.
        friendly = _without(friendly)
        hostile = _without(hostile)
        if target == "hostile":
            hostile.append(clean)
        else:
            friendly.append(clean)
        return friendly, hostile

    return friendly, hostile


def extract_staging_system_names(staging_system, *lists) -> list[str]:
    """Pure, Tk-free extractor for the systems worth pre-warming.

    Collects system names the user actually cares about: the single configured
    ``staging_system`` plus any number of staging *lists*. Each list item may be
    a plain name string or a ``{"id": ..., "name": ...}`` dict (the jump-range
    staging lists are persisted as either form, depending on when they were
    saved), so this normalizes both shapes to a bare name.

    Blanks are skipped and names are de-duped case-insensitively while
    preserving first-seen order and the original casing of the first
    occurrence. Returns a NEW list; inputs are never mutated, so this is safe to
    unit-test and to call from the prewarm worker.
    """
    def _name_of(item) -> str:
        if isinstance(item, dict):
            return str(item.get("name", "") or "").strip()
        return str(item or "").strip()

    out: list[str] = []
    seen: set[str] = set()

    def _add(raw):
        name = _name_of(raw)
        if not name:
            return
        key = name.lower()
        if key in seen:
            return
        seen.add(key)
        out.append(name)

    _add(staging_system)
    for lst in lists:
        for item in (lst or ()):
            _add(item)
    return out
