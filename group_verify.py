"""Group configuration - can this character reach the group's market structure?
(pure: stdlib and ``group_bundle`` only; no Tk, no ``fc_gui``, no network, no disk, no log).

A group file may name a default market structure. The importer's own character may not be
able to dock there, so the import never writes the structure: ``settings.market_staging``
(``group_items_settings``) queues ONE task in the feature's side file instead::

    {"kind": "verify_structure", "structure_id": 1000000000001, "name": "Example Keepstar",
     "system_id": 30000001, "attempts": 0}

After start-up, once a character is signed in, the app asks ESI - one call,
``GET /universe/structures/{id}/`` - whether that character can see the structure. This
module is the decision around that call and nothing else; the caller owns the thread, the
ESI client, the side file and the Settings tab:

* :func:`verify_structure` - the task and the answer of the one call -> a :class:`Verdict`;
* :func:`after_verdict` - what is left of the task (``None`` = finished);
* :func:`attention_reason` - the sentence the Settings tab shows, or ``None``;
* :func:`unanswered` - the verdict of a call that told us nothing (``retry`` or
  ``give_up``), for a caller that failed before it could ask.

Outcomes: ``ok`` and ``renamed`` (reachable - ``Verdict.name`` is the name to store),
``no_access`` (not a usable task, 403 / 404, or a structure outside the group's system),
``no_character`` (nobody signed in with the structure scope: ask again after a sign-in),
``retry`` (not answered: ask again at the next start) and ``give_up`` (not answered
``MAX_ATTEMPTS`` times).

The task comes from a file on disk and the name from the network, so every function is
TOTAL: garbage in gives a verdict, ``None`` or the task as it was - never an exception. The
caller's task is never changed. The rules it mirrors:

* an id is a whole number above 0 that fits 64 bits, as the producer writes it; ``True`` is
  not id 1, and digits in a string are not an id (the producer never writes text);
* ``attempts`` counts the way ``group_apply`` keeps it across a re-import: a plain int
  above 0, anything else is 0;
* a name is display text from someone else: one line, clipped to the producer's own bound
  (``group_items_settings.MAX_PLACE_NAME``), and beyond ``group_bundle.clean_text`` it
  loses every control, format (zero-width, bidi override) and line / paragraph separator
  character - the visible-label rule of ``group_bundle.clean_tag``. BOTH names go through
  it before they are compared, so the stored name and ESI's can only differ by what a
  reader would see.
"""

import unicodedata
from dataclasses import dataclass

from group_bundle import clean_text, has_visible

MAX_ATTEMPTS = 5         # unanswered calls before the user is told to pick for themselves
NAME_CLIP = 120          # group_items_settings.MAX_PLACE_NAME - the stored name's own bound
OUTCOMES = ("ok", "renamed", "no_access", "no_character", "retry", "give_up")

_MAX_ID = 2 ** 63 - 1                   # every EVE id fits a signed 64-bit integer
_NAME_SCAN = 1000                       # characters of a raw name looked at before the clip
_NAME_REMOVED_CATEGORIES = ("Cc", "Cf", "Zl", "Zp")
_REFUSED = (403, 404)                   # no docking access / no such structure
_FINISHED = frozenset(["ok", "renamed", "no_access", "give_up"])

_WRONG_SYSTEM = "it is not in the group's staging system"
_UNNAMED = "the group's market structure"
_NO_ACCESS = ("Your character cannot reach %s (no docking access, or it no longer exists)."
              " Pick the market structure yourself.")
_NOT_USABLE = ("The group's market structure%s cannot be used: %s. Pick the market structure"
               " yourself.")
_NO_CHARACTER = ("The group's market structure%s will be set once your main character is"
                 " signed in with market access.")
_GIVE_UP = "The group's market structure%s could not be checked. Pick it yourself."


@dataclass(frozen=True)
class Verdict:
    outcome: str        # one of OUTCOMES
    name: str = ""      # ok / renamed: the structure's name, clean - the one to store
    message: str = ""   # no_access: why, when it is not a plain 403 / 404


def _whole(value):
    """``value`` as an id - a whole number above 0 that fits 64 bits - or ``None``."""
    if type(value) is float and value.is_integer():     # never true for inf / nan
        value = int(value)
    if type(value) is not int or not 0 < value <= _MAX_ID:
        return None
    return value


def _name(value) -> str:
    """``value`` as a structure name fit to store and to show, or ``""``."""
    if not isinstance(value, str):
        return ""
    text = clean_text(value, _NAME_SCAN)
    text = "".join(ch for ch in text
                   if unicodedata.category(ch) not in _NAME_REMOVED_CATEGORIES)
    text = clean_text(text, NAME_CLIP)      # again: the removal may have uncovered whitespace
    return text if has_visible(text) else ""


def _attempts(task) -> int:
    tried = task.get("attempts") if isinstance(task, dict) else None
    return tried if type(tried) is int and tried > 0 else 0


def _outcome(verdict) -> str:
    outcome = verdict.outcome if isinstance(verdict, Verdict) else None
    return outcome if isinstance(outcome, str) else ""


def _why(verdict) -> str:
    """A ``no_access`` verdict's own reason as half a sentence - one line, nothing hidden,
    no full stop of its own - or ``""`` when it carries none."""
    return _name(getattr(verdict, "message", None)).rstrip(". ")


def unanswered(task) -> Verdict:
    """The call told us nothing: once more at the next start (``retry``), or enough
    (``give_up``, at the ``MAX_ATTEMPTS``-th unanswered call). The ONE owner of that
    choice: a caller whose own code failed before :func:`verify_structure` could decide
    uses it too, so that a failure at every start still runs out of attempts. Total."""
    return Verdict("retry" if _attempts(task) + 1 < MAX_ATTEMPTS else "give_up")


def verify_structure(task, *, authed, has_scope, get_structure) -> Verdict:
    """Decide one ``verify_structure`` task.

    ``get_structure(structure_id) -> (body | None, status)`` is called at most ONCE, with
    the id as an int, and only for a usable task and a character that is signed in
    (``authed``) with the structure scope (``has_scope``). Whatever it raises or returns
    that is not an answer counts as "not answered".
    """
    structure_id = _whole(task.get("structure_id")) if isinstance(task, dict) else None
    if structure_id is None:
        return Verdict("no_access")
    if not authed or not has_scope:
        return Verdict("no_character")
    try:
        body, status = get_structure(structure_id)
    except Exception:
        return unanswered(task)
    if type(status) is not int:
        return unanswered(task)
    if status in _REFUSED:
        return Verdict("no_access")
    if status != 200 or not isinstance(body, dict):
        return unanswered(task)
    name = _name(body.get("name"))
    if not name:
        return unanswered(task)
    wanted = _whole(task.get("system_id"))
    if wanted is not None:              # a task without a system is not checked against one
        found = _whole(body.get("solar_system_id"))
        if found is None:               # ESI always sends it: this is not a whole answer
            return unanswered(task)
        if found != wanted:
            return Verdict("no_access", "", _WRONG_SYSTEM)
    if name == _name(task.get("name")):
        return Verdict("ok", name)
    return Verdict("renamed", name)


def after_verdict(task, verdict):
    """What stays queued: ``None`` when the task is finished (``ok``, ``renamed``,
    ``no_access``, ``give_up``), a COPY with one more attempt for ``retry``, and the task as
    it was for ``no_character`` - and for anything that is not a verdict, which taught us
    nothing. Something that is not a task is finished."""
    if not isinstance(task, dict):
        return None
    outcome = _outcome(verdict)
    if outcome in _FINISHED:
        return None
    if outcome == "retry":
        return dict(task, attempts=_attempts(task) + 1)
    return task


def attention_reason(task, verdict):
    """The sentence for the Settings tab (section Market), or ``None`` when the verdict
    needs none (``ok``, ``renamed``, ``retry``, or not a verdict). Always one line; a task
    without a usable name is "the group's market structure".

    A ``no_access`` verdict that says why (``Verdict.message`` - today: the structure is
    in another system) is NOT "no docking access, or it no longer exists": the character
    reached it. That sentence names the reason instead."""
    outcome = _outcome(verdict)
    if outcome not in ("no_access", "no_character", "give_up"):
        return None
    name = _name(task.get("name")) if isinstance(task, dict) else ""
    quoted = "'%s'" % (name,) if name else ""
    if outcome == "no_access":
        why = _why(verdict)
        if why:
            return _NOT_USABLE % (" " + quoted if quoted else "", why)
        return _NO_ACCESS % (quoted or _UNNAMED,)
    template = _NO_CHARACTER if outcome == "no_character" else _GIVE_UP
    return template % (" " + quoted if quoted else "",)
