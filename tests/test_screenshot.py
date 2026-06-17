import fc_gui


def test_x11_prefers_maim_with_xclip():
    cap, clip, err = fc_gui.build_linux_screenshot_cmds(False, {"maim", "scrot", "xclip"}, 10, 20, 300, 400, "/tmp/o.png")
    assert err is None
    assert cap == ["maim", "-g", "300x400+10+20", "/tmp/o.png"]
    assert clip == ["xclip", "-selection", "clipboard", "-t", "image/png"]


def test_x11_falls_back_to_scrot_then_import():
    cap, _, _ = fc_gui.build_linux_screenshot_cmds(False, {"scrot", "xclip"}, 0, 0, 100, 100, "/tmp/o.png")
    assert cap[0] == "scrot" and cap[1:] == ["-a", "0,0,100,100", "/tmp/o.png"]
    cap2, _, _ = fc_gui.build_linux_screenshot_cmds(False, {"import"}, 0, 0, 100, 100, "/tmp/o.png")
    assert cap2[0] == "import"


def test_x11_no_clipboard_tool_returns_none_clip():
    cap, clip, err = fc_gui.build_linux_screenshot_cmds(False, {"maim"}, 0, 0, 10, 10, "/tmp/o.png")
    assert err is None and cap[0] == "maim" and clip is None


def test_no_capture_tool_returns_error():
    cap, clip, err = fc_gui.build_linux_screenshot_cmds(False, set(), 0, 0, 10, 10, "/tmp/o.png")
    assert cap is None and clip is None and err


def test_wayland_uses_grim_and_wlcopy():
    cap, clip, err = fc_gui.build_linux_screenshot_cmds(True, {"grim", "wl-copy"}, 5, 6, 7, 8, "/tmp/o.png")
    assert err is None
    assert cap == ["grim", "-g", "5,6 7x8", "/tmp/o.png"]
    assert clip == ["wl-copy", "--type", "image/png"]
