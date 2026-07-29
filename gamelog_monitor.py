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
not resolve to a tracked character are header-read ONCE then skipped until the
tracked set changes (set_tracked_characters). Incremental byte-offset tailing +
the cheap "(combat)" prefix reject keep N-client scanning sub-millisecond.

Header-read economics (PERF PV2 / QUALITY_AUDIT C2-18, fixed 2026-07-28): a real
Gamelogs folder is an ARCHIVE, not a working set — 13,544 files on the owner's
box, of which only ~70 were written in the last week. Two rules keep the poll
thread off that archive:
  * a resolved `Listener:` is cached by MEMBERSHIP, so an empty header (~48% of
    the folder predates the `Listener:` line) is a real answer to the TAILER,
    never a per-poll miss — but the gated scan still re-asks an empty answer,
    because a transient open failure and EVE's create-then-write-the-header gap
    both LOOK like "no header", so a live file self-heals within one backstop;
  * only a file that could still be live — written inside
    `_SEED_TAIL_WINDOW_SECONDS`, or already holding unread bytes — pays the 2 KB
    header read at seed. Older files are registered at EOF with the read
    DEFERRED and are resolved lazily if they ever show a sign of life.

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


# ── source diagnostics ───────────────────────────────────────────────────────
# The damage-flash AND decloak features both read the Gamelogs folder, so a
# missing/empty/mis-detected folder kills BOTH silently (the friend-box bug:
# flashes work for the owner, dead for a friend whose Documents layout differs).
# The monitor keeps a cheap immutable status snapshot — computed on its OWN poll
# thread, read by the settings status line via status_snapshot() — so the source
# is diagnosable at a glance without any per-tick IO on the Tk thread.
@dataclass(frozen=True)
class GamelogStatus:
    logs_dir: str           # the effective Gamelogs directory ("" if none)
    dir_exists: bool        # os.path.isdir(logs_dir)
    scanning: bool          # True once a live poll produced this (vs a static probe)
    file_count: int         # gamelog files discovered in the folder
    active_count: int       # discovered files whose Listener is a tracked character
    unmatched: tuple        # tracked characters with no discovered gamelog file yet


def compute_status(logs_dir, positions, listeners, tracked, *,
                   scanning, dir_exists=None):
    """Build a :class:`GamelogStatus` from the monitor's in-memory dicts.

    Does at most one cheap ``os.path.isdir`` stat (NEVER a glob — the mtime-gate
    invariant forbids per-poll globbing) unless ``dir_exists`` is supplied.
    ``positions`` is ``{path: offset}``; ``listeners`` is ``{path: char}`` whose
    value may also be ``""`` (read, no `Listener:` line) or ``None`` (header read
    DEFERRED at seed) — both are falsy here, so such a file counts toward
    ``file_count`` but never toward ``active_count`` or ``present``.
    ``tracked`` is the lowercased tracked-char set, or ``None`` (track everything).
    Pure apart from the optional single stat — fully unit-testable."""
    d = logs_dir or ""
    if dir_exists is None:
        try:
            dir_exists = bool(d) and os.path.isdir(d)
        except OSError:
            dir_exists = False
    tset = (None if tracked is None
            else {str(t).strip().lower() for t in tracked if str(t).strip()})
    present = set()
    active = 0
    for path in positions:
        ln = (listeners.get(path) or "").strip().lower()
        if not ln:
            continue
        present.add(ln)
        if tset is None or ln in tset:
            active += 1
    unmatched = tuple(sorted(tset - present)) if tset else ()
    return GamelogStatus(logs_dir=d, dir_exists=bool(dir_exists),
                         scanning=bool(scanning), file_count=len(positions),
                         active_count=active, unmatched=unmatched)


def probe_status(logs_dir):
    """Static (monitor-not-running) snapshot: folder existence only, no scan."""
    return compute_status(logs_dir, {}, {}, None, scanning=False)


def gamelog_status_line(snap):
    """Concise, actionable one-liner for the settings status label. Pure.

    Order matters: absence beats emptiness beats character-mismatch, so the most
    actionable message always wins."""
    if snap is None:
        return "Gamelogs: source unknown"
    d = snap.logs_dir
    if not d:
        return "Gamelogs: no folder found — set one with Browse…"
    if not snap.dir_exists:
        return f"Gamelogs: folder not found — {d}"
    if not snap.scanning:
        return f"Gamelogs: {d}"                 # idle: monitor not running yet
    if snap.file_count == 0:
        return ("Gamelogs: folder has no gamelog files — enable EVE ▸ "
                "Settings ▸ 'Log game events to file'")
    if snap.active_count == 0:
        return (f"Gamelogs: {snap.file_count} file(s) found, none for your "
                f"previewed characters yet")
    if snap.unmatched:
        return (f"Gamelogs: watching {snap.active_count} file(s) — no recent log "
                f"for {', '.join(snap.unmatched)}")
    return f"Gamelogs: watching {snap.active_count} file(s)"


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
        # path -> `Listener:` for that file. FOUR states, all load-bearing:
        #   ABSENT   never registered (only reachable before seed_file runs);
        #   None     header read DEFERRED at seed (archive file — see seed_file);
        #   ""       read produced no name. Usually final (~48% of a real folder
        #            predates the header line) but NOT always — see discover_files;
        #   "Name"   resolved and authoritative.
        self._listeners: dict[str, str | None] = {}
        # New-file discovery gate (see discover_files) — mirrors chat_monitor's
        # `_last_dir_mtime` gate verbatim: re-globbing the Gamelogs directory is
        # O(files-in-dir) and, like Chatlogs, it accumulates files indefinitely
        # over a long session, so an unconditional glob every 1s poll holds the
        # GIL long enough to stall the Tk main thread. A new Gamelog file bumps
        # the directory's mtime (creation), while damage/decloak lines appended
        # to already-tracked files do not, so we glob only when the directory
        # changed since the last scan, with a backstop interval as a safety net.
        self._last_dir_mtime: float | None = None
        self._last_full_scan_monotonic: float = 0.0
        # Set of lowercased character names with a live preview tile; None means
        # "track everything" (before the tick has told us otherwise).
        self._tracked: set[str] | None = None
        # Daemon thread / stop-event fields (mirror ChatMonitor).
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._state_dirty = False
        self._last_state_flush = 0.0
        self._persisted_state: dict[str, dict] = self._load_state()
        # Cheap diagnostic snapshot read by the settings status line via
        # status_snapshot(); refreshed at the end of every poll_once and whenever
        # the watched dir is re-pointed. Immutable → atomic reference swap, so the
        # Tk thread reads it without a lock. Seeded here so a reader before the
        # first poll gets a valid (idle) snapshot rather than None.
        self._status = probe_status(self._logs_dir)

    # --- public interface --------------------------------------------------

    def status_snapshot(self):
        """Return the latest :class:`GamelogStatus` (atomic reference read; the
        poll thread swaps in a fresh immutable snapshot each cycle)."""
        return self._status

    def set_logs_dir(self, logs_dir) -> None:
        """Re-point the monitor at a new Gamelogs directory (settings override
        change) WITHOUT a restart.

        Clears per-file tail state so the old dir's files stop being tailed,
        resets the discovery mtime-gate so the new dir is re-globbed on the next
        poll, and refreshes the status snapshot immediately. A no-op when the
        directory is unchanged (so a redundant Clear/Browse never wipes live
        tail positions)."""
        new = logs_dir or None
        if new == self._logs_dir:
            return
        self._logs_dir = new
        self._positions.clear()
        self._fingerprints.clear()
        self._buffers.clear()
        self._listeners.clear()
        self._last_dir_mtime = None
        self._last_full_scan_monotonic = 0.0
        self._status = probe_status(self._logs_dir)

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

    def _listener_for(self, path) -> str:
        """Cached `Listener:` for `path` — reads the 2 KB header AT MOST ONCE.

        MEMBERSHIP-based on purpose. The shape this replaces,
        ``self._listeners.get(path) or self._read_listener(path)``, treated a
        legitimately EMPTY header ("") as a cache MISS, so every headerless file
        was re-opened and re-parsed on EVERY 1 s poll, forever — ~6,500 files on
        the owner's folder (~48% of it predates the `Listener:` header line), at
        0.15–4.45 ms each. That alone kept the poll thread permanently saturated
        (PERF PV2 / QUALITY_AUDIT C2-18).

        A stored ``None`` means the header read was DEFERRED at seed time (see
        seed_file): resolving it here is the lazy path, taken only once the file
        has earned an open by actually having content to tail. A stored ``""`` is
        returned verbatim — re-asking an empty answer is the gated scan's job
        alone (discover_files), never the tailer's, or the per-poll re-read this
        cache exists to kill comes straight back.
        """
        cached = self._listeners.get(path)
        if cached is not None:          # "" is a REAL answer (headerless file)
            return cached
        listener = self._read_listener(path)
        self._listeners[path] = listener
        return listener

    def _within_tail_window(self, mtime) -> bool:
        """True when `mtime` is recent enough that EVE could still be appending."""
        if mtime is None:
            return False
        return (time.time() - mtime) <= self._SEED_TAIL_WINDOW_SECONDS

    def _is_tracked(self, listener: str) -> bool:
        if self._tracked is None:
            return True
        return bool(listener) and listener.lower() in self._tracked

    # --- discovery ---------------------------------------------------------

    def _logs_directory(self):
        return self._logs_dir

    # Backstop: force a full directory re-glob at least this often even when the
    # directory mtime looks unchanged, mirroring chat_monitor._discover_files's
    # _DIR_RESCAN_INTERVAL_SECONDS. 60s bounds worst-case new-file discovery
    # latency while cutting the per-second glob by ~60x on a long session.
    _DIR_RESCAN_INTERVAL_SECONDS = 60.0

    # Live tail window (PERF PV2). A file whose last write is older than this
    # cannot be one a client still holds open — EVE opens a NEW Gamelog per
    # session, and a week of silence means that session is long over — so its
    # `Listener:` header is not worth reading at seed time. Measured file ages in
    # the owner's 13,544-file folder: 4 within 1 h, 12 within 6 h, 45 within 24 h,
    # 71 within 7 days. A week therefore header-reads ~0.5% of the folder
    # (~11–36 ms) instead of all of it (~60 s), while keeping every file the
    # client could conceivably still be appending to on the EAGER path.
    _SEED_TAIL_WINDOW_SECONDS = 7 * 24 * 3600.0

    def _dir_stat_index(self, logs_dir):
        """``{path: os.stat_result}`` for the whole Gamelogs dir, from ONE
        directory enumeration. ``{}`` on any failure (callers fall back to a
        direct stat).

        ``os.scandir`` serves size/mtime out of the enumeration itself, whereas
        ``os.stat``/``getsize`` re-opens each file — which on this directory
        class (OneDrive-hosted, real-time AV filter) costs 71–85 µs per file and
        never warms up. Measured on the owner's 13,544-file folder: **62 ms for
        the whole sweep vs 963 ms of individual stats**. Called ONLY from
        discover_files' already-gated scan branch (directory-mtime change or the
        60 s backstop) — never per poll."""
        index = {}
        try:
            with os.scandir(logs_dir) as it:
                for entry in it:
                    try:
                        if not entry.is_file():
                            continue
                        # Key exactly as glob.glob builds its results, so the
                        # index and self._positions share one path spelling.
                        index[os.path.join(logs_dir, entry.name)] = entry.stat()
                    except OSError:
                        continue
        except OSError:
            return {}
        return index

    def _resolve_deferred(self, path, st) -> None:
        """Promote a file whose listener is not yet a real name — DEFERRED at seed
        (``None``) or an empty ``""`` that may have been transient (see
        discover_files) — once it shows a sign of life: unread bytes past our
        recorded position, or a write inside the live tail window.

        The store is unconditional and lands BEFORE any tail emission this cycle
        (poll_once runs discover_files first), so a promoted file is attributed
        from its very next line; a re-read that is still headerless simply stores
        ``""`` again, harmlessly.

        Runs on discover_files' gated scan, reusing the stat that scan already
        holds, so a dead archive file costs nothing at all — no open, and no
        per-poll stat either. Checking growth per POLL instead is not viable at
        this scale: 13,544 individual stats measure ~0.96 s on this folder class,
        which would re-saturate exactly the thread PV2 frees. The cost of that
        choice is bounded promotion latency (one backstop interval, 60 s) for a
        file the client cannot actually be writing to (see
        _SEED_TAIL_WINDOW_SECONDS)."""
        if st is None:
            return
        if (st.st_size > self._positions.get(path, 0)
                or self._within_tail_window(st.st_mtime)):
            self._listeners[path] = self._read_listener(path)

    def discover_files(self):
        """Register any new Gamelog files (seed to EOF). Constant-rotation safe:
        new files appear every jump/warp/session change.

        Skips the (potentially very expensive) directory glob when the Gamelogs
        directory is unchanged since the last scan: a new Gamelog file bumps the
        directory's mtime, whereas appends to already-tracked files do not, so a
        stable mtime means there is no new file to discover. A long backstop
        interval still forces an occasional full re-glob as a safety net. This
        keeps the once-per-second poll off the GIL-heavy glob that otherwise
        stalls the Tk main thread once the Gamelogs folder accumulates many
        files over a long session (mirrors chat_monitor._discover_files)."""
        logs_dir = self._logs_directory()
        if not logs_dir:
            return
        now_monotonic = time.monotonic()
        try:
            dir_mtime = os.path.getmtime(logs_dir)
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
        try:
            paths = glob.glob(os.path.join(logs_dir, "*.txt"))
        except OSError:
            return
        # One enumeration feeds BOTH jobs below (seed sizing + deferred
        # promotion); see _dir_stat_index for why it is not one stat per file.
        stats = self._dir_stat_index(logs_dir)
        for path in paths:
            if path not in self._positions:
                self.seed_file(path, st=stats.get(path))
            elif not self._listeners.get(path):
                # FALSY listener — retry it here, gated by _resolve_deferred's
                # "grew OR still inside the tail window" test (PERF PV2). All
                # three falsy states earn that retry:
                #   None   → header read DEFERRED at seed (archive file);
                #   ""     → a read that found no `Listener:`. Usually the real,
                #            final answer — but _read_listener ALSO returns "" on
                #            an OSError (this box's real-time AV filter fails
                #            opens transiently, a documented class) and for a live
                #            file whose first 2 KB do not hold the header YET:
                #            creating the file bumps the dir mtime and therefore
                #            arms the very next gated scan, which races EVE's
                #            header write. Treating those as authoritative forever
                #            silently kills damage flash + decloak alerts for that
                #            character until restart, so the empty answer must
                #            stay retryable HERE (self-heals within one backstop,
                #            ≤60 s) even though it is authoritative to the tailer;
                #   absent → never resolved at all; same treatment.
                # The archive cost model is unchanged: an out-of-window file that
                # has not grown fails _resolve_deferred's gate and is never opened
                # (~6,500 such files on the owner's box), and an in-window empty
                # header is retried at most once per gated scan (backstop 60 s,
                # but a dir-mtime bump — e.g. a new gamelog being created — can
                # arm one sooner) — a few dozen (71 in-window files measured).
                self._resolve_deferred(path, stats.get(path))

    def seed_file(self, path, st=None):
        """Register a file and set its position to EOF (existing lines not replayed).

        `st` is an optional pre-fetched ``os.stat_result`` (see _dir_stat_index);
        omit it and the file is stat'ed directly.

        HEADER-READ POLICY (PERF PV2): the 2 KB `Listener:` header read is paid
        only for a file that could still be LIVE — last written inside
        `_SEED_TAIL_WINDOW_SECONDS`, or already holding unread bytes (a
        persisted-state resume gap means we are about to tail it anyway). Every
        older file is registered at EOF with its listener DEFERRED (`None`) and
        is never opened; `_resolve_deferred` reads its header on a later gated
        scan if it ever grows, and attribution works from that point on.
        Before this, enabling previews header-read the whole folder — 13,533
        files × 4.45 ms ≈ **60 s of blocking I/O**, re-paid on every
        `set_logs_dir`."""
        if st is None:
            try:
                st = os.stat(path)
            except OSError:
                st = None
        size = st.st_size if st is not None else 0
        mtime = st.st_mtime if st is not None else None
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
        eager = resume < size or self._within_tail_window(mtime)
        self._listeners[path] = self._read_listener(path) if eager else None

    def poll_once(self):
        """One cycle: discover new files, then tail each tracked file."""
        self.discover_files()
        for path in list(self._positions):
            if self._listeners.get(path, "") is None:
                # DEFERRED (None): the seed skipped the 2 KB header read (archive
                # file, older than the live tail window). Not opened and not even
                # stat'ed here — discover_files' gated scan owns EVERY promotion;
                # this site never re-asks a question that would cost an open.
                # (That asymmetry with discover_files' `not …` test is deliberate:
                # the gated scan retries falsy listeners nearly for free off a
                # stat it already holds, whereas retrying here would be per-poll.)
                # The "" default is load-bearing — it maps the fourth state,
                # ABSENT, onto "never resolved" (resolved by _listener_for below)
                # rather than onto "deferred". The remaining two states fall
                # through by design: a real name is tailed, and "" reaches
                # _is_tracked, which rejects it whenever a tracked set exists
                # (fc_gui always supplies one; `None` = track-everything is only
                # the pre-first-tick state).
                continue
            listener = self._listener_for(path)
            if not self._is_tracked(listener):
                continue
            self.poll_file(path)
        self._maybe_flush_state()
        # Refresh the diagnostic snapshot from the just-updated in-memory state
        # (one cheap isdir, no glob). Runs on the poll thread — never the Tk one.
        self._status = compute_status(self._logs_dir, self._positions,
                                      self._listeners, self._tracked,
                                      scanning=True)

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

        The file's `Listener:` is resolved only once there is actually new text
        to attribute (below), so a pass that finds nothing new never opens the
        file for its header (PERF PV2). That resolve is DEFENCE IN DEPTH for
        DIRECT callers only: reached via poll_once, a still-deferred (`None`)
        file is `continue`d before it ever gets here, so in the running monitor
        the deferred→resolved promotion always happens on discover_files' gated
        scan instead. Keep the call — it makes poll_file correct standalone.
        """
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

        # There IS new text — now the header is worth resolving (cached after the
        # first read; a deferred seed resolves here, lazily, exactly once).
        listener = self._listener_for(path)
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
