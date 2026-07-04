import eve_client_tracker as ect


class FakeWin32:
    """Dict-of-windows fake, mirrors tests/test_eveo_tracker.py's FakeWin32 shape."""

    def __init__(self, windows):
        # windows: {hwnd: dict(title=..., visible=True, owner_exe="exefile.exe",
        #                      rect=(0,0,100,100), iconic=False, pid=42)}
        self.windows = windows

    def enum_windows(self):
        return list(self.windows)

    def is_visible(self, hwnd):
        return self.windows[hwnd].get("visible", True)

    def get_title(self, hwnd):
        return self.windows[hwnd]["title"]

    def get_owner_exe(self, hwnd):
        return self.windows[hwnd].get("owner_exe", "")

    def get_rect(self, hwnd):
        return self.windows[hwnd].get("rect", (0, 0, 100, 100))

    def is_iconic(self, hwnd):
        return self.windows[hwnd].get("iconic", False)

    def get_pid(self, hwnd):
        return self.windows[hwnd].get("pid", 0)

    def is_window(self, hwnd):
        return hwnd in self.windows


def test_finds_logged_in_login_and_frontier_clients_only():
    w = FakeWin32({
        1: dict(title="EVE - Kirejen", owner_exe="exefile.exe", pid=10),
        2: dict(title="EVE", owner_exe="exefile.exe", pid=11),                # login screen
        3: dict(title="EVE Frontier - Alt Two", owner_exe="exefile.exe", pid=12),
        4: dict(title="EVE - Fake", owner_exe="notepad.exe"),                 # wrong exe
        5: dict(title="EVE-O Preview", owner_exe="EVE-O Preview.exe"),        # the tool
        6: dict(title="EVE - Hidden", owner_exe="exefile.exe", visible=False),
    })
    clients = {c.hwnd: c for c in ect.find_clients(win32=w)}
    assert set(clients) == {1, 2, 3}
    assert clients[1].char_name == "Kirejen"
    assert clients[2].char_name == "" and clients[2].is_login
    assert clients[3].char_name == "Alt Two"


def test_em_dash_titles_are_normalized():
    w = FakeWin32({1: dict(title="EVE — Kirejen", owner_exe="exefile.exe", pid=1)})
    (c,) = ect.find_clients(win32=w)
    assert c.char_name == "Kirejen"


def test_diff_reports_added_retitled_removed():
    a = ect.ClientWindow(hwnd=1, char_name="", title="EVE", rect=(0, 0, 1, 1),
                         is_iconic=False, pid=5)
    b1 = ect.ClientWindow(hwnd=1, char_name="Kirejen", title="EVE - Kirejen",
                          rect=(0, 0, 1, 1), is_iconic=False, pid=5)
    c = ect.ClientWindow(hwnd=2, char_name="Alt", title="EVE - Alt",
                         rect=(0, 0, 1, 1), is_iconic=False, pid=6)
    added, retitled, removed = ect.diff_clients({1: a}, {1: b1, 2: c})
    assert added == [c]
    assert retitled == [(a, b1)]
    added, retitled, removed = ect.diff_clients({1: b1, 2: c}, {2: c})
    assert removed == [b1] and not added and not retitled


def test_identity_check_rejects_reused_hwnd():
    w = FakeWin32({1: dict(title="Calculator", owner_exe="calc.exe", pid=99)})
    old = ect.ClientWindow(hwnd=1, char_name="Kirejen", title="EVE - Kirejen",
                           rect=(0, 0, 1, 1), is_iconic=False, pid=5)
    assert not ect.still_same_client(old, win32=w)


def test_key_strips_and_lowercases():
    # .key must match the ESI poller's `.strip().lower()` state keys exactly,
    # even if a client title carried stray padding around the char name.
    padded = ect.ClientWindow(hwnd=1, char_name=" Kirejen ", title="EVE -  Kirejen ",
                              rect=(0, 0, 1, 1), is_iconic=False, pid=5)
    assert padded.key == "kirejen"
    plain = ect.ClientWindow(hwnd=2, char_name="Alt Two", title="EVE - Alt Two",
                             rect=(0, 0, 1, 1), is_iconic=False, pid=6)
    assert plain.key == "alt two"
