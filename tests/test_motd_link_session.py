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
