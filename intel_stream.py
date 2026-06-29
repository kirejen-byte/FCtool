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
