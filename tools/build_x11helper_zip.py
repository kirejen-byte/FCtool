"""Pack vendor/x11helper/ into the single shipped file vendor/x11helper.zip.

Why a zip: FCTool ships as a PyInstaller ONE-FILE exe, and every ``datas`` member
is extracted to the temp dir on each cold boot.  The vendored python-xlib tree is
~60 files; bundling the directory would add that cost to every Windows start for a
payload only Linux (Proton/Wine) users ever open.  One zip = one extracted file,
and the Linux helper puts the zip itself on ``sys.path`` (zipimport handles pure
Python packages), so nothing unpacks it either.

The output is DETERMINISTIC -- sorted member order, fixed timestamps and fixed
permission bits -- so rebuilding it never produces a spurious diff or a different
release checksum.

Usage:
    py -3.12 tools/build_x11helper_zip.py [src_dir] [out_zip]
"""

from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SRC = REPO_ROOT / "vendor" / "x11helper"
DEFAULT_OUT = REPO_ROOT / "vendor" / "x11helper.zip"

# Fixed for reproducibility (zip has no "no timestamp" encoding).
FIXED_DATE_TIME = (2020, 1, 1, 0, 0, 0)
# 0o644 regular file, in the high 16 bits as zipfile expects.
FIXED_EXTERNAL_ATTR = (0o100644 << 16)

# Sibling helper-side modules that live in vendor/x11helper/ but arrive in later
# tasks of the Linux-preview plan.  They are included when present; their absence
# is a warning, never an error, so this packer is usable from Task 3 onward.
SIBLING_FILES = ("helper.py", "xrender.py")

# The wire-protocol codec lives at the REPO ROOT as a single source of truth
# (the Windows side imports it directly from there too).  It is packed at the
# ZIP ROOT -- not nested under vendor/x11helper/ -- so the Linux helper's
# zipimport sys.path entry (the zip file itself) sees it right next to Xlib/.
ROOT_FILES = ("x11_thumbs_proto.py",)

EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".pyd", ".so", ".dll")


def _normalise_eol(data: bytes) -> bytes:
    """Normalise CRLF/CR line endings to LF.

    Used ONLY for the repo-authored sibling/root members (`SIBLING_FILES`,
    `ROOT_FILES`) -- never for the vendored upstream tree (Xlib/**, six.py,
    LICENSES.txt, ...), which `.gitattributes` (`vendor/x11helper/** -text`)
    keeps verbatim and byte-for-byte across clones on purpose.

    helper.py / xrender.py / x11_thumbs_proto.py are ordinary repo-authored
    Python, committed CRLF per this repo's convention, but a Linux clone (or
    any checkout with core.autocrlf off) sees them as LF on disk. Without
    normalising, the zip's bytes would depend on the OS/checkout that ran the
    packer -- breaking the byte-reproducibility contract asserted by
    test_built_zip_matches_the_shipped_zip_exactly. Normalising to a single
    canonical EOL regardless of the on-disk encoding restores that contract.
    """
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"")


def _is_excluded(rel_posix: str, name: str) -> bool:
    if "__pycache__" in rel_posix.split("/"):
        return True
    if ".dist-info" in rel_posix or ".egg-info" in rel_posix:
        return True
    if name.endswith(EXCLUDED_SUFFIXES):
        return True
    return False


def collect(src_dir) -> list:
    """Return the sorted list of archive member names to pack from `src_dir`."""
    src_dir = Path(src_dir)
    names = []
    for dirpath, dirnames, filenames in os.walk(src_dir):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for fn in sorted(filenames):
            full = Path(dirpath) / fn
            rel = full.relative_to(src_dir).as_posix()
            if _is_excluded(rel, fn):
                continue
            names.append(rel)
    return sorted(names)


def build(src_dir, out_path, root_dir=None) -> list:
    """Write a deterministic zip of `src_dir` (plus ROOT_FILES) to `out_path`.

    Returns the sorted member names.  Two runs over the same tree produce
    byte-identical archives.

    `root_dir` is where ROOT_FILES are resolved from -- the repo root, i.e.
    the parent of the vendor/ dir that contains `src_dir`.  It defaults to
    `src_dir.parent.parent` (vendor/x11helper -> vendor -> repo root) but is
    overridable so tests can build from a throwaway tmp layout.
    """
    src_dir = Path(src_dir)
    out_path = Path(out_path)
    root_dir = Path(root_dir) if root_dir is not None else src_dir.parent.parent
    names = collect(src_dir)

    for sibling in SIBLING_FILES:
        if sibling not in names:
            print(
                "build_x11helper_zip: NOTE - %s not present yet in %s (arrives in a "
                "later task); packing without it." % (sibling, src_dir)
            )

    root_members = []
    for root_file in ROOT_FILES:
        if (root_dir / root_file).is_file():
            root_members.append(root_file)
        else:
            print(
                "build_x11helper_zip: NOTE - %s not present yet at repo root %s "
                "(arrives in a later task); packing without it." % (root_file, root_dir)
            )

    # Guard against a name existing both in the vendored tree and in
    # ROOT_FILES: the ROOT copy wins (root_dir is the documented single
    # source of truth for ROOT_FILES) and the tree copy is skipped, with a
    # NOTE -- silently packing whichever `sorted()` happened to order last
    # would make the zip's content depend on which copy the filesystem walk
    # saw, and zipfile.writestr() with a duplicate name would silently write
    # two entries for it besides.
    for root_file in root_members:
        if root_file in names:
            names.remove(root_file)
            print(
                "build_x11helper_zip: NOTE - %s exists both in %s and at repo "
                "root %s; using the repo-root copy and skipping the tree copy."
                % (root_file, src_dir, root_dir)
            )

    all_names = sorted(names + [r for r in root_members if r not in names])

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_name(out_path.name + ".tmp")
    with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel in all_names:
            info = zipfile.ZipInfo(rel, date_time=FIXED_DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = FIXED_EXTERNAL_ATTR
            info.create_system = 3  # Unix, so the archive does not vary by builder OS
            base = root_dir if rel in root_members else src_dir
            data = (base / rel).read_bytes()
            if rel in SIBLING_FILES or rel in ROOT_FILES:
                data = _normalise_eol(data)
            zf.writestr(info, data)
    if out_path.exists():
        out_path.unlink()
    tmp_path.replace(out_path)
    return all_names


def main(argv) -> int:
    src = Path(argv[0]) if len(argv) > 0 else DEFAULT_SRC
    out = Path(argv[1]) if len(argv) > 1 else DEFAULT_OUT
    if not src.is_dir():
        print("build_x11helper_zip: ERROR - source dir not found: %s" % src)
        return 2
    names = build(src, out)
    print(
        "build_x11helper_zip: wrote %s (%d members, %d bytes)"
        % (out, len(names), out.stat().st_size)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
