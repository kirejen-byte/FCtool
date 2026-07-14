"""Infrastructure manager dialog (Task 6, plan §3.9).

A hidden Toplevel for editing the friendly-structure database: clipboard import
(two-phase preview with an NPC-include toggle), ESI region-scan controls, a
sortable/searchable structure list, per-region scan config, and manual add/edit.

Architecture rule (plan §2): this module imports **tkinter only**. The store,
scanner and the import/clipboard callables all arrive as constructor arguments
and are duck-typed — infra_dialog never imports a sibling ``infra_*`` module nor
``fc_gui``/``map_tab``. ``fc_gui`` (Task 7) is the sole composer that wires the
real InfraStore / InfraScanner / infra_parser together and passes them in.

All operations run synchronously on the Tk thread. The only async surface is the
scanner's progress/done callbacks, which the host marshals through ``_post_ui``
before calling ``push_scan_progress`` / ``push_scan_done`` / ``set_scan_status``
here — so this module makes no thread-safety assumptions of its own.
"""
import logging
import tkinter as tk
from tkinter import ttk, messagebox

log = logging.getLogger(__name__)

# Canonical category order — a frozen contract constant (plan §3.2). Re-declared
# locally rather than imported from infra_parser to honour the "tkinter only"
# isolation rule; must stay in lockstep with infra_parser.CATEGORIES.
CATEGORIES = ("citadel", "engineering", "refinery", "gate", "flex", "npc", "unknown")
# Manual add offers every real category except NPC stations (plan §3.9).
MANUAL_CATEGORIES = tuple(c for c in CATEGORIES if c != "npc")

# Always appended to any scan-results line (plan §6 UI-copy requirement / §3.5).
SCAN_CAVEAT = ("ESI scan can miss role-gated structures — "
               "paste from the structure browser for full coverage.")

# Palette mirrors fc_gui / fleet_template_window (importing them would break the
# isolation rule and, for fc_gui, be circular).
BG_DARK = "#1a1a1a"
BG_PANEL = "#252525"
BG_ENTRY = "#2d2d2d"
FG_TEXT = "#d0d0d0"
FG_DIM = "#808080"
FG_ACCENT = "#4ea1d3"
FG_GREEN = "#5fb85f"
FG_YELLOW = "#d6b656"
FG_RED = "#d35f5f"
BORDER_COLOR = "#3a3a3a"

# Tree columns (order is the display order) and their header labels.
#
# Owner UX round: the "category" and "type" column KEYS keep their positions, but
# their SEMANTICS are swapped. The prominent "category" slot now shows the
# SPECIFIC structure type (header "Type", e.g. "Fortizar"/"Keepstar" — sortable
# via the existing header machinery); the narrower "type" slot now shows the
# coarse category (header "Category", e.g. "citadel"). Keys stay put so
# _sort_key (which zips COLUMNS with _row_values) needs no change.
COLUMNS = ("system", "name", "category", "type", "source", "last_seen", "status")
HEADINGS = {"system": "System", "name": "Name", "category": "Type",
            "type": "Category", "source": "Source", "last_seen": "Last seen",
            "status": "Status"}
_COL_WIDTH = {"system": 90, "name": 240, "category": 150, "type": 92,
              "source": 84, "last_seen": 128, "status": 74}


def entry_key(entry: dict) -> str:
    """Canonical store key for an entry dict (plan §3.1): ``str(structure_id)``
    when present, else ``name::{system_name}::{name}``. The dialog derives keys
    from the entry dicts ``store.entries()`` hands back so it can address
    ``store.remove`` / ``set_status`` / ``set_notes`` without a lookup method."""
    sid = entry.get("structure_id")
    if sid is not None:
        return str(sid)
    return "name::{}::{}".format(entry.get("system_name", "") or "",
                                 entry.get("name", "") or "")


def _fmt_last_seen(iso) -> str:
    """Trim an aware-UTC ISO timestamp to 'YYYY-MM-DD HH:MM' for display."""
    if not iso:
        return ""
    return str(iso)[:16].replace("T", " ")


class _Tooltip:
    """Minimal hover tooltip (used for the 'needs ESI login' hint on disabled
    scan controls). tkinter-only; best-effort — never raises into the caller."""

    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip = None
        try:
            widget.bind("<Enter>", self._show, add="+")
            widget.bind("<Leave>", self._hide, add="+")
        except Exception:
            pass

    def _show(self, _evt=None):
        if self.tip or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 12
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 2
            self.tip = tk.Toplevel(self.widget)
            self.tip.wm_overrideredirect(True)
            self.tip.wm_geometry(f"+{x}+{y}")
            tk.Label(self.tip, text=self.text, bg="#ffffe0", fg="#000000",
                     relief="solid", borderwidth=1, padx=4, pady=1).pack()
        except Exception:
            self.tip = None

    def _hide(self, _evt=None):
        if self.tip is not None:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None


class InfraManagerDialog(tk.Toplevel):
    """Structure-database manager (plan §3.9).

    Constructor contract (byte-for-byte with the plan):

        InfraManagerDialog(parent, store, scanner, regions_catalog, system_names,
                           clipboard_get, import_clipboard, import_manual,
                           on_changed, initial_system_id=None)

    store            InfraStore-shaped (entries/remove/set_status/set_notes/
                     get_regions/set_regions/scan_state/STALE_DAYS).
    scanner          InfraScanner-shaped, or None => scan controls are disabled
                     with a 'needs ESI login' tooltip.
    regions_catalog  list[(region_id, name)].
    system_names     sorted list[str] for the manual-add combobox.
    clipboard_get    zero-arg callable returning clipboard text.
    import_clipboard callable(text, include_npc=False, preview=False) -> dict
                     {"report": MergeReport|None, "links": int, "npc": int,
                      "unparsed": int, "by_category": {cat: n}}.  preview=True
                     parses without writing (report is None).
    import_manual    callable(entry_dict) -> None (source="manual" upsert).
    on_changed       zero-arg callable the host uses to re-push the overlay.
    initial_system_id  pre-filter the list to one system (or None for all).
    type_name_fn     optional callable(type_id, structure_id) -> str giving the
                     SPECIFIC type label for the prominent "Type" column
                     (fc_gui passes infra_parser.type_name). None (default) falls
                     back to the coarse category, honouring the tkinter-only
                     isolation rule (the dialog never imports infra_parser).
    autocomplete_cls optional widget CLASS for the region entry. When it exposes
                     ``update_completions`` (an AutocompleteEntry) it is used so
                     matches pop up AS YOU TYPE; otherwise (default) a plain
                     ttk.Combobox is built. Appended last / backwards compatible.
    """

    def __init__(self, parent, store, scanner, regions_catalog, system_names,
                 clipboard_get, import_clipboard, import_manual,
                 on_changed, initial_system_id=None,
                 type_name_fn=None, autocomplete_cls=None):
        super().__init__(parent)
        self.store = store
        self.scanner = scanner
        self._regions_catalog = list(regions_catalog or [])
        self._region_name = {int(rid): name for rid, name in self._regions_catalog}
        self._system_names = list(system_names or [])
        self._clipboard_get = clipboard_get
        self._import_clipboard = import_clipboard
        self._import_manual = import_manual
        self._on_changed = on_changed if callable(on_changed) else (lambda: None)
        self._system_filter = initial_system_id
        self._type_name_fn = type_name_fn if callable(type_name_fn) else None
        self._autocomplete_cls = autocomplete_cls

        self._sort_col = None
        self._sort_reverse = False
        self._entries_by_key = {}          # key -> entry dict currently in tree

        # sub-window / form handles (created lazily; referenced by tests)
        self._preview_win = None
        self._manual_win = None
        self._edit_win = None
        self._edit_entry = None

        self._search_var = tk.StringVar(self, "")
        self._status_var = tk.StringVar(self, "")

        self.title("Infrastructure Manager")
        self.configure(bg=BG_DARK)
        try:
            self.geometry("980x560")
            self.minsize(720, 400)
        except Exception:
            pass

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self._build_toolbar()      # row 0
        self._build_body()         # row 1 (region panel + tree)
        self._build_statusbar()    # row 2

        self._search_var.trace_add("write", lambda *_a: self._apply_filter())
        self._reload_tree()
        self._refresh_regions()
        if self._system_filter is not None:
            self._set_status(
                "Showing one system — clear Search to see everything.")

    # ── row 0: toolbar ───────────────────────────────────────────────────────
    def _build_toolbar(self):
        bar = tk.Frame(self, bg=BG_PANEL)
        bar.grid(row=0, column=0, sticky="ew")

        def mkbtn(text, cmd):
            b = ttk.Button(bar, text=text, style="Dark.TButton", command=cmd)
            b.pack(side=tk.LEFT, padx=3, pady=4)
            return b

        self._import_btn = mkbtn("Import clipboard", self._on_import_clipboard)
        self._scan_btn = mkbtn("Scan regions now", self._on_scan_now)
        self._add_btn = mkbtn("Add manual…", self._open_add_manual)
        self._reverify_btn = mkbtn("Re-verify selected", self._on_reverify)
        self._markdead_btn = mkbtn("Mark dead", self._on_mark_dead)
        self._delete_btn = mkbtn("Delete", self._on_delete)

        # right-aligned live search
        self._search_entry = ttk.Entry(bar, textvariable=self._search_var, width=24)
        self._search_entry.pack(side=tk.RIGHT, padx=(3, 8), pady=4)
        tk.Label(bar, text="Search:", bg=BG_PANEL, fg=FG_DIM).pack(
            side=tk.RIGHT, pady=4)

        if self.scanner is None:
            self._scan_btn.config(state="disabled")
            self._reverify_btn.config(state="disabled")
            _Tooltip(self._scan_btn, "needs ESI login")
            _Tooltip(self._reverify_btn, "needs ESI login")

    # ── row 1: region panel + structure tree ─────────────────────────────────
    def _build_body(self):
        body = tk.Frame(self, bg=BG_DARK)
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        self._build_region_panel(body)
        self._build_tree(body)

    def _build_region_panel(self, parent):
        panel = tk.Frame(parent, bg=BG_PANEL, width=240)
        panel.grid(row=0, column=0, sticky="nsew", padx=(4, 2), pady=4)
        panel.grid_propagate(False)

        tk.Label(panel, text="Scan regions", bg=BG_PANEL, fg=FG_TEXT).pack(
            anchor="w", padx=6, pady=(6, 2))
        self._region_list = tk.Listbox(
            panel, height=8, exportselection=False, bg=BG_ENTRY, fg=FG_TEXT,
            selectbackground=FG_ACCENT, activestyle="none",
            highlightthickness=0, borderwidth=0)
        self._region_list.pack(fill=tk.BOTH, expand=True, padx=6)

        add_row = tk.Frame(panel, bg=BG_PANEL)
        add_row.pack(fill=tk.X, padx=6, pady=(4, 2))
        names = sorted(n for _rid, n in self._regions_catalog)
        entry_cls = self._autocomplete_cls or ttk.Combobox
        # Capability-gate on update_completions (the house pattern — mirrors
        # map_tab's search box). An injected AutocompleteEntry pops its suggestion
        # list AS YOU TYPE; the plain ttk.Combobox only opens on the arrow.
        if hasattr(entry_cls, "update_completions"):
            try:
                self._region_combo = entry_cls(
                    add_row, completions=names,
                    on_select=self._on_region_selected, width=16)
            except TypeError:
                self._region_combo = entry_cls(add_row, width=16)
            self._region_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
            # AutocompleteEntry self-binds <Return> to select the highlighted
            # suggestion; add="+" so BOTH fire — the widget inserts the name into
            # the field first, then our handler commits it like the Add button.
            # Without add="+" we'd clobber the widget's own selection handler.
            self._region_combo.bind("<Return>", self._on_add_region, add="+")
        else:
            self._region_combo = entry_cls(add_row, values=names, width=16)
            self._region_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
            self._region_combo.bind("<KeyRelease>", self._on_region_type)

        btns = tk.Frame(panel, bg=BG_PANEL)
        btns.pack(fill=tk.X, padx=6, pady=(0, 6))
        ttk.Button(btns, text="Add", style="Dark.TButton",
                   command=self._on_add_region).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(btns, text="Remove", style="Dark.TButton",
                   command=self._on_remove_region).pack(side=tk.LEFT)

        self._region_rows = []             # listbox index -> region_id

    def _build_tree(self, parent):
        wrap = tk.Frame(parent, bg=BG_DARK)
        wrap.grid(row=0, column=1, sticky="nsew", padx=(2, 4), pady=4)
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)

        self._tree = ttk.Treeview(wrap, columns=COLUMNS, show="headings",
                                  selectmode="extended")
        for col in COLUMNS:
            self._tree.heading(col, text=HEADINGS[col],
                               command=lambda c=col: self._sort_by(c))
            self._tree.column(col, width=_COL_WIDTH[col], anchor="w",
                              stretch=(col == "name"))
        vsb = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        self._tree.tag_configure("dead", foreground=FG_RED)
        self._tree.tag_configure("manual", foreground=FG_ACCENT)
        self._tree.bind("<Double-Button-1>", lambda _e: self._on_edit_selected())
        self._tree.bind("<Button-3>", self._on_tree_right_click)

    # ── row 2: status bar ────────────────────────────────────────────────────
    def _build_statusbar(self):
        bar = tk.Frame(self, bg=BG_PANEL)
        bar.grid(row=2, column=0, sticky="ew")
        tk.Label(bar, textvariable=self._status_var, bg=BG_PANEL, fg=FG_DIM,
                 anchor="w").pack(fill=tk.X, padx=8, pady=3)

    def _set_status(self, text):
        self._status_var.set(text or "")

    # ── tree population / filter / sort ──────────────────────────────────────
    def _visible_entries(self):
        try:
            entries = self.store.entries() or []
        except Exception as exc:
            log.warning("infra store.entries() failed: %s", exc)
            entries = []
        query = (self._search_var.get() or "").strip().casefold()
        sysf = self._system_filter
        out = []
        for e in entries:
            if sysf is not None and e.get("system_id") != sysf:
                continue
            if query:
                parts = [str(e.get(k, "") or "") for k in (
                    "system_name", "name", "category", "source", "status")]
                parts.append(self._entry_type_name(e))   # match the specific type name too
                hay = " ".join(parts).casefold()
                if query not in hay:
                    continue
            out.append(e)
        if self._sort_col:
            out.sort(key=self._sort_key, reverse=self._sort_reverse)
        return out

    def _sort_key(self, entry):
        val = dict(zip(COLUMNS, self._row_values(entry)))[self._sort_col]
        return str(val).casefold()

    def _entry_type_name(self, entry):
        """Specific type label for the prominent "Type" column. Uses the injected
        type_name_fn(type_id, structure_id) when present; without it (the
        backwards-compatible default) falls back to the coarse category — what
        this column displayed before the UX round."""
        fn = self._type_name_fn
        if fn is None:
            return entry.get("category") or ""
        try:
            return fn(entry.get("type_id"), entry.get("structure_id")) or ""
        except Exception:
            return entry.get("category") or ""

    def _row_values(self, entry):
        # "category" slot → SPECIFIC type (header "Type"); "type" slot → coarse
        # category (header "Category"). See the COLUMNS/HEADINGS note above.
        return (
            entry.get("system_name") or "—",
            entry.get("name") or "",
            self._entry_type_name(entry),
            entry.get("category") or "",
            entry.get("source") or "",
            _fmt_last_seen(entry.get("last_seen")),
            entry.get("status") or "alive",
        )

    def _reload_tree(self):
        self._tree.delete(*self._tree.get_children())
        self._entries_by_key = {}
        for entry in self._visible_entries():
            key = entry_key(entry)
            self._entries_by_key[key] = entry
            if entry.get("status") == "dead":
                tags = ("dead",)
            elif entry.get("source") == "manual":
                tags = ("manual",)
            else:
                tags = ()
            self._tree.insert("", "end", iid=key, values=self._row_values(entry),
                              tags=tags)

    def _apply_filter(self):
        self._reload_tree()

    def _sort_by(self, col):
        if self._sort_col == col:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_col = col
            self._sort_reverse = False
        self._reload_tree()

    def _selected_keys(self):
        return list(self._tree.selection())

    # ── row actions ──────────────────────────────────────────────────────────
    def _on_delete(self):
        keys = self._selected_keys()
        if not keys:
            self._set_status("Select rows to delete.")
            return
        count = len(keys)
        if not messagebox.askyesno(
                "Delete structures",
                f"Delete {count} structure{'s' if count != 1 else ''} from "
                "the local database?"):
            return
        try:
            n = self.store.remove(keys)
        except Exception as exc:
            log.warning("infra store.remove failed: %s", exc)
            self._set_status("Delete failed.")
            return
        self._on_changed()
        self._reload_tree()
        count = n if isinstance(n, int) else len(keys)
        self._set_status(f"Deleted {count} entr{'y' if count == 1 else 'ies'}.")

    def _on_mark_dead(self):
        keys = self._selected_keys()
        if not keys:
            self._set_status("Select rows to mark dead.")
            return
        count = len(keys)
        if not messagebox.askyesno(
                "Mark dead",
                f"Mark {count} structure{'s' if count != 1 else ''} dead?"):
            return
        for k in keys:
            try:
                self.store.set_status(k, "dead")
            except Exception as exc:
                log.warning("infra store.set_status failed: %s", exc)
        self._on_changed()
        self._reload_tree()
        self._set_status(f"Marked {len(keys)} dead.")

    def _on_reverify(self):
        if self.scanner is None:
            return
        keys = self._selected_keys()
        regions = []
        for k in keys:
            entry = self._entries_by_key.get(k)
            rid = entry.get("region_id") if entry else None
            if rid is not None and rid not in regions:
                regions.append(rid)
        if not regions:
            self._set_status("Selected rows have no region to re-verify.")
            return
        started = self.scanner.scan_regions(regions)
        self._set_status("Re-verifying selected region(s)…" if started
                         else "Scanner busy or not authenticated.")

    def _on_scan_now(self):
        if self.scanner is None:
            return
        regions = list(self.store.get_regions())
        if not regions:
            self._set_status("No scan regions configured — add one on the left.")
            return
        started = self.scanner.scan_regions(regions)
        self._set_status("Scanning configured regions…" if started
                         else "Scanner busy or not authenticated.")

    # ── scanner progress sinks (host calls these, already _post_ui-marshalled) ─
    def set_scan_status(self, text):
        """Direct status-line setter for the host's scanner callbacks."""
        self._set_status(text)

    def push_scan_progress(self, payload):
        """Format an InfraScanner on_progress payload (plan §3.5) into the
        status line: {"phase","done","total","system","found","errors"}."""
        try:
            p = payload or {}
            self._set_status(
                "Scan {phase}: {done}/{total}  {system}  "
                "found {found}, errors {errors}".format(
                    phase=p.get("phase", ""), done=p.get("done", 0),
                    total=p.get("total", 0), system=p.get("system", ""),
                    found=p.get("found", 0), errors=p.get("errors", 0)))
        except Exception as exc:
            log.warning("infra push_scan_progress failed: %s", exc)

    def push_scan_done(self, report):
        """Format an InfraScanner on_done payload (MergeReport + cancelled/
        resolve_aborted flags) and ALWAYS append the role-gated caveat (§6)."""
        r = report or {}
        parts = ", ".join(f"{k} {r.get(k, 0)}" for k in
                          ("added", "updated", "upgraded", "unchanged") if k in r)
        flags = []
        if r.get("cancelled"):
            flags.append("cancelled")
        if r.get("resolve_aborted"):
            flags.append("resolve aborted (403 streak)")
        line = "Scan complete" + (f": {parts}" if parts else "")
        if flags:
            line += " (" + ", ".join(flags) + ")"
        line += "  " + SCAN_CAVEAT
        self._set_status(line)
        self._reload_tree()

    # ── clipboard import (two-phase preview) ─────────────────────────────────
    def _on_import_clipboard(self):
        try:
            text = self._clipboard_get() or ""
        except Exception as exc:
            log.warning("infra clipboard_get failed: %s", exc)
            text = ""
        if not text.strip():
            self._set_status("Clipboard is empty.")
            return
        try:
            summary = self._import_clipboard(text, preview=True)
        except Exception as exc:
            log.warning("infra import preview failed: %s", exc)
            self._set_status("Could not parse clipboard contents.")
            return
        self._open_import_preview(text, summary or {})

    def _open_import_preview(self, text, summary):
        self._close_preview()
        win = tk.Toplevel(self)
        win.title("Import from clipboard")
        win.configure(bg=BG_DARK)
        win.transient(self)
        self._preview_win = win
        self._preview_text = text

        links = summary.get("links", 0)
        npc = summary.get("npc", 0)
        unparsed = summary.get("unparsed", 0)
        by_cat = summary.get("by_category", {}) or {}
        seg = ", ".join(f"{by_cat[c]} {c}" for c in CATEGORIES if by_cat.get(c))
        msg = f"{links} links"
        if seg:
            msg += f": {seg}"
        msg += (f" — {npc} NPC (excluded by default), "
                f"{unparsed} unparsed — Import?")
        tk.Label(win, text=msg, bg=BG_DARK, fg=FG_TEXT, wraplength=460,
                 justify="left").pack(padx=12, pady=(12, 6), anchor="w")

        self._preview_include_npc = tk.BooleanVar(win, False)
        ttk.Checkbutton(win, text="Include NPC stations",
                        variable=self._preview_include_npc).pack(
            anchor="w", padx=12)

        btns = tk.Frame(win, bg=BG_DARK)
        btns.pack(fill=tk.X, padx=12, pady=10)
        ttk.Button(btns, text="Import", style="Dark.TButton",
                   command=self._confirm_import).pack(side=tk.RIGHT, padx=(4, 0))
        ttk.Button(btns, text="Cancel", style="Dark.TButton",
                   command=self._close_preview).pack(side=tk.RIGHT)

    def _confirm_import(self):
        text = getattr(self, "_preview_text", "")
        include = bool(self._preview_include_npc.get()) \
            if getattr(self, "_preview_include_npc", None) is not None else False
        try:
            result = self._import_clipboard(text, include_npc=include)
        except Exception as exc:
            log.warning("infra clipboard import failed: %s", exc)
            self._set_status("Import failed.")
            return
        report = (result or {}).get("report") or {}
        self._close_preview()
        self._on_changed()
        self._reload_tree()
        self._set_status(
            "Imported clipboard: +{a} added, {u} updated, {g} upgraded.".format(
                a=report.get("added", 0), u=report.get("updated", 0),
                g=report.get("upgraded", 0)))

    def _close_preview(self):
        win = getattr(self, "_preview_win", None)
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass
        self._preview_win = None

    # ── manual add ───────────────────────────────────────────────────────────
    def _open_add_manual(self):
        win = tk.Toplevel(self)
        win.title("Add structure (manual)")
        win.configure(bg=BG_DARK)
        win.transient(self)
        self._manual_win = win
        self._manual_system_var = tk.StringVar(win, "")
        self._manual_name_var = tk.StringVar(win, "")
        self._manual_cat_var = tk.StringVar(win, MANUAL_CATEGORIES[0])

        form = tk.Frame(win, bg=BG_DARK)
        form.pack(padx=12, pady=12)
        tk.Label(form, text="System", bg=BG_DARK, fg=FG_TEXT).grid(
            row=0, column=0, sticky="w", pady=3)
        ttk.Combobox(form, textvariable=self._manual_system_var,
                     values=self._system_names, width=26).grid(
            row=0, column=1, pady=3, padx=(6, 0))
        tk.Label(form, text="Name", bg=BG_DARK, fg=FG_TEXT).grid(
            row=1, column=0, sticky="w", pady=3)
        ttk.Entry(form, textvariable=self._manual_name_var, width=28).grid(
            row=1, column=1, pady=3, padx=(6, 0))
        tk.Label(form, text="Category", bg=BG_DARK, fg=FG_TEXT).grid(
            row=2, column=0, sticky="w", pady=3)
        ttk.Combobox(form, textvariable=self._manual_cat_var,
                     values=list(MANUAL_CATEGORIES), width=26,
                     state="readonly").grid(row=2, column=1, pady=3, padx=(6, 0))

        btns = tk.Frame(win, bg=BG_DARK)
        btns.pack(fill=tk.X, padx=12, pady=(0, 10))
        ttk.Button(btns, text="Add", style="Dark.TButton",
                   command=self._submit_manual).pack(side=tk.RIGHT, padx=(4, 0))
        ttk.Button(btns, text="Cancel", style="Dark.TButton",
                   command=win.destroy).pack(side=tk.RIGHT)

    def _submit_manual(self):
        name = (self._manual_name_var.get() or "").strip()
        system = (self._manual_system_var.get() or "").strip()
        category = (self._manual_cat_var.get() or "unknown").strip()
        if not name:
            self._set_status("Name is required for a manual entry.")
            return
        entry = {"name": name, "type_id": None, "structure_id": None,
                 "category": category, "system_name": system,
                 "gate_to_system_name": None}
        try:
            self._import_manual(entry)
        except Exception as exc:
            log.warning("infra manual add failed: %s", exc)
            self._set_status("Manual add failed.")
            return
        win = getattr(self, "_manual_win", None)
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass
        self._manual_win = None
        self._on_changed()
        self._reload_tree()
        self._set_status(f"Added manual entry: {name}")

    # ── edit (name only for manual rows; notes for any row) ──────────────────
    def _on_edit_selected(self):
        keys = self._selected_keys()
        if not keys:
            return
        entry = self._entries_by_key.get(keys[0])
        if entry is not None:
            self._open_edit(entry)

    def _open_edit(self, entry):
        win = tk.Toplevel(self)
        win.title("Edit structure")
        win.configure(bg=BG_DARK)
        win.transient(self)
        self._edit_win = win
        self._edit_entry = entry
        is_manual = (entry.get("source") == "manual")

        form = tk.Frame(win, bg=BG_DARK)
        form.pack(padx=12, pady=12)
        tk.Label(form, text="Name", bg=BG_DARK, fg=FG_TEXT).grid(
            row=0, column=0, sticky="w", pady=3)
        self._edit_name_var = tk.StringVar(win, entry.get("name", "") or "")
        self._edit_name_entry = ttk.Entry(form, textvariable=self._edit_name_var,
                                          width=32)
        self._edit_name_entry.grid(row=0, column=1, pady=3, padx=(6, 0))
        if not is_manual:
            # Imported rows: name is authoritative from the source — notes only.
            self._edit_name_entry.config(state="disabled")

        tk.Label(form, text="Notes", bg=BG_DARK, fg=FG_TEXT).grid(
            row=1, column=0, sticky="w", pady=3)
        self._edit_notes_var = tk.StringVar(win, entry.get("notes", "") or "")
        ttk.Entry(form, textvariable=self._edit_notes_var, width=32).grid(
            row=1, column=1, pady=3, padx=(6, 0))
        if not is_manual:
            tk.Label(form, text="(imported row — notes only)",
                     bg=BG_DARK, fg=FG_DIM).grid(
                row=2, column=1, sticky="w", padx=(6, 0))

        btns = tk.Frame(win, bg=BG_DARK)
        btns.pack(fill=tk.X, padx=12, pady=(0, 10))
        ttk.Button(btns, text="Save", style="Dark.TButton",
                   command=self._save_edit).pack(side=tk.RIGHT, padx=(4, 0))
        ttk.Button(btns, text="Cancel", style="Dark.TButton",
                   command=win.destroy).pack(side=tk.RIGHT)

    def _save_edit(self):
        entry = getattr(self, "_edit_entry", None)
        if entry is None:
            return
        old_key = entry_key(entry)
        notes = (self._edit_notes_var.get() or "").strip()
        is_manual = (entry.get("source") == "manual")
        new_name = (self._edit_name_var.get() or "").strip()
        try:
            if is_manual and new_name and new_name != (entry.get("name") or ""):
                # Rename a manual row: no store rename primitive exists, so drop
                # the old (name-keyed) record and re-add via import_manual with
                # the new name + notes.
                self.store.remove([old_key])
                self._import_manual({
                    "name": new_name, "type_id": None, "structure_id": None,
                    "category": entry.get("category", "unknown"),
                    "system_name": entry.get("system_name", "") or "",
                    "gate_to_system_name": entry.get("gate_to_system_name"),
                    "notes": notes})
            else:
                self.store.set_notes(old_key, notes)
        except Exception as exc:
            log.warning("infra edit save failed: %s", exc)
            self._set_status("Save failed.")
            return
        win = getattr(self, "_edit_win", None)
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass
        self._edit_win = None
        self._on_changed()
        self._reload_tree()
        self._set_status("Saved.")

    # ── right-click menu (mirrors the row toolbar) ───────────────────────────
    def _on_tree_right_click(self, evt):
        row = self._tree.identify_row(evt.y)
        if row and row not in self._tree.selection():
            self._tree.selection_set(row)
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Edit…", command=self._on_edit_selected)
        menu.add_command(label="Re-verify selected", command=self._on_reverify)
        menu.add_command(label="Mark dead", command=self._on_mark_dead)
        menu.add_command(label="Delete", command=self._on_delete)
        try:
            menu.tk_popup(evt.x_root, evt.y_root)
        finally:
            menu.grab_release()

    # ── region config ────────────────────────────────────────────────────────
    def _on_region_type(self, _evt=None):
        typed = (self._region_combo.get() or "").casefold()
        matches = [n for _r, n in self._regions_catalog if typed in n.casefold()]
        self._region_combo.config(
            values=matches or [n for _r, n in self._regions_catalog])

    def _on_region_selected(self):
        """AutocompleteEntry on_select hook. The widget has already inserted the
        picked region name into the entry (feeding the Add button), exactly like
        choosing a value in the old combobox; reflect it in the status line. The
        user still commits with Add / Enter — a selection alone does not add."""
        name = (self._region_combo.get() or "").strip()
        if name:
            self._set_status(f"Region '{name}' selected — press Enter or click Add.")

    def _on_add_region(self, _evt=None):
        name = (self._region_combo.get() or "").strip()
        rid = next((r for r, n in self._regions_catalog if n == name), None)
        if rid is None:
            self._set_status(f"Unknown region: {name or '(blank)'}")
            return
        current = list(self.store.get_regions())
        if rid not in current:
            current.append(rid)
            self.store.set_regions(current)
        self._refresh_regions()
        self._set_status(f"Added scan region: {name}")

    def _on_remove_region(self):
        sel = self._region_list.curselection()
        if not sel:
            self._set_status("Select a region to remove.")
            return
        rid = self._region_rows[sel[0]]
        self.store.set_regions([r for r in self.store.get_regions() if r != rid])
        self._refresh_regions()
        self._set_status(
            f"Removed scan region: {self._region_name.get(rid, rid)}")

    def _refresh_regions(self):
        self._region_list.delete(0, tk.END)
        self._region_rows = []
        try:
            reg_state = (self.store.scan_state() or {}).get("regions", {}) or {}
        except Exception as exc:
            log.warning("infra scan_state failed: %s", exc)
            reg_state = {}
        for rid in self.store.get_regions():
            name = self._region_name.get(int(rid), f"Region {rid}")
            iso = reg_state.get(rid) or reg_state.get(str(rid))
            when = _fmt_last_seen(iso) if iso else "never"
            self._region_list.insert(tk.END, f"{name}   last scan: {when}")
            self._region_rows.append(int(rid))
