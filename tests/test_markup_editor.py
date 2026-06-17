"""Tests for markup_editor.MarkupEditor's pending (typing-style) toolbar.

Bug 3: with no selection every toolbar action used to early-return, so "custom
colour" (and B/I/U, size, clear) did nothing. The fix arms a PENDING style that
applies to subsequently-typed characters; with a selection the old behaviour is
unchanged. These tests pin both paths.

Tk is required and must be MAPPED + FOCUSED — a withdrawn root does NOT insert on
``event_generate``. Tk creation is wrapped so a headless CI (no display) skips
the whole module rather than erroring.
"""
import pytest

import motd_markup
import markup_editor


# A real, on-screen, focused Tk root: needed because event_generate only inserts
# typed characters into a mapped widget that holds focus. Module-scoped so the
# (relatively expensive) root is created once; per-test cleanup wipes the editor.
@pytest.fixture(scope="module")
def root():
    try:
        import tkinter as tk
        r = tk.Tk()
    except Exception as exc:  # pragma: no cover - headless CI without a display
        pytest.skip(f"Tk unavailable in this environment: {exc}")
    r.geometry("500x300+120+120")
    r.deiconify()
    try:
        r.update()
    except Exception as exc:  # pragma: no cover - display present but unusable
        r.destroy()
        pytest.skip(f"Tk root could not be mapped: {exc}")
    yield r
    try:
        r.destroy()
    except Exception:
        pass


@pytest.fixture
def ed(root):
    """A fresh, focused MarkupEditor packed into the mapped root."""
    editor = markup_editor.MarkupEditor(root)
    editor.pack(fill="both", expand=True)
    editor.text.focus_force()
    root.update()
    yield editor
    editor.destroy()
    root.update()


def _type(editor, root, text):
    """Type ``text`` via real key events so the pending handlers run.

    event_generate on Windows derives e.char from keysym; e.state carries only
    the Shift bit for capitals (not Control/Alt), so the pending arm-guard fires.
    """
    for ch in text:
        editor.text.event_generate("<KeyPress>", keysym=ch)
        editor.text.event_generate("<KeyRelease>", keysym=ch)
        root.update()


# --- 1. no-selection arms pending (with fg_/size_ exclusivity) -------------

def test_no_selection_arms_pending(ed):
    # No selection: each action arms a pending tag rather than no-op'ing.
    ed.apply_color("#ff4444")
    ed.toggle_bold()
    ed.apply_size(18)

    assert "fg_ff4444" in ed._pending_tags
    assert "bold" in ed._pending_tags
    assert "size_18" in ed._pending_tags

    # fg_ exclusivity: a second colour replaces the first (one colour pending).
    ed.apply_color("#00ff88")
    fg_pending = {t for t in ed._pending_tags if t.startswith("fg_")}
    assert fg_pending == {"fg_00ff88"}

    # size_ exclusivity: a second size replaces the first (one size pending).
    ed.apply_size(12)
    size_pending = {t for t in ed._pending_tags if t.startswith("size_")}
    assert size_pending == {"size_12"}


# --- 2. custom colour opens the dialog regardless of selection -------------

def test_custom_color_opens_dialog_regardless_of_selection(ed, monkeypatch):
    calls = {"n": 0}

    def fake_askcolor(*args, **kwargs):
        calls["n"] += 1
        return ((255, 0, 0), "#ff0000")

    monkeypatch.setattr(markup_editor.colorchooser, "askcolor", fake_askcolor)

    # The bug: with NO selection the dialog never opened. Now it must.
    ed._pick_custom_color()
    assert calls["n"] == 1
    assert "fg_ff0000" in ed._pending_tags

    # With a selection it applies to the range (today's path).
    ed._clear_pending()
    ed.text.insert("1.0", "hello")
    ed.text.tag_add("sel", "1.0", "1.5")
    ed.text.focus_force()
    ed._pick_custom_color()
    assert calls["n"] == 2
    assert "<color=0xffFF0000>" in ed.get_markup()


# --- 3. pending → typing round-trips through get_markup --------------------

def test_pending_typing_round_trip(ed, root):
    # Arm red + bold, type RED; then clear pending, type plain.
    ed.apply_color("#ff0000")
    ed.toggle_bold()
    _type(ed, root, "RED")

    ed._clear_pending()
    _type(ed, root, "plain")

    markup = ed.get_markup()
    assert markup == "<color=0xffFF0000><b>RED</b></color>plain"
    # And it survives a full parse→serialise round-trip through the Tk-free
    # module byte-for-byte (not merely "parses to something truthy").
    assert motd_markup.segments_to_markup(
        motd_markup.parse_markup(markup)) == markup


# --- 4. navigation keys must not leak the pending style onto existing text --

def test_navigation_does_not_leak_pending(ed, root):
    ed.text.insert("1.0", "abc")
    ed.text.mark_set("insert", "1.0")
    root.update()

    ed.apply_color("#ff0000")  # arm a pending colour
    for ev in ("<Left>", "<Right>", "<Home>", "<End>", "<BackSpace>",
               "<Control-a>"):
        ed.text.event_generate(ev)
        root.update()

    # No character may carry an fg_* tag — cursor advance is not a real insert.
    content = ed.text.get("1.0", "end-1c")
    idx = "1.0"
    for _ch in content:
        tags = ed.text.tag_names(idx)
        assert not any(t.startswith("fg_") for t in tags)
        idx = ed.text.index(f"{idx} +1c")


# --- 5. selection path is unchanged ----------------------------------------

def test_selection_path_unchanged(ed):
    ed.text.insert("1.0", "hello")
    ed.text.tag_add("sel", "1.0", "1.3")  # select 'hel'

    ed.apply_color("#ff0000")
    ed.toggle_bold()
    ed.apply_size(18)

    # Same markup the editor produced before the pending mechanism existed, and
    # the selection path leaves nothing pending.
    assert ed.get_markup() == (
        "<color=0xffFF0000><fontsize=18><b>hel</b></fontsize></color>lo")
    assert ed._pending_tags == set()
