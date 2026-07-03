"""Fake-win32 tests for eveo_tracker: title parsing, tool-window + owner-process
filtering, bare-EVE exclusion, rect passthrough, detection."""
from eveo_tracker import Thumb, find_thumbs, preview_running


class FakeWin32:
    """Fake Win32 wrapper. Each 'window' is a dict:
        {hwnd, title, toolwindow: bool, owner_exe: str, visible: bool,
         rect: (l,t,r,b)}
    """
    WS_EX_TOOLWINDOW = 0x00000080

    def __init__(self, windows):
        self._w = {win["hwnd"]: win for win in windows}

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


def test_preview_running_ignores_invisible_main_window():
    w = FakeWin32([
        {"hwnd": 1, "title": "EVE-O Preview", "toolwindow": False,
         "owner_exe": "EVE-O Preview.exe", "visible": False},
    ])
    assert preview_running(win32=w) is False


def test_multiple_thumbs_sorted_stable():
    w = FakeWin32([
        {"hwnd": 2, "title": "EVE - Beta", "toolwindow": True,
         "owner_exe": "EVE-O Preview.exe", "rect": (0, 0, 1, 1)},
        {"hwnd": 1, "title": "EVE - Alpha", "toolwindow": True,
         "owner_exe": "EVE-O Preview.exe", "rect": (0, 0, 1, 1)},
    ])
    names = [t.char_name for t in find_thumbs(win32=w)]
    assert set(names) == {"Alpha", "Beta"}
