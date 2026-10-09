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
spring-forward gap) resolves the way Python resolves it. On Windows those
defaults only work from 1970 to the year 3000, which is why ``parse_date``
refuses a year outside ``MIN_YEAR``..``MAX_YEAR``: a unix this module produced
can always be worded again.
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
_DATE_RE = re.compile(r"([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})")
_TIME_RE = re.compile(r"([0-9]{1,2}):([0-9]{2})|([0-9]{2})([0-9]{2})")


# -- parsing -----------------------------------------------------------------
def _clean(text) -> str:
    return text.strip() if isinstance(text, str) else ""


def parse_date(text: str) -> tuple[int, int, int]:
    """``"YYYY-MM-DD"`` -> ``(year, month, day)``; ``ValueError`` says why not."""
    text = _clean(text)
    if not text:
        raise ValueError("Enter a date as YYYY-MM-DD")
    match = _DATE_RE.fullmatch(text)
    if match is None:
        raise ValueError(
            "The date must look like YYYY-MM-DD, for example 2026-10-10")
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
    """``"HH:MM"`` or ``"HHMM"``, 24-hour -> ``(hour, minute)``; ``ValueError``."""
    text = _clean(text)
    if not text:
        raise ValueError("Enter a time as HH:MM")
    match = _TIME_RE.fullmatch(text)
    if match is None:
        raise ValueError(
            "The time must look like HH:MM or HHMM (24-hour), for example 19:00")
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


def _utc(unix) -> datetime:
    """The aware UTC datetime of a unix timestamp (no platform time calls)."""
    return _EPOCH + timedelta(seconds=int(unix))


def _local(unix, utc_to_local) -> datetime:
    """The NAIVE local datetime of a unix timestamp."""
    convert = _default_utc_to_local if utc_to_local is None else utc_to_local
    return convert(_utc(unix))


# -- typed fields <-> unix ---------------------------------------------------
def to_unix(date_text: str, time_text: str, basis: str, *,
            local_to_utc=None) -> int:
    """The unix timestamp of a typed date + time. ``basis`` is ``"eve"`` (the
    fields are UTC) or ``"local"`` (they go through ``local_to_utc``).
    ``ValueError`` with a readable message for anything that cannot be used."""
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
            # The OS zone tables do not reach this date (Windows: before 1970).
            raise ValueError(
                "This computer cannot convert that local date to EVE time"
            ) from None
    return (aware - _EPOCH) // _ONE_SECOND


def fields_for(unix: int, basis: str, *, utc_to_local=None) -> tuple[str, str]:
    """``(date_text, time_text)`` that ``to_unix`` turns back into ``unix`` in
    the same basis (seconds are not part of the fields)."""
    _check_basis(basis)
    moment = _utc(unix) if basis == "eve" else _local(unix, utc_to_local)
    return ("%04d-%02d-%02d" % (moment.year, moment.month, moment.day),
            "%02d:%02d" % (moment.hour, moment.minute))


def quick_target(now_unix: int, delta_minutes: int) -> int:
    """``now + delta``, rounded UP to the next 5-minute mark (a moment already
    on a mark stays there)."""
    target = int(now_unix) + int(delta_minutes) * 60
    return -(-target // 300) * 300


# -- output ------------------------------------------------------------------
def tag(unix: int, style: str) -> str:
    """The Discord timestamp tag, e.g. ``<t:1791658800:t>``."""
    _check_style(style)
    return "<t:%d:%s>" % (int(unix), style)


def eve_label(unix: int, now_unix: int) -> str:
    """``19:00 EVE`` when ``unix`` falls on the EVE (UTC) date of ``now_unix``,
    else ``Sat 10 Oct 19:00 EVE``."""
    when, now = _utc(unix), _utc(now_unix)
    clock = "%02d:%02d EVE" % (when.hour, when.minute)
    if when.date() == now.date():
        return clock
    return "%s %d %s %s" % (_WEEKDAYS[when.weekday()][:3], when.day,
                            _MONTHS[when.month - 1][:3], clock)


def format_when(unix: int | None, preset: str, *, now_unix: int,
                now_text: str = "Now") -> str:
    """The text the When pill emits: ``now_text`` for no time, else the EVE
    label plus Discord tags in one of ``PRESETS`` (unknown -> the default)."""
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
    the local zone -- the preview's wording. ``R`` counts from ``now_unix``."""
    _check_style(style)
    if style == "R":
        return _relative(int(unix) - int(now_unix))
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
    far away it is -- ``already passed (...)`` once it is behind ``now_unix``."""
    local = _local(unix, utc_to_local)
    today = _local(now_unix, utc_to_local)
    local_part = "%02d:%02d local" % (local.hour, local.minute)
    if local.date() != today.date():
        local_part = "%s %s" % (_WEEKDAYS[local.weekday()][:3], local_part)
    delta = int(unix) - int(now_unix)
    wording = _relative(delta)
    if delta < 0:
        wording = "already passed (%s)" % wording
    separator = " %s " % chr(0xB7)                      # middle dot
    return separator.join((eve_label(unix, now_unix), local_part, wording))
