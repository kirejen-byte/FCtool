r"""Windows/Wine side of the Linux X11 preview backend.

Two objects live here:

* :class:`HelperSupervisor` owns the helper process and the control channel:
  it binds a loopback listener, spawns a native Linux ``python3`` through
  Wine's ``fork_and_exec`` of a non-PE file, authenticates the helper's first
  ``hello`` line against a one-shot token, and then runs ONE daemon reader
  thread over the socket.  ``start()`` only binds the listener: the whole
  spawn/hello budget runs on a second daemon thread, so the UI thread that
  enables previews is never blocked and ``stop()`` can abort a start that is
  still waiting.  Everything the UI thread may read is the immutable
  :class:`HelperStatus` snapshot.
* :class:`X11ThumbBackend` is the duck type ``dwm_thumbs.Thumbnail`` consumes
  through ``TileWindow(dwm=...)``: ``register`` / ``unregister`` / ``update``
  / ``query_source_size``, with the same failure contract (``OSError`` on a
  source that cannot be bound, so the caller books the client as stranded).

Facts this module rests on (see the plan's section 0 - do not re-derive):

* Wine's ``CreateProcessW`` on a non-PE file fork/execs it, the child inherits
  the real Unix environment, argv is passed verbatim, and **stdio is closed
  for a GUI parent** - so pipes are unusable and the control channel is TCP on
  127.0.0.1 (shared with the child inside the pressure-vessel netns).
* That same call returns SUCCESS with a **zeroed** ``PROCESS_INFORMATION``
  (no pid, NULL handles), which makes ``subprocess.Popen`` raise
  ``[WinError 6] Invalid handle`` AFTER the child is already exec'd - so the
  spawner under Wine is :func:`wine_spawn`, not ``Popen``.
* No environment is passed or altered: the helper needs the Unix ``DISPLAY``
  and ``XAUTHORITY`` it inherits, and anything we injected would be the
  Windows-side (renamed) spelling.
* The helper's real kill path is EOF on the control socket; a Wine process
  handle for a Unix child is not trusted, so ``terminate()`` is best effort
  only.

Constraints: no UI toolkit import anywhere in this module (the reader thread
must never touch widgets), every log string ASCII (cp1252 console trap), and
every public entry point total enough that a dead helper degrades to stranded
tiles rather than a traceback.
"""
from __future__ import annotations

import ctypes
import os
import secrets
import select
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Optional, Tuple

import app_path
import wine_detect
import x11_thumbs_proto as proto
from app_log import get_logger
from app_version import APP_VERSION

log = get_logger(__name__)

#: How long one spawn attempt may take to produce a valid ``hello``.
HELLO_TIMEOUT_S = 8

#: Total spawn attempts allowed to ONE supervisor.  ``make_backend`` builds a
#: fresh supervisor every time previews are enabled, so this budget bounds a
#: single enable (a respawn storm), not the process lifetime.
MAX_SPAWNS_PER_SESSION = 3

#: Reply budget for ``attach`` (X round trips plus picture setup).
ATTACH_TIMEOUT_S = 3.0

#: Reply budget for an on-demand ``size`` query.
SIZE_TIMEOUT_S = 1.0

#: recv() slice, so stop() is observed promptly by the reader thread.
_RECV_TIMEOUT_S = 0.25

#: The one-liner Wine hands to the native interpreter.  The vendor zip is the
#: LAST argv entry precisely so this string can find it without parsing flags.
BOOTSTRAP = ("import sys; sys.path.insert(0, sys.argv[-1]); "
             "import helper; sys.exit(helper.main(sys.argv[1:]))")

#: Shown when the EVE client has no ``__wine_x11_whole_window`` property.
WAYLAND_MESSAGE = (
    "EVE client has no X11 window - Wine's Wayland driver is active "
    "(PROTON_ENABLE_WAYLAND). Previews need X11/Xwayland, the same "
    "requirement as EVE Preview Manager.")

#: Hard ceiling for build_report's output (the tester pastes it into a reply).
MAX_REPORT_BYTES = 4096

#: Per-section line caps inside the report.
MAX_REPORT_CLIENTS = 16
MAX_REPORT_PROBE = 16

STATES = ("idle", "starting", "ready", "failed", "stopped")


def _ascii(value, default="-"):
    """Collapse any value to a one-line ASCII string (cp1252 log trap)."""
    if value is None:
        return default
    try:
        text = str(value)
    except Exception:
        return default
    text = text.encode("ascii", "replace").decode("ascii")
    return text.replace("\r", " ").replace("\n", " ").strip() or default


# --------------------------------------------------------------- snapshot


@dataclass(frozen=True)
class HelperStatus:
    """Immutable view of the helper, the only thing the UI thread reads.

    A fresh object is built by every :meth:`HelperSupervisor.status_snapshot`
    call, so a caller can hold one across ticks and compare it safely.
    """

    state: str = "idle"
    python: Optional[str] = None          # the Unix interpreter we spawned
    display: Optional[str] = None         # DISPLAY the helper actually opened
    frames: int = 0
    damage_events: int = 0
    coalesced: int = 0
    errors: int = 0
    malformed: int = 0
    dropped: int = 0
    repairs: int = 0
    activations: int = 0
    uptime_s: float = 0.0
    last_error: Optional[str] = None
    spawn_attempts: int = 0
    #: One line per FAILED spawn attempt, ``"<python>: <error>"``, newest
    #: last, at most the final 3.  ``last_error`` is last-wins by design, so
    #: without this a three-candidate failure reaches the tester as a single
    #: line and the earlier interpreters' verdicts are lost.
    spawn_errors: Tuple[str, ...] = ()
    # hello fields (report section [helper])
    helper_python: Optional[str] = None   # the helper's sys.version
    xrender: Optional[Tuple[int, int]] = None
    damage: Optional[Tuple[int, int]] = None
    shape: Optional[bool] = None
    composite: Optional[bool] = None
    screen: Optional[Tuple[int, int, int]] = None   # (w, h, depth)
    #: Strings :func:`build_report` must scrub from its free-text fields -
    #: every one-shot auth token this supervisor has issued.  ``repr=False``
    #: keeps them out of any log line that formats the snapshot; nothing
    #: renders the field itself.
    redact: Tuple[str, ...] = field(default=(), repr=False)


# --------------------------------------------------------------- internals


class _Waiter(object):
    """One outstanding request/reply correlation slot."""

    __slots__ = ("reply_type", "event", "message")

    def __init__(self, reply_type):
        self.reply_type = reply_type
        self.event = threading.Event()
        self.message = None


def _default_listener():
    """A bound, listening loopback socket on an ephemeral port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
    except Exception:
        try:
            sock.close()
        except Exception:
            pass
        raise
    return sock


def _as_int(value):
    """``value`` as an int, or None - never raises.

    Every number crossing the wire (or coming back from ``x_window_of``) goes
    through here, because the backend's contract is OSError or nothing: a
    ValueError from ``int()`` would escape ``register`` uncaught.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _pair(value):
    """A 2-int tuple from a decoded JSON pair, or None."""
    try:
        if isinstance(value, (list, tuple)) and len(value) == 2:
            return (int(value[0]), int(value[1]))
    except Exception:
        pass
    return None


def python_candidates(facts):
    """Ordered Unix interpreters to try: the facts snapshot, else a probe.

    ONE owner, because ``make_backend`` must decide "there is nothing to
    spawn" exactly the way the supervisor would decide it.
    """
    pythons = getattr(facts, "pythons", None) if facts is not None else None
    if pythons:
        return list(pythons)
    try:
        return list(wine_detect.find_unix_python())
    except Exception:
        return []


def _screen(value):
    """A ``(w, h, depth)`` tuple from a hello screen object, or None."""
    try:
        if isinstance(value, dict):
            return (int(value.get("w", 0)), int(value.get("h", 0)),
                    int(value.get("depth", 0)))
    except Exception:
        pass
    return None


# ----------------------------------------------------------------- spawner


#: Spawn kwargs for the ``Popen`` branch ONLY: Wine closes a GUI parent's
#: stdio anyway, and an inherited pipe would wedge the child on its first
#: write.  :func:`wine_spawn` takes argv and nothing else.
_POPEN_KWARGS = {"stdin": subprocess.DEVNULL,
                 "stdout": subprocess.DEVNULL,
                 "stderr": subprocess.DEVNULL,
                 "close_fds": True}


class _STARTUPINFOW(ctypes.Structure):
    """Win32 ``STARTUPINFOW`` - only ``cb`` is ever set (no std handles)."""

    _fields_ = [("cb", ctypes.c_ulong),
                ("lpReserved", ctypes.c_wchar_p),
                ("lpDesktop", ctypes.c_wchar_p),
                ("lpTitle", ctypes.c_wchar_p),
                ("dwX", ctypes.c_ulong),
                ("dwY", ctypes.c_ulong),
                ("dwXSize", ctypes.c_ulong),
                ("dwYSize", ctypes.c_ulong),
                ("dwXCountChars", ctypes.c_ulong),
                ("dwYCountChars", ctypes.c_ulong),
                ("dwFillAttribute", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong),
                ("wShowWindow", ctypes.c_ushort),
                ("cbReserved2", ctypes.c_ushort),
                ("lpReserved2", ctypes.c_void_p),
                ("hStdInput", ctypes.c_void_p),
                ("hStdOutput", ctypes.c_void_p),
                ("hStdError", ctypes.c_void_p)]


class _PROCESS_INFORMATION(ctypes.Structure):
    """Win32 ``PROCESS_INFORMATION`` - Wine zeroes ALL of it for a Unix
    child (no pid, NULL handles)."""

    _fields_ = [("hProcess", ctypes.c_void_p),
                ("hThread", ctypes.c_void_p),
                ("dwProcessId", ctypes.c_ulong),
                ("dwThreadId", ctypes.c_ulong)]


class _UnixChild(object):
    """All Wine gives back for a fork/exec'd Unix child: a pid, at most.

    Deliberately NOT a ``Popen`` look-alike beyond the two calls the
    supervisor makes: there is no process handle to wait on, so every method
    answers honestly that it cannot know.  ``poll()`` returning None keeps
    the supervisor's contract ("assume it runs until the control channel says
    otherwise") - EOF on that channel is the real death signal, and
    ``terminate()`` was already best-effort insurance before this existed.
    """

    __slots__ = ("pid",)

    returncode = None

    def __init__(self, pid=None):
        self.pid = pid

    def poll(self):
        return None

    def wait(self, timeout=None):
        return None

    def terminate(self):
        return None

    def kill(self):
        return None


def _format_error(code):
    """The Win32 message for ``code``, ASCII and never raising."""
    try:
        return _ascii(ctypes.FormatError(code))
    except Exception:
        return "error %s" % (_ascii(code),)


def _close_handles(k32, handles):
    """Close each NON-NULL handle; Wine hands back NULLs for a Unix child.

    Closing a NULL handle is exactly the ``[WinError 6]`` that made ``Popen``
    unusable here, so a zero is skipped rather than closed-and-forgiven, and
    every real close is isolated so one failure cannot lose the other.
    """
    close = None
    for handle in handles:
        if not handle:
            continue
        if close is None:
            close = getattr(k32, "CloseHandle", None)
            if close is None:
                return
            try:
                close.argtypes = [ctypes.c_void_p]
                close.restype = ctypes.c_int
            except Exception:
                pass
        try:
            close(handle)
        except Exception:
            pass


def wine_spawn(argv, kernel32=None):
    """Start a native Unix program through Wine's ``CreateProcessW``.

    ``subprocess.Popen`` CANNOT do this.  For a non-PE executable Wine
    ``fork_and_exec``s the file and returns STATUS_SUCCESS with a ZEROED
    ``PROCESS_INFORMATION`` - no pid, NULL process and thread handles.
    CPython's ``Popen._execute_child`` then calls ``CloseHandle`` on that NULL
    thread handle, gets ``ERROR_INVALID_HANDLE`` and raises ``OSError``
    **after the child has already been exec'd**: the first Linux tester's
    three "[WinError 6] Invalid handle" attempts had each actually started a
    helper, and the supervisor moved on believing none of them had.

    So the call is made raw.  argv is rendered with ``list2cmdline`` because
    Wine parses the command line with Windows quoting rules before handing
    the pieces to the Unix child as its argv; no handles are inherited, no
    creation flags, no environment and no working directory are imposed (the
    helper needs the Unix env it inherits); and a returned handle is closed
    only when it is non-NULL.  ``OSError`` is raised only when CreateProcessW
    itself fails.  ``kernel32`` is injectable for tests and nothing is loaded
    at import time.
    """
    # CreateProcessW may modify lpCommandLine, so it must be a writable buffer.
    cmdline = ctypes.create_unicode_buffer(
        subprocess.list2cmdline([str(arg) for arg in argv]))
    k32 = kernel32
    if k32 is None:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = getattr(k32, "CreateProcessW", None)
    if create is None:
        raise OSError("kernel32 exports no CreateProcessW")
    create.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_int, ctypes.c_ulong,
                       ctypes.c_void_p, ctypes.c_wchar_p,
                       ctypes.POINTER(_STARTUPINFOW),
                       ctypes.POINTER(_PROCESS_INFORMATION)]
    create.restype = ctypes.c_int
    startup = _STARTUPINFOW()
    startup.cb = ctypes.sizeof(_STARTUPINFOW)
    info = _PROCESS_INFORMATION()
    ctypes.set_last_error(0)
    created = create(None, cmdline, None, None, False, 0, None, None,
                     ctypes.byref(startup), ctypes.byref(info))
    if not created:
        code = ctypes.get_last_error()
        raise OSError(code, "CreateProcessW failed: %s"
                      % (_format_error(code),))
    _close_handles(k32, (info.hProcess, info.hThread))
    return _UnixChild(info.dwProcessId or None)


# ------------------------------------------------------------- supervisor


class HelperSupervisor(object):
    """Owns the helper process, the control socket and the reader thread."""

    def __init__(self, spawn=None, listen_factory=None, facts=None,
                 bundle_dir=None, log=None):
        #: None means "decide at spawn time" (see :meth:`_default_spawn`):
        #: the Wine verdict is a DLL probe, which must not run at import.
        self._spawn = spawn
        self._listen_factory = listen_factory or _default_listener
        self._facts = facts
        self._bundle_dir = bundle_dir or app_path.bundle_dir
        self._log = log if log is not None else globals()["log"]

        self._lock = threading.Lock()          # guards every snapshot field
        self._send_lock = threading.Lock()     # serialises sendall
        self._state = "idle"
        self._python = None
        self._display = None
        self._frames = 0
        self._damage_events = 0
        self._coalesced = 0
        self._errors = 0
        self._malformed = 0
        self._dropped = 0
        self._repairs = 0
        self._activations = 0
        self._uptime_s = 0.0
        self._last_error = None
        self._spawn_attempts = 0
        self._spawn_errors = []                # one line per FAILED attempt
        self._hello = {}

        self._sock = None
        self._listener = None
        self._proc = None
        self._thread = None
        self._starter = None
        self._stop = threading.Event()
        self._settled = threading.Event()   # start attempt reached a verdict
        self._decoder = None
        self._pipelined = []                   # decoded behind the hello
        self._tokens = []                      # issued tokens, for scrubbing
        self._pending = {}                     # id -> [_Waiter]
        self._pending_typed = {}               # reply_type -> [_Waiter]
        self._size_cb = None

    # --- public surface ---------------------------------------------------

    def status_snapshot(self) -> HelperStatus:
        """A NEW immutable snapshot of everything worth reporting."""
        with self._lock:
            return HelperStatus(
                state=self._state,
                python=self._python,
                display=self._display,
                frames=self._frames,
                damage_events=self._damage_events,
                coalesced=self._coalesced,
                errors=self._errors,
                malformed=self._malformed,
                dropped=self._dropped,
                repairs=self._repairs,
                activations=self._activations,
                uptime_s=self._uptime_s,
                last_error=self._last_error,
                spawn_attempts=self._spawn_attempts,
                spawn_errors=tuple(self._spawn_errors),
                helper_python=self._hello.get("python"),
                xrender=_pair(self._hello.get("xrender")),
                damage=_pair(self._hello.get("damage")),
                shape=self._hello.get("shape"),
                composite=self._hello.get("composite"),
                screen=_screen(self._hello.get("screen")),
                redact=tuple(self._tokens),
            )

    @property
    def facts(self):
        """The Wine facts in force: those passed in, else what we collected.

        None until the starter thread's ``collect()`` lands (the caller's
        thread never pays for those filesystem probes).
        """
        with self._lock:
            return self._facts

    def note_error(self, message) -> None:
        """Record a caller-side failure in the snapshot (ASCII, last wins)."""
        self._note_error(message)

    def on_size(self, callback) -> None:
        """Register the (id, w, h) sink for helper size messages.

        The callback runs on the reader thread and must not touch widgets.
        """
        with self._lock:
            self._size_cb = callback

    def start(self) -> None:
        """Bind the control listener and start the helper OFF-thread.

        Returns immediately - the caller is the UI thread, which must never
        pay a spawn budget.  Only the listener bind happens here; the
        spawn/hello loop runs on the ``x11-thumbs-starter`` daemon thread and
        settles the state at ``ready`` or ``failed``.  Never raises, and is a
        no-op while ``starting`` or ``ready``.
        """
        with self._lock:
            if self._state in ("starting", "ready"):
                return
            self._state = "starting"
            self._pipelined = []
            self._spawn_errors = []      # a fresh cycle, not stale attempts
        self._stop.clear()
        self._settled.clear()
        try:
            self._listener = self._listen_factory()
        except Exception as exc:
            self._fail("cannot bind the control listener: %s" % (_ascii(exc),))
            return
        thread = threading.Thread(target=self._start_thread,
                                  name="x11-thumbs-starter", daemon=True)
        with self._lock:
            self._starter = thread
        thread.start()

    def wait_ready(self, timeout_s) -> bool:
        """Block until the start attempt settles; True iff it reached ready.

        For tests and the diagnostic only: ``register`` never waits, it reads
        the snapshot and gives up immediately.
        """
        try:
            self._settled.wait(timeout_s)
        except Exception:
            pass
        return self.status_snapshot().state == "ready"

    def stop(self) -> None:
        """End the helper and every thread.  Valid in EVERY state.

        A starter thread sitting in its hello wait is aborted through the stop
        event (both of its waits are sliced), so this returns promptly instead
        of paying out the remaining spawn budget.
        """
        self._stop.set()
        self._settled.set()
        self._close_listener()          # nothing new may connect
        self._teardown_channel()
        starter = self._starter
        if starter is not None and starter.is_alive() \
                and starter is not threading.current_thread():
            try:
                starter.join(timeout=2)
            except Exception:
                pass
        # The starter may have committed a socket in the same instant we set
        # the stop event; tear the channel down again now that it is joined.
        self._teardown_channel()
        self._close_listener()
        self._release_waiters()
        with self._lock:
            self._state = "stopped"
            self._sock = None
            self._thread = None
            self._starter = None
            self._proc = None

    def _teardown_channel(self):
        """Quit + close the control socket, kill the child, join the reader.

        The socket is popped out of ``self._sock`` atomically before it is
        touched: ``stop()`` calls this twice (the starter may commit a
        socket in the same instant ``stop`` fires), and a second call must
        see nothing to tear down rather than re-sending ``quit`` on the
        socket the first call already closed.
        """
        with self._lock:
            sock = self._sock
            self._sock = None
        if sock is not None:
            try:
                self._write_line(sock, proto.encode({"type": proto.T_QUIT}))
            except Exception:
                pass
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass
        proc = self._proc
        if proc is not None:
            # Wine's handle for a Unix child is not trusted; EOF above is the
            # real kill path.  This is insurance, and must never raise.
            try:
                proc.terminate()
            except Exception:
                pass
        thread = self._thread
        if thread is not None and thread.is_alive() \
                and thread is not threading.current_thread():
            try:
                thread.join(timeout=2)
            except Exception:
                pass

    def send(self, msg) -> None:
        """Encode and write one message.  Never raises; drops are counted.

        Guaranteed ATOMIC: one line is written under ``_send_lock`` on a
        socket that is in BLOCKING mode for its whole committed life (the
        reader waits with ``select`` and never calls ``settimeout``), so
        ``sendall`` either writes the entire line or fails - a partial JSON
        object can never reach the helper.  Before that write, a bounded
        ``select`` for write-readiness (``_RECV_TIMEOUT_S``) means a stalled
        helper - a full send buffer - can never block the caller (often the
        UI thread) indefinitely: not writable within the budget is counted
        as a drop, same as any other failure, and ``sendall`` is never
        called in that case.  A failed write tears the channel down rather
        than leaving a torn stream behind it.
        """
        sock = self._sock
        if sock is None:
            self._count_drop()
            return
        try:
            line = proto.encode(msg)
        except proto.ProtoError as exc:
            self._note_error("cannot encode %s: %s"
                             % (_ascii(msg.get("type") if isinstance(msg, dict)
                                       else msg), _ascii(exc)))
            return
        try:
            if not self._write_line(sock, line):
                self._count_drop()
                self._note_error("send would block")
                return
        except Exception as exc:
            self._count_drop()
            self._note_error("send failed: %s" % (_ascii(exc),))
            self._abandon_channel(sock)

    def _write_line(self, sock, line):
        """Write one already-encoded line if ``sock`` is writable in time.

        Returns True on a completed ``sendall``, False when the write
        budget (``_RECV_TIMEOUT_S``) elapsed with the socket still not
        writable - not a socket error, just a caller that must not block.
        Anything ``select`` or ``sendall`` raises propagates: that IS a
        socket error, for the caller to count and act on.
        """
        with self._send_lock:
            _r, writable, _x = select.select([], [sock], [],
                                             _RECV_TIMEOUT_S)
            if not writable:
                return False
            sock.sendall(line)
            return True

    def _abandon_channel(self, sock):
        """Drop a torn control channel: nothing may write to it again.

        Closing it also wakes the reader, whose EOF/error path is the single
        owner of the "the helper is gone" verdict.  A concurrent ``stop()``
        owns the terminal state, so the failure is recorded only outside one.
        """
        with self._lock:
            if self._sock is sock:
                self._sock = None
            if not self._stop.is_set() and self._state == "ready":
                self._state = "failed"
        try:
            sock.close()
        except Exception:
            pass

    def request(self, msg, reply_type, timeout_s):
        """Send ``msg`` and wait for its reply.

        Correlated by ``id`` when the message carries one, else by reply type
        alone (``probe``).  Returns the matching reply, an ``error`` message
        carrying the same id, or None on timeout.
        """
        waiter = _Waiter(reply_type)
        key = msg.get("id") if isinstance(msg, dict) else None
        with self._lock:
            if isinstance(key, int) and not isinstance(key, bool):
                self._pending.setdefault(key, []).append(waiter)
            else:
                key = None
                self._pending_typed.setdefault(reply_type, []).append(waiter)
        try:
            self.send(msg)
            waiter.event.wait(timeout_s)
            return waiter.message
        finally:
            with self._lock:
                table = (self._pending if key is not None
                         else self._pending_typed)
                slot = key if key is not None else reply_type
                bucket = table.get(slot)
                if bucket and waiter in bucket:
                    bucket.remove(waiter)
                if bucket is not None and not bucket:
                    table.pop(slot, None)

    # --- start helpers ----------------------------------------------------

    def _start_thread(self) -> None:
        """The whole spawn/hello budget, off the caller's thread."""
        try:
            self._start_inner()
        except Exception as exc:          # pragma: no cover - belt and braces
            self._fail("helper start crashed: %s" % (_ascii(exc),))
        finally:
            self._settled.set()

    def _start_inner(self) -> None:
        vendor = self._vendor_zip_unix()
        if not vendor:
            self._fail("cannot resolve the Unix path of vendor/x11helper.zip "
                       "(not running under Wine?)")
            return
        candidates = self._python_candidates()
        if not candidates:
            self._fail("no Unix python3 interpreter found in the prefix")
            return
        listener = self._listener
        try:
            port = listener.getsockname()[1]
        except Exception as exc:
            self._fail("the control listener is gone: %s" % (_ascii(exc),))
            return
        for candidate in candidates:
            if self._stop.is_set():
                return
            with self._lock:
                if self._spawn_attempts >= MAX_SPAWNS_PER_SESSION:
                    attempts_left = False
                else:
                    self._spawn_attempts += 1
                    attempts_left = True
            if not attempts_left:
                break
            if self._try_candidate(candidate, port, vendor):
                return
        if self._stop.is_set():
            return
        self._close_listener()
        with self._lock:
            failed_for = self._last_error
            self._state = "failed"
        self._log.warning("[x11] helper failed to start: %s",
                          _ascii(failed_for))

    def _try_candidate(self, python, port, vendor) -> bool:
        token = secrets.token_hex(16)
        with self._lock:
            self._tokens.append(token)
            del self._tokens[:-8]
        argv = [python, "-c", BOOTSTRAP,
                "--host", "127.0.0.1",
                "--port", str(port),
                "--token", token,
                "--vendor", vendor]
        spawn = self._spawn
        if spawn is None:
            spawn = self._default_spawn()
        # wine_spawn takes argv and nothing else; only Popen gets the
        # no-env/no-stdio kwargs (Wine closes a GUI parent's handles anyway,
        # and an inherited pipe would wedge the child on its first write).
        kwargs = {} if spawn is wine_spawn else dict(_POPEN_KWARGS)
        try:
            proc = spawn(argv, **kwargs)
        except Exception as exc:
            self._note_error("spawn %s failed: %s"
                             % (_ascii(python), _ascii(exc)))
            self._note_attempt(python)
            return False
        conn, decoder, hello, backlog = self._await_hello(token)
        if hello is None or self._stop.is_set():
            self._kill(proc, conn)
            self._note_attempt(python)
            return False
        try:
            # Blocking for the rest of its life: see send().  The reader waits
            # with select, so nothing ever mutates this timeout again.
            conn.settimeout(None)
        except Exception as exc:
            self._note_error("cannot switch the control socket to blocking: %s"
                             % (_ascii(exc),))
            self._kill(proc, conn)
            self._note_attempt(python)
            return False
        with self._lock:
            if self._stop.is_set():      # a stop() landed in this instant
                stopping = True
            else:
                stopping = False
                self._sock = conn
                self._proc = proc
                self._decoder = decoder
                self._pipelined = list(backlog or ())
                self._python = python
                self._hello = dict(hello)
                self._display = hello.get("display")
                self._state = "ready"
                self._last_error = None
        if stopping:
            self._kill(proc, conn)
            return False
        self._close_listener()
        self.send({"type": proto.T_OK})
        self._thread = threading.Thread(target=self._reader_loop,
                                        name="x11-thumbs-reader", daemon=True)
        self._thread.start()
        self._settled.set()
        self._log.info("[x11] helper ready via %s (display=%s)",
                       _ascii(python), _ascii(hello.get("display")))
        return True

    def _accept(self, deadline):
        """Wait for the helper's connection in stop-observing slices.

        The slices are what makes ``stop()`` prompt: a single ``accept`` with
        the whole budget could not be interrupted from another thread without
        closing the socket underneath it.
        """
        listener = self._listener
        if listener is None:
            self._note_error("helper did not connect: listener already closed")
            return None
        while True:
            if self._stop.is_set():
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._note_error("helper did not connect within %ss"
                                 % (HELLO_TIMEOUT_S,))
                return None
            try:
                listener.settimeout(min(remaining, _RECV_TIMEOUT_S))
                conn, _addr = listener.accept()
                return conn
            except socket.timeout:
                continue
            except Exception as exc:
                self._note_error("helper did not connect: %s" % (_ascii(exc),))
                return None

    def _await_hello(self, token):
        """Accept one connection and read its first line within the budget.

        Returns ``(conn, decoder, hello, backlog)``.  NOTHING the helper sent
        is discarded: the decoder (and so its residual partial line) is handed
        to the reader thread, and any message the same TCP segment carried
        BEHIND the hello comes back as ``backlog`` for the reader to dispatch
        before it touches the socket.
        """
        deadline = time.monotonic() + float(HELLO_TIMEOUT_S)
        conn = self._accept(deadline)
        if conn is None:
            return None, None, None, ()
        decoder = proto.LineDecoder()
        try:
            while True:
                if self._stop.is_set():
                    return conn, None, None, ()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._note_error("helper sent no hello within %ss"
                                     % (HELLO_TIMEOUT_S,))
                    return conn, None, None, ()
                conn.settimeout(min(remaining, _RECV_TIMEOUT_S))
                try:
                    data = conn.recv(65536)
                except socket.timeout:
                    # Let the deadline test above own the message: "no hello"
                    # is the fact the tester needs, not "timed out".
                    continue
                if not data:
                    self._note_error("helper closed before hello")
                    return conn, None, None, ()
                messages = decoder.feed(data)
                for index, msg in enumerate(messages):
                    problem = self._check_hello(msg, token)
                    if problem:
                        self._note_error(problem)
                        return conn, None, None, ()
                    return conn, decoder, msg, messages[index + 1:]
        except Exception as exc:
            self._note_error("hello read failed: %s" % (_ascii(exc),))
            return conn, None, None, ()

    @staticmethod
    def _check_hello(msg, token):
        """None when ``msg`` is a hello bearing our token, else the problem."""
        if msg.get("type") != proto.T_HELLO:
            return ("first helper line was '%s', not hello"
                    % (_ascii(msg.get("type")),))
        problem = proto.validate(msg)
        if problem:
            return "bad hello: %s" % (_ascii(problem),)
        if msg.get("token") != token:
            return "helper presented the wrong token"
        return None

    def _kill(self, proc, conn):
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        if proc is None:
            return
        try:
            proc.terminate()
        except Exception:
            pass

    def _default_spawn(self):
        """The spawner to use when the caller injected none.

        Resolved HERE, on the starter thread at spawn time, and never at
        import: the verdict is a DLL probe, and the answer differs per host.
        Under Wine the child is a native Unix binary, which only
        :func:`wine_spawn` can start (see its docstring); everywhere else -
        the Windows test rig included - plain ``Popen`` is correct.
        """
        try:
            wine = bool(wine_detect.is_wine())
        except Exception:
            wine = False
        return wine_spawn if wine else subprocess.Popen

    def _note_attempt(self, python):
        """Keep this attempt's verdict, so the report shows ALL of them.

        ``last_error`` is last-wins by design, so three candidates failing
        three different ways would otherwise reach the tester as one line.
        A stop() in flight is not a failure and records nothing.
        """
        if self._stop.is_set():
            return
        with self._lock:
            self._spawn_errors.append("%s: %s" % (_ascii(python),
                                                  _ascii(self._last_error)))
            del self._spawn_errors[:-3]

    def _vendor_zip_unix(self):
        base = self._bundle_dir
        try:
            base = base() if callable(base) else base
        except Exception:
            return None
        if not base:
            return None
        win_path = os.path.join(base, "vendor", "x11helper.zip")
        try:
            return wine_detect.unix_path(win_path)
        except Exception:
            return None

    def _python_candidates(self):
        """The interpreters to try, collecting the facts here if need be.

        ``make_backend`` hands us ``facts=None`` precisely so the filesystem
        probes inside ``wine_detect.collect`` run on the starter thread and
        not on the UI thread that enabled previews.
        """
        facts = self._facts
        if facts is None:
            try:
                facts = wine_detect.collect()
            except Exception as exc:
                self._note_error("cannot collect the Wine facts: %s"
                                 % (_ascii(exc),))
                facts = None
            with self._lock:
                self._facts = facts
        return python_candidates(facts)

    def _close_listener(self):
        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                listener.close()
            except Exception:
                pass

    def _fail(self, message):
        self._note_error(message)
        self._close_listener()
        with self._lock:
            if not self._stop.is_set():   # a concurrent stop() owns the state
                self._state = "failed"
        self._settled.set()
        self._log.warning("[x11] %s", _ascii(message))

    def _note_error(self, message):
        with self._lock:
            self._last_error = _ascii(message)

    def _count_drop(self):
        with self._lock:
            self._dropped += 1

    # --- reader thread ----------------------------------------------------

    def _reader_loop(self):
        """Drain the control socket until EOF or stop().  Never touches UI.

        Readability is waited for with ``select``, never ``settimeout``: the
        socket must stay blocking so a concurrent ``send`` is atomic.  A loop
        that ends for any reason OTHER than ``stop()`` means the helper died,
        and the state settles at ``failed`` - the report has to distinguish a
        crash from a clean shutdown.
        """
        sock = self._sock
        decoder = self._decoder or proto.LineDecoder()
        with self._lock:
            backlog = self._pipelined
            self._pipelined = []
        self._dispatch_all(backlog)          # pipelined behind the hello
        died = False
        while not self._stop.is_set():
            try:
                readable, _w, _x = select.select([sock], [], [],
                                                 _RECV_TIMEOUT_S)
                if not readable:
                    continue
                data = sock.recv(65536)
            except Exception as exc:
                died = True
                # A stop() in progress owns the narrative - a socket it is
                # in the middle of closing raises here for reasons that have
                # nothing to do with the helper, so a clean shutdown must
                # never look like a crash in last_error.
                if not self._stop.is_set() and self._sock is not None:
                    self._note_error("control channel read failed: %s"
                                     % (_ascii(exc),))
                break
            if not data:
                died = True
                if not self._stop.is_set():
                    self._note_error("helper closed the control channel (EOF)")
                break
            try:
                messages = decoder.feed(data)
            except proto.ProtoError as exc:
                died = True
                self._note_error("control stream desynced: %s" % (_ascii(exc),))
                break
            self._dispatch_all(messages)
        # Settle the verdict BEFORE waking the waiters, so every caller that
        # wakes on a dead helper already reads the dead state.
        with self._lock:
            if died and not self._stop.is_set():
                if self._state == "ready":
                    self._state = "failed"
            elif self._state == "ready":
                self._state = "stopped"
        self._release_waiters()

    def _dispatch_all(self, messages):
        for msg in messages or ():
            try:
                self._dispatch(msg)
            except Exception as exc:       # one bad message never kills us
                self._note_error("dispatch failed: %s" % (_ascii(exc),))

    def _dispatch(self, msg):
        mtype = msg.get("type")
        if mtype == proto.T_MALFORMED:
            with self._lock:
                self._malformed += 1
            return
        if mtype == proto.T_STATS:
            self._apply_stats(msg)
            return
        if mtype == proto.T_ERROR:
            self._apply_error(msg)
        elif mtype == proto.T_SIZE:
            self._apply_size(msg)
        self._resolve(msg)

    def _apply_stats(self, msg):
        with self._lock:
            for key, attr in (("frames", "_frames"),
                              ("damage_events", "_damage_events"),
                              ("coalesced", "_coalesced"),
                              ("errors", "_errors")):
                value = msg.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    setattr(self, attr, value)
            for key, attr in (("repairs", "_repairs"),
                              ("activations", "_activations")):
                total = _as_int(msg.get(key))
                if total is not None:
                    setattr(self, attr, total)
            uptime = msg.get("uptime_s")
            if isinstance(uptime, (int, float)) and not isinstance(uptime, bool):
                self._uptime_s = float(uptime)

    def _apply_error(self, msg):
        code = msg.get("code")
        text = "%s: %s" % (_ascii(code), _ascii(msg.get("msg")))
        fatal = msg.get("id") is None and isinstance(code, str) \
            and code.startswith("no_")
        with self._lock:
            self._last_error = text
            self._errors += 1
            if fatal:
                self._state = "failed"
        if fatal:
            self._log.warning("[x11] helper reported a fatal condition: %s",
                              text)

    def _apply_size(self, msg):
        with self._lock:
            callback = self._size_cb
        if callback is None:
            return
        try:
            callback(msg.get("id"), msg.get("w"), msg.get("h"))
        except Exception as exc:
            self._note_error("size callback failed: %s" % (_ascii(exc),))

    def _resolve(self, msg):
        """Hand ``msg`` to the first waiter expecting it (id, then type)."""
        mtype = msg.get("type")
        target = None
        with self._lock:
            mid = msg.get("id")
            if isinstance(mid, int) and not isinstance(mid, bool):
                for waiter in self._pending.get(mid, ()):
                    if waiter.reply_type == mtype or mtype == proto.T_ERROR:
                        target = waiter
                        break
            if target is None:
                for waiter in self._pending_typed.get(mtype, ()):
                    target = waiter
                    break
        if target is not None:
            target.message = msg
            target.event.set()

    def _release_waiters(self):
        with self._lock:
            buckets = list(self._pending.values()) + \
                list(self._pending_typed.values())
            self._pending = {}
            self._pending_typed = {}
        for bucket in buckets:
            for waiter in bucket:
                waiter.event.set()


# ----------------------------------------------------------------- backend


class X11ThumbBackend(object):
    """The ``dwm=`` duck type, backed by the native X11 helper."""

    def __init__(self, supervisor, x_window_of=None, min_interval_ms=33,
                 heartbeat_ms=500):
        self.supervisor = supervisor
        self._x_window_of = (x_window_of if x_window_of is not None
                             else wine_detect.x_window_of)
        self._min_interval_ms = int(min_interval_ms)
        self._heartbeat_ms = int(heartbeat_ms)
        self._lock = threading.Lock()
        self._next_id = 1
        self._sizes = {}        # handle -> (w, h)
        self._rects = {}        # handle -> last rect sent
        self._visible = {}      # handle -> last visibility sent
        try:
            supervisor.on_size(self._on_size)
        except Exception:
            pass

    # --- duck type --------------------------------------------------------

    @property
    def ready(self) -> bool:
        """True only while the supervisor holds a live, authenticated helper.

        The UI gate: tiles ask this before binding anything, and a backend
        that is still ``starting`` simply has no thumbnails yet.
        """
        return self._helper_state() == "ready"

    def register(self, dest_hwnd: int, src_hwnd: int) -> int:
        """Bind one EVE window into one tile; raises OSError when it cannot.

        OSError is the WHOLE failure contract - ``_preview_spawn_tile`` books
        the client as stranded on OSError and lets anything else crash the
        tick - so every value that crosses the wire is converted defensively
        and every reply is validated before it is read.  A helper that is not
        ``ready`` fails HERE, before any send or wait, so a tick never stalls
        on the attach budget while the helper is still starting.
        """
        state = self._helper_state()
        if state != "ready":
            raise OSError("helper not ready: %s" % (_ascii(state),))
        src_x = _as_int(self.x_id_for(src_hwnd))
        if not src_x:
            self._note(WAYLAND_MESSAGE)
            raise OSError(WAYLAND_MESSAGE)
        dst_x = _as_int(self.x_id_for(dest_hwnd))
        if not dst_x:
            message = ("preview tile hwnd %s has no X11 window"
                       % (_ascii(dest_hwnd),))
            self._note(message)
            raise OSError(message)
        with self._lock:
            handle = self._next_id
            self._next_id += 1
        reply = self._request(
            {"type": proto.T_ATTACH, "id": handle, "src": src_x,
             "dst": dst_x, "rect": [0, 0, 1, 1],
             "min_interval_ms": self._min_interval_ms,
             "heartbeat_ms": self._heartbeat_ms},
            proto.T_ATTACHED, ATTACH_TIMEOUT_S)
        if reply is None:
            # A late helper would hold a thumbnail nobody owns any more.
            self._drop_stray(handle)
            raise OSError("helper did not answer attach for src %s"
                          % (_ascii(src_x),))
        if reply.get("type") != proto.T_ATTACHED:
            raise OSError("helper refused attach for src %s: %s"
                          % (_ascii(src_x), _ascii(reply.get("msg"))))
        problem = proto.validate(reply)
        width, height = _as_int(reply.get("src_w")), _as_int(reply.get("src_h"))
        if problem is not None or width is None or height is None:
            self._drop_stray(handle)        # it DID attach - undo that
            raise OSError("malformed attached reply for src %s: %s"
                          % (_ascii(src_x), _ascii(problem, "no source size")))
        with self._lock:
            self._sizes[handle] = (width, height)
            self._rects[handle] = None
            self._visible[handle] = None
        return handle

    def _drop_stray(self, handle):
        """Best-effort detach of a handle we are about to forget.

        Skipped once the helper is gone: there is no stray thumbnail then, and
        a doomed send would only overwrite the last_error that explains why.
        """
        if self._helper_state() == "ready":
            self._send({"type": proto.T_DETACH, "id": handle})

    def update(self, handle, rect, visible=True, opacity=255,
               client_only=True):
        """Move/show the child window.  Zero-write on unchanged values.

        ``opacity`` and ``client_only`` have no X11 equivalent in this design
        (the helper composites SRC at full opacity over the whole window) and
        are accepted only to keep the DWM signature.
        """
        try:
            new_rect = [int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3])]
        except Exception:
            return
        visible = bool(visible)
        with self._lock:
            if handle not in self._rects:
                return
            rect_changed = self._rects.get(handle) != new_rect
            vis_changed = self._visible.get(handle) != visible
            if rect_changed:
                self._rects[handle] = new_rect
            if vis_changed:
                self._visible[handle] = visible
        if rect_changed and new_rect[2] > new_rect[0] \
                and new_rect[3] > new_rect[1]:
            self._send({"type": proto.T_UPDATE, "id": handle,
                        "rect": new_rect})
        if vis_changed:
            self._send({"type": proto.T_VISIBLE, "id": handle, "on": visible})

    def query_source_size(self, handle):
        """The source's (w, h): cached, else asked for, else last known.

        Total: a dead helper, a timeout or a garbage reply all degrade to the
        last known size (or ``(0, 0)``), never to an exception.
        """
        with self._lock:
            cached = self._sizes.get(handle)
        if cached is not None:
            return cached
        if self._helper_state() != "ready":
            # A dead/starting helper will never answer; asking would just
            # burn the whole SIZE_TIMEOUT_S for an unknown handle.
            return (0, 0)
        reply = self._request({"type": proto.T_SIZE, "id": handle},
                              proto.T_SIZE, SIZE_TIMEOUT_S)
        if reply is not None and reply.get("type") == proto.T_SIZE:
            width, height = reply.get("w"), reply.get("h")
            if isinstance(width, int) and not isinstance(width, bool) \
                    and isinstance(height, int) \
                    and not isinstance(height, bool):
                with self._lock:
                    self._sizes[handle] = (width, height)
                return (width, height)
        with self._lock:
            return self._sizes.get(handle) or (0, 0)

    def unregister(self, handle) -> None:
        """Detach one thumbnail and forget it.  Never raises."""
        with self._lock:
            known = handle in self._rects or handle in self._sizes
            self._sizes.pop(handle, None)
            self._rects.pop(handle, None)
            self._visible.pop(handle, None)
        if known:
            self._send({"type": proto.T_DETACH, "id": handle})

    def activate(self, src_hwnd) -> bool:
        """Ask the helper to focus one EVE client.  Fire-and-forget, total.

        The X11 route exists because Wine denies a plain SetForegroundWindow
        from a process whose input ``user_time`` is older than the foreground's
        - which is every HOTKEY-driven swap, since WM_HOTKEY never bumps that
        clock.  The helper instead sends ``_NET_ACTIVE_WINDOW`` as a pager on
        its own X connection and Wine's FocusIn handler does the rest.

        Returns True when the message was HANDED to the supervisor, never that
        the window is now focused (there is no reply and no confirmation:
        ``GetForegroundWindow`` under Wine is stale right after a switch).
        False means nothing was sent: the helper is not ready, or the hwnd has
        no X window (Wayland / a window that just died).
        """
        if not self.ready:
            return False
        src_x = _as_int(self.x_id_for(src_hwnd))
        if not src_x:
            return False
        self._send({"type": proto.T_ACTIVATE, "src": src_x})
        return True

    def close(self) -> None:
        """Stop the helper (idempotent)."""
        try:
            self.supervisor.stop()
        except Exception as exc:
            log.warning("[x11] helper stop failed: %s", _ascii(exc))

    # --- helpers ----------------------------------------------------------

    def _helper_state(self):
        """The supervisor's state, or ``unknown`` when it cannot be read."""
        try:
            return self.supervisor.status_snapshot().state
        except Exception:
            return "unknown"

    def _send(self, msg):
        """Fire one message at the supervisor.  Total, like ``send`` itself."""
        try:
            self.supervisor.send(msg)
        except Exception as exc:          # a duck-typed supervisor may raise
            log.warning("[x11] send %s failed: %s",
                        _ascii(msg.get("type")), _ascii(exc))

    def _request(self, msg, reply_type, timeout_s):
        """``supervisor.request``, degraded to None instead of an exception."""
        try:
            reply = self.supervisor.request(msg, reply_type, timeout_s)
        except Exception as exc:
            log.warning("[x11] request %s failed: %s",
                        _ascii(msg.get("type")), _ascii(exc))
            return None
        return reply if isinstance(reply, dict) else None

    def x_id_for(self, hwnd):
        """The X window id behind a HWND, or None (total)."""
        if not hwnd:
            return None
        try:
            return self._x_window_of(hwnd)
        except Exception:
            return None

    def _on_size(self, handle, width, height):
        """Reader-thread sink for unsolicited size pushes."""
        width, height = _as_int(width), _as_int(height)
        if width is None or height is None:
            return
        with self._lock:
            if handle in self._rects or handle in self._sizes:
                self._sizes[handle] = (width, height)

    def _note(self, message):
        try:
            self.supervisor.note_error(message)
        except Exception:
            pass


# ------------------------------------------------------------- factories


def wine_activator(backend):
    """Return the ``hwnd -> bool`` callable ``window_activator`` expects.

    Injection, not an import: ``window_activator`` must not learn about the
    X11 helper (it stays the ONE module allowed to touch a client window, and
    it must keep importing clean on real Windows).  The caller registers the
    result with ``window_activator.set_wine_activator`` and clears it with
    ``set_wine_activator(None)`` when the backend goes away.

    Total by contract: a dead/duck-typed backend reads as False (not sent),
    never as an exception on the UI thread.
    """
    def _activate(hwnd):
        try:
            return bool(backend.activate(hwnd))
        except Exception as exc:
            log.warning("[x11] activate failed: %s", _ascii(exc))
            return False

    return _activate


def make_backend(cfg, log=None):
    """Build the Wine-side backend and start the helper OFF-thread.

    ``cfg`` is the materialised preview config; only ``linux_fps_cap`` and
    ``linux_heartbeat_ms`` are read.  Returns None ONLY when this is not a
    Wine process - the one verdict that costs nothing - and then nothing at
    all was started.  Otherwise the backend comes back immediately with its
    supervisor still ``starting``: the caller gates real work on
    ``backend.ready``, which is what keeps the UI thread out of the spawn
    budget.  The interpreter hunt (``wine_detect.collect``, which stats the
    prefix) belongs to that same off-thread budget, so ``facts=None`` goes in
    and the starter collects; a prefix with no Unix ``python3`` settles at
    ``failed`` with the reason in the snapshot, which is also what the
    diagnostic report needs to say.  Never raises.
    """
    logger = log if log is not None else globals()["log"]
    try:
        wine = bool(wine_detect.is_wine())
    except Exception:
        wine = False
    if not wine:
        logger.info("[x11] not a Wine process - native previews stay on DWM")
        return None
    try:
        cap = int(cfg.get("linux_fps_cap", 30) or 30)
    except Exception:
        cap = 30
    cap = max(1, min(240, cap))
    try:
        heartbeat = int(cfg.get("linux_heartbeat_ms", 500) or 500)
    except Exception:
        heartbeat = 500
    heartbeat = max(50, heartbeat)
    logger.info("[x11] starting helper (fps_cap=%s heartbeat_ms=%s)",
                _ascii(cap), _ascii(heartbeat))
    supervisor = HelperSupervisor(facts=None, log=logger)
    supervisor.start()
    return X11ThumbBackend(supervisor, min_interval_ms=max(1, 1000 // cap),
                           heartbeat_ms=heartbeat)


def probe_clients(backend, clients):
    """Map EVE clients to report rows ``{hwnd, title, x_id}``.

    ``clients`` may be HWNDs or ``ClientWindow`` records.  Split out of
    :func:`run_probe` so the diagnostic still names every window (and its
    missing X id) when the probe itself times out.
    """
    entries = []
    for client in clients or ():
        hwnd = client if isinstance(client, int) else getattr(client, "hwnd",
                                                              None)
        title = "" if isinstance(client, int) else getattr(client, "title", "")
        x_id = backend.x_id_for(hwnd) if backend is not None else None
        entries.append({"hwnd": hwnd, "title": title, "x_id": x_id})
    return entries


def run_probe(backend, clients, seconds=5):
    """Ask the helper to measure each client's damage/composite behaviour.

    Returns the ``probe_result`` payload with a ``clients`` list merged in so
    the report can name each window, or ``{}`` when the helper does not
    answer.
    """
    if backend is None:
        return {}
    entries = probe_clients(backend, clients)
    src_list = [int(e["x_id"]) for e in entries if e["x_id"]]
    seconds = max(1, int(seconds or 1))
    reply = backend.supervisor.request(
        {"type": proto.T_PROBE, "src_list": src_list, "seconds": seconds},
        proto.T_PROBE_RESULT, seconds + 5.0)
    if reply is None or reply.get("type") != proto.T_PROBE_RESULT:
        return {}
    result = dict(reply)
    result["clients"] = entries
    return result


# ---------------------------------------------------------------- report


def _report_fctool():
    lines = []
    frozen = bool(getattr(sys, "frozen", False))
    built = "-"
    if frozen:
        try:
            stamp = os.path.getmtime(sys.executable)
            built = time.strftime("%Y-%m-%d", time.gmtime(stamp))
        except Exception:
            built = "-"
    lines.append("version=%s frozen=%s built=%s"
                 % (_ascii(APP_VERSION), "yes" if frozen else "no", built))
    return lines


def _report_helper(snapshot):
    if snapshot is None:
        return ["state=none"]
    lines = ["state=%s python=%s attempts=%s"
             % (_ascii(snapshot.state), _ascii(snapshot.python),
                _ascii(snapshot.spawn_attempts))]
    screen = snapshot.screen
    lines.append("helper_python=%s display=%s"
                 % (_ascii(snapshot.helper_python), _ascii(snapshot.display)))
    lines.append("render=%s damage=%s shape=%s composite=%s screen=%s"
                 % (_ascii(snapshot.xrender), _ascii(snapshot.damage),
                    _ascii(snapshot.shape), _ascii(snapshot.composite),
                    ("%sx%sx%s" % screen) if screen else "-"))
    # One line per failed attempt: last_error alone hides the first two.
    for index, attempt in enumerate(getattr(snapshot, "spawn_errors", ())
                                    or (), 1):
        lines.append("attempt %d: %s" % (index, _ascii(attempt)))
    return lines


def _report_clients(clients):
    if not clients:
        return ["none"]
    lines = []
    for entry in clients[:MAX_REPORT_CLIENTS]:
        x_id = entry.get("x_id")
        lines.append(
            "%s hwnd=%s x=%s viewable=%s depth=%s size=%s"
            % (_ascii(entry.get("title"))[:40], _ascii(entry.get("hwnd")),
               _ascii(x_id) if x_id else "NO-X-WINDOW",
               _ascii(entry.get("viewable")), _ascii(entry.get("depth")),
               _ascii(entry.get("size"))))
    if len(clients) > MAX_REPORT_CLIENTS:
        lines.append("... %d more" % (len(clients) - MAX_REPORT_CLIENTS,))
    return lines


def _report_probe(results):
    if not results:
        return ["none"]
    lines = []
    for item in results[:MAX_REPORT_PROBE]:
        if not isinstance(item, dict):
            continue
        lines.append("src=%s damage=%s composites=%s avg_ms=%s"
                     % (_ascii(item.get("src")),
                        _ascii(item.get("damage_events")),
                        _ascii(item.get("composites")),
                        _ascii(item.get("avg_composite_ms"))))
    if len(results) > MAX_REPORT_PROBE:
        lines.append("... %d more" % (len(results) - MAX_REPORT_PROBE,))
    return lines or ["none"]


def _merge_clients(clients, results):
    """Fold each probe result into the client entry sharing its X id."""
    by_src = {}
    for item in results or ():
        if isinstance(item, dict) and item.get("src") is not None:
            by_src[item.get("src")] = item
    merged = []
    for entry in clients or ():
        if not isinstance(entry, dict):
            continue
        row = dict(entry)
        found = by_src.get(row.get("x_id"))
        if found:
            row.setdefault("viewable", found.get("viewable"))
            row.setdefault("depth", found.get("depth"))
            size = found.get("src_size")
            if size and "size" not in row:
                try:
                    row["size"] = "%sx%s" % (size[0], size[1])
                except Exception:
                    pass
        merged.append(row)
    return merged


def _scrub(text, secrets_seq):
    """Replace every secret in ``secrets_seq`` with ``<redacted>``.

    Short strings are ignored: a secret must be long enough that replacing it
    cannot shred ordinary report text.
    """
    for item in secrets_seq or ():
        if isinstance(item, str) and len(item) >= 8:
            text = text.replace(item, "<redacted>")
    return text


def build_report(facts, snapshot, probe_result, clients=None) -> str:
    """The diagnostic text the Linux tester pastes back.

    Fixed section order ``[fctool] [host] [helper] [clients] [probe] [stats]
    [last_error]``, ASCII only, never longer than ``MAX_REPORT_BYTES``.  It
    carries no XAUTHORITY path and no client command line by construction:
    only the fields named below are ever rendered.  ``last_error`` is the one
    field whose text is not ours, so as a second line of defence every string
    in ``snapshot.redact`` (the one-shot auth tokens the supervisor issued) is
    replaced with ``<redacted>`` before the report is capped.
    """
    payload = probe_result if isinstance(probe_result, dict) else {}
    results = payload.get("results")
    results = results if isinstance(results, list) else []
    rows = clients if clients is not None else payload.get("clients")
    rows = _merge_clients(rows, results)

    sections = [
        ("[fctool]", _report_fctool()),
        ("[host]", [_ascii(facts)]),
        ("[helper]", _report_helper(snapshot)),
        ("[clients]", _report_clients(rows)),
        ("[probe]", _report_probe(results)),
        ("[stats]", ["frames=%s damage=%s coalesced=%s errors=%s "
                     "malformed=%s dropped=%s repairs=%s activations=%s "
                     "uptime_s=%s"
                     % (_ascii(getattr(snapshot, "frames", 0)),
                        _ascii(getattr(snapshot, "damage_events", 0)),
                        _ascii(getattr(snapshot, "coalesced", 0)),
                        _ascii(getattr(snapshot, "errors", 0)),
                        _ascii(getattr(snapshot, "malformed", 0)),
                        _ascii(getattr(snapshot, "dropped", 0)),
                        _ascii(getattr(snapshot, "repairs", 0)),
                        _ascii(getattr(snapshot, "activations", 0)),
                        _ascii(getattr(snapshot, "uptime_s", 0)))]),
        ("[last_error]", [_ascii(getattr(snapshot, "last_error", None))]),
    ]
    redact = getattr(snapshot, "redact", ())
    lines = []
    for header, body in sections:
        lines.append(header)
        for line in body:
            # scrub BEFORE the per-line cut, or a secret straddling the cut
            # would survive as an unmatchable fragment
            lines.append("  " + _scrub(_ascii(line, default="-"), redact)[:160])
    text = "\n".join(lines) + "\n"
    text = _scrub(text, redact)
    text = text.encode("ascii", "replace").decode("ascii")
    if len(text) > MAX_REPORT_BYTES:
        text = text[:MAX_REPORT_BYTES - 14].rstrip() + "\n... truncated\n"
    return text
