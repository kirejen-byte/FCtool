"""Per-module EWAR cycle times and the window resolver for the preview icons.

The preview's EWAR icons expire on a window derived from the cycle time of the
module that caused them (design A1).  A flat window per effect would be badly
wrong for capacitor warfare: a small neutralizer cycles in 6 s and a capital
one in 48 s, so one number either blinks out mid-cycle or lingers for four
cycles after the module stopped.

Every incoming capacitor-warfare and ECM line in the gamelog names its module,
so the cycle is a NAME lookup rather than an inference from the GJ amount.
That matters beyond accuracy: hulls such as Curse, Armageddon and Sentinel
bonus neutralizer AMOUNT and never DURATION, so a bonused medium neutralizer
hits for heavy-neutralizer numbers while still cycling at 12 s.  A name lookup
is simply right where an amount-derived guess doubles the window.

:data:`MODULE_CYCLE_MS` is baked in as a literal by
``tools/build_ewar_cycles.py`` from the repository's ``fit_dogma.json.gz``
(attribute 73, duration).  It is NOT read from that blob at runtime: the blob
is megabytes unpacked and this module sits on the preview tick.  Importing
this module opens no file, starts no thread and touches no Tk.

:func:`cycle_ms_for` is reached from that tick, where an escaping exception
retires a tile.  It therefore NEVER raises and NEVER returns a non-positive
number, whatever it is handed.
"""
import math
import re

# --- BEGIN GENERATED TABLE: tools/build_ewar_cycles.py ---
# Generated 2026-10-30 by tools/build_ewar_cycles.py from
# fit_dogma.json.gz (SDE build 3494416, attribute 73) + fit_types.json.
# 151 modules kept; 13 in-scope types carry no duration and rely on
# the size-word fallback below:
#   Capital Abyssal Energy Neutralizer  (no attribute 73)
#   Capital Abyssal Energy Nosferatu  (no attribute 73)
#   ECM Jammer Burst Projector  (no attribute 73)
#   Heavy Abyssal Energy Neutralizer  (no attribute 73)
#   Heavy Abyssal Energy Nosferatu  (no attribute 73)
#   Medium Abyssal Energy Neutralizer  (no attribute 73)
#   Medium Abyssal Energy Nosferatu  (no attribute 73)
#   Small Abyssal Energy Neutralizer  (no attribute 73)
#   Small Abyssal Energy Nosferatu  (no attribute 73)
#   Standup Gravimetric ECM Script  (no attribute 73)
#   Standup Ladar ECM Script  (no attribute 73)
#   Standup Magnetometric ECM Script  (no attribute 73)
#   Standup Radar ECM Script  (no attribute 73)
# DO NOT EDIT THIS REGION BY HAND -- re-run the tool instead.
MODULE_CYCLE_MS: dict[str, int] = {
    "'Gambler' Ladar ECM": 20000,
    "'Heist' Radar ECM": 20000,
    "'Marshall' Magnetometric ECM": 20000,
    "'Plunderer' Gravimetric ECM": 20000,
    'Aergia Compact Magnetometric ECM': 20000,
    "Ahremen's Modified Heavy Energy Neutralizer": 24000,
    "Ahremen's Modified Heavy Energy Nosferatu": 10000,
    'Ammatar Navy Heavy Energy Neutralizer': 24000,
    'Ammatar Navy Heavy Energy Nosferatu': 10000,
    'Ammatar Navy Medium Energy Neutralizer': 12000,
    'Ammatar Navy Medium Energy Nosferatu': 5000,
    'Ammatar Navy Small Energy Neutralizer': 6000,
    'Ammatar Navy Small Energy Nosferatu': 2500,
    'BZ-5 Scoped Gravimetric ECM': 20000,
    "Brokara's Modified Heavy Energy Neutralizer": 24000,
    "Brokara's Modified Heavy Energy Nosferatu": 10000,
    'CZ-4 Compact Gravimetric ECM': 20000,
    'Capital Energy Neutralizer I': 48000,
    'Capital Energy Neutralizer II': 48000,
    'Capital Energy Nosferatu I': 20000,
    'Capital Energy Nosferatu II': 20000,
    'Capital Ghoul Compact Energy Nosferatu': 20000,
    'Capital Gremlin Compact Energy Neutralizer': 48000,
    'Capital Infectious Scoped Energy Neutralizer': 48000,
    'Capital Knave Scoped Energy Nosferatu': 20000,
    "Chelm's Modified Heavy Energy Neutralizer": 24000,
    "Chelm's Modified Heavy Energy Nosferatu": 10000,
    'Compulsive Scoped Multispectral ECM': 20000,
    'Corpii A-Type Small Energy Neutralizer': 6000,
    'Corpii A-Type Small Energy Nosferatu': 2500,
    'Corpii B-Type Small Energy Neutralizer': 6000,
    'Corpii B-Type Small Energy Nosferatu': 2500,
    'Corpii C-Type Small Energy Neutralizer': 6000,
    'Corpii C-Type Small Energy Nosferatu': 2500,
    'Corpum A-Type Medium Energy Neutralizer': 12000,
    'Corpum A-Type Medium Energy Nosferatu': 5000,
    'Corpum B-Type Medium Energy Neutralizer': 12000,
    'Corpum B-Type Medium Energy Nosferatu': 5000,
    'Corpum C-Type Medium Energy Neutralizer': 12000,
    'Corpum C-Type Medium Energy Nosferatu': 5000,
    'Corpus A-Type Heavy Energy Neutralizer': 24000,
    'Corpus A-Type Heavy Energy Nosferatu': 10000,
    'Corpus B-Type Heavy Energy Neutralizer': 24000,
    'Corpus B-Type Heavy Energy Nosferatu': 10000,
    'Corpus C-Type Heavy Energy Neutralizer': 24000,
    'Corpus C-Type Heavy Energy Nosferatu': 10000,
    'Corpus X-Type Heavy Energy Neutralizer': 24000,
    'Corpus X-Type Heavy Energy Nosferatu': 10000,
    'Dark Blood Capital Energy Neutralizer': 48000,
    'Dark Blood Capital Energy Nosferatu': 20000,
    'Dark Blood Heavy Energy Neutralizer': 24000,
    'Dark Blood Heavy Energy Nosferatu': 10000,
    'Dark Blood Medium Energy Neutralizer': 12000,
    'Dark Blood Medium Energy Nosferatu': 5000,
    'Dark Blood Small Energy Neutralizer': 6000,
    'Dark Blood Small Energy Nosferatu': 2500,
    "Draclira's Modified Heavy Energy Neutralizer": 24000,
    "Draclira's Modified Heavy Energy Nosferatu": 10000,
    'Dread Guristas Multispectral ECM': 20000,
    'Energy Neutralization Burst Projector': 480000,
    'Enfeebling Scoped Ladar ECM': 20000,
    "Estamel's Modified Multispectral ECM": 20000,
    'FZ-3a Enduring Gravimetric ECM': 20000,
    'Gloom Enduring Radar ECM': 20000,
    'Gravimetric ECM I': 20000,
    'Gravimetric ECM II': 20000,
    'Halting Compact Ladar ECM': 20000,
    "Heavy 'Moat' Energy Neutralizer": 24000,
    "Heavy 'Vrykolakas' Energy Nosferatu": 10000,
    'Heavy Energy Neutralizer I': 24000,
    'Heavy Energy Neutralizer II': 24000,
    'Heavy Energy Nosferatu I': 10000,
    'Heavy Energy Nosferatu II': 10000,
    'Heavy Ghoul Compact Energy Nosferatu': 10000,
    'Heavy Gremlin Compact Energy Neutralizer': 24000,
    'Heavy Infectious Scoped Energy Neutralizer': 24000,
    'Heavy Knave Scoped Energy Nosferatu': 10000,
    'Hypnos Scoped Magnetometric ECM': 20000,
    'Imperial Navy Heavy Energy Neutralizer': 24000,
    'Imperial Navy Heavy Energy Nosferatu': 10000,
    'Imperial Navy Medium Energy Neutralizer': 12000,
    'Imperial Navy Medium Energy Nosferatu': 5000,
    'Imperial Navy Small Energy Neutralizer': 6000,
    'Imperial Navy Small Energy Nosferatu': 2500,
    'Induced Compact Multispectral ECM': 20000,
    'Initiated Enduring Multispectral ECM': 20000,
    "Kaikka's Modified Multispectral ECM": 20000,
    'Ladar ECM I': 20000,
    'Ladar ECM II': 20000,
    'Languid Enduring Ladar ECM': 20000,
    'Legion Gravimetric ECM': 20000,
    'Legion Ladar ECM': 20000,
    'Legion Magnetometric ECM': 20000,
    'Legion Multispectral ECM': 20000,
    'Legion Radar ECM': 20000,
    'Magnetometric ECM I': 20000,
    'Magnetometric ECM II': 20000,
    "Makra's Modified Small Energy Neutralizer": 6000,
    "Makra's Modified Small Energy Nosferatu": 2500,
    "Medium 'Ditch' Energy Neutralizer": 12000,
    "Medium 'Strigoi' Energy Nosferatu": 5000,
    'Medium Energy Neutralizer I': 12000,
    'Medium Energy Neutralizer II': 12000,
    'Medium Energy Nosferatu I': 5000,
    'Medium Energy Nosferatu II': 5000,
    'Medium Ghoul Compact Energy Nosferatu': 5000,
    'Medium Gremlin Compact Energy Neutralizer': 12000,
    'Medium Infectious Scoped Energy Neutralizer': 12000,
    'Medium Knave Scoped Energy Nosferatu': 5000,
    "Megaera's Modified Medium Energy Nosferatu": 5000,
    'Morpheus Enduring Magnetometric ECM': 20000,
    'Multispectral ECM I': 20000,
    'Multispectral ECM II': 20000,
    "Panola's Modified Gravimetric ECM": 20000,
    "Panola's Modified Ladar ECM": 20000,
    "Panola's Modified Magnetometric ECM": 20000,
    "Panola's Modified Radar ECM": 20000,
    'Radar ECM I': 20000,
    'Radar ECM II': 20000,
    "Raysere's Modified Heavy Energy Neutralizer": 24000,
    "Raysere's Modified Heavy Energy Nosferatu": 10000,
    "Ryhad's Modified Medium Energy Neutralizer": 12000,
    "Ryhad's Modified Medium Energy Nosferatu": 5000,
    "Selynne's Modified Heavy Energy Neutralizer": 24000,
    "Selynne's Modified Heavy Energy Nosferatu": 10000,
    'Shade Compact Radar ECM': 20000,
    "Small 'Caltrop' Energy Neutralizer": 6000,
    "Small 'Upir' Energy Nosferatu": 2500,
    'Small Energy Neutralizer I': 6000,
    'Small Energy Neutralizer II': 6000,
    'Small Energy Nosferatu I': 2500,
    'Small Energy Nosferatu II': 2500,
    'Small Ghoul Compact Energy Nosferatu': 2500,
    'Small Gremlin Compact Energy Neutralizer': 6000,
    'Small Infectious Scoped Energy Neutralizer': 6000,
    'Small Knave Scoped Energy Nosferatu': 2500,
    "Tairei's Modified Heavy Energy Neutralizer": 24000,
    "Tairei's Modified Heavy Energy Nosferatu": 10000,
    "Thon's Modified Multispectral ECM": 20000,
    'True Sansha Capital Energy Neutralizer': 48000,
    'True Sansha Capital Energy Nosferatu': 20000,
    'True Sansha Heavy Energy Neutralizer': 24000,
    'True Sansha Heavy Energy Nosferatu': 10000,
    'True Sansha Medium Energy Neutralizer': 12000,
    'True Sansha Medium Energy Nosferatu': 5000,
    'True Sansha Small Energy Neutralizer': 6000,
    'True Sansha Small Energy Nosferatu': 2500,
    'Umbra Scoped Radar ECM': 20000,
    "Vepas' Modified Multispectral ECM": 20000,
    "Vizan's Modified Heavy Energy Neutralizer": 24000,
    "Vizan's Modified Heavy Energy Nosferatu": 10000,
}
# --- END GENERATED TABLE ---

# ---------------------------------------------------------------------------
# Resolution (design A1c).  Hand written -- the generator never touches it.
# ---------------------------------------------------------------------------

#: Used when nothing else answers.  Mid-way between a medium neutralizer and a
#: heavy one, so a module the table has never heard of is wrong by at most one
#: size step in either direction.
_DEFAULT_CYCLE_MS = 15000

#: A structure module is fitted as "Standup <type name>" and the gamelog
#: writes that prefixed name, while the SDE type name carries no prefix.
_STANDUP_PREFIX = "standup "

#: Canonical cycle per size, per family -- read straight off the generated
#: table, where every entry of a size shares one duration.  A test pins that.
_SIZES = ("small", "medium", "heavy", "capital")
_NEUTRALIZER_BY_SIZE = {"small": 6000, "medium": 12000, "heavy": 24000, "capital": 48000}
_NOSFERATU_BY_SIZE = {"small": 2500, "medium": 5000, "heavy": 10000, "capital": 20000}

#: Amount buckets, DELIBERATELY BIASED DOWNWARD (design A1c).  Each threshold
#: is the top of a size's unbonused GJ band as measured from the dogma blob
#: (neutralizer attribute 97: 45-60 / 150-195 / 500-650 / 3600-4400;
#: nosferatu attribute 90: 8-11 / 30-39 / 100-130 / 700-800), and the cycle it
#: maps to is the size BELOW that band.  An amount that lands squarely in the
#: heavy band is therefore read as a bonused medium, not as a heavy.
#:
#: This is the ship-bonus guard and the direction is the whole point: reading
#: a bonused module one size too big would double its window and leave a stuck
#: icon on a calm tile, which is the failure the feature cannot afford.  The
#: bucket is only ever reached for a name the table does not carry AND that
#: has no size word, so an unbonused module read one step small is a rare,
#: cheap error -- the icon clears early rather than late.
_NEUTRALIZER_BUCKETS = ((195.0, 6000), (650.0, 12000), (4400.0, 24000))
_NOSFERATU_BUCKETS = ((39.0, 2500), (130.0, 5000), (800.0, 10000))

_CYCLE_BY_LOWER_NAME = {name.lower(): ms for name, ms in MODULE_CYCLE_MS.items()}
_SIZE_RE = re.compile(r"\b(" + "|".join(_SIZES) + r")\b")


def _as_name(module_name) -> str:
    """Coerce anything at all to a stripped, lower-cased name."""
    if not isinstance(module_name, str):
        return ""
    return str.strip(module_name).lower()


def _as_amount(amount):
    """Coerce an amount to a positive float, or None when it says nothing.

    The incoming nosferatu line renders a NEGATIVE amount ("-10 GJ energy
    drained to ..."), so the magnitude is what carries the information.  A
    bool, a string, zero, a NaN and an infinity all say nothing.
    """
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return None
    value = abs(float(amount))
    if not math.isfinite(value) or value <= 0.0:
        return None
    return value


def _family_table(name: str, neutralizer, nosferatu):
    """Pick the nosferatu or neutralizer variant of a per-family table.

    Nosferatu wins a name carrying both words: its cycles are the shorter
    ones, which keeps the whole resolver biased the safe way.
    """
    if "nosferatu" in name:
        return nosferatu
    if "neutralizer" in name:
        return neutralizer
    return None


def _from_table(name: str):
    """Step 1: an exact hit, give or take casing and the Standup prefix."""
    ms = _CYCLE_BY_LOWER_NAME.get(name)
    if ms is None and name.startswith(_STANDUP_PREFIX):
        ms = _CYCLE_BY_LOWER_NAME.get(name[len(_STANDUP_PREFIX):])
    return ms


def _from_size_word(name: str):
    """Step 2: a size word plus a family word.

    This is what carries the eight Abyssal neutralizer/nosferatu types, which
    have no dogma stats at all because theirs are rolled per item -- but whose
    names still say Small/Medium/Heavy/Capital.  A mutation shifts the
    duration within a band, never across one.
    """
    by_size = _family_table(name, _NEUTRALIZER_BY_SIZE, _NOSFERATU_BY_SIZE)
    if by_size is None:
        return None
    found = _SIZE_RE.findall(name)
    if not found:
        return None
    # Several size words in one name is nonsense; take the smallest, in
    # keeping with the downward bias.
    return by_size.get(min(found, key=_SIZES.index))


def _from_amount(name: str, value):
    """Step 3: bucket the GJ amount, biased toward the smaller module."""
    if value is None:
        return None
    buckets = _family_table(name, _NEUTRALIZER_BUCKETS, _NOSFERATU_BUCKETS)
    if buckets is _NOSFERATU_BUCKETS:
        largest = _NOSFERATU_BY_SIZE["capital"]
    else:
        # No family word: the amount can only have come off a capacitor
        # warfare line, and the neutralizer bands are the ones that scale with
        # the numbers such a line carries.
        buckets = buckets or _NEUTRALIZER_BUCKETS
        largest = _NEUTRALIZER_BY_SIZE["capital"]
    for ceiling, ms in buckets:
        if value <= ceiling:
            return ms
    return largest


def cycle_ms_for(module_name, amount=None) -> int:
    """Return the cycle time in milliseconds for ``module_name``.

    ``module_name`` is the module as the gamelog named it; ``amount`` is the
    line's GJ figure, or None for an effect that carries none (a jam).  The
    four steps are tried in order and the first that answers wins:

    1. an exact :data:`MODULE_CYCLE_MS` hit;
    2. a size word in the name together with a family word;
    3. the amount, bucketed downward;
    4. :data:`_DEFAULT_CYCLE_MS`.

    Always returns a positive int.  Never raises -- see the module docstring.
    """
    try:
        name = _as_name(module_name)
        resolved = _from_table(name)
        if resolved is None:
            resolved = _from_size_word(name)
        if resolved is None:
            resolved = _from_amount(name, _as_amount(amount))
    except Exception:  # pragma: no cover - the tick must survive anything
        resolved = None
    if isinstance(resolved, int) and not isinstance(resolved, bool) and resolved > 0:
        return resolved
    return _DEFAULT_CYCLE_MS
