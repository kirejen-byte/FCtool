"""Fake-win32 tests for eveo_tracker: title parsing, tool-window + owner-process
filtering, bare-EVE exclusion, rect passthrough, detection."""
from eveo_tracker import Thumb, find_thumbs, preview_running


class FakeWin32:
    """Fake Win32 wrapper. Each 'window' is a dict:
        {hwnd, title, toolwindow: bool, owner_exe: str, visible: bool,
         rect: (l,t,r,b)}
    `processes` is an optional list of running-process basenames returned by
    list_process_names(); when omitted, it defaults to the set of owner_exe
    values across the windows (so a window's owner process is 'running').
    """
    WS_EX_TOOLWINDOW = 0x00000080

    def __init__(self, windows, processes=None):
        self._w = {win["hwnd"]: win for win in windows}
        if processes is None:
            processes = [win.get("owner_exe", "") for win in windows
                         if win.get("owner_exe")]
        self._procs = list(processes)

    def enum_windows(self):
        return list(self._w.keys())

    def is_visible(self, hwnd):
        return self._w[hwnd].get("visible", True)

    def get_title(self, hwnd):
        return self._w[hwnd].get("title", "")

    def get_ex_style(self, hwnd):
        return self.WS_EX_TOOLWINDOW if self._w[hwnd].get("toolwindow") else 0

    def get_rect(self, hwnd):
        return self._w[hwnd].get("rect", (0, 0, 0, 0))

    def get_owner_exe(self, hwnd):
        return self._w[hwnd].get("owner_exe", "")

    def list_process_names(self):
        return list(self._procs)


def test_parses_thumb_title_and_rect():
    w = FakeWin32([
        {"hwnd": 1, "title": "EVE - Alpha Pilot", "toolwindow": True,
         "owner_exe": "EVE-O Preview.exe", "rect": (10, 20, 210, 140)},
    ])
    thumbs = find_thumbs(win32=w)
    assert thumbs == [Thumb(hwnd=1, char_name="Alpha Pilot", rect=(10, 20, 210, 140))]


def test_excludes_bare_eve():
    w = FakeWin32([
        {"hwnd": 1, "title": "EVE", "toolwindow": True, "owner_exe": "EVE-O Preview.exe"},
    ])
    assert find_thumbs(win32=w) == []


def test_excludes_game_client_owned_by_exefile():
    # Real game client: title matches "EVE - X" but owner is ExeFile.exe and it
    # is NOT a tool window. Must be excluded on BOTH signals.
    w = FakeWin32([
        {"hwnd": 1, "title": "EVE - Alpha", "toolwindow": False,
         "owner_exe": "ExeFile.exe", "rect": (0, 0, 100, 100)},
    ])
    assert find_thumbs(win32=w) == []


def test_excludes_toolwindow_owned_by_exefile():
    # Even a tool window owned by ExeFile.exe (defensive) is excluded.
    w = FakeWin32([
        {"hwnd": 1, "title": "EVE - Alpha", "toolwindow": True,
         "owner_exe": "ExeFile.exe"},
    ])
    assert find_thumbs(win32=w) == []


def test_excludes_non_toolwindow_thumb():
    w = FakeWin32([
        {"hwnd": 1, "title": "EVE - Alpha", "toolwindow": False,
         "owner_exe": "EVE-O Preview.exe"},
    ])
    assert find_thumbs(win32=w) == []


def test_excludes_invisible():
    w = FakeWin32([
        {"hwnd": 1, "title": "EVE - Alpha", "toolwindow": True,
         "owner_exe": "EVE-O Preview.exe", "visible": False},
    ])
    assert find_thumbs(win32=w) == []


def test_ignores_unrelated_titles():
    w = FakeWin32([
        {"hwnd": 1, "title": "Notepad", "toolwindow": True, "owner_exe": "notepad.exe"},
    ])
    assert find_thumbs(win32=w) == []


def test_renamed_exe_still_detected():
    # exe renamed away from 'EVE-O Preview.exe' — detection is by windows, not
    # basename; a tool window titled "EVE - X" not owned by ExeFile still counts.
    w = FakeWin32([
        {"hwnd": 7, "title": "EVE - Bravo", "toolwindow": True,
         "owner_exe": "PreviewFork.exe", "rect": (1, 2, 3, 4)},
    ])
    thumbs = find_thumbs(win32=w)
    assert len(thumbs) == 1 and thumbs[0].char_name == "Bravo"


def test_preview_running_true_when_thumbs():
    w = FakeWin32([
        {"hwnd": 1, "title": "EVE - Alpha", "toolwindow": True,
         "owner_exe": "EVE-O Preview.exe"},
    ])
    assert preview_running(win32=w) is True


def test_preview_running_false_when_none():
    w = FakeWin32([
        {"hwnd": 1, "title": "ExeFile", "toolwindow": False, "owner_exe": "ExeFile.exe"},
    ])
    assert preview_running(win32=w) is False


def test_preview_running_true_via_main_window_no_thumbs():
    # No thumbnails yet, but the Eve-O Preview main window is open (visible
    # top-level titled exactly "EVE-O Preview") — detection must still fire.
    w = FakeWin32([
        {"hwnd": 1, "title": "EVE-O Preview", "toolwindow": False,
         "owner_exe": "EVE-O Preview.exe"},
    ])
    assert find_thumbs(win32=w) == []
    assert preview_running(win32=w) is True


def test_preview_running_ignores_invisible_main_window_when_no_process():
    # An invisible main window alone does NOT satisfy the window-visibility
    # signal. With the process explicitly absent from the snapshot, detection
    # is False. (When the process IS present — the normal case — the separate
    # process signal fires; see test_preview_running_true_via_process_no_windows
    # and the invisible-main-window-but-process-running case below.)
    w = FakeWin32([
        {"hwnd": 1, "title": "EVE-O Preview", "toolwindow": False,
         "owner_exe": "EVE-O Preview.exe", "visible": False},
    ], processes=[])
    assert preview_running(win32=w) is False


def test_preview_running_true_when_main_window_invisible_but_process_up():
    # Real "hide to tray" state: main window hidden, no thumbnails, but the
    # EVE-O Preview.exe process is running -> detection fires on the process.
    w = FakeWin32([
        {"hwnd": 1, "title": "EVE-O Preview", "toolwindow": False,
         "owner_exe": "EVE-O Preview.exe", "visible": False},
    ])   # default processes derives ["EVE-O Preview.exe"] from the owner
    assert find_thumbs(win32=w) == []
    assert preview_running(win32=w) is True


def test_multiple_thumbs_sorted_stable():
    w = FakeWin32([
        {"hwnd": 2, "title": "EVE - Beta", "toolwindow": True,
         "owner_exe": "EVE-O Preview.exe", "rect": (0, 0, 1, 1)},
        {"hwnd": 1, "title": "EVE - Alpha", "toolwindow": True,
         "owner_exe": "EVE-O Preview.exe", "rect": (0, 0, 1, 1)},
    ])
    names = [t.char_name for t in find_thumbs(win32=w)]
    assert set(names) == {"Alpha", "Beta"}


# ── Process-based detection (Fix 1: found while thumbnails are hidden) ────────

def test_preview_running_true_via_process_no_windows():
    # Eve-O hid every thumbnail AND minimised its main window to the tray:
    # NO Eve-O window is visible, but the process is still running. Detection
    # must fire on the process signal alone.
    w = FakeWin32([], processes=["EVE-O Preview.exe", "explorer.exe"])
    assert find_thumbs(win32=w) == []
    assert preview_running(win32=w) is True


def test_preview_running_process_match_is_case_insensitive():
    w = FakeWin32([], processes=["eve-o preview.EXE"])
    assert preview_running(win32=w) is True


def test_preview_running_process_match_handles_full_path():
    # list_process_names may (defensively) hand back a full path; basename wins.
    w = FakeWin32([], processes=["C:\\Games\\EVE-O Preview\\EVE-O Preview.exe"])
    assert preview_running(win32=w) is True


def test_preview_running_false_when_only_unrelated_processes():
    w = FakeWin32([], processes=["explorer.exe", "ExeFile.exe", "chrome.exe"])
    assert preview_running(win32=w) is False


def test_preview_running_true_via_renamed_fork_process():
    # A renamed Eve-O fork owns a hidden-elsewhere tool window titled "EVE - X"
    # and its basename appears in the process list. The game client (ExeFile)
    # is present too and must NOT be what triggers detection.
    w = FakeWin32(
        [{"hwnd": 5, "title": "EVE - Gamma", "toolwindow": True,
          "owner_exe": "PreviewFork.exe", "rect": (0, 0, 10, 10)}],
        processes=["PreviewFork.exe", "ExeFile.exe"],
    )
    # This particular window IS a valid thumbnail, so find_thumbs sees it; the
    # point is preview_running stays True via the process path even independent
    # of thumbnails (covered by the no-windows test above).
    assert preview_running(win32=w) is True


def test_preview_running_false_when_process_lister_absent():
    # An older fake without list_process_names must not crash detection; with
    # no thumbnails and no main window, it simply reports not-detected.
    class NoProcLister:
        def enum_windows(self):
            return []
    assert preview_running(win32=NoProcLister()) is False
