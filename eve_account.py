"""Account identity acquisition + the ``AccountMap`` ground-truth service.

Pure module: stdlib ``ctypes`` only (no psutil, mirroring ``eve_client_tracker``/
``eveo_tracker``), Tk-free, every external effect injectable. A worker thread may
write ``AccountMap`` while the Tk tick reads it (repo invariant: workers never
touch Tk); the map is plain lock-free data owned by whoever constructs it — the
GUI drives ``observe`` from the tick and reads on the same thread, so no lock is
needed today. If a future caller mutates it off the tick thread, add a lock.

SECURITY (design §12 — a reviewer checks these):
  * An EVE client's command line carries live ``/ssoToken`` and ``/refreshToken``.
    ``parse_launcher_data`` receives the raw string and returns **only an int**;
    the raw command line is never stored, logged, displayed, or returned by any
    function here. ``_RealWin32.command_line`` is the sole producer of the raw
    string and its every caller (``account_id_for_pid``) discards it immediately.
  * The launcher-log reader retains only ``(float ts, int userId)`` tuples — raw
    log lines are dropped for the same reason.
  * Account *display names* (launcher state/roster files) are login-credential
    halves: never read here. Labels come only from user aliases and char names.

Interfaces the rest of the feature depends on (keep these names/types stable):
  parse_launcher_data(cmdline) -> int | None
  account_id_for_pid(pid, win32) -> int | None                       # Tier 0
  account_id_from_log(proc_start_utc, log_reader, window_s=5.0) -> int | None  # Tier 1
  class AccountMap(*, win32, log_reader, store, clock, aliases, hint)
Production collaborators (injected in tests, wired by later tasks):
  real_win32() -> _RealWin32                 # PEB command line + create time
  read_launcher_log_records(...) -> list[tuple[float, int]]
  class FileStore(path)                      # atomic sidecar IO
  class CorruptSidecar(Exception)            # load() signal, carries .raw
"""
from __future__ import annotations

import base64
import binascii
import ctypes
import glob
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# --- tuning ------------------------------------------------------------------
_NEG_RETRY_INTERVAL_S = 10.0   # a per-PID miss re-probes no sooner than this
_MAX_NEG_RETRIES = 3           # size of the FAST initial burst (design §6): 1
                               # initial probe + 3 retries = 4 probes ~10s apart.
                               # NOT a give-up threshold — after the burst the
                               # probe keeps retrying on the slow cadence below.
_NEG_RETRY_INTERVAL_LONG_S = 60.0  # post-burst slow-retry cadence; a stranded PID
                               # is re-probed at this interval forever, never given
                               # up on. AV on this box can block the cross-process
                               # command-line read for a client's first ~minute
                               # (fc_gui.py:17604), so a client launched WHILE FCTool
                               # is running can miss its whole burst. The old permanent
                               # exhaustion then stranded that account at None for the
                               # life of a long-lived process (a login screen never
                               # dies) — blanking its labels, breaking acct: cycling and
                               # login-drag persistence. The slow backoff re-probes Tier 0
                               # ONLY (the cheap cmdline ctypes read) and self-heals the
                               # instant AV releases the process; Tier 1's ~512KB log-tail
                               # read is deterministic per process, so the fast burst
                               # already gave it its shot. A truly-unresolvable PID (e.g.
                               # GeForce Now, no local cmdline) costs one sub-ms probe/60s.
_LOG_TAIL_BYTES = 512 * 1024   # launcher-log tail read (design §6)
_LOG_MAX_AGE_S = 48 * 3600     # ignore launcher logs older than 48 h


# ---------------------------------------------------------------------------
# Tier 0 parser + probe (pure)
# ---------------------------------------------------------------------------
_LAUNCHER_DATA_RE = re.compile(r"/LauncherData=([A-Za-z0-9+/=]+)")


def parse_launcher_data(cmdline: str) -> int | None:
    """Account id from a client's ``/LauncherData=`` argument, else ``None``.

    Contract (design §6): regex-extract the base64 blob, decode it, split on
    ``:``, take field index 3 as an int. Any deviation (no match, bad base64,
    fewer than four fields, non-numeric id) yields ``None``. The input is
    treated as secret: it is never echoed, stored, or logged — only an int (or
    ``None``) leaves this function.
    """
    if not cmdline:
        return None
    m = _LAUNCHER_DATA_RE.search(cmdline)
    if not m:
        return None
    try:
        decoded = base64.b64decode(m.group(1)).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return None
    parts = decoded.split(":")
    if len(parts) < 4:
        return None
    field = parts[3]
    # ``isdigit()`` alone rejects "", "-5", "notanumber" but is also True for
    # non-ASCII digits: superscripts like "²" make int() raise, and
    # Arabic-Indic digits silently parse. Require ASCII too so this is an
    # all-ASCII-decimal string and int() cannot raise.
    return int(field) if field.isascii() and field.isdigit() else None


def account_id_for_pid(pid: int, win32) -> int | None:
    """Tier 0: read *pid*'s command line via *win32* and parse the account id.

    ``win32.command_line`` raises ``OSError`` when the process is gone or the
    read is denied; that (and an empty command line) yields ``None`` so the
    caller can fall through to Tier 1. The raw command line is a local here and
    never leaves this function.
    """
    try:
        cmdline = win32.command_line(pid)
    except OSError:
        return None
    return parse_launcher_data(cmdline)


def account_id_from_log(proc_start_utc: float, log_reader, window_s: float = 5.0) -> int | None:
    """Tier 1: correlate the launcher log against a process's start time.

    *log_reader* returns already-parsed ``(utc_ts, userId)`` records (no raw
    text). Keep those within ``window_s`` of *proc_start_utc*; accept iff
    exactly one distinct userId remains. Zero or several -> ``None`` (never
    guess — design §6).
    """
    try:
        records = list(log_reader())
    except Exception:
        return None
    ids = set()
    for rec in records:
        try:
            ts, uid = rec
            ts = float(ts)
            uid = int(uid)
        except (TypeError, ValueError):
            continue  # malformed record (unpack or numeric conversion) -> skip
        if abs(ts - proc_start_utc) <= window_s:
            ids.add(uid)
    if len(ids) == 1:
        return next(iter(ids))
    return None


# ---------------------------------------------------------------------------
# Sidecar signal + helpers
# ---------------------------------------------------------------------------
class CorruptSidecar(Exception):
    """Raised by :meth:`AccountMap.load` when the sidecar is malformed or an
    unknown version. Carries the original text as ``.raw`` so the wiring layer
    can preserve it as ``account_char_map.json.corrupt`` (config.json
    precedent). The map is left EMPTY and fully usable after this is raised —
    memory is cleared *before* the raise, so a caller that catches it can keep
    going and rebuild from live observation.
    """

    def __init__(self, message: str, raw: str | None = None):
        super().__init__(message)
        self.raw = raw


def _normalize_label(text: str) -> str:
    """Trim and collapse internal whitespace (design §9.5 alias normalization)."""
    return " ".join(str(text).split())


# ---------------------------------------------------------------------------
# AccountMap — three-tier cache (per-PID probe / live session map / sidecar)
# ---------------------------------------------------------------------------
class AccountMap:
    """Ground-truth account<->character map fed by per-PID probing.

    Collaborators are injected (design §4): *win32* (``command_line`` +
    ``process_create_time``), *log_reader* (``() -> list[(ts, userId)]``),
    *store* (``read()``/``write()`` sidecar IO), *clock* (``() -> float`` UTC
    epoch), *aliases* (``() -> {"<id>": "label"}`` from config), *hint*
    (``(int) -> str | None`` last-active char name, wrapping the mtime co-flush
    heuristic). The module imports neither config nor Tk.
    """

    def __init__(self, *, win32, log_reader, store, clock, aliases, hint):
        self._win32 = win32
        self._log_reader = log_reader
        self._store = store
        self._clock = clock
        self._aliases = aliases
        self._hint = hint

        # Tier 1 cache: per-PID probe result (positive cached forever; None =
        # missed-so-far). ``_pid_neg[pid] = (attempts, last_attempt_ts)``.
        self._pid_cache: dict[int, int | None] = {}
        self._pid_neg: dict[int, tuple[int, float]] = {}

        # Tier 2 cache: live session map, rebuilt every observe() from the
        # current clients. ``_live_accounts`` includes login-screen accounts;
        # ``_live_*_to_*`` cover logged-in clients only (they have a char key).
        self._live_accounts: set[int] = set()
        self._live_acct_to_char: dict[int, str] = {}
        self._live_char_to_acct: dict[str, int] = {}

        # Tier 3 cache: persistent sidecar ``{char_key: {"account", "seen"}}``.
        self._sidecar: dict[str, dict] = {}
        self._dirty = False

    # -- persistence --------------------------------------------------------
    def load(self) -> None:
        """Load the sidecar into memory. Missing file -> empty (normal first
        run). Malformed / unknown-version -> memory left empty and
        :class:`CorruptSidecar` raised (the wiring preserves the file as
        ``.corrupt``)."""
        self._sidecar = {}
        self._dirty = False
        text = self._store.read()
        if text is None:
            return
        try:
            data = json.loads(text)
        except (ValueError, TypeError) as exc:
            raise CorruptSidecar("sidecar is not valid JSON", raw=text) from exc
        if (not isinstance(data, dict) or data.get("version") != 1
                or not isinstance(data.get("chars"), dict)):
            raise CorruptSidecar("sidecar has an unknown version or shape", raw=text)
        chars: dict[str, dict] = {}
        for key, val in data["chars"].items():
            if not isinstance(key, str) or not isinstance(val, dict):
                continue
            acct = val.get("account")
            if isinstance(acct, bool) or not isinstance(acct, int):
                continue  # skip a malformed entry; the file as a whole is fine
            seen = val.get("seen")
            chars[key] = {"account": acct, "seen": seen if isinstance(seen, str) else ""}
        self._sidecar = chars
        self._dirty = False

    def save(self) -> None:
        """Write the sidecar iff it changed since load/last save (OneDrive-
        friendly — design §14)."""
        if not self._dirty:
            return
        payload = {"version": 1, "chars": self._sidecar}
        self._store.write(json.dumps(payload, ensure_ascii=False, indent=2,
                                     sort_keys=True))
        self._dirty = False

    # -- observation --------------------------------------------------------
    def observe(self, clients: list) -> None:
        """Probe each client's PID, rebuild the live map, upsert the sidecar.

        Reads each client's ``.pid``, ``.is_login``, ``.key`` (the
        ``ClientWindow`` surface; tests pass a stub exposing them). Resolution
        per PID is per-PID cache -> Tier 0 -> Tier 1, cached with a fast
        negative-retry burst then an indefinite slow backoff (a transient miss
        self-heals). The live map is rebuilt from scratch each call so a
        departed client stops resolving via ``char_for_account``. A per-client
        probe fault is contained — that client is left unknown, the next is
        unaffected (design §11). The caller gates this behind the
        ``preview.account_identity`` kill switch (Task 6 wiring); when off it
        simply is not called.
        """
        live_pids = {c.pid for c in clients}
        # Evict caches for PIDs the tracker no longer reports (design §7.1).
        self._pid_cache = {p: v for p, v in self._pid_cache.items() if p in live_pids}
        self._pid_neg = {p: v for p, v in self._pid_neg.items() if p in live_pids}

        self._live_accounts = set()
        self._live_acct_to_char = {}
        self._live_char_to_acct = {}
        for c in clients:
            try:
                account = self._resolve_pid(c.pid)
                if account is None:
                    continue
                self._live_accounts.add(account)
                if not c.is_login:
                    key = c.key
                    if key:
                        self._live_acct_to_char[account] = key
                        self._live_char_to_acct[key] = account
                        self._upsert_sidecar(key, account)
            except Exception:
                # Never let one bad client poison the tick. PID is not secret.
                log.debug("account probe failed for pid=%s", getattr(c, "pid", None))
                continue

    def _resolve_pid(self, pid: int) -> int | None:
        """Per-PID cache -> Tier 0 -> Tier 1. A miss is retried in a fast initial
        burst (Tier 0 + Tier 1), then INDEFINITELY on a slow backoff that runs
        Tier 0 alone — a transient failure (AV blocking the command-line read on
        a client's first ~minute) must self-heal, so a live PID is never
        permanently given up on. Tier 1 is burst-only (see the Tier 1 gate)."""
        cached = self._pid_cache.get(pid, _UNSET)
        if cached is not _UNSET and cached is not None:
            return cached  # positive result — probe exactly once per process
        now = self._clock()
        neg = self._pid_neg.get(pid)
        attempts = neg[0] if neg is not None else 0   # prior misses (0 = first probe)
        if neg is not None:
            last_ts = neg[1]
            # Fast burst first (attempts <= _MAX_NEG_RETRIES) for quick
            # resolution, then an indefinite slow backoff instead of giving up:
            # keep re-probing every _NEG_RETRY_INTERVAL_LONG_S so an AV-blocked
            # startup resolves the moment the command-line read succeeds.
            interval = (_NEG_RETRY_INTERVAL_S if attempts <= _MAX_NEG_RETRIES
                        else _NEG_RETRY_INTERVAL_LONG_S)
            if now - last_ts < interval:
                return None  # too soon to retry on the current cadence

        account = account_id_for_pid(pid, self._win32)          # Tier 0
        # Tier 1 (launcher-log correlation) runs only during the fast burst. Its
        # result is deterministic for a fixed process-create-time, so a 60s
        # backoff re-run cannot change the answer — it would only repeat a
        # ~512KB log-tail read on the Tk/render thread. The burst already gave
        # Tier 1 its shot (covering a slightly-late log line); the indefinite
        # backoff needs only the cheap Tier 0 cmdline read, which is the probe
        # that actually self-heals once AV releases the process.
        if account is None and attempts <= _MAX_NEG_RETRIES:    # Tier 1
            try:
                start = self._win32.process_create_time(pid)
                account = account_id_from_log(start, self._log_reader)
            except OSError:
                account = None

        if account is not None:
            self._pid_cache[pid] = account
            self._pid_neg.pop(pid, None)
            return account
        self._pid_neg[pid] = ((neg[0] + 1) if neg else 1, now)
        self._pid_cache[pid] = None
        return None

    def _upsert_sidecar(self, char_key: str, account_id: int) -> None:
        """Record ``char_key -> account_id`` (last observation wins on a
        conflict). Unchanged mappings do not dirty the map — that is what keeps
        steady-state ticks from churning the sidecar file."""
        prev = self._sidecar.get(char_key)
        if prev is not None and prev.get("account") == account_id:
            return
        self._sidecar[char_key] = {"account": account_id, "seen": self._now_iso()}
        self._dirty = True

    def _now_iso(self) -> str:
        return datetime.fromtimestamp(self._clock(), timezone.utc).isoformat()

    # -- queries ------------------------------------------------------------
    def account_for_pid(self, pid: int) -> int | None:
        """Cached probe result for *pid* (``observe`` does the probing). Both
        "never probed" and "probed and missed" read as ``None``."""
        return self._pid_cache.get(pid)

    def account_for_char(self, char_key: str) -> int | None:
        """Resolve a character's account: live probe -> sidecar -> hint -> None
        (design §8, strictly-decreasing reliability)."""
        key = (char_key or "").strip().lower()
        if not key:
            return None
        if key in self._live_char_to_acct:
            return self._live_char_to_acct[key]
        entry = self._sidecar.get(key)
        if entry is not None:
            return entry.get("account")
        # Hint step: the injected hint answers per-account (id -> char name), so
        # we can only consult it for accounts already known via another source.
        for aid in self.known_accounts():
            h = self._hint(aid)
            if h and h.strip().lower() == key:
                return aid
        return None

    def char_for_account(self, account_id: int) -> str | None:
        """The live logged-in character key on *account_id*, else ``None`` (live
        clients only — a login screen has no character)."""
        return self._live_acct_to_char.get(account_id)

    def known_accounts(self) -> list[int]:
        """All account ids in play: union of live (login + logged-in), sidecar,
        and alias keys. Sorted; non-integer alias keys are ignored."""
        accts = set(self._live_accounts)
        for entry in self._sidecar.values():
            a = entry.get("account")
            if isinstance(a, int) and not isinstance(a, bool):
                accts.add(a)
        for k in self._aliases().keys():
            try:
                accts.add(int(k))
            except (ValueError, TypeError):
                continue
        return sorted(accts)

    def label_for_account(self, account_id: int) -> str:
        """Display label for an account: user alias -> mtime-hint char name ->
        ``"Account <id>"`` (design §2 / §9.5). A blank alias is ignored."""
        alias = self._aliases().get(str(account_id))
        if alias:
            norm = _normalize_label(alias)
            if norm:
                return norm
        h = self._hint(account_id)
        if h and h.strip():
            return h.strip()
        return f"Account {account_id}"

    def mismatches(self, manual_accounts: dict | None = None) -> dict[str, int]:
        """Characters whose manual grouping disagrees with observation.

        Consumed by Task 5. Given the manual ``{char_key: label}`` grouping,
        group chars by case-folded label; within any group whose members'
        *observed* accounts are not unanimous, return ``{char_key: observed}``
        for each member that has an observed account. Called with no argument
        (the plan's ``mismatches()`` shorthand) it is inert and returns ``{}`` —
        there is no manual grouping to contradict.
        """
        if not manual_accounts:
            return {}
        groups: dict[str, list[str]] = {}
        for char_key, label in manual_accounts.items():
            norm_label = _normalize_label(label).lower()
            if not norm_label:
                continue
            groups.setdefault(norm_label, []).append(str(char_key).strip().lower())
        out: dict[str, int] = {}
        for members in groups.values():
            observed = {}
            for ck in members:
                a = self.account_for_char(ck)
                if a is not None:
                    observed[ck] = a
            if len(set(observed.values())) > 1:
                out.update(observed)
        return out


_UNSET = object()  # sentinel: "this PID has never been probed" vs. "probed None"


# ---------------------------------------------------------------------------
# Production sidecar store (atomic)
# ---------------------------------------------------------------------------
class FileStore:
    """Sidecar IO for :class:`AccountMap` (injected as *store*). ``read`` returns
    the file text or ``None`` when absent/unreadable; ``write`` is atomic
    (temp file + ``os.replace``) so a crash mid-write never truncates the map."""

    def __init__(self, path: str):
        self._path = path

    def read(self) -> str | None:
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return None
        except OSError:
            return None

    def write(self, text: str) -> None:
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, self._path)


# ---------------------------------------------------------------------------
# Production launcher-log reader (Tier 1 insurance; behind the injectable seam,
# so untested-in-CI). Retains ONLY (float ts, int userId) — never raw text.
# ---------------------------------------------------------------------------
_LOG_USERID_RE = re.compile(r"userId['\"]?\s*[:=]\s*(\d+)")
_LOG_TS_RE = re.compile(
    r"(\d{4})[-/](\d{2})[-/](\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:[.,](\d{1,6}))?")


def _parse_log_line(line: str) -> tuple[float, int] | None:
    """One structured ``Started client`` line -> ``(utc_ts, userId)`` or ``None``.

    Retains nothing of the raw line but the two numbers. NOTE: launcher-log
    timestamps are treated as *local* time (``datetime.timestamp()`` on a naive
    value), matching ``process_create_time``'s UTC epoch when the log clock is
    local. This is the one calibration assumption of the insurance path (spec
    §16 item 2, open); Tier 0 covers all clients today so this rarely runs.
    """
    if "Started client" not in line:
        return None
    mu = _LOG_USERID_RE.search(line)
    if not mu:
        return None
    mt = _LOG_TS_RE.search(line)
    if not mt:
        return None
    try:
        year, month, day, hour, minute, second = (int(mt.group(i)) for i in range(1, 7))
        frac = mt.group(7)
        micro = int(frac.ljust(6, "0")[:6]) if frac else 0
        ts = datetime(year, month, day, hour, minute, second, micro).timestamp()
        uid = int(mu.group(1))
    except (ValueError, OverflowError, OSError):
        return None
    return (ts, uid)


def read_launcher_log_records(appdata: str | None = None,
                              now: float | None = None) -> list[tuple[float, int]]:
    """Tail ``%APPDATA%\\EVE Online\\logs\\eve-online-launcher-*.log`` for recent
    ``Started client`` records. Best-effort and defensive: any missing dir,
    unreadable file, or unparsable line is skipped. Returns only
    ``(float ts, int userId)`` tuples (design §12)."""
    base = appdata or os.environ.get("APPDATA", "")
    if not base:
        return []
    logdir = os.path.join(base, "EVE Online", "logs")
    now_ts = time.time() if now is None else now
    out: list[tuple[float, int]] = []
    try:
        paths = glob.glob(os.path.join(logdir, "eve-online-launcher-*.log"))
    except OSError:
        return []
    for path in paths:
        try:
            st = os.stat(path)
            if now_ts - st.st_mtime > _LOG_MAX_AGE_S:
                continue
            size = st.st_size
            with open(path, "rb") as f:
                if size > _LOG_TAIL_BYTES:
                    f.seek(size - _LOG_TAIL_BYTES)
                    f.readline()  # discard the partial line at the seek point
                raw = f.read()
            text = raw.decode("utf-8", "replace")
        except OSError:
            continue
        for line in text.splitlines():
            rec = _parse_log_line(line)
            if rec is not None:
                out.append(rec)
    return out


# ---------------------------------------------------------------------------
# Production win32 backend (ctypes; mirrors scratchpad/peb_probe.py, proven live
# 2026-08-25). Never used in tests — they inject a fake with the same surface.
# ---------------------------------------------------------------------------
# x64 PEB offsets (ProcessParameters, then RTL_USER_PROCESS_PARAMETERS.CommandLine).
_PEB_PROCESSPARAMETERS_OFF = 0x20
_RTLUPP_COMMANDLINE_OFF = 0x70
# 100-ns intervals between 1601-01-01 (FILETIME epoch) and 1970-01-01 (Unix epoch).
_FILETIME_TO_UNIX = 116444736000000000


class _PROCESS_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("Reserved1", ctypes.c_void_p),
        ("PebBaseAddress", ctypes.c_void_p),
        ("Reserved2", ctypes.c_void_p * 2),
        ("UniqueProcessId", ctypes.c_void_p),
        ("Reserved3", ctypes.c_void_p),
    ]


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ushort),
        ("MaximumLength", ctypes.c_ushort),
        ("Buffer", ctypes.c_void_p),
    ]


class _RealWin32:
    """Real ctypes backend: ``command_line`` (PEB read) + ``process_create_time``
    (``GetProcessTimes``). Instantiated lazily via :func:`real_win32`; never used
    in tests. ``command_line`` returns SECRET data — its only caller
    (``account_id_for_pid``) parses it to an int and drops it at once."""

    def __init__(self):
        import ctypes as _c
        from ctypes import wintypes

        self._ctypes = _c
        self._ntdll = _c.WinDLL("ntdll")
        self._kernel32 = _c.WinDLL("kernel32", use_last_error=True)

        self._ntdll.NtQueryInformationProcess.argtypes = [
            wintypes.HANDLE, _c.c_int, _c.c_void_p, _c.c_ulong,
            _c.POINTER(_c.c_ulong)]
        self._ntdll.NtQueryInformationProcess.restype = _c.c_long

        self._kernel32.OpenProcess.argtypes = [
            wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._kernel32.OpenProcess.restype = wintypes.HANDLE
        self._kernel32.ReadProcessMemory.argtypes = [
            wintypes.HANDLE, _c.c_void_p, _c.c_void_p, _c.c_size_t,
            _c.POINTER(_c.c_size_t)]
        self._kernel32.ReadProcessMemory.restype = wintypes.BOOL
        self._kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE, _c.POINTER(wintypes.FILETIME),
            _c.POINTER(wintypes.FILETIME), _c.POINTER(wintypes.FILETIME),
            _c.POINTER(wintypes.FILETIME)]
        self._kernel32.GetProcessTimes.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL

    def _rpm(self, handle, addr, size):
        ctypes = self._ctypes
        buf = (ctypes.c_char * size)()
        read = ctypes.c_size_t(0)
        ok = self._kernel32.ReadProcessMemory(
            handle, ctypes.c_void_p(addr), buf, size, ctypes.byref(read))
        if not ok:
            raise OSError(f"ReadProcessMemory failed err={ctypes.get_last_error()}")
        return bytes(buf[:read.value])

    def command_line(self, pid: int) -> str:
        """Raw command line of *pid* via the PEB (x64). SECRET — see class doc.
        Raises ``OSError`` on any failure (denied / process gone)."""
        ctypes = self._ctypes
        PROCESS_QUERY_INFORMATION = 0x0400
        PROCESS_VM_READ = 0x0010
        handle = self._kernel32.OpenProcess(
            PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not handle:
            raise OSError(f"OpenProcess failed err={ctypes.get_last_error()}")
        try:
            pbi = _PROCESS_BASIC_INFORMATION()
            status = self._ntdll.NtQueryInformationProcess(
                handle, 0, ctypes.byref(pbi), ctypes.sizeof(pbi), None)
            if status != 0:
                raise OSError(
                    f"NtQueryInformationProcess status=0x{status & 0xffffffff:08x}")
            peb = pbi.PebBaseAddress or 0
            if not peb:
                raise OSError("PEB base address unavailable")
            pp = int.from_bytes(
                self._rpm(handle, peb + _PEB_PROCESSPARAMETERS_OFF, 8), "little")
            us = _UNICODE_STRING.from_buffer_copy(
                self._rpm(handle, pp + _RTLUPP_COMMANDLINE_OFF,
                          ctypes.sizeof(_UNICODE_STRING)))
            if not us.Length or not us.Buffer:
                return ""
            raw = self._rpm(handle, us.Buffer, us.Length)
            return raw.decode("utf-16-le", "replace")
        finally:
            self._kernel32.CloseHandle(handle)

    def process_create_time(self, pid: int) -> float:
        """*pid*'s creation time as a UTC epoch (``GetProcessTimes``). Works
        under ``PROCESS_QUERY_LIMITED_INFORMATION`` even when a VM read is denied
        (the Tier 1 case). Raises ``OSError`` on failure."""
        ctypes = self._ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = self._kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            raise OSError(f"OpenProcess failed err={ctypes.get_last_error()}")
        try:
            creation = wintypes.FILETIME()
            dummy = wintypes.FILETIME()
            ok = self._kernel32.GetProcessTimes(
                handle, ctypes.byref(creation), ctypes.byref(dummy),
                ctypes.byref(dummy), ctypes.byref(dummy))
            if not ok:
                raise OSError(f"GetProcessTimes failed err={ctypes.get_last_error()}")
            ft = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            return (ft - _FILETIME_TO_UNIX) / 10_000_000.0
        finally:
            self._kernel32.CloseHandle(handle)


_REAL_WIN32_SINGLETON: "_RealWin32 | None" = None


def real_win32() -> "_RealWin32":
    """Lazily-created real backend singleton. Importing this module on a
    non-Windows / headless box never touches ``ctypes.WinDLL``; the fake win32
    the tests inject means this is only reached in production."""
    global _REAL_WIN32_SINGLETON
    if _REAL_WIN32_SINGLETON is None:
        if sys.platform != "win32":
            raise RuntimeError("eve_account real backend requires Windows")
        _REAL_WIN32_SINGLETON = _RealWin32()
    return _REAL_WIN32_SINGLETON
