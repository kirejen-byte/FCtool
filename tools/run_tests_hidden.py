"""Run pytest on a hidden Win32 desktop so test windows never cover (or steal
focus from) whatever is on the interactive desktop -- e.g. a fullscreen game.

Usage:
    py -3.12 tools/run_tests_hidden.py [--visible] [--python "py -3.12"]
                                       [--desktop NAME] [--grace SECONDS]
                                       [--] <pytest args, passed verbatim>

Everything the launcher does not recognise is passed to pytest unchanged (use
``--`` if a pytest arg ever collides with a launcher flag).  Example -- the
validated full-suite pair:

    py -3.12 tools/run_tests_hidden.py tests -q -n auto -o faulthandler_timeout=120 --deselect tests/test_map_zoom_anim.py::test_tick_cadence_is_work_bound_not_work_plus_tickms
    py -3.12 tools/run_tests_hidden.py tests/test_map_zoom_anim.py::test_tick_cadence_is_work_bound_not_work_plus_tickms -q

How it works: ``CreateDesktopW`` makes (or re-opens) a desktop object in the
current window station; ``CreateProcessW`` starts the interpreter with
``STARTUPINFO.lpDesktop`` naming it.  Every window that process and its
children (pytest-xdist workers inherit the desktop) create lives on that
desktop: invisible on the interactive desktop and unable to take its
foreground.  The std handles are inherited, so output streams to this console
(or to whatever file/pipe the launcher's own stdout is redirected to).

The child tree runs inside a Job object with KILL_ON_JOB_CLOSE: on Ctrl-C the
console delivers the interrupt to pytest too, so the launcher first gives it
``--grace`` seconds (default 10) to report and exit; a second Ctrl-C, or the
grace expiring, terminates the whole tree (py.exe, python.exe, every xdist
worker).  If the launcher itself dies, the job handle closes and the tree dies
with it -- nothing is left running unseen on the hidden desktop.

No admin rights needed.  Stdlib only (ctypes).  ``--visible`` (or a non-Windows
platform) is a plain pass-through.  Exit code = pytest's exit code (2 if the
hidden launch itself failed).
"""
from __future__ import annotations

import shlex
import subprocess
import sys
import time

DEFAULT_DESKTOP = "FCToolTests"
DEFAULT_PYTHON = "py -3.12"
DEFAULT_GRACE_S = 10.0

USAGE = ("usage: py -3.12 tools/run_tests_hidden.py [--visible] "
         "[--python CMD] [--desktop NAME] [--grace SECONDS] [--] <pytest args...>")


def _parse(argv):
    opts = {"visible": False, "python": DEFAULT_PYTHON,
            "desktop": DEFAULT_DESKTOP, "grace": DEFAULT_GRACE_S}
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            rest.extend(argv[i + 1:])
            break
        if a == "--visible":
            opts["visible"] = True
        elif a in ("--python", "--desktop", "--grace") and i + 1 < len(argv):
            key = a[2:]
            opts[key] = float(argv[i + 1]) if key == "grace" else argv[i + 1]
            i += 1
        elif a == "--launcher-help":
            print(USAGE)
            print(__doc__)
            sys.exit(0)
        else:
            rest.append(a)
        i += 1
    return opts, rest


def _command(opts, pytest_args):
    # posix=False keeps Windows paths intact; strip the quotes it leaves on.
    interp = [p.strip('"') for p in shlex.split(opts["python"], posix=False)]
    return interp + ["-m", "pytest"] + list(pytest_args)


def _run_visible(cmd):
    try:
        return subprocess.call(cmd)
    except KeyboardInterrupt:
        return 130


# --------------------------------------------------------------------------
# Win32 plumbing
# --------------------------------------------------------------------------
GENERIC_ALL = 0x10000000
STARTF_USESTDHANDLES = 0x00000100
HANDLE_FLAG_INHERIT = 0x00000001
CREATE_SUSPENDED = 0x00000004
CREATE_UNICODE_ENVIRONMENT = 0x00000400
JobObjectExtendedLimitInformation = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
SEM_FAILCRITICALERRORS = 0x0001
SEM_NOGPFAULTERRORBOX = 0x0002
WAIT_OBJECT_0 = 0
STD_HANDLES = (-10, -11, -12)          # input, output, error


def _win32():
    import ctypes
    from ctypes import wintypes as w

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    u32 = ctypes.WinDLL("user32", use_last_error=True)

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [("cb", w.DWORD), ("lpReserved", w.LPWSTR),
                    ("lpDesktop", w.LPWSTR), ("lpTitle", w.LPWSTR),
                    ("dwX", w.DWORD), ("dwY", w.DWORD),
                    ("dwXSize", w.DWORD), ("dwYSize", w.DWORD),
                    ("dwXCountChars", w.DWORD), ("dwYCountChars", w.DWORD),
                    ("dwFillAttribute", w.DWORD), ("dwFlags", w.DWORD),
                    ("wShowWindow", w.WORD), ("cbReserved2", w.WORD),
                    ("lpReserved2", ctypes.c_void_p),
                    ("hStdInput", w.HANDLE), ("hStdOutput", w.HANDLE),
                    ("hStdError", w.HANDLE)]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", w.HANDLE), ("hThread", w.HANDLE),
                    ("dwProcessId", w.DWORD), ("dwThreadId", w.DWORD)]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", w.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", w.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", w.DWORD),
                    ("SchedulingClass", w.DWORD)]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    u32.CreateDesktopW.argtypes = [w.LPCWSTR, w.LPCWSTR, ctypes.c_void_p,
                                   w.DWORD, w.DWORD, ctypes.c_void_p]
    u32.CreateDesktopW.restype = w.HANDLE
    u32.OpenDesktopW.argtypes = [w.LPCWSTR, w.DWORD, w.BOOL, w.DWORD]
    u32.OpenDesktopW.restype = w.HANDLE
    u32.CloseDesktop.argtypes = [w.HANDLE]
    u32.CloseDesktop.restype = w.BOOL

    k32.CreateProcessW.argtypes = [w.LPCWSTR, w.LPWSTR, ctypes.c_void_p,
                                   ctypes.c_void_p, w.BOOL, w.DWORD,
                                   ctypes.c_void_p, w.LPCWSTR,
                                   ctypes.POINTER(STARTUPINFOW),
                                   ctypes.POINTER(PROCESS_INFORMATION)]
    k32.CreateProcessW.restype = w.BOOL
    k32.GetStdHandle.argtypes = [w.DWORD]
    k32.GetStdHandle.restype = w.HANDLE
    k32.SetHandleInformation.argtypes = [w.HANDLE, w.DWORD, w.DWORD]
    k32.SetHandleInformation.restype = w.BOOL
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
    k32.CreateJobObjectW.restype = w.HANDLE
    k32.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int,
                                            ctypes.c_void_p, w.DWORD]
    k32.SetInformationJobObject.restype = w.BOOL
    k32.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    k32.AssignProcessToJobObject.restype = w.BOOL
    k32.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
    k32.TerminateJobObject.restype = w.BOOL
    k32.ResumeThread.argtypes = [w.HANDLE]
    k32.ResumeThread.restype = w.DWORD
    k32.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    k32.WaitForSingleObject.restype = w.DWORD
    k32.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
    k32.GetExitCodeProcess.restype = w.BOOL
    k32.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
    k32.TerminateProcess.restype = w.BOOL
    k32.CloseHandle.argtypes = [w.HANDLE]
    k32.CloseHandle.restype = w.BOOL
    k32.SetErrorMode.argtypes = [w.UINT]
    k32.SetErrorMode.restype = w.UINT

    class Win32:
        pass
    api = Win32()
    api.ctypes, api.w, api.k32, api.u32 = ctypes, w, k32, u32
    api.STARTUPINFOW = STARTUPINFOW
    api.PROCESS_INFORMATION = PROCESS_INFORMATION
    api.JOBOBJECT_EXTENDED_LIMIT_INFORMATION = JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    return api


def _open_or_create_desktop(api, name):
    """Return (handle, created).  Re-uses a desktop a concurrent run still holds."""
    h = api.u32.OpenDesktopW(name, 0, False, GENERIC_ALL)
    if h:
        return h, False
    h = api.u32.CreateDesktopW(name, None, None, 0, GENERIC_ALL, None)
    if not h:
        err = api.ctypes.get_last_error()
        raise OSError(err, "CreateDesktopW(%r) failed: %s"
                      % (name, api.ctypes.FormatError(err).strip()))
    return h, True


def _make_job(api):
    job = api.k32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = api.JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not api.k32.SetInformationJobObject(
            job, JobObjectExtendedLimitInformation,
            api.ctypes.byref(info), api.ctypes.sizeof(info)):
        api.k32.CloseHandle(job)
        return None
    return job


def _kill(api, job, hproc):
    if job:
        api.k32.TerminateJobObject(job, 130)
    else:
        api.k32.TerminateProcess(hproc, 130)


def run_hidden(cmd, desktop, grace_s):
    api = _win32()
    ctypes = api.ctypes
    hdesk, _created = _open_or_create_desktop(api, desktop)
    job = None
    pi = api.PROCESS_INFORMATION()
    try:
        si = api.STARTUPINFOW()
        si.cb = ctypes.sizeof(si)
        si.lpDesktop = desktop          # no backslash = THIS window station
        std = [api.k32.GetStdHandle(n) for n in STD_HANDLES]
        if all(h and h != -1 for h in std):
            for h in std:               # console / pipe / file: make inheritable
                api.k32.SetHandleInformation(h, HANDLE_FLAG_INHERIT,
                                             HANDLE_FLAG_INHERIT)
            si.dwFlags |= STARTF_USESTDHANDLES
            si.hStdInput, si.hStdOutput, si.hStdError = std

        # A crashing interpreter must not park an invisible WER dialog on the
        # hidden desktop (the run would hang with nobody able to dismiss it).
        # The error mode is inherited by the whole child tree.
        api.k32.SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX)

        cmdline = ctypes.create_unicode_buffer(subprocess.list2cmdline(cmd))
        ok = api.k32.CreateProcessW(
            None, cmdline, None, None, True,
            CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT,
            None, None, ctypes.byref(si), ctypes.byref(pi))
        if not ok:
            err = ctypes.get_last_error()
            raise OSError(err, "CreateProcessW failed for %r: %s"
                          % (cmdline.value, ctypes.FormatError(err).strip()))

        job = _make_job(api)
        if job and not api.k32.AssignProcessToJobObject(job, pi.hProcess):
            api.k32.CloseHandle(job)
            job = None
        api.k32.ResumeThread(pi.hThread)

        interrupted_at = None
        while True:
            try:
                if api.k32.WaitForSingleObject(pi.hProcess, 200) == WAIT_OBJECT_0:
                    break
                if interrupted_at is not None and \
                        time.monotonic() - interrupted_at > grace_s:
                    sys.stderr.write("[run_tests_hidden] child still running "
                                     "%.0fs after Ctrl-C; killing the process "
                                     "tree\n" % grace_s)
                    _kill(api, job, pi.hProcess)
            except KeyboardInterrupt:
                if interrupted_at is None:
                    # The console delivered Ctrl-C to pytest as well; let it
                    # print its summary and exit on its own first.
                    interrupted_at = time.monotonic()
                    sys.stderr.write("[run_tests_hidden] Ctrl-C: waiting up to "
                                     "%.0fs for pytest (Ctrl-C again kills it "
                                     "now)\n" % grace_s)
                else:
                    _kill(api, job, pi.hProcess)

        code = api.w.DWORD(0)
        api.k32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
        rc = code.value
        if rc >= 0x80000000:            # NTSTATUS-style crash code
            sys.stderr.write("[run_tests_hidden] child exited with 0x%08X\n" % rc)
        return rc
    finally:
        if job:
            api.k32.CloseHandle(job)    # KILL_ON_JOB_CLOSE reaps any straggler
        for h in (pi.hThread, pi.hProcess):
            if h:
                api.k32.CloseHandle(h)
        api.u32.CloseDesktop(hdesk)     # object dies with its last handle


def main(argv=None):
    opts, pytest_args = _parse(sys.argv[1:] if argv is None else argv)
    cmd = _command(opts, pytest_args)
    if opts["visible"] or sys.platform != "win32":
        return _run_visible(cmd)
    try:
        return run_hidden(cmd, opts["desktop"], opts["grace"])
    except OSError as exc:
        sys.stderr.write("[run_tests_hidden] %s\n" % (exc,))
        sys.stderr.write("[run_tests_hidden] re-run with --visible to bypass "
                         "the hidden desktop\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
