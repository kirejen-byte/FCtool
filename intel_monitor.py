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

INTEL_CHANNELS = {
    "I. Ftn Intel",
    "I. Delve & Q Intel",
    "I. C Ring Intel",
    "I. Aridia Intel",
    "I. OR Intel",
}

# Regex for detecting d-scan URLs (includes zero.the-initiative.rocks intel scans)
DSCAN_URL_PATTERN = re.compile(
    r"(https?://(?:dscan\.info|dscan\.me|adashboard\.info"
    r"|zero\.the-initiative\.rocks/intel/scan)/\S+)",
    re.IGNORECASE,
)

# Regex for hostile count: "5+ reds", "10 hostiles", "3 neuts", "+30", "30+", etc.
# Matches numbers followed by keywords OR standalone numbers with +
COUNT_PATTERN = re.compile(
    r"(\d+)\+?\s*(?:reds?|hostiles?|neuts?|pilots?|in\s+local)", re.IGNORECASE
)
# Fallback: bare numbers like "+30" or "30+" not attached to system-name-like tokens
BARE_COUNT_PATTERN = re.compile(
    r"(?<![A-Za-z\-])[\+]?(\d+)\+?(?![A-Za-z\-\d])", re.IGNORECASE
)

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

        # Extract pilot count if mentioned (from remaining text, after system name removed)
        count_match = COUNT_PATTERN.search(remaining)
        if count_match:
            report.pilot_count = int(count_match.group(1))
        else:
            # Fallback: try bare numbers like "+30" or "30+"
            # Strip any other system-name-like tokens first (e.g. "in XY-503")
            cleaned = _strip_system_refs(remaining, resolve_system)
            bare_match = BARE_COUNT_PATTERN.search(cleaned)
            if bare_match:
                val = int(bare_match.group(1))
                if val >= 1:
                    report.pilot_count = val

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
) -> list[dict]:
    """
    Scan chat log directory to find which intel channels are currently active.

    Returns list of dicts: {name, active, file_path}
    A channel is "active" if it has a log file modified today.
    """
    results = []
    today = date.today()

    for channel_name in sorted(INTEL_CHANNELS):
        # EVE log filenames start with the channel name
        # Format: "ChannelName_YYYYMMDD_HHMMSS.txt"
        # But spaces and dots in channel names are kept as-is
        pattern = os.path.join(logs_path, f"{channel_name}*.txt")
        matching_files = glob.glob(pattern)

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
    - Alliance Auth intel tool HTML (zero.the-initiative.rocks/intel/scan/)

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
    (e.g. zero.the-initiative.rocks/intel/scan/).

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
