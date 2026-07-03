import queue

import hotkey_service as hs


def test_parse_hotkey_strings():
    assert hs.parse_hotkey("F13") == (hs.MOD_NOREPEAT, 0x7C)
    assert hs.parse_hotkey("Control+Shift+F4") == (
        hs.MOD_CONTROL | hs.MOD_SHIFT | hs.MOD_NOREPEAT, 0x73)
    assert hs.parse_hotkey("Alt+X") == (hs.MOD_ALT | hs.MOD_NOREPEAT, ord("X"))
    assert hs.parse_hotkey("Ctrl+9") == (hs.MOD_CONTROL | hs.MOD_NOREPEAT, ord("9"))
    with __import__("pytest").raises(ValueError):
        hs.parse_hotkey("Windows+F1")   # Win modifier unsupported (EVE-O parity)
    with __import__("pytest").raises(ValueError):
        hs.parse_hotkey("Ctrl+")


class ScriptedBackend:
    """Fake user32: scripted GetMessage stream; records register/unregister."""

    def __init__(self, fail_ids=(), script=()):
        self.registered, self.unregistered = [], []
        self.fail_ids = set(fail_ids)
        self._script = list(script)      # [(msg, wparam), ...] then auto WM_QUIT
        self.thread_id = 777

    def current_thread_id(self):
        return self.thread_id

    def register(self, hk_id, mods, vk):
        self.registered.append((hk_id, mods, vk))
        return hk_id not in self.fail_ids

    def last_error(self):
        return 1409  # ERROR_HOTKEY_ALREADY_REGISTERED

    def unregister(self, hk_id):
        self.unregistered.append(hk_id)

    def get_message(self):
        if self._script:
            return self._script.pop(0)
        return (hs.WM_QUIT, 0)

    def post_quit(self, thread_id):
        self._script = []


def test_service_delivers_events_and_reports_conflicts():
    be = ScriptedBackend(fail_ids={2},
                         script=[(hs.WM_HOTKEY, 1), (hs.WM_HOTKEY, 1)])
    svc = hs.HotkeyService(backend=be)
    svc.start({1: (hs.MOD_NOREPEAT, 0x7C), 2: (hs.MOD_NOREPEAT, 0x7D)})
    got = [svc.events.get(timeout=2), svc.events.get(timeout=2)]
    svc.stop()
    assert got == [1, 1]
    assert svc.failures == {2: 1409}
    assert sorted(be.unregistered) == [1]   # only successful ids unregistered


def test_restart_replaces_bindings():
    be = ScriptedBackend()
    svc = hs.HotkeyService(backend=be)
    svc.start({1: (0x4000, 0x7C)})
    svc.restart({3: (0x4000, 0x7E)})
    svc.stop()
    assert (3, 0x4000, 0x7E) in be.registered
