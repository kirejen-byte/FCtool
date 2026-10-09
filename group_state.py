"""Group configuration: the files.

Everything the import machinery reads or writes on disk goes through this module, and it
reaches exactly these names inside the ``app_dir`` it is handed (spec 6.7 and 7.1):

* the five state files of ``group_items.FILE_NAMES`` - ``load_state`` / ``write_state``;
* three protocol files - ``group_import_pending.json`` (staged work),
  ``group_import_applying.json`` (the same file, renamed while it is applied) and
  ``group_config_state.json`` (history, attention, tasks, remembered exports, last report);
* the ``backups`` directory - one ``group-import-YYYYMMDD-HHMMSS`` folder per apply, holding a
  copy of every file about to change plus ``manifest.json``;
* temp siblings of the above (``<name>.<pid>.<n>.tmp``) - and those a killed write left
  beside the state, pending, applying and side files are removed again, under the apply lock
  (``claim_pending``, ``restore_backup``, ``prune_backups``);
* where the system has no named mutexes (anything but Windows), the lock file
  ``group_import.lock``.

Nothing else is ever opened: no sign-in file, no character map, no cache, no monitor state.
Backup manifests and the pending file are data anyone can edit, so a manifest can only ever
name one of the five state files (its ``name`` field is ignored, the file name comes from the
key) and a backup name can only ever be a folder this module could have created.

Who raises what
---------------
This module runs in the boot hook, before any window exists. Readers are TOTAL: a missing,
locked or damaged file gives a clean value, never an exception. A writer that could not do its
job says so, because carrying on in silence would lose settings:

* ``StateWriteError`` (``.file`` = the file name) - ``write_state``, ``make_backup``,
  ``write_pending``, ``update_applying``, ``cancel_pending``. Two subclasses:
  ``StateTooBigError`` - ``write_pending`` / ``update_applying`` for work larger than
  ``MAX_PENDING_BYTES``: nothing was written; ``StateApplyingError`` - ``write_pending`` /
  ``cancel_pending`` while the apply lock is held elsewhere: nothing was staged, nothing
  cancelled;
* ``ValueError`` - a programming error: a value that is no file content, an unknown file key;
* ``FileNotFoundError`` - ``restore_backup`` for a backup without a usable manifest;
* ``StateBusyError`` (``.file``) - the file is THERE and could not be opened right now:
  ``restore_backup`` for the manifest, ``read_sidecar(strict=True)`` for the side file.

``write_sidecar`` and ``finish_applying`` return ``False`` instead; everything else returns a
plain value. An ``app_dir`` that is no path - no string, empty, holding a NUL or (on Windows)
one of ``* ? " < > |`` or a colon anywhere but behind the drive letter, or a path-like whose
conversion raises - reads as "nothing there" for a reader and is a ``StateWriteError`` for a
writer (``cancel_pending``: ``False``, ``restore_backup``: ``FileNotFoundError``).

Busy is not damaged
-------------------
Antivirus and sync tools hold a file open for a moment, and every other open is refused
meanwhile. The four small files - pending, applying, a backup's manifest, the side file - are
therefore tried ``WRITE_ATTEMPTS`` times, ``WRITE_DELAY_S`` apart, and a file that is there
and still refuses is reported as BUSY, never as damaged or missing:

* ``read_pending`` / ``read_applying`` / ``claim_pending`` -> ``{"kind": "busy"}`` (a claim
  renames nothing then); ``{"kind": "invalid"}`` stays for content that was read and is not
  usable, ``None`` for no file;
* ``restore_backup`` raises ``StateBusyError``; ``list_backups(include_busy=True)`` lists such
  a backup as ``{"name", "busy": True}`` (it is left out by default);
* ``read_sidecar(strict=True)`` raises ``StateBusyError``, so that a read-modify-write can
  skip its write instead of replacing the history with an empty one.

Deleting an applying file or overwriting the history on the strength of "busy" would lose
what a retry a moment later reads fine. A file that turns up between a failed open and the
look whether it exists (a stage landing at that instant) is tried again as well, never called
damaged; and so is one that could not be read for want of memory - that passes, too.

A manifest and the side file are not read beyond ``MAX_SMALL_FILE_BYTES``. The pending and
the applying file hold a whole group file and have ``MAX_PENDING_BYTES``; they are written
compact, and work that would not fit is refused when it is staged (``StateTooBigError``) -
not staged and then called damaged at the next start.

One applier at a time
---------------------
A rename is no mutual exclusion on Windows: two processes started within a millisecond both
"win" pending -> applying. ``apply_lock(app_dir)`` is the exclusion - a context manager that
yields ``True`` when this thread of this process holds the lock of that folder and ``False``
when another process or thread does. It never raises and never waits; it is re-entrant; on
Windows it is a named mutex, which the system releases when its owner dies (no stale lock).
``claim_pending`` takes it itself around the rename, returns ``None`` when it is held
elsewhere, and never replaces an applying file that is already there. A caller that applies
should hold ``apply_lock`` for the whole apply and treat ``False`` as "another FCTool is at
it". ``write_pending`` and ``cancel_pending`` take it too, so nothing is staged or cancelled
under a claim: while the lock is held elsewhere both raise ``StateApplyingError`` ("an import
is being applied - try again in a moment") - the cancel only when something IS staged; with
nothing staged it answers ``False`` as ever. ``restore_backup`` and ``prune_backups`` are for
the holder of the lock as well.

Reading a file: MISSING is not UNREADABLE
-----------------------------------------
A file that does not exist is created from the app's defaults by the items; a file that exists
and cannot be read must never be written. Confusing the two would replace a user's library
with an empty one, so only "no such file" counts as missing - a lock, a folder in the file's
place, bad bytes, bad JSON or a JSON value that is no object all read as ``UNREADABLE``. A
refused open is retried briefly first (antivirus and sync tools hold files for milliseconds).
``changed_files`` compares what would be WRITTEN (the canonical JSON text), so ``1`` and
``True`` differ and a different key order alone does not.

Pure of the UI: stdlib, ``app_io``, ``default_config``, ``group_bundle`` (one constant) and
``group_items`` only.
"""

import contextlib
import ctypes
import hashlib
import itertools
import json
import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone

from app_io import atomic_write_json
from default_config import DEFAULT_CONFIG
from group_bundle import MAX_FILE_BYTES
from group_items import FILE_NAMES, UNREADABLE, State


STATE_FILE = "group_config_state.json"
PENDING_FILE = "group_import_pending.json"
APPLYING_FILE = "group_import_applying.json"
BACKUP_DIR = "backups"
BACKUP_PREFIX = "group-import-"
MANIFEST_FILE = "manifest.json"
LOCK_FILE = "group_import.lock"         # only where there are no named mutexes
KEEP_BACKUPS = 5
HISTORY_KEEP = 10

# The indentation each file's own writer uses, so a file written here differs from the app's
# output only where a value differs (config.json: fc_gui._save_config, indent=4).
INDENT = {"config": 4, "fittings": 2, "templates": 2, "infra": 2, "palette": 1}

WRITE_ATTEMPTS = 5          # write_state, per file; a restore's replace / delete; and the
WRITE_DELAY_S = 0.2         # opens of the four small files (pending, applying, manifest, side)
MAX_SMALL_FILE_BYTES = 16 * 1024 * 1024     # a manifest and the side file: not read beyond
# The pending and the applying file carry a whole group file (at most MAX_FILE_BYTES on disk,
# and a hand-made one can come out larger when it is written again), plus a small header.
MAX_PENDING_BYTES = 2 * MAX_FILE_BYTES + 65536
POLL_S = 0.25               # wait_for_exit
DEFAULT_WAIT_S = 8.0

_READ_ATTEMPTS = 3          # a refused open of a state file (not a missing file)
_READ_DELAY_S = 0.1
_MAX_WAIT_S = 300.0
_MAX_SAME_SECOND = 1000
_MAX_SUFFIX = 999999        # what _BACKUP_NAME can still read back
_LOCK_GRACE_S = 2.0         # a lock file without a pid in it is only then stale
_MUTEX_PREFIX = "Local\\FCToolGroupImport-"
_USE_MUTEX = os.name == "nt"
_PROTOCOL_INDENT = 1        # the side file
_TEXT_CLIP = 200
_KINDS = ("import", "restore")
_MAX_SEQ = 2 ** 53          # a manifest's `seq` beyond this was not written here
_TEMP_STALE_S = 3600.0      # a temp sibling this old belongs to no write that still runs
_EXTENDED = "\\\\?\\"         # the prefix of an extended-length Windows path
_NO_NAME_CHARS = frozenset('*?"<>|:')   # no Windows file name holds one of these
_MSG_APPLYING = "an import is being applied - try again in a moment"

_BACKUP_NAME = re.compile(re.escape(BACKUP_PREFIX) + r"(\d{8}-\d{6})(?:-(\d{1,6}))?")
_SHA256 = re.compile(r"[0-9a-f]{64}")
# What a killed write of one of this module's files leaves beside it: the five state files
# (a restore here, and the app's own writer, name their temp files alike), the pending and
# the applying file, the side file. ``<name>.<pid>.<n>.tmp``, ASCII digits only.
_TEMP_NAME = re.compile("(?:%s)\\.([0-9]{1,10})\\.[0-9]{1,20}\\.tmp" % "|".join(
    re.escape(name) for name in list(FILE_NAMES.values())
    + [PENDING_FILE, APPLYING_FILE, STATE_FILE]))
_TMP_COUNTER = itertools.count()


class _Marker:
    __slots__ = ("_name",)

    def __init__(self, name):
        self._name = name

    def __repr__(self):
        return self._name


_MISSING = _Marker("MISSING")   # "no such file" - never handed to a caller
_BUSY = _Marker("BUSY")         # "it is there and could not be opened" - never handed out


class StateWriteError(Exception):
    """A file of the feature could not be written, copied or removed. ``file`` names it;
    ``message`` replaces the default text."""

    def __init__(self, file, message=None):
        super().__init__(message if isinstance(message, str) and message
                         else "could not write %s" % (file,))
        self.file = file


class StateTooBigError(StateWriteError):
    """The work is larger than the pending / applying file may be (``MAX_PENDING_BYTES``):
    it could not be read back, so nothing was written."""

    def __init__(self, file):
        super().__init__(file, "%s would be too large" % (file,))


class StateApplyingError(StateWriteError):
    """The apply lock is held elsewhere - an import is being applied right now - so nothing
    was staged and nothing cancelled: what is staged is still staged. Try again in a moment."""

    def __init__(self, file):
        super().__init__(file, _MSG_APPLYING)


class StateBusyError(Exception):
    """A file of the feature is there and could not be opened right now (something else holds
    it). ``file`` names it. Not damaged, not missing: trying again later is the answer."""

    def __init__(self, file):
        super().__init__("%s is in use" % (file,))
        self.file = file


# -- paths and raw reads -----------------------------------------------------

def _root(app_dir) -> str:
    """``app_dir`` as a string. ``ValueError`` when it is no path: whatever its conversion
    raises, no string, empty, a NUL - or, on Windows, a character no file name can hold
    (``open`` answers those with "invalid argument", which is neither "no such file" nor
    "in use", and would read as a file that is busy for ever) or a colon that is not the
    drive letter's (that names a device or a stream, not a folder)."""
    try:
        root = os.fspath(app_dir)
    except Exception:
        raise ValueError("app_dir is not a path") from None
    if not isinstance(root, str) or not root or "\0" in root:
        raise ValueError("app_dir is not a path")
    if os.name == "nt":
        named = root[len(_EXTENDED):] if root.replace("/", "\\").startswith(_EXTENDED) else root
        if named[1:2] == ":" and named[0].isascii() and named[0].isalpha():
            named = named[2:]                   # the drive letter's own colon
        if not _NO_NAME_CHARS.isdisjoint(named):
            raise ValueError("app_dir is not a path")
    return root


def _path(app_dir, *parts) -> str:
    return os.path.join(_root(app_dir), *parts)


def _writable_root(app_dir, file) -> str:
    """``_root`` for a writer: a folder that is no path cannot be written to."""
    try:
        return _root(app_dir)
    except ValueError as exc:
        raise StateWriteError(file) from exc


def _size_of(handle):
    try:
        return os.fstat(handle.fileno()).st_size
    except (OSError, ValueError, AttributeError, TypeError):
        return None                             # no size to ask for: the read is counted


def _read_text(path, sleep, attempts=_READ_ATTEMPTS, delay=_READ_DELAY_S, limit=None,
               encoding="utf-8"):
    """The file's text, or one of three markers: ``_MISSING`` (no such file), ``_BUSY`` (it is
    there and every open or read was refused, or memory ran out - both pass) or
    ``UNREADABLE`` (it is there and holds nothing usable: a folder, a link or junction to
    nothing, bad bytes, more than ``limit``)."""
    for attempt in range(attempts):
        try:
            if os.path.isdir(path):
                return UNREADABLE
            with open(path, "r", encoding=encoding) as handle:
                if limit is None:
                    return handle.read()
                size = _size_of(handle)
                if size is not None and size > limit:
                    return UNREADABLE
                # Asked for by the file's own size: a read of n characters sets n bytes
                # aside before it looks, and the cap is tens of megabytes.
                text = handle.read((limit if size is None else size) + 1)
                return UNREADABLE if len(text) > limit else text
        except FileNotFoundError:
            if not os.path.lexists(path):
                return _MISSING
            if _is_link(path):
                return UNREADABLE               # a link that leads nowhere
            # There after all: it was staged, or replaced, at this very moment. Look again.
        except (IsADirectoryError, NotADirectoryError, ValueError, TypeError):
            return UNREADABLE                   # ValueError: bad bytes, or a path that is none
        except OSError:
            if _is_link(path) and not os.path.exists(path):
                return UNREADABLE               # a junction that leads nowhere
        except MemoryError:
            pass                                # like a refused open: it may fit in a moment
        if attempt < attempts - 1:
            sleep(delay)
    return _BUSY


def _read_json(path, sleep, **budget):
    """The parsed JSON value, ``_MISSING``, ``_BUSY`` or ``UNREADABLE``. Never raises on file
    content. No memory to parse it with is ``_BUSY``: the file may be perfectly good."""
    text = _read_text(path, sleep, **budget)
    if text is _MISSING or text is _BUSY or text is UNREADABLE:
        return text
    try:
        return json.loads(text)
    except MemoryError:
        return _BUSY
    except (ValueError, RecursionError):
        return UNREADABLE


def _read_small(path, sleep, limit=None):
    """``_read_json`` for the four small files: the writers' retry budget, and a size cap
    (``MAX_SMALL_FILE_BYTES`` unless ``limit`` says otherwise)."""
    return _read_json(path, sleep, attempts=WRITE_ATTEMPTS, delay=WRITE_DELAY_S,
                      limit=MAX_SMALL_FILE_BYTES if limit is None else limit)


def _digest(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256(path):
    """Hex digest of the file, or ``None`` when it cannot be read."""
    try:
        return _digest(path)
    except (OSError, ValueError):
        return None


def _sha256_retrying(path, sleep):
    """``_sha256`` that waits for a file something else holds; ``None`` at once for no file."""
    for attempt in range(WRITE_ATTEMPTS):
        try:
            return _digest(path)
        except (FileNotFoundError, ValueError):
            return None
        except OSError:
            if attempt < WRITE_ATTEMPTS - 1:
                sleep(WRITE_DELAY_S)
    return None


def _sync_file(path) -> None:
    """Flush ``path`` to the disk. Raises ``OSError`` when that cannot be done."""
    with open(path, "r+b") as handle:
        os.fsync(handle.fileno())


def _sync_retrying(path, sleep) -> None:
    """``_sync_file`` that waits for a file something else holds for a moment."""
    for attempt in range(WRITE_ATTEMPTS):
        try:
            _sync_file(path)
            return
        except FileNotFoundError:
            raise
        except OSError:
            if attempt == WRITE_ATTEMPTS - 1:
                raise
            sleep(WRITE_DELAY_S)


def _remove_quiet(path) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _remove_retrying(path, sleep) -> bool:
    """True when no file is left at ``path``."""
    for attempt in range(WRITE_ATTEMPTS):
        try:
            os.remove(path)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            if os.path.isdir(path):
                return False
            if attempt < WRITE_ATTEMPTS - 1:
                sleep(WRITE_DELAY_S)
    return False


def _replace_retrying(source, target, sleep) -> bool:
    for attempt in range(WRITE_ATTEMPTS):
        try:
            os.replace(source, target)
            return True
        except FileNotFoundError:
            return False
        except OSError:
            if attempt < WRITE_ATTEMPTS - 1:
                sleep(WRITE_DELAY_S)
    return False


def _remove_entry(path, sleep) -> bool:
    """``_remove_retrying`` for a protocol file - and an EMPTY folder in its place goes as
    well (``os.rmdir``: never what is in a folder, never what a link leads to). True when
    nothing is left at ``path``."""
    if _remove_retrying(path, sleep):
        return True
    try:
        if os.path.isdir(path):
            os.rmdir(path)
    except OSError:
        return False
    return not os.path.lexists(path)


def _write_bytes(path, raw, sleep) -> None:
    """Put ``raw`` at ``path`` atomically: a temp sibling, flushed to the disk, then
    ``os.replace`` (retried while the target is held). Raises ``OSError``; the temp sibling
    is removed and the target is left as it was."""
    temp = "%s.%d.%d.tmp" % (path, os.getpid(), next(_TMP_COUNTER))
    try:
        with open(temp, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        if not _replace_retrying(temp, path, sleep):
            raise OSError("could not move the new file into place")
    except BaseException:
        _remove_quiet(temp)
        raise


def _force_remove(function, path, _problem) -> None:
    """``shutil.rmtree``'s error hook: a read-only file is made writable and tried again."""
    if function in (os.unlink, os.remove, os.rmdir):
        try:
            os.chmod(path, 0o700)
            function(path)
        except OSError:
            pass


def _remove_tree(path) -> None:
    """Delete a backup folder with what is in it, read-only copies included. Best effort."""
    try:
        try:
            shutil.rmtree(path, onexc=_force_remove)
        except TypeError:                       # before Python 3.12: the hook's older name
            shutil.rmtree(path, onerror=_force_remove)
    except Exception:
        pass


def _sweep_temp_files(root) -> None:
    """Remove the temp files a killed write left in the data folder (``_TEMP_NAME``:
    ``<one of this module's files>.<pid>.<n>.tmp``): those whose process is gone, and those
    made more than ``_TEMP_STALE_S`` ago, because pid numbers are handed out again. Every
    other name is left alone. Best effort, silent.

    A temp file of a write that may still run must never be touched: on Windows a delete
    that was opened on the temp file's name can land after the rename - on the live file.
    So one that carries THIS process's pid is never swept (another thread may be about to
    rename it), and age is counted from when the file was MADE as well as from when it was
    last written - a restore's temp file is a ``copy2`` and carries the old time of the
    file it copies. Callers hold ``apply_lock``."""
    try:
        listed = os.listdir(root)
    except (OSError, ValueError, TypeError):
        return
    for name in listed:
        match = _TEMP_NAME.fullmatch(name)
        if not match or int(match.group(1)) == os.getpid():
            continue
        path = os.path.join(root, name)
        try:
            stale = not _pid_running(int(match.group(1))) or time.time() - max(
                os.path.getmtime(path), os.path.getctime(path)) > _TEMP_STALE_S
        except Exception:
            continue
        if stale:
            _remove_quiet(path)                 # a file: a folder of that name stays


# -- the five state files ----------------------------------------------------

def _default_config() -> dict:
    # The same deep copy fc_gui._load_config takes when there is no config.json.
    return json.loads(json.dumps(DEFAULT_CONFIG))


def load_state(app_dir, *, sleep=time.sleep) -> State:
    """Read the five files. ``config.json`` missing -> the app's defaults; a missing side file
    -> ``None``; anything that exists and is not a readable JSON object -> ``UNREADABLE``."""
    values = {}
    for key, name in FILE_NAMES.items():
        try:
            # The palette's own reader (palette.load) takes a byte order mark; so does this.
            value = _read_json(_path(app_dir, name), sleep,
                               encoding="utf-8-sig" if key == "palette" else "utf-8")
        except ValueError:                      # app_dir is no path: nothing may be written
            value = UNREADABLE
        if value is _MISSING:
            value = _default_config() if key == "config" else None
        elif not isinstance(value, dict):
            value = UNREADABLE
        values[key] = value
    return State(**values)


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True)


def changed_files(old, new) -> list:
    """The file keys whose value differs, in ``FILE_NAMES`` order. A file that was
    ``UNREADABLE`` is never listed - it is never written.

    "Differs" is about what would be written: the canonical JSON text of both values is
    compared, so ``1`` / ``True`` and ``40`` / ``40.0`` differ (``==`` calls them equal, and
    the items report such a pair as an update), while another key order alone is no change.
    A value JSON cannot hold counts as changed - ``write_state`` then says why."""
    changed = []
    for key in FILE_NAMES:
        before = getattr(old, key, UNREADABLE)
        if before is UNREADABLE:
            continue
        try:
            same = _canonical(before) == _canonical(getattr(new, key, UNREADABLE))
        except Exception:
            same = False
        if not same:
            changed.append(key)
    return changed


def write_state(app_dir, old, new, *, sleep=time.sleep) -> list:
    """Write the files that differ, each atomically and in its own indentation; return their
    keys. A failing write is retried (``WRITE_ATTEMPTS`` tries, ``WRITE_DELAY_S`` apart), then
    ``StateWriteError`` names the file - files written before it stay written, the caller
    restores its backup. A new value that is not a dict raises ``ValueError`` before anything
    is written; an ``app_dir`` that is no path is a ``StateWriteError`` for the first file
    that was to be written."""
    keys = changed_files(old, new)
    for key in keys:
        if not isinstance(getattr(new, key, None), dict):
            raise ValueError("no content to write for %s" % (FILE_NAMES[key],))
    if not keys:
        return keys
    root = _writable_root(app_dir, FILE_NAMES[keys[0]])
    for key in keys:
        name = FILE_NAMES[key]
        _write_retrying(os.path.join(root, name), getattr(new, key), INDENT[key], sleep, name)
    return keys


def _write_retrying(path, data, indent, sleep, name) -> None:
    last = None
    for attempt in range(WRITE_ATTEMPTS):
        try:
            atomic_write_json(path, data, indent=indent)
            return
        except OSError as exc:
            last = exc
            if attempt < WRITE_ATTEMPTS - 1:
                sleep(WRITE_DELAY_S)
        except Exception as exc:                # not serialisable: trying again cannot help
            raise StateWriteError(name) from exc
    raise StateWriteError(name) from last


# -- backups -----------------------------------------------------------------

def _as_utc(now) -> datetime:
    if isinstance(now, datetime):
        try:
            if now.tzinfo is None:
                return now.replace(tzinfo=timezone.utc)
            return now.astimezone(timezone.utc)
        except (ValueError, OverflowError, OSError):
            pass
    return datetime.now(timezone.utc)


def _iso(moment) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_now_iso() -> str:
    """The current UTC time as ``YYYY-MM-DDTHH:MM:SSZ``."""
    return _iso(datetime.now(timezone.utc))


def _backup_keys(keys) -> list:
    if isinstance(keys, (str, bytes)):
        raise ValueError("keys must be a list of file keys")
    try:
        listed = list(keys)
    except TypeError:
        raise ValueError("keys must be a list of file keys") from None
    wanted = []
    for key in listed:
        if not isinstance(key, str) or key not in FILE_NAMES:
            raise ValueError("unknown file key %r" % (key,))
        if key not in wanted:
            wanted.append(key)
    return wanted


def _new_backup_folder(root, moment, last_by_name=False):
    """Create and return ``(name, path)`` of a fresh backup folder; a taken name gets -2, -3.

    The suffix only ever grows within one second: a lower one that a prune freed is not
    handed out again, or the newest backup would sort as the oldest and be pruned next. The
    data folder itself is never created here - a backup of a folder that is not there is a
    mistake, not a first run.

    ``last_by_name``: the name must sort after every name that is there, whatever the clock
    says - it then takes the newest of their seconds, with the next suffix."""
    if not os.path.isdir(root):
        raise FileNotFoundError(root)
    parent = os.path.join(root, BACKUP_DIR)
    try:
        os.mkdir(parent)
    except FileExistsError:
        pass
    there = [match for match in map(_BACKUP_NAME.fullmatch, os.listdir(parent)) if match]
    stamp = moment.strftime("%Y%m%d-%H%M%S")
    if last_by_name:
        stamp = max([stamp] + [match.group(1) for match in there])
    base = BACKUP_PREFIX + stamp
    highest = 0
    for match in there:
        if match.group(1) == stamp:
            highest = max(highest, int(match.group(2) or 1))
    for n in range(highest + 1, min(highest + _MAX_SAME_SECOND, _MAX_SUFFIX) + 1):
        name = base if n == 1 else "%s-%d" % (base, n)
        path = os.path.join(parent, name)
        try:
            os.mkdir(path)
        except FileExistsError:
            continue
        return name, path
    raise FileExistsError(base)


def _copy_retrying(source, target, sleep) -> bool:
    """Copy ``source``; False when there is no such file. Raises ``OSError`` when it is there
    and cannot be copied."""
    for attempt in range(_READ_ATTEMPTS):
        try:
            shutil.copy2(source, target)
            return True
        except FileNotFoundError:
            if os.path.lexists(source):
                raise
            return False
        except OSError:
            if attempt == _READ_ATTEMPTS - 1:
                raise
            sleep(_READ_DELAY_S)
    return False


def _settle_copy(copy, sleep) -> str:
    """Flush a fresh backup copy to the disk and return its sha256; ``OSError`` when it
    cannot be flushed or read back.

    The copy is made writable first - the COPY, never the file it was taken from: a read-only
    attribute comes along with ``copy2``, and would stop the flush, a later delete and the
    way back. A scanner that grabs a new file for a moment is waited for."""
    try:
        os.chmod(copy, 0o600)
    except OSError:
        pass                                    # the flush says whether that mattered
    _sync_retrying(copy, sleep)
    digest = _sha256_retrying(copy, sleep)
    if digest is None:
        raise OSError("the copy cannot be read back")
    return digest


def _next_seq(parent, sleep):
    """One more than the highest ``seq`` a manifest under ``parent`` carries - or ``None``
    when a manifest is in use: the highest is not known then, and a guess could come out
    LOWER than a backup made earlier."""
    highest = 0
    for name in _backup_names(parent):
        manifest = _read_manifest(os.path.join(parent, name), sleep)
        if manifest is _BUSY:
            return None
        if manifest is not None and manifest["seq"] is not None:
            highest = max(highest, manifest["seq"])
    return highest + 1


def make_backup(app_dir, keys, *, label, now=None, sleep=time.sleep) -> str:
    """Copy the named state files into a new ``backups/group-import-YYYYMMDD-HHMMSS`` folder
    (UTC; ``now`` is a ``datetime``) with a ``manifest.json``, and return the folder's name.

    A file that does not exist is recorded as ``existed: false`` - a restore removes it again.
    Every copy is flushed to the disk before the manifest (flushed too) vouches for it: a
    power cut must not leave a manifest over copies that never arrived. A backup that could
    not be completed is removed and ``StateWriteError`` raised: nothing may be written on the
    strength of half a backup. A data folder that does not exist is NOT created
    (``StateWriteError``). An unknown key raises ``ValueError``.

    The manifest carries ``seq``, one more than the highest of the backups that are there:
    the order they were made in, whatever the clock said (``prune_backups`` and
    ``list_backups`` go by it). When a manifest that is there is in use, the highest is not
    known: ``seq`` is then ``null`` and the backup is ranked by its NAME - which is, for that
    case alone, made to sort after every backup folder that is there (the newest of their
    seconds, next suffix, should the clock have gone back), so that it never ranks below a
    backup made before it. ``created`` is what the clock said, always."""
    root = _writable_root(app_dir, BACKUP_DIR)
    wanted = _backup_keys(keys)
    moment = _as_utc(now)
    seq = _next_seq(os.path.join(root, BACKUP_DIR), sleep)
    try:
        name, folder = _new_backup_folder(root, moment, last_by_name=seq is None)
    except OSError as exc:
        raise StateWriteError(BACKUP_DIR) from exc
    failing = MANIFEST_FILE
    try:
        entries = []
        for key in wanted:
            failing = FILE_NAMES[key]
            copy = os.path.join(folder, failing)
            existed = _copy_retrying(os.path.join(root, failing), copy, sleep)
            digest = _settle_copy(copy, sleep) if existed else None
            entries.append({"key": key, "name": failing, "existed": existed, "sha256": digest})
        failing = MANIFEST_FILE
        manifest = {"version": 1, "label": _clip(label), "created": _iso(moment), "seq": seq,
                    "files": entries}
        atomic_write_json(os.path.join(folder, MANIFEST_FILE), manifest, indent=2)
    except Exception as exc:
        _remove_tree(folder)
        raise StateWriteError(failing) from exc
    return name


def _clip(value) -> str:
    return value[:_TEXT_CLIP] if isinstance(value, str) else ""


def _is_link(path) -> bool:
    isjunction = getattr(os.path, "isjunction", None)
    return os.path.islink(path) or bool(isjunction and isjunction(path))


def _backup_names(parent) -> list:
    """Names of the backup folders under ``parent``, newest first. Only real directories this
    module could have created: the name pattern is the whole test, links are left alone."""
    try:
        listed = os.listdir(parent)
    except (OSError, ValueError, TypeError):
        return []
    found = []
    for name in listed:
        match = _BACKUP_NAME.fullmatch(name)
        if not match:
            continue
        path = os.path.join(parent, name)
        if os.path.isdir(path) and not _is_link(path):
            found.append(((match.group(1), int(match.group(2) or 1)), name))
    found.sort(reverse=True)
    return [name for _key, name in found]


def _backup_folder(app_dir, name):
    """Path of the backup called ``name``, or ``None`` when that is no backup name (or
    ``app_dir`` no path)."""
    if not isinstance(name, str) or not _BACKUP_NAME.fullmatch(name):
        return None
    try:
        return _path(app_dir, BACKUP_DIR, name)
    except ValueError:
        return None


def _read_manifest(folder, sleep):
    """``{label, created, seq, files}`` rebuilt from the manifest; ``None`` when there is
    none or it is not usable; ``_BUSY`` when it is there and could not be opened.

    The manifest is a file on disk: every entry is rebuilt from scratch. Only the five state
    files can be named, each once (a key listed twice keeps its first entry only, and loses
    that too when it is malformed), ``existed`` must be a real boolean - anything else could
    turn into a delete - and a copied file must carry its sha256. ``seq`` is a positive int
    or ``None``."""
    data = _read_small(os.path.join(folder, MANIFEST_FILE), sleep)
    if data is _BUSY:
        return _BUSY
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        return None
    entries, seen = [], set()
    for entry in data["files"]:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if not isinstance(key, str) or key not in FILE_NAMES or key in seen:
            continue
        seen.add(key)
        existed, digest = entry.get("existed"), entry.get("sha256")
        if existed is True:
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                continue
        elif existed is False:
            digest = None
        else:
            continue
        entries.append({"key": key, "name": FILE_NAMES[key], "existed": existed,
                        "sha256": digest})
    seq = data.get("seq")
    if type(seq) is not int or not 0 < seq < _MAX_SEQ:
        seq = None
    return {"label": _clip(data.get("label")), "created": _clip(data.get("created")),
            "seq": seq, "files": entries}


def list_backups(app_dir, *, include_busy=False, sleep=time.sleep) -> list:
    """The backups, newest first, each ``{name, label, created, files}`` with ``files`` =
    ``[{key, name, existed, sha256}]``. Folders without a usable manifest are skipped.

    Newest first as ``prune_backups`` ranks them (``_backup_states``): by the order they were
    made in, not by what the clock said - ``[0]`` is the backup made last.

    A backup whose manifest is there but could not be opened right now is skipped too, unless
    ``include_busy`` is true: then it is listed as ``{"name": ..., "busy": True}`` - it is
    neither gone nor damaged, and a restore of it raises ``StateBusyError``."""
    try:
        parent = _path(app_dir, BACKUP_DIR)
    except ValueError:
        return []
    backups = []
    for name, state, manifest in _backup_states(parent, sleep):
        if state == _IN_USE and include_busy:
            backups.append({"name": name, "busy": True})
        elif state == _GOOD:
            backups.append({"name": name, "label": manifest["label"],
                            "created": manifest["created"], "files": manifest["files"]})
    return backups


def _install_copy(source, target, digest, sleep) -> bool:
    """Put a verified copy of ``source`` at ``target``: a temp sibling, flushed to the disk,
    then ``os.replace``."""
    temp = "%s.%d.%d.tmp" % (target, os.getpid(), next(_TMP_COUNTER))
    try:
        shutil.copy2(source, temp)
        if _sha256(temp) == digest:
            _sync_file(temp)
            if _replace_retrying(temp, target, sleep):
                return True
    except OSError:
        pass
    _remove_quiet(temp)
    return False


def restore_backup(app_dir, name, *, sleep=time.sleep) -> list:
    """Put the files of backup ``name`` back and return the file names that are now as they
    were when it was taken.

    All or nothing as far as the backup goes: EVERY copy is checked against its recorded
    sha256 first, and when one is gone, damaged or cannot be read, no live file is touched
    and ``[]`` returned - half a restore is a mix of two states.

    Then, per manifest entry: ``existed`` -> the copy goes back atomically (temp sibling,
    flushed, ``os.replace``), unless the live file already equals it; not ``existed`` -> the
    live file is removed if present. A file that could not be replaced or removed is simply
    left out of the result - compare it with the manifest's files to know whether the restore
    was complete.

    Raises ``FileNotFoundError`` when ``name`` is no backup (a link or junction in a backup's
    place is none) or its manifest is not usable, and ``StateBusyError`` when the manifest is
    there but could not be opened right now.

    The caller holds ``apply_lock``: two restores at once, or a restore beside an apply,
    would mix two states (the temp files a killed write left are swept here as well)."""
    folder = _backup_folder(app_dir, name)
    if folder is not None and _is_link(folder):
        folder = None                           # a link could lead anywhere
    manifest = _read_manifest(folder, sleep) if folder is not None else None
    if manifest is _BUSY:
        raise StateBusyError(MANIFEST_FILE)
    if manifest is None:
        raise FileNotFoundError("no backup manifest for %r" % (name,))
    root = os.path.dirname(os.path.dirname(folder))
    _sweep_temp_files(root)                     # what a restore that was killed left behind
    for entry in manifest["files"]:
        if entry["existed"] and _sha256_retrying(
                os.path.join(folder, entry["name"]), sleep) != entry["sha256"]:
            return []
    restored = []
    for entry in manifest["files"]:
        file_name = entry["name"]
        live = os.path.join(root, file_name)
        if not entry["existed"]:
            done = _remove_retrying(live, sleep)
        elif _sha256(live) == entry["sha256"]:
            done = True
        else:
            done = _install_copy(os.path.join(folder, file_name), live, entry["sha256"], sleep)
        if done:
            restored.append(file_name)
    return restored


_GOOD, _IN_USE, _DAMAGED, _HALF = "good", "in use", "damaged", "half"


def _backup_states(parent, sleep) -> list:
    """``[(name, state, manifest | None)]`` for the backup folders, newest first - the one
    ranking, for the listing and for pruning alike. The rule:

    * all folders are put in the order of their NAMES (time, then suffix), newest first;
    * the backups whose manifest carries a ``seq`` are then re-dealt among the places THEY
      hold, highest ``seq`` first: among themselves they rank by the order they were made
      in, even when the clock was set back in between;
    * a folder without a readable ``seq`` - a manifest in use or damaged, none at all, or a
      backup made while another manifest was in use (``seq: null``) - keeps the place its
      name gives it. Such a backup gets a name that sorts after every folder that was there
      (``make_backup``), and every backup made later that carries a ``seq`` carries a higher
      one than all before it - so it never ranks below a backup made before it."""
    found = []
    for name in _backup_names(parent):
        folder = os.path.join(parent, name)
        manifest = _read_manifest(folder, sleep)
        if manifest is _BUSY:
            found.append((name, _IN_USE, None))
        elif manifest is not None:
            found.append((name, _GOOD, manifest))
        elif os.path.lexists(os.path.join(folder, MANIFEST_FILE)):
            found.append((name, _DAMAGED, None))
        else:
            found.append((name, _HALF, None))

    def seq(entry):
        return entry[2] and entry[2]["seq"]

    numbered = iter(sorted((entry for entry in found if seq(entry) is not None),
                           key=seq, reverse=True))
    return [next(numbered) if seq(entry) is not None else entry for entry in found]


def prune_backups(app_dir, keep=KEEP_BACKUPS, *, sleep=time.sleep) -> None:
    """Delete all but the newest ``keep`` backups. Best effort, silent.

    Only a backup that can be restored takes one of the ``keep`` places: a folder with a
    usable manifest, or with one that is in use right now (that one is never deleted here).
    Newest is by the manifests' ``seq``, by name where there is none (``_backup_states`` has
    the rule; ``list_backups`` lists in the same order). Of the folders of this module's
    naming that are no backup:

    * one WITHOUT a manifest - what a backup that failed leaves when it cannot be removed -
      goes as soon as a good backup is newer (a newer one may be a backup in the making);
    * one with a manifest that is not usable goes when a good backup of its age would:
      once ``keep`` good ones are newer.

    Temp files a killed write left beside this module's files are removed as well. The
    caller holds ``apply_lock``: a prune beside an apply could delete the backup that
    apply is about to need."""
    if type(keep) is not int or keep < 0:
        keep = KEEP_BACKUPS
    try:
        root = _root(app_dir)
    except ValueError:
        return
    _sweep_temp_files(root)
    parent = os.path.join(root, BACKUP_DIR)
    newer = 0                                   # restorable backups newer than this folder
    for name, state, _manifest in _backup_states(parent, sleep):
        if state in (_GOOD, _IN_USE):
            newer += 1
            doomed = newer > keep and state == _GOOD
        elif state == _HALF:
            doomed = newer >= 1
        else:
            doomed = newer >= max(keep, 1)
        if doomed:
            _remove_tree(os.path.join(parent, name))


# -- the state side file -----------------------------------------------------

def _empty_sidecar() -> dict:
    return {"version": 1, "imports": [], "attention": [], "tasks": [], "exports": {},
            "last_report": None}


def read_sidecar(app_dir, *, strict=False, sleep=time.sleep) -> dict:
    """``group_config_state.json`` with every known key present and of its type: ``imports``,
    ``attention`` and ``tasks`` are lists of dicts, ``exports`` a dict, ``last_report`` a dict
    or ``None``. Missing or damaged -> the empty one. Other keys are kept as stored.

    A file that is there but could not be opened right now reads as the empty one as well -
    unless ``strict`` is true: then ``StateBusyError`` is raised. Whoever reads in order to
    write the file back passes ``strict=True`` and skips the write on that error; writing the
    empty one back would replace the whole history."""
    side = _empty_sidecar()
    try:
        path = _path(app_dir, STATE_FILE)
    except ValueError:
        return side
    stored = _read_small(path, sleep)
    if stored is _BUSY and strict:
        raise StateBusyError(STATE_FILE)
    if not isinstance(stored, dict):
        return side
    for key, value in stored.items():
        if key not in side:
            side[key] = value
    version = stored.get("version")
    if type(version) is int and version >= 1:
        side["version"] = version
    for key in ("imports", "attention", "tasks"):
        if isinstance(stored.get(key), list):
            side[key] = [entry for entry in stored[key] if isinstance(entry, dict)]
    if isinstance(stored.get("exports"), dict):
        side["exports"] = stored["exports"]
    if isinstance(stored.get("last_report"), dict):
        side["last_report"] = stored["last_report"]
    return side


def write_sidecar(app_dir, data) -> bool:
    """Write the side file atomically. False on any failure; never raises."""
    if not isinstance(data, dict):
        return False
    try:
        atomic_write_json(_path(app_dir, STATE_FILE), data, indent=_PROTOCOL_INDENT)
    except Exception:
        return False
    return True


# -- one applier at a time ---------------------------------------------------

_WAIT_OBJECT_0 = 0x00
_WAIT_ABANDONED = 0x80          # its last owner died holding it: it is ours now
_NO_FOLDER = _Marker("NO_FOLDER")
_locks = {}                     # lock key -> [owner thread, depth, what to release]
_locks_guard = threading.Lock()


def _lock_identity(app_dir):
    """``(mutex name, lock file path)`` for the folder - one lock per folder, however the
    path to it is spelled: upper or lower case, an 8.3 short name, through a junction or a
    link, with the ``\\\\?\\`` prefix or without."""
    final = os.path.abspath(_root(app_dir))
    try:
        final = os.path.realpath(final)         # the folder itself, not one way to it
    except Exception:
        pass
    if final.startswith(_EXTENDED + "UNC\\"):
        final = "\\\\" + final[len(_EXTENDED) + 4:]
    elif final.startswith(_EXTENDED):
        final = final[len(_EXTENDED):]
    spelled = os.path.normcase(final).lower()
    digest = hashlib.sha256(spelled.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    return _MUTEX_PREFIX + digest, os.path.join(final, LOCK_FILE)


def _mutex_take(name):
    """A handle on the named mutex, owned by this thread - or ``None``: another thread or
    process owns it. Never waits."""
    lib, _wintypes = _win32()
    handle = lib.CreateMutexW(None, False, name)
    if not handle:
        return None
    if lib.WaitForSingleObject(handle, 0) in (_WAIT_OBJECT_0, _WAIT_ABANDONED):
        return handle
    lib.CloseHandle(handle)
    return None


def _mutex_drop(handle) -> None:
    lib, _wintypes = _win32()
    lib.ReleaseMutex(handle)
    lib.CloseHandle(handle)


def _lock_file_stale(path) -> bool:
    """Whether the lock file's owner is gone. It holds the owner's pid; one without a pid is
    being written this moment, or is litter once it has aged."""
    try:
        with open(path, "r", encoding="ascii") as handle:
            text = handle.read(32).strip()
    except (OSError, ValueError):
        return False
    if text.isdigit():
        pid = int(text)
        # Our own pid, yet no thread of this process holds the lock (the caller looked):
        # an earlier process with this number left it.
        return pid == os.getpid() or not _pid_running(pid)
    try:
        return time.time() - os.path.getmtime(path) > _LOCK_GRACE_S
    except OSError:
        return False


def _lock_file_take(path):
    """Create the lock file (``O_CREAT | O_EXCL``, holding this pid) and return its path;
    ``None`` when a live process has it. A folder that is not there has nothing to guard."""
    for attempt in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if attempt or not _lock_file_stale(path):
                return None
            _remove_quiet(path)
            continue
        except FileNotFoundError:
            return _NO_FOLDER
        except OSError:
            return None
        try:
            os.write(fd, b"%d" % os.getpid())
        except OSError:
            os.close(fd)
            _remove_quiet(path)
            return None
        os.close(fd)
        return path
    return None


def _lock_enter(key, name, path) -> bool:
    me = threading.get_ident()
    with _locks_guard:
        entry = _locks.get(key)
        if entry is not None:
            if entry[0] != me:
                return False
            entry[1] += 1
            return True
        token = _mutex_take(name) if _USE_MUTEX else _lock_file_take(path)
        if token is None:
            return False
        _locks[key] = [me, 1, token]
        return True


def _lock_leave(key) -> None:
    with _locks_guard:
        entry = _locks.get(key)
        if entry is None or entry[0] != threading.get_ident():
            return
        entry[1] -= 1
        if entry[1] > 0:
            return
        del _locks[key]
        token = entry[2]
        try:
            if token is _NO_FOLDER:
                pass
            elif isinstance(token, str):
                _remove_quiet(token)
            else:
                _mutex_drop(token)
        except Exception:
            pass


@contextlib.contextmanager
def apply_lock(app_dir):
    """``with apply_lock(app_dir) as held:`` - ``held`` is ``True`` when this thread of this
    process now holds the folder's apply lock, ``False`` when another process or thread does
    (or ``app_dir`` is no path). Only the holder may claim and apply staged work. One lock
    per folder, whichever way the path to it is spelled.

    Never raises, never waits. Re-entrant: the holder can take it again (``claim_pending``,
    ``write_pending`` and ``cancel_pending`` do), and it is given up when the outermost
    ``with`` ends. Across processes it is a named mutex on Windows
    (``Local\\FCToolGroupImport-<16 hex of the folder's sha256>``) - the system releases it
    when its owner dies, so there is no stale lock - and elsewhere the lock file
    ``group_import.lock`` holding the owner's pid, removed on the way out and taken over when
    that process is gone."""
    key, held = None, False
    try:
        name, path = _lock_identity(app_dir)
        key = name if _USE_MUTEX else os.path.normcase(path)
        held = _lock_enter(key, name, path)
    except Exception:
        held = False
    try:
        yield held
    finally:
        if held:
            _lock_leave(key)


# -- pending / applying ------------------------------------------------------

def _protocol(value):
    """What a pending or applying file holds: ``None`` when there is no file, the dict when it
    names a known kind, ``{"kind": "busy"}`` when it is there and could not be opened right
    now, else ``{"kind": "invalid"}`` - it was read and is not usable."""
    if value is _MISSING:
        return None
    if value is _BUSY:
        return {"kind": "busy"}
    if isinstance(value, dict):
        kind = value.get("kind")
        if isinstance(kind, str) and kind in _KINDS:
            return value
    return {"kind": "invalid"}


def _read_protocol(app_dir, name, sleep):
    try:
        path = _path(app_dir, name)
    except ValueError:
        return None
    return _protocol(_read_small(path, sleep, MAX_PENDING_BYTES))


def _protocol_bytes(name, data) -> bytes:
    """What goes into a pending / applying file: compact JSON, pure ASCII. Judged before any
    file is made - ``ValueError`` for no dict, ``StateWriteError`` for a value JSON cannot
    hold, ``StateTooBigError`` for more than this module would read back."""
    if not isinstance(data, dict):
        raise ValueError("%s takes a dict" % (name,))
    try:
        raw = json.dumps(data, indent=None, separators=(",", ":"),
                         ensure_ascii=True).encode("ascii")
    except Exception as exc:
        raise StateWriteError(name) from exc
    if len(raw) > MAX_PENDING_BYTES:
        raise StateTooBigError(name)
    return raw


def _write_protocol(path, name, raw, sleep) -> None:
    try:
        _write_bytes(path, raw, sleep)
    except Exception as exc:
        raise StateWriteError(name) from exc


def read_pending(app_dir, *, sleep=time.sleep):
    """The staged work, ``None`` when nothing is staged, ``{"kind": "invalid"}`` for a pending
    file that is damaged (it is still there, and a claim will discard it), ``{"kind": "busy"}``
    for one that is there and could not be opened right now (try again later)."""
    return _read_protocol(app_dir, PENDING_FILE, sleep)


def write_pending(app_dir, data, *, sleep=time.sleep) -> None:
    """Stage ``data`` atomically, replacing anything staged before. ``StateWriteError`` when
    it could not be written; its subclass ``StateTooBigError`` when it is larger than
    ``MAX_PENDING_BYTES`` - then nothing is written at all, what was staged before stays.

    Takes ``apply_lock`` itself, so that no claim is under way while the file is replaced.
    Held by another process or thread - an import is being applied right now - it writes
    nothing and raises ``StateApplyingError`` (a ``StateWriteError``: "an import is being
    applied - try again in a moment")."""
    raw = _protocol_bytes(PENDING_FILE, data)
    root = _writable_root(app_dir, PENDING_FILE)
    with apply_lock(root) as held:
        if not held:
            raise StateApplyingError(PENDING_FILE)
        _write_protocol(os.path.join(root, PENDING_FILE), PENDING_FILE, raw, sleep)


def cancel_pending(app_dir, *, sleep=time.sleep) -> bool:
    """Delete the pending file; True if one existed, False when nothing is staged (and for an
    ``app_dir`` that is no path). A file that is there and cannot be deleted raises
    ``StateWriteError`` - it would still be applied at the next start.

    Takes ``apply_lock`` itself, and looks and deletes under it. While another process or
    thread holds it and a pending file IS there, nothing is deleted and
    ``StateApplyingError`` raised: the work is still staged - it is being claimed right now,
    or will be applied at the next start - and ``False`` would tell the caller it was
    cancelled. An empty folder in the file's place is removed (``True``); one that holds
    something is not (``StateWriteError``)."""
    try:
        root = _root(app_dir)
    except ValueError:
        return False
    path = os.path.join(root, PENDING_FILE)
    with apply_lock(root) as held:
        if not os.path.lexists(path):
            return False
        if not held:
            raise StateApplyingError(PENDING_FILE)
        if not _remove_entry(path, sleep):
            raise StateWriteError(PENDING_FILE)
        return True


def claim_pending(app_dir, *, sleep=time.sleep):
    """Rename pending -> applying (``os.replace``) and return its content, as read just before
    the rename. Exclusive: of all the threads and processes that ask, one gets the content -
    and what it gets is what the applying file holds, because nothing is staged under the
    lock the claim holds (``write_pending``).

    ``None`` - nothing was claimed: there is no pending file; or ``apply_lock`` is held by
    another process or thread (the claim takes it itself, a caller that already holds it
    keeps it); or an applying file is already there (it is never replaced); or the rename
    failed. Whatever was staged stays staged.

    ``{"kind": "busy"}`` - the pending file is there but could not be opened right now;
    nothing is renamed.

    Content that is not a dict with ``kind`` import / restore is claimed all the same and
    reads as ``{"kind": "invalid"}``. A FOLDER in the pending file's place reads as
    ``{"kind": "invalid"}`` too, but is left where it is: renamed, it would sit in the
    applying file's place, where nothing could remove it.

    Under the lock, before the claim, the temp files a killed stage or write left in the
    data folder are swept (``_sweep_temp_files``)."""
    try:
        root = _root(app_dir)
    except ValueError:
        return None
    pending = os.path.join(root, PENDING_FILE)
    applying = os.path.join(root, APPLYING_FILE)
    with apply_lock(root) as held:
        if not held:
            return None
        _sweep_temp_files(root)
        if os.path.lexists(applying):
            return None
        if os.path.isdir(pending):
            return _protocol(UNREADABLE)
        value = _read_small(pending, sleep, MAX_PENDING_BYTES)
        if value is _MISSING:
            return None
        if value is _BUSY:
            return _protocol(value)
        if not _replace_retrying(pending, applying, sleep):
            return None
    return _protocol(value)


def read_applying(app_dir, *, sleep=time.sleep):
    """The applying file: ``None`` when there is none, ``{"kind": "invalid"}`` when damaged,
    ``{"kind": "busy"}`` when it is there and could not be opened right now - an interrupted
    run may be behind it, so it must not be deleted on the strength of that."""
    return _read_protocol(app_dir, APPLYING_FILE, sleep)


def update_applying(app_dir, data, *, sleep=time.sleep) -> None:
    """Rewrite the applying file atomically (it gains ``backup`` once the backup exists).
    ``StateWriteError`` when it could not be written (``StateTooBigError`` when it would be
    larger than ``MAX_PENDING_BYTES``: nothing is written then)."""
    raw = _protocol_bytes(APPLYING_FILE, data)
    root = _writable_root(app_dir, APPLYING_FILE)
    _write_protocol(os.path.join(root, APPLYING_FILE), APPLYING_FILE, raw, sleep)


def finish_applying(app_dir, *, sleep=time.sleep) -> bool:
    """Delete the applying file. True when none is left, False when it could not be removed
    (the next start would then take the apply for an interrupted one). Never raises.

    An EMPTY folder in the file's place is removed as well - it would block every later
    claim; a folder that holds something is left alone (``False``)."""
    try:
        path = _path(app_dir, APPLYING_FILE)
    except ValueError:
        return False
    return _remove_entry(path, sleep)


# -- waiting for the process that staged the work ----------------------------

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_ACCESS_DENIED = 5
_STILL_ACTIVE = 259
_kernel32 = None


def _win32():
    """``(kernel32, wintypes)`` with the calls used here typed. A private ``WinDLL`` instance,
    so the argument types set here cannot clash with another module's."""
    global _kernel32
    from ctypes import wintypes
    if _kernel32 is None:
        lib = ctypes.WinDLL("kernel32", use_last_error=True)
        filetime_p = ctypes.POINTER(wintypes.FILETIME)
        lib.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        lib.OpenProcess.restype = wintypes.HANDLE
        lib.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        lib.GetExitCodeProcess.restype = wintypes.BOOL
        lib.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (filetime_p,) * 4
        lib.GetProcessTimes.restype = wintypes.BOOL
        lib.CloseHandle.argtypes = (wintypes.HANDLE,)
        lib.CloseHandle.restype = wintypes.BOOL
        lib.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        lib.CreateMutexW.restype = wintypes.HANDLE
        lib.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        lib.WaitForSingleObject.restype = wintypes.DWORD
        lib.ReleaseMutex.argtypes = (wintypes.HANDLE,)
        lib.ReleaseMutex.restype = wintypes.BOOL
        _kernel32 = lib
    return _kernel32, wintypes


def _valid_pid(pid) -> bool:
    return type(pid) is int and 0 < pid < 2 ** 32


def _win_process(pid):
    """``(running, creation_time | None)`` for a Windows pid.

    A process that has exited can still be opened while anyone holds a handle on it, so
    "opens" is not "runs": the exit code decides. A process that exists and may not be queried
    (access denied) counts as running, with no creation time."""
    lib, wintypes = _win32()
    handle = lib.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED, None
    try:
        code = wintypes.DWORD()
        if not lib.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True, None
        if code.value != _STILL_ACTIVE:
            return False, None
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not lib.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                   ctypes.byref(kernel), ctypes.byref(user)):
            return True, None
        stamp = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return True, stamp or None
    finally:
        lib.CloseHandle(handle)


def process_started(pid):
    """When the running process ``pid`` was created, as one int (Windows: its creation
    FILETIME, 100 ns ticks since 1601). ``None`` for a pid that is not running, on any
    failure, and on other systems. Together with the pid it names one process for good -
    Windows hands a pid number out again once its process is gone."""
    if os.name != "nt" or not _valid_pid(pid):
        return None
    try:
        return _win_process(pid)[1]
    except Exception:
        return None


def _pid_running(pid) -> bool:
    if not _valid_pid(pid):
        return False
    if os.name == "nt":
        return _win_process(pid)[0]
    # NEVER os.kill(pid, 0) on Windows - there it terminates the process.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _writer_alive(pid, started) -> bool:
    """The default probe: the process exists and - when its start time is known - is the very
    process that staged the work, not a later one that was given the same pid."""
    if type(started) is int:
        return process_started(pid) == started
    return _pid_running(pid)


def _wait_limit(timeout_s) -> float:
    if type(timeout_s) in (int, float) and timeout_s == timeout_s \
            and timeout_s not in (float("inf"), float("-inf")):
        return min(max(float(timeout_s), 0.0), _MAX_WAIT_S)
    return DEFAULT_WAIT_S


def wait_for_exit(pid, started=None, timeout_s=DEFAULT_WAIT_S, *, probe=None,
                  sleep=time.sleep) -> bool:
    """Wait until process ``pid`` (started at ``started``) is gone; False when it is still
    there after ``timeout_s``. True at once for no pid, a non-int and this process itself.

    ``probe(pid, started) -> bool`` says "alive"; it is asked every ``POLL_S`` seconds and an
    exception from it counts as not alive. The wait is counted in ``sleep`` calls."""
    if type(pid) is not int or pid <= 0 or pid == os.getpid():
        return True
    if probe is None:
        probe = _writer_alive
    limit = _wait_limit(timeout_s)
    waited = 0.0
    while True:
        try:
            alive = bool(probe(pid, started))
        except Exception:
            alive = False
        if not alive:
            return True
        if waited >= limit:
            return False
        sleep(POLL_S)
        waited += POLL_S
