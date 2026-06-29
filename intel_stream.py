"""
intel_stream — pure, fast, deterministic annotation of raw intel lines.

No Tk, no network. Imports the existing regexes from intel_monitor (does NOT
duplicate them) and system_coords.resolve_name for system detection. Produces
non-overlapping Spans (longest match wins on conflict). 'name' spans are NOT
produced here -- they are added asynchronously by intel_resolver.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from system_coords import resolve_name
from intel_monitor import (
    DSCAN_URL_PATTERN,
    COUNT_PATTERN,
    EXPLICIT_PLUS_COUNT_PATTERN,
    BARE_COUNT_PATTERN,
    HOSTILE_CONTEXT_PATTERN,
    BARE_COUNT_PROXIMITY,
    CLEAR_PATTERN,
    CAMP_PATTERN,
    SPIKE_PATTERN,
    CYNO_PATTERN,
    _INTEL_KEYWORDS,
)


@dataclass(frozen=True)
class Span:
    start: int          # char offset into the message text (inclusive)
    end: int            # exclusive
    kind: str           # 'system'|'count'|'dscan_url'|'clear'|'camp'|'spike'|'cyno'|'name'
    value: str          # the matched substring
    payload: dict       # kind-specific: {'system_id': int}, {'count': int}, {'url': str}, else {}


# Word tokens with their char offsets. EVE system names contain letters,
# digits and dashes (e.g. "1DQ1-A", "C-N4OD"), so include '-' in the token.
_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-]*")


def _system_spans(text: str) -> list[Span]:
    """Slide 1->3-word windows across the line; a window is a system iff
    resolve_name(window) is non-None. Longest match wins; spans are
    non-overlapping. Single-word matches shorter than 3 chars are ignored."""
    words = [(m.group(0), m.start(), m.end()) for m in _WORD_RE.finditer(text)]
    spans: list[Span] = []
    i = 0
    n = len(words)
    while i < n:
        matched = None
        # Prefer the longest window (3 -> 2 -> 1 words) starting at i.
        for size in (3, 2, 1):
            if i + size > n:
                continue
            chunk = words[i:i + size]
            phrase = " ".join(w for w, _s, _e in chunk)
            if size == 1 and len(phrase) < 3:
                continue
            sid = resolve_name(phrase)
            if sid is not None:
                start = chunk[0][1]
                end = chunk[-1][2]
                matched = Span(start=start, end=end, kind="system",
                               value=text[start:end],
                               payload={"system_id": sid})
                i += size
                break
        if matched is not None:
            spans.append(matched)
        else:
            i += 1
    return spans


def _overlaps(start: int, end: int, taken: list[Span]) -> bool:
    return any(not (end <= s.start or start >= s.end) for s in taken)


def _count_spans(text: str, taken: list[Span]) -> list[Span]:
    """count spans via the three intel_monitor tiers, non-overlapping with
    already-taken (system) spans. payload={'count': int}."""
    out: list[Span] = []
    claimed: list[Span] = []

    def _try_add(start: int, end: int, value: int):
        if value < 1:
            return
        if _overlaps(start, end, taken) or _overlaps(start, end, claimed):
            return
        sp = Span(start=start, end=end, kind="count",
                  value=text[start:end], payload={"count": value})
        out.append(sp)
        claimed.append(sp)

    # Tier 1: keyword-adjacent ("5 reds"). group(1) is the digits.
    for m in COUNT_PATTERN.finditer(text):
        _try_add(m.start(1), m.end(1), int(m.group(1)))
    # Tier 2: explicit plus ("+5"/"5+"). One of the two groups holds the digits.
    for m in EXPLICIT_PLUS_COUNT_PATTERN.finditer(text):
        digits = m.group(1) or m.group(2)
        gi = 1 if m.group(1) else 2
        _try_add(m.start(gi), m.end(gi), int(digits))
    # Tier 3: bare digit, only when a hostile keyword is within proximity.
    for m in BARE_COUNT_PATTERN.finditer(text):
        ws = max(0, m.start() - BARE_COUNT_PROXIMITY)
        we = m.end() + BARE_COUNT_PROXIMITY
        if HOSTILE_CONTEXT_PATTERN.search(text[ws:we]):
            _try_add(m.start(1), m.end(1), int(m.group(1)))
    return out


_KEYWORD_PATTERNS = (
    ("clear", CLEAR_PATTERN),
    ("camp", CAMP_PATTERN),
    ("spike", SPIKE_PATTERN),
    ("cyno", CYNO_PATTERN),
)


def _keyword_and_url_spans(text: str, taken: list[Span]) -> list[Span]:
    out: list[Span] = []
    claimed: list[Span] = []

    def _add(start: int, end: int, kind: str, payload: dict):
        if _overlaps(start, end, taken) or _overlaps(start, end, claimed):
            return
        sp = Span(start=start, end=end, kind=kind,
                  value=text[start:end], payload=payload)
        out.append(sp)
        claimed.append(sp)

    for m in DSCAN_URL_PATTERN.finditer(text):
        _add(m.start(1), m.end(1), "dscan_url", {"url": m.group(1)})
    for kind, pat in _KEYWORD_PATTERNS:
        for m in pat.finditer(text):
            _add(m.start(), m.end(), kind, {})
    return out


def annotate(text: str) -> list[Span]:
    """Detect important spans in a raw intel line. Pure; deterministic;
    non-overlapping (longest match wins on conflict). Total: never raises.
    'name' spans are NOT produced here."""
    if not text:
        return []
    try:
        spans = _system_spans(text)
        spans += _keyword_and_url_spans(text, spans)
        spans += _count_spans(text, spans)
    except Exception:
        return []
    spans.sort(key=lambda s: s.start)
    return spans


def candidate_names(text: str) -> list[str]:
    """Probable pilot-name candidates for the async resolver: tokens that are
    NOT system names, intel keywords, URLs, or bare numbers. Two adjacent
    eligible tokens fuse into a single 'First Last' candidate (one skip).
    Pure; deterministic. Shared by intel_monitor.resolve_characters (DRY)."""
    if not text:
        return []
    tokens = text.split()
    candidates: list[str] = []
    skip_next = 0
    for i, token in enumerate(tokens):
        if skip_next > 0:
            skip_next -= 1
            continue
        clean = token.strip("*!?.,;:+()[]<>")
        if not clean or len(clean) < 2:
            continue
        if clean.lower() in _INTEL_KEYWORDS:
            continue
        if clean.startswith("http"):
            continue
        if clean.replace("+", "").replace("-", "").isdigit():
            continue
        if resolve_name(clean) is not None:
            continue
        if i + 1 < len(tokens):
            next_clean = tokens[i + 1].strip("*!?.,;:+()[]<>")
            if (next_clean and len(next_clean) >= 2
                    and next_clean.lower() not in _INTEL_KEYWORDS
                    and resolve_name(next_clean) is None):
                candidates.append(f"{clean} {next_clean}")
                skip_next = 1
                continue
        candidates.append(clean)
    return candidates
