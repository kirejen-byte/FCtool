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


def annotate(text: str) -> list[Span]:
    """Detect important spans in a raw intel line. Pure; deterministic;
    non-overlapping (longest match wins on conflict). Total: never raises.
    'name' spans are NOT produced here."""
    if not text:
        return []
    spans = _system_spans(text)
    spans.sort(key=lambda s: s.start)
    return spans
