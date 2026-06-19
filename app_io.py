"""Shared atomic JSON writer.

Provides :func:`atomic_write_json`, a small helper used across the app to
persist JSON config/cache files durably. The write goes to a sibling
``<path>.tmp`` file first, is flushed (and optionally fsync'd) while the
handle is still open, and then atomically moved into place with
``os.replace``. This guarantees that ``path`` is never observed in a
partially-written state: a reader sees either the old contents or the
fully-written new contents, never a truncated mix.

The helper RAISES on any failure (e.g. a value that cannot be serialized,
or an I/O error), cleaning up the temp file first. Callers are expected to
catch and log; the original file on disk is left untouched on failure.
"""

import json
import os


def atomic_write_json(path, data, *, indent=2, ensure_ascii=True, fsync=True):
    """Atomically write ``data`` as JSON to ``path``.

    The data is first written to ``f"{path}.tmp"`` (in the same directory
    as ``path`` so that ``os.replace`` is an atomic rename on the same
    filesystem), flushed, optionally fsync'd to durable storage, and then
    moved into place. The destination ``path`` is therefore never left in
    a partially-written state.

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
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
