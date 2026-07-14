"""Shared atomic JSON writer.

Provides :func:`atomic_write_json`, a small helper used across the app to
persist JSON config/cache files durably. The write goes to a sibling temp
file with a per-writer-unique suffix (``<path>.<pid>.<n>.tmp``), is flushed
(and optionally fsync'd) while the handle is still open, and then atomically
moved into place with ``os.replace`` (retried a few times on a transient
Windows file lock from OneDrive/antivirus). The unique suffix means two
concurrent writers of the same path can never collide on a single temp file
(finding A4): whoever ``os.replace``s last wins and every replace is atomic.
This guarantees that ``path`` is never observed in a partially-written state:
a reader sees either the old contents or the fully-written new contents,
never a truncated mix.

The helper RAISES on any failure (e.g. a value that cannot be serialized,
or an I/O error), cleaning up the temp file first. Callers are expected to
catch and log; the original file on disk is left untouched on failure.
"""

import itertools
import json
import os
import time


# os.replace can raise PermissionError (WinError 32) on Windows when the
# destination file is momentarily held open by another process — most often
# OneDrive or antivirus syncing/scanning it. The lock clears in milliseconds, so
# retry a few times with a short backoff before giving up.
_REPLACE_ATTEMPTS = 5
_REPLACE_BACKOFF_S = 0.08


def _replace_with_retry(tmp: str, path: str) -> None:
    last = None
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as e:
            last = e
            if attempt < _REPLACE_ATTEMPTS - 1:
                time.sleep(_REPLACE_BACKOFF_S * (attempt + 1))
    raise last


# A process-wide monotonic counter: combined with the pid it makes every temp
# file name unique per writer (see _unique_tmp). itertools.count().__next__ is
# atomic under the GIL, so concurrent threads never draw the same value.
_TMP_COUNTER = itertools.count()


def _unique_tmp(path: str) -> str:
    """Return a temp path sibling to ``path``, unique to THIS write call.

    Shape ``f"{path}.{pid}.{n}.tmp"``: the pid disambiguates separate processes
    (two app instances over a OneDrive-synced dir, or a frozen exe plus a dev
    run) and the monotonic counter disambiguates concurrent writers inside one
    process (the worker-thread + Tk-thread save race, finding A4). Two writers
    of the same ``path`` therefore never select the same temp file and cannot
    truncate each other's in-flight write; whichever calls ``os.replace`` last
    wins, and every replace is atomic, so the destination is still never
    observed partially written.

    (itertools.count + os.getpid over uuid4: the counter's next() is atomic
    under the GIL and cheaper than uuid, and the pid already supplies the
    cross-process uniqueness uuid would add — uuid's global uniqueness buys
    nothing extra here.)
    """
    return f"{path}.{os.getpid()}.{next(_TMP_COUNTER)}.tmp"


def atomic_write_json(path, data, *, indent=2, ensure_ascii=True, fsync=True):
    """Atomically write ``data`` as JSON to ``path``.

    The data is first written to a per-writer-unique temp file beside ``path``
    (see :func:`_unique_tmp`; in the same directory as ``path`` so that
    ``os.replace`` is an atomic rename on the same filesystem), flushed,
    optionally fsync'd to durable storage, and then moved into place. The
    replace is retried a few times on a transient Windows file lock (e.g.
    OneDrive/antivirus). The destination ``path`` is therefore never left in a
    partially-written state.

    Args:
        path: Destination file path.
        data: Any JSON-serializable object.
        indent: Indentation passed to ``json.dump`` (default 2).
        ensure_ascii: Passed to ``json.dump`` (default True).
        fsync: If True (default), ``os.fsync`` the file before replacing,
            providing a stronger durability guarantee at some I/O cost.

    Raises:
        Exception: Re-raises any error encountered while serializing or
            writing (e.g. ``TypeError`` for non-serializable data, or
            ``OSError`` for I/O failures). The temp file is removed first
            and the original ``path`` is left untouched.
    """
    tmp = _unique_tmp(path)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, ensure_ascii=ensure_ascii)
            f.flush()
            if fsync:
                os.fsync(f.fileno())
        _replace_with_retry(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
