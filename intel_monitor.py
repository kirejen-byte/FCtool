"""
Intelligence Fusion — Intel Channel Monitor & Parser
Parses EVE Online intel channel messages, extracts system names, hostile reports,
d-scan links, and fuses intel data for the web dashboard.
"""

import os
import re
import glob
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Callable

from chat_monitor import ChatMessage

# ── Report Coalescing ────────────────────────────────────────────────────────
# Tracks recent reports by (reporter, channel) to merge rapid-fire posts.
# Key: (reporter, channel) → IntelReport
_coalesce_buffer: dict[tuple[str, str], "IntelReport"] = {}
_COALESCE_WINDOW_SECONDS = 60  # Merge messages within this window


# ── Constants ────────────────────────────────────────────────────────────────

# First-run seed for the user's tracked intel channels. Intentionally empty so a
# fresh install is group-neutral; the GUI/web layers let users add their own
# channels, and this set is imported elsewhere purely as that initial seed.
INTEL_CHANNELS: set[str] = set()

# EVE writes one UTF-16LE log per channel-session, named
#   "<ChannelName>_YYYYMMDD_HHMMSS[_<charid>].txt"
# The trailing "_<charid>" exists in newer clients and is absent in old ones.
# The channel name is the substring BEFORE this suffix and may itself contain
# spaces, dots, dashes, brackets, parentheses, '+', and '&'.
CHAT_LOG_SUFFIX_PATTERN = re.compile(r"_(\d{8})_(\d{6})(?:_\d+)?\.txt$")

# EVE chat-log header lines, e.g. "  Channel ID:      -84651075". The value is
# captured raw (string): player channels use a NEGATIVE integer, built-in
# channels a small positive one, and the token could in principle be a
# non-integer — so callers must NOT int()-parse it. The leading "﻿?" tolerates a
# stray BOM if the line is read without BOM-stripping; ``open(encoding="utf-16")``
# already consumes the BOM, but the optional match keeps this robust either way.
CHANNEL_ID_HEADER_PATTERN = re.compile(
    r"^﻿?\s*Channel ID:\s*(.+?)\s*$", re.MULTILINE
)
CHANNEL_NAME_HEADER_PATTERN = re.compile(
    r"^﻿?\s*Channel Name:\s*(.+?)\s*$", re.MULTILINE
)

# Regex for detecting d-scan URLs from the common public d-scan hosts.
DSCAN_URL_PATTERN = re.compile(
    r"(https?://(?:dscan\.info|dscan\.me|adashboard\.info)/\S+)",
    re.IGNORECASE,
)

# Regex for hostile count: "5+ reds", "10 hostiles", "3 neuts", etc.
# Matches numbers followed by an explicit hostile/count keyword.
COUNT_PATTERN = re.compile(
    r"(\d+)\+?\s*(?:reds?|hostiles?|neuts?|pilots?|in\s+local)", re.IGNORECASE
)

# Tier 1: explicit plus-sign count forms — "+5", "5+" — unambiguous in EVE intel.
# Boundary on the non-'+' side prevents matches inside tokens like "1DQ1-A".
# The right side of "+5" deliberately allows a trailing letter (e.g. "+5s jita")
# since the plus already disambiguates; same for the left of "5+".
EXPLICIT_PLUS_COUNT_PATTERN = re.compile(
    r"(?<![A-Za-z\-\d])\+(\d+)(?![\-\d])"    # +5, +5s, +30 jita
    r"|"
    r"(?<![A-Za-z\-\d])(\d+)\+(?![A-Za-z\-\d])"  # 5+, 30+
)

# Tier 2: bare integer. Kept for callers that want the raw regex, but the parser
# gates its use with HOSTILE_CONTEXT_PATTERN so bare digits only count when the
# message has a hostile-context keyword (avoids matching clock times, ticket
# numbers, ISK amounts, etc.).
BARE_COUNT_PATTERN = re.compile(
    r"(?<![A-Za-z\-\d:#/])(\d+)(?![A-Za-z\-\d:])", re.IGNORECASE
)

# Hostile-context keywords that license a bare digit as a pilot count.
# Case-insensitive word-boundary match anywhere in the normalized message.
HOSTILE_CONTEXT_PATTERN = re.compile(
    r"\b(?:hostiles?|reds?|neuts?|enem(?:y|ies)|clr|pilots?|dudes?|guys?|gang)\b",
    re.IGNORECASE,
)

# How close (in characters, either side) a hostile-context keyword must be to
# a bare-digit candidate for that digit to be accepted as a pilot count.
# Tuned so the keyword and digit stay in the same clause: small enough to
# reject "the reds left, 5 jumps to go" but wide enough to accept
# "gang of 8" and short adjacency phrases like "clr 5".
BARE_COUNT_PROXIMITY = 10

# Regex for clear reports
CLEAR_PATTERN = re.compile(r"\bclr\b|\bclear\b|\bnv\b|\bnvi\b", re.IGNORECASE)

# Regex for gate camp / bubble mentions
CAMP_PATTERN = re.compile(r"\bcamp\b|\bbubble[ds]?\b|\bgate\s*camp", re.IGNORECASE)

# Regex for spike
SPIKE_PATTERN = re.compile(r"\bspike\b", re.IGNORECASE)


# ── Data Model ───────────────────────────────────────────────────────────────

@dataclass
class IntelReport:
    timestamp: datetime
    channel: str
    reporter: str
    system_name: str  # Extracted system name, or "" if not identified
    system_id: int | None = None
    region_name: str = ""
    report_type: str = "hostile"  # hostile, clear, dscan, info
    raw_message: str = ""
    pilot_count: int | None = None
    dscan_url: str = ""
    dscan_ships: dict[str, int] | None = None
    dscan_total: int = 0
    dscan_summary: str = ""  # Short summary: "15 ships, 8 Retributions"
    route_from_staging: str = ""
    has_camp: bool = False
    has_spike: bool = False
    has_cyno_beacon: bool = False
    characters: list[dict] | None = None  # [{name, id, corp, alliance}]

    def serialize(self) -> dict:
        return {
            "type": "intel",
            "timestamp": self.timestamp.isoformat() if self.timestamp else "",
            "channel": self.channel,
            "reporter": self.reporter,
            "system_name": self.system_name,
            "system_id": self.system_id,
            "region_name": self.region_name,
            "report_type": self.report_type,
            "raw_message": self.raw_message,
            "pilot_count": self.pilot_count,
            "dscan_url": self.dscan_url,
            "dscan_ships": self.dscan_ships,
            "dscan_total": self.dscan_total,
            "dscan_summary": self.dscan_summary,
            "route_from_staging": self.route_from_staging,
            "has_camp": self.has_camp,
            "has_spike": self.has_spike,
            "has_cyno_beacon": self.has_cyno_beacon,
            "characters": self.characters,
            "dotlan_url": f"https://evemaps.dotlan.net/system/{self.system_name.replace(' ', '_')}" if self.system_name else "",
        }


def coalesce_report(report: "IntelReport") -> tuple["IntelReport", bool]:
    """
    Merge rapid-fire posts from the same reporter into a single report.

    Returns (report, is_new):
      - is_new=True  → this is a fresh report, display it
      - is_new=False → this was merged into an existing report, push an update
    """
    key = (report.reporter, report.channel)
    now = report.timestamp

    # Check if there's a recent report from the same person
    existing = _coalesce_buffer.get(key)
    if existing and (now - existing.timestamp).total_seconds() <= _COALESCE_WINDOW_SECONDS:
        # Merge into existing report
        existing.raw_message += "  //  " + report.raw_message
        # Upgrade fields if the new report has better data
        if report.system_name and not existing.system_name:
            existing.system_name = report.system_name
            existing.system_id = report.system_id
        if report.pilot_count and (not existing.pilot_count or report.pilot_count > existing.pilot_count):
            existing.pilot_count = report.pilot_count
        if report.dscan_url and not existing.dscan_url:
            existing.dscan_url = report.dscan_url
        if report.has_camp:
            existing.has_camp = True
        if report.has_spike:
            existing.has_spike = True
        if report.report_type == "dscan" and existing.report_type != "dscan":
            existing.report_type = "dscan"
        if report.characters:
            existing.characters = (existing.characters or []) + report.characters
        return existing, False

    # New report — store it
    _coalesce_buffer[key] = report

    # Clean up old entries
    cutoff = now
    for k in list(_coalesce_buffer):
        if (cutoff - _coalesce_buffer[k].timestamp).total_seconds() > _COALESCE_WINDOW_SECONDS * 3:
            del _coalesce_buffer[k]

    return report, True


def make_dscan_summary(ships: dict[str, int], total: int) -> str:
    """Create a short summary like '15 ships, 8 Retributions'."""
    if not ships or total == 0:
        return ""
    top_name, top_count = max(ships.items(), key=lambda x: x[1])
    return f"{total} ships, {top_count} {top_name}"


# ── Message Parsing ──────────────────────────────────────────────────────────

def parse_intel_message(
    msg: ChatMessage,
    resolve_system: Callable[[str], int | None],
) -> IntelReport | None:
    """
    Parse an intel channel message into an IntelReport.

    Args:
        msg: ChatMessage from the chat log monitor
        resolve_system: Function that takes a system name and returns its ID or None
                       (typically jump_range.search_system)

    Returns:
        IntelReport or None if the message can't be parsed as intel
    """
    text = msg.message.strip()
    if not text:
        return None

    # Skip very short messages that are just punctuation or emotes
    if len(text) <= 1:
        return None

    report = IntelReport(
        timestamp=msg.timestamp,
        channel=msg.channel,
        reporter=msg.sender,
        system_name="",
        raw_message=text,
    )

    # Extract dscan URL if present
    dscan_match = DSCAN_URL_PATTERN.search(text)
    if dscan_match:
        report.dscan_url = dscan_match.group(1)
        # Remove the URL from text for further parsing
        text = text[:dscan_match.start()] + text[dscan_match.end():]
        text = text.strip()

    # Detect camp/bubble mentions
    if CAMP_PATTERN.search(text):
        report.has_camp = True

    # Detect spike mentions
    if SPIKE_PATTERN.search(text):
        report.has_spike = True

    # Try to extract system name from the beginning of the message
    # EVE system names: letters, numbers, dashes (e.g., "C-N4OD", "1DQ1-A", "NOL-M9")
    # Also multi-word names like "Korasen" or "Old Man Star"
    system_name, remaining = _extract_system_name(text, resolve_system)

    if system_name:
        report.system_name = system_name
        report.system_id = resolve_system(system_name)

        # Classify the remaining text
        remaining = remaining.strip()

        if CLEAR_PATTERN.search(remaining):
            report.report_type = "clear"
        elif report.dscan_url:
            report.report_type = "dscan"
        else:
            report.report_type = "hostile"

        # Extract pilot count using a three-tier approach:
        #   1. Keyword-adjacent count (COUNT_PATTERN)  — "5 hostiles", "3 neuts"
        #   2. Explicit plus-sign form (EXPLICIT_PLUS)  — "+5", "5+"
        #   3. Bare integer, ONLY if a hostile-context keyword
        #      (HOSTILE_CONTEXT_PATTERN) is *within BARE_COUNT_PROXIMITY
        #      characters* of the digit on either side. A whole-message
        #      gate is too loose — e.g. "the reds left, 5 jumps to go"
        #      would otherwise produce pilot_count=5. Proximity keeps the
        #      digit and the licensing keyword in the same clause.
        # Strip system-name-like tokens first (e.g. "in XY-503") so their
        # digits aren't mistaken for pilot counts.
        cleaned = _strip_system_refs(remaining, resolve_system)

        count_match = COUNT_PATTERN.search(remaining)
        if count_match:
            report.pilot_count = int(count_match.group(1))
        else:
            plus_match = EXPLICIT_PLUS_COUNT_PATTERN.search(cleaned)
            if plus_match:
                val = int(plus_match.group(1) or plus_match.group(2))
                if val >= 1:
                    report.pilot_count = val
            else:
                # Proximity-gated bare-digit tier: accept the first bare
                # digit whose surrounding window contains a hostile keyword.
                for bare_match in BARE_COUNT_PATTERN.finditer(cleaned):
                    window_start = max(0, bare_match.start() - BARE_COUNT_PROXIMITY)
                    window_end = bare_match.end() + BARE_COUNT_PROXIMITY
                    window = cleaned[window_start:window_end]
                    if HOSTILE_CONTEXT_PATTERN.search(window):
                        val = int(bare_match.group(1))
                        if val >= 1:
                            report.pilot_count = val
                            break

    elif report.dscan_url:
        # Has dscan link but no system name identified
        report.report_type = "dscan"
    else:
        # No system name found — treat as general info
        report.report_type = "info"

    return report


def _extract_system_name(
    text: str,
    resolve_system: Callable[[str], int | None],
) -> tuple[str, str]:
    """
    Try to extract an EVE system name from the start of a message.
    Returns (system_name, remaining_text) or ("", original_text).
    """
    # Split on whitespace, try matching progressively fewer tokens
    tokens = text.split()
    if not tokens:
        return "", text

    # Try up to 3 tokens (handles names like "Old Man Star")
    max_tokens = min(3, len(tokens))
    for n in range(max_tokens, 0, -1):
        candidate = " ".join(tokens[:n])
        # Clean common prefixes/suffixes from system names in intel
        candidate_clean = candidate.strip("*!?.,;:")
        if not candidate_clean:
            continue

        system_id = resolve_system(candidate_clean)
        if system_id:
            remaining = " ".join(tokens[n:])
            return candidate_clean, remaining

    return "", text


def _strip_system_refs(
    text: str,
    resolve_system: Callable[[str], int | None],
) -> str:
    """
    Remove system-name-like tokens from text so their numbers aren't
    mistaken for pilot counts.  E.g. "+30 in XY-503" → "+30 in "
    """
    tokens = text.split()
    result = []
    i = 0
    while i < len(tokens):
        token = tokens[i].strip("*!?.,;:")
        if token and resolve_system(token):
            i += 1  # skip this token — it's a system name
            continue
        result.append(tokens[i])
        i += 1
    return " ".join(result)


# ── Channel Scanning ─────────────────────────────────────────────────────────

def scan_available_channels(
    logs_path: str,
    tracked_character: str | None = None,
    channels=None,
) -> list[dict]:
    """
    Scan chat log directory to find which intel channels are currently active.

    Args:
        logs_path: Directory containing EVE chat logs.
        tracked_character: If set, only count log files whose "Listener:" header
            (matched here against the first 2KB of the file) contains this name.
        channels: Iterable of channel names to check. Defaults to INTEL_CHANNELS
            when None, preserving the original behavior for existing callers.

    Returns list of dicts: {name, active, file_path}
    A channel is "active" if it has a log file modified today.
    """
    if channels is None:
        channels = INTEL_CHANNELS

    results = []
    today = date.today()

    # Glob the logs dir once and match channel-name prefixes case-INSENSITIVELY,
    # so case-sensitive filesystems (Linux) behave like Windows.
    all_txt = glob.glob(os.path.join(logs_path, "*.txt"))

    for channel_name in sorted(channels):
        cn_lower = channel_name.lower()
        matching_files = [
            fp for fp in all_txt
            if os.path.basename(fp).lower().startswith(cn_lower)
        ]

        active = False
        latest_file = None
        latest_mtime = 0

        for filepath in matching_files:
            # If we have a character filter, check the file's Listener header
            if tracked_character:
                try:
                    with open(filepath, "r", encoding="utf-16-le", errors="replace") as f:
                        header = f.read(2048)
                    if tracked_character.lower() not in header.lower():
                        continue
                except OSError:
                    continue

            try:
                mtime = os.path.getmtime(filepath)
                mod_date = date.fromtimestamp(mtime)
                if mod_date == today:
                    active = True
                if mtime > latest_mtime:
                    latest_mtime = mtime
                    latest_file = filepath
            except OSError:
                continue

        results.append({
            "name": channel_name,
            "active": active,
            "file_path": latest_file,
        })

    return results


def discover_channels(
    logs_path: str,
    tracked_character: str | None = None,
    max_age_days: int | None = 30,
) -> list[dict]:
    """
    Discover every chat channel that has a log file in ``logs_path``, derived
    purely from the on-disk filenames.

    Unlike :func:`scan_available_channels`, this does not start from a fixed set
    of channel names — it returns ALL channels found on disk so a UI layer can
    filter as it sees fit. This helper is intentionally decision-neutral: no
    intel-name filtering, no noise filtering beyond the ``max_age_days`` bound.

    Channel-name derivation:
        EVE log files are named "<ChannelName>_YYYYMMDD_HHMMSS[_<charid>].txt".
        The channel name is the substring BEFORE the trailing timestamp/charid
        suffix (see ``CHAT_LOG_SUFFIX_PATTERN``). The "_<charid>" segment is
        optional (present in newer clients, absent in old ones), and channel
        names may legitimately contain spaces, '.', '-', '[', ']', '(', ')',
        '+', and '&'. Files whose names do not match the pattern are skipped,
        making this robust against unrelated files in the directory.

    Multiple sessions/charids of the same channel collapse into ONE entry,
    keyed by the derived channel name. The entry reports the newest matching
    file.

    Args:
        logs_path: Directory containing EVE chat logs. A missing, empty, or
            non-directory path yields an empty list.
        tracked_character: If provided, narrows results to channels whose newest
            log file lists this character in its "Listener:" header. To stay
            efficient, the header is read lazily for at most ONE file per
            distinct channel (the newest), rather than for every session file
            across years of history. Matching is case-insensitive substring,
            mirroring :func:`scan_available_channels`.
        max_age_days: Exclude channels whose newest matching file is older than
            this many days (bounds noise from years of accumulated logs). Pass
            ``None`` to include channels of any age.

    Returns:
        A list of dicts sorted by name (case-insensitive), each shaped::

            {
                "name": str,            # derived channel name
                "active": bool,         # newest file modified within the last day (today)
                "last_modified": float, # epoch seconds of the newest matching file
                "file_path": str,       # absolute path of the newest matching file
            }
    """
    if not logs_path or not os.path.isdir(logs_path):
        return []

    try:
        entries = os.listdir(logs_path)
    except OSError:
        return []

    today = date.today()

    # Aggregate newest file per distinct channel name.
    # name -> {"file_path": str, "last_modified": float}
    newest: dict[str, dict] = {}

    for filename in entries:
        match = CHAT_LOG_SUFFIX_PATTERN.search(filename)
        if not match:
            continue  # not an EVE chat log filename — skip junk

        channel_name = filename[: match.start()]
        if not channel_name:
            continue  # defensive: no name before the suffix

        filepath = os.path.join(logs_path, filename)
        try:
            mtime = os.path.getmtime(filepath)
        except OSError:
            continue

        prev = newest.get(channel_name)
        if prev is None or mtime > prev["last_modified"]:
            newest[channel_name] = {
                "file_path": filepath,
                "last_modified": mtime,
            }

    results = []
    for channel_name, info in newest.items():
        mtime = info["last_modified"]

        # Age bound: drop channels whose newest file is too old.
        if max_age_days is not None:
            age_days = (today - date.fromtimestamp(mtime)).days
            if age_days > max_age_days:
                continue

        # Optional character narrowing: read the Listener header of only the
        # newest file for this channel (one header read per distinct channel).
        if tracked_character:
            try:
                with open(info["file_path"], "r", encoding="utf-16-le",
                          errors="replace") as f:
                    header = f.read(2048)
            except OSError:
                continue
            if tracked_character.lower() not in header.lower():
                continue

        active = date.fromtimestamp(mtime) == today

        results.append({
            "name": channel_name,
            "active": active,
            "last_modified": mtime,
            "file_path": info["file_path"],
        })

    results.sort(key=lambda d: d["name"].lower())
    return results


def read_channel_id(logs_path: str, channel_name: str) -> str | None:
    """Read a channel's numeric ID from the header of its newest log file.

    EVE chat logs begin (after a UTF-16 BOM) with a header block that, for a
    PLAYER channel, carries ``Channel ID:  -84651075`` (a NEGATIVE integer) and
    ``Channel Name:  <name>``. This finds the candidate ``.txt`` logs for
    ``channel_name`` using the SAME case-insensitive filename-prefix matching as
    :func:`scan_available_channels`/:func:`discover_channels`, then — among those
    whose header ``Channel Name:`` equals ``channel_name`` (case-insensitive,
    stripped) — reads the NEWEST by mtime and returns its raw ``Channel ID``.

    The id is returned as a STRING with any leading minus preserved and is NOT
    int()-parsed: it is treated as an opaque token (it could be a hex/UUID form
    in some clients), and the caller decides how to interpret it.

    Args:
        logs_path: Directory containing EVE chat logs.
        channel_name: The channel's display name (matched case-insensitively).

    Returns:
        The raw ``Channel ID`` value as a string, or ``None`` if no candidate
        log, no name-matching header, or no id could be read (including on any
        I/O or decode error).
    """
    if not logs_path or not channel_name or not os.path.isdir(logs_path):
        return None

    cn_lower = channel_name.lower()
    want_name = channel_name.strip().lower()

    # Same case-insensitive filename-prefix match used elsewhere in this module.
    all_txt = glob.glob(os.path.join(logs_path, "*.txt"))
    candidates = [
        fp for fp in all_txt
        if os.path.basename(fp).lower().startswith(cn_lower)
    ]

    # Sort newest-first so the first header whose Channel Name matches wins.
    def _mtime(fp: str) -> float:
        try:
            return os.path.getmtime(fp)
        except OSError:
            return -1.0

    candidates.sort(key=_mtime, reverse=True)

    for filepath in candidates:
        try:
            # encoding="utf-16" consumes the BOM EVE writes at the top of the file.
            with open(filepath, encoding="utf-16") as f:
                header = f.read(2048)
        except (OSError, UnicodeError, ValueError):
            continue

        name_match = CHANNEL_NAME_HEADER_PATTERN.search(header)
        if not name_match:
            continue
        if name_match.group(1).strip().lower() != want_name:
            continue

        id_match = CHANNEL_ID_HEADER_PATTERN.search(header)
        if not id_match:
            # Newest name-matching file has no id — treat as not found rather
            # than falling back to an older session that might be a different
            # channel that merely shares this filename prefix.
            return None
        return id_match.group(1).strip()

    return None


# ── D-Scan Parsing ───────────────────────────────────────────────────────────

# Capital ship names for dscan classification
CAPITAL_SHIP_NAMES = {
    # Dreadnoughts
    "Revelation", "Naglfar", "Moros", "Phoenix",
    "Chemosh", "Caiman", "Zirnitra", "Bane",
    # Carriers
    "Archon", "Thanatos", "Nidhoggur", "Chimera",
    "Vanguard", "Lif",
    # FAX
    "Apostle", "Ninazu", "Lif", "Minokawa",
    # Supercarriers
    "Aeon", "Nyx", "Hel", "Wyvern", "Vendetta", "Revenant",
    # Titans
    "Avatar", "Erebus", "Ragnarok", "Leviathan", "Molok", "Komodo",
    # Rorqual
    "Rorqual",
}


# Regex for extracting ship rows from Alliance Auth intel tool HTML tables
# Matches: <td>ShipName</td> ... <td>Count</td> patterns in table rows
_HTML_TABLE_ROW = re.compile(
    r"<tr[^>]*>\s*<td[^>]*>(?:<[^>]+>)*\s*([^<]+?)\s*(?:</[^>]+>)*</td>"
    r"(?:\s*<td[^>]*>.*?</td>)*?"
    r"\s*<td[^>]*>\s*(\d+)\s*</td>\s*</tr>",
    re.IGNORECASE | re.DOTALL,
)

# Detect whether text is HTML
_IS_HTML = re.compile(r"<\s*(?:html|head|body|table|div)\b", re.IGNORECASE)


def parse_dscan_text(text: str) -> dict:
    """
    Parse d-scan output from various formats:
    - Raw EVE d-scan paste (tab-separated)
    - dscan.info summary pages
    - Alliance Auth intel tool HTML (e.g. an Alliance-Auth /intel/scan/ page)

    Returns: {ships: {name: count}, total: int, capital_count: int}
    """
    # If it looks like HTML, use the HTML table parser
    if _IS_HTML.search(text[:500]):
        return _parse_dscan_html(text)

    ships: dict[str, int] = {}
    total = 0
    capital_count = 0

    for line in text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue

        # Try tab-separated format (raw dscan paste)
        parts = line.split("\t")
        if len(parts) >= 2:
            ship_name = parts[1].strip() if len(parts) > 1 else parts[0].strip()
            # Count each line as 1 ship
            ships[ship_name] = ships.get(ship_name, 0) + 1
            total += 1
            if ship_name in CAPITAL_SHIP_NAMES:
                capital_count += 1
            continue

        # Try "Ship Name    Count" format (dscan.info summary)
        match = re.match(r"^(.+?)\s{2,}(\d+)$", line)
        if match:
            ship_name = match.group(1).strip()
            count = int(match.group(2))
            ships[ship_name] = ships.get(ship_name, 0) + count
            total += count
            if ship_name in CAPITAL_SHIP_NAMES:
                capital_count += count

    return {
        "ships": ships,
        "total": total,
        "capital_count": capital_count,
    }


def _parse_dscan_html(html: str) -> dict:
    """
    Parse d-scan data from Alliance Auth intel tool HTML pages
    (e.g. an Alliance-Auth /intel/scan/ page).

    The page has tables with "Ship Class" / "Count" columns.
    The first table under "All Ships" contains the aggregate data.
    """
    ships: dict[str, int] = {}
    total = 0
    capital_count = 0

    # Find the "All Ships" section — it's the first table after that heading
    # The HTML structure: <h3>All Ships (N)</h3> ... <table>...</table>
    all_ships_marker = re.search(
        r"All Ships\s*\(?(\d+)\)?", html, re.IGNORECASE
    )
    if all_ships_marker:
        # Search for the first table after the marker
        search_start = all_ships_marker.end()
        table_match = re.search(
            r"<table\b[^>]*>(.*?)</table>",
            html[search_start:search_start + 10000],
            re.IGNORECASE | re.DOTALL,
        )
        if table_match:
            table_html = table_match.group(1)
            # Extract rows: <td>ShipName</td><td>Count</td>
            # The actual structure has images and sorting controls inside tds,
            # so we strip tags from the ship name cell
            for row_match in re.finditer(
                r"<tr[^>]*>(.*?)</tr>", table_html, re.DOTALL | re.IGNORECASE
            ):
                row = row_match.group(1)
                cells = re.findall(
                    r"<td[^>]*>(.*?)</td>", row, re.DOTALL | re.IGNORECASE
                )
                if len(cells) >= 2:
                    # Strip HTML tags from ship name
                    name = re.sub(r"<[^>]+>", "", cells[0]).strip()
                    count_text = re.sub(r"<[^>]+>", "", cells[-1]).strip()
                    if name and count_text.isdigit():
                        count = int(count_text)
                        ships[name] = ships.get(name, 0) + count
                        total += count
                        if name in CAPITAL_SHIP_NAMES:
                            capital_count += count
            if ships:
                return {
                    "ships": ships,
                    "total": total,
                    "capital_count": capital_count,
                }

    # Fallback: try to find ANY table rows with name/count pattern
    for row_match in _HTML_TABLE_ROW.finditer(html):
        name = re.sub(r"<[^>]+>", "", row_match.group(1)).strip()
        count_text = row_match.group(2).strip()
        if name and count_text.isdigit() and not name.startswith("Ship"):
            count = int(count_text)
            ships[name] = ships.get(name, 0) + count
            total += count
            if name in CAPITAL_SHIP_NAMES:
                capital_count += count

    return {"ships": ships, "total": total, "capital_count": capital_count}


# ── Character Resolution ─────────────────────────────────────────────────────

ESI_BASE = "https://esi.evetech.net/latest"
ESI_HEADERS = {"User-Agent": "FCTool/1.0 (EVE FC Assistant)"}

# ── Standings Whitelist ──────────────────────────────────────────────────────
# Cache of entity IDs (characters, corps, alliances) with positive standing.
# Anyone NOT on this list is assumed hostile (including neutrals).
_standings_whitelist: set[int] = set()
_standings_loaded: bool = False


def load_standings_whitelist(esi_auth) -> set[int]:
    """
    Load positive-standing entities from ESI contacts.
    Uses the authenticated character's contacts (personal, corp, alliance).
    Returns a set of entity IDs with standing > 0.

    Args:
        esi_auth: An ESIAuth instance (from esi_auth.py) or None
    """
    global _standings_whitelist, _standings_loaded
    if _standings_loaded:
        return _standings_whitelist

    whitelist = set()
    if not esi_auth or not hasattr(esi_auth, 'esi_get'):
        _standings_loaded = True
        return whitelist

    try:
        char_id = getattr(esi_auth, '_character_id', None)
        if not char_id:
            _standings_loaded = True
            return whitelist

        # Personal contacts
        contacts = esi_auth.esi_get(f"/characters/{char_id}/contacts/")
        if contacts:
            for c in contacts:
                if c.get("standing", 0) > 0:
                    whitelist.add(c["contact_id"])

        # Corporation contacts
        corp_id = None
        char_info = esi_auth.esi_get(f"/characters/{char_id}/")
        if char_info:
            corp_id = char_info.get("corporation_id")
        if corp_id:
            corp_contacts = esi_auth.esi_get(f"/corporations/{corp_id}/contacts/")
            if corp_contacts:
                for c in corp_contacts:
                    if c.get("standing", 0) > 0:
                        whitelist.add(c["contact_id"])

        # Alliance contacts
        if corp_id:
            corp_info = esi_auth.esi_get(f"/corporations/{corp_id}/")
            if corp_info:
                alliance_id = corp_info.get("alliance_id")
                if alliance_id:
                    # Add own alliance members implicitly
                    whitelist.add(alliance_id)
                    ally_contacts = esi_auth.esi_get(f"/alliances/{alliance_id}/contacts/")
                    if ally_contacts:
                        for c in ally_contacts:
                            if c.get("standing", 0) > 0:
                                whitelist.add(c["contact_id"])

    except Exception as e:
        print(f"[Intel] Error loading standings whitelist: {e}")

    _standings_whitelist = whitelist
    _standings_loaded = True
    print(f"[Intel] Loaded standings whitelist: {len(whitelist)} entities")
    return whitelist


def is_hostile(character_info: dict, whitelist: set[int] | None = None) -> bool:
    """
    Check if a character is hostile (not on the positive-standing whitelist).
    If no whitelist is available, assumes hostile.
    """
    if whitelist is None:
        whitelist = _standings_whitelist

    char_id = character_info.get("character_id", 0)
    if char_id and char_id in whitelist:
        return False

    # Check corp/alliance membership
    # (resolve_characters stores corp and alliance as names, not IDs)
    # For now, if the character_id isn't directly in the whitelist, they're hostile
    return True

# Keywords/tokens that should NOT be treated as character names
_INTEL_KEYWORDS = {
    "clr", "clear", "nv", "nvi", "spike", "camp", "bubble", "bubbles",
    "gate", "gatecamp", "reds", "red", "hostiles", "hostile", "neuts",
    "neut", "pilots", "pilot", "local", "in", "on", "at", "to", "from",
    "and", "or", "the", "a", "an", "with", "no", "yes", "afk",
    "+1", "+2", "+3", "+4", "+5", "status", "warp", "jump", "cyno",
    "titan", "bridge", "dread", "fax", "carrier", "super", "rorqual",
}


def resolve_characters(
    raw_message: str,
    system_name: str,
    resolve_system: Callable[[str], int | None],
) -> list[dict]:
    """
    Try to identify character names in an intel message and resolve them via ESI.
    Returns list of {name, character_id, corporation, alliance} dicts.

    Uses the public ESI /universe/ids/ endpoint (no auth needed).
    """
    import requests as _req

    # Extract candidate names: tokens that are NOT system names, keywords, URLs, or numbers
    tokens = raw_message.split()
    candidates = []
    skip_next = 0
    for i, token in enumerate(tokens):
        if skip_next > 0:
            skip_next -= 1
            continue
        clean = token.strip("*!?.,;:+()[]<>")
        if not clean or len(clean) < 2:
            continue
        low = clean.lower()
        if low in _INTEL_KEYWORDS:
            continue
        if clean.startswith("http"):
            continue
        if clean.replace("+", "").replace("-", "").isdigit():
            continue
        if clean == system_name:
            continue
        if resolve_system(clean):
            continue
        # Multi-word names: try "First Last" (2 tokens)
        if i + 1 < len(tokens):
            next_clean = tokens[i + 1].strip("*!?.,;:+()[]<>")
            if (next_clean and len(next_clean) >= 2
                    and next_clean.lower() not in _INTEL_KEYWORDS
                    and not resolve_system(next_clean)):
                candidates.append(f"{clean} {next_clean}")
                skip_next = 1
                continue
        candidates.append(clean)

    if not candidates:
        return []

    # Resolve via ESI /universe/ids/
    results = []
    try:
        resp = _req.post(
            f"{ESI_BASE}/universe/ids/",
            json=candidates,
            headers=ESI_HEADERS,
            timeout=8,
        )
        if not resp.ok:
            return []
        data = resp.json()
        for char in data.get("characters", [])[:5]:
            info = {"name": char["name"], "character_id": char["id"],
                    "corporation": "", "alliance": ""}
            try:
                cresp = _req.get(
                    f"{ESI_BASE}/characters/{char['id']}/",
                    headers=ESI_HEADERS, timeout=5,
                )
                if cresp.ok:
                    cdata = cresp.json()
                    corp_id = cdata.get("corporation_id")
                    alliance_id = cdata.get("alliance_id")
                    if alliance_id:
                        aresp = _req.get(
                            f"{ESI_BASE}/alliances/{alliance_id}/",
                            headers=ESI_HEADERS, timeout=5,
                        )
                        if aresp.ok:
                            info["alliance"] = aresp.json().get("name", "")
                    if corp_id:
                        cresp2 = _req.get(
                            f"{ESI_BASE}/corporations/{corp_id}/",
                            headers=ESI_HEADERS, timeout=5,
                        )
                        if cresp2.ok:
                            info["corporation"] = cresp2.json().get("name", "")
            except Exception:
                pass
            info["hostile"] = is_hostile(info)
            results.append(info)
    except Exception:
        pass
    return results
