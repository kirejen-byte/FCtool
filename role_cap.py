"""Role Tracker cap enforcement AFTER the fact — the pure decision half.

A Role Tracker group ("slot") can carry an optional cap. Chat routing
(``FCToolGUI._check_role_letters``) consults the cap only when someone is
ADDED: a full group is skipped and the pilot overflows into the next group
with the same letter. Lowering a cap once people are already listed used to
change nothing but the counter's colour, so a "FAX 1" capped at 3 could
silently keep 5 pilots. The owner's rule: changing the cap after the fact
moves the BOTTOM pilots (the most recently added) either to the next
identical group — the next slot with the SAME LETTER that has room — or out
of the tracker when none has room.

This module owns the decisions so they are testable without Tk:

* :func:`effective_cap` — what the cap widgets mean (``None`` = uncapped);
* :func:`evictions`     — how many pilots must leave;
* :func:`has_room`      — the SAME room test the chat routing uses;
* :func:`find_overflow_slot` — where one evictee goes.

The Tk half (destroying and rebuilding person rows, logging, repainting the
counters) lives in ``FCToolGUI._apply_role_cap``. Stdlib only, no Tk, no
fc_gui import.
"""
from __future__ import annotations

from typing import Callable, Optional, Sequence


def effective_cap(enabled: bool, text: object) -> Optional[int]:
    """The cap a slot actually enforces, or ``None`` when it enforces none.

    Mirrors the add-time check in ``_add_person_to_slot`` /
    ``_check_role_letters``: the "Cap" box must be ticked AND the entry must
    parse as an integer greater than zero. Unticked, blank, ``0``, negative
    or non-numeric text all mean "no cap" — so they never evict anyone.
    """
    if not enabled:
        return None
    try:
        cap = int(str(text).strip())
    except (TypeError, ValueError):
        return None
    return cap if cap > 0 else None


def evictions(count: int, cap: Optional[int]) -> int:
    """How many of ``count`` listed pilots exceed ``cap`` (never negative).

    ``cap`` is an :func:`effective_cap` result; ``None`` (uncapped) evicts
    nobody, and so does raising a cap to or above the current count.
    """
    if cap is None or cap <= 0:
        return 0
    return max(0, int(count) - cap)


def has_room(count: int, cap: Optional[int]) -> bool:
    """True when a slot holding ``count`` pilots can take one more.

    Same test as the chat routing's overflow scan: no cap, or fewer pilots
    than the cap.
    """
    return cap is None or cap <= 0 or count < cap


def _norm(letter: object) -> str:
    return str(letter or "").strip().lower()


def find_overflow_slot(letters: Sequence[object], current_index: int,
                       letter: object,
                       room: Callable[[int], bool]) -> Optional[int]:
    """Index of the "next identical group" with room, or ``None``.

    ``letters`` is every slot's key letter in ``_role_slots`` order. The
    search starts at the slot AFTER ``current_index`` and wraps round to the
    slots before it, so a later same-letter group is preferred and an earlier
    one is the fallback; the current slot itself is never a candidate. Only
    slots whose letter equals ``letter`` (case-insensitive, whitespace
    stripped) qualify, and of those the first for which ``room(index)`` is
    true wins. A blank ``letter`` matches nothing: a group without a key
    letter has no identical group.
    """
    want = _norm(letter)
    n = len(letters)
    if not want or n == 0:
        return None
    for step in range(1, n):
        i = (current_index + step) % n
        if i == current_index:
            continue
        if _norm(letters[i]) == want and room(i):
            return i
    return None
