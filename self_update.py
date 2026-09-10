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
  deliberately paranoid — size, then GitHub's SHA-256, then the zip's own
  integrity, then its member set, then the PE header of what came out — and
  every failure deletes the partial file it produced. At no point is there
  anything half-written where a later step would find it.
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
import os
import re
import shutil
import struct
import sys
import time
import zipfile
from dataclasses import dataclass
from typing import Callable

import requests

from app_path import _is_dir_writable
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
# Exactly one member, at the zip root, spelled exactly this.
EXE_MEMBER = "FCTool.exe"

_DIGEST_RE = re.compile(r"^sha256:([0-9a-fA-F]{64})$", re.IGNORECASE)
# A plain file name: no separators, no drive, no dots-dots, no absurd length.
_SAFE_ZIP_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}\.zip$", re.IGNORECASE)
_FALLBACK_ZIP = "FCTool_update.zip"

#: ``progress_cb(done_bytes, total_bytes)`` — called on the worker thread.
ProgressCb = Callable[[int, int], None]


@dataclass(frozen=True)
class InstallResult:
    """The outcome of one install attempt, as the dialog wants to show it.

    ``stage`` is the step that ended it (``"preflight"``, ``"download"``,
    ``"verify"``, ``"extract"``, ``"swap"``, ``"done"``) and ``message`` is
    the human sentence to print. Defined here rather than beside the
    orchestration that returns it because both the swap layer and the dialog
    depend on the shape.
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
        never be able to point a write anywhere but at the staging dir.
        """
        try:
            raw = getattr(self.asset, "name", "") or ""
            name = os.path.basename(str(raw).strip())
            return name if _SAFE_ZIP_RE.match(name) else _FALLBACK_ZIP
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
            exe_size = 0
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
                if stamp - last_byte_ts > STALL_S:
                    return "The download stalled — try again."
                if not chunk:
                    continue          # keep-alive: no bytes, clock keeps running
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

    Five checks, cheapest first: the byte count GitHub published, GitHub's
    SHA-256 streamed over the file, ``zipfile``'s own CRC pass, exactly ONE
    member named exactly ``FCTool.exe`` at the root, and that member's
    declared size inside the same 20-500 MB band. Case matters
    (``fctool.EXE`` is refused) and so does the path (``sub/FCTool.exe`` is
    refused): a release that does not have the documented shape is not one we
    can install unattended.

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
        if not os.path.isfile(zip_path):
            return "The downloaded file is missing."
        if (not isinstance(expected_size, int) or isinstance(expected_size, bool)
                or expected_size <= 0):
            return "GitHub did not publish a usable size for this release."

        actual = os.path.getsize(zip_path)
        if actual != expected_size:
            return (f"The download is {actual} bytes, GitHub published "
                    f"{expected_size} — it was not installed.")

        if _sha256_file(zip_path) != want:
            return "The download did not match GitHub's checksum — it was not installed."

        with zipfile.ZipFile(zip_path) as archive:
            broken = archive.testzip()
            if broken:
                return f"The downloaded zip is damaged ({broken})."
            reason, info = _single_member(archive)
            if reason:
                return reason
            declared = _sane_size(getattr(info, "file_size", None))
            if not MIN_ASSET_BYTES <= declared <= MAX_ASSET_BYTES:
                return (f"{EXE_MEMBER} inside the zip is an unexpected size "
                        f"({_mb(declared)} MB).")
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
            if not MIN_ASSET_BYTES <= declared <= MAX_ASSET_BYTES:
                return (f"{EXE_MEMBER} inside the zip is an unexpected size "
                        f"({_mb(declared)} MB).")

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
    """``(None, info)`` when the archive holds exactly one root ``FCTool.exe``,
    else ``(reason, None)``. One owner for the rule both readers apply."""
    try:
        infos = list(archive.infolist())
    except Exception:
        return "The downloaded zip could not be read.", None
    if len(infos) != 1 or getattr(infos[0], "filename", None) != EXE_MEMBER:
        return (f"The zip does not contain exactly one {EXE_MEMBER} at its root."), None
    return None, infos[0]


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
