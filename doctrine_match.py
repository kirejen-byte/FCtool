"""Group configuration: which doctrines in a group file look like ones the importer has.

A ``.fctg`` group file's doctrines are matched today by EXACT name only, so a pilot with
"Feroxes" who imports the group's "Ferox Fleet" ends up with two near-identical doctrines.
This module finds those pairs, so the Import page can ask what happens to each (design spec
``2026-10-22-doctrine-merge-on-group-import-design.md`` section 4, decision D1).

A doctrine is reduced to a ``Sig``: its name, the hull of every slot (a slot's hull is its
ACTIVE fit's hull - every refit of a slot shares it - or, when the active fit is missing, the
first refit's that resolves) and the content hash of every fit and
refit it names. Two doctrines are **similar** when:

* their names are equal, or loosely equal (``loose_name``: case, blanks, punctuation,
  ``>>`` / ``<<`` and bracketed tickers ignored - a name that is nothing but those never
  matches loosely); OR
* they share at least ``MIN_SHARED_HULLS`` hulls and the shared hulls are at least
  ``HULL_RATIO`` of the hulls the two fly between them (hull SETS: two Ferox slots are one
  hull); OR
* both fly one hull, the same one, and have at least one identical fit.

Pairing is one-to-one (``pair_doctrines``): exact-name pairs first - they are bound by
today's rule - then the rest by score, best first; each side of a pair is used at most once.
Only the FIRST doctrine of a name, on either side, is ever paired: ``merge_share`` takes the
first local doctrine of a name, and a name used twice in a file keeps its first.

Pure and total: stdlib and ``fit_models`` only - no Tk, no ``fc_gui`` - because it runs on
the boot path through ``group_items_fittings``. Garbage in a ``Sig`` or a doctrine is
ignored, never raised.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from fit_models import fit_content_hash


HULL_RATIO = 0.6                    # shared hulls / hulls in union, at least
MIN_SHARED_HULLS = 2                # ... and at least this many shared
_EPSILON = 1e-9                     # 3 of 5 is 0.6, not a float hair under it

_BRACKETED = re.compile(r"\[[^\]]*\]")


def loose_name(name: str) -> str:
    """``name`` with case, blanks, punctuation and bracketed runs ignored.

    Casefolded; every ``[...]`` run (a ticker) dropped; ``>``, ``<`` and every other
    character that is neither alphanumeric nor whitespace dropped; blanks collapsed to one
    space; stripped. ``"[ABCD] >>Ferox<<  Fleet!"`` -> ``"ferox fleet"``. A non-string is
    ``""``.
    """
    if not isinstance(name, str):
        return ""
    text = _BRACKETED.sub(" ", name.casefold())
    text = "".join(ch for ch in text if ch.isalnum() or ch.isspace())
    return " ".join(text.split())


@dataclass(frozen=True)
class Sig:
    """What the detector compares of one doctrine.

    Hashable: ``hull_names`` (display only) takes no part in ``==`` or ``hash``.
    """

    name: str
    hulls: tuple = ()                               # one hull type id per slot, slot order
    fits: frozenset = frozenset()                   # content hashes of every fit and refit
    hull_names: dict = field(default_factory=dict, hash=False,
                             compare=False)         # hull type id -> hull name


def _content_hash(fit):
    try:
        digest = fit_content_hash(fit.parsed)
    except Exception:                               # a hand-edited fit with junk in it
        return None
    return digest if isinstance(digest, str) else None


def sig_of_doctrine(doctrine, fits) -> Sig:
    """The ``Sig`` of ``doctrine`` against the library fits ``fits`` (fit id -> ``Fit``).

    ``fits`` must be a mapping fit id -> ``Fit`` (e.g. the store's internal dict keyed by fit
    id); a non-mapping reads as no fits.

    A slot's hull is the ``hull_type_id`` of the first of its fits that is in ``fits``: its
    active fit (``fit_id``), else - the active fit missing - its first refit that resolves
    (the store guarantees every refit of a slot shares the slot's hull). A fit id that is not
    in ``fits`` is skipped; a slot none of whose fits resolve has no hull here.
    """
    if not isinstance(fits, Mapping):
        fits = {}
    name = getattr(doctrine, "name", "")
    members = getattr(doctrine, "members", None)
    hulls, hashes, hull_names = [], set(), {}
    for member in members if isinstance(members, list) else ():
        active = getattr(member, "fit_id", None)
        refits = getattr(member, "refits", None)
        ids = [active] + (list(refits) if isinstance(refits, list) else [])
        hull_pending = True                         # the slot's hull is not read yet
        for fit_id in ids:
            try:
                fit = fits.get(fit_id)
            except Exception:                       # an unhashable id, a broken mapping
                fit = None
            if fit is None:
                continue
            digest = _content_hash(fit)
            if digest is not None:
                hashes.add(digest)
            if not hull_pending:                    # a later fit: its hash, never a hull
                continue
            hull_pending = False
            hull = getattr(fit, "hull_type_id", None)
            if _is_hull(hull):
                hulls.append(hull)
                hull_name = getattr(fit, "hull_name", None)
                if isinstance(hull_name, str) and hull_name and hull not in hull_names:
                    hull_names[hull] = hull_name
    return Sig(name=name if isinstance(name, str) else "", hulls=tuple(hulls),
               fits=frozenset(hashes), hull_names=hull_names)


@dataclass(frozen=True)
class Pair:
    """A group doctrine and the importer's doctrine it looks like."""

    incoming_index: int                 # position in the file's doctrine list
    incoming_name: str
    local_id: str
    local_name: str
    exact: bool                         # the names are exactly equal (today's conflict)
    shared_hulls: int
    union_hulls: int
    same_fits: int
    mine_only: tuple                    # hull names only the importer's doctrine flies, sorted
    theirs_only: tuple                  # hull names only the group's doctrine flies, sorted
    loose: bool = False                 # the names are loosely, not exactly, equal

    def reason(self) -> str:
        """Why the pair was made, in words: ``"6 of 7 ships, 4 identical fits"``.

        A pair of no shared hulls made by its name reads ``"same name"`` instead of
        ``"0 of N ships"`` (``"same name, 1 identical fit"``).
        """
        if not self.shared_hulls and (self.exact or self.loose):
            text = "same name"
        elif self.union_hulls:
            text = "%d of %d ships" % (self.shared_hulls, self.union_hulls)
        else:
            text = "no ships"
        if self.same_fits:
            text += ", %d identical fit%s" % (self.same_fits,
                                               "" if self.same_fits == 1 else "s")
        return text


def _is_hull(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _clean(sig):
    """``(name or None, hull set, fit-hash set, hull names, loose name)`` of a ``Sig``.

    Garbage -> empty. The loose name is computed here, once per doctrine, never per pair.
    """
    name = getattr(sig, "name", None)
    if not isinstance(name, str):
        name = None
    try:
        hulls = frozenset(h for h in getattr(sig, "hulls", ()) if _is_hull(h))
    except TypeError:
        hulls = frozenset()
    try:
        hashes = frozenset(f for f in getattr(sig, "fits", ()) if isinstance(f, str))
    except TypeError:
        hashes = frozenset()
    names = getattr(sig, "hull_names", None)
    return (name, hulls, hashes, names if isinstance(names, dict) else {},
            loose_name(name) if name is not None else "")


def _hull_label(hull, names):
    label = names.get(hull)
    return label if isinstance(label, str) and label else str(hull)


def _labels(hulls, names):
    return tuple(sorted((_hull_label(h, names) for h in hulls),
                        key=lambda label: (label.casefold(), label)))


def _similar(mine, theirs):
    """``(similar, loose_equal, shared, union, same_fits)`` for two cleaned sigs."""
    m_name, m_hulls, m_fits, _, m_loose = mine
    t_name, t_hulls, t_fits, _, t_loose = theirs
    shared = len(m_hulls & t_hulls)
    union = len(m_hulls) + len(t_hulls) - shared
    same_fits = len(m_fits & t_fits)
    loose_equal = bool(m_loose) and m_loose == t_loose
    similar = (
        (m_name is not None and m_name == t_name)
        or loose_equal
        or (shared >= MIN_SHARED_HULLS and shared >= HULL_RATIO * union - _EPSILON)
        or (len(m_hulls) == 1 and m_hulls == t_hulls and same_fits >= 1))
    return similar, loose_equal, shared, union, same_fits


def _pair(index, theirs, local_id, mine):
    _, loose_equal, shared, union, same_fits = _similar(mine, theirs)
    exact = mine[0] is not None and mine[0] == theirs[0]
    return Pair(incoming_index=index, incoming_name=theirs[0] or "", local_id=local_id,
                local_name=mine[0] or "", exact=exact,
                shared_hulls=shared, union_hulls=union, same_fits=same_fits,
                mine_only=_labels(mine[1] - theirs[1], mine[3]),
                theirs_only=_labels(theirs[1] - mine[1], theirs[3]),
                loose=loose_equal and not exact)


def pair_doctrines(local, incoming) -> list:
    """Pair each group doctrine with at most one of the importer's, one-to-one.

    ``local`` is ``(doctrine id, Sig)`` in library order; ``incoming`` is the file's
    ``Sig`` list in file order. Only the first doctrine of a name on either side takes
    part (and the first entry of a repeated library id). Steps:

    1. exact names: each incoming doctrine, in file order, takes the local doctrine of
       exactly its name;
    2. every other similar pair is scored ``(loose_equal, shared/union hulls, same_fits)``
       and taken best first - ties go to the lower incoming index, then library order -
       when neither side is already taken.

    Returns the ``Pair`` list ordered by ``incoming_index``. Never raises.
    """
    try:
        local = list(local)
    except TypeError:
        local = []
    try:
        incoming = list(incoming)
    except TypeError:
        incoming = []

    mine = []                               # (library position, id, cleaned sig)
    seen_ids, seen_names = set(), set()
    for position, entry in enumerate(local):
        if not (isinstance(entry, tuple) and len(entry) == 2):
            continue
        local_id, sig = entry
        if not isinstance(local_id, str) or not isinstance(sig, Sig):
            continue
        cleaned = _clean(sig)
        if local_id in seen_ids or cleaned[0] in seen_names:
            continue
        seen_ids.add(local_id)
        if cleaned[0] is not None:
            seen_names.add(cleaned[0])
        mine.append((position, local_id, cleaned))

    theirs = []                             # (incoming index, cleaned sig)
    seen_names = set()
    for index, sig in enumerate(incoming):
        if not isinstance(sig, Sig):
            continue
        cleaned = _clean(sig)
        if cleaned[0] in seen_names:
            continue
        if cleaned[0] is not None:
            seen_names.add(cleaned[0])
        theirs.append((index, cleaned))

    pairs, taken_local, taken_incoming = [], set(), set()

    by_name = {cleaned[0]: (local_id, cleaned) for _, local_id, cleaned in mine
               if cleaned[0] is not None}
    for index, cleaned in theirs:
        match = by_name.get(cleaned[0]) if cleaned[0] is not None else None
        if match is None or match[0] in taken_local:
            continue
        taken_local.add(match[0])
        taken_incoming.add(index)
        pairs.append(_pair(index, cleaned, match[0], match[1]))

    candidates = []
    for index, t_sig in theirs:
        if index in taken_incoming:
            continue
        for position, local_id, m_sig in mine:
            if local_id in taken_local:
                continue
            similar, loose_equal, shared, union, same_fits = _similar(m_sig, t_sig)
            if not similar:
                continue
            ratio = shared / union if union else 0.0
            candidates.append(((-int(loose_equal), -ratio, -same_fits, index, position),
                               index, t_sig, local_id, m_sig))
    candidates.sort(key=lambda row: row[0])
    for _, index, t_sig, local_id, m_sig in candidates:
        if index in taken_incoming or local_id in taken_local:
            continue
        taken_local.add(local_id)
        taken_incoming.add(index)
        pairs.append(_pair(index, t_sig, local_id, m_sig))

    pairs.sort(key=lambda pair: pair.incoming_index)
    return pairs
