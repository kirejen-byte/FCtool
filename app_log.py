"""Single logging entry point for the app.

This module is the app's one place to obtain a configured logger. On the
first call it configures the root logger once: a rotating file sink at
``<app_dir>/fctool.log`` plus a console sink that writes to the real
stdout (``sys.__stdout__``) so log lines survive the app's runtime
redirect of ``sys.stdout`` to ``os.devnull``.

Modules should do::

    from app_log import get_logger
    log = get_logger(__name__)

File-handler creation is wrapped so that an unwritable log directory
degrades gracefully to console-only logging and never crashes the app.
"""

import logging
import logging.handlers
import os
import sys
import threading
import time

from app_path import app_dir

_configured = False


def get_logger(name: str = "fctool") -> logging.Logger:
    """Return a logger, configuring the root logger once on first call.

    The first invocation sets the root level to ``INFO`` and attaches a
    rotating file handler (1 MB x 3 backups) and a stdout stream handler.
    Subsequent calls only look up and return the named logger, so handlers
    are never duplicated.

    Args:
        name: Logger name (typically ``__name__``). Defaults to "fctool".

    Returns:
        The :class:`logging.Logger` for ``name``.
    """
    global _configured
    if not _configured:
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"
        )

        try:
            file_handler = logging.handlers.RotatingFileHandler(
                os.path.join(app_dir(), "fctool.log"),
                maxBytes=1_000_000,
                backupCount=3,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except Exception:
            pass

        if sys.__stdout__ is not None:
            stream_handler = logging.StreamHandler(sys.__stdout__)
            stream_handler.setFormatter(formatter)
            root.addHandler(stream_handler)

        _configured = True

    return logging.getLogger(name)


# ── Uncaught-exception logging ────────────────────────────────────────────────
# In the windowed one-file exe sys.stdout / sys.stderr are None (fc_gui points
# them at os.devnull), so an uncaught exception at boot, in a Tk callback or in a
# worker thread used to leave nothing in fctool.log. These hooks write the
# exception (with traceback) to the log AND chain to the previous hook, so a
# source run from a console looks exactly as before.

_RATE_WINDOW_S = 60.0
_rate_lock = threading.Lock()
_rate_state: dict = {}      # (exc type name, innermost "file:line") -> [last_ts, suppressed]
_now = time.monotonic       # seam for tests


def _innermost_site(tb) -> str:
    """``file:line`` of the innermost traceback frame ('?' without one)."""
    site = "?"
    while tb is not None:
        site = f"{tb.tb_frame.f_code.co_filename}:{tb.tb_lineno}"
        tb = tb.tb_next
    return site


def log_uncaught(exc_type, exc, tb, source: str = "") -> bool:
    """Log an uncaught exception in full, at most once per 60 s per (type,
    innermost file:line); the next one logged notes how many were suppressed.
    ``KeyboardInterrupt`` / ``SystemExit`` are never logged. Never raises.
    Returns True when a record was written."""
    try:
        if exc_type is None or issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            return False
        key = (getattr(exc_type, "__name__", str(exc_type)), _innermost_site(tb))
        now = _now()
        with _rate_lock:
            entry = _rate_state.get(key)
            if entry is not None and now - entry[0] < _RATE_WINDOW_S:
                entry[1] += 1
                return False
            suppressed = entry[1] if entry is not None else 0
            _rate_state[key] = [now, 0]
            if len(_rate_state) > 256:          # bounded: drop the oldest keys
                for old in sorted(_rate_state, key=lambda k: _rate_state[k][0])[:128]:
                    if old != key:
                        _rate_state.pop(old, None)
        note = (f" ({suppressed} similar suppressed since the last report)"
                if suppressed else "")
        logging.getLogger("fctool.uncaught").error(
            "Uncaught exception%s%s", f" in {source}" if source else "", note,
            exc_info=(exc_type, exc, tb))
        return True
    except Exception:
        return False


def _wrap_sys_hook(prev):
    def _fctool_excepthook(exc_type, exc, tb):
        log_uncaught(exc_type, exc, tb, "main thread")
        try:
            prev(exc_type, exc, tb)
        except Exception:
            pass
    _fctool_excepthook._fctool_hook = True
    return _fctool_excepthook


def _wrap_thread_hook(prev):
    def _fctool_threading_excepthook(args):
        try:
            name = getattr(args.thread, "name", "") or "worker"
            log_uncaught(args.exc_type, args.exc_value, args.exc_traceback,
                         f"thread {name}")
        except Exception:
            pass
        try:
            prev(args)
        except Exception:
            pass
    _fctool_threading_excepthook._fctool_hook = True
    return _fctool_threading_excepthook


def install_exception_hooks() -> None:
    """Chain ``sys.excepthook`` and ``threading.excepthook`` through
    :func:`log_uncaught`. Idempotent; the previous hooks still run."""
    try:
        if not getattr(sys.excepthook, "_fctool_hook", False):
            sys.excepthook = _wrap_sys_hook(sys.excepthook)
        if not getattr(threading.excepthook, "_fctool_hook", False):
            threading.excepthook = _wrap_thread_hook(threading.excepthook)
    except Exception:
        pass


def install_tk_exception_hook(root) -> None:
    """Make Tk callback exceptions on ``root`` reach the log, then fall through
    to the root's own ``report_callback_exception`` (the default stderr print)."""
    try:
        prev = root.report_callback_exception
        if getattr(prev, "_fctool_hook", False):
            return

        def _fctool_report_callback_exception(exc_type, exc, tb):
            log_uncaught(exc_type, exc, tb, "Tk callback")
            try:
                prev(exc_type, exc, tb)
            except Exception:
                pass

        _fctool_report_callback_exception._fctool_hook = True
        root.report_callback_exception = _fctool_report_callback_exception
    except Exception:
        pass
