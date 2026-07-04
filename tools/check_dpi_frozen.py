"""Frozen-exe DPI verification (plan Task D1 / spec risk S3). Run manually:

    python tools/check_dpi_frozen.py

or against the frozen build (bundle as a dev-only console exe, or wire a
``--check-dpi`` early-exit in the frozen entrypoint). Prints the value of
``GetAwarenessFromDpiAwarenessContext(GetThreadDpiAwarenessContext())``.

Expected ``2`` == PROCESS_PER_MONITOR_DPI_AWARE (the PMv2 manifest + the runtime
``SetProcessDpiAwarenessContext(-4)`` fallback both resolve to this awareness
value). Not imported by the app. Read-only DPI queries only: this script never
creates a window, changes focus, or sends input, so it is safe to run on a live
desktop.
"""
from __future__ import annotations

import sys

# DPI_AWARENESS enum (winuser.h) — return values of
# GetAwarenessFromDpiAwarenessContext:
#   -1 INVALID, 0 UNAWARE, 1 SYSTEM_AWARE, 2 PER_MONITOR_AWARE (covers PMv1+PMv2)
DPI_AWARENESS_INVALID = -1
DPI_AWARENESS_UNAWARE = 0
DPI_AWARENESS_SYSTEM_AWARE = 1
DPI_AWARENESS_PER_MONITOR_AWARE = 2

EXPECTED = DPI_AWARENESS_PER_MONITOR_AWARE

_LABELS = {
    DPI_AWARENESS_INVALID: "INVALID",
    DPI_AWARENESS_UNAWARE: "UNAWARE",
    DPI_AWARENESS_SYSTEM_AWARE: "SYSTEM_AWARE",
    DPI_AWARENESS_PER_MONITOR_AWARE: "PER_MONITOR_AWARE",
}


def query_awareness(user32=None) -> int:
    """Return the current thread's DPI_AWARENESS value.

    ``user32`` is injectable for tests/non-disruptive checks; in production it is
    ``ctypes.windll.user32``. Both calls are read-only (no side effects on any
    window, focus, or input).
    """
    if user32 is None:  # pragma: no cover — real Win32 path
        if sys.platform != "win32":
            raise RuntimeError("DPI awareness check requires Windows")
        import ctypes

        user32 = ctypes.windll.user32
    ctx = user32.GetThreadDpiAwarenessContext()
    return int(user32.GetAwarenessFromDpiAwarenessContext(ctx))


def main(user32=None) -> int:
    awareness = query_awareness(user32=user32)
    label = _LABELS.get(awareness, f"UNKNOWN({awareness})")
    ok = awareness == EXPECTED
    print(f"DPI awareness = {awareness} ({label}) "
          f"-> expected {EXPECTED} ({_LABELS[EXPECTED]}): "
          f"{'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
