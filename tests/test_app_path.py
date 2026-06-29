import os
import app_path


def _reset():
    app_path._resolved_app_dir = None


def test_app_dir_source_mode(monkeypatch):
    _reset()
    monkeypatch.setattr(app_path.sys, "frozen", False, raising=False)
    assert app_path.app_dir() == os.path.dirname(os.path.abspath(app_path.__file__))
    _reset()


def test_app_dir_uses_exe_dir_when_writable(monkeypatch, tmp_path):
    _reset()
    monkeypatch.setattr(app_path.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app_path.sys, "executable", str(tmp_path / "FCTool.exe"))
    monkeypatch.setattr(app_path, "_is_dir_writable", lambda p: True)
    assert app_path.app_dir() == str(tmp_path)
    _reset()


def test_app_dir_falls_back_to_localappdata_when_readonly(monkeypatch, tmp_path):
    _reset()
    monkeypatch.setattr(app_path.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app_path.sys, "executable",
                        r"C:\Program Files\FC manage\FCTool.exe")
    monkeypatch.setattr(app_path, "_is_dir_writable", lambda p: False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    d = app_path.app_dir()
    assert d == str(tmp_path / "FCTool")
    assert os.path.isdir(d)            # created on demand
    _reset()


def test_app_dir_is_cached(monkeypatch, tmp_path):
    _reset()
    monkeypatch.setattr(app_path.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app_path.sys, "executable", str(tmp_path / "FCTool.exe"))
    calls = {"n": 0}
    def probe(p):
        calls["n"] += 1
        return True
    monkeypatch.setattr(app_path, "_is_dir_writable", probe)
    app_path.app_dir(); app_path.app_dir()
    assert calls["n"] == 1              # probed once, then cached
    _reset()
