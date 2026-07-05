"""Tail EVE combat Gamelogs (UTF-8) and emit incoming-damage events.

Parallel of chat_monitor.py, NOT a reuse: Gamelogs are UTF-8 (chat is UTF-16LE),
so the byte-alignment (`& ~1`), half-byte handling, and `*2` position math of
chat_monitor are DELIBERATELY absent here — UTF-8 positions advance by the raw
byte length of the consumed text. Everything else (rotation/truncation via the
tail-fingerprint, partial-line buffering, per-file position persistence, glob
discovery, seed-to-EOF, 1 s poll) mirrors chat_monitor.py.

Gamelogs live in `<Documents>/EVE/logs/Gamelogs` — the sibling of Chatlogs;
resolution reuses eve_paths' redirection-aware Documents root (see
eve_paths.gamelogs_dir_for). New Gamelog files appear on every jump/warp/session
change, so the discovery loop tolerates constant rotation exactly as
chat_monitor._discover_files does.

Scope note (SCAN EVERY PREVIEW CHARACTER, cheaply): the monitor globs the whole
Gamelogs dir and maps each file to its character via the `Listener:` header, so
all characters with a live preview tile can be covered. Files whose Listener does
not resolve to a tracked character are header-read once then skipped until the
tracked set changes (set_tracked_characters). Incremental byte-offset tailing +
the cheap "(combat)" prefix reject keep N-client scanning sub-millisecond.

ENGLISH CLIENT ONLY: the `(combat) … from` keyword match assumes the English
localization. Localized clients use translated keywords and are out of scope
for v1 (documented in the settings fine print).
"""
from __future__ import annotations

import glob
import json
import os
import re
import threading
import time
from dataclasses import dataclass

from app_io import atomic_write_json
from app_log import get_logger
from app_path import app_dir  # same import chat_monitor uses (chat_monitor.py:56)

# Reuse chat_monitor's verified header pattern verbatim (identical log header).
from chat_monitor import HEADER_LISTENER_PATTERN

log = get_logger(__name__)

STATE_FILE_PATH = os.path.join(app_dir(), "gamelog_monitor_state.json")

# Gamelog header line 1 is the literal word "Gamelog" (Chatlogs say "Channel ...").
_GAMELOG_HEADER_TOKEN = "Gamelog"

# PELD-parity timestamp + incoming-damage regexes (English client).
_GAMELOG_TS = r"\[\s*(?P<ts>\d{4}\.\d{2}\.\d{2}\s+\d{2}:\d{2}:\d{2})\s*\]"
DAMAGE_IN_RE = re.compile(
    _GAMELOG_TS
    + r"\s*\(combat\)\s*<[^>]*><b>(?P<dmg>\d+)</b>.*?>\s*from\b.*?"
      r"<b>(?:<[^>]*>)*(?P<attacker>[^<]+)</b>",
    re.IGNORECASE,
)
# NOTE (2026 fix): the attacker clause allows zero-or-more nested tags between
# the opening <b> and the name — the 2026 client wraps the attacker name in a
# nested <color=..> tag INSIDE the <b>…</b>, e.g.
#   … from</font> <b><color=0xffffffff>Corpum Dark Priest</b> …
# The pre-fix `<b>([^<]+)</b>` could not span that nested tag, so it matched 0%
# of real incoming-combat lines and the damage flash was silently suppressed.
# Simpler PELD fallback if a client variant slips past the full pattern; still
# gated on `from` (never `to`) so outgoing damage is never matched.
DAMAGE_IN_FALLBACK_RE = re.compile(
    r"\(combat\)\s*<.*?><b>(?P<dmg>\d+).*?>\s*from\b",
    re.IGNORECASE,
)

# ── Decloak notify line (English client) ─────────────────────────────────────
# EVE writes a (notify) line to the character's OWN Gamelog the instant their
# cloak drops. TWO templates share one prefix — the cause object varies widely
# (stargates, Ansiblex, Keepstar/Fortizar/Astrahus/POS, ships, ESS "Invisible
# Cloud - Disallow Cloaking", a Mobile Observatory pulse, …):
#   … (notify) Your cloak deactivates due to proximity to a nearby Stargate (…).
#   … (notify) Your cloak deactivates due to a pulse from a Mobile Observatory …
# Capture whatever follows "deactivates due to " up to the trailing period, so
# BOTH templates match and the cause is reported for logging.
#
# NEAR-MISS lines that appear in real logs and must NOT match (they are not
# decloaks): "Your cloaking systems are unable to activate due to …",
# "You cannot cloak your ship as you are being targeted…", and "Your targeting
# attempt fails because your ship is cloaked." — none contain the exact
# "cloak deactivates due to" phrase this pattern anchors on.
DECLOAK_RE = re.compile(
    r"\(notify\)\s+Your cloak deactivates due to\s+(?P<cause>.+?)\.?\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DamageEvent:
    timestamp: str          # "YYYY.MM.DD HH:MM:SS" as written in the log
    character_name: str     # from the file's "Listener:" header
    amount: int
    attacker: str           # raw "Name[CORP](Ship)" blob, may be "" via fallback


@dataclass(frozen=True)
class DecloakEvent:
    timestamp: str          # "YYYY.MM.DD HH:MM:SS" as written in the log
    character_name: str     # from the file's "Listener:" header
    cause: str              # e.g. "proximity to a nearby Stargate (Caldari System)"


def parse_damage_line(line: str):
    """Return (dmg:int, attacker:str) for an INCOMING combat line, else None.

    Outgoing ('to'), mining, notify, and malformed lines all yield None.
    """
    if not line or "(combat)" not in line:
        return None
    m = DAMAGE_IN_RE.search(line)
    if m:
        try:
            return (int(m.group("dmg")), m.group("attacker").strip())
        except (TypeError, ValueError):
            return None
    m = DAMAGE_IN_FALLBACK_RE.search(line)
    if m:
        try:
            return (int(m.group("dmg")), "")
        except (TypeError, ValueError):
            return None
    return None


def parse_decloak_line(line: str):
    """Return the decloak CAUSE (str) for a "(notify) Your cloak deactivates …"
    line, else None.

    Matches BOTH the proximity template and the Mobile Observatory template
    (they share the "cloak deactivates due to" prefix); rejects the near-miss
    "unable to activate" / "cannot cloak" / "targeting attempt fails" notify
    lines. Short-circuits on the cheap "cloak deactivates" substring so the
    tailer only runs the regex on candidate lines."""
    if not line or "cloak deactivates" not in line:
        return None
    m = DECLOAK_RE.search(line)
    if m:
        return (m.group("cause") or "").strip()
    return None


def _ts_of(line: str) -> str:
    m = re.search(_GAMELOG_TS, line)
    return m.group("ts") if m else ""


class GamelogMonitor:
    """UTF-8 Gamelog tailer. on_event(DamageEvent) is called on the polling
    thread; the fc_gui wiring (Task B6) marshals to the Tk thread. An optional
    on_decloak(DecloakEvent) callback rides the SAME tailing pass — each complete
    line is checked for a decloak notify alongside the incoming-damage parse — so
    covering decloaks costs one extra (short-circuited) regex per line and no
    second file read.

    Structure mirrors chat_monitor.ChatMonitor: a discovery loop over the
    Gamelogs dir + per-file tailer with rotation/truncation detection. The
    ONLY substantive differences are UTF-8 decode + no byte-alignment on the
    read position.
    """

    def __init__(self, on_event, logs_dir=None, state_path=STATE_FILE_PATH,
                 poll_interval=1.0, on_decloak=None):
        self._on_event = on_event
        self._on_decloak = on_decloak        # optional DecloakEvent sink (parallel of on_event)
        self._logs_dir = logs_dir            # resolved lazily (see B6 wiring)
        self._state_path = state_path
        self._poll_interval = poll_interval
        self._positions: dict[str, int] = {}         # path -> byte offset
        self._fingerprints: dict[str, bytes] = {}    # path -> tail bytes
        self._buffers: dict[str, str] = {}           # path -> partial-line text
        self._listeners: dict[str, str] = {}         # path -> char name
        # Set of lowercased character names with a live preview tile; None means
        # "track everything" (before the tick has told us otherwise).
        self._tracked: set[str] | None = None
        # Daemon thread / stop-event fields (mirror ChatMonitor).
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._state_dirty = False
        self._last_state_flush = 0.0
        self._persisted_state: dict[str, dict] = self._load_state()

    # --- public interface --------------------------------------------------

    def set_tracked_characters(self, names) -> None:
        """Restrict active tailing to these character names (lowercased match).

        Called from the fc_gui tick when the set of live preview tiles changes.
        Pass None to track every discovered character.
        """
        if names is None:
            self._tracked = None
        else:
            self._tracked = {str(n).strip().lower() for n in names if str(n).strip()}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                log.exception("[preview] gamelog poll failed")
            self._stop.wait(self._poll_interval)

    # --- header (identical to chat_monitor: read first ~2KB, find Listener) ---

    def _read_listener(self, path) -> str:
        try:
            with open(path, "rb") as f:
                head = f.read(2048)
            text = head.decode("utf-8", errors="replace")   # UTF-8, not UTF-16LE
        except OSError:
            return ""
        if _GAMELOG_HEADER_TOKEN not in text:
            return ""                                        # not a Gamelog file
        for ln in text.splitlines():
            m = HEADER_LISTENER_PATTERN.search(ln)
            if m:
                return m.group(1).strip()
        return ""

    def _is_tracked(self, listener: str) -> bool:
        if self._tracked is None:
            return True
        return bool(listener) and listener.lower() in self._tracked

    # --- discovery ---------------------------------------------------------

    def _logs_directory(self):
        return self._logs_dir

    def discover_files(self):
        """Register any new Gamelog files (seed to EOF). Constant-rotation safe:
        new files appear every jump/warp/session change."""
        logs_dir = self._logs_directory()
        if not logs_dir:
            return
        try:
            paths = glob.glob(os.path.join(logs_dir, "*.txt"))
        except OSError:
            return
        for path in paths:
            if path not in self._positions:
                self.seed_file(path)

    def seed_file(self, path):
        """Register a file and set its position to EOF (existing lines not replayed)."""
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        # Resume from persisted state when the file is known and hasn't shrunk.
        key = self._state_key(path)
        prior = self._persisted_state.get(key)
        resume = size
        if prior:
            try:
                saved = int(prior.get("last_pos", 0))
            except (TypeError, ValueError):
                saved = 0
            if 0 <= saved <= size:
                resume = saved
        self._positions[path] = resume
        self._fingerprints[path] = b""
        self._buffers[path] = ""
        self._listeners[path] = self._read_listener(path)

    def poll_once(self):
        """One cycle: discover new files, then tail each tracked file."""
        self.discover_files()
        for path in list(self._positions):
            listener = self._listeners.get(path) or self._read_listener(path)
            self._listeners.setdefault(path, listener)
            if not self._is_tracked(listener):
                continue
            self.poll_file(path)
        self._maybe_flush_state()

    def poll_file(self, path):
        """One tailing pass over `path`. UTF-8, NO byte alignment.

        Mirrors chat_monitor._tail_file (rotation/truncation via fingerprint,
        partial-line buffer) EXCEPT:
          - read_start = self._positions.get(path, 0)   # NOT `& ~1`
          - decode raw bytes as UTF-8
          - new position = read_start + len(consumed raw bytes)
        For each COMPLETE line, parse_damage_line(); on a hit, emit a
        DamageEvent(timestamp=_ts_of(line), character_name=listener, ...). The
        SAME line is also run through parse_decloak_line() (short-circuited on a
        cheap substring); on a hit a DecloakEvent is emitted via on_decloak.
        """
        listener = self._listeners.get(path) or self._read_listener(path)
        self._listeners.setdefault(path, listener)
        read_start = self._positions.get(path, 0)
        fingerprint = self._fingerprints.get(path, b"")
        try:
            size = os.path.getsize(path)
        except OSError:
            return

        # Truncation/rotation guard (mirrors chat_monitor._check_rotation, minus
        # the `& ~1` alignment): a shrunk file OR a changed tail fingerprint means
        # the file was replaced/rewritten — reseed from 0.
        rotated = False
        if size < read_start:
            rotated = True
        if not rotated and fingerprint and read_start >= len(fingerprint) \
                and size >= read_start:
            try:
                with open(path, "rb") as f:
                    f.seek(read_start - len(fingerprint))
                    tail_sample = f.read(len(fingerprint))
            except OSError:
                tail_sample = b""
            if tail_sample and tail_sample != fingerprint:
                rotated = True
        if rotated:
            read_start = 0
            self._buffers[path] = ""
            self._fingerprints[path] = b""
            fingerprint = b""

        if size <= read_start:
            self._positions[path] = read_start
            return

        try:
            with open(path, "rb") as f:
                f.seek(read_start)
                raw = f.read(size - read_start)
        except OSError:
            return
        if not raw:
            return

        text = self._buffers.get(path, "") + raw.decode("utf-8", errors="replace")
        lines = text.split("\n")
        self._buffers[path] = lines.pop()            # trailing partial line
        for ln in lines:
            hit = parse_damage_line(ln)
            if hit is not None:
                dmg, attacker = hit
                self._on_event(DamageEvent(timestamp=_ts_of(ln),
                                           character_name=listener,
                                           amount=dmg, attacker=attacker))
            # Decloak notify (same pass; short-circuited inside parse_decloak_line
            # so it's ~free on the overwhelming majority of lines).
            if self._on_decloak is not None:
                cause = parse_decloak_line(ln)
                if cause is not None:
                    self._on_decloak(DecloakEvent(timestamp=_ts_of(ln),
                                                  character_name=listener,
                                                  cause=cause))

        new_pos = read_start + len(raw)              # raw byte length; no *2
        self._positions[path] = new_pos
        # Record a tail fingerprint of what we just consumed (up to 64 bytes) so
        # a truncate-then-rewrite with unchanged size is still caught next poll.
        fp_len = min(64, len(raw))
        if fp_len > 0:
            self._fingerprints[path] = raw[-fp_len:]
        self._persisted_state[self._state_key(path)] = {
            "last_pos": new_pos,
            "last_updated": time.time(),
        }
        self._state_dirty = True

    # --- state persistence (mirrors chat_monitor) --------------------------

    @staticmethod
    def _state_key(filepath: str) -> str:
        return os.path.abspath(filepath)

    def _load_state(self) -> dict[str, dict]:
        try:
            with open(self._state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
        return {}

    def _maybe_flush_state(self) -> None:
        if not self._state_dirty:
            return
        now = time.time()
        if (now - self._last_state_flush) <= 30.0:
            return
        self._save_state()
        self._last_state_flush = now
        self._state_dirty = False

    def _save_state(self) -> None:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self._state_path)) or ".",
                        exist_ok=True)
        except OSError:
            pass
        try:
            atomic_write_json(self._state_path, self._persisted_state,
                              indent=None, ensure_ascii=True)
        except Exception:
            log.exception("[preview] failed to persist gamelog monitor state to %s",
                          self._state_path)
