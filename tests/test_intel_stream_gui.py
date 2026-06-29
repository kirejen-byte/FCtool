import pytest
tk = pytest.importorskip("tkinter")
# NOTE (test-scaffold deviation): the plan prescribed a module-level
#   _root = tk.Tk(); _root.destroy()
# headless guard. On this Windows Tcl build that destroy corrupts the Tcl
# library-path resolution and makes the NEXT tk.Tk() fail with a spurious
# "couldn't read init.tcl". The repo's own Tk tests (test_fleet_template_window)
# instead create a fresh root per test and never pre-create/destroy one at
# import. We follow that working convention: no module-level root; each test's
# fresh tk.Tk() (built in _make_host) is the first interpreter and works. The
# "no display" skip intent is preserved by skipping on TclError in _make_host.

import collections
import types

import fc_gui
from intel_stream import Span


class FakeMsg:
    def __init__(self, channel, sender, message, ts=None):
        from datetime import datetime
        self.channel = channel
        self.sender = sender
        self.message = message
        self.timestamp = ts or datetime(2026, 6, 29, 12, 0, 0)


def _make_host():
    """Minimal host with a real Text widget + the stream methods bound."""
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display available")
    host = types.SimpleNamespace()
    host.root = root
    txt = tk.Text(root)
    host._intel_log = txt
    host._intel_buffer = collections.deque(maxlen=5)
    host._intel_channels_enabled = {"Delve.Intel"}
    host._intel_find_var = tk.StringVar(value="")
    host._intel_new_count = 0
    host._intel_channel_colors = {}
    host._intel_autoscroll_paused = False
    host._intel_new_btn = None
    host._intel_resolver = None
    # bind the real methods. _bind_system_span / _bind_dscan_span are needed
    # so _render_line can wire system + dscan spans; _intel_apply_resolutions
    # and _intel_system_menu are bound (Task-8 inert stubs) so the system/dscan
    # span paths never raise AttributeError during the smoke test.
    for name in ("_render_line", "_intel_stream_ingest", "_channel_color",
                 "_passes_view_filter", "_intel_update_new_button",
                 "_bind_system_span", "_bind_dscan_span",
                 "_intel_apply_resolutions", "_intel_system_menu"):
        setattr(host, name, types.MethodType(getattr(fc_gui.FCToolGUI, name), host))
    # class-level attrs the bound methods reference but SimpleNamespace lacks
    host._CHANNEL_PALETTE = fc_gui.FCToolGUI._CHANNEL_PALETTE
    # configure the tags the renderer uses
    for tag in ("intel_system", "intel_count", "intel_clear", "intel_camp",
                "intel_spike", "intel_cyno", "intel_dscan", "intel_priority",
                "channel"):
        txt.tag_config(tag)
    return root, host, txt


def test_buffer_trims_at_cap():
    root, host, txt = _make_host()
    try:
        for i in range(8):
            host._intel_stream_ingest(
                FakeMsg("Delve.Intel", "Scout", f"line {i}"), [], None, False)
        assert len(host._intel_buffer) == 5  # deque maxlen
    finally:
        root.destroy()


def test_render_line_writes_verbatim_and_tags_system():
    root, host, txt = _make_host()
    try:
        msg = FakeMsg("Delve.Intel", "Scout", "Amamake 5 reds")
        spans = [Span(0, 7, "system", "Amamake", {"system_id": 30002187}),
                 Span(8, 9, "count", "5", {"count": 5})]
        host._render_line((msg, spans, None, False))
        body = txt.get("1.0", "end-1c")
        assert "Delve.Intel" in body
        assert "Scout > Amamake 5 reds" in body
        # the system tag covers "Amamake" somewhere in the line
        ranges = txt.tag_ranges("intel_system")
        assert ranges
        covered = txt.get(ranges[0], ranges[1])
        assert covered == "Amamake"
    finally:
        root.destroy()


def test_hidden_channel_not_rendered():
    root, host, txt = _make_host()
    try:
        host._intel_stream_ingest(
            FakeMsg("OtherChan", "X", "hello"), [], None, False)
        # buffered (source of truth) but not rendered (channel disabled)
        assert len(host._intel_buffer) == 1
        assert "hello" not in txt.get("1.0", "end-1c")
    finally:
        root.destroy()


def test_autoscroll_pause_increments_new_counter():
    root, host, txt = _make_host()
    try:
        host._intel_autoscroll_paused = True
        host._intel_stream_ingest(
            FakeMsg("Delve.Intel", "X", "a line"), [], None, False)
        assert host._intel_new_count == 1
    finally:
        root.destroy()


# high_priority is a MODULE-LEVEL pure function in fc_gui (introduced here,
# reused unchanged by Task 10). Its tests live with its definition.
def test_high_priority_meets_min_reported():
    from intel_monitor import IntelReport
    from datetime import datetime
    r = IntelReport(timestamp=datetime.now(), channel="c", reporter="r",
                    system_name="Jita", pilot_count=12)
    assert fc_gui.high_priority(r, 10) is True
    assert fc_gui.high_priority(r, 20) is False


def test_high_priority_route_from_staging():
    from intel_monitor import IntelReport
    from datetime import datetime
    r = IntelReport(timestamp=datetime.now(), channel="c", reporter="r",
                    system_name="Jita", pilot_count=0,
                    route_from_staging="Stage -> Jita: **2 jumps**")
    assert fc_gui.high_priority(r, 0) is True


def test_high_priority_none_report():
    assert fc_gui.high_priority(None, 5) is False
