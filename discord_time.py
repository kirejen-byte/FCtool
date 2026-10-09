"""Typed date + time -> unix -> Discord ``<t:UNIX:STYLE>`` tags (pure).

The Discord ping's "When" field: the FC types a date and a time in EVE time
(UTC) or in their own local time; this module turns that into a unix timestamp,
into the timestamp tags Discord renders in every reader's own zone, and into
the wording the preview and the echo line show for them.

Pure: stdlib ``datetime`` only -- no Tk, no I/O, no ``zoneinfo`` / ``tzdata``,
and NO CLOCK READS: every function that needs "now" is handed ``now_unix``.
Weekday and month names are hard-coded English (never ``strftime``, which
follows the process locale).

The two zone converters are injectable so tests never touch the process time
zone (Windows has no ``time.tzset``):

* ``local_to_utc(naive_local_datetime) -> aware UTC datetime``
* ``utc_to_local(aware UTC datetime) -> naive local datetime``

The defaults are the stdlib ``astimezone()``, i.e. the operating system's zone
rules for the date in question. A local time that does not exist (the
spring-forward gap) resolves the way Python resolves it. A local time that
happens TWICE (the repeated hour of a fall-back night) cannot be told apart in
naive fields, so ``to_unix(*fields_for(u, "local"), "local") == u`` does not
hold inside that hour; everywhere else, and always in the EVE basis, a
whole-minute ``u`` round-trips.

Errors. The only exception this module raises for an argument VALUE is
``ValueError``, with a message fit for the echo line:

* text that is not a date / time, an unknown basis or style;
* a ``unix`` that is not a whole number, or lies outside the years 1..9999
  (``datetime``'s range) -- in EVERY function that takes one except
  ``quick_target`` and ``tag``, which do integer arithmetic only and so can
  hand back a value the others then refuse;
* a moment the zone converter cannot handle: an ``OSError`` / ``OverflowError``
  from the default OR an injected converter is re-raised as ``ValueError``.
  The Windows defaults fail for a negative ``unix`` and from 32536800000
  (January 3001) on -- measured -- which is why ``parse_date`` keeps typed
  years inside ``MIN_YEAR``..``MAX_YEAR``.

A converter that breaks its contract (returns something else than documented
above) is a programming error and is not masked.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

STYLES = ("t", "T", "d", "D", "f", "F", "s", "S", "R")
PRESETS = ("time_countdown", "full", "datetime_countdown")
DEFAULT_PRESET = "time_countdown"
# preset -> short human label for the format dropdown.
PRESET_LABELS: dict[str, str] = {
    "time_countdown": "Time + countdown",
    "full": "Full date and time",
    "datetime_countdown": "Date, time + countdown",
}

BASES = ("eve", "local")
MIN_YEAR = 1970
MAX_YEAR = 2999

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
             "Saturday", "Sunday")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_ONE_SECOND = timedelta(seconds=1)
_DAY = 86400
# Relative-time units, smallest first: (singular name, size in seconds).
_UNITS = (("second", 1), ("minute", 60), ("hour", 3600), ("day", _DAY),
          ("month", 30 * _DAY), ("year", 365 * _DAY))

# Explicit ASCII classes: these digits go to int(), which str.isdigit() does
# not guarantee to accept (tests/test_no_isdigit_int.py).
# Dates are STRICT (four, two, two digits): "2026-1-5" is one keystroke short
# of "2026-10-10" and must not pass as a valid 1 October / 5 January.
# Times: "HH:MM", "H:MM" (unambiguous with the colon) or exactly "HHMM".
_DATE_RE = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_TIME_RE = re.compile(r"([0-9]{1,2}):([0-9]{2})|([0-9]{2})([0-9]{2})")


# -- parsing -----------------------------------------------------------------
def _clean(text) -> str:
    return text.strip() if isinstance(text, str) else ""


def parse_date(text: str) -> tuple[int, int, int]:
    """Exactly ``"YYYY-MM-DD"`` -> ``(year, month, day)``; ``ValueError`` says
    why not (a one-digit month or day is refused, not guessed at)."""
    text = _clean(text)
    if not text:
        raise ValueError("Enter a date as YYYY-MM-DD")
    match = _DATE_RE.fullmatch(text)
    if match is None:
        raise ValueError(
            "The date must look like YYYY-MM-DD (two-digit month and day), "
            "for example 2026-01-05")
    year, month, day = (int(part) for part in match.groups())
    if not MIN_YEAR <= year <= MAX_YEAR:
        raise ValueError(
            "The year must be between %d and %d" % (MIN_YEAR, MAX_YEAR))
    if not 1 <= month <= 12:
        raise ValueError("The month must be between 01 and 12")
    try:
        datetime(year, month, day)
    except ValueError:
        raise ValueError("%s %d has no day %d"
                         % (_MONTHS[month - 1], year, day)) from None
    return year, month, day


def parse_time(text: str) -> tuple[int, int]:
    """``"HH:MM"``, ``"H:MM"`` or four-digit ``"HHMM"``, 24-hour ->
    ``(hour, minute)``; ``ValueError`` says why not."""
    text = _clean(text)
    if not text:
        raise ValueError("Enter a time as HH:MM")
    match = _TIME_RE.fullmatch(text)
    if match is None:
        raise ValueError(
            "The time must look like HH:MM or HHMM (24-hour), for example 19:00")
    # Exactly one alternative matched, so exactly two groups are set.
    hour, minute = (int(part) for part in match.groups() if part is not None)
    if hour > 23:
        raise ValueError("The hour must be between 00 and 23")
    if minute > 59:
        raise ValueError("The minute must be between 00 and 59")
    return hour, minute


def _check_basis(basis) -> None:
    if not (isinstance(basis, str) and basis in BASES):
        raise ValueError(
            "Unknown time basis %r: expected 'eve' or 'local'" % (basis,))


def _check_style(style) -> None:
    if not (isinstance(style, str) and style in STYLES):
        raise ValueError("Unknown Discord timestamp style %r: expected one of %s"
                         % (style, " ".join(STYLES)))


# -- zone conversion ---------------------------------------------------------
def _default_local_to_utc(naive):
    return naive.astimezone(timezone.utc)


def _default_utc_to_local(aware):
    return aware.astimezone().replace(tzinfo=None)


def _whole(value, what="timestamp") -> int:
    """``value`` as an int; ``ValueError`` when it is not a number at all
    (None, text, infinity, NaN)."""
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("That %s is not a whole number" % what) from None


def _utc(unix) -> datetime:
    """The aware UTC datetime of a unix timestamp (no platform time calls).
    ``ValueError`` outside ``datetime``'s years 1..9999."""
    seconds = _whole(unix)
    try:
        return _EPOCH + timedelta(seconds=seconds)
    except OverflowError:
        raise ValueError(
            "That timestamp is outside the years 1 to 9999") from None


def _local(unix, utc_to_local) -> datetime:
    """The NAIVE local datetime of a unix timestamp. ``ValueError`` when the
    moment is out of range or the zone converter (default or injected) cannot
    handle it."""
    moment = _utc(unix)
    convert = _default_utc_to_local if utc_to_local is None else utc_to_local
    try:
        return convert(moment)
    except (OverflowError, OSError):
        # The OS zone tables do not reach this moment (Windows: a negative
        # unix, or one from January 3001 on).
        raise ValueError(
            "This computer cannot show that moment in local time") from None


# -- typed fields <-> unix ---------------------------------------------------
def to_unix(date_text: str, time_text: str, basis: str, *,
            local_to_utc=None) -> int:
    """The unix timestamp of a typed date + time. ``basis`` is ``"eve"`` (the
    fields are UTC) or ``"local"`` (they go through ``local_to_utc``).
    ``ValueError`` with a readable message for anything that cannot be used,
    including a date the zone converter (default or injected) refuses with
    ``OSError`` / ``OverflowError``."""
    _check_basis(basis)
    year, month, day = parse_date(date_text)
    hour, minute = parse_time(time_text)
    naive = datetime(year, month, day, hour, minute)
    if basis == "eve":
        aware = naive.replace(tzinfo=timezone.utc)
    else:
        convert = _default_local_to_utc if local_to_utc is None else local_to_utc
        try:
            aware = convert(naive)
        except (OverflowError, OSError):
            # The OS zone tables do not reach this date (measured on Windows:
            # the first hours of 1970-01-01 local, whatever the zone).
            raise ValueError(
                "This computer cannot convert that local date to EVE time"
            ) from None
    return (aware - _EPOCH) // _ONE_SECOND


def fields_for(unix: int, basis: str, *, utc_to_local=None) -> tuple[str, str]:
    """``(date_text, time_text)`` of ``unix`` in ``basis``, in the shape
    ``to_unix`` reads. Seconds are not part of the fields.

    ``to_unix(*fields_for(u, basis), basis) == u`` holds for a whole-minute
    ``u`` in the EVE basis always, and in the local basis EXCEPT inside the
    repeated hour of a fall-back night: both occurrences of 01:30 give the same
    naive fields, and ``to_unix`` can only return one of them. Do not rely on a
    general local round trip.

    ``ValueError`` for an unknown basis or a ``unix`` that cannot be
    represented (out of ``datetime``'s range, or refused by the converter)."""
    _check_basis(basis)
    moment = _utc(unix) if basis == "eve" else _local(unix, utc_to_local)
    return ("%04d-%02d-%02d" % (moment.year, moment.month, moment.day),
            "%02d:%02d" % (moment.hour, moment.minute))


def quick_target(now_unix: int, delta_minutes: int) -> int:
    """``now + delta``, rounded UP to the next 5-minute mark (a moment already
    on a mark stays there). Integer arithmetic only: an absurd delta returns an
    absurd number, which the wording functions then refuse with ``ValueError``."""
    target = _whole(now_unix) + _whole(delta_minutes, "number of minutes") * 60
    return -(-target // 300) * 300


# -- output ------------------------------------------------------------------
def tag(unix: int, style: str) -> str:
    """The Discord timestamp tag, e.g. ``<t:1791658800:t>``. Builds no
    datetime, so any whole number is tagged; ``ValueError`` for an unknown
    style or a ``unix`` that is not a number."""
    _check_style(style)
    return "<t:%d:%s>" % (_whole(unix), style)


def eve_label(unix: int, now_unix: int) -> str:
    """``19:00 EVE`` when ``unix`` falls on the EVE (UTC) date of ``now_unix``,
    else ``Sat 10 Oct 19:00 EVE``. ``ValueError`` when either is outside the
    years 1..9999."""
    when, now = _utc(unix), _utc(now_unix)
    clock = "%02d:%02d EVE" % (when.hour, when.minute)
    if when.date() == now.date():
        return clock
    return "%s %d %s %s" % (_WEEKDAYS[when.weekday()][:3], when.day,
                            _MONTHS[when.month - 1][:3], clock)


def format_when(unix: int | None, preset: str, *, now_unix: int,
                now_text: str = "Now") -> str:
    """The text the When pill emits: ``now_text`` for no time, else the EVE
    label plus Discord tags in one of ``PRESETS`` (unknown -> the default).
    Only ``None`` means "no time": ``0`` is 1970-01-01 00:00 EVE.
    ``ValueError`` when ``unix`` or ``now_unix`` cannot be represented."""
    if unix is None:
        return now_text
    if not (isinstance(preset, str) and preset in PRESETS):
        preset = DEFAULT_PRESET
    eve = eve_label(unix, now_unix)
    dash = chr(0x2014)                                  # em dash
    if preset == "full":
        return "%s %s %s" % (eve, dash, tag(unix, "F"))
    if preset == "datetime_countdown":
        return "%s %s %s (%s)" % (eve, dash, tag(unix, "f"), tag(unix, "R"))
    return "%s (%s your time, %s)" % (eve, tag(unix, "t"), tag(unix, "R"))


def _relative(delta: int) -> str:
    """Discord's ``R`` wording for ``delta`` seconds from now (+ = future)."""
    span = abs(delta)
    if span == 0:
        return "now"
    index = 0
    while index + 1 < len(_UNITS) and span >= _UNITS[index + 1][1]:
        index += 1
    name, size = _UNITS[index]
    count = (2 * span + size) // (2 * size)             # round half up
    if index + 1 < len(_UNITS) and count * size >= _UNITS[index + 1][1]:
        name, count = _UNITS[index + 1][0], 1           # 60 minutes -> 1 hour
    words = "%d %s%s" % (count, name, "" if count == 1 else "s")
    return "in " + words if delta > 0 else words + " ago"


def render_style(unix: int, style: str, *, now_unix: int,
                 utc_to_local=None) -> str:
    """What Discord shows for ``<t:unix:style>`` to an en-US, 12-hour reader in
    the local zone -- the preview's wording. ``R`` counts from ``now_unix``.

    ``unix`` may be digits a user typed into a tag: one that cannot be
    represented (``<t:1791658800000>``, milliseconds by mistake) raises
    ``ValueError`` in EVERY style, ``R`` included, and so does a moment the
    zone converter refuses -- never ``OverflowError`` / ``OSError``."""
    _check_style(style)
    if style == "R":
        # Through _utc, not bare subtraction: an out-of-range moment is as
        # invalid in a countdown as in a date.
        return _relative((_utc(unix) - _utc(now_unix)) // _ONE_SECOND)
    local = _local(unix, utc_to_local)
    half = "AM" if local.hour < 12 else "PM"
    hour = local.hour % 12 or 12
    short_time = "%d:%02d %s" % (hour, local.minute, half)
    long_time = "%d:%02d:%02d %s" % (hour, local.minute, local.second, half)
    short_date = "%d/%d/%02d" % (local.month, local.day, local.year % 100)
    long_date = "%s %d, %d" % (_MONTHS[local.month - 1], local.day, local.year)
    if style == "t":
        return short_time
    if style == "T":
        return long_time
    if style == "d":
        return short_date
    if style == "D":
        return long_date
    if style == "f":
        return "%s at %s" % (long_date, short_time)
    if style == "F":
        return "%s, %s at %s" % (_WEEKDAYS[local.weekday()], long_date,
                                 short_time)
    if style == "s":
        return "%s, %s" % (short_date, short_time)
    return "%s, %s" % (short_date, long_time)           # "S"


def describe(unix: int, now_unix: int, *, utc_to_local=None) -> str:
    """The echo line under the time fields: the EVE label, the local clock time
    (with its weekday when the local date is not today's local date) and how
    far away it is -- ``already passed (...)`` once it is behind ``now_unix``
    (at exactly ``now_unix`` the last part reads ``now``). ``ValueError`` when
    either moment cannot be represented or converted to local time."""
    local = _local(unix, utc_to_local)
    today = _local(now_unix, utc_to_local)
    local_part = "%02d:%02d local" % (local.hour, local.minute)
    if local.date() != today.date():
        local_part = "%s %s" % (_WEEKDAYS[local.weekday()][:3], local_part)
    delta = _whole(unix) - _whole(now_unix)
    wording = _relative(delta)
    if delta < 0:
        wording = "already passed (%s)" % wording
    separator = " %s " % chr(0xB7)                      # middle dot
    return separator.join((eve_label(unix, now_unix), local_part, wording))
