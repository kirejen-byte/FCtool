"""Group configuration: stage an import, apply it at the next start.

The running app never changes its own settings files for an import. It STAGES the work
(``stage_import`` / ``stage_restore`` write ``group_import_pending.json``) and restarts; the
boot hook then calls :func:`apply_pending` before any window, store or monitor exists, so
everything that boots afterwards reads files that already hold the import (spec 7).

``apply_pending`` - the steps of spec 7.3
-----------------------------------------
1. An applying file left by a dead run -> its backup is put back (``interrupted_restored``).
2. Nothing staged -> ``None``.
3. The process that staged the work is still alive -> ``deferred``; the pending file stays.
4. pending -> applying (the claim).
5. ``restore`` -> the backup's files are put back (``restored``).
6. Read the group file and the five state files. ``config.json`` unreadable -> ``failed``.
7. Validate, then apply every selected item to a copy of the state.
8. Nothing differs -> ``unchanged``: no backup, no write.
9. Back up the files that differ; record the backup's name in the applying file.
10. Write the changed files. A failure puts the backup back -> ``failed``.
11. History, attention, tasks, tag and report go to the side file; old backups are pruned; the
    applying file is deleted.

It NEVER raises - it runs before a window exists - and it never leaves a mix of old and new
files on purpose: once a file may have been written, every failure puts the backup back. When
even that cannot be finished (a file stays locked), the applying file is kept, so the next
start tries again, at most ``RESTORE_ATTEMPTS`` times in all; after the last one the history
gets a ``failed`` entry that names the backup, so it can still be restored by hand.

All or nothing, the side file included: data files that are new while the history entry, the
tag, the queued tasks and the attention flags could not be saved are an import nobody can
see or take back - so they are put back too (``failed``: "could not be recorded"). The same
when anything raises between the data files and their record (``failed``: "could not be
completed"; that run is not marked ``done``). A restore whose side-file half could not be
saved keeps its applying file and is repeated at the next start. The boot flags of a
``failed`` report are always False.

A state file in use is no failure either. The five state files are read BEFORE the claim;
when one that the staged work needs - for an import ``config.json`` and the files of the
selected items, for a restore the files in its backup - is there and cannot be opened right
now, the answer is ``deferred`` ("Your settings files are in use ..."): nothing claimed,
nothing touched. A file that opens and does not parse keeps its own rule (step 6; an item
whose own file is damaged is skipped).

One applier at a time: claim, backup, write and record all run inside
``group_state.apply_lock`` - and nothing else does. The wait for the process that staged the
work (step 3, up to eight seconds) happens BEFORE the lock is taken; under the lock the
pending file is looked at again, and when it names another writer than the one that was
waited for, the lock is given up and that one is waited for first. While another FCTool
holds the lock it is asked for again every ``group_state.POLL_S`` seconds, for up to
``APPLYING_WAIT`` seconds, as long as there is staged work to do - a pending file or an
applying file: the holder is a start before its claim or in the middle of its writes, and
a start that went on now would read the old files, or files half old and half new, with
the other FCTool still at them. Then the answer is ``deferred`` and nothing was touched
(with neither file there, there is nothing to wait for: ``None``). A run
that raises is rolled back inside the lock as well. The helpers that edit the side file
(``set_tag``, ``add_attention`` ...) take the same lock; they wait for it up to
``EDIT_WAIT`` seconds (they run on the Tk thread), and write nothing when another FCTool
still holds it then: each returns its "could not save" value - never silence.

A build that is missing item modules (``group_items.LOAD_FAILURES``: a broken frozen build)
applies no import: with half a registry the file would "apply" with whatever is left and be
consumed. ``plan_import`` and ``stage_import`` refuse it in the window; ``apply_pending``
clears the staged import and reports ``failed`` - nothing claimed, nothing written. A
staged restore and the put-back of an interrupted run need no item and go ahead.

Busy is not damaged, gone or empty: antivirus and sync tools hold a file open for a moment.
A pending, applying or side file that is there and cannot be opened right now -> ``deferred``,
nothing touched, nothing deleted (the side file holds the history and the ``done`` token; it
is read BEFORE the claim). A backup whose manifest is in use is tried again at the next
start; it is "gone" only when ``group_state`` says there is no such backup. And whoever reads
the side file in order to write it back skips the write when the read was refused - writing
the empty one would replace the history, the tag, the exports, the attention and the tasks.

Three things in the protocol files go beyond the plan's list of keys:

* ``writer_image`` - the image name of the process that staged the work ("fctool.exe",
  "python.exe"; ``group_state.process_image``), beside its pid and start time. A pid is
  handed out again once its process is gone, and nearly half the processes of a usual
  desktop refuse to be opened: by its image the default wait tells an FCTool that cannot
  be asked (waited for) from a stranger that got the writer's pid (not waited for). A
  pending file without it - an older build's - is judged by this process's own image.
* ``token`` - every staged file carries a random one, and the side file remembers the token of
  the last finished run as ``done``. An applying file whose token is ``done`` belongs to a run
  that finished and only could not delete it: it is removed, never "restored".
  ``done`` is written when a run ends that got as far as its backup (applied, restored,
  rolled back - and the one failure in between: the applying file could not record the
  backup, so no state file was written), and for an ``unchanged`` run whose applying file
  could not be removed - never for a failure before the backup existed. A pending file
  without a token (a hand-made one) gets one at the claim, written into the applying file.
* Once the backup exists the applying file is rewritten SMALL: ``{version, kind, token,
  staged_at, meta, backup, applier_pid, applier_started}`` - the group file itself is not
  needed any more. The applier's pid and start time let a SECOND FCTool that starts while the
  first is still writing see that the run is alive: it reports ``deferred`` and touches
  nothing, instead of "restoring" a backup under the first one's hands.

The report (a JSON dict; also kept as ``last_report`` in the side file)::

    {"status": applied | unchanged | failed | deferred | restored | interrupted_restored,
     "kind": import | restore, "bundle": {"id", "name", "revision"}, "at": "<UTC>",
     "backup": "<folder name or blank>", "items": [ItemResult.to_dict(), ...],
     "problems": [...], "message": "<one sentence>",
     "flags": {"coalitions_seeded": bool, "motd_disarm": bool}, "log": [...],
     "seen": False}

``log`` holds lines for the application log, not for the user (ASCII, no names). Today there
is one: the applying file could not be removed at the end of the run. That changes nothing
at the next start - the run is marked ``done``, so the file is removed then, never
"restored" - and the line is not part of the report the side file keeps. While such a
leftover cannot be removed, work staged behind it is reported ``deferred`` at every start.

What the side file KEEPS of a report - as ``last_report`` and as a history entry - is a
bounded copy (``_kept``): of every list of names or lines the first ``_STORED_KEEP``
entries and one last ``"and <n> more"``, each entry at most ``_STORED_CLIP`` characters,
and fewer entries still when the copy would not fit ``_STORED_BYTES``. The report
``apply_pending`` RETURNS is whole. One import of five thousand fits used to cost the side
file megabytes, twice; two of them, and it was larger than it is ever read back.

An applying file whose CONTENT is damaged names no backup any more, while the state files
may be half written. It is cleared, but not silently: when the backup made last has no
history entry - then it can be the dead run's own - the report (``failed``) names it and
the history gets a ``failed`` entry for it. A backup that an entry already names belongs to
a FINISHED import: the dead run made none and wrote nothing.

Files (owner rule A2): this module reaches the disk only through ``group_state`` - the five
state files, the three protocol files and ``backups`` - plus ONE listing of ``app_dir`` for
names matching ``esi_tokens*.json`` (``is_fresh``); those files are never opened.

The group tag is cleaned by ``group_bundle.clean_tag`` - here as everywhere - never by
``clean_text``.

Pure: stdlib, ``group_bundle``, ``group_items`` and ``group_state`` only. No Tk, no
``ui_theme``, no ``fc_gui``, no network.
"""

import glob
import json
import os
import re
import time
import uuid

import group_bundle
import group_items
import group_state
from group_bundle import BundleError, clean_tag, clean_text
from group_items import FILE_NAMES, ItemResult, Options
from group_state import (HISTORY_KEEP, StateApplyingError, StateBusyError, StateTooBigError,
                         StateWriteError)


TAG_ITEM = "meta.tag"                   # the pseudo-result of the group tag
FLAGS = ("coalitions_seeded", "motd_disarm")
STATUSES = ("applied", "unchanged", "failed", "deferred", "restored", "interrupted_restored")
RESTORE_ATTEMPTS = 3                    # starts that try to put one backup back, in all
APPLYING_WAIT = 60.0                    # seconds a start waits for the apply lock while there
#                                         is staged work to do: another FCTool is at it
APPLIER_WAIT = APPLYING_WAIT            # the older name of that ONE limit (it was 8 seconds,
#                                         for a lock held before the claim: too short)
EDIT_WAIT = 0.5                         # seconds a side-file helper waits for the apply lock
MAX_EXPORTS = 50                        # export names remembered (the newest)
MAX_TASKS = 20                          # queued tasks kept (the newest)
MAX_CHOICE_GROUPS = 20                  # groups whose doctrine choices are remembered (newest)
MAX_CHOICES = 500                       # ...and choices of one group (the newest)
CHOICES_KEY = "doctrine_choices"        # the side file's key for them (_put_choices)
VERIFY_TASK = "verify_structure"
TOKEN_FILES = "esi_tokens*.json"        # looked for by NAME only

# What the window says when staging failed - see stage_error_text().
MSG_TOO_BIG = "This group file is too large to import."
MSG_APPLYING = "Another FCTool is applying an import - try again in a moment."
MSG_NOT_STAGED = "The import could not be saved for the next start; nothing was changed."
# ...and when this build cannot apply a group file at all (%s: the missing item modules).
MSG_INCOMPLETE = ("This FCTool is incomplete and cannot apply a group file (missing: %s). "
                  "Reinstall or update FCTool, then import the file again.")

_KINDS = ("import", "restore")
_VERSION = 1                            # of the pending / applying files
_CONFIG_FILE = FILE_NAMES["config"]
_sleep = time.sleep                     # between two tries for the apply lock (a test seam)
_clock = time.monotonic                 # ...and what bounds that wait in seconds (a seam too)
_HEX32 = re.compile(r"[0-9a-f]{32}")
_MORE_LINE = re.compile(r"and ([0-9]{1,12}) more")      # the line that ends a bounded list
_REASON_CLIP = 500
_REFUSAL_CLIP = 200                     # of the instruction a StateWriteError carries
_EDIT_TRIES = 25                        # more looks at the apply lock, within EDIT_WAIT
_MAX_WRITERS = 4                        # staging processes one start waits for, in all
_MAX_MODULES = 8                        # MSG_INCOMPLETE: module names shown...
_MAX_MODULE_NAME = 40                   # ...and how long one of them may be
_STORED_KEEP = (50, 10, 0)              # _kept: entries of one list, the first that fits...
_STORED_BYTES = 64 * 1024               # ...this many bytes of JSON for one stored report
_STORED_CLIP = 500                      # characters of one stored name or line
_UNLISTED = ("attention", "tasks")      # _kept: lists of an item that hold no text
_STATE_NAMES = tuple(FILE_NAMES.values())       # the files a record can be about (_held)
_BACKUP_CLIP = 64                       # ...and how long a backup's name in it may be
_TAG_LOG = 40                           # entries of the tag's history kept (_log_tag)
_CHOICE_CLIP = 200                      # a remembered name / local id longer is not kept
_CHOICES_BYTES = 4 * 1024 * 1024        # JSON of all remembered choices: the oldest groups
                                        # go until it fits (one full group always does)
_OFFERED = {True: ("theirs", "mine"),   # the choices a pair offers: an exact-name pair...
            False: ("both", "theirs", "mine")}      # ...and one of different names
_DEFAULT_WAIT = group_state.wait_for_exit       # apply_pending's own `wait` (_writer_gone)
_CONTINUE = object()                    # _resume: the leftover is gone, go on with step 2
_AGAIN = object()                       # _apply_locked: give the lock up and look again

_MSG_APPLIED = "Group configuration applied."
_MSG_NOT_RECORDED = "The import could not be recorded, so nothing was changed. Try again."
_MSG_NOT_COMPLETED = "The import could not be completed, so nothing was changed. Try again."
_MSG_UNCHANGED = "Nothing to change - your settings already match."
_MSG_NOTHING_APPLIED = "Nothing was changed - some items of this file could not be applied."
# The three "not now" sentences, in the words of what is staged: (import, restore).
_MSG_DEFERRED = ("Another FCTool window is still open - the import will be applied at the "
                 "next start.",
                 "Another FCTool window is still open - your previous settings will be "
                 "restored at the next start.")
_MSG_CHANGING = ("The staged import kept changing while FCTool was starting. It is applied "
                 "the next time FCTool starts.",
                 "The staged restore kept changing while FCTool was starting. Your previous "
                 "settings are restored the next time FCTool starts.")
_MSG_NOT_CLAIMED = ("The staged import could not be started - it will be tried at the next "
                    "start.",
                    "The staged restore could not be started - it will be tried at the next "
                    "start.")
_MSG_BUSY = ("Another FCTool window is applying the import right now.",
             "Another FCTool window is restoring your previous settings right now.")
_MSG_IN_USE = ("The group configuration files are in use by another program - the staged "
               "work will be tried again at the next start.")
_MSG_FILES_IN_USE = ("Your settings files are in use by another program right now. FCTool "
                     "will try again at the next start.")
_MSG_ITEM_IN_USE = "%s is in use by another program - try again"    # plan_import, per item
_MSG_DAMAGED = "The staged import was damaged and has been discarded; nothing was changed."
# A FOLDER that holds something sits where a protocol file belongs: nothing here deletes
# what is in a folder, so "discarded" would be untrue - the user has to remove it.
_MSG_PENDING_STUCK = ("The staged import was damaged and could not be removed - delete '%s' "
                      "in FCTool's folder." % group_state.PENDING_FILE)
_MSG_LEFTOVER_STUCK = (" Its leftover could not be removed - delete '%s' in FCTool's folder."
                       % group_state.APPLYING_FILE)
_MSG_WAITS_BEHIND = " The staged import is applied once it is gone."
_MSG_DAMAGED_RESTORE = ("The staged restore was damaged and has been discarded; nothing was "
                        "changed.")
_MSG_RECORD_DAMAGED = ("An import was interrupted and its recovery record was damaged. If your "
                       "settings look wrong, restore this backup.")
_MSG_CONFIG = "config.json could not be read; nothing was changed."
_MSG_CONFIG_ITEM = "%s could not be read" % _CONFIG_FILE     # the engine's own wording
_MSG_INTERRUPTED = "The import was interrupted; your previous settings were restored."
_MSG_INTERRUPTED_EARLY = "The import was interrupted before it changed anything."
_MSG_RESTORED = "Your previous settings were restored."
_MSG_RESTORE_UNRECORDED = ("Your previous settings were restored, but that could not be "
                           "recorded yet - it will be finished at the next start.")
_MSG_RESTORED_UNSAVED = "Your previous settings were restored, but that could not be recorded."
_MSG_NO_BACKUP = "The backup to restore no longer exists; nothing was changed."
_MSG_BAD_BACKUP = ("This backup is damaged or was made by another FCTool version; nothing "
                   "was changed.")
_MSG_CRASHED_IMPORT = "The import could not be applied"
_MSG_CRASHED_RESTORE = "Your previous settings could not be restored"
_STATUS_MESSAGES = {                    # _stored_report: a stored report without a message
    "applied": _MSG_APPLIED, "unchanged": _MSG_UNCHANGED, "restored": _MSG_RESTORED,
    "interrupted_restored": _MSG_INTERRUPTED, "deferred": _MSG_IN_USE,
    "failed": _MSG_CRASHED_IMPORT + "."}
_LOG_CLEANUP = "group import: the applying file could not be removed - cleanup is pending"


# -- small helpers -----------------------------------------------------------

def _quiet(call, *args, default=None):
    """``call(*args)``, or ``default`` when it raises."""
    try:
        return call(*args)
    except Exception:
        return default


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _no_meta() -> dict:
    return {"id": "", "name": "", "revision": 0}


def _meta_dict(source) -> dict:
    """``{id, name, revision}`` from a ``BundleMeta`` or from a dict out of a file. Total."""
    if isinstance(source, dict):
        get = source.get
    else:
        def get(field):
            return getattr(source, field, None)
    bundle_id, revision = get("id"), get("revision")
    return {"id": bundle_id if isinstance(bundle_id, str) and _HEX32.fullmatch(bundle_id) else "",
            "name": clean_text(get("name")),
            "revision": revision if type(revision) is int and revision >= 0 else 0}


def _peek_meta(protocol) -> dict:
    """Which group file a pending or applying file is about, without validating it."""
    if not isinstance(protocol, dict):
        return _no_meta()
    meta = protocol.get("meta")
    if not isinstance(meta, dict):
        envelope = protocol.get("bundle")
        meta = envelope.get("bundle") if isinstance(envelope, dict) else None
    return _meta_dict(meta) if isinstance(meta, dict) else _no_meta()


def _token(protocol) -> str:
    token = protocol.get("token") if isinstance(protocol, dict) else None
    return token if isinstance(token, str) and token else ""


def _kind(value) -> str:
    return value if isinstance(value, str) and value in _KINDS else "import"


def _worded(pair, kind) -> str:
    """The sentence of ``pair`` (import, restore) for what is staged."""
    return pair[1] if kind == "restore" else pair[0]


def _tag(value) -> str:
    """The ONE rule for a tag that comes in from anywhere - a file, the side file, a caller:
    ``group_bundle.clean_tag`` of a ``str``; anything that is no ``str`` is no tag."""
    return clean_tag(value) if isinstance(value, str) else ""


def _refusal(exc, plain="%s") -> str:
    """What a ``StateWriteError`` is about, for a message: ``plain % its file`` - or, when
    the error carries more than the standard "could not write <file>", the file and what it
    says, as one clean ASCII line ("backups: too many backups share one name - remove the
    newest folder in backups"). An instruction is no use when it stays in the exception."""
    name = clean_text(_text(getattr(exc, "file", "")))
    said = clean_text(_quiet(str, exc, default=""), _REFUSAL_CLIP)
    said = said.encode("ascii", "replace").decode("ascii")
    if not said or said in (name, "could not write %s" % name):
        return plain % name
    return "%s: %s" % (name, said) if name else said


def _known_version(protocol) -> bool:
    version = protocol.get("version") if isinstance(protocol, dict) else None
    return type(version) is int and version == _VERSION


def _in_use(protocol) -> bool:
    """``group_state`` read the file as "there, and cannot be opened right now"."""
    return isinstance(protocol, dict) and protocol.get("kind") == "busy"


def _as_options(options) -> Options:
    return options if isinstance(options, Options) else Options.from_json(options)


def _writer(staged):
    """Which process staged ``staged`` (a pending file's content) - ``(pid, start time)``,
    each ``None`` when it is no int - or ``None`` for content that names no kind of work:
    nothing is waited for then."""
    if not isinstance(staged, dict) or staged.get("kind") not in _KINDS:
        return None
    pid, started = staged.get("writer_pid"), staged.get("writer_started")
    return (pid if type(pid) is int else None, started if type(started) is int else None)


def _writer_gone(wait, staged) -> bool:
    """Ask ``wait`` whether the process that staged ``staged`` is gone: ``wait(pid,
    started)``, as ``apply_pending`` promises its caller. The DEFAULT wait
    (``group_state.wait_for_exit``) is handed two things more: the image name that was
    staged with the work (``writer_image``; a pending file of an older build has none) -
    by which a process that cannot be opened is told from a stranger that was given the
    writer's pid - and this module's ``_sleep``. A wait that raises counts as "gone": a
    probe that fails is "not alive" (``group_state``)."""
    pid, started = staged.get("writer_pid"), staged.get("writer_started")
    try:
        if wait is _DEFAULT_WAIT:
            return bool(wait(pid, started, image=staged.get("writer_image"), sleep=_sleep))
        return bool(wait(pid, started))
    except Exception:
        return True


class IncompleteBuildError(StateWriteError):
    """``stage_import`` on a build that is missing item modules: nothing was staged. Its
    text is the sentence to show (``stage_error_text`` hands it on)."""


def _incomplete() -> str:
    """``""`` - or, when item modules of this build failed to load (a broken frozen build),
    the sentence that says so: such a build applies no group file. The registry is loaded
    first, so the answer is about THIS process. The names are module names out of the code,
    cleaned all the same (ASCII identifiers, bounded) - never text out of a file. Total."""
    try:
        group_items.registry()
        failures = group_items.LOAD_FAILURES
        if not (isinstance(failures, (list, tuple)) and failures):
            return ""
        names = set()
        for entry in failures:
            name = entry[0] if isinstance(entry, (list, tuple)) and entry else None
            if isinstance(name, str) and name.isascii() and name.isidentifier() \
                    and len(name) <= _MAX_MODULE_NAME:
                names.add(name)
        names = sorted(names)
        more = len(names) - _MAX_MODULES
        shown = ", ".join(names[:_MAX_MODULES]) + (" and %d more" % more if more > 0 else "")
        return MSG_INCOMPLETE % (shown or "item modules")
    except Exception:
        return MSG_INCOMPLETE % "item modules"


def _cut(lines, keep) -> list:
    """A list of text cut to ``keep`` entries and one last ``"and <n> more"`` (a count line
    that is cut away is added up, not counted as one), each entry at most ``_STORED_CLIP``
    characters. A list that is short enough comes back entry for entry. Total: what is no
    list is an empty one, what is no text is left out."""
    lines = [line for line in lines if isinstance(line, str)] \
        if isinstance(lines, (list, tuple)) else []
    tail = lines[keep:]
    if tail and not (len(tail) == 1 and _MORE_LINE.fullmatch(tail[0])):
        more = 0
        for line in tail:
            counted = _MORE_LINE.fullmatch(line)
            more += int(counted.group(1)) if counted else 1
        lines = lines[:keep] + ["and %d more" % more]
    return [line[:_STORED_CLIP] for line in lines]


def _kept(report) -> dict:
    """``report`` as the side file keeps it (the module docstring says why): a copy whose
    lists of names and lines - ``added`` ... ``warnings`` of every item, ``problems``,
    ``log`` - are cut (``_cut``) to the first size of ``_STORED_KEEP`` with which the copy
    fits ``_STORED_BYTES``; at the last size every such list is its count line alone. An
    item's ``attention``, ``tasks`` and ``flags`` are kept as they are - a restore reads
    them. A report that is small anyway comes back equal. Total."""
    kept = dict(report)
    items = report.get("items")
    items = [item for item in items if isinstance(item, dict)] \
        if isinstance(items, (list, tuple)) else []
    for keep in _STORED_KEEP:
        kept["items"] = [{key: _cut(value, keep) if isinstance(value, (list, tuple))
                          and key not in _UNLISTED else value for key, value in item.items()}
                         for item in items]
        kept["problems"] = _cut(report.get("problems"), keep)
        kept["log"] = _cut(report.get("log"), keep)
        try:
            if len(json.dumps(kept, ensure_ascii=True)) <= _STORED_BYTES:
                break
        except Exception:               # not JSON at all: the write says so, not this
            break
    return kept


def _report(status, kind, message, *, meta=None, backup="", items=(), problems=()) -> dict:
    items = [item for item in items if isinstance(item, dict)]
    # The boot flags are for what IS in the files now: a failed run was taken back (or
    # never wrote), whatever its items say they did on the way.
    raised = [item["flags"] for item in items if isinstance(item.get("flags"), dict)] \
        if status != "failed" else []
    flags = {name: any(bool(item_flags.get(name)) for item_flags in raised) for name in FLAGS}
    return {"status": status, "kind": _kind(kind),
            "bundle": dict(meta) if isinstance(meta, dict) else _no_meta(),
            "at": _text(_quiet(group_state.utc_now_iso)),
            "backup": _text(backup), "items": items,
            "problems": [line for line in problems if isinstance(line, str)],
            "message": message, "flags": flags, "log": [], "seen": False}


def _stored_report(stored):
    """The side file's ``last_report`` as a well-formed report, or ``None`` when it names no
    status this build knows. A report this module stored comes back as it was stored. One
    that was edited by hand keeps its status and whatever has the right type; the rest is
    blank - the caller of ``apply_pending`` is never handed a dict it cannot read."""
    status = stored.get("status") if isinstance(stored, dict) else None
    if not (isinstance(status, str) and status in STATUSES):
        return None

    def lines(key, wanted):
        listed = stored.get(key)
        return [line for line in listed if isinstance(line, wanted)] \
            if isinstance(listed, list) else []

    bundle, flags, message = stored.get("bundle"), stored.get("flags"), stored.get("message")
    flags = flags if isinstance(flags, dict) else {}
    return {"status": status, "kind": _kind(stored.get("kind")),
            "bundle": _meta_dict(bundle) if isinstance(bundle, dict) else _no_meta(),
            "at": _text(stored.get("at")), "backup": _text(stored.get("backup")),
            "items": lines("items", dict), "problems": lines("problems", str),
            "message": message if isinstance(message, str) and message
            else _STATUS_MESSAGES[status],
            "flags": {name: flags.get(name) is True for name in FLAGS},
            "log": lines("log", str), "seen": False}


def _logged(report, line) -> dict:
    """A copy of ``report`` whose ``log`` (made a list of text, whatever was stored) ends
    with ``line``."""
    lines = report.get("log")
    lines = [text for text in lines if isinstance(text, str)] if isinstance(lines, list) else []
    return dict(report, log=lines + [line])


def _clean_up(app_dir, report) -> dict:
    """Delete the applying file and return ``report``. When it could not be removed the
    report's ``log`` says so: the next start finds it again - and, the run being marked
    ``done`` by then, removes it instead of "restoring" anything."""
    if _quiet(group_state.finish_applying, app_dir) is not True:
        report["log"] = _logged(report, _LOG_CLEANUP)["log"]
    return report


# -- the side file -----------------------------------------------------------

def _side_tag(side) -> str:
    return _tag(side.get("tag")) if isinstance(side, dict) else ""


def _store_tag(side, tag) -> None:
    if tag:
        side["tag"] = tag
    else:
        side.pop("tag", None)


def _edit_side(app_dir, change, *, same=False, busy=None) -> bool:
    """Read the side file, let ``change(side)`` edit it, write it back. ``change`` returns
    ``False`` for "nothing to write" - the answer is ``same`` then. True when the file was
    written; False when it could not be. Never raises.

    A side file that is in use is NOT written (the strict read raises ``StateBusyError``):
    what would go back is the empty one with this single edit in it. ``busy``, a list,
    gets one entry then - the caller knows that the file was looked at and could not be
    read, and need not look again.

    All of it happens inside ``group_state.apply_lock`` - and not at all while another
    FCTool holds that lock: an apply that finishes between this read and this write would
    lose its history entry, its tag, its ``done`` token and its report to the older copy.
    The lock is asked for ``_EDIT_TRIES`` more times within ``EDIT_WAIT`` seconds (of
    ``_sleep``, or on ``_clock``, whichever is reached first): it is held for moments only -
    by another helper, a stage, a claim - and an edit that gave up at the first look was an
    edit lost. Short, because the callers are on the Tk thread."""
    try:
        root = os.fspath(app_dir)
        if not isinstance(root, str) or not root:
            return False                # no folder: no lock to wait for
        began = _clock()
        for look in range(_EDIT_TRIES + 1):
            with group_state.apply_lock(root) as held:
                if held:
                    try:
                        side = group_state.read_sidecar(root, strict=True)
                    except StateBusyError:
                        if isinstance(busy, list):
                            busy.append(True)
                        return False
                    if change(side) is False:
                        return same
                    return bool(group_state.write_sidecar(root, side))
            if look == _EDIT_TRIES or _clock() - began >= EDIT_WAIT:
                return False
            _sleep(EDIT_WAIT / _EDIT_TRIES)
    except Exception:
        pass
    return False


def _entry(kept, **extra) -> dict:
    """The history entry of a stored report: the report without ``seen``, and ``extra``."""
    entry = {key: value for key, value in kept.items() if key != "seen"}
    entry.update(extra)
    return entry


def _remember(app_dir, report, *, token="", side=None, change=None) -> bool:
    """Keep ``report`` - its bounded copy, ``_kept`` - as the side file's ``last_report``
    (after ``change(side, that copy)``), and ``token`` as the finished run. Best effort:
    False when it could not be saved - also when no ``side`` read earlier was handed in and
    the file is in use right now."""
    try:
        if side is None:
            side = group_state.read_sidecar(app_dir, strict=True)
        kept = _kept(report)
        if change is not None:
            change(side, kept)
        side["last_report"] = kept
        if token:
            side["done"] = token
        return bool(group_state.write_sidecar(app_dir, side))
    except Exception:
        return False


def _finish(app_dir, report, *, token="", side=None, change=None) -> dict:
    """The end of a run: remember the report, delete the applying file, return the report."""
    _remember(app_dir, report, token=token, side=side, change=change)
    return _clean_up(app_dir, report)


def _clean_attention(entry):
    """``{section, item_id, reason}`` from one stored or reported entry, or ``None``."""
    if not isinstance(entry, dict):
        return None
    section = clean_text(_text(entry.get("section")))
    if not section:
        return None
    return {"section": section, "item_id": clean_text(_text(entry.get("item_id"))),
            "reason": clean_text(_text(entry.get("reason")), _REASON_CLIP)}


def _merge_attention(stored, incoming) -> list:
    """One entry per ``(section, item_id)``; a newer one replaces the older and goes last."""
    merged = [entry for entry in map(_clean_attention, stored) if entry is not None]
    for entry in map(_clean_attention, incoming):
        if entry is None:
            continue
        pair = (entry["section"], entry["item_id"])
        merged = [old for old in merged if (old["section"], old["item_id"]) != pair]
        merged.append(entry)
    return merged


def _merge_tasks(stored, incoming) -> list:
    """Add ``incoming`` tasks. At most ONE ``verify_structure`` task exists: a new one
    replaces any older one and keeps its ``attempts`` only for the same structure. A task
    that is already listed word for word is not listed twice. And the list is bounded:
    ``MAX_TASKS`` at most - the oldest tasks go first, the structure check never (it is
    the one task the app acts on today, and there is only one of it)."""
    merged = [task for task in stored if isinstance(task, dict)]
    for task in incoming:
        if not isinstance(task, dict):
            continue
        task = dict(task)
        if task.get("kind") == VERIFY_TASK:
            attempts = 0
            for old in merged:
                tried = old.get("attempts")
                if old.get("kind") == VERIFY_TASK \
                        and old.get("structure_id") == task.get("structure_id") \
                        and type(tried) is int and tried > 0:
                    attempts = tried
            merged = [old for old in merged if old.get("kind") != VERIFY_TASK]
            task["attempts"] = attempts
        elif task in merged:
            continue
        merged.append(task)
    spare = len(merged) - MAX_TASKS
    for task in list(merged) if spare > 0 else ():
        if spare > 0 and task.get("kind") != VERIFY_TASK:
            merged.remove(task)
            spare -= 1
    return merged


# -- remembered doctrine choices (spec D7) -----------------------------------
#
# The side file's ``doctrine_choices``: ``{bundle id: {incoming doctrine name: {"action",
# "local_id"}}}`` - per group file (its ``bundle.id``) the per-doctrine choices that an
# import HONOURED, oldest group first and, within one, oldest choice first. Written only
# inside an import's own side-file change (``_import``: step 8 for an unchanged run, step
# 11 with the history - same lock, same all-or-nothing); a restore leaves it alone.
# ``plan_matches`` hands them back for the pairs they still fit.

def _clean_choice(value):
    """``{"action", "local_id"}`` out of a stored or staged choice, or ``None``."""
    if not isinstance(value, dict):
        return None
    action, local_id = value.get("action"), value.get("local_id")
    if not (isinstance(action, str) and action in group_items.RESOLUTION_ACTIONS
            and isinstance(local_id, str) and 0 < len(local_id) <= _CHOICE_CLIP):
        return None
    return {"action": action, "local_id": local_id}


def _choice_name(name) -> bool:
    return isinstance(name, str) and 0 < len(name) <= _CHOICE_CLIP


def _side_choices(side) -> dict:
    """The side file's ``doctrine_choices`` as clean copies, in their order; whatever is
    not a group id with at least one well-formed choice is no entry. Total."""
    stored = side.get(CHOICES_KEY) if isinstance(side, dict) else None
    groups = {}
    for bundle_id, choices in stored.items() if isinstance(stored, dict) else ():
        if not (isinstance(bundle_id, str) and _HEX32.fullmatch(bundle_id)
                and isinstance(choices, dict)):
            continue
        kept = {}
        for name, value in choices.items():
            choice = _clean_choice(value)
            if choice is not None and _choice_name(name):
                kept[name] = choice
        if kept:
            groups[bundle_id] = kept
    return groups


def _bounded_choices(groups) -> dict:
    """``groups`` cut to the newest ``MAX_CHOICE_GROUPS`` groups of at most ``MAX_CHOICES``
    choices each (the newest), then - oldest group first - to ``_CHOICES_BYTES`` of JSON:
    the side file always reads back (``group_state.write_sidecar``). One full group of the
    longest names and ids is ~2.4 MB as JSON, so the newest group always stays."""
    groups = {bundle_id: dict(list(choices.items())[-MAX_CHOICES:])
              for bundle_id, choices in list(groups.items())[-MAX_CHOICE_GROUPS:]}
    sizes = {bundle_id: len(json.dumps({bundle_id: choices}, ensure_ascii=True))
             for bundle_id, choices in groups.items()}
    total = sum(sizes.values())
    for bundle_id in list(groups):
        if total <= _CHOICES_BYTES:
            break
        del groups[bundle_id]
        total -= sizes[bundle_id]
    return groups


def _put_choices(side, bundle_id, made) -> bool:
    """Remember ``made`` - ``(honoured, decided)`` of ``_choices_made``, or ``None`` - for
    the group ``bundle_id`` in ``side``. That group's older choices for the doctrines this
    import merged (``decided``) go; the ``honoured`` ones are added as its newest, and the
    group becomes the newest one. A key that held anything else is repaired on the way.
    -> True when ``side`` changed (and must be written)."""
    if made is None or not (isinstance(bundle_id, str) and _HEX32.fullmatch(bundle_id)):
        return False
    honoured, decided = made
    groups = _side_choices(side)
    choices = {name: choice for name, choice in groups.pop(bundle_id, {}).items()
               if name not in decided}
    choices.update(honoured)
    if choices:
        groups[bundle_id] = choices
    groups = _bounded_choices(groups)
    stored = side.get(CHOICES_KEY, None)
    if not groups:
        if CHOICES_KEY not in side:
            return False
        del side[CHOICES_KEY]
        return True
    # In order: a group that becomes the newest one is a change worth writing.
    try:
        if json.dumps(stored) == json.dumps(groups):
            return False
    except Exception:                   # what was stored is no JSON: replaced
        pass
    side[CHOICES_KEY] = groups
    return True


def _visible_ascii(text) -> str:
    return "".join(ch for ch in text if " " < ch <= "~")


def _may_name(name, line) -> bool:
    """Whether ``line`` - a fallback line of ``merge_share``, ``"<name as quoted>: <why> -
    <default> instead"`` - may be the one of the doctrine ``name``. Never False for the
    line of ``name`` (the quoted name drops characters - control and format ones, the
    blanks at its ends, everything after its 60th - but never one that is visible ASCII,
    and keeps the rest in order); now and then True for another doctrine's line, which
    leaves that choice unremembered: the safe side. No ``unicodedata`` here (purity)."""
    shown, said = _visible_ascii(name), _visible_ascii(line)
    common = 0
    while common < len(shown) and common < len(said) and shown[common] == said[common]:
        common += 1
    return ":" in said[:common + 1]


def _choices_made(results, payloads, options, aliases):
    """The doctrine choices this import HONOURED: ``(honoured, decided)`` - ``honoured``
    ``{incoming name: {"action", "local_id"}}``, ``decided`` the incoming names the
    doctrines item merged this time - or ``None`` when that item did not run (unticked,
    absent, ``failed`` or ``skipped``). Never raises (``None`` then).

    A staged choice is keyed by its record key; only a key that IS its doctrine's name (the
    first doctrine of a name) is ever handed to the store. It was honoured when it is not
    one of the store's fallbacks (``MergeResult.resolution_fallbacks``, in the item's
    ``warnings`` - ``_may_name``) AND it landed as that choice lands: "keep mine" leaves a
    keep-mine alias (``state.aliases``), "use the group's" an update (or nothing to update)
    without one, "keep both" a new doctrine without an alias. "merge" is not taken by this
    build (spec Phase 2) and is never remembered yet."""
    try:
        result = next((result for result in results
                       if getattr(result, "item_id", None) == DOCTRINES_ITEM), None)
        if result is None or result.status in ("failed", "skipped"):
            return None
        landed = {name: kind for kind in ("added", "updated", "unchanged", "skipped")
                  for name in getattr(result, kind) if isinstance(name, str)}
        decided = set(landed)
        staged = options.resolutions.get(DOCTRINES_ITEM) \
            if isinstance(options.resolutions, dict) else None
        honoured = {}
        if not isinstance(staged, dict) or not staged:
            return honoured, decided
        item = group_items.get_item(DOCTRINES_ITEM)
        names = {record.key for record in item.records(payloads.get(DOCTRINES_ITEM))
                 if record.key == record.label}
        fallen = [line for line in result.warnings
                  if isinstance(line, str) and line.endswith(" instead")]
        aliases = aliases if isinstance(aliases, dict) else {}
        for name, raw in staged.items():
            choice = _clean_choice(raw)
            if choice is None or name not in names or name not in decided \
                    or not _choice_name(name) \
                    or any(_may_name(name, line) for line in fallen):
                continue
            keep_mine = getattr(aliases.get(name), "keep_mine", None)
            action = choice["action"]
            if action == "mine":
                taken = keep_mine is True
            elif action == "theirs":
                taken = keep_mine is not True and landed[name] in ("updated", "unchanged")
            elif action == "both":
                taken = name not in aliases and landed[name] == "added"
            else:
                taken = False
            if taken:
                honoured[name] = choice
        return honoured, decided
    except Exception:
        return None


def _remembered(app_dir, bundle, pairs) -> dict:
    """``plan_matches``' ``remembered``: the stored choices of this group file for the
    ``pairs`` they still fit - the same local doctrine, and an action that pair offers
    (``_OFFERED``; "keep both" only for a pair of different names, "merge" not yet). A
    side file that cannot be read now remembers nothing. Reads only."""
    bundle_id = _meta_dict(getattr(bundle, "meta", None))["id"]
    if not bundle_id or not pairs:
        return {}
    stored = _side_choices(_quiet(group_state.read_sidecar, app_dir)).get(bundle_id, {})
    found = {}
    for key, pair in pairs.items():
        choice = stored.get(key)
        if choice is not None and choice["local_id"] == getattr(pair, "local_id", None) \
                and choice["action"] in _OFFERED[getattr(pair, "exact", None) is True]:
            found[key] = dict(choice)
    return found


# -- the group tag -----------------------------------------------------------

def _tag_result(file_tag, options, current):
    """The ``meta.tag`` pseudo-result, or ``None`` when the import leaves the tag alone (the
    file has none, or its row is unticked)."""
    tag = _tag(file_tag)
    if not tag or not options.apply_tag:
        return None
    result = ItemResult(TAG_ITEM)
    if tag != current:
        result.status = "applied"
        result.notes.append("by %s -> by %s" % (current, tag) if current else "by %s" % tag)
    return result


def get_tag(app_dir) -> str:
    """The group tag shown under the logo; ``""`` for none and for a damaged side file."""
    return _side_tag(_quiet(group_state.read_sidecar, app_dir))


def set_tag(app_dir, tag) -> str:
    """Store the tag (cleaned by ``group_bundle.clean_tag``: at most ``group_bundle.MAX_TAG``
    characters, nothing invisible; blank - and anything that is no ``str`` - clears it).

    -> the cleaned tag when it is stored now (or is the stored one already). When it could
    not be stored - the side file cannot be written, or another FCTool is applying an
    import at this moment - the answer is what ``get_tag`` would read: the OLD tag, or
    ``""`` when the side file cannot even be read right now (in use). So
    ``set_tag(x) != clean_tag(x)`` means "not saved".

    The side file is read ONCE for all of that. This runs on the Tk thread, and a file
    that another program holds costs every read most of a second: the old tag is what the
    edit's own read saw, and a read that was refused is not tried a second time."""
    cleaned = _tag(tag)
    seen, busy = [], []

    def change(side):
        seen.append(_side_tag(side))
        if seen[0] == cleaned and (cleaned or "tag" not in side):
            return False
        held = _held(side)              # a tag typed by hand goes into the tag's history:
        events = _tag_events(side, held)        # a later restore leaves it alone (_restore)
        _store_tag(side, cleaned)
        _log_tag(side, events, _live(side, held), cleaned)

    if _edit_side(app_dir, change, same=True, busy=busy):
        return cleaned
    if seen:                            # read, and not written: the tag is the old one
        return seen[0]
    if busy:                            # there, and not to be read now: as get_tag says
        return ""
    return get_tag(app_dir)             # the lock was never ours: nothing was read yet


# -- staging -----------------------------------------------------------------

def plan_import(app_dir, bundle, options, *, ctx=None):
    """What applying ``bundle`` would do to the files as they are now.
    -> ``([ItemResult, ...], problems)``. Runs the real apply code on a copy; writes nothing.

    With a ``config.json`` that cannot be read the apply changes nothing at all (step 6), so
    every result is ``skipped`` with that reason here, and the problems start with it.

    A state file that is merely IN USE by another program is shown the same way - the plan
    never waits or defers - with the reason "<file> is in use by another program - try
    again": for ``config.json`` on every result, for another file on the items that name it
    (the apply itself would wait for the next start then; see ``apply_pending``).

    And the same on a build that is missing item modules (``group_items.LOAD_FAILURES``):
    every result ``skipped`` with ``MSG_INCOMPLETE``, which is the first problem as well -
    such a build applies nothing, so the plan promises nothing."""
    options = _as_options(options)
    busy = []
    state = group_state.load_state(app_dir, busy_out=busy)
    payloads, problems = group_items.validate_bundle(bundle, ctx)
    results = group_items.preview(state, payloads, options, ctx)
    tag_result = _tag_result(getattr(getattr(bundle, "meta", None), "tag", ""), options,
                             get_tag(app_dir))
    if tag_result is not None:
        results.append(tag_result)
    incomplete = _incomplete()
    if incomplete:                      # a build without all its items applies nothing
        results = [ItemResult(result.item_id, status="skipped", warnings=[incomplete])
                   for result in results]
        return results, [incomplete] + list(problems)
    if "config" in busy:
        results = [ItemResult(result.item_id, status="skipped",
                              warnings=[_MSG_ITEM_IN_USE % _CONFIG_FILE]) for result in results]
        return results, [_MSG_FILES_IN_USE] + list(problems)
    for index, result in enumerate(results):
        item = group_items.get_item(result.item_id)
        held = [key for key in getattr(item, "files", ()) if key in busy]
        if held:
            results[index] = ItemResult(result.item_id, status="skipped", warnings=[
                _MSG_ITEM_IN_USE % FILE_NAMES.get(key, key) for key in held])
    if not isinstance(state.config, dict):
        results = [ItemResult(result.item_id, status="skipped", warnings=[_MSG_CONFIG_ITEM])
                   for result in results]
        problems = [_MSG_CONFIG] + list(problems)
    return results, problems


DOCTRINES_ITEM = "fittings.doctrines"   # the one item whose pairs the window asks about


def _no_matches() -> dict:
    return {"pairs": {}, "remembered": {}}


def plan_matches(app_dir, bundle, *, ctx=None) -> dict:
    """The group file's doctrines that look like ones the importer already has, for the
    window to ask about next to ``plan_import``. -> ``{"pairs": {record_key: Pair},
    "remembered": {record_key: {"action", "local_id"}}}`` (``remembered``: the choices an
    earlier import of this group - same ``bundle.id`` - honoured, for the pairs they still
    fit: same local doctrine, an action the pair offers; ``_remembered``).

    Writes nothing and never raises: a build that is missing item modules, a
    ``config.json`` or library that cannot be read or is in use right now, no library at
    all (nothing to pair with) and any exception all give the empty answer. Whether the
    library is one this build can pair against at all (a newer format) is the doctrines
    item's own question, answered in its ``matches``."""
    try:
        if _incomplete():
            return _no_matches()
        busy = []
        state = group_state.load_state(app_dir, busy_out=busy)
        if {"config", "fittings"} & set(busy) or not isinstance(state.config, dict) \
                or not isinstance(state.fittings, dict):
            return _no_matches()
        payloads, _problems = group_items.validate_bundle(bundle, ctx)
        # both were built for this call and are dropped after it: nothing to copy
        pairs = group_items.matches(state, payloads, ctx, copy=False).get(DOCTRINES_ITEM)
        found = _no_matches()
        if isinstance(pairs, dict):
            found["pairs"] = {key: pair for key, pair in pairs.items() if isinstance(key, str)}
            found["remembered"] = _quiet(_remembered, app_dir, bundle, found["pairs"],
                                         default={})
        return found
    except Exception:
        return _no_matches()


def stage_error_text(exc) -> str:
    """The sentence the window shows for an exception of ``stage_import`` / ``stage_restore``:
    ``MSG_TOO_BIG`` (``group_state.StateTooBigError``), ``MSG_APPLYING`` (another FCTool
    holds the apply lock at this moment), ``MSG_INCOMPLETE`` with the missing modules
    (``IncompleteBuildError``: this build cannot apply a group file) or ``MSG_NOT_STAGED``
    (anything else). Total."""
    if isinstance(exc, StateTooBigError):
        return MSG_TOO_BIG
    if isinstance(exc, StateApplyingError):
        return MSG_APPLYING
    if isinstance(exc, IncompleteBuildError):
        return _quiet(str, exc, default="") or MSG_INCOMPLETE % "item modules"
    return MSG_NOT_STAGED


def _stage(app_dir, kind, writer_pid, work) -> None:
    """Write the pending file; ``work()`` gives the keys of this kind. Whatever goes wrong
    is a ``StateWriteError`` and nothing was staged. One that ``group_state`` raises passes
    through AS IT IS - its class (``StateTooBigError``) and its message ("an import is being
    applied ...") say what happened; only other exceptions - an argument that is no group
    file - are wrapped."""
    try:
        pid = os.getpid() if writer_pid is None else writer_pid
        # pid, start time and image name: together they tell the process that staged this
        # from one that was handed its pid later (group_state._writer_alive).
        data = {"version": _VERSION, "kind": kind, "writer_pid": pid,
                "writer_started": group_state.process_started(pid),
                "writer_image": _quiet(group_state.process_image, pid),
                "staged_at": group_state.utc_now_iso(), "token": uuid.uuid4().hex}
        data.update(work())
        group_state.write_pending(app_dir, data)
    except StateWriteError:
        raise
    except Exception as exc:
        raise StateWriteError(group_state.PENDING_FILE) from exc


def stage_import(app_dir, bundle, options, *, writer_pid=None) -> None:
    """Stage ``bundle`` for the next start, replacing anything staged before. Nothing is
    applied here. Raises ``group_state.StateWriteError`` - and nothing else - when the work
    could not be staged; ``stage_error_text`` turns it into the sentence to show. On a
    build that is missing item modules nothing is staged (``IncompleteBuildError``, a
    ``StateWriteError``): the next start would only clear it again."""
    incomplete = _quiet(_incomplete, default=MSG_INCOMPLETE % "item modules")
    if incomplete:
        raise IncompleteBuildError(group_state.PENDING_FILE, incomplete)
    _stage(app_dir, "import", writer_pid,
           lambda: {"bundle": group_bundle.to_envelope(bundle),
                    "options": _as_options(options).to_json()})


def stage_restore(app_dir, backup_name, *, writer_pid=None) -> None:
    """Stage putting the backup ``backup_name`` back at the next start. Raises
    ``group_state.StateWriteError`` - and nothing else - when it could not be staged."""
    _stage(app_dir, "restore", writer_pid, lambda: {"backup": backup_name})


# -- putting a backup back ---------------------------------------------------

_NOT_LISTED = object()                  # _backup_files: the listing does not show the backup
_LISTED_IN_USE = object()               # ...or shows it with a manifest that is in use


def _backup_files(app_dir, name):
    """The file names backup ``name`` holds, ``_LISTED_IN_USE`` or ``_NOT_LISTED``."""
    for backup in group_state.list_backups(app_dir, include_busy=True):
        if backup.get("name") == name:
            if backup.get("busy"):
                return _LISTED_IN_USE
            return [entry["name"] for entry in backup["files"]]
    return _NOT_LISTED


def _put_back(app_dir, name):
    """Restore backup ``name``. -> ``(outcome, names not restored, names restored)`` with
    ``outcome`` = ``ok`` (every file is as it was), ``partial``, ``gone`` or ``damaged``.
    Total.

    ``gone`` only when the listing does not show the backup AND ``restore_backup`` says so
    itself (``FileNotFoundError``: no such backup, no manifest in it). ``damaged`` when
    ``restore_backup`` read the manifest and found it unusable
    (``group_state.BackupDamagedError`` - one entry that is not what a backup of this build
    holds is enough): nothing was touched, and no later start will fare better. Everything
    else that is not a complete restore is ``partial`` - the backup is still there and the
    next start tries again: a manifest that is in use, a backup that was just listed and is
    "not found" a moment later, and a listing that does not show a backup which restores all
    the same (the folder could not be listed for a moment).

    ``ok`` means what it says: ``group_state`` uses a manifest whole or not at all, so the
    files that were wanted are all the files the backup holds."""
    if not (isinstance(name, str) and name):
        return "gone", [], []
    try:
        wanted = _backup_files(app_dir, name)
        if wanted is _LISTED_IN_USE:
            return "partial", [], []
        try:
            restored = group_state.restore_backup(app_dir, name)
        except group_state.BackupDamagedError:
            return "damaged", [], []
        except FileNotFoundError:
            return ("gone" if wanted is _NOT_LISTED else "partial"), [], []
        if wanted is _NOT_LISTED:
            wanted = _backup_files(app_dir, name)
        if not isinstance(wanted, list):        # restored, but how much of it is not known
            return "partial", [], []
    except Exception:                           # StateBusyError among them
        return "partial", [], []
    missing = [file_name for file_name in wanted if file_name not in restored]
    return ("partial" if missing else "ok"), missing, \
        [file_name for file_name in wanted if file_name in restored]


def _unrestored(app_dir, protocol, kind, meta, backup, why, outcome, missing, *, items=(),
                problems=(), side=None) -> dict:
    """A backup could not be put back completely. Keep the applying file, so the next start
    tries again - unless the backup is gone, is damaged (no later start can read it either)
    or ``RESTORE_ATTEMPTS`` starts have tried. An import that is given up for good leaves a
    ``failed`` history entry naming its backup: the files are a mix, and that backup is the
    way out (History can offer it; a damaged one it can only name)."""
    files = ", ".join(missing) if missing else "some files"
    tried = protocol.get("restore_attempts") if isinstance(protocol, dict) else None
    tried = tried + 1 if type(tried) is int and tried >= 0 else 1
    if outcome == "gone":
        message = "%s, and the backup of your settings is missing." % why
    elif outcome == "damaged":
        message = ("%s, and the backup of your settings is damaged or was made by another "
                   "FCTool version - your settings may be partly changed; its files are in "
                   "the backups folder (%s)." % (why, backup))
    elif tried >= RESTORE_ATTEMPTS:
        message = "%s; %s could not be put back - the copies are in the backups folder (%s)." \
                  % (why, files, backup)
    else:
        message = "%s; %s could not be put back yet - it will be tried again at the next start." \
                  % (why, files)
    report = _report("failed", kind, message, meta=meta, backup=backup, items=items,
                     problems=problems)
    if outcome in ("gone", "damaged") or tried >= RESTORE_ATTEMPTS \
            or not isinstance(protocol, dict):
        change = None
        if outcome != "gone" and kind == "import" and backup:
            def change(side, kept):     # the tag was never touched; `under`: what every
                side["imports"] = (side["imports"] + [_entry(     # file holds - see _held
                    kept, tag="", tag_before="", under=_held(side))])[-HISTORY_KEEP:]
        return _finish(app_dir, report, token=_token(protocol), side=side, change=change)
    kept = dict(protocol)
    kept["restore_attempts"] = tried
    _quiet(group_state.update_applying, app_dir, kept)
    _remember(app_dir, report, side=side)   # no `done` token: this run is not finished
    return report


# -- apply_pending -----------------------------------------------------------

def apply_pending(app_dir, *, ctx=None, wait=group_state.wait_for_exit):
    """Apply the staged work, if any (the module docstring lists the steps). -> the report
    dict, or ``None`` when nothing was staged. Never raises.

    ``wait(pid, started) -> bool`` says whether the process that staged the work is gone.
    It is called with the pid and start time out of the pending file, BEFORE the apply lock
    is taken and never under it - it may take its time (``group_state.wait_for_exit``:
    eight seconds) or answer at once. ``False`` -> ``deferred``, the work stays staged.
    """
    # Literals only out here: nothing outside the `try` can raise.
    txn = {"kind": "import", "meta": {"id": "", "name": "", "revision": 0}, "claimed": False,
           "backup": "", "protocol": None, "wrote": False, "committed": False,
           "recorded": False, "items": [], "problems": []}
    try:
        return _apply(app_dir, ctx, wait, txn)
    except Exception as exc:            # raised outside the lock: nothing was claimed yet
        return _crashed(app_dir, txn, exc)


def _crashed(app_dir, txn, exc) -> dict:
    """The last line of defence: something raised that should not have. Never raises."""
    try:
        return _crash_report(app_dir, txn, type(exc).__name__)
    except Exception:
        pass
    # Not even the report could be built: literals and built-ins only from here on.
    restore = txn.get("kind") == "restore"
    flags = {"coalitions_seeded": False, "motd_disarm": False}
    if txn.get("committed") is True and (restore or txn.get("recorded") is True):
        status = "restored" if restore else "applied"
        message = _MSG_RESTORED if restore else _MSG_APPLIED
        try:                            # the files are new: this boot needs the boot flags
            for item in txn.get("items") or ():
                raised = item.get("flags") if isinstance(item, dict) else None
                for name in flags:
                    if isinstance(raised, dict) and raised.get(name):
                        flags[name] = True
        except Exception:
            pass
    else:
        status = "failed"
        message = (_MSG_CRASHED_RESTORE if restore else _MSG_CRASHED_IMPORT) + "."
    return {"status": status, "kind": "restore" if restore else "import",
            "bundle": {"id": "", "name": "", "revision": 0}, "at": "", "backup": "",
            "items": [], "problems": [], "message": message, "flags": flags, "log": [],
            "seen": False}


def _crash_report(app_dir, txn, what) -> dict:
    """What a run that raised ``what`` left behind, said in the words of its kind."""
    kind, meta, protocol = _kind(txn["kind"]), txn["meta"], txn["protocol"]
    restore = kind == "restore"
    if txn["committed"] and restore:    # the files are what the report says they are
        return _finish(app_dir, _report("restored", kind, _MSG_RESTORED, meta=meta,
                                        backup=txn["backup"]), token=_token(protocol))
    if txn["committed"] and txn["recorded"]:
        # New files AND their record: the import is complete, step 11 stored its report and
        # the `done` token. With its items - their boot flags count at this boot.
        return _clean_up(app_dir, _report("applied", kind, _MSG_APPLIED, meta=meta,
                                          backup=txn["backup"], items=txn["items"],
                                          problems=txn["problems"]))
    if txn["committed"]:
        # New files and NO record - no history entry, tag, task or attention flag. All or
        # nothing: the files go back, as after a side file that could not be saved. The run
        # is not marked `done`: should its applying file stay, the next start puts the
        # (same) files back once more instead of trusting this one.
        backup = txn["backup"]
        outcome, missing, _files = _put_back(app_dir, backup)
        if outcome != "ok":             # the applying file stays: the next start restores
            return _unrestored(app_dir, protocol, kind, meta, backup,
                               "The import could not be completed", outcome, missing,
                               items=txn["items"], problems=txn["problems"])
        return _finish(app_dir, _report("failed", kind, _MSG_NOT_COMPLETED, meta=meta,
                                        backup=backup, items=txn["items"],
                                        problems=txn["problems"]))
    why = "%s (%s)" % (_MSG_CRASHED_RESTORE if restore else _MSG_CRASHED_IMPORT, what)
    if restore and txn["claimed"]:
        # It may have put SOME files back before it died: the applying file stays, and the
        # next start goes on with it - counted, like every restore that could not finish.
        name = protocol.get("backup") if isinstance(protocol, dict) else ""
        return _unrestored(app_dir, protocol, kind, meta, _text(name), why, "partial", [])
    backup = txn["backup"]
    if txn["wrote"] and backup:
        outcome, missing, _files = _put_back(app_dir, backup)
        if outcome != "ok":
            return _unrestored(app_dir, protocol, kind, meta, backup, why, outcome, missing)
        message = "%s; your previous settings were restored." % why
    else:
        message = "%s; nothing was changed." % why
    report = _report("failed", kind, message, meta=meta, backup=backup)
    if txn["claimed"]:
        return _finish(app_dir, report, token=_token(protocol) if backup else "")
    return report


def _apply(app_dir, ctx, wait, txn):
    """Wait for the process that staged the work, then do the work inside the folder's
    apply lock.

    The wait comes FIRST and outside the lock (step 3): it can take seconds, and a lock held
    that long shuts out every side-file helper of the FCTool that is closing and makes a
    second start give up. ``gone`` remembers the writers that were waited for; under the
    lock the pending file is read again (``_apply_locked``), and when it names a writer
    that is not among them - staged anew meanwhile - the lock is given up and that writer
    is waited for, ``_MAX_WRITERS`` times at most. A build that is missing item modules
    waits for nobody: it clears the staged import under the lock.

    A wait that answers "still running" ends the start with ``deferred`` - unless the work
    is gone by then (the FCTool that staged it has cancelled it meanwhile): then there is
    nothing to promise, and the look starts over. A start that meets a new writer every
    time it has waited for one gives up after ``_MAX_WRITERS`` and says that.

    While another FCTool holds the lock, ask again every ``POLL_S`` seconds - and start
    from scratch once it is free: what was staged may be done by then. As long as there is
    staged work to do - a pending file OR an applying file - the holder is another start
    that is about to claim, or is writing: it is waited for, ``APPLYING_WAIT`` seconds at
    most, then the answer is ``deferred`` with nothing touched. ONE limit: no helper holds
    the lock for seconds, so a shorter one for "no applying file yet" has no case - and a
    start that gave up before the holder's claim read the old files while the holder
    went on to write the new ones. With neither file there, there is nothing to wait
    for (``None``). Seconds of ``_sleep`` or seconds on ``_clock``, whichever is reached
    first: a look at a protocol file that is in use takes a second by itself; the count
    starts when the wait for the writer is over.

    The unlocked look reads the PENDING file first, then the applying file. A claim renames
    pending -> applying, so whoever misses the pending file finds the applying file; read
    the other way round, a start that straddles the claim sees neither and boots on the old
    files while the other FCTool is still writing the new ones.

    Whatever raises in there is dealt with in there: putting the backup back is part of the
    apply, and no other FCTool may claim or "resume" anything while it goes on."""
    waited, began, gone, looks, anew = 0.0, _clock(), set(), 0, 0
    while True:
        pending = group_state.read_pending(app_dir)
        leftover = group_state.read_applying(app_dir)
        if leftover is None and pending is None:
            return None                 # nothing staged, nothing left over
        asked = False
        writer = _writer(pending) if leftover is None else None
        if writer is not None and writer not in gone \
                and not (pending["kind"] == "import" and _incomplete()):
            kind, meta = pending["kind"], _peek_meta(pending)           # step 3
            txn["kind"], txn["meta"] = kind, meta
            if not _writer_gone(wait, pending):
                # Still running. The work may have been cancelled while this start waited:
                # "will be applied at the next start" is said only of work that is there.
                looks += 1
                if looks <= _MAX_WRITERS and group_state.read_pending(app_dir) is None:
                    continue
                return _report("deferred", kind, _worded(_MSG_DEFERRED, kind), meta=meta)
            if len(gone) >= _MAX_WRITERS:       # each one gone, and always another: enough
                return _report("deferred", kind, _worded(_MSG_CHANGING, kind), meta=meta)
            gone.add(writer)
            asked = True
            waited, began = 0.0, _clock()       # the wait for the LOCK starts here
        again = False
        with group_state.apply_lock(app_dir) as held:
            if held:
                try:
                    report = _apply_locked(app_dir, ctx, txn, gone)
                except Exception as exc:
                    return _crashed(app_dir, txn, exc)
                if report is not _AGAIN:
                    return report
                again = True
        if again:                       # another writer: asked with the lock given up
            looks += 1
            anew += asked               # its writer was asked about - and it was another's
            if looks > _MAX_WRITERS:    # staged again and again: the next start's business
                # What stopped this start is said as it was: the work was staged anew
                # between two looks - or no writer was ever asked, because a protocol
                # file read "in use" outside the lock at every look.
                kind = _kind(txn["kind"])
                return _report("deferred", kind,
                               _worded(_MSG_CHANGING, kind) if anew else _MSG_IN_USE,
                               meta=txn["meta"])
            continue
        # Held elsewhere, and there IS staged work (a pending or an applying file was read
        # above): another start is before its claim, or writing. One limit for both.
        if waited >= APPLYING_WAIT or _clock() - began >= APPLYING_WAIT:
            staged = pending if leftover is None else leftover
            kind = _kind(staged.get("kind"))
            return _report("deferred", kind, _worded(_MSG_BUSY, kind), meta=_peek_meta(staged))
        _sleep(group_state.POLL_S)
        waited += group_state.POLL_S


def _apply_locked(app_dir, ctx, txn, gone):
    """The work under the lock. ``gone``: the writers ``_apply`` waited for. ``_AGAIN``
    when the pending file names one that is not among them."""
    leftover = group_state.read_applying(app_dir)                       # step 1
    if leftover is not None:
        if _in_use(leftover):           # a dead run may be behind it: never cleared unread
            return _report("deferred", "import", _MSG_IN_USE)
        report = _resume(app_dir, leftover, txn)
        if report is not _CONTINUE:
            return report

    pending = group_state.read_pending(app_dir)                         # step 2
    if pending is None:
        return None
    if _in_use(pending):
        return _report("deferred", "import", _MSG_IN_USE)
    if pending.get("kind") in _KINDS:
        txn["kind"], txn["meta"] = pending["kind"], _peek_meta(pending)
        incomplete = _incomplete() if pending["kind"] == "import" else ""
        if incomplete:
            # This build cannot apply it, and no later start of it can: the staged import
            # is cleared - not claimed, nothing written - and the report says what to do.
            _quiet(group_state.cancel_pending, app_dir)
            return _report("failed", "import", incomplete, meta=txn["meta"])
        if _writer(pending) not in gone:                                # step 3, checked
            return _AGAIN               # staged anew while this start waited for another
    try:                                # the history, the tag, `done`: BEFORE the claim
        side = group_state.read_sidecar(app_dir, strict=True)
    except StateBusyError:
        return _report("deferred", txn["kind"], _MSG_IN_USE, meta=txn["meta"])
    state, busy = None, []
    if pending.get("kind") in _KINDS:
        # The five state files, read BEFORE the claim as well: one of them that the staged
        # work needs and that another program holds open right now is no reason to fail and
        # discard the import (or to skip one item and apply the rest) - nothing is claimed,
        # nothing is touched, and the next start finds it all as it is.
        state = group_state.load_state(app_dir, busy_out=busy)
        if busy and not _files_needed(app_dir, pending).isdisjoint(busy):
            return _report("deferred", txn["kind"], _MSG_FILES_IN_USE, meta=txn["meta"])

    claimed = group_state.claim_pending(app_dir)                        # step 4
    if claimed is None:
        if group_state.read_pending(app_dir) is None:
            return None                 # cancelled meanwhile
        return _report("deferred", txn["kind"], _worded(_MSG_NOT_CLAIMED, txn["kind"]),
                       meta=txn["meta"])
    if _in_use(claimed):                # nothing was renamed
        return _report("deferred", txn["kind"], _MSG_IN_USE, meta=txn["meta"])
    if claimed.get("kind") in _KINDS and not _token(claimed):
        # A hand-made file. Without a token its run could never be marked `done` - and the
        # token has to be in the applying file ON DISK too: a restore, and an import that
        # changes no data file, never rewrite it, and a leftover without the token would be
        # done a second time at the next start, over whatever the user changed meanwhile.
        claimed = dict(claimed, token=uuid.uuid4().hex)
        _quiet(group_state.update_applying, app_dir, claimed)
    txn.update(claimed=True, protocol=claimed, kind=_kind(claimed.get("kind")),
               meta=_peek_meta(claimed))
    if claimed.get("kind") == "restore":                                # step 5
        return _restore(app_dir, claimed, txn, side)
    if claimed.get("kind") == "import":
        return _import(app_dir, claimed, ctx, txn, side, state)
    # Not usable. A file was renamed by the claim and goes with the applying file; a FOLDER
    # in the pending file's place was left where it is - an empty one is removed here, or
    # every start would report it again (the lock is ours: nothing new can be staged). One
    # that holds something cannot be removed, and the report must not say it was.
    _quiet(group_state.cancel_pending, app_dir)
    stuck = _quiet(group_state.read_pending, app_dir) is not None
    return _finish(app_dir, _report("failed", "import",
                                    _MSG_PENDING_STUCK if stuck else _MSG_DAMAGED), side=side)


def _files_needed(app_dir, staged) -> set:
    """The keys of the state files the staged work cannot do without. A restore: the files
    its backup holds (none when the backup cannot be listed - ``_put_back`` deals with
    that). An import: the files of every selected item - and ``config``, always: step 6
    fails the whole import without it."""
    if staged.get("kind") == "restore":
        held = _backup_files(app_dir, staged.get("backup"))
        held = held if isinstance(held, list) else ()
        return {key for key, name in FILE_NAMES.items() if name in held}
    needed = {"config"}
    options = staged.get("options")
    selected = options.get("selected") if isinstance(options, dict) else None
    for item in group_items.registry() if isinstance(selected, dict) else ():
        if item.id in selected:
            needed.update(item.files)
    return needed


def _resume(app_dir, leftover, txn):
    """Step 1: an applying file is there although no apply is running."""
    token = _token(leftover)
    kind = leftover.get("kind")
    meta = _peek_meta(leftover)
    txn.update(kind=_kind(kind), meta=meta)
    try:
        side = group_state.read_sidecar(app_dir, strict=True)
    except StateBusyError:
        # Finished or interrupted? `done` cannot be read right now, and taking it for
        # "interrupted" would put the backup back over a completed import.
        return _report("deferred", kind, _MSG_IN_USE, meta=meta)
    if kind not in _KINDS:              # it was read, and holds nothing usable
        return _record_damaged(app_dir, side)
    if token and side.get("done") == token:
        # That run finished - its report is in the side file. Only its applying file could
        # not be deleted: putting the backup back now would undo a completed import.
        removed = group_state.finish_applying(app_dir) is True
        stored = side.get("last_report")
        last = _stored_report(stored) \
            if isinstance(stored, dict) and stored.get("seen") is False else None
        if last is not None:
            return last if removed else _logged(last, _LOG_CLEANUP)
        if removed:
            return _CONTINUE
        waiting = group_state.read_pending(app_dir)
        if waiting is None:
            return None
        # Staged work waits behind a leftover that cannot be removed (a claim never replaces
        # an applying file): say so at every start, instead of nothing at all.
        return _logged(_report("deferred", waiting.get("kind"), _MSG_IN_USE,
                               meta=_peek_meta(waiting)), _LOG_CLEANUP)
    if kind == "restore":               # the restore the user asked for: do it now
        txn.update(claimed=True, protocol=leftover)
        return _restore(app_dir, leftover, txn, side)
    backup = leftover.get("backup")
    if not (isinstance(backup, str) and backup):
        # No backup recorded -> no state file was written (the backup comes first).
        return _finish(app_dir, _report("failed", "import", _MSG_INTERRUPTED_EARLY, meta=meta),
                       side=side)
    applier, started = leftover.get("applier_pid"), leftover.get("applier_started")
    if type(applier) is int and type(started) is int and applier != os.getpid() \
            and _quiet(group_state.process_started, applier) == started:
        # Not a dead run: another FCTool is writing these files at this moment.
        return _report("deferred", "import", _worded(_MSG_BUSY, "import"), meta=meta,
                       backup=backup)
    outcome, missing, _files = _put_back(app_dir, backup)
    if outcome == "ok":
        return _finish(app_dir, _report("interrupted_restored", "import", _MSG_INTERRUPTED,
                                        meta=meta, backup=backup), token=token, side=side)
    return _unrestored(app_dir, leftover, "import", meta, backup,
                       "The import was interrupted", outcome, missing, side=side)


def _record_damaged(app_dir, side) -> dict:
    """An applying file whose content is not usable. Which backup it named is lost with it,
    and the state files may be half written - so before it is cleared, the NEWEST backup
    (the backup made last, in use or not) goes into the report and into the history as a
    ``failed`` entry - but only when it can be the dead run's own. A backup that a history
    entry already names belongs to a run that FINISHED: then the dead run made none, wrote
    no state file, and "restore this backup" would take back the wrong import. The same
    without any backup at all.

    It is cleared - when it can be. A FOLDER that holds something in the applying file's
    place cannot (nothing here deletes what is in a folder), and it blocks every claim:
    then the report says so, says which name to delete, and that staged work waits behind
    it - at every start, until it is gone."""
    newest = ""
    try:
        listed = group_state.list_backups(app_dir, include_busy=True)
        newest = _text(listed[0].get("name")) if listed else ""
    except Exception:
        pass
    change = None
    if not newest or any(entry.get("backup") == newest for entry in side["imports"]):
        report = _report("failed", "import", _MSG_INTERRUPTED_EARLY)
    else:
        report = _report("failed", "import", _MSG_RECORD_DAMAGED, backup=newest)

        def change(side, kept):
            side["imports"] = (side["imports"] + [_entry(
                kept, tag="", tag_before="", under=_held(side))])[-HISTORY_KEEP:]

    report = _finish(app_dir, report, side=side, change=change)
    if report["log"]:                   # it is still there: the report must not hide that
        report["message"] += _MSG_LEFTOVER_STUCK
        if _quiet(group_state.read_pending, app_dir) is not None:
            report["message"] += _MSG_WAITS_BEHIND
        _remember(app_dir, dict(report, log=[]), side=side)     # what is kept says the same
    return report


def _added_attention(entry) -> set:
    """The ``(section, item_id)`` pairs the import of history ``entry`` flagged."""
    pairs = set()
    listed = entry.get("items")
    for item in listed if isinstance(listed, list) else ():
        flagged = item.get("attention") if isinstance(item, dict) else None
        for flag in map(_clean_attention, flagged if isinstance(flagged, list) else ()):
            if flag is not None:
                pairs.add((flag["section"], flag["item_id"]))
    return pairs


def _set_the_tag(entry) -> bool:
    """Whether the import of history ``entry`` changed the group tag."""
    listed = entry.get("items")
    return entry.get("status") == "applied" and any(
        isinstance(item, dict) and item.get("item_id") == TAG_ITEM
        and item.get("status") == "applied"
        for item in (listed if isinstance(listed, list) else ()))


# -- the record: what each file holds, and who set the tag --------------------------------
#
# Which imports are in effect after a restore is RECORDED, never worked out afterwards:
#
# * ``side["on_disk"]`` - state file name -> the backups of the imports whose change that
#   file holds, oldest first (a backup's name stands for its import);
# * ``entry["under"]`` - in the history entry of an import: for each file it changed, that
#   list as it was just before (for a ``failed`` entry: of every file - which files its
#   backup holds is not known where it is written);
# * ``side["tag_log"]`` - the tag's history, oldest first: ``{"tag", "by"}``, ``by`` the
#   backup of the import that set it, ``""`` for a tag typed by hand (or set by an import
#   that changed no file).
#
# An import pushes its backup onto the lists of the files it changed; a restore puts
# ``under`` back for the files its backup held. An import is IN EFFECT while one of the
# files it changed still holds its change: the ``restored`` marks, the import "in use"
# (the newest in effect) and the tag (the last entry of the log that counts: typed by
# hand, or set by an import in effect) all follow from that - in any order of restores,
# with any number of imports. An import that changed no file is never "restored".
#
# A side file an earlier build wrote has none of the three, and it keeps working: an import
# without ``under`` is marked restored when its OWN backup is put back and not otherwise;
# the files that backup put back count as holding no recorded change from then on (they are
# as they were before any record began); and the tag's history is read out of the history
# entries (``tag`` / ``tag_before``), an import counting while it is not marked restored.
# Every new import and restore records, so such a file grows into a recorded one.

def _held(side) -> dict:
    """``side["on_disk"]``, cleaned: state file name -> backups, oldest first (the newest
    ``HISTORY_KEEP`` of them). Total: only the five file names, only short text."""
    stored, held = side.get("on_disk"), {}
    for name in _STATE_NAMES if isinstance(stored, dict) else ():
        stack = stored.get(name)
        if isinstance(stack, list):
            held[name] = [backup for backup in stack
                          if isinstance(backup, str) and 0 < len(backup) <= _BACKUP_CLIP
                          ][-HISTORY_KEEP:]
    return held


def _on_disk(entry, held):
    """Does one of the files the import of ``entry`` changed still hold its change?
    ``None`` for an entry that does not say which files it changed: an import that changed
    none (a tag alone), an entry of an earlier build."""
    under, backup = entry.get("under"), entry.get("backup")
    if not isinstance(under, dict) or not under or not (isinstance(backup, str) and backup):
        return None
    return any(backup in held.get(name, ()) for name in under if isinstance(name, str))


def _live(side, held) -> set:
    """The backups of the imports in effect: those a file's record holds, and those of the
    applied history entries that are not marked restored (an entry without a record has
    only its mark to go by)."""
    live = {backup for stack in held.values() for backup in stack}
    for entry in side["imports"]:
        backup = entry.get("backup")
        if entry.get("status") == "applied" and not entry.get("restored") \
                and isinstance(backup, str) and backup:
            live.add(backup)
    return live


def _tag_of(events, live) -> str:
    """The tag a log says is in use: that of its last entry that counts."""
    tag = ""
    for event in events:
        if not event["by"] or event["by"] in live:
            tag = event["tag"]
    return tag


def _tag_events(side, held) -> list:
    """The tag's history as ``[{"tag", "by"}, ...]`` - ``side["tag_log"]``, cleaned; for a
    side file without one, read out of the history entries: before every import that set
    the tag, the tag it found when no entry before it explains that one, then its own.

    And always true to the tag that IS in use: when the log does not arrive at it - it was
    changed where nothing is logged - that tag is the newest entry, as typed by hand."""
    stored = side.get("tag_log")
    if isinstance(stored, list):
        events = [{"tag": _tag(event.get("tag")),
                   "by": event["by"] if isinstance(event.get("by"), str) else ""}
                  for event in stored[-_TAG_LOG:] if isinstance(event, dict)]
    else:
        events, last = [], ""
        for entry in side["imports"]:
            if _set_the_tag(entry):
                before, backup = _tag(entry.get("tag_before")), entry.get("backup")
                if before != last:
                    events.append({"tag": before, "by": ""})
                last = _tag(entry.get("tag"))
                events.append({"tag": last, "by": backup if isinstance(backup, str) else ""})
    current = _side_tag(side)
    if _tag_of(events, _live(side, held)) != current:
        events.append({"tag": current, "by": ""})
    return events


def _log_tag(side, events, live, tag=None, by="") -> None:
    """Store the tag's history - with one more entry, when ``tag`` is given: set by the
    import whose backup is ``by``, or typed by hand. A tag typed over a typed one replaces
    it; a log longer than ``_TAG_LOG`` loses its oldest entries to ONE that says what they
    come to (with the imports in effect now)."""
    if tag is not None:
        if not by and events and not events[-1]["by"]:
            events.pop()
        events.append({"tag": tag, "by": by})
    spare = len(events) - _TAG_LOG
    if spare > 0:
        events[:spare + 1] = [{"tag": _tag_of(events[:spare + 1], live), "by": ""}]
    side["tag_log"] = events


def _take_back(side, entry, files, stamp, backup="") -> list:
    """The backup of history ``entry`` was put back - ``files`` are as they were just
    before that import. Each of them holds again what was under that import (``under``;
    nothing recorded, for an entry that does not say), and every applied import is
    ``restored`` or not by whether a file still holds its change - ALL of them, earlier and
    later ones: one that went with its last file is marked, one whose change is back in a
    file is un-marked. An entry without a record keeps the mark it has; ``entry`` itself is
    marked whatever it says. -> the entries that were in effect and are not any more,
    oldest first.

    ``entry`` is ``None`` for a backup the history does not know (its entry was deleted by
    hand): then ``backup``, its name, is all there is to go by. A file whose list holds
    that name keeps what was under it - the list is cut there; a file whose list does not
    is left as it is, the backup says nothing about it.

    Three things are as they are on purpose. A ``failed`` entry keeps its ``restored``
    mark for good - it never was in effect, the mark only says its backup was put back.
    An import that comes back in effect gets its mark and its tag back, not the attention
    flags it raised or the structure check it queued: those were taken back with it, and
    are not raised a second time behind the user's back. And a side file an earlier build
    wrote, without the record, can show a mark that is out of date until the next import
    or restore writes one (the comment above ``_held``)."""
    listed = [old for old in side["imports"] if old.get("status") == "applied"]
    was = {id(old) for old in listed if not old.get("restored")}
    held = _held(side)
    under = entry.get("under") if entry is not None else None
    for name in files:
        stack = under.get(name) if isinstance(under, dict) else None
        if name not in _STATE_NAMES:
            continue
        if entry is None:               # the unknown backup: cut the list at its name
            listed_there = held.get(name, [])
            if backup in listed_there:
                held[name] = listed_there[:listed_there.index(backup)]
        else:
            held[name] = [below for below in stack if isinstance(below, str)
                          ][-HISTORY_KEEP:] if isinstance(stack, list) else []
    side["on_disk"] = held
    for old in listed:
        there = _on_disk(old, held)
        if there:
            old.pop("restored", None)
        elif there is False and not old.get("restored"):
            old["restored"] = stamp
    if entry is not None:
        entry["restored"] = stamp
    return [old for old in listed if id(old) in was and old.get("restored")]


def _newest_in_effect(side):
    """``_applied`` of the newest applied import in the history that is not restored, or
    ``None``."""
    for entry in map(_history_entry, reversed(side["imports"])):
        if entry["status"] == "applied" and not entry.get("restored"):
            return _applied(dict(entry["bundle"], tag=entry["tag"], at=entry["at"]))
    return None


def _restore(app_dir, protocol, txn, side) -> dict:
    """Step 5: put a backup back - and bring the side file's bookkeeping in line with what
    the files hold now. All of it follows from the RECORD (the comment above ``_held``):

    * the record of each restored file is what was under that import; every applied
      import is ``restored`` or not by whether a file still holds its change
      (``_take_back``). So a later import goes along when its last file went back, an
      earlier one comes back when a file holds its change again, and two restores add up;
    * what is in effect (``applied``, read by ``applied_bundle``) is the newest applied
      import of the history that is not restored - nothing, when there is none;
    * the tag is the one of the newest entry of the tag's history that counts: typed by
      hand, or set by an import that is in effect. So a tag goes back only with the import
      that set it - not because another import, one that is still in effect, set the same
      text - and a tag that was typed by hand after it stays;
    * and, when ``config.json`` is among the restored files, the attention flags of the
      imports that are no longer in effect and every ``verify_structure`` task (that
      worker would write the group's structure into the settings the user just took back).

    An import that changed no file - a tag alone - has no backup and is never restored.

    A backup that is damaged (``_put_back``) is not restored in part: nothing is changed,
    and the report says so."""
    token = _token(protocol)
    if not _known_version(protocol):
        return _finish(app_dir, _report("failed", "restore", _MSG_DAMAGED_RESTORE), side=side)
    name = protocol.get("backup")
    entry = None
    if isinstance(name, str) and name:
        for candidate in side["imports"]:
            if candidate.get("backup") == name:
                entry = candidate
    meta = _meta_dict(entry.get("bundle")) if entry is not None \
        and isinstance(entry.get("bundle"), dict) else _no_meta()
    txn["meta"] = meta
    outcome, missing, files = _put_back(app_dir, name)
    if outcome in ("gone", "damaged"):  # nothing was touched, and no later start can do it
        return _finish(app_dir, _report(
            "failed", "restore", _MSG_NO_BACKUP if outcome == "gone" else _MSG_BAD_BACKUP,
            meta=meta), side=side)
    if outcome != "ok":
        return _unrestored(app_dir, protocol, "restore", meta, name,
                           "Your previous settings could not be restored completely",
                           outcome, missing, side=side)
    txn.update(committed=True, backup=name)
    report = _report("restored", "restore", _MSG_RESTORED, meta=meta, backup=name)

    def change(side, _kept):
        config_back = _CONFIG_FILE in files
        if config_back:
            side["tasks"] = [task for task in side["tasks"] if task.get("kind") != VERIFY_TASK]
        events = _tag_events(side, _held(side))     # the tag's history, as it is BEFORE
        went = _take_back(side, entry, files, report["at"], _text(name))
        live = _live(side, side["on_disk"])
        side["applied"] = _newest_in_effect(side)
        _store_tag(side, _tag_of(events, live))
        _log_tag(side, events, live)
        if entry is not None and not any(old is entry for old in went):
            went.append(entry)                      # a failed one, or one long restored
        added = set().union(*map(_added_attention, went)) if config_back else ()
        if added:
            side["attention"] = [flag for flag in _merge_attention(side["attention"], ())
                                 if (flag["section"], flag["item_id"]) not in added]

    if _remember(app_dir, report, token=token, side=side, change=change):
        return _clean_up(app_dir, report)
    # The files are back and the side file is not: it still shows the import's tag, still
    # queues its verification, and the entry is not marked restored. So the applying file
    # stays and the next start repeats the restore - it changes no file then - and the
    # side file with it. Counted like every restore that could not finish.
    tried = protocol.get("restore_attempts")
    tried = tried + 1 if type(tried) is int and tried >= 0 else 1
    if tried >= RESTORE_ATTEMPTS:
        report["message"] = _MSG_RESTORED_UNSAVED
        return _clean_up(app_dir, report)
    _quiet(group_state.update_applying, app_dir, dict(protocol, restore_attempts=tried))
    report["message"] = _MSG_RESTORE_UNRECORDED
    return report


def _import(app_dir, claimed, ctx, txn, side, old=None) -> dict:
    """Steps 6 to 11. ``side`` is the side file and ``old`` the five state files, both as
    read before the claim (none of the files this import needs was in use then)."""
    token = _token(claimed)
    meta = txn["meta"]

    def failed(message, **extra):
        return _finish(app_dir, _report("failed", "import", message, meta=meta, **extra),
                       token=token if txn["backup"] else "", side=side)

    chosen = claimed.get("options")
    if not _known_version(claimed) or "backup" in claimed or "restore_attempts" in claimed \
            or not (isinstance(chosen, dict) and isinstance(chosen.get("selected"), dict)):
        # Not what stage_import writes. An unknown version may mean anything; `backup` or
        # `restore_attempts` would be "restored" after a crash; and without its selection
        # the file would apply nothing but its tag - over the user's own.
        return failed(_MSG_DAMAGED)
    try:                                                                # step 6
        bundle, _notes = group_bundle.from_envelope(claimed.get("bundle"))
    except BundleError as exc:
        return failed(str(exc))
    meta = txn["meta"] = _meta_dict(bundle.meta)
    options = Options.from_json(chosen)
    if old is None:
        old = group_state.load_state(app_dir)
    if not isinstance(old.config, dict):    # there, and not readable: no waiting helps
        return failed(_MSG_CONFIG)

    payloads, problems = group_items.validate_bundle(bundle, ctx)       # step 7
    new, results = group_items.apply_bundle(old.copy(), payloads, options, ctx)
    tag_before = _side_tag(side)
    file_tag = _tag(bundle.meta.tag)
    tag_result = _tag_result(file_tag, options, tag_before)
    if tag_result is not None:
        results.append(tag_result)
    items = [result.to_dict() for result in results]
    txn.update(items=items, problems=problems)      # for a report after a crash
    new_tag = file_tag if tag_result is not None and tag_result.status == "applied" else None
    new_tasks = [task for item in items for task in item["tasks"]]
    new_attention = [entry for item in items for entry in item["attention"]]
    changed = group_state.changed_files(old, new)
    made = _choices_made(results, payloads, options, getattr(new, "aliases", None))

    if not changed and new_tag is None and not new_tasks:               # step 8
        dirty = False
        if new_attention:               # a flag is worth keeping without any change
            flags = _merge_attention(side["attention"], new_attention)
            if flags != side["attention"]:
                side["attention"] = flags
                dirty = True
        # ... and so is a doctrine choice: "keep mine" everywhere changes no file, and
        # the next file of this group must offer it again (spec D7)
        if _put_choices(side, meta["id"], made):
            dirty = True
        if dirty:
            _quiet(group_state.write_sidecar, app_dir, side)
        stuck = any(item["status"] in ("failed", "skipped") for item in items)
        report = _report("unchanged", "import",
                         _MSG_NOTHING_APPLIED if stuck else _MSG_UNCHANGED,
                         meta=meta, items=items, problems=problems)
        if _quiet(group_state.finish_applying, app_dir) is not True:
            # The run is over and its applying file is still there: mark it `done` (the one
            # time an unchanged run touches the side file), or the next start would call it
            # "interrupted". The report was handed over just now: stored as seen.
            side["last_report"] = dict(_kept(report), seen=True)
            side["done"] = token
            _quiet(group_state.write_sidecar, app_dir, side)
            report["log"] = [_LOG_CLEANUP]
        return report

    backup = ""
    if changed:
        try:                                                            # step 9
            backup = group_state.make_backup(app_dir, changed, label=_backup_label(meta))
        except StateWriteError as exc:
            return failed("A backup of your settings could not be made (%s); nothing was "
                          "changed." % _refusal(exc), items=items, problems=problems)
        txn["backup"] = backup
        small = {"version": _VERSION, "kind": "import", "token": token,
                 "staged_at": _text(claimed.get("staged_at")), "meta": meta, "backup": backup,
                 "applier_pid": os.getpid(),
                 "applier_started": group_state.process_started(os.getpid())}
        try:
            group_state.update_applying(app_dir, small)
        except StateWriteError as exc:
            return failed("The import could not be started (%s); nothing was changed."
                          % _refusal(exc, "%s could not be written"), backup=backup,
                          items=items, problems=problems)
        txn["protocol"] = small
        txn["wrote"] = True             # from here on a failure puts the backup back
        try:                                                            # step 10
            group_state.write_state(app_dir, old, new)
        except StateWriteError as exc:
            # `wrote` stays set until the put-back is KNOWN to be complete: when it is not,
            # and something raises while that is written down, the crash handler must find
            # "files may be new" - or it reports "nothing was changed" over a mix, deletes
            # the applying file and marks the run done.
            why = _refusal(exc, "%s could not be written")
            outcome, missing, _files = _put_back(app_dir, backup)
            if outcome == "ok":
                txn["wrote"] = False
                return failed("%s; your previous settings were restored." % why,
                              backup=backup, items=items, problems=problems)
            return _unrestored(app_dir, small, "import", meta, backup, why, outcome, missing,
                               items=items, problems=problems, side=side)
        txn["wrote"] = False
        txn["committed"] = True

    report = _report("applied", "import", _MSG_APPLIED, meta=meta, backup=backup,   # step 11
                     items=items, problems=problems)

    def change(side, kept):
        side["attention"] = _merge_attention(side["attention"], new_attention)
        side["tasks"] = _merge_tasks(side["tasks"], new_tasks)
        # The record (the comment above `_held`): what each file this import changed held
        # before goes into its entry, its backup onto those files' lists, and the tag it
        # set into the tag's history. A restore - of this backup or any other - goes by it.
        held = _held(side)
        events = _tag_events(side, held)            # ...as it is BEFORE this import
        under = {FILE_NAMES[key]: list(held.get(FILE_NAMES[key], ())) for key in changed} \
            if backup else {}
        for name, stack in under.items():
            held[name] = (stack + [backup])[-HISTORY_KEEP:]
        side["on_disk"] = held
        entry = _entry(kept, tag=file_tag, tag_before=tag_before,
                       applied_before=_applied_now(side), under=under)
        side["imports"] = (side["imports"] + [entry])[-HISTORY_KEEP:]
        side["applied"] = {"name": meta["name"], "revision": meta["revision"],
                           "tag": file_tag, "at": _text(kept.get("at"))}
        if new_tag is not None:
            _store_tag(side, new_tag)
        _log_tag(side, events, _live(side, held), new_tag, backup)
        _put_choices(side, meta["id"], made)        # with the history: all or nothing

    if _remember(app_dir, report, token=token, side=side, change=change):
        txn["recorded"] = True          # new files AND their record: nothing takes it back
        _quiet(group_state.prune_backups, app_dir)
        return _clean_up(app_dir, report)
    # All or nothing. Without the side file the import has no history entry, no tag, no
    # queued task and no attention flag - nobody could see it or take it back.
    if not changed:                     # the tag and the tasks live in the side file only
        return _clean_up(app_dir, _report(
            "failed", "import", "The import could not be saved (%s could not be written); "
            "nothing was changed." % group_state.STATE_FILE,
            meta=meta, items=items, problems=problems))
    txn.update(committed=False, wrote=True)         # the data files are new: take them back
    outcome, missing, _files = _put_back(app_dir, backup)
    if outcome != "ok":
        # The applying file stays: the next start restores. And `wrote` stays set, as in
        # step 10 - a crash from here on must not be taken for "nothing was changed".
        return _unrestored(app_dir, small, "import", meta, backup,
                           "The import could not be recorded", outcome, missing,
                           items=items, problems=problems)
    txn["wrote"] = False
    # `side` holds the entry of an import that is no longer there: saved from a fresh read
    return _finish(app_dir, _report("failed", "import", _MSG_NOT_RECORDED, meta=meta,
                                    backup=backup, items=items, problems=problems),
                   token=token)


def _backup_label(meta) -> str:
    if meta["name"]:
        return "before importing %s r%d" % (meta["name"], meta["revision"])
    return "before a group import"


# -- attention, tasks, exports, report, history ------------------------------

def attention(app_dir) -> list:
    """The attention flags, oldest first: ``[{section, item_id, reason}, ...]``."""
    side = _quiet(group_state.read_sidecar, app_dir)
    return _side_attention(side) if isinstance(side, dict) else []


def add_attention(app_dir, section, item_id, reason) -> bool:
    """Flag a Settings section (its TOC title). One flag per ``(section, item_id)``.

    -> True when the side file holds the flag now (stored, or there already); False when
    it does not - no section, the side file in use or not writable, or another FCTool held
    the apply lock for all of ``EDIT_WAIT``. Like every helper here that stores something:
    the answer says whether it did."""
    entry = _clean_attention({"section": section, "item_id": item_id, "reason": reason})
    if entry is None:
        return False

    def change(side):
        merged = _merge_attention(side["attention"], [entry])
        if merged == side["attention"]:
            return False
        side["attention"] = merged

    return _edit_side(app_dir, change, same=True)


def clear_attention(app_dir, section) -> bool:
    """Drop every flag of ``section``. True when at least one was dropped (and saved)."""
    def change(side):
        flags = _merge_attention(side["attention"], ())
        kept = [entry for entry in flags if entry["section"] != section]
        if len(kept) == len(flags):
            return False
        side["attention"] = kept

    return _edit_side(app_dir, change)


def tasks(app_dir) -> list:
    """The work queued for after the restart (dicts with a ``kind``)."""
    side = _quiet(group_state.read_sidecar, app_dir)
    return _side_tasks(side) if isinstance(side, dict) else []


def set_tasks(app_dir, tasks) -> bool:
    """Replace the queued tasks. Entries that are not dicts are left out. -> True when the
    side file holds that list now, False when it could not be stored."""
    wanted = [task for task in tasks if isinstance(task, dict)] \
        if isinstance(tasks, (list, tuple)) else []

    def change(side):
        if side["tasks"] == wanted:
            return False
        side["tasks"] = wanted

    return _edit_side(app_dir, change, same=True)


def remember_export(app_dir, name):
    """-> ``(bundle id, revision)`` for a file saved under ``name``: the first use of a name
    gives a new id and revision 1, every later use the same id and the next revision.

    -> ``None`` when the pair could not be stored (the side file is in use or cannot be
    written, or another FCTool is applying an import at this moment). A pair that is not
    remembered is never handed out: it would be a new id for a name that has one, or the
    same revision for two different files. The caller says "try again".

    The newest ``MAX_EXPORTS`` names are remembered - newest by when they were last used. A
    name that fell out starts again as a new file (a new id, revision 1)."""
    key = clean_text(name)
    answer = [group_bundle.new_bundle_id(), 1]

    def change(side):
        exports = side["exports"] if isinstance(side.get("exports"), dict) else {}
        known = exports.pop(key, None)              # ...and back in at the end: the newest
        if isinstance(known, dict):
            bundle_id, revision = known.get("id"), known.get("revision")
            if isinstance(bundle_id, str) and _HEX32.fullmatch(bundle_id) \
                    and type(revision) is int and revision >= 1:
                answer[:] = [bundle_id, revision + 1]
        exports[key] = {"id": answer[0], "revision": answer[1]}
        for oldest in list(exports)[:max(0, len(exports) - MAX_EXPORTS)]:
            del exports[oldest]
        side["exports"] = exports

    if not _edit_side(app_dir, change):
        return None
    return answer[0], answer[1]


def mark_report_seen(app_dir) -> bool:
    """The last report was shown: it does not open again. -> True when the side file says
    so now (or holds no unseen report), False when that could not be stored - then the
    report opens once more at the next start."""
    def change(side):
        last = side.get("last_report")
        if not isinstance(last, dict) or last.get("seen") is True:
            return False
        last["seen"] = True

    return _edit_side(app_dir, change, same=True)


def _history_entry(entry) -> dict:
    """One stored history entry as the window may be handed it. The side file is a file
    anyone can edit: every key a report has comes out with its type - ``status`` one of
    ``STATUSES`` or, for anything else, ``""`` (text all the same; such an entry is shown,
    and nothing is offered for it), ``items`` a list of dicts, ``problems`` and ``log``
    lists of text, a ``bundle`` dict, text for ``at``, ``backup``, ``message`` and (when
    it is there) ``restored``, cleaned tags. Whatever else an entry holds
    (``applied_before``, a key of a later build) is kept as stored."""
    status = entry.get("status")
    known = isinstance(status, str) and status in STATUSES
    shaped = _stored_report(entry if known else dict(entry, status="failed"))
    del shaped["seen"]
    if not known:
        shaped.update(status="", message=_text(entry.get("message")))
    shown = dict(entry, **shaped)
    shown.pop("seen", None)
    shown.update(tag=_tag(entry.get("tag")), tag_before=_tag(entry.get("tag_before")))
    if "restored" in entry:
        shown["restored"] = _text(entry["restored"])
    return shown


def history(app_dir) -> list:
    """The applied imports, NEWEST first (at most ``HISTORY_KEEP``). Each entry is what the
    side file kept of the import's report (``_kept``: long lists end in ``"and <n> more"``)
    without ``seen``, plus ``tag`` (the file's tag), ``tag_before``, ``applied_before``
    (what was in effect before that import - ``applied_bundle``'s keys and ``at``, when it
    had been applied - or ``None``; kept for the record, no restore goes by it), ``under``
    (per file the import changed, what that file held before - the record a restore DOES
    go by, see ``_held``) and ``restored`` (when) - while no file holds that import's
    change: its own backup was put back, or another one's that took its last file along.
    The mark goes again when a later restore brings its change back.

    Entries with the status ``failed`` are imports whose files may be a mix - one that could
    not be put back after ``RESTORE_ATTEMPTS`` starts, one whose backup or recovery record
    was damaged. Their ``backup`` names the folder to restore by hand; their ``tag`` and
    ``tag_before`` are blank (they never touched the tag).

    The entries are copies, and well-formed whatever the file holds (``_history_entry``):
    ``tag`` and ``tag_before`` are always there and always cleaned, a status is always text
    (blank for one no build wrote), a list is always a list."""
    side = _quiet(group_state.read_sidecar, app_dir)
    if not isinstance(side, dict):
        return []
    return [_history_entry(entry) for entry in reversed(side["imports"])]


def _applied(value):
    """``{"name", "revision", "tag", "at"}`` out of a stored value, or ``None`` for what is
    no dict. Total: the parts are cleaned, whatever was stored. ``at`` is when that import
    was applied (its report's stamp), ``""`` when the value does not say."""
    if not isinstance(value, dict):
        return None
    name, revision = value.get("name"), value.get("revision")
    return {"name": clean_text(name) if isinstance(name, str) else "",
            "revision": revision if type(revision) is int and revision >= 0 else 0,
            "tag": _tag(value.get("tag")), "at": _text(value.get("at"))}


def _applied_now(side):
    """What is in effect according to ``side`` - ``{"name", "revision", "tag", "at"}`` or
    ``None``: its ``applied`` key - written by every import and every restore - or, for a
    side file that has none yet (an older build wrote it), the newest applied import of
    the history that was not restored since. An ``applied`` without its stamp gets the
    ``at`` of the newest applied history entry of that name and revision, if there is one."""
    if "applied" not in side:
        return _newest_in_effect(side)
    applied = _applied(side["applied"])
    if applied is not None and not applied["at"]:
        for entry in map(_history_entry, reversed(side["imports"])):
            if entry["status"] == "applied" and (applied["name"], applied["revision"]) \
                    == (entry["bundle"]["name"], entry["bundle"]["revision"]):
                applied["at"] = entry["at"]
                break
    return applied


# What the single readers answer for a side file as ``group_state.read_sidecar`` hands it
# out - one rule per key, shared with overview().

def _side_attention(side) -> list:
    return _merge_attention(side["attention"], ())


def _side_tasks(side) -> list:
    return list(side["tasks"])


def _side_applied(side):
    """``applied_bundle``'s answer: what is in effect, without its stamp."""
    applied = _applied_now(side)
    return None if applied is None else {key: applied[key]
                                         for key in ("name", "revision", "tag")}


def applied_bundle(app_dir):
    """``{"name", "revision", "tag"}`` of the import that is in effect, or ``None``: the
    import applied last - or, after a restore, the newest applied import of the history
    that is not restored. An import is in effect - not restored - while one of the files it
    changed still holds its change; which file holds what is recorded with every import
    and every restore (``_held``, ``_take_back``), so that holds for any order of restores."""
    side = _quiet(group_state.read_sidecar, app_dir)
    return _quiet(_side_applied, side) if isinstance(side, dict) else None


# -- fresh installs ----------------------------------------------------------

def _fresh_folder(app_dir, config):
    """The data folder as text while the first-run offer is still possible - the side file
    has the last word (``_side_fresh``) - or ``None`` when it is not, whatever that file
    holds: a tracked character, or no folder to look in."""
    tracked = config.get("tracked_character") if isinstance(config, dict) else None
    if tracked is not None and not (isinstance(tracked, str) and not tracked.strip()):
        return None
    root = os.fspath(app_dir)
    return root if isinstance(root, str) and root else None


def _side_fresh(root, side) -> bool:
    """``side`` was read STRICTLY: no import yet, the offer not dismissed - and no sign-in
    file in ``root`` (looked for by name, never opened)."""
    if side["imports"] or side.get("welcome_dismissed") is True:
        return False
    return not glob.glob(os.path.join(glob.escape(root), TOKEN_FILES))


def is_fresh(app_dir, config) -> bool:
    """True while the first-run offer may show (spec 9.1): no import yet, the offer not
    dismissed, no tracked character, no sign-in file. Reads only; never writes. The sign-in
    files are looked for by name - they are never opened. A side file that is in use may
    hold a whole history: then the answer is ``False`` - the offer is never shown on a
    guess."""
    try:
        root = _fresh_folder(app_dir, config)
        if root is None:
            return False
        side = group_state.read_sidecar(root, strict=True)      # StateBusyError -> False
        return _side_fresh(root, side)
    except Exception:
        return False


def overview(app_dir, config=None) -> dict:
    """What the main window asks at every start, from ONE read of the side file::

        {"tag": <get_tag(app_dir)>, "attention": <attention(app_dir)>,
         "tasks": <tasks(app_dir)>, "applied": <applied_bundle(app_dir)> + "at",
         "fresh": <is_fresh(app_dir, config)>, "welcome_dismissed": <bool>}

    Each value is what the single helper answers for the same file - the rules are shared,
    not repeated. ``applied`` is ``None`` or ``applied_bundle``'s three keys and ``at``: when
    that import was applied (the ``at`` of its report and history entry), ``""`` when
    nothing says. ``welcome_dismissed``: the first-run offer was closed.

    It takes no lock, writes nothing and never raises. The read is a strict one. A side
    file that is IN USE right now gives what the single helpers give then: no tag, no
    flags, no tasks, nothing applied - and ``fresh`` is ``False``, ``welcome_dismissed``
    ``False``: the file may hold a whole history, and a first-run offer is never made on a
    guess. A missing or damaged side file reads as the empty one (``fresh`` can be True
    then); a folder that is no path gives the blank answers with ``fresh`` False."""
    seen = {"tag": "", "attention": [], "tasks": [], "applied": None, "fresh": False,
            "welcome_dismissed": False}
    try:
        side = group_state.read_sidecar(app_dir, strict=True)
    except Exception:                   # StateBusyError: there, and not to be had now
        return seen
    if not isinstance(side, dict):
        return seen
    seen["tag"] = _quiet(_side_tag, side, default="")
    seen["attention"] = _quiet(_side_attention, side, default=[])
    seen["tasks"] = _quiet(_side_tasks, side, default=[])
    seen["applied"] = _quiet(_applied_now, side)
    seen["welcome_dismissed"] = side.get("welcome_dismissed") is True
    try:
        root = _fresh_folder(app_dir, config)
        seen["fresh"] = root is not None and _side_fresh(root, side)
    except Exception:
        pass
    return seen


def dismiss_welcome(app_dir) -> bool:
    """The first-run offer was closed: it stays away for good. -> True when the side file
    says so now, False when that could not be stored (the offer shows again next time)."""
    def change(side):
        if side.get("welcome_dismissed") is True:
            return False
        side["welcome_dismissed"] = True

    return _edit_side(app_dir, change, same=True)
