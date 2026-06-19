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
