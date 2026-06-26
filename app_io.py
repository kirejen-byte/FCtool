"""Shared atomic JSON writer.

Provides :func:`atomic_write_json`, a small helper used across the app to
persist JSON config/cache files durably. The write goes to a sibling
``<path>.tmp`` file first, is flushed (and optionally fsync'd) while the
handle is still open, and then atomically moved into place with
``os.replace`` (retried a few times on a transient Windows file lock from
OneDrive/antivirus). This guarantees that ``path`` is never observed in a
partially-written state: a reader sees either the old contents or the
fully-written new contents, never a truncated mix.

The helper RAISES on any failure (e.g. a value that cannot be serialized,
or an I/O error), cleaning up the temp file first. Callers are expected to
catch and log; the original file on disk is left untouched on failure.
"""

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


def atomic_write_json(path, data, *, indent=2, ensure_ascii=True, fsync=True):
    """Atomically write ``data`` as JSON to ``path``.

    The data is first written to ``f"{path}.tmp"`` (in the same directory
    as ``path`` so that ``os.replace`` is an atomic rename on the same
    filesystem), flushed, optionally fsync'd to durable storage, and then
    moved into place. The replace is retried a few times on a transient
    Windows file lock (e.g. OneDrive/antivirus). The destination ``path``
    is therefore never left in a partially-written state.

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
    tmp = f"{path}.tmp"
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
