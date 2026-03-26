"""
Autocomplete Entry Widget
Tkinter entry with dropdown autocomplete for EVE system names.
Supports display labels (e.g., "Jita (The Forge)") while inserting just the value ("Jita").
"""

import tkinter as tk


class AutocompleteEntry(tk.Entry):
    """
    Entry widget with dropdown autocomplete.
    Filters suggestions as the user types.

    completions: list of values (system names)
    labels: optional dict {value: display_label} for showing extra info in dropdown
    """

    def __init__(self, master, completions: list[str], max_shown: int = 12,
                 labels: dict[str, str] | None = None, **kwargs):
        super().__init__(master, **kwargs)
        self._completions = completions
        self._completions_lower = [(c, c.lower()) for c in completions]
        self._labels = labels or {}  # value -> display text
        self._max_shown = max_shown
        self._listbox: tk.Toplevel | None = None
        self._lb: tk.Listbox | None = None
        self._current_matches: list[str] = []  # stores values (not display text)

        self.bind("<KeyRelease>", self._on_key)
        self.bind("<FocusOut>", self._on_focus_out)
        self.bind("<Escape>", self._close_dropdown)
        self.bind("<Down>", self._move_selection_down)
        self.bind("<Up>", self._move_selection_up)
        self.bind("<Return>", self._on_enter)
        self.bind("<Tab>", self._on_tab)

    def update_completions(self, completions: list[str],
                           labels: dict[str, str] | None = None):
        """Update the completion list and optional labels."""
        self._completions = completions
        self._completions_lower = [(c, c.lower()) for c in completions]
        if labels is not None:
            self._labels = labels

    def _on_key(self, event):
        if event.keysym in ("Down", "Up", "Return", "Tab", "Escape",
                            "Shift_L", "Shift_R", "Control_L", "Control_R",
                            "Alt_L", "Alt_R"):
            return

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
        else:
            self._listbox = tk.Toplevel(self)
            self._listbox.wm_overrideredirect(True)
            self._listbox.attributes("-topmost", True)

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
        x = self.winfo_rootx()
        y = self.winfo_rooty() + self.winfo_height()
        w = max(self.winfo_width(), 350)
        h = min(len(matches), self._max_shown) * 20 + 4
        self._listbox.geometry(f"{w}x{h}+{x}+{y}")
        self._listbox.deiconify()

        if self._lb.size() > 0:
            self._lb.selection_set(0)

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

    def _close_dropdown(self, event=None):
        if self._listbox:
            self._listbox.destroy()
            self._listbox = None
            self._lb = None
        self._current_matches = []

    def _on_focus_out(self, event):
        # Small delay to allow click events on the listbox to fire first
        self.after(150, self._close_dropdown)

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
