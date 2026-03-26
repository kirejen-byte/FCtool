"""
EVE Online Chat Log Monitor
Tails EVE chat log files in real-time, parsing UTF-16LE encoded messages.
"""

import os
import re
import glob
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable


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


class ChatLogFile:
    """Tracks a single chat log file, remembering read position."""

    def __init__(self, filepath: str):
        self.filepath = filepath
        self.channel_name = ""
        self.listener = ""
        self._last_pos = 0
        self._header_parsed = False

    def _parse_header(self, lines: list[str]):
        for line in lines:
            m = HEADER_CHANNEL_PATTERN.search(line)
            if m:
                self.channel_name = m.group(1).strip()
            m = HEADER_LISTENER_PATTERN.search(line)
            if m:
                self.listener = m.group(1).strip()
        self._header_parsed = True

    def read_new_lines(self) -> list[ChatMessage]:
        """Read any new lines appended since last check."""
        messages = []
        try:
            file_size = os.path.getsize(self.filepath)
            if file_size <= self._last_pos:
                return messages

            with open(self.filepath, "r", encoding="utf-16-le", errors="replace") as f:
                f.seek(self._last_pos)
                content = f.read()
                self._last_pos = f.tell()

            lines = content.split("\n")

            if not self._header_parsed:
                self._parse_header(lines)

            for line in lines:
                line = line.strip()
                if not line:
                    continue
                m = MESSAGE_PATTERN.match(line)
                if m:
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
        except (OSError, IOError) as e:
            pass  # File may be locked by EVE, retry next poll
        return messages


class ChatMonitor:
    """
    Monitors the EVE chat logs directory for new messages.
    Polls for file changes and dispatches messages to registered callbacks.
    """

    def __init__(self, logs_path: str, poll_interval: float = 1.0,
                 channel_filter: str | None = None,
                 listener_filter: str | None = None):
        self.logs_path = logs_path
        self.poll_interval = poll_interval
        self.channel_filter = channel_filter
        self.listener_filter = listener_filter  # Character name to track
        self._tracked_files: dict[str, ChatLogFile] = {}
        self._callbacks: list[Callable[[ChatMessage], None]] = []
        self._running = False

    def on_message(self, callback: Callable[[ChatMessage], None]):
        """Register a callback for new chat messages."""
        self._callbacks.append(callback)

    def _discover_files(self):
        """Find chat log files matching the channel and listener filters."""
        pattern = os.path.join(self.logs_path, "*.txt")
        for filepath in glob.glob(pattern):
            if filepath in self._tracked_files:
                continue
            basename = os.path.basename(filepath)
            if self.channel_filter:
                if not basename.lower().startswith(self.channel_filter.lower()):
                    continue
            log_file = ChatLogFile(filepath)
            # Skip to end of existing content so we only get NEW messages
            try:
                log_file._last_pos = os.path.getsize(filepath)
                # But parse the header from existing content
                with open(filepath, "r", encoding="utf-16-le", errors="replace") as f:
                    header = f.read(2048)
                log_file._parse_header(header.split("\n"))
            except OSError:
                pass

            # If a listener (character) filter is set, skip files from other characters
            if self.listener_filter and log_file.listener:
                if log_file.listener.lower() != self.listener_filter.lower():
                    continue

            self._tracked_files[filepath] = log_file

    def _poll_once(self) -> list[ChatMessage]:
        """Single poll cycle: discover new files and read new messages."""
        self._discover_files()
        all_messages = []
        for log_file in self._tracked_files.values():
            messages = log_file.read_new_lines()
            all_messages.extend(messages)
        return all_messages

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

    def get_available_listeners(self) -> list[str]:
        """Scan log files to find all character names (listeners) with fleet channels."""
        listeners = set()
        pattern = os.path.join(self.logs_path, "*.txt")
        filter_prefix = (self.channel_filter or "").lower()
        for filepath in glob.glob(pattern):
            basename = os.path.basename(filepath)
            if filter_prefix and not basename.lower().startswith(filter_prefix):
                continue
            try:
                with open(filepath, "r", encoding="utf-16-le", errors="replace") as f:
                    header = f.read(2048)
                for line in header.split("\n"):
                    m = HEADER_LISTENER_PATTERN.search(line)
                    if m:
                        listeners.add(m.group(1).strip())
            except OSError:
                pass
        return sorted(listeners)

    def stop(self):
        self._running = False
