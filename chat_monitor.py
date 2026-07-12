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

# Dedupe TTL - drop duplicate messages (same channel/ts/sender/body) seen within this window.
DEDUPE_TTL_SECONDS = 60.0


class ChatLogFile:
    """Tracks a single chat log file, remembering byte-offset read position."""

    def __init__(self, filepath: str):
        self.filepath = filepath
        self.channel_name = ""
        self.listener = ""
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
    # (or when two events land within one mtime tick). 60 s bounds worst-case
    # new-channel latency while cutting the per-second glob by ~60x.
    _DIR_RESCAN_INTERVAL_SECONDS = 60.0

    def _discover_files(self):
        """Find chat log files matching the channel and listener filters.

        Skips the (potentially very expensive) directory glob when the logs
        directory is unchanged since the last scan: a new EVE chat-log file bumps
        the directory's mtime, whereas appends to already-tracked files do not, so
        a stable mtime means there is no new file to discover. A long backstop
        interval still forces an occasional full re-glob as a safety net. This
        keeps the common idle poll off the GIL-heavy glob that otherwise stalls the
        Tk main thread when the Chatlogs folder holds tens of thousands of files."""
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
        if unchanged and within_backstop:
            return  # directory unchanged since last scan — no new files to find
        self._last_dir_mtime = dir_mtime
        self._last_full_scan_monotonic = now_monotonic

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

        for filepath in all_files:
            if filepath in self._tracked_files:
                continue
            basename = os.path.basename(filepath)
            if self.channel_filter:
                if not basename.lower().startswith(self.channel_filter.lower()):
                    continue
            log_file = ChatLogFile(filepath)

            # Parse the header up front so the listener filter can be applied.
            try:
                with open(filepath, "rb") as f:
                    header_bytes = f.read(4096)
                header = header_bytes.decode("utf-16-le", errors="replace")
                log_file._parse_header(header.split("\n"))
            except OSError:
                pass

            # If a listener (character) filter is set, skip files from other characters
            if self.listener_filter and log_file.listener:
                if log_file.listener.lower() != self.listener_filter.lower():
                    continue

            # Seed position. For a previously-seen file, resume from persisted state;
            # otherwise jump to EOF so we don't replay a day's history.
            try:
                st = os.stat(filepath)
                st_size = st.st_size
                st_ino = getattr(st, "st_ino", 0) or 0
            except OSError:
                st_size = 0
                st_ino = 0

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

            self._tracked_files[filepath] = log_file

    def _poll_once(self) -> list[ChatMessage]:
        """Single poll cycle: discover new files and read new messages."""
        self._discover_files()
        all_messages: list[ChatMessage] = []
        had_activity = False

        # Evict stale dedupe entries once per poll.
        self._evict_dedupe()

        for log_file in self._tracked_files.values():
            raw_messages = log_file.read_new_lines()
            if raw_messages:
                had_activity = True
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

        # Flush state: after any activity, or periodically every ~30s.
        now = time.time()
        if self._state_dirty and (had_activity or (now - self._last_state_flush) > 30.0):
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
            atomic_write_json(
                self._state_path,
                self._persisted_state,
                indent=None,
                ensure_ascii=True,
            )
        except Exception:
            # Best-effort - if we can't persist, log and try again later.
            log.exception("Failed to persist chat monitor state to %s", self._state_path)
