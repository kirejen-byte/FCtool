"""Self-update — fetch a newer FCTool build and get it safely onto disk.

``update_check`` answers "is there a newer release?" and stops there. This
module is the half that acts on the answer: preflight, download, verify,
extract (this file's Task-2 core), then the rename swap, the boot marker and
the relaunch watchdog that follow it.

Design rules, all load-bearing
------------------------------
* **Pure.** No UI import of any kind — this module must stay importable and
  testable with no display, on any platform, and the layering runs one way
  only: ``app_version`` <- ``update_check`` <- ``self_update`` <- the update
  dialog <- the app shell. A source-text test enforces it.
* **Total.** Every public function returns a value (a human-readable reason
  string, or ``None`` meaning "clear") for ANY input, and never raises. The
  caller runs on a worker thread and paints the result into a dialog; a
  traceback there would be a frozen dialog and a log line nobody reads.
* **Nothing is installed unless every check passed.** The order is
  deliberately paranoid — size, then GitHub's SHA-256, then the member set and
  the declared sizes read out of the central directory, then the zip's own
  CRC pass, then the PE header of what came out — and every failure deletes
  the partial file it produced. At no point is there anything half-written
  where a later step would find it, and nothing is ever inflated before the
  cheap checks have agreed that inflating it is safe.
* **Only our own files.** The updater reads and writes exactly
  ``<exe_dir>/FCTool.exe``, its ``.old``/``.failed`` siblings and
  ``<exe_dir>/updates/*``. It never touches config, tokens, caches or any
  other user data — and staging deliberately lives on the exe's OWN volume so
  the two renames of the swap are atomic renames, not copy-and-delete.
* **The remote name is data, not a path.** ``asset.name`` comes from GitHub;
  :attr:`InstallPlan.zip_name` refuses anything that is not a plain ``*.zip``
  file name and falls back to a fixed local one, so no archive name can steer
  a write out of the staging directory.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass
from typing import Callable

import requests

from app_io import atomic_write_json
from app_path import _is_dir_writable
from app_version import APP_VERSION, parse_version
from update_check import HEADERS, AssetInfo, _quiet_log

# The asset must be a plausible FCTool build: today's zip is ~53 MB, and both
# ends of this band are far enough away that a legitimate release will not
# drift into them, while a 404 HTML page or a 2 GB mistake cannot get through.
MIN_ASSET_BYTES = 20 * 2**20
MAX_ASSET_BYTES = 500 * 2**20
# Free space demanded on top of (zip + 2x exe): staging headroom plus enough
# slack that we never leave the user's volume completely full.
FREE_MARGIN_BYTES = 64 * 2**20
CHUNK = 256 * 1024
# No bytes at all for this long means the socket is dead in a way that read
# timeouts do not always catch (a trickle of keep-alives, a stuck CDN edge).
STALL_S = 30
# (connect, read) — the app-wide convention for every outbound call.
TIMEOUT = (10, 30)
# The one member we install: at the zip root, spelled exactly this, and the
# only member in the archive that may be a program at all.
EXE_MEMBER = "FCTool.exe"
# The published zip is FCTool.exe beside a clean config.json and a SETUP.txt.
# The cap leaves room for a couple more small siblings and is nowhere near an
# archive whose member LIST is itself the attack.
MAX_ZIP_MEMBERS = 16
# What the siblings may add on top of one build before the archive is refused.
# Today they are ~2 KB together, so this is pure slack — but it means no
# sibling can declare a gigabyte and get inflated on the strength of a
# well-behaved FCTool.exe.
SIBLING_SLACK_BYTES = 16 * 2**20
# Between the tries at removing a stale .old: a OneDrive/antivirus lock on a
# file nobody has mapped clears in milliseconds (the app_io replace pattern).
SWAP_BACKOFF_S = 0.08

_DIGEST_RE = re.compile(r"^sha256:([0-9a-fA-F]{64})$", re.IGNORECASE)
# A plain file name: no separators, no drive, no dots-dots, no absurd length.
_SAFE_ZIP_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}\.zip$", re.IGNORECASE)
# ...and Windows device names, which pass that pattern but are not files at
# all: opening "NUL.zip" writes to the null device, "COM1.zip" to a serial
# port. The extension is irrelevant to the rule — the STEM is the device.
_RESERVED_STEMS = frozenset(
    ("con", "prn", "aux", "nul")
    + tuple(f"com{d}" for d in "123456789")
    + tuple(f"lpt{d}" for d in "123456789"))
_FALLBACK_ZIP = "FCTool_update.zip"

#: ``progress_cb(done_bytes, total_bytes)`` — called on the worker thread.
ProgressCb = Callable[[int, int], None]


#: Every value :func:`run_install` can put in :attr:`InstallResult.stage`.
#: The first five name the step that FAILED; ``installed`` is the only one
#: that ever comes back with ``ok=True``; ``error`` is the catch-all for a
#: crash no branch anticipated. Deliberately not "install" — one character
#: from the success value is a trap for anything that matches on a prefix.
INSTALL_STAGES = ("preflight", "download", "verify", "extract", "swap",
                  "installed", "error")


@dataclass(frozen=True)
class InstallResult:
    """The outcome of one install attempt, as the dialog wants to show it.

    ``stage`` is one of :data:`INSTALL_STAGES` — the step that ended the
    install — and ``message`` is the human sentence to print. Defined here
    rather than beside the orchestration that returns it because both the
    swap layer and the dialog depend on the shape.
    """

    ok: bool
    stage: str
    message: str


@dataclass(frozen=True)
class InstallPlan:
    """Every path one install touches, derived once from the release.

    Deliberately a value: it is built on the UI thread, handed to a worker and
    read from both, so it must be immutable and free of live handles.
    """

    tag: str
    asset: AssetInfo
    exe_path: str
    staging_dir: str

    @property
    def zip_name(self) -> str:
        """The downloaded archive's LOCAL name.

        GitHub's asset name is used when it is a plain ``*.zip`` file name and
        replaced by a fixed one otherwise — a name is remote data and must
        never be able to point a write anywhere but at the staging dir, nor at
        a Windows device (``NUL.zip``, ``COM1.zip``) that is not a file.
        """
        try:
            raw = getattr(self.asset, "name", "") or ""
            name = os.path.basename(str(raw).strip())
            if not _SAFE_ZIP_RE.match(name):
                return _FALLBACK_ZIP
            if name.split(".", 1)[0].strip().lower() in _RESERVED_STEMS:
                return _FALLBACK_ZIP
            return name
        except Exception:
            return _FALLBACK_ZIP

    @property
    def zip_path(self) -> str:
        return os.path.join(self.staging_dir, self.zip_name)

    @property
    def part_path(self) -> str:
        """The in-flight download. Never renamed to ``.zip`` until complete."""
        return self.zip_path + ".part"

    @property
    def new_path(self) -> str:
        """The extracted exe, before the swap gives it the real name."""
        return os.path.join(self.staging_dir, EXE_MEMBER + ".new")

    @property
    def old_path(self) -> str:
        """The running exe after the swap renames it aside; still mapped."""
        return self.exe_path + ".old"

    @property
    def failed_path(self) -> str:
        """Where a new exe that would not boot is parked by the rollback."""
        return self.exe_path + ".failed"


def make_plan(tag, asset, exe_path) -> InstallPlan | None:
    """Build the :class:`InstallPlan` for ``asset``, or ``None`` if it can't.

    ``None`` covers every unusable input — no tag, an asset missing a name, a
    size or a URL, no exe path — because the caller's answer to all of them is
    the same: offer the release page instead. Staging is
    ``<exe folder>/updates`` so both renames of the swap stay on one volume.
    """
    try:
        if not isinstance(tag, str) or not tag.strip():
            return None
        if not isinstance(exe_path, str) or not exe_path.strip():
            return None
        name = getattr(asset, "name", None)
        size = getattr(asset, "size", None)
        url = getattr(asset, "url", None)
        if not isinstance(name, str) or not name.strip():
            return None
        if not isinstance(size, int) or isinstance(size, bool):
            return None
        if not isinstance(url, str) or not url.strip():
            return None
        exe = os.path.abspath(exe_path.strip())
        return InstallPlan(tag.strip(), asset, exe,
                           os.path.join(os.path.dirname(exe), "updates"))
    except Exception as exc:
        _quiet_log(f"make_plan {type(exc).__name__}: {exc}")
        return None


def parse_digest(digest) -> str | None:
    """Return the lowercase 64-hex sha256 out of ``"sha256:<hex>"``, else ``None``.

    GitHub computes this server-side at upload. Anything else — a different
    algorithm, a truncated hex run, an absent field on an old release — is not
    something we can check a download against, so it is ``None`` and the
    install is refused before a single byte is fetched.
    """
    try:
        if not isinstance(digest, str):
            return None
        match = _DIGEST_RE.match(digest.strip())
        return match.group(1).lower() if match else None
    except Exception:
        return None


def preflight(plan, *, frozen=None, platform=None, disk_usage=shutil.disk_usage) -> str | None:
    """Everything that must be true BEFORE we spend 53 MB of someone's data.

    Returns a sentence to show the user, or ``None`` when the install may
    proceed. Checked in this order on purpose — cheapest and most categorical
    first, so a source checkout never reaches a disk-space message:

    1. frozen build (a source install updates with ``git pull``);
    2. Windows (the rename-swap trick is a Windows behaviour);
    3. the exe's own folder is writable (a Program Files install is not);
    4. no stale ``FCTool.exe.old`` that refuses to be removed — that means a
       previous update is installed but not restarted, and its file is still
       the mapped image of a running process;
    5. free space for the zip plus two copies of the exe plus a margin;
    6. the asset is a plausible size and carries a checksum we can verify.

    The three environment seams are injected so the whole table is testable on
    any platform; each defaults to the live value.
    """
    try:
        if not isinstance(plan, InstallPlan):
            return "This release has nothing that can be installed automatically."

        if frozen is None:
            frozen = bool(getattr(sys, "frozen", False))
        if not frozen:
            return "Running from source — pull the repo instead."

        if platform is None:
            platform = sys.platform
        if platform != "win32":
            return "In-app updates are Windows-only — download the zip from the release page."

        exe_dir = os.path.dirname(plan.exe_path)
        if not _is_dir_writable(exe_dir):
            return ("FCTool cannot write to its own folder; "
                    "download the zip from the release page.")

        if os.path.exists(plan.old_path):
            try:
                os.remove(plan.old_path)
            except Exception:
                return "Restart FCTool to finish the previous update first."

        zip_size = _sane_size(getattr(plan.asset, "size", None))
        try:
            exe_size = os.path.getsize(plan.exe_path)
        except Exception:
            # An unreadable exe is not a reason to UNDER-estimate: the build we
            # are about to install is the best stand-in for the one on disk, so
            # the space demand stays honest instead of collapsing to the zip.
            exe_size = zip_size
        need = zip_size + 2 * exe_size + FREE_MARGIN_BYTES
        free = None
        try:
            free = int(disk_usage(exe_dir).free)
        except Exception:
            free = None      # an unreadable volume is not a reason to refuse
        if free is not None and free < need:
            return (f"Not enough disk space — {_mb(need)} MB needed, "
                    f"{_mb(free)} MB free.")

        if not MIN_ASSET_BYTES <= zip_size <= MAX_ASSET_BYTES:
            return (f"The release zip is an unexpected size ({_mb(zip_size)} MB) "
                    f"— download it from the release page.")

        if parse_digest(getattr(plan.asset, "digest", "")) is None:
            return "GitHub did not publish a checksum for this release."

        return None
    except Exception as exc:
        _quiet_log(f"preflight {type(exc).__name__}: {exc}")
        return "The update could not be prepared."


def download(url, part_path, expected_size, progress_cb, cancel_event, *,
             get=requests.get, now=time.monotonic) -> str | None:
    """Stream ``url`` into ``part_path``. Reason string, or ``None`` on success.

    Success means the file is complete and exactly ``expected_size`` bytes;
    the caller renames it to ``.zip`` itself. ANY other outcome — a wrong
    ``Content-Length``, a short or oversized body, a stalled socket, a
    cancelled dialog, a dead connection — deletes the partial file, so a retry
    always starts from a clean slate and no half-download can ever be mistaken
    for an archive.

    ``progress_cb(done, total)`` is throttled to ~10 Hz (a 53 MB download is
    ~200 chunks; a repaint per chunk would be pointless) and is always called
    once more with ``done == total`` on success. A callback that raises is
    swallowed: the dialog is not allowed to break the download.
    """
    if not isinstance(part_path, str) or not part_path.strip():
        return "The update has nowhere to download to."
    part = part_path
    if not isinstance(url, str) or not url.strip():
        return "This release has no download link."
    if (not isinstance(expected_size, int) or isinstance(expected_size, bool)
            or expected_size <= 0):
        return "GitHub did not publish a usable size for this release."
    if expected_size > MAX_ASSET_BYTES:
        return "The release zip is an unexpected size — download it from the release page."

    resp = None
    ok = False
    done = 0
    try:
        parent = os.path.dirname(part)
        if parent:
            os.makedirs(parent, exist_ok=True)
        _remove_quiet(part)

        resp = get(url, headers=dict(HEADERS), timeout=TIMEOUT, stream=True)
        status = getattr(resp, "status_code", None)
        if status != 200:
            return f"Download failed (HTTP {status})."

        declared = _content_length(resp)
        if declared is not None and declared != expected_size:
            return "The download did not match the size GitHub published."

        last_byte_ts = now()
        last_cb_ts = last_byte_ts
        _report(progress_cb, 0, expected_size)

        with open(part, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=CHUNK):
                if _cancelled(cancel_event):
                    return "Download cancelled."
                stamp = now()
                if not chunk:
                    # A keep-alive carried no bytes. This is the ONLY thing
                    # that may expire the stall clock: iter_content BLOCKS
                    # until a whole 256 KiB chunk has arrived, so a chunk that
                    # DID carry bytes proves the link is alive no matter how
                    # long it took to fill — timing it would make every link
                    # slower than ~8.7 KB/s permanently "stalled".
                    if stamp - last_byte_ts > STALL_S:
                        return "The download stalled — try again."
                    continue
                last_byte_ts = stamp
                done += len(chunk)
                if done > expected_size:
                    return "The download was larger than GitHub published."
                fh.write(chunk)
                if stamp - last_cb_ts >= 0.1:
                    last_cb_ts = stamp
                    _report(progress_cb, done, expected_size)

        if done != expected_size:
            return "The download ended early — try again."
        _report(progress_cb, done, expected_size)
        ok = True
        return None
    except Exception as exc:
        _quiet_log(f"download {type(exc).__name__}: {exc}")
        return "Download failed — try again."
    finally:
        try:
            if resp is not None and hasattr(resp, "close"):
                resp.close()
        except Exception:
            pass
        if not ok:
            _remove_quiet(part)


def verify_zip(zip_path, expected_size, digest) -> str | None:
    """Judge the downloaded archive. Reason string, or ``None`` when it is ours.

    Five checks, in the one order that never inflates a byte on the strength
    of an unchecked number: the size GitHub published must itself be a
    plausible build (a 4 GB "asset" is refused before we stream a hash over
    it), the file must be exactly that many bytes, its SHA-256 must be
    GitHub's, its member set must be the release shape read straight out of
    the central directory (:func:`_single_member` — this is where a declared
    size gets vetoed), and only THEN does ``zipfile``'s CRC pass actually
    decompress anything.

    This is integrity, not authenticity — GitHub's digest proves the bytes we
    got are the bytes that were uploaded, and the app's trust model is already
    "whoever can push a release to this repo".
    """
    try:
        if not isinstance(zip_path, str) or not zip_path.strip():
            return "There is no downloaded file to verify."
        want = parse_digest(digest)
        if want is None:
            return "GitHub did not publish a checksum for this release."
        if (not isinstance(expected_size, int) or isinstance(expected_size, bool)
                or expected_size <= 0):
            return "GitHub did not publish a usable size for this release."
        if not MIN_ASSET_BYTES <= expected_size <= MAX_ASSET_BYTES:
            return (f"The release zip is an unexpected size "
                    f"({_mb(expected_size)} MB) — it was not installed.")
        if not os.path.isfile(zip_path):
            return "The downloaded file is missing."

        actual = os.path.getsize(zip_path)
        if actual != expected_size:
            return (f"The download is {actual} bytes, GitHub published "
                    f"{expected_size} — it was not installed.")

        if _sha256_file(zip_path) != want:
            return "The download did not match GitHub's checksum — it was not installed."

        with zipfile.ZipFile(zip_path) as archive:
            reason, _info = _single_member(archive)
            if reason:
                return reason
            broken = archive.testzip()
            if broken:
                return f"The downloaded zip is damaged ({broken})."
        return None
    except zipfile.BadZipFile:
        return "The downloaded file is not a valid zip."
    except Exception as exc:
        _quiet_log(f"verify_zip {type(exc).__name__}: {exc}")
        return "The download could not be verified."


def extract_exe(zip_path, new_path) -> str | None:
    """Stream the single ``FCTool.exe`` member out to ``new_path``.

    Never ``extractall`` and never a path built from an archive name: the one
    permitted member is looked up by exact name and written to a destination
    the CALLER chose. The read is capped, the written size must equal what the
    zip declared, and the result must actually be a Windows executable — the
    ``MZ`` magic plus a real ``PE\\0\\0`` at the ``e_lfanew`` offset — because
    what gets renamed over ``FCTool.exe`` is the one file whose validity we
    cannot test after the fact.

    The member set is re-checked here even though :func:`verify_zip` already
    did: this function must be safe to call on its own.
    """
    if not isinstance(zip_path, str) or not zip_path.strip():
        return "There is no downloaded file to unpack."
    if not isinstance(new_path, str) or not new_path.strip():
        return "The update has nowhere to unpack to."

    ok = False
    try:
        parent = os.path.dirname(new_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        _remove_quiet(new_path)

        with zipfile.ZipFile(zip_path) as archive:
            reason, info = _single_member(archive)
            if reason:
                return reason
            declared = _sane_size(getattr(info, "file_size", None))

            written = 0
            with archive.open(info) as src, open(new_path, "wb") as dst:
                while True:
                    buf = src.read(CHUNK)
                    if not buf:
                        break
                    written += len(buf)
                    if written > MAX_ASSET_BYTES:
                        return "The file inside the zip is far larger than it declared."
                    dst.write(buf)
            if written != declared:
                return (f"The unpacked file is the wrong size ({written} bytes, "
                        f"the zip declared {declared}).")

        reason = _check_pe(new_path)
        if reason:
            return reason
        ok = True
        return None
    except zipfile.BadZipFile:
        return "The downloaded file is not a valid zip."
    except Exception as exc:
        _quiet_log(f"extract_exe {type(exc).__name__}: {exc}")
        return "The download could not be unpacked."
    finally:
        if not ok:
            _remove_quiet(new_path)


# ── the swap: two renames, and a way back from each of them ─────────────────

def swap_in(plan, *, replace=os.replace, remove=os.remove, retries=5,
            sleep=time.sleep) -> str | None:
    """Make the extracted build the one that runs. Reason, or ``None``.

    Windows refuses to delete or overwrite the image of a running process, but
    it happily RENAMES it — that is the whole trick, and the reason this needs
    no helper script, no second copy of the exe and no elevation::

        FCTool.exe      -> FCTool.exe.old     (we keep running from it)
        updates/...new  -> FCTool.exe

    Both are same-volume renames (staging is deliberately a subfolder of the
    exe's own directory), so each is atomic: there is no instant at which
    ``FCTool.exe`` is a half-written file. The ordering is chosen so that
    every failure leaves something runnable at ``FCTool.exe``:

    * the new file vanished (an antivirus quarantine between extract and swap)
      — nothing is renamed at all;
    * a stale ``.old`` will not go — an update is installed but not restarted
      and its file is a live process's image; nothing is renamed;
    * the first rename fails — nothing has moved;
    * the second fails — the first is undone and the old build is back;
    * even the undo fails (a same-volume rename into a name we just vacated:
      never seen) — the message names both files so the user can rename by
      hand, and the running process is unaffected either way, because its
      image is mapped from the ``.old`` file it already holds open.

    ``replace``/``remove``/``sleep`` are seams: the whole table above is
    testable without a locked file or a real antivirus.
    """
    try:
        if not isinstance(plan, InstallPlan):
            return "There is nothing staged to install."

        exe, old, new = plan.exe_path, plan.old_path, plan.new_path

        if not os.path.isfile(new):
            return "The new file vanished before install (antivirus?)."

        reason = _clear_stale_old(old, remove=remove, retries=retries, sleep=sleep)
        if reason:
            return reason

        try:
            replace(exe, old)
        except Exception as exc:
            _quiet_log(f"swap stage {type(exc).__name__}: {exc}")
            return "Could not stage the current FCTool.exe — nothing was changed."

        try:
            replace(new, exe)
        except Exception as exc:
            _quiet_log(f"swap install {type(exc).__name__}: {exc}")
            try:
                replace(old, exe)
            except Exception as undo:
                _quiet_log(f"swap restore {type(undo).__name__}: {undo}")
                return (f'The update could not be completed. Rename "{old}" '
                        f'back to "{exe}" to restore the previous version.')
            return ("Could not put the new version in place — "
                    "the previous version was restored.")
        return None
    except Exception as exc:
        _quiet_log(f"swap_in {type(exc).__name__}: {exc}")
        return "The update could not be installed."


# ── the marker: the only channel between the old process and the new one ────

#: Lives in the staging dir, beside the archive it came from.
MARKER_NAME = "pending_update.json"
#: ``installed`` -> ``launched`` -> ``booted`` | ``rolled_back``.
MARKER_STATES = ("installed", "launched", "booted", "rolled_back")


@dataclass
class Marker:
    """What the swap knows and the next boot needs to know.

    Mutable on purpose: exactly one field changes at each hand-off, and the
    file is rewritten atomically every time (:func:`app_io.atomic_write_json`),
    so a reader never sees a half-written marker — only the previous state or
    the next one.

    ``old_pid`` is recorded for diagnosis only; nothing waits on it. The old
    process has already finished every save before it spawns the new one, so
    making the new build wait for a pid would add seconds to every restart and
    buy nothing.
    """

    expected_tag: str
    previous_version: str
    state: str
    ts: float
    old_pid: int


@dataclass(frozen=True)
class Notice:
    """A one-line thing to tell the user at boot. ``kind`` is ``"updated"`` or
    ``"rolled_back"``; the caller picks the colour, not the words."""

    kind: str
    text: str


def marker_path(staging_dir) -> str:
    """The marker's full path, or ``""`` when ``staging_dir`` is unusable
    (every reader treats ``""`` as "no marker")."""
    try:
        if not isinstance(staging_dir, str) or not staging_dir.strip():
            return ""
        return os.path.join(staging_dir, MARKER_NAME)
    except Exception:
        return ""


def write_marker(path, marker) -> bool:
    """Write ``marker`` atomically. ``True`` when it landed.

    Refuses anything it could not read back — a wrong type, an unknown state —
    rather than writing a file the next boot would have to treat as corrupt.
    """
    try:
        if not isinstance(path, str) or not path.strip():
            return False
        if not isinstance(marker, Marker) or marker.state not in MARKER_STATES:
            return False
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        atomic_write_json(path, {
            "expected_tag": str(marker.expected_tag),
            "previous_version": str(marker.previous_version),
            "state": str(marker.state),
            "ts": float(marker.ts),
            "old_pid": int(marker.old_pid),
        })
        return True
    except Exception as exc:
        _quiet_log(f"write_marker {type(exc).__name__}: {exc}")
        return False


def read_marker(path) -> Marker | None:
    """The marker at ``path``, or ``None`` for anything unusable.

    Absent, unreadable, not JSON, not an object, missing a field, holding a
    field of the wrong type, or naming a ``state`` outside
    :data:`MARKER_STATES` all collapse to the same answer on purpose: the
    marker is a hint about what to clean up, and a hint we cannot trust is one
    we ignore. :func:`startup_housekeeping` then sweeps staging and says
    nothing — which is exactly what an interrupted install deserves. The state
    check mirrors :func:`write_marker`'s refusal to write one, so the only
    ways to get an unknown state past a reader are a hand-edited file and a
    downgrade to a build that predates the state — and both should be read as
    "no marker", not acted on blindly.
    """
    try:
        if not isinstance(path, str) or not path.strip():
            return None
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict):
            return None
        tag = raw.get("expected_tag")
        previous = raw.get("previous_version")
        state = raw.get("state")
        if not isinstance(tag, str) or not isinstance(previous, str):
            return None
        if not isinstance(state, str) or state not in MARKER_STATES:
            return None
        ts = raw.get("ts")
        pid = raw.get("old_pid")
        if isinstance(ts, bool) or not isinstance(ts, (int, float)):
            return None
        if isinstance(pid, bool) or not isinstance(pid, int):
            return None
        return Marker(tag, previous, state, float(ts), pid)
    except Exception:
        return None


def clear_marker(path) -> None:
    """Delete the marker. A missing (or locked) one is not an error."""
    _remove_quiet(path)


def mark_booted(staging_dir, current_version) -> bool:
    """Record "the new build is up" — the signal the watchdog waits for.

    Called by the NEW process a few seconds into its own boot. It writes only
    when a marker exists AND names the version now running, so a marker left
    by some other update (or an old copy the user started by hand) can never
    be mistaken for a successful boot of THIS one. Versions are compared the
    way :func:`update_check.check` compares them — zero-padded, ``v`` optional
    — so ``v5.7.0`` and ``5.7.0`` are one version.
    """
    try:
        path = marker_path(staging_dir)
        marker = read_marker(path)
        if marker is None or not _same_version(marker.expected_tag, current_version):
            return False
        return _advance_marker(path, "booted")
    except Exception as exc:
        _quiet_log(f"mark_booted {type(exc).__name__}: {exc}")
        return False


def startup_housekeeping(exe_path, staging_dir, current_version) -> Notice | None:
    """Clean up after the previous update and say what happened, if anything.

    Runs on a worker a few seconds into every boot. Four cases, and the marker
    (plus the version actually running) decides which:

    * **no marker** — nothing is pending, so anything left in staging is
      debris from an interrupted attempt, and a ``.old``/``.failed`` beside
      the exe is the residue of an update that already finished. Sweep both
      and say nothing. This is also where a ``.old`` that was locked on the
      previous boot (the old process was still alive) finally goes.
    * **rolled_back** — checked BEFORE the version test on purpose: after a
      rollback the running version is the OLD one, so the tags deliberately do
      not match. Clean up and tell the user their update did not start.
    * **the marker names the version now running** — the update worked. That
      holds whether the state is ``booted``, ``installed`` or ``launched``:
      being here, as this version, IS the success. Delete ``.old`` (one
      successful boot is all we keep it for), ``.failed``, and the archive.
    * **anything else** (``installed``/``launched`` naming a version that is
      not us) — the user launched the old copy while an install waits for its
      restart. Touch NOTHING: that ``.old`` may be the mapped image of the
      process asking this question.

    Every delete is best-effort — a locked file is skipped, the notice is
    still returned, and the next boot's first case picks it up. Nothing here
    raises: it runs on a worker at startup and its worst possible outcome is
    a file left on disk.
    """
    try:
        path = marker_path(staging_dir)
        marker = read_marker(path)

        if marker is None:
            _remove_quiet(path)                 # a corrupt marker IS no marker
            _sweep_staging(staging_dir)
            _remove_quiet(_sibling(exe_path, ".old"))
            _remove_quiet(_sibling(exe_path, ".failed"))
            return None

        if marker.state == "rolled_back":
            _remove_quiet(_sibling(exe_path, ".failed"))
            _sweep_staging(staging_dir)
            clear_marker(path)
            return Notice("rolled_back",
                          f"Update to {_vtag(marker.expected_tag)} failed to start "
                          f"and was rolled back — the release page has the manual "
                          f"download.")

        if _same_version(marker.expected_tag, current_version):
            _remove_quiet(_sibling(exe_path, ".old"))
            _remove_quiet(_sibling(exe_path, ".failed"))
            _sweep_staging(staging_dir)
            clear_marker(path)
            return Notice("updated", f"Updated to {_vtag(marker.expected_tag)}.")

        return None
    except Exception as exc:
        _quiet_log(f"housekeeping {type(exc).__name__}: {exc}")
        return None


# ── the whole install, in order ──────────────────────────────────────────────

def run_install(plan, progress_cb, cancel_event, *,
                previous_version=APP_VERSION) -> InstallResult:
    """Run every step, in the one order that is safe. Never raises.

    ``preflight -> download -> verify -> extract -> marker -> swap``: nothing
    is written where the next boot would look for it until the bytes have been
    matched against GitHub's checksum and the file that came out of the zip
    has been proven to be a Windows program.

    A failure at any stage removes what that stage produced (and everything
    earlier that is now pointless — a verified-but-unswappable zip is 53 MB of
    nothing), so a second attempt always starts clean and no half-finished
    artefact can be mistaken for a finished one. The single exception is the
    unpacked build after a swap that could not be undone: with no
    ``FCTool.exe`` on disk it stops being debris and becomes a rescue.
    ``FCTool.exe`` is untouched until :func:`swap_in`, and that function is
    itself reversible.

    The cancel flag is re-read once more between the extract and the swap: up
    to there, stopping costs a deleted file; past it, the user's program has
    already been renamed.

    The marker is written BEFORE the swap so that a crash between the two
    still leaves the next boot able to explain itself. Its failure is not
    fatal: a marker that will not write costs the "Updated to X" notice and
    the automatic rollback watch, which is not worth refusing the update the
    user asked for.
    """
    try:
        if not isinstance(plan, InstallPlan):
            return InstallResult(False, "preflight",
                                 "This release has nothing that can be "
                                 "installed automatically.")

        reason = preflight(plan)
        if reason:
            return InstallResult(False, "preflight", reason)

        try:
            os.makedirs(plan.staging_dir, exist_ok=True)
        except Exception as exc:
            _quiet_log(f"staging {type(exc).__name__}: {exc}")
            return InstallResult(False, "preflight",
                                 "FCTool could not create its updates folder.")

        size = getattr(plan.asset, "size", None)
        reason = download(getattr(plan.asset, "url", None), plan.part_path, size,
                          progress_cb, cancel_event)
        if reason:
            _remove_quiet(plan.part_path)
            return InstallResult(False, "download", reason)

        try:
            os.replace(plan.part_path, plan.zip_path)
        except Exception as exc:
            _quiet_log(f"download rename {type(exc).__name__}: {exc}")
            _remove_quiet(plan.part_path)
            return InstallResult(False, "download",
                                 "The download could not be finished.")

        reason = verify_zip(plan.zip_path, size, getattr(plan.asset, "digest", ""))
        if reason:
            _remove_quiet(plan.zip_path)
            return InstallResult(False, "verify", reason)

        reason = extract_exe(plan.zip_path, plan.new_path)
        if reason:
            _remove_quiet(plan.new_path)
            _remove_quiet(plan.zip_path)
            return InstallResult(False, "extract", reason)

        # The last moment at which stopping is free. Everything above this
        # line is a file in the staging dir; everything below renames the
        # program the user is running. A dialog closed during the download
        # must not be answered with a swapped exe half a minute later.
        if _cancelled(cancel_event):
            _remove_quiet(plan.new_path)
            _remove_quiet(plan.zip_path)
            return InstallResult(False, "swap", "Update cancelled before install.")

        path = marker_path(plan.staging_dir)
        write_marker(path, Marker(plan.tag, str(previous_version), "installed",
                                  time.time(), os.getpid()))

        reason = swap_in(plan)
        if reason:
            clear_marker(path)
            _remove_quiet(plan.zip_path)
            # ``.new`` is debris only while there is still an FCTool.exe to
            # run. In swap_in's restore-failed branch there is not, and this
            # file is one of the two the message tells the user to rename —
            # deleting it would take away the simpler of the two rescues.
            if os.path.isfile(plan.exe_path):
                _remove_quiet(plan.new_path)
            return InstallResult(False, "swap", reason)

        _remove_quiet(plan.zip_path)
        return InstallResult(True, "installed",
                             f"Installed {_vtag(plan.tag)} — restart FCTool to "
                             f"run it.")
    except Exception as exc:
        _quiet_log(f"run_install {type(exc).__name__}: {exc}")
        return InstallResult(False, "error", "The update could not be installed.")


# ── the relaunch watchdog (runs after the mainloop, writes only the marker) ──

#: Detach the child so it outlives us and owns its own console signals. Read
#: through ``getattr`` because these names exist only on Windows and this
#: module must stay importable (and testable) everywhere.
CREATION_FLAGS = (getattr(subprocess, "DETACHED_PROCESS", 0)
                  | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))


def relaunch_and_watch(exe_path, staging_dir, *, popen=subprocess.Popen,
                       watch_s=20.0, poll_s=0.5, sleep=time.sleep,
                       now=time.monotonic) -> str:
    """Start the new build and stay alive long enough to undo it if it dies.

    Called from ``main()`` AFTER the mainloop has ended and logging has been
    shut down, so this process holds no app-data file open — and it writes
    nothing but the marker from here on, which is what makes it safe for the
    new process to be opening the same config and log at the same time.

    Returns one of ``"booted"`` (the new build reported for duty),
    ``"timeout"`` (alive but silent — assume it is fine and get out of its
    way), ``"rolled_back"`` (it died young; the previous build is back and
    running), ``"spawn_failed"`` (nothing started; the update stays on disk
    for the user's next manual launch) or ``"rollback_failed"`` (it died and
    could not be undone; the new build is still there and runnable).

    The watch is deliberately short and deliberately one-sided: it can only
    make things better. Every second of it is a second the user is staring at
    a window that has already closed.

    LOG-FREE BY CONSTRUCTION, and it must stay that way. This runs after
    ``logging.shutdown()``, while the NEW process already has ``fctool.log``
    open: a handler re-opened from this dying process would be a second writer
    on the same file. Nothing here logs above DEBUG, and ``app_log`` pins the
    root at INFO, so every ``_quiet_log`` on this path is dropped before a
    handler is asked for. Anyone raising this module's log level, or adding a
    louder call below, has to keep that true.
    """
    try:
        if not isinstance(exe_path, str) or not exe_path.strip():
            return "spawn_failed"
        path = marker_path(staging_dir)
        _advance_marker(path, "launched", old_pid=os.getpid())

        proc = _spawn(popen, exe_path)
        if proc is None:
            _advance_marker(path, "installed")
            return "spawn_failed"

        watch = _positive(watch_s, 20.0)
        step = _positive(poll_s, 0.5)
        start = now()
        # A belt for a seam that never advances: the loop is bounded by polls
        # as well as by the clock, so a stubbed monotonic cannot hang an exit.
        for _ in range(int(watch / step) + 2):
            _call_quiet(sleep, step)
            marker = read_marker(path)
            if marker is not None and marker.state == "booted":
                return "booted"
            if _poll(proc) is not None:
                return _rollback(exe_path, path, popen)
            if now() - start >= watch:
                break
        return "timeout"
    except Exception as exc:
        _quiet_log(f"relaunch {type(exc).__name__}: {exc}")
        return "timeout"


# ── internals ────────────────────────────────────────────────────────────────

def _mb(count) -> str:
    """``count`` bytes as a whole number of MB, for a user-facing sentence."""
    try:
        return f"{int(count) / float(2**20):.0f}"
    except Exception:
        return "?"


def _sane_size(value) -> int:
    """A byte count we can do arithmetic with: 0 for anything unusable."""
    try:
        if isinstance(value, bool) or not isinstance(value, int):
            return 0
        return value if value > 0 else 0
    except Exception:
        return 0


def _remove_quiet(path) -> None:
    """Delete ``path`` if it is there. A locked file is not worth an exception."""
    try:
        if isinstance(path, str) and path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def _report(progress_cb, done: int, total: int) -> None:
    """Hand progress to the caller. A callback that raises is its own problem."""
    try:
        if callable(progress_cb):
            progress_cb(done, total)
    except Exception:
        pass


def _cancelled(cancel_event) -> bool:
    """True only when a real event says so; ``None`` means "no cancel"."""
    try:
        return bool(cancel_event.is_set())
    except Exception:
        return False


def _content_length(resp):
    """The response's ``Content-Length`` as an int, or ``None`` if absent/odd."""
    try:
        raw = resp.headers.get("Content-Length")
        return int(str(raw).strip()) if raw is not None else None
    except Exception:
        return None


def _sha256_file(path) -> str:
    """Streamed sha256 hex of ``path``; ``""`` when it cannot be read (which
    then simply fails the comparison)."""
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            while True:
                buf = fh.read(CHUNK)
                if not buf:
                    break
                digest.update(buf)
        return digest.hexdigest()
    except Exception:
        return ""


def _single_member(archive):
    """``(None, info)`` for the archive's ``FCTool.exe``, else ``(reason, None)``.

    The published release zip is NOT a one-file archive — it is ``FCTool.exe``
    beside a clean ``config.json`` and a ``SETUP.txt`` — so the rule is a
    member-SET rule, not a member count:

    * exactly one entry named exactly ``FCTool.exe`` (``fctool.EXE`` is not it);
    * no other entry whose name ends in ``.exe`` — a second program in the
      archive is the whole game, since the one we install runs unattended;
    * no entry carrying a path of any kind (a separator either way, ``..``, a
      drive colon, or the trailing slash that marks a directory);
    * at most :data:`MAX_ZIP_MEMBERS` entries;
    * ``FCTool.exe``'s declared size inside the asset band;
    * every declared size TOGETHER no more than one build plus
      :data:`SIBLING_SLACK_BYTES`, so a well-behaved exe cannot escort a
      sibling that inflates to a gigabyte.

    All of it reads the central directory only — no decompression — which is
    what lets :func:`verify_zip` run it before ``testzip()``. One owner for
    the rule both readers apply.
    """
    try:
        infos = list(archive.infolist())
    except Exception:
        return "The downloaded zip could not be read.", None

    if len(infos) > MAX_ZIP_MEMBERS:
        return (f"The zip holds more than {MAX_ZIP_MEMBERS} files — that is "
                f"not an FCTool release."), None

    found = []
    total = 0
    for info in infos:
        name = str(getattr(info, "filename", "") or "")
        # zipfile spells a directory entry with a trailing slash, so the
        # separator test covers those too.
        if (not name or "/" in name or "\\" in name
                or ".." in name or ":" in name):
            return ("The zip contains a file path where a plain file name "
                    "should be."), None
        if name == EXE_MEMBER:
            found.append(info)
        elif name.lower().endswith(".exe"):
            return f"The zip contains another program beside {EXE_MEMBER}.", None
        total += _sane_size(getattr(info, "file_size", None))

    if len(found) != 1:
        return (f"The zip does not contain exactly one {EXE_MEMBER} at its root."), None

    declared = _sane_size(getattr(found[0], "file_size", None))
    if not MIN_ASSET_BYTES <= declared <= MAX_ASSET_BYTES:
        return (f"{EXE_MEMBER} inside the zip is an unexpected size "
                f"({_mb(declared)} MB)."), None
    if total > MAX_ASSET_BYTES + SIBLING_SLACK_BYTES:
        return (f"The files inside the zip add up to {_mb(total)} MB — far "
                f"more than an FCTool release."), None
    return None, found[0]


def _check_pe(path) -> str | None:
    """``None`` when ``path`` starts like a Windows executable.

    ``MZ`` at 0, a 32-bit ``e_lfanew`` at 60 that points inside the file, and
    ``PE\\0\\0`` at that offset. Cheap, and it is the difference between
    renaming an executable over FCTool.exe and renaming a truncated download
    over it.
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            head = fh.read(64)
            if len(head) < 64 or head[:2] != b"MZ":
                return "The unpacked file is not a Windows program."
            offset = struct.unpack("<I", head[60:64])[0]
            if offset < 4 or offset + 4 > size:
                return "The unpacked file is not a Windows program."
            fh.seek(offset)
            if fh.read(4) != b"PE\0\0":
                return "The unpacked file is not a Windows program."
        return None
    except Exception as exc:
        _quiet_log(f"pe check {type(exc).__name__}: {exc}")
        return "The unpacked file could not be checked."


def _clear_stale_old(old, *, remove, retries, sleep) -> str | None:
    """Get rid of a leftover ``FCTool.exe.old``, or say why we must not swap.

    Two very different files wear this name: garbage from an update that
    already restarted (delete it) and the mapped image of a process that is
    running RIGHT NOW (never deletable, and the reason a second update must
    wait for a restart). The only way to tell them apart is to try — with a
    short backoff, because the third possibility is a transient OneDrive or
    antivirus lock that clears in milliseconds.
    """
    try:
        if not isinstance(old, str) or not old or not os.path.exists(old):
            return None
        attempts = retries if isinstance(retries, int) and retries > 0 else 1
        for attempt in range(attempts):
            try:
                remove(old)
                return None
            except Exception as exc:
                _quiet_log(f"stale .old {type(exc).__name__}: {exc}")
                if attempt < attempts - 1:
                    _call_quiet(sleep, SWAP_BACKOFF_S * (attempt + 1))
        return "Restart FCTool to finish the previous update first."
    except Exception as exc:
        _quiet_log(f"stale .old {type(exc).__name__}: {exc}")
        return "Restart FCTool to finish the previous update first."


def _advance_marker(path, state, *, old_pid=None) -> bool:
    """Move an EXISTING marker to ``state``. False when there is none.

    Never creates one: a marker is written exactly once, by the install that
    earned it. Without it there is nothing to advance and nothing to clean up
    — the callers all degrade to "watch the process and hope", which is what
    they would do anyway.
    """
    marker = read_marker(path)
    if marker is None:
        return False
    marker.state = state
    marker.ts = time.time()
    if old_pid is not None:
        marker.old_pid = old_pid
    return write_marker(path, marker)


def _same_version(left, right) -> bool:
    """Are these two tags the same version? ``v5.7`` == ``5.7.0``.

    Zero-padded like :func:`update_check.check`, because the tag on GitHub
    carries a ``v`` and ``APP_VERSION`` does not, and a tag with a different
    number of components is still the same release.
    """
    first = parse_version(left)
    second = parse_version(right)
    if first is None or second is None:
        return False
    width = max(len(first), len(second))
    return (first + (0,) * (width - len(first))
            == second + (0,) * (width - len(second)))


def _vtag(tag) -> str:
    """A tag as the user should read it: ``v5.7.0``, whichever form we hold."""
    try:
        text = str(tag).strip()
        if not text:
            return "the new version"
        return text if text[:1] in ("v", "V") else "v" + text
    except Exception:
        return "the new version"


def _sibling(exe_path, suffix) -> str:
    """``FCTool.exe`` + ``suffix``, or ``""`` when there is no exe path."""
    try:
        if not isinstance(exe_path, str) or not exe_path.strip():
            return ""
        return exe_path + suffix
    except Exception:
        return ""


def _sweep_staging(staging_dir) -> None:
    """Delete leftover archives and unpacked builds from the staging dir.

    Exactly three suffixes, and nothing else in there is touched: the marker
    (``.json``) is state, not debris, and the caller decides when it goes.
    """
    try:
        if not isinstance(staging_dir, str) or not os.path.isdir(staging_dir):
            return
        for name in os.listdir(staging_dir):
            if name.lower().endswith((".part", ".zip", ".new")):
                _remove_quiet(os.path.join(staging_dir, name))
    except Exception as exc:
        _quiet_log(f"sweep {type(exc).__name__}: {exc}")


def _spawn(popen, exe_path):
    """Start ``exe_path`` detached, or ``None`` if it would not start.

    ``cwd`` is the exe's own folder so the new process resolves its data files
    the way a double-click would, whatever directory this one was started in.
    """
    try:
        return popen([exe_path], cwd=os.path.dirname(exe_path) or None,
                     close_fds=True, creationflags=CREATION_FLAGS)
    except Exception as exc:
        _quiet_log(f"spawn {type(exc).__name__}: {exc}")
        return None


def _poll(proc):
    """The child's exit code, or ``None`` while it is alive.

    A ``poll`` that raises counts as alive: an unreadable handle is not
    evidence that the new build failed, and rolling back on a guess would undo
    a perfectly good update.
    """
    try:
        return proc.poll()
    except Exception:
        return None


def _rollback(exe_path, path, popen) -> str:
    """Undo the swap after the new build died, and start the old one again.

    Order matters and is the reverse of :func:`swap_in`: park the failed build
    under ``.failed`` (kept, not deleted — it is the only evidence of what
    went wrong), then give its name back to ``.old``. If the FIRST rename
    fails, stop: the new build is still in place and still runnable, which is
    strictly better than a half-finished rollback. If the second fails we are
    momentarily without an ``FCTool.exe``, so the failed build is put back
    rather than leaving the user with no program at all.
    """
    failed = _sibling(exe_path, ".failed")
    old = _sibling(exe_path, ".old")
    try:
        os.replace(exe_path, failed)
    except Exception as exc:
        _quiet_log(f"rollback park {type(exc).__name__}: {exc}")
        return "rollback_failed"
    try:
        os.replace(old, exe_path)
    except Exception as exc:
        _quiet_log(f"rollback restore {type(exc).__name__}: {exc}")
        try:
            os.replace(failed, exe_path)
        except Exception:
            pass
        return "rollback_failed"
    _advance_marker(path, "rolled_back")
    _spawn(popen, exe_path)
    return "rolled_back"


def _call_quiet(fn, value) -> None:
    """Call an injected one-argument seam (``sleep``) and swallow anything."""
    try:
        if callable(fn):
            fn(value)
    except Exception:
        pass


def _positive(value, default: float) -> float:
    """``value`` as a positive float, or ``default`` — a zero or negative
    interval from a caller must not turn the watch into a spin."""
    try:
        number = float(value)
        return number if number > 0 else default
    except Exception:
        return default
