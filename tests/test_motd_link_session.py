"""Regression tests for the session-scoped MOTD auto-update link.

Bug: the "Auto-update MOTD" link state used to be persisted to
config["fittings"]["motd_link"] and restored on startup. After a restart the
autopush loop would then compute the MOTD from the freshly-opened tab's default
state and push that default over the fleet's real MOTD (since
_motd_last_pushed_markup is None on a fresh launch). The fix makes the link a
deliberate per-session action: it always starts OFF, and the gate in
_motd_maybe_autopush returns immediately when the link is disabled.

These tests pin both halves of the fix:
  1. the init decision (_motd_link_initial_state) ignores persisted config, and
  2. the autopush gate no-ops (never touches auth) when the link is off.
"""

import types

import pytest

import fc_gui


def test_motd_link_starts_off_despite_persisted_config():
    """_motd_link_initial_state must ignore any persisted "motd_link" value and
    always return False, so a freshly-opened MOTD is never auto-pushed on
    startup."""
    # Even a config that explicitly persisted the link as enabled…
    cfg_on = {"fittings": {"motd_link": True}}
    assert fc_gui._motd_link_initial_state(cfg_on) is False

    # …and the off / missing / malformed cases all resolve to False too.
    assert fc_gui._motd_link_initial_state({"fittings": {"motd_link": False}}) is False
    assert fc_gui._motd_link_initial_state({"fittings": {}}) is False
    assert fc_gui._motd_link_initial_state({}) is False


def test_motd_maybe_autopush_noop_when_link_disabled():
    """When the link is disabled, _motd_maybe_autopush must return immediately
    without resolving the FC auth (the gate that prevents a startup push)."""

    def _explode():
        pytest.fail(
            "_motd_maybe_autopush touched FC auth while the link was disabled; "
            "the disabled-link gate is broken"
        )

    host = types.SimpleNamespace()
    host._motd_link_enabled = False
    # If the gate is broken and execution falls through, resolving the auth is
    # the very next thing it does — make that fatal so the test fails loudly.
    host._motd_selected_fc_auth = _explode
    host.esi_auth = None

    bound = types.MethodType(fc_gui.FCToolGUI._motd_maybe_autopush, host)
    # Should simply return None without raising / without calling _explode.
    assert bound() is None


# ── First-manual-Set arms the auto-update link for the session ──────────────
#
# Startup stays OFF (pinned above), but the user's refinement is that a
# *successful* first manual "Set as fleet MOTD" push should switch auto-update
# ON automatically — no extra click. A failed / not-boss push must not arm it.
# These drive the REAL _set_fleet_motd completion path (its worker + root.after
# marshalling run inline) so the actual wiring — not a reimplementation — is
# under test.


class _FakeVar:
    """Minimal tk.BooleanVar stand-in (get/set)."""

    def __init__(self, value=False):
        self._v = bool(value)

    def get(self):
        return self._v

    def set(self, value):
        self._v = bool(value)

    def config(self, **_kw):  # some code paths call .config on widgets
        pass


class _FakeWidget:
    """No-op stand-in for a tk widget (only .config is exercised here)."""

    def config(self, **_kw):
        pass


class _InlineRoot:
    """root.after(delay, fn, *args) that runs fn(*args) immediately."""

    def after(self, _delay, fn, *args):
        fn(*args)


class _FakeAuth:
    def __init__(self, ok):
        self._ok = ok
        self.is_authenticated = True

    def set_fleet_motd(self, _fleet_id, _markup):
        return self._ok


def _make_set_host(markup, *, boss=True, push_ok=True):
    """Build a SimpleNamespace host wired for the real _set_fleet_motd path."""
    host = types.SimpleNamespace()
    host.root = _InlineRoot()
    host._motd_is_boss = boss
    host._motd_fleet_id = 42 if boss else None
    host.esi_auth = _FakeAuth(push_ok)
    host._motd_selected_fc_auth = lambda: _FakeAuth(push_ok)
    host._motd_fleet_status = _FakeWidget()
    host._motd_output_markup = lambda: (markup, False)
    host._motd_budget = lambda: 3000
    host._auto_select_fleet_doctrine = lambda _name: None
    # Link state as it is at tab-build time: OFF, nothing pushed yet.
    host._motd_link_enabled = False
    host._motd_link_var = _FakeVar(False)
    host._motd_link_state = "off"
    host._motd_last_pushed_markup = None
    host._motd_last_push_ts = None
    host._motd_last_check_ts = None
    host._motd_link_indicator = None  # _motd_update_link_indicator no-ops
    # Bind the REAL completion helpers the success path invokes, so the actual
    # arming logic (not a stub) is what runs under test.
    host._motd_arm_link = types.MethodType(fc_gui.FCToolGUI._motd_arm_link, host)
    host._motd_update_link_indicator = types.MethodType(
        fc_gui.FCToolGUI._motd_update_link_indicator, host
    )
    return host


def _run_set_fleet_motd(host, monkeypatch):
    """Invoke the real _set_fleet_motd with the worker thread run inline."""

    class _FakeThread:
        def __init__(self, target=None, daemon=None, **_kw):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(fc_gui.threading, "Thread", _FakeThread)
    # Guard against any dialog being invoked in the success/failure paths.
    monkeypatch.setattr(
        fc_gui.messagebox, "showwarning", lambda *a, **k: None, raising=False
    )
    monkeypatch.setattr(
        fc_gui.messagebox, "showerror", lambda *a, **k: None, raising=False
    )
    monkeypatch.setattr(
        fc_gui.messagebox, "askyesno", lambda *a, **k: True, raising=False
    )
    bound = types.MethodType(fc_gui.FCToolGUI._set_fleet_motd, host)
    bound()


def test_manual_set_arms_autoupdate(monkeypatch):
    """A successful manual 'Set as fleet MOTD' arms the session link: the link
    becomes enabled, the checkbox var flips True, and the just-pushed markup is
    recorded so the autopush loop won't immediately re-push the identical text.
    """
    markup = "<font>doctrine A — 3 tackle needed</font>"
    host = _make_set_host(markup, boss=True, push_ok=True)

    _run_set_fleet_motd(host, monkeypatch)

    assert host._motd_link_enabled is True
    assert host._motd_link_var.get() is True
    assert host._motd_link_state == "ok"
    assert host._motd_last_pushed_markup == markup
    assert host._motd_last_push_ts is not None


def test_failed_set_does_not_arm(monkeypatch):
    """A push that ESI rejects (set_fleet_motd -> False) must leave the link
    OFF — a failed manual Set never arms auto-update."""
    markup = "<font>doctrine B</font>"
    host = _make_set_host(markup, boss=True, push_ok=False)

    _run_set_fleet_motd(host, monkeypatch)

    assert host._motd_link_enabled is False
    assert host._motd_link_var.get() is False
    assert host._motd_last_pushed_markup is None
    assert host._motd_last_push_ts is None


def test_not_boss_set_does_not_arm(monkeypatch):
    """The not-boss guard returns before any push, so the link stays OFF."""
    markup = "<font>doctrine C</font>"
    host = _make_set_host(markup, boss=False, push_ok=True)

    _run_set_fleet_motd(host, monkeypatch)

    assert host._motd_link_enabled is False
    assert host._motd_link_var.get() is False
    assert host._motd_last_pushed_markup is None
