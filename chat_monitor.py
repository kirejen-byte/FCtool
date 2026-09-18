"""
EVE Online Chat Log Monitor
Tails EVE chat log files in real-time, parsing UTF-16LE encoded messages.

Phase 2a rewrite:
    * binary-mode reads with 2-byte alignment (UTF-16-LE is 2 bytes / code unit)
    * rotation / truncation detection via os.stat (size + inode)
    * partial-line buffering (holds incomplete trailing line until newline arrives)
    * per-file position persistence across restarts in chat_monitor_state.json
    * short-lived dedupe set keyed on (channel, timestamp, sender, hash(message))
"""

import glob
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from app_io import atomic_write_json
from app_log import get_logger

log = get_logger(__name__)


@dataclass
class ChatMessage:
    timestamp: datetime
    sender: str
    message: str
    channel: str
    raw_line: str


# Header format:
#   Channel ID:      fleet_1213112261803
#   Channel Name:    Fleet
#   Listener:        Securitas Protector
#   Session started: 2026.03.25 20:38:13
#
# Message format (each line starts with BOM \ufeff):
#   \ufeff[ 2026.03.25 20:38:22 ] Cylic Mithuza > you gotta collect them

MESSAGE_PATTERN = re.compile(
    r"^\ufeff?\[\s*(\d{4}\.\d{2}\.\d{2}\s+\d{2}:\d{2}:\d{2})\s*\]\s+(.+?)\s+>\s+(.*)"
)

HEADER_CHANNEL_PATTERN = re.compile(r"Channel Name:\s+(.+)")
HEADER_LISTENER_PATTERN = re.compile(r"Listener:\s+(.+)")


# Sidecar state file, kept in the app's writable data dir (see app_path.app_dir).
from app_path import app_dir

STATE_FILE_PATH = os.path.join(app_dir(), "chat_monitor_state.json")

# The INTEL monitor is a second, independent ChatMonitor (listener_filter=None,
# many channel prefixes) over the same directory, and it MUST NOT share the
# fleet monitor's sidecar: each instance loads the file once at construction and
# rewrites it whole from its own _persisted_state, so two monitors on one path
# overwrite each other's positions on every flush — including the shutdown
# flush, where fc_gui persists the chat positions and the intel stop immediately
# replaces the file with the intel ones. Separate files, no shared writer.
INTEL_STATE_FILE_PATH = os.path.join(app_dir(), "chat_monitor_state_intel.json")

# Dedupe TTL - drop duplicate messages (same channel/ts/sender/body) seen within this window.
DEDUPE_TTL_SECONDS = 60.0

# ── Tiered polling (2026-09-17 uptime-performance work) ─────────────────────
#
# A lived-in Chatlogs folder accumulates one file per channel per session
# forever: the owner's fleet monitor tracked 2,606 `Fleet_*` files and os.stat'ed
# every one of them once a second (0.5-1.2 s per pass at 462 us/stat), while
# 2,419 of them had not been written to in 180 days and 18 in the last week.
# Tracking a file therefore no longer means polling it every second:
#
#   FAST tier - activity within ACTIVE_WINDOW_S, plus the PINNED newest file of
#     every group. Polled at the caller's cadence (1 s), exactly as before, so
#     message latency for a live log is unchanged. The newest file of a group is
#     ALWAYS fast however idle it looks (owner's decision): FCTool is routinely
#     open before EVE, and that file is where the next line will land.
#   SLOW tier - everything else. Read once every IDLE_POLL_INTERVAL_S and NOT
#     stat'ed in between; a read that finds new bytes promotes it back to fast.
#
# Files idle past DISCOVERY_MAX_IDLE_S are not tracked at all unless they are
# their group's newest, and a file the filters reject is remembered in
# ChatMonitor._ignored so a rescan never re-reads its header.
ACTIVE_WINDOW_S = 600.0
IDLE_POLL_INTERVAL_S = 30.0
DISCOVERY_MAX_IDLE_S = 7 * 86400
# Interval between state-file writes. The 37 KB sidecar used to be rewritten
# (atomically, so fsync'd) on every pass that saw a chat line; positions are
# only a replay optimisation, so an interval plus an explicit flush at shutdown
# (ChatMonitor.flush_state) loses nothing that matters.
STATE_FLUSH_INTERVAL_S = 30.0
# How many times a path whose header could not be OPENED is re-probed before it
# falls back to the ordinary rescan cadence. Bounded so a permanently
# unreadable file cannot turn the retry into a per-poll cost; it is still never
# added to ChatMonitor._ignored, so a later real rescan picks it up again.
_HEADER_RETRY_ATTEMPTS = 3

# OneDrive (and any Windows cloud-sync provider) leaves "cloud-only" files in
# the folder: a directory entry with no local body. `os.stat` answers fine and
# reports a recall attribute, but EVERY `open()` on one raises OSError — a
# failed hydration request that also pokes OneDrive.exe. Measured on the
# owner's OneDrive-hosted Chatlogs dir 2026-09-17: 1,098 of 4,784 `Fleet*.txt`
# were cloud-only (observed st_file_attributes 0x400020), and probing their
# headers spent ~4 s of the poll thread per rescan pass — each one re-probed
# _HEADER_RETRY_ATTEMPTS times as a "transient" unreadable header. A cloud-only
# file cannot be a log EVE is currently writing (EVE writes locally, and a
# written file is hydrated), so discovery recognises them from the stat it
# already takes and skips them WITHOUT opening: no header read, no retry
# budget. A non-Windows stat_result has no `st_file_attributes`, so the
# getattr default of 0 leaves every POSIX candidate unaffected.
_CLOUD_PLACEHOLDER_ATTRS = (
    0x00400000        # FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS: dehydrated body,
                      #   reading it triggers a network recall
    | 0x00040000      # FILE_ATTRIBUTE_RECALL_ON_OPEN: even opening it recalls
    | 0x00001000      # FILE_ATTRIBUTE_OFFLINE: body not immediately available
)


class ChatLogFile:
    """Tracks a single chat log file, remembering byte-offset read position."""

    def __init__(self, filepath: str):
        self.filepath = filepath
        self.channel_name = ""
        self.listener = ""
        # Tiering state (see the module-level note). ``last_mtime`` is the wall
        # clock mtime of the last read that found NEW BYTES (seeded from the
        # discovery stat), never a bare "somebody touched the file" stamp — an
        # AV scanner or a cloud-sync agent bumps mtime on logs EVE stopped
        # writing months ago. ``next_poll_at`` is a time.monotonic() deadline.
        self.last_mtime: float = 0.0
        self.next_poll_at: float = 0.0
        # True for the newest file of its group: always polled at the fast
        # cadence. Recomputed by ChatMonitor after every discovery pass.
        self.pinned: bool = False
        # False = this file may never HOLD a pin (and never counts as its
        # group's newest). Set for a header-LESS file (no Listener line), whose
        # group key is (channel, "") — a group nothing with a real listener can
        # ever join — so a pin there would make it immortally fast and immune
        # to the DISCOVERY_MAX_IDLE_S cutoff. On the owner's box that was a
        # 2014 log. Two cases, both in _discover_files: always under a listener
        # filter (the group is a group of one by construction), and without one
        # only once the file is idle past the cutoff. It is still TRACKED when
        # recent enough; it just competes for nothing.
        self.pinnable: bool = True
        # Channel + listener identity used for pin selection; set by
        # ChatMonitor at track time ((channel, listener) - see _group_key).
        self.group_key: tuple[str, str] = ("", "")
        # "Which session is this" ordering within the group, computed ONCE at
        # track time (ChatMonitor._pin_rank). Cached deliberately: it is a pure
        # function of the filename for every real EVE log, and for the rare
        # stamp-less name it freezes the mtime we saw at discovery rather than
        # letting a later AV/cloud touch reorder the group.
        self.pin_rank: tuple = ()
        # Byte offset into the file (not character offset). We always read in "rb".
        self._last_pos: int = 0
        # Trailing partial text we decoded but haven't emitted because no newline was
        # seen yet. On the next poll we re-seek to the start of these bytes and decode
        # again with the newly-arrived bytes appended.
        self._partial: str = ""
        self._header_parsed = False
        # Last observed stat info, used for rotation detection.
        self._last_ino: int = 0
        # Rotation fingerprint. Stored fingerprint = raw bytes of the tail of what
        # we last consumed (the N bytes ending at _last_pos). If a rewrite
        # happens, those bytes won't match on next poll even if size/inode do.
        self._tail_fingerprint: bytes = b""

    # -- header parsing -------------------------------------------------

    def _parse_header(self, lines: list[str]):
        for line in lines:
            m = HEADER_CHANNEL_PATTERN.search(line)
            if m:
                self.channel_name = m.group(1).strip()
            m = HEADER_LISTENER_PATTERN.search(line)
            if m:
                self.listener = m.group(1).strip()
        self._header_parsed = True

    # -- tailing --------------------------------------------------------

    def _check_rotation(self, st_size: int, st_ino: int, tail_sample: bytes) -> bool:
        """Return True and reset state if the file was truncated or replaced.

        Detection signals (any one triggers a reset):
          * current size is smaller than our last known position (classic truncate)
          * inode changed (NTFS file reference / POSIX inode)
          * tail fingerprint mismatch - the bytes immediately preceding our last
            read position are no longer what we consumed (catches truncate-then-
            rewrite even when size/inode both look unchanged)
        """
        rotated = False
        if st_size < self._last_pos:
            rotated = True
        # st_ino == 0 on some filesystems (e.g., FAT); require both to be known.
        if self._last_ino and st_ino and st_ino != self._last_ino:
            rotated = True
        if (self._tail_fingerprint and tail_sample
                and tail_sample != self._tail_fingerprint):
            rotated = True
        if rotated:
            self._last_pos = 0
            self._partial = ""
            self._header_parsed = False
            self._tail_fingerprint = b""
        return rotated

    def read_new_lines(self) -> list[ChatMessage]:
        """Read any new lines appended since last check.

        Reads the file in binary mode starting at ``self._last_pos`` aligned down
        to an even byte boundary (UTF-16-LE code units are 2 bytes), decodes the
        remaining bytes, and emits complete lines. Any trailing partial line (no
        newline yet) is NOT emitted and ``self._last_pos`` is advanced only to the
        even byte offset where that partial begins. Next poll re-reads those same
        bytes plus any newly-arrived ones and tries again.
        """
        messages: list[ChatMessage] = []
        try:
            st = os.stat(self.filepath)
            st_size = st.st_size
            st_ino = getattr(st, "st_ino", 0) or 0
            st_mtime = getattr(st, "st_mtime", 0.0) or 0.0

            # Sample tail bytes immediately before our last read position. If the
            # file was rewritten, these bytes will differ from what we recorded.
            tail_sample = b""
            fingerprint_len = len(self._tail_fingerprint)
            if fingerprint_len and self._last_pos >= fingerprint_len and st_size >= self._last_pos:
                try:
                    with open(self.filepath, "rb") as f:
                        f.seek(self._last_pos - fingerprint_len)
                        tail_sample = f.read(fingerprint_len)
                except OSError:
                    tail_sample = b""

            self._check_rotation(st_size, st_ino, tail_sample)

            # Remember stat info for next poll's rotation check.
            self._last_ino = st_ino

            # Align read start down to an even byte - UTF-16-LE code units are 2 bytes.
            read_start = self._last_pos & ~1
            if st_size <= read_start:
                return messages

            with open(self.filepath, "rb") as f:
                f.seek(read_start)
                raw = f.read()

            if not raw:
                return messages

            # Drop any trailing half-byte so we only decode whole code units. The
            # trailing byte will still be on disk and re-read next poll when its
            # partner arrives.
            if len(raw) % 2 == 1:
                raw = raw[:-1]

            if not raw:
                # Only a half byte available - nothing to do.
                return messages

            # New bytes arrived: this file is live, so remember WHEN. The stat
            # we already performed supplies the timestamp (never a second stat -
            # the whole point of the tiering is to stop stat'ing quiet files),
            # but it is FLOORED AT NOW: we have just witnessed bytes arriving,
            # which is a stronger statement than anything the recorded mtime can
            # make. A clock-skewed source (OneDrive and other cloud-sync agents
            # write back timestamps from another machine) can hand back an mtime
            # minutes in the past, and a file whose mtime lands more than
            # ACTIVE_WINDOW_S behind would fall straight back to the slow tier
            # after every single read while it is actively being written.
            self.last_mtime = max(st_mtime, time.time())

            text = raw.decode("utf-16-le", errors="replace")

            # Split on '\n'. If the decoded text does NOT end in a newline the
            # final split element is an incomplete line we must buffer.
            ends_with_newline = text.endswith("\n")
            parts = text.split("\n")

            if ends_with_newline:
                complete_lines = parts[:-1]  # final element is "" after trailing \n
                partial_code_units = 0
            else:
                complete_lines = parts[:-1]
                partial_code_units = len(parts[-1])

            # Advance _last_pos to the byte offset where the partial begins.
            # Each decoded code unit = 2 bytes. Anything unconsumed (partial text +
            # an unpaired trailing byte, if any) stays on disk for re-read.
            consumed_code_units = len(text) - partial_code_units
            self._last_pos = read_start + consumed_code_units * 2
            # Track partial purely for observability / tests - it's re-read from
            # disk next poll, not prepended.
            self._partial = parts[-1] if not ends_with_newline else ""

            # Record tail fingerprint (up to 64 bytes) of what we just consumed.
            # Used next poll to detect truncate-then-rewrite where size and inode
            # may be unchanged.
            fp_len = min(64, self._last_pos - read_start, self._last_pos)
            if fp_len > 0:
                consumed_raw = raw[:consumed_code_units * 2]
                if len(consumed_raw) >= fp_len:
                    self._tail_fingerprint = consumed_raw[-fp_len:]

            if not self._header_parsed:
                # Header lines are in the first chunk; parse from complete_lines.
                self._parse_header(complete_lines)

            for line in complete_lines:
                # Strip CR (from CRLF) and any stray whitespace.
                line = line.rstrip("\r").strip()
                if not line:
                    continue
                m = MESSAGE_PATTERN.match(line)
                if not m:
                    continue
                try:
                    ts = datetime.strptime(m.group(1), "%Y.%m.%d %H:%M:%S")
                except ValueError:
                    ts = datetime.now()
                messages.append(ChatMessage(
                    timestamp=ts,
                    sender=m.group(2).strip(),
                    message=m.group(3).strip(),
                    channel=self.channel_name,
                    raw_line=line,
                ))
        except (OSError, IOError):
            # File may be transiently locked by EVE (retry next poll), but a
            # permanently unreadable chat file would otherwise be invisible.
            log.warning("read_new_lines failed for %s", self.filepath, exc_info=True)
        return messages


# ── Current-session backfill ────────────────────────────────────────────────
#
# The live tail deliberately seeds an unknown file at EOF (see
# ChatMonitor._discover_files) so startup never replays a day of history. That
# leaves a gap for consumers whose state is built from chat CONTENT rather than
# events — the command-burst charge tracker being the case in point: charges
# linked before FCTool started are simply invisible. These helpers read the
# CURRENT session's log once, from byte 0. They are one-shot startup helpers,
# never poll-path calls — the no-glob-per-poll rule stands for anything that
# runs on a timer.

# EVE names a chat log "<Channel>_<YYYYMMDD>_<HHMMSS>_<characterID>.txt"; older
# logs predate the characterID suffix, so only the stamp itself is required.
SESSION_STAMP_PATTERN = re.compile(r"_(\d{8})_(\d{6})")


def _session_sort_key(filepath: str) -> tuple:
    """Ordering key for "which of these logs is the current session".

    The filename stamp is authoritative and outranks mtime outright, because
    mtime records whatever last TOUCHED the file — AV scanners, backup and
    cloud-sync agents all bump an old log long after EVE stopped writing it.
    mtime therefore only orders files whose name carries no usable stamp
    (tier 0); a stamped file always beats an unstamped one and the two scales
    are never compared against each other. The path is the final tiebreak, so
    two files claiming the same second still resolve deterministically.
    """
    m = SESSION_STAMP_PATTERN.search(os.path.basename(filepath))
    if m:
        try:
            stamp = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
            return (1, stamp, filepath)
        except ValueError:
            pass  # stamp-SHAPED but not a real date - fall through to mtime
    try:
        mtime = os.path.getmtime(filepath)
    except OSError:
        mtime = 0.0
    return (0, mtime, filepath)


def _header_listener(filepath: str) -> str | None:
    """Listener (character) name from a log's header — three distinct answers.

    ``None`` means the header could not be READ at all (the transient
    open-failure class this box demonstrably has). ``""`` means it was read
    fine and simply carries no Listener line. A name means it was read and that
    is who was listening. The caller MUST keep the first two apart: an
    unreadable newest candidate is a reason to stop, while a listener-less one
    is kept (the ``_discover_files`` mirror — the filter can only exclude what
    it can positively read).
    """
    try:
        with open(filepath, "rb") as f:
            header_bytes = f.read(4096)
    except OSError:
        return None
    header = header_bytes.decode("utf-16-le", errors="replace")
    for line in header.split("\n"):
        m = HEADER_LISTENER_PATTERN.search(line)
        if m:
            return m.group(1).strip()
    return ""


def find_current_session_file(logs_path: str, channel_prefix: str,
                              listener: str | None = None) -> str | None:
    """Newest session log for a channel, or None when nothing matches.

    Channel matching is a case-insensitive basename prefix (the
    ``_discover_files`` rule, so a case-sensitive filesystem behaves like
    Windows). A listener filter keeps only files whose header Listener is that
    character; a file whose header carries no Listener at all is KEPT, exactly
    as ``_discover_files`` does — the filter can only exclude what it can
    positively read.

    Candidates are walked NEWEST-FIRST and the walk stops at the first keeper,
    so the ordinary case opens exactly one header. That matters: a lived-in
    Chatlogs folder holds thousands of files for a single channel prefix (4,627
    ``Fleet*`` in the folder this was measured in — ~2.9 s of header opens if
    every one is probed), and this runs on the fleet-poll thread.

    Raises OSError when the newest still-eligible candidate's header cannot be
    read. Walking past it would hand back a PRIOR fleet's log — the one
    contamination this feature must never produce — and taking it blind would
    defeat the multi-boxer filter, so neither is an option; failing lets the
    caller's attempt budget retry, which costs nothing.
    """
    if not logs_path or not str(logs_path).strip():
        return None      # unconfigured path: never let os.path.join glob the CWD
    try:
        candidates = glob.glob(os.path.join(logs_path, "*.txt"))
    except OSError:
        return None

    prefix = (channel_prefix or "").lower()
    if prefix:
        candidates = [fp for fp in candidates
                      if os.path.basename(fp).lower().startswith(prefix)]
    if not candidates:
        return None

    candidates.sort(key=_session_sort_key, reverse=True)
    if not listener:
        return candidates[0]

    wanted = listener.strip().lower()
    for fp in candidates:
        found = _header_listener(fp)
        if found is None:
            raise OSError(f"newest candidate header unreadable: {fp}")
        if not found or found.lower() == wanted:
            return fp
    return None


def read_full_session(filepath: str) -> list[ChatMessage]:
    """Parse a chat log from byte 0 and return every message it holds.

    One ChatLogFile at position 0, read once: header lines do not match
    MESSAGE_PATTERN so they drop out on their own (while still being parsed for
    the channel name).

    Raises OSError when the read consumed NOTHING from a non-empty file.
    ``read_new_lines`` contains its own OSError and returns [], which is
    otherwise indistinguishable from a genuinely message-less session — and
    that ambiguity is what let a transient open failure look like success. A
    header-only file advances the position past its header, so a session where
    nobody has spoken yet still returns [] rather than raising. (``getsize``
    raising is the same failure: the log vanished between glob and read.)
    """
    log_file = ChatLogFile(filepath)
    log_file._last_pos = 0
    messages = log_file.read_new_lines()
    if not messages and log_file._last_pos == 0 and os.path.getsize(filepath) > 0:
        raise OSError(f"read consumed nothing from {filepath}")
    return messages


def backfill_current_session(logs_path: str, channel_prefix: str,
                             listener: str | None,
                             sink: Callable[[ChatMessage], None]) -> int | None:
    """Feed every message of the current session's log to ``sink``; return count.

    Only the NEWEST session file is read, so a prior session's log is never a
    source — the caller adds the second half of that guarantee by asking only
    while the session it wants is demonstrably the current one.

    IO failures return None so the caller can retry; 0 means a readable,
    message-less session and is a final answer. Those two must not be conflated:
    EVE creates the log at JOIN, so an FC who starts FCTool before anyone speaks
    meets an empty-but-healthy session as a matter of course, while a briefly
    unopenable log is exactly the transient a retry cures. An exception raised
    by ``sink`` is deliberately NOT contained — that is the caller's own bug,
    and swallowing it would disguise a broken consumer as "nothing to backfill".
    """
    try:
        filepath = find_current_session_file(logs_path, channel_prefix, listener)
        if not filepath:
            return 0
        messages = read_full_session(filepath)
    except OSError:
        log.warning("backfill_current_session failed for %s", logs_path,
                    exc_info=True)
        return None
    for msg in messages:
        sink(msg)
    return len(messages)


class ChatMonitor:
    """
    Monitors the EVE chat logs directory for new messages.
    Polls for file changes and dispatches messages to registered callbacks.
    """

    def __init__(self, logs_path: str, poll_interval: float = 1.0,
                 channel_filter: str | None = None,
                 listener_filter: str | None = None,
                 channel_filters: list[str] | None = None,
                 state_path: str | None = None,
                 dedupe_ttl: float = DEDUPE_TTL_SECONDS):
        self.logs_path = logs_path
        self.poll_interval = poll_interval
        self.channel_filter = channel_filter
        self.channel_filters = channel_filters  # Multiple channel prefixes
        self.listener_filter = listener_filter  # Character name to track
        self._tracked_files: dict[str, ChatLogFile] = {}
        # New-file discovery gate (see _discover_files). Re-globbing the EVE
        # Chatlogs directory is O(files-in-dir) and that directory accumulates one
        # file per channel per session indefinitely (tens of thousands over time),
        # so an unconditional glob every poll holds the GIL long enough to stall the
        # Tk main thread. A new chat-log FILE bumps the directory's mtime (creation),
        # while message appends do not — so we glob only when the directory changed
        # since the last scan, with a long safety-net interval as a backstop.
        self._last_dir_mtime: float | None = None
        self._last_full_scan_monotonic: float = 0.0
        # Paths that matched the prefix glob but were REJECTED at discovery: a
        # different listener, or idle past DISCOVERY_MAX_IDLE_S with a newer
        # sibling holding their group. A rescan skips these without opening
        # them — re-reading every rejected file's 4 KB header is what made the
        # backstop re-glob cost 3-4 s on the owner's box. Bounded by the number
        # of prefix-matched files, not by the whole directory. A file whose
        # header could not be READ is deliberately NOT ignored: the filter can
        # only exclude what it positively read (the find_current_session_file
        # rule), so a transiently unopenable log is retried on the next scan.
        self._ignored: set[str] = set()
        # Paths whose 4 KB header could not be OPENED (the transient AV /
        # cloud-sync failure class this box demonstrably has) mapped to the
        # re-probe attempts they have left. These are NEVER ignored — the
        # listener filter can only exclude what it positively read — and they
        # are re-probed on the next poll WITHOUT a directory glob, because the
        # likeliest moment for a transient open failure is the instant EVE
        # creates the log we most need (fleet join), and the mtime gate would
        # otherwise make us wait out the 300 s backstop for it.
        self._header_retry: dict[str, int] = {}
        self._callbacks: list[Callable[[ChatMessage], None]] = []
        self._running = False

        # Persistence
        self._state_path = state_path or STATE_FILE_PATH
        self._persisted_state: dict[str, dict] = self._load_state()
        self._state_dirty = False
        self._last_state_flush = 0.0

        # Dedupe
        self._dedupe_ttl = float(dedupe_ttl)
        self._seen: dict[tuple, float] = {}

    # -- public interface ----------------------------------------------

    def on_message(self, callback: Callable[[ChatMessage], None]):
        """Register a callback for new chat messages."""
        self._callbacks.append(callback)

    def poll(self) -> list[ChatMessage]:
        """Public single-poll method for use by the main loop."""
        messages = self._poll_once()
        for msg in messages:
            for cb in self._callbacks:
                cb(msg)
        return messages

    def run(self):
        """Blocking poll loop. Use poll() instead for integration with async main loop."""
        self._running = True
        print(f"[ChatMonitor] Watching: {self.logs_path}")
        if self.channel_filter:
            print(f"[ChatMonitor] Filter: {self.channel_filter}*")
        print("[ChatMonitor] Waiting for new messages...")

        while self._running:
            self.poll()
            time.sleep(self.poll_interval)

    def stop(self):
        self._running = False
        # The per-pass flush is throttled (STATE_FLUSH_INTERVAL_S), so the
        # positions of the last interval only survive a teardown because of
        # this. stop() is inert for poll()-driven use otherwise, which is why
        # fc_gui also calls flush_state() where it drops a monitor without
        # stopping it (a tracked-character change, a Settings->Save rebuild).
        self.flush_state()

    def flush_state(self) -> None:
        """Persist the tail positions NOW if anything is pending (no throttle).

        The explicit "we are going away" seam: called by :meth:`stop` and by
        fc_gui at shutdown and wherever a live monitor is replaced. Safe to
        call from another thread than the poller — ``_save_state`` snapshots
        the dict before serialising it.
        """
        if not self._state_dirty:
            return
        self._save_state()
        self._last_state_flush = time.time()
        self._state_dirty = False

    def tier_of(self, filepath: str) -> str:
        """``"fast"`` or ``"slow"`` for a tracked path (observability seam).

        Fast = pinned (its group's newest) or new bytes seen within
        ACTIVE_WINDOW_S. An untracked or ignored path reads as ``"slow"``:
        nothing polls it at all.
        """
        log_file = self._tracked_files.get(filepath)
        if log_file is None:
            return "slow"
        return "fast" if self._is_fast(log_file, time.time()) else "slow"

    @staticmethod
    def _is_fast(log_file: "ChatLogFile", now_wall: float) -> bool:
        """Tier decision. Uses the CACHED mtime by design — asking the
        filesystem here would reintroduce the per-file per-pass stat that the
        tiering exists to remove."""
        return (log_file.pinned
                or (now_wall - log_file.last_mtime) < ACTIVE_WINDOW_S)

    def get_available_listeners(self, max_age_days: int = 7) -> list[str]:
        """Scan log files to find all character names (listeners) with fleet channels.
        Only checks files modified within max_age_days to avoid scanning years of history."""
        listeners = set()
        filter_prefix = (self.channel_filter or "").lower()
        cutoff = time.time() - (max_age_days * 86400)

        # Use targeted glob if we have a channel filter, otherwise scan all
        if filter_prefix:
            pattern = os.path.join(self.logs_path, f"{self.channel_filter}*.txt")
        else:
            pattern = os.path.join(self.logs_path, "*.txt")

        for filepath in glob.glob(pattern):
            # Skip files older than cutoff
            try:
                if os.path.getmtime(filepath) < cutoff:
                    continue
            except OSError:
                continue
            basename = os.path.basename(filepath)
            if filter_prefix and not basename.lower().startswith(filter_prefix):
                continue
            try:
                with open(filepath, "rb") as f:
                    header_bytes = f.read(4096)
                header = header_bytes.decode("utf-16-le", errors="replace")
                for line in header.split("\n"):
                    m = HEADER_LISTENER_PATTERN.search(line)
                    if m:
                        listeners.add(m.group(1).strip())
            except OSError:
                pass
        return sorted(listeners)

    # -- internals -----------------------------------------------------

    # Backstop: force a full directory re-glob at least this often even when the
    # directory mtime looks unchanged, so a new file is still discovered on any
    # exotic filesystem whose directory mtime does not advance on file creation
    # (or when two events land within one mtime tick). The interval is 300 s
    # rather than the original 60 s because the mtime gate already catches every
    # real creation: this only has to cover a filesystem that does not move the
    # directory mtime at all, and each firing walks the whole Chatlogs folder
    # (3-4 s on the owner's 54,667-file directory before the ignore set).
    _DIR_RESCAN_INTERVAL_SECONDS = 300.0

    def _group_key(self, filepath: str, listener: str | None) -> tuple[str, str]:
        """(real channel name, listener) — the pin selection scope.

        One group per channel per character, so a multi-boxer's newest copy of
        each tracked channel is pinned independently (the intel monitor runs
        with ``listener_filter=None`` and takes the listener from the header).
        Both halves are lowercased because every filter in this module matches
        case-insensitively.

        The channel half is the REAL channel name carried by the basename — the
        text before the session stamp — NOT the configured filter that matched
        it. A filter is a PREFIX, so a file belonging to a DIFFERENT channel
        whose name merely extends one (``channel_filters=["Delve"]`` and a file
        ``Delve Intel_20260917_120000_1.txt``) would otherwise join the tracked
        channel's group; with the newer stamp it then takes the pin and the
        tracked ``Delve_…`` log drops to the 30 s tier — the exact failure the
        pin exists to prevent. The configured prefix survives only as the
        FALLBACK, for a name that carries no stamp at all (longest match, since
        ``channel_filters`` is a user-ordered list and overlapping entries are
        ordinary).

        Trade-off, deliberately accepted: keying on the real name yields MORE
        groups, and therefore more pinned (always-fast) files, than keying on
        the filter did. But each of those pins is then a genuine channel's
        newest log rather than whichever prefix-sharing stranger sorted last,
        which is what the owner's 1 s guarantee is actually about.
        """
        basename = os.path.basename(filepath)
        stamp = SESSION_STAMP_PATTERN.search(basename)
        if stamp:
            channel = basename[:stamp.start()].rstrip("_").strip().lower()
            if channel:
                return (channel, (listener or "").strip().lower())
        lowered_base = basename.lower()
        prefix = ""
        candidates = list(self.channel_filters or [])
        if self.channel_filter:
            candidates.append(self.channel_filter)
        for candidate in candidates:
            lowered = (candidate or "").lower()
            if (lowered and lowered_base.startswith(lowered)
                    and len(lowered) > len(prefix)):
                prefix = lowered
        return (prefix, (listener or "").strip().lower())

    @staticmethod
    def _pin_rank(filepath: str) -> tuple:
        """Ordering for "newest in the group" — the FILENAME STAMP, not mtime.

        Delegates to :func:`_session_sort_key`, whose docstring carries the
        reason: mtime records whatever last TOUCHED a log, and AV scanners,
        backup and cloud-sync agents all bump files EVE stopped writing months
        ago. Ranking by mtime therefore handed the pin to whichever ancient log
        was touched most recently — measured on the owner's box, a 2014 file
        took the pin and the real current-session log dropped to the slow tier,
        which is the exact failure the pin exists to prevent.

        A consequence worth stating: the rank does not move when a file receives
        BYTES, so a sibling that starts talking can never steal the pin from a
        file with a newer filename stamp (it is promoted to the fast tier on its
        own merits by ``last_mtime``, which is all activity should buy).
        """
        return _session_sort_key(filepath)

    def _repin_groups(self) -> None:
        """Pin the newest tracked file of every group, unpin the rest.

        A newly created file that becomes its group's newest takes the pin over
        and the previous holder drops back to the ordinary tier rules. A file
        flagged ``pinnable = False`` never competes (see ChatLogFile).
        """
        best: dict[tuple[str, str], tuple] = {}
        for filepath, log_file in self._tracked_files.items():
            if not log_file.pinnable:
                continue
            rank = log_file.pin_rank
            current = best.get(log_file.group_key)
            if current is None or rank > current[0]:
                best[log_file.group_key] = (rank, filepath)
        winners = {filepath for _rank, filepath in best.values()}
        for filepath, log_file in self._tracked_files.items():
            log_file.pinned = filepath in winners

    def _discover_files(self):
        """Find chat log files matching the channel and listener filters.

        Skips the (potentially very expensive) directory glob when the logs
        directory is unchanged since the last scan: a new EVE chat-log file bumps
        the directory's mtime, whereas appends to already-tracked files do not, so
        a stable mtime means there is no new file to discover. A long backstop
        interval still forces an occasional full re-glob as a safety net. This
        keeps the common idle poll off the GIL-heavy glob that otherwise stalls the
        Tk main thread when the Chatlogs folder holds tens of thousands of files.

        One exception to the gate: when a previous scan could not OPEN a
        candidate's header, this runs a glob-free "retry only" pass over exactly
        those paths, so a transient open failure on a just-created log costs one
        poll instead of a whole backstop interval."""
        now_monotonic = time.monotonic()
        try:
            dir_mtime = os.path.getmtime(self.logs_path)
        except OSError:
            dir_mtime = None
        unchanged = (dir_mtime is not None
                     and dir_mtime == self._last_dir_mtime)
        within_backstop = (
            (now_monotonic - self._last_full_scan_monotonic)
            < self._DIR_RESCAN_INTERVAL_SECONDS)
        retry_only = False
        if unchanged and within_backstop:
            if not self._header_retry:
                return  # directory unchanged since last scan — nothing to find
            # No new files, but a header we could not open last time is still
            # owed a retry. Re-probe exactly those paths through the normal
            # pipeline — no glob, so this costs nothing at directory scale.
            retry_only = True
        else:
            self._last_dir_mtime = dir_mtime
            self._last_full_scan_monotonic = now_monotonic

        if retry_only:
            all_files = sorted(self._header_retry)
        else:
            # If multiple channel filters are set, glob each one separately (much faster)
            # Glob once and match channel prefixes case-INSENSITIVELY so a
            # case-sensitive filesystem (Linux) behaves like Windows.
            all_txt = glob.glob(os.path.join(self.logs_path, "*.txt"))
            if self.channel_filters:
                prefixes = tuple(p.lower() for p in self.channel_filters)
                all_files = [
                    fp for fp in all_txt
                    if os.path.basename(fp).lower().startswith(prefixes)
                ]
            else:
                all_files = all_txt

        now_wall = time.time()

        # Pass 1: probe every NOT-yet-known candidate exactly once — ONE stat
        # (attributes/mtime/size/inode) and then its 4 KB header (for the
        # listener). Files already tracked or already in _ignored never reach
        # this. The stat comes FIRST so a cloud-only placeholder is recognised
        # and skipped before anything tries to open it.
        candidates: list[tuple] = []
        for filepath in all_files:
            if filepath in self._tracked_files or filepath in self._ignored:
                continue
            basename = os.path.basename(filepath)
            if self.channel_filter:
                if not basename.lower().startswith(self.channel_filter.lower()):
                    continue

            try:
                st = os.stat(filepath)
                st_size = st.st_size
                st_ino = getattr(st, "st_ino", 0) or 0
                st_mtime = getattr(st, "st_mtime", 0.0) or 0.0
                st_attrs = getattr(st, "st_file_attributes", 0) or 0
            except OSError:
                st_size = 0
                st_ino = 0
                st_mtime = 0.0
                st_attrs = 0

            # A cloud-only placeholder (see _CLOUD_PLACEHOLDER_ATTRS): every
            # open() on it fails, so the header probe would burn its whole
            # retry budget on it every rescan, poking the sync engine each
            # time. It cannot be a log EVE is writing, so ignore it outright —
            # and drop any retry budget it accumulated before this stat, or the
            # glob-free retry pass would keep it alive for the process's life.
            if st_attrs & _CLOUD_PLACEHOLDER_ATTRS:
                self._header_retry.pop(filepath, None)
                self._ignored.add(filepath)
                continue

            log_file = ChatLogFile(filepath)

            # Parse the header up front so the listener filter can be applied.
            # The three outcomes are kept apart (the _header_listener rule):
            # unreadable / readable-but-listener-less / a name.
            header_ok = True
            try:
                with open(filepath, "rb") as f:
                    header_bytes = f.read(4096)
                if not header_bytes:
                    # ZERO BYTES is not an answer, it is a race: the directory
                    # mtime bumps at CreateFile, so a poll can land between
                    # EVE creating the log and EVE writing its header. Treating
                    # that as "read fine, no Listener line" was permanent
                    # damage: the file was tracked with channel_name="" and
                    # listener="" and _header_parsed=True (so the tail never
                    # re-parsed), pinnable=False under a listener filter — so
                    # the CURRENT fleet log lost the pin to the previous
                    # session's, then dropped to the 30 s tier once its
                    # ACTIVE_WINDOW_S ran out. Fall into the retry budget
                    # instead; the header lands within milliseconds.
                    header_ok = False
                else:
                    header = header_bytes.decode("utf-16-le", errors="replace")
                    log_file._parse_header(header.split("\n"))
            except OSError:
                header_ok = False

            if not header_ok:
                # Could not READ it — so we know nothing, and the filter can
                # only exclude what it positively read. NEVER ignored: budget a
                # re-probe instead (bounded, so a permanently unreadable file
                # cannot turn into a per-poll retry) and leave it untracked so
                # the retry actually re-reads the header. Tracking it blind
                # would fix a wrong group key and an unknowable listener in
                # place for the monitor's whole life.
                left = self._header_retry.get(filepath, _HEADER_RETRY_ATTEMPTS)
                if left > 1:
                    self._header_retry[filepath] = left - 1
                else:
                    self._header_retry.pop(filepath, None)
                continue
            self._header_retry.pop(filepath, None)

            # If a listener (character) filter is set, skip files from other
            # characters — and REMEMBER them, so no later rescan re-reads this
            # header.
            if self.listener_filter and log_file.listener:
                if log_file.listener.lower() != self.listener_filter.lower():
                    self._ignored.add(filepath)
                    continue

            # A header we read fine that carries no Listener line is an
            # unattributable log, and it lands in the group (channel, "") — a
            # group nothing with a real listener can ever join. A pin there is
            # therefore effectively permanent AND exempts the file from the
            # DISCOVERY_MAX_IDLE_S cutoff forever, which on the owner's box
            # kept a truncated 2014 log immortally fast. Two cases:
            #   * a listener filter is active (the fleet monitor): it may never
            #     hold a pin at all — the group is a group of one by
            #     construction;
            #   * no listener filter (the intel monitor): (channel, "") is a
            #     legitimate group, so it may hold a pin — but only while it
            #     is RECENT. Past the idle cutoff it competes for nothing and
            #     falls through to the cutoff rule below like any other file.
            if not log_file.listener:
                if self.listener_filter:
                    log_file.pinnable = False
                elif (now_wall - st_mtime) > DISCOVERY_MAX_IDLE_S:
                    log_file.pinnable = False

            log_file.group_key = self._group_key(filepath, log_file.listener)
            log_file.pin_rank = self._pin_rank(filepath)
            log_file.last_mtime = st_mtime
            candidates.append((filepath, log_file, st_size, st_ino, st_mtime))

        # Pass 2: newest-by-FILENAME-STAMP per group (_pin_rank) across the new
        # candidates AND the files already tracked, so a long-idle newcomer with
        # a newer sibling is correctly recognised as NOT its group's newest.
        # Files that may not hold a pin are excluded outright — they must not be
        # able to claim a group's "newest" slot and so escape the idle cutoff.
        newest: dict[tuple[str, str], tuple] = {}
        for _filepath, log_file in self._tracked_files.items():
            if not log_file.pinnable:
                continue
            if log_file.pin_rank > newest.get(log_file.group_key, ()):
                newest[log_file.group_key] = log_file.pin_rank
        for _filepath, log_file, _size, _ino, _st_mtime in candidates:
            if not log_file.pinnable:
                continue
            if log_file.pin_rank > newest.get(log_file.group_key, ()):
                newest[log_file.group_key] = log_file.pin_rank

        for filepath, log_file, st_size, st_ino, st_mtime in candidates:
            # A file nobody has written to in DISCOVERY_MAX_IDLE_S is dead
            # history — unless it is its group's newest, which is the one file
            # that must stay live even when it looks ancient. "Idle" is an mtime
            # question (has anything touched this at all), while "newest" is a
            # filename-stamp question (which session is current); the two scales
            # are deliberately different.
            if ((now_wall - st_mtime) > DISCOVERY_MAX_IDLE_S
                    and (not log_file.pinnable
                         or log_file.pin_rank
                         != newest.get(log_file.group_key))):
                self._ignored.add(filepath)
                continue

            # Seed position. For a previously-seen file, resume from persisted state;
            # otherwise jump to EOF so we don't replay a day's history.
            key = self._state_key(filepath)
            prior = self._persisted_state.get(key)
            resume_pos = st_size  # default: skip to EOF for unknown files
            if prior:
                try:
                    saved_pos = int(prior.get("last_pos", 0))
                    saved_ino = int(prior.get("last_ino", 0))
                except (TypeError, ValueError):
                    saved_pos = 0
                    saved_ino = 0
                # Treat as the same file only if inode matches (when both known) and
                # current size has not shrunk below the saved position.
                inode_ok = (not saved_ino) or (not st_ino) or (saved_ino == st_ino)
                if inode_ok and st_size >= saved_pos:
                    resume_pos = saved_pos
                # else: treat as rotated/new - start from EOF (resume_pos = st_size).

            log_file._last_pos = resume_pos
            log_file._last_ino = st_ino
            # A file discovered already idle has just consumed its stat and has
            # nothing pending (its tail sits at EOF), so hand it a full slow
            # slot rather than reading it on this pass. Pinning is applied
            # below and overrides this anyway.
            if (not self._is_fast(log_file, now_wall)) and resume_pos >= st_size:
                log_file.next_poll_at = now_monotonic + IDLE_POLL_INTERVAL_S

            self._tracked_files[filepath] = log_file

        # The newest tracked file of each group is always polled at the fast
        # cadence; a new session file takes the pin over from its predecessor.
        self._repin_groups()

    def _poll_once(self) -> list[ChatMessage]:
        """Single poll cycle: discover new files and read the due ones.

        "Due" is the tiering rule (see the module-level note): pinned files and
        files with recent activity are read every pass; everything else is read
        once per IDLE_POLL_INTERVAL_S and is not even stat'ed in between, since
        the stat that decides the tier is the CACHED one from its last read.
        """
        self._discover_files()
        all_messages: list[ChatMessage] = []

        # Evict stale dedupe entries once per poll.
        self._evict_dedupe()

        now_wall = time.time()
        now_monotonic = time.monotonic()

        for log_file in self._tracked_files.values():
            fast = self._is_fast(log_file, now_wall)
            if not fast and now_monotonic < log_file.next_poll_at:
                continue            # slow tier, not its slot: no stat, no read
            raw_messages = log_file.read_new_lines()
            if not fast:
                # Next slot is measured from the read we just did, whether or
                # not it found anything. A read that DID find new bytes has
                # refreshed last_mtime, so the file is fast again regardless.
                log_file.next_poll_at = now_monotonic + IDLE_POLL_INTERVAL_S
            if raw_messages:
                # Update persisted state for this file.
                self._persisted_state[self._state_key(log_file.filepath)] = {
                    "last_pos": log_file._last_pos,
                    "last_ino": log_file._last_ino,
                    "last_updated": time.time(),
                }
                self._state_dirty = True

            for msg in raw_messages:
                if self._is_duplicate(msg):
                    continue
                all_messages.append(msg)

        # Flush state on an INTERVAL, never per active pass: the sidecar was
        # rewritten (atomically, so fsync'd) on every pass that saw a chat line,
        # which during a busy fleet meant a 37 KB fsync every second. Positions
        # are a replay optimisation, and flush_state() covers the teardown.
        now = time.time()
        if (self._state_dirty
                and (now - self._last_state_flush) >= STATE_FLUSH_INTERVAL_S):
            self._save_state()
            self._last_state_flush = now
            self._state_dirty = False

        return all_messages

    # -- dedupe --------------------------------------------------------

    @staticmethod
    def _dedupe_key(msg: ChatMessage) -> tuple:
        ts_str = msg.timestamp.strftime("%Y.%m.%d %H:%M:%S") if isinstance(msg.timestamp, datetime) else str(msg.timestamp)
        body_hash = hashlib.sha1(msg.message.encode("utf-8", errors="replace")).hexdigest()[:16]
        return (msg.channel, ts_str, msg.sender, body_hash)

    def _is_duplicate(self, msg: ChatMessage) -> bool:
        key = self._dedupe_key(msg)
        now = time.time()
        if key in self._seen:
            # Refresh (keeps very chatty duplicates suppressed for the full TTL
            # from last sighting rather than first).
            self._seen[key] = now
            return True
        self._seen[key] = now
        return False

    def _evict_dedupe(self) -> None:
        if not self._seen:
            return
        cutoff = time.time() - self._dedupe_ttl
        # Build list of stale keys to avoid mutating during iteration.
        stale = [k for k, t in self._seen.items() if t < cutoff]
        for k in stale:
            self._seen.pop(k, None)

    # -- state persistence --------------------------------------------

    @staticmethod
    def _state_key(filepath: str) -> str:
        # EVE filenames already embed channel name + ISO timestamp + characterID,
        # so the full path is stable enough as a key.
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

    def _save_state(self) -> None:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self._state_path)) or ".", exist_ok=True)
        except OSError:
            pass
        try:
            # Preserve the original compact (no-indent) JSON formatting.
            # dict() is a C-level copy (atomic under the GIL): flush_state() can
            # be called from the Tk thread while the poll thread is mutating
            # _persisted_state, and serialising the live dict would then raise
            # "dictionary changed size during iteration".
            atomic_write_json(
                self._state_path,
                dict(self._persisted_state),
                indent=None,
                ensure_ascii=True,
            )
        except Exception:
            # Best-effort - if we can't persist, log and try again later.
            log.exception("Failed to persist chat monitor state to %s", self._state_path)
