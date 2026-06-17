"""
Auto-detect the EVE Online chat-logs folder.

The app stores the chat-logs folder in ``config["eve_logs_path"]`` (e.g.
``C:/Users/<user>/Documents/EVE/logs/Chatlogs``). When that value is blank or
still holds the shipped placeholder (``.../YOUR_USER/...``), we auto-detect it
so users don't have to configure it by hand.

EVE writes logs under the Windows "Documents" folder, which is frequently
redirected to OneDrive (e.g. ``C:/Users/<user>/OneDrive/Documents/EVE/...``).
Detection is redirection-aware: it asks Windows for the real Documents folder
(known-folder API, then the registry) and also probes OneDrive locations
directly.

Standard library only. The filesystem/registry/ctypes touch-points are all
injectable (``documents``, ``home``, ``exists``) so tests never hit the real
machine.
"""

import glob
import os
import sys

# Relative path of the EVE chat-logs folder beneath a "Documents" root.
_EVE_LOGS_SUBPATH = ("EVE", "logs", "Chatlogs")


def _looks_like_placeholder(value: str) -> bool:
    """Return True if ``value`` is whitespace-only or a known placeholder.

    Older configs (and copy-paste setups) sometimes carry a ``.../YOUR_USER/...``
    stand-in for the real user folder; treat any occurrence of "YOUR_USER" (in
    any case) as not-a-real-setting so auto-detection kicks in.
    """
    if not value or not value.strip():
        return True
    return "your_user" in value.lower()


def _logs_under(root):
    """Join an EVE-logs subpath onto a Documents-style ``root``.

    Returns ``None`` when ``root`` is falsy so callers can filter blanks.
    """
    if not root:
        return None
    return os.path.join(root, *_EVE_LOGS_SUBPATH)


def get_documents_dir():
    """Return the real Windows "Documents" folder, redirection-aware.

    Resolution order (each step wrapped so a failure falls through):

    1. Windows known-folder ``FOLDERID_Documents`` via ``SHGetKnownFolderPath``
       — this reflects OneDrive "Known Folder Move" redirection.
    2. Registry ``HKCU\\...\\User Shell Folders`` value ``Personal`` (with
       ``%USERPROFILE%``-style variables expanded).
    3. ``~/Documents`` as a last resort.

    On non-Windows platforms only step 3 is used.

    Returns the resolved path, or ``None`` if every strategy fails.
    """
    if sys.platform.startswith("win"):
        # 1) Known-folder API — authoritative, honours OneDrive redirection.
        try:
            import ctypes
            from ctypes import wintypes

            # FOLDERID_Documents {FDD39AD0-238F-46AF-ADB4-6C85480369C7}
            class _GUID(ctypes.Structure):
                _fields_ = [
                    ("Data1", wintypes.DWORD),
                    ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8),
                ]

            folderid_documents = _GUID(
                0xFDD39AD0,
                0x238F,
                0x46AF,
                (ctypes.c_ubyte * 8)(
                    0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7
                ),
            )

            path_ptr = ctypes.c_wchar_p()
            shell32 = ctypes.windll.shell32
            # SHGetKnownFolderPath(rfid, dwFlags=0, hToken=0, ppszPath)
            hr = shell32.SHGetKnownFolderPath(
                ctypes.byref(folderid_documents),
                0,
                0,
                ctypes.byref(path_ptr),
            )
            try:
                if hr == 0 and path_ptr.value:
                    return path_ptr.value
            finally:
                if path_ptr.value is not None:
                    ctypes.windll.ole32.CoTaskMemFree(path_ptr)
        except Exception:
            pass

        # 2) Registry — User Shell Folders "Personal" (may contain env vars).
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion"
                r"\Explorer\User Shell Folders",
            ) as key:
                raw, _ = winreg.QueryValueEx(key, "Personal")
            if raw:
                expanded = os.path.expandvars(raw)
                # Only trust it if expansion actually resolved any variables.
                if "%" not in expanded:
                    return expanded
        except Exception:
            pass

    # 3) Fallback: ~/Documents (also the only step on non-Windows).
    try:
        return os.path.join(os.path.expanduser("~"), "Documents")
    except Exception:
        return None


def candidate_logs_paths(documents=None, home=None):
    """Build the ordered, de-duped list of candidate EVE chat-logs paths.

    Order (highest priority first):

    1. ``<documents>/EVE/logs/Chatlogs`` — the redirection-aware Documents root.
    2. ``<home>/Documents/EVE/logs/Chatlogs`` — plain (non-redirected) Documents.
    3. ``<home>/OneDrive/Documents/EVE/logs/Chatlogs`` — the default OneDrive.
    4. Any ``<home>/OneDrive*/Documents/EVE/logs/Chatlogs`` discovered by
       globbing ``OneDrive*`` directories (e.g. "OneDrive - Company").

    Blanks/``None`` are dropped and duplicates removed while preserving order.

    Args:
        documents: Documents root; defaults to :func:`get_documents_dir`.
        home: User home dir; defaults to ``os.path.expanduser("~")``.
    """
    if documents is None:
        documents = get_documents_dir()
    if home is None:
        home = os.path.expanduser("~")

    candidates = [
        _logs_under(documents),
        _logs_under(os.path.join(home, "Documents")) if home else None,
        _logs_under(os.path.join(home, "OneDrive", "Documents")) if home else None,
    ]

    # Discover OneDrive* variants (e.g. "OneDrive - Contoso") by globbing.
    if home:
        try:
            for od_dir in sorted(glob.glob(os.path.join(home, "OneDrive*"))):
                candidates.append(_logs_under(os.path.join(od_dir, "Documents")))
        except Exception:
            pass

    # Linux: EVE runs under Wine/Proton/Lutris, so its logs live inside a prefix.
    # These globs simply match nothing on Windows, so adding them is harmless there.
    if home:
        prefix_user_globs = [
            os.path.join(home, ".wine", "drive_c", "users", "*"),
            os.path.join(home, ".local", "share", "Steam", "steamapps",
                         "compatdata", "*", "pfx", "drive_c", "users", "*"),
            os.path.join(home, ".steam", "steam", "steamapps",
                         "compatdata", "*", "pfx", "drive_c", "users", "*"),
            os.path.join(home, "Games", "*", "drive_c", "users", "*"),
        ]
        for g in prefix_user_globs:
            try:
                for user_dir in sorted(glob.glob(g)):
                    candidates.append(_logs_under(os.path.join(user_dir, "Documents")))
            except Exception:
                pass

    # De-dupe while preserving order; drop blanks/None.
    seen = set()
    ordered = []
    for path in candidates:
        if not path or path in seen:
            continue
        seen.add(path)
        ordered.append(path)
    return ordered


def resolve_eve_logs_path(configured, *, documents=None, home=None,
                          exists=os.path.exists):
    """Resolve the EVE chat-logs folder, auto-detecting when unset.

    Args:
        configured: The value currently stored in ``config["eve_logs_path"]``.
        documents: Override the Documents root (for tests). Defaults to
            :func:`get_documents_dir` via :func:`candidate_logs_paths`.
        home: Override the home directory (for tests).
        exists: Predicate used to test whether a path exists. Injectable so
            tests never touch the real filesystem. Defaults to
            ``os.path.exists``.

    Behaviour:
        * If ``configured`` is a non-empty, non-placeholder string, it is
          returned unchanged — an explicit user setting is always respected,
          even if the folder doesn't exist yet.
        * Otherwise the candidate paths from :func:`candidate_logs_paths` are
          probed in order and the first one for which ``exists(path)`` is true
          is returned.
        * If none of the candidates exist, the primary default
          (``<documents>/EVE/logs/Chatlogs``) is returned so the result is
          still a sensible value to show/use.
    """
    # Respect an explicit, real user setting.
    if isinstance(configured, str) and not _looks_like_placeholder(configured):
        return configured

    candidates = candidate_logs_paths(documents=documents, home=home)

    for path in candidates:
        try:
            if exists(path):
                return path
        except Exception:
            continue

    # Nothing exists yet — fall back to the primary default (first candidate),
    # which is the Documents-based path.
    if candidates:
        return candidates[0]

    # Extremely degraded environment (no documents, no home): build the default
    # directly so we never return None.
    if documents is None:
        documents = get_documents_dir()
    return _logs_under(documents) or os.path.join(*_EVE_LOGS_SUBPATH)
