import window_activator as wa


class FakeUser32:
    def __init__(self, iconic=False, fg_results=(True,)):
        self.calls = []
        self.iconic = iconic
        self._fg = list(fg_results)

    def is_iconic(self, hwnd):
        return self.iconic

    def show_window(self, hwnd, cmd):
        self.calls.append(("show", hwnd, cmd))
        if cmd == wa.SW_RESTORE:
            self.iconic = False
        return True

    def set_foreground(self, hwnd):
        self.calls.append(("fg", hwnd))
        return self._fg.pop(0) if self._fg else False

    def alt_nudge(self):
        self.calls.append(("alt",))

    def switch_to_window(self, hwnd):
        self.calls.append(("switch", hwnd))


def test_activate_restores_minimized_then_foregrounds():
    u = FakeUser32(iconic=True, fg_results=(True,))
    assert wa.activate(42, win32=u)
    assert u.calls == [("show", 42, wa.SW_RESTORE), ("fg", 42)]


def test_activate_falls_back_to_alt_nudge_then_switch():
    u = FakeUser32(fg_results=(False, False))
    assert wa.activate(42, win32=u)  # switch fallback counts as handled
    assert ("alt",) in u.calls and ("switch", 42) in u.calls


def test_minimize_silent_and_restore_noactivate():
    u = FakeUser32()
    wa.minimize(42, win32=u)
    wa.restore_no_focus(42, win32=u)
    assert ("show", 42, wa.SW_MINIMIZE) in u.calls
    assert ("show", 42, wa.SW_SHOWNOACTIVATE) in u.calls
