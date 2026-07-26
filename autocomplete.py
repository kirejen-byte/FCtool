"""
Autocomplete Entry Widget
Tkinter entry with dropdown autocomplete for EVE system names.
Supports display labels (e.g., "Jita (The Forge)") while inserting just the value ("Jita").

The suggestion list is a borderless (``overrideredirect``) Toplevel. Two
Windows-specific behaviours shape the code below and are documented at their
call sites: the popup needs a ``-topmost`` band to stack over the (active) main
window, but that band must be RELEASED whenever the application itself is not
active — see :meth:`AutocompleteEntry._sync_topmost` — and its geometry must be
clamped onto the virtual desktop — see :meth:`AutocompleteEntry._screen_edges`.
"""

import tkinter as tk

#: How long to wait, after a window that was holding the input focus loses it,
#: before asking whether the WHOLE APPLICATION lost it. Windows defocuses the
#: old window BEFORE focusing the new one, so an immediate probe reads "nothing
#: focused" during a purely in-app hand-off (entry <-> popup). Invisible to the
#: user: it only re-bands an already-drawn window.
_APP_FOCUS_SETTLE_MS = 200


class AutocompleteEntry(tk.Entry):
    """
    Entry widget with dropdown autocomplete.
    Filters suggestions as the user types.

    completions: list of values (system names)
    labels: optional dict {value: display_label} for showing extra info in dropdown
    """

    def __init__(self, master, completions: list[str], max_shown: int = 12,
                 labels: dict[str, str] | None = None, on_select=None, **kwargs):
        super().__init__(master, **kwargs)
        self._completions = completions
        self._completions_lower = [(c, c.lower()) for c in completions]
        self._labels = labels or {}  # value -> display text
        self._max_shown = max_shown
        # Optional callback fired when a value is chosen from the dropdown
        # (click/Enter/Tab). Lets callers react to a SELECTION, which — unlike
        # typing — never raises <KeyRelease>.
        self._on_select = on_select
        self._listbox: tk.Toplevel | None = None
        self._lb: tk.Listbox | None = None
        self._current_matches: list[str] = []  # stores values (not display text)

        #: Last ``-topmost`` value pushed onto the live popup (None = unknown /
        #: no popup). Compare-before-set, so a re-assert costs no SetWindowPos.
        self._dd_topmost = None
        #: Pending ``_sync_topmost`` handle (the settle-delayed app-focus recheck).
        self._topmost_after = None
        #: Pending focus-out close handle. Tracked (it was fire-and-forget) so a
        #: stale timer can never close a dropdown opened AFTER it was armed, and
        #: so a widget torn down inside the delay leaves no timer pointing at a
        #: deleted Tcl command. Nothing cancels it to KEEP a dropdown alive —
        #: that would be the "open with no path to close" trap.
        self._close_after = None

        self.bind("<KeyRelease>", self._on_key)
        self.bind("<FocusOut>", self._on_focus_out)
        self.bind("<Escape>", self._close_dropdown)
        self.bind("<Down>", self._move_selection_down)
        self.bind("<Up>", self._move_selection_up)
        self.bind("<Return>", self._on_enter)
        self.bind("<Tab>", self._on_tab)

        # App-level focus tracking (topmost re-sync). Neither the entry's nor the
        # popup's own <FocusIn> fires when the user returns to the application by
        # clicking some THIRD widget, so bind the containing toplevel: <FocusIn>
        # propagates to every ancestor between the toplevel and the newly focused
        # widget (but NOT to unrelated siblings), which makes the toplevel the one
        # widget guaranteed to be an ancestor of whatever the user actually clicks.
        # add="+" because the toplevel is shared app-wide, and torn down on our own
        # <Destroy> so a long-lived root never accumulates one callback per entry.
        self._app_toplevel = self.winfo_toplevel()
        self._app_focus_in_id = self._app_toplevel.bind(
            "<FocusIn>", self._on_app_focus_in, add="+")
        self.bind("<Destroy>", self._on_entry_destroy, add="+")

    def update_completions(self, completions: list[str],
                           labels: dict[str, str] | None = None):
        """Update the completion list and optional labels."""
        self._completions = completions
        self._completions_lower = [(c, c.lower()) for c in completions]
        if labels is not None:
            self._labels = labels
        try:
            if self.focus_get() is self:
                self._refresh_dropdown()
        except tk.TclError:
            pass

    def _on_key(self, event):
        if event.keysym in ("Down", "Up", "Return", "Tab", "Escape",
                            "Shift_L", "Shift_R", "Control_L", "Control_R",
                            "Alt_L", "Alt_R"):
            return

        self._refresh_dropdown()

    def _refresh_dropdown(self):
        text = self.get().strip()
        if len(text) < 1:
            self._close_dropdown()
            return

        text_lower = text.lower()
        # Prioritize starts-with matches, then contains matches
        starts = []
        contains = []
        for name, name_lower in self._completions_lower:
            if name_lower.startswith(text_lower):
                starts.append(name)
            elif text_lower in name_lower:
                contains.append(name)

        matches = starts + contains
        if not matches:
            self._close_dropdown()
            return

        self._show_dropdown(matches[:self._max_shown])

    def _show_dropdown(self, matches: list[str]):
        """Show dropdown. matches contains values; display uses labels if available."""
        self._current_matches = matches

        if self._listbox:
            self._lb.delete(0, tk.END)
            # Re-assert on every refresh, not only at creation: a popup that
            # released the band while the user was in another application must
            # come back on top of the main window as soon as they type again.
            self._apply_topmost(True)
        else:
            self._listbox = tk.Toplevel(self)
            self._listbox.wm_overrideredirect(True)
            self._dd_topmost = None
            self._apply_topmost(True)
            # A mouse press on a suggestion moves the input focus onto this
            # popup, and from then on it is the ONLY window of ours that sees an
            # app switch (the entry and the root get nothing). Bind both edges.
            self._listbox.bind("<FocusOut>", self._on_dropdown_focus_out, add="+")
            self._listbox.bind("<FocusIn>", self._on_dropdown_focus_in, add="+")

            self._lb = tk.Listbox(
                self._listbox,
                font=("Consolas", 10),
                bg="#0f3460",
                fg="#e0e0e0",
                selectbackground="#1a5a90",
                selectforeground="#ffffff",
                borderwidth=1,
                relief=tk.RIDGE,
                activestyle="none",
            )
            self._lb.pack(fill=tk.BOTH, expand=True)
            self._lb.bind("<ButtonRelease-1>", self._on_click)
            self._lb.bind("<Double-Button-1>", self._on_click)

        for m in matches:
            display = self._labels.get(m, m)
            self._lb.insert(tk.END, display)

        # Position below the entry — wider to fit region names
        w = max(self.winfo_width(), 350)
        h = min(len(matches), self._max_shown) * 20 + 4
        x = self.winfo_rootx()
        top_ref = self.winfo_rooty()
        y = top_ref + self.winfo_height()
        # …then keep it on the desktop: a full 12-row list is ~244px deep and a
        # 350px-wide popup easily overhangs a narrow entry, so an entry near a
        # screen edge would otherwise push suggestions off it entirely. Flip
        # above the entry when there is no room below but room above; clamp
        # either way (which also covers "no room in either direction").
        x0, y0, x1, y1 = self._screen_edges()
        if y + h > y1 and top_ref - h >= y0:
            y = top_ref - h
        x = max(x0, min(x, x1 - w))
        y = max(y0, min(y, y1 - h))
        self._listbox.geometry(f"{w}x{h}+{int(x)}+{int(y)}")
        self._listbox.deiconify()

        if self._lb.size() > 0:
            self._lb.selection_set(0)

    def _screen_edges(self) -> tuple:
        """The usable desktop as EDGES ``(x0, y0, x1, y1)`` — this module's own
        convention (mind the clash: ``preview_layout`` uses ``(x, y, w, h)``).

        Backed by Tk's virtual-root query, which reports the FULL multi-monitor
        area; ``winfo_screenwidth``/``screenheight`` report the PRIMARY monitor
        only and would shove a popup anchored on a secondary monitor into the
        primary monitor's coordinate space. Deliberately self-contained (no
        ctypes, no fc_gui/monitor_pin coupling) and a test seam: override it on
        the instance to simulate a small or offset screen deterministically."""
        x0 = self.winfo_vrootx()
        y0 = self.winfo_vrooty()
        return (x0, y0, x0 + self.winfo_vrootwidth(),
                y0 + self.winfo_vrootheight())

    # -- topmost banding (never outrank other applications) ------------------ #
    def _apply_topmost(self, want: bool) -> None:
        """Push ``want`` onto the live popup's ``-topmost`` band, skipping the
        call when it already holds that value."""
        want = bool(want)
        if self._listbox is None or want == self._dd_topmost:
            return
        try:
            self._listbox.attributes("-topmost", want)
        except tk.TclError:
            return
        self._dd_topmost = want

    def _app_has_focus(self) -> bool:
        """True while SOME window of THIS application holds the input focus.

        Tcl's ``focus -displayof`` reports an empty string when no window of the
        application has the focus, i.e. the user is in a different app (verified
        cross-process on Windows; ``<Activate>``/``<Deactivate>`` are never
        delivered here, so focus is the only signal). Anything unexpected reports
        True: a wrong "active" merely preserves the old behaviour, whereas a
        wrong "inactive" would drop the suggestion list behind the main window."""
        try:
            name = str(self.tk.call("focus", "-displayof", self._w))
        except tk.TclError:
            return True
        return bool(name) and name != "none"

    def _cancel_topmost_recheck(self) -> None:
        if self._topmost_after is not None:
            try:
                self.after_cancel(self._topmost_after)
            except (tk.TclError, ValueError):
                pass
            self._topmost_after = None

    def _arm_topmost_recheck(self) -> None:
        """Re-check the application's focus state once the hand-off has settled,
        rather than deciding from the first (transiently focus-less) reading.
        No popup, nothing to re-band — and no timer either."""
        self._cancel_topmost_recheck()
        if self._listbox is None:
            return
        try:
            self._topmost_after = self.after(_APP_FOCUS_SETTLE_MS,
                                             self._sync_topmost)
        except tk.TclError:
            self._topmost_after = None

    def _sync_topmost(self) -> None:
        """Re-band the open popup against the application's focus state: on top
        while this app is active, an ordinary window once it is not."""
        self._topmost_after = None
        if self._listbox is None:
            return
        self._apply_topmost(self._app_has_focus())

    def _on_dropdown_focus_in(self, _e=None) -> None:
        # Our own popup holds the focus, so the application is demonstrably
        # active: re-band immediately and drop any pending re-check.
        self._cancel_topmost_recheck()
        self._apply_topmost(True)

    def _on_dropdown_focus_out(self, _e=None) -> None:
        self._arm_topmost_recheck()

    def _on_app_focus_in(self, _e=None) -> None:
        """Fires whenever ANY window of this application regains real OS input
        focus — the gap the entry's and the popup's own <FocusIn> leave open (see
        the binding note in ``__init__``). An unambiguous "active" signal."""
        self._cancel_topmost_recheck()
        self._apply_topmost(True)

    def _on_entry_destroy(self, _e=None) -> None:
        """Undo the ``__init__``-time bind on the (shared, longer-lived) toplevel
        so a destroyed entry's handler does not linger on it."""
        self._cancel_close_timer()
        self._cancel_topmost_recheck()
        try:
            self._app_toplevel.unbind("<FocusIn>", self._app_focus_in_id)
        except tk.TclError:
            pass

    def _get_selected_value(self) -> str | None:
        """Get the actual value (not display text) for the selected listbox item."""
        if not self._lb:
            return None
        sel = self._lb.curselection()
        if not sel:
            return None
        idx = sel[0]
        if idx < len(self._current_matches):
            return self._current_matches[idx]
        return None

    def _insert_value(self, value: str):
        """Insert a value into the entry field."""
        self.delete(0, tk.END)
        self.insert(0, value)
        if self._on_select is not None:
            try:
                self._on_select()
            except Exception:
                pass

    def _cancel_close_timer(self) -> None:
        if self._close_after is not None:
            try:
                self.after_cancel(self._close_after)
            except (tk.TclError, ValueError):
                pass
            self._close_after = None

    def _close_dropdown(self, event=None):
        self._cancel_close_timer()
        self._cancel_topmost_recheck()
        if self._listbox:
            self._listbox.destroy()
            self._listbox = None
            self._lb = None
        self._dd_topmost = None          # band state dies with the window
        self._current_matches = []

    def _on_focus_out(self, event):
        # Small delay to allow click events on the listbox to fire first
        self._cancel_close_timer()
        self._close_after = self.after(150, self._close_dropdown)
        # The entry — not the popup — is what normally holds the focus while
        # suggestions are showing, so this is the app-switch edge that matters
        # most. The close above normally wins that race and takes the band with
        # it; arming here keeps the RELEASE from depending on the close's fate,
        # so deferring or cancelling it later (the swallowed-click fix the MOTD
        # palette needed) cannot silently float the popup over other apps again.
        self._arm_topmost_recheck()

    def _on_click(self, event):
        value = self._get_selected_value()
        if value:
            self._insert_value(value)
        self._close_dropdown()

    def _on_enter(self, event):
        value = self._get_selected_value()
        if value:
            self._insert_value(value)
            self._close_dropdown()

    def _on_tab(self, event):
        value = self._get_selected_value()
        if value:
            self._insert_value(value)
            self._close_dropdown()
            return "break"  # Prevent tab from moving focus

    def _move_selection_down(self, event):
        if self._lb:
            sel = self._lb.curselection()
            if sel:
                idx = sel[0] + 1
                if idx < self._lb.size():
                    self._lb.selection_clear(0, tk.END)
                    self._lb.selection_set(idx)
                    self._lb.see(idx)
            elif self._lb.size() > 0:
                self._lb.selection_set(0)
            return "break"

    def _move_selection_up(self, event):
        if self._lb:
            sel = self._lb.curselection()
            if sel:
                idx = sel[0] - 1
                if idx >= 0:
                    self._lb.selection_clear(0, tk.END)
                    self._lb.selection_set(idx)
                    self._lb.see(idx)
            return "break"
