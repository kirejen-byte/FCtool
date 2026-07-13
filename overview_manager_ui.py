"""Overview tab content — repository list + per-account distribution + folder notice.

Wave D1 of the Overview Manager (spec 2026-07-12-overview-manager-design.md §4.4,
plan 2026-07-13-overview-manager-ui-p2.md). This module is widgets + glue only:
all pack modelling / persistence / YAML lives in overview_schema / overview_yaml /
overview_store. It never imports fc_gui (house containment pattern, mirrors
fleet_template_window.py) and never reaches into the parallel-built editor or
.dat modules — the Edit action and live-.dat reads arrive as injected provider
callables.

Construction: ``build_overview_tab(parent, providers) -> tk.Frame`` where
``providers`` is an ``OverviewProviders`` bundle of callables/objects. All state
lives on the returned frame object; no globals, no shared mutable app state.

Threading discipline (docs/agents/CODEBASE_MAP.md): background workers (folder
scan, drift compare) NEVER touch Tk directly — every UI mutation is marshalled
through ``providers.post_ui`` (the app's main-thread dispatcher). The worker
bodies are exposed as plain methods so tests can call them synchronously with an
inline ``post_ui``.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog

import overview_schema
import overview_yaml
from app_log import get_logger

log = get_logger(__name__)

# ── Palette (mirrors fc_gui.py:150-164 by VALUE — importing fc_gui is circular
# and forbidden by containment; keep these in sync if the app palette changes). ─
BG_DARK = "#1a1a2e"
BG_PANEL = "#16213e"
BG_ENTRY = "#0f3460"
FG_TEXT = "#e0e0e0"
FG_DIM = "#888899"
FG_ACCENT = "#00d4ff"
FG_GREEN = "#00ff88"
FG_RED = "#ff4444"
FG_ORANGE = "#ff8c00"
FG_YELLOW = "#ffdd00"
FG_WHITE = "#ffffff"
BORDER_COLOR = "#2a2a4a"

_FONT = ("Consolas", 9)
_FONT_SM = ("Consolas", 8)
_FONT_BOLD = ("Consolas", 9, "bold")

# The client's import dialog lists Documents\EVE\Overview only; FCTool stages a
# pack as this-prefixed file so its own staged files are trivially distinguished
# from foreign packs during the folder-notice scan.
STAGED_PREFIX = "FCTool - "

# Auto folder-scan is deferred this many ms after build so it (a) never runs on
# the construction critical path and (b) is cancelled by the test harness's
# after-draining teardown before it can spawn a worker (keeps tests off-thread).
_AUTO_SCAN_DELAY_MS = 250


@dataclass
class OverviewProviders:
    """Everything the Overview tab needs from the host app, injected so this
    module stays free of fc_gui and of the parallel-built editor/.dat modules.

    store:            overview_store.OverviewStore (repository; auto-persists).
    get_config:       () -> dict          live app config (mutated in place).
    save_config:      () -> None          persist the app config to disk.
    overview_dir:     () -> str           Documents\\EVE\\Overview (fallback dir).
    list_accounts:    () -> list[tuple[int, str, float]]
                                          (account_id, core_user_path, mtime).
    read_live:        (path) -> tuple     (OverviewPack, LiveNotes); MAY RAISE.
    live_fingerprint: (path) -> str       fingerprint of the live overview; MAY RAISE.
    post_ui:          (fn, *args) -> None marshal fn onto the Tk main thread.
    open_editor:      (record) -> None    open the pack editor for a PackRecord.
    build_fc_standard:() -> OverviewPack | None   starter template (None = n/a).
    """
    store: Any
    get_config: Callable[[], dict]
    save_config: Callable[[], None]
    overview_dir: Callable[[], str]
    list_accounts: Callable[[], list]
    read_live: Callable[[str], tuple]
    live_fingerprint: Callable[[str], str]
    post_ui: Callable[..., None]
    open_editor: Callable[[Any], None]
    build_fc_standard: Callable[[], Any]


# ── module-level helpers ───────────────────────────────────────────────────────
def open_folder(path: str) -> None:
    """Open ``path`` in the OS file browser (Windows ``os.startfile``).

    Guarded for a missing path/platform: does NOT create the folder (creation is
    a staging concern — the caller creates it only when writing a staged file).
    Silent no-op when the path is blank, absent, or startfile is unavailable
    (non-Windows, or a headless test)."""
    if not path or not os.path.isdir(path):
        return
    try:
        os.startfile(path)  # type: ignore[attr-defined]  # Windows only
    except (AttributeError, OSError):
        pass


def _safe_component(name: str) -> str:
    """Strip characters illegal in a Windows filename from a pack name so it can
    form ``FCTool - <name>.yaml``. Clean names pass through unchanged."""
    out = name or ""
    for ch in '\\/:*?"<>|\n\r\t':
        out = out.replace(ch, "_")
    return out.strip() or "overview"


def _staged_filename(name: str) -> str:
    return f"{STAGED_PREFIX}{_safe_component(name)}.yaml"


def _sections_summary(pack) -> str:
    """Compact 'which wire sections are populated' line for the pack list."""
    parts = []
    if pack.presets:
        parts.append("presets")
    if pack.tabs:
        parts.append("tabs")
    if any((pack.flag_order, pack.flag_states, pack.background_order,
            pack.background_states, pack.state_colors, pack.state_blinks)):
        parts.append("appearance")
    if any((pack.column_order, pack.overview_columns)):
        parts.append("columns")
    if any((pack.ship_labels, pack.ship_label_order)):
        parts.append("labels")
    if pack.user_settings:
        parts.append("misc")
    return ", ".join(parts) if parts else "(empty)"


def _fmt_ts(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError, TypeError, OverflowError):
        return ""


def _fmt_mtime(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%m-%d %H:%M")
    except (ValueError, OSError, TypeError, OverflowError):
        return "?"


def _fmt_iso(s) -> str:
    try:
        return datetime.fromisoformat(str(s)).strftime("%m-%d %H:%M")
    except (ValueError, TypeError):
        return str(s)


def build_overview_tab(parent, providers: OverviewProviders) -> tk.Frame:
    """Construct the Overview tab and return its frame (the frame IS the
    controller — an OverviewTab, a tk.Frame subclass carrying all state)."""
    return OverviewTab(parent, providers)


class OverviewTab(tk.Frame):
    def __init__(self, parent, providers: OverviewProviders):
        super().__init__(parent, bg=BG_DARK)
        self.providers = providers
        self._selected_pack_id: str | None = None
        # account_id -> {row, nick(StringVar), staged, mark, last, drift}
        self._account_rows: dict[int, dict] = {}
        self._drift_state: dict[int, str] = {}       # account_id -> last drift state
        self._folder_notices: list[dict] = []        # last folder-scan results
        self._selection_buttons: list = []           # buttons gated on a selection
        # Popup menus: at most ONE live tk.Menu per button (_fresh_menu destroys
        # the previous instance before building the next — no per-click leak).
        self._new_menu_widget: tk.Menu | None = None
        self._acct_menu_widget: tk.Menu | None = None

        # First-run seeding BEFORE the list is populated so the seeded pack shows.
        self._seed_fc_standard_if_needed()

        self._build_top()
        self._build_actions()      # packed to BOTTOM before body claims the middle
        self._build_body()
        self._refresh_pack_list()

        # Kick a background folder scan shortly after build (never on the
        # construction path; cancelled by the test harness before it can fire).
        try:
            self.after(_AUTO_SCAN_DELAY_MS, self._scan_folder)
        except tk.TclError:
            pass

    # ── config access ─────────────────────────────────────────────────────────
    def _ov_config(self) -> dict:
        """The mutable ``overview`` config sub-dict with all keys defaulted.

        NB: do NOT write ``get_config() or {}`` — a first-run config is an empty
        (falsy) dict, and ``or {}`` would return a throwaway, so the seeding flag
        would never persist and FC Standard would re-seed on every launch."""
        cfg = self.providers.get_config()
        if cfg is None:
            cfg = {}
        ov = cfg.setdefault("overview", {})
        ov.setdefault("account_nicknames", {})
        ov.setdefault("distribution_log", {})
        ov.setdefault("fc_standard_seeded", False)
        ov.setdefault("overview_dir_override", "")
        return ov

    def _overview_dir(self) -> str:
        ov = self._ov_config()
        override = (ov.get("overview_dir_override") or "").strip()
        return override or self.providers.overview_dir()

    def _account_label(self, account_id) -> str:
        nick = self._ov_config()["account_nicknames"].get(str(account_id), "")
        return nick.strip() if isinstance(nick, str) else ""

    # ── first-run seeding ─────────────────────────────────────────────────────
    def _seed_fc_standard_if_needed(self) -> None:
        """Seed the 'FC Standard' starter pack once, iff the repository is empty
        and we have never seeded. Idempotent: the flag makes it a strict
        once-ever action even across rebuilds (mirrors _seed_default_fleet_template)."""
        ov = self._ov_config()
        if ov.get("fc_standard_seeded"):
            return
        try:
            if self.providers.store.list_packs():
                return
        except Exception:
            log.exception("[overview] seeding skipped: store.list_packs failed")
            return
        try:
            pack = self.providers.build_fc_standard()
        except Exception:
            log.exception("[overview] seeding skipped: build_fc_standard failed")
            pack = None
        if pack is None:
            return  # template unavailable — leave the flag unset, retry next run
        try:
            self.providers.store.add_pack(
                "FC Standard", pack, source="editor",
                notes="FCTool starter template")
        except Exception:
            log.exception("[overview] seeding failed: could not add 'FC Standard'")
            return
        ov["fc_standard_seeded"] = True
        self.providers.save_config()

    # ── construction: top folder-notice strip ─────────────────────────────────
    def _build_top(self) -> None:
        top = tk.Frame(self, bg=BG_DARK)
        top.pack(side=tk.TOP, fill=tk.X, padx=6, pady=(6, 2))
        header = tk.Frame(top, bg=BG_DARK)
        header.pack(fill=tk.X)
        ttk.Button(header, text="Scan folder", style="Dark.TButton",
                   command=self._scan_folder).pack(side=tk.LEFT)
        tk.Label(header, text="  New overview files in your Overview folder:",
                 bg=BG_DARK, fg=FG_DIM, font=_FONT).pack(side=tk.LEFT)
        self._notice_rows = tk.Frame(top, bg=BG_DARK)
        self._notice_rows.pack(fill=tk.X, pady=(2, 0))

    # ── construction: bottom actions bar ──────────────────────────────────────
    def _build_actions(self) -> None:
        bar = tk.Frame(self, bg=BG_PANEL)
        bar.pack(side=tk.BOTTOM, fill=tk.X)
        stage = ttk.Button(bar, text="Stage to EVE", style="Dark.TButton",
                           command=self._stage_selected)
        stage.pack(side=tk.LEFT, padx=6, pady=6)
        self._selection_buttons.append(stage)
        ttk.Button(bar, text="Open Overview folder", style="Dark.TButton",
                   command=self._open_overview_folder).pack(side=tk.LEFT, padx=2)
        self._import_acct_btn = ttk.Button(
            bar, text="Import current from account ▾", style="Dark.TButton",
            command=self._account_import_menu)
        self._import_acct_btn.pack(side=tk.LEFT, padx=2)
        ttk.Button(bar, text="Instructions", style="Dark.TButton",
                   command=self._show_instructions).pack(side=tk.RIGHT, padx=6)

    # ── construction: body (left list, right distribution) ─────────────────────
    def _build_body(self) -> None:
        body = tk.Frame(self, bg=BG_DARK)
        body.pack(fill=tk.BOTH, expand=True, padx=6, pady=2)
        self._build_left(body)
        self._build_right(body)

    def _build_left(self, parent) -> None:
        left = tk.Frame(parent, bg=BG_PANEL, bd=1, relief=tk.RIDGE)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 4))

        btns = tk.Frame(left, bg=BG_PANEL)
        btns.pack(fill=tk.X, padx=4, pady=4)
        self._new_btn = ttk.Button(btns, text="New ▾", style="Dark.TButton",
                                   command=self._new_menu)
        self._new_btn.pack(side=tk.LEFT, padx=2)
        ttk.Button(btns, text="Import…", style="Dark.TButton",
                   command=self._import_file).pack(side=tk.LEFT, padx=2)
        dup = ttk.Button(btns, text="Duplicate", style="Dark.TButton",
                         command=self._duplicate_selected)
        dup.pack(side=tk.LEFT, padx=2)
        edit = ttk.Button(btns, text="Edit", style="Dark.TButton",
                          command=self._edit_selected)
        edit.pack(side=tk.LEFT, padx=2)
        delete = ttk.Button(btns, text="Delete", style="Red.TButton",
                            command=self._delete_selected)
        delete.pack(side=tk.LEFT, padx=2)
        self._selection_buttons += [dup, edit, delete]

        wrap = tk.Frame(left, bg=BG_PANEL)
        wrap.pack(fill=tk.BOTH, expand=True, padx=4, pady=(0, 4))
        cols = ("name", "sections", "fp", "modified")
        self._tree = ttk.Treeview(wrap, columns=cols, show="headings",
                                  selectmode="browse")
        for cid, text, width in (("name", "Pack", 160), ("sections", "Sections", 150),
                                 ("fp", "Fingerprint", 90), ("modified", "Modified", 120)):
            self._tree.heading(cid, text=text)
            self._tree.column(cid, width=width, stretch=(cid in ("name", "sections")))
        vsb = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._tree.bind("<<TreeviewSelect>>", self._on_pack_select)
        self._tree.bind("<Double-Button-1>", lambda e: self._edit_selected())

    def _build_right(self, parent) -> None:
        right = tk.Frame(parent, bg=BG_PANEL, bd=1, relief=tk.RIDGE)
        right.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)
        header = tk.Frame(right, bg=BG_PANEL)
        header.pack(fill=tk.X, padx=4, pady=4)
        tk.Label(header, text="Distribution", font=_FONT_BOLD, fg=FG_ACCENT,
                 bg=BG_PANEL).pack(side=tk.LEFT)
        refresh = ttk.Button(header, text="Refresh drift", style="Dark.TButton",
                             command=self._refresh_drift)
        refresh.pack(side=tk.RIGHT)
        self._selection_buttons.append(refresh)

        holder = tk.Frame(right, bg=BG_DARK)
        holder.pack(fill=tk.BOTH, expand=True, padx=4, pady=(0, 4))
        inner = self._make_scrollable(holder)
        self._build_account_rows(inner)

    def _make_scrollable(self, parent) -> tk.Frame:
        canvas = tk.Canvas(parent, bg=BG_DARK, highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        inner = tk.Frame(canvas, bg=BG_DARK)
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _resize(_e=None):
            try:
                canvas.configure(scrollregion=canvas.bbox("all"))
                canvas.itemconfigure(win_id, width=canvas.winfo_width())
            except tk.TclError:
                pass
        inner.bind("<Configure>", _resize)
        canvas.bind("<Configure>", _resize)
        return inner

    def _build_account_rows(self, inner) -> None:
        try:
            accounts = list(self.providers.list_accounts() or [])
        except Exception:
            # Render the graceful empty state below, but never silently: the
            # user sees "no accounts" while the log carries the real cause.
            log.exception("[overview] list_accounts provider failed "
                          "(distribution panel shows no accounts)")
            accounts = []
        if not accounts:
            tk.Label(inner, text="No EVE accounts found.", bg=BG_DARK,
                     fg=FG_DIM, font=_FONT).pack(anchor="w", padx=4, pady=6)
            return
        for account_id, _path, mtime in accounts:
            self._build_account_row(inner, account_id, mtime)

    def _build_account_row(self, parent, account_id, mtime) -> None:
        row = tk.Frame(parent, bg=BG_PANEL)
        row.pack(fill=tk.X, padx=2, pady=2)
        nick_var = tk.StringVar(value=self._account_label(account_id))
        nick = tk.Entry(row, textvariable=nick_var, bg=BG_ENTRY, fg=FG_TEXT,
                        insertbackground=FG_TEXT, font=_FONT, width=16,
                        relief=tk.SOLID, borderwidth=1)
        nick.grid(row=0, column=0, rowspan=2, padx=(4, 8), pady=4, sticky="w")
        nick.bind("<FocusOut>",
                  lambda e, aid=account_id, v=nick_var: self._save_nickname(aid, v.get()))
        nick.bind("<Return>",
                  lambda e, aid=account_id, v=nick_var: self._save_nickname(aid, v.get()))

        meta = tk.Label(row, text=f"acct {account_id} · settings updated {_fmt_mtime(mtime)}",
                        bg=BG_PANEL, fg=FG_DIM, font=_FONT_SM, anchor="w")
        meta.grid(row=0, column=1, sticky="w")
        staged = tk.Label(row, text="staged: —", bg=BG_PANEL, fg=FG_DIM,
                          font=_FONT_SM, anchor="w")
        staged.grid(row=0, column=2, padx=8, sticky="w")
        drift = tk.Label(row, text="drift: —", bg=BG_PANEL, fg=FG_DIM,
                         font=_FONT_SM, anchor="w")
        drift.grid(row=0, column=3, padx=8, sticky="w")

        mark = ttk.Button(row, text="Mark imported ✓", style="Dark.TButton",
                          command=lambda aid=account_id: self._mark_imported(aid))
        mark.grid(row=1, column=1, sticky="w", pady=(0, 2))
        last = tk.Label(row, text="not imported", bg=BG_PANEL, fg=FG_DIM,
                        font=_FONT_SM, anchor="w")
        last.grid(row=1, column=2, columnspan=2, padx=8, sticky="w")

        self._account_rows[account_id] = {
            "row": row, "nick": nick_var, "staged": staged,
            "mark": mark, "last": last, "drift": drift,
        }

    # ── pack list ─────────────────────────────────────────────────────────────
    def refresh(self) -> None:
        """Public repaint hook. fc_gui wires the editor's on_save to call this so
        an edited pack's name/fingerprint/modified/staged state repaint here."""
        self._refresh_pack_list()

    def _refresh_pack_list(self) -> None:
        prev = self._selected_pack_id
        self._tree.delete(*self._tree.get_children())
        for rec in self.providers.store.list_packs():
            self._tree.insert(
                "", "end", iid=rec.pack_id,
                values=(rec.name, _sections_summary(rec.pack),
                        (rec.fingerprint or "")[:8], _fmt_ts(rec.modified)))
        if prev and self._tree.exists(prev):
            self._tree.selection_set(prev)
            self._selected_pack_id = prev
        else:
            self._selected_pack_id = None
        self._update_buttons()
        self._refresh_distribution_for_selection()

    def _select_pack(self, pack_id) -> None:
        if pack_id and self._tree.exists(pack_id):
            self._tree.selection_set(pack_id)
            self._tree.see(pack_id)
            self._selected_pack_id = pack_id
            self._update_buttons()
            self._refresh_distribution_for_selection()

    def _on_pack_select(self, _evt=None) -> None:
        sel = self._tree.selection()
        self._selected_pack_id = sel[0] if sel else None
        self._update_buttons()
        self._refresh_distribution_for_selection()

    def _selected_record(self):
        if not self._selected_pack_id:
            return None
        return self.providers.store.get_pack(self._selected_pack_id)

    def _update_buttons(self) -> None:
        has = self._selected_record() is not None
        state = "normal" if has else "disabled"
        for b in self._selection_buttons:
            try:
                b.config(state=state)
            except tk.TclError:
                pass
        for widgets in self._account_rows.values():
            try:
                widgets["mark"].config(state=state)
            except tk.TclError:
                pass

    # ── dialogs (wrapped so tests can override per-instance) ───────────────────
    def _info(self, title, msg) -> None:
        messagebox.showinfo(title, msg, parent=self)

    def _error(self, title, msg) -> None:
        messagebox.showerror(title, msg, parent=self)

    def _confirm(self, title, msg) -> bool:
        return bool(messagebox.askyesno(title, msg, parent=self))

    # ── popup menus ────────────────────────────────────────────────────────────
    def _fresh_menu(self, attr: str) -> tk.Menu:
        """Destroy the previous popup stored on ``attr`` and return a new menu
        remembered there. Content is rebuilt per click (account nicknames
        change), but menu widgets never accumulate beyond one per button."""
        old = getattr(self, attr, None)
        if old is not None:
            try:
                old.destroy()
            except tk.TclError:
                pass
        m = tk.Menu(self, tearoff=0, bg=BG_PANEL, fg=FG_TEXT)
        setattr(self, attr, m)
        return m

    # ── New (menu) ─────────────────────────────────────────────────────────────
    def _new_menu(self) -> None:
        m = self._fresh_menu("_new_menu_widget")
        m.add_command(label="Empty pack", command=self._new_empty)
        m.add_command(label="FC Standard template", command=self._new_fc_standard)
        try:
            b = self._new_btn
            m.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())
        finally:
            m.grab_release()

    def _new_empty(self) -> None:
        name = simpledialog.askstring("New overview", "Pack name:", parent=self)
        if not name:
            return
        try:
            rec = self.providers.store.add_pack(
                name, overview_schema.OverviewPack(), source="editor")
        except Exception as e:
            self._error("New overview", f"Could not create the pack:\n{e}")
            return
        self._refresh_pack_list()
        self._select_pack(rec.pack_id)

    def _new_fc_standard(self) -> None:
        try:
            pack = self.providers.build_fc_standard()
        except Exception as e:
            self._error("FC Standard", f"Could not build the template:\n{e}")
            return
        if pack is None:
            self._info("FC Standard", "The FC Standard template is unavailable.")
            return
        try:
            rec = self.providers.store.add_pack(
                "FC Standard", pack, source="editor",
                notes="FCTool starter template")
        except Exception as e:
            log.exception("[overview] add_pack('FC Standard') failed")
            self._error("FC Standard", f"Could not save the pack:\n{e}")
            return
        self._refresh_pack_list()
        self._select_pack(rec.pack_id)

    # ── Import from file ───────────────────────────────────────────────────────
    def _import_file(self) -> None:
        path = filedialog.askopenfilename(
            parent=self, title="Import overview YAML",
            initialdir=self._overview_dir(),
            filetypes=[("Overview YAML", "*.yaml"), ("All files", "*.*")])
        if path:
            self._import_path(path)

    def _import_path(self, path):
        """Load a YAML file into a new pack, dedup-by-fingerprint. Returns the
        new PackRecord, or None (parse error or duplicate). Testable entry point
        (bypasses the file dialog)."""
        try:
            pack = overview_yaml.load_file(path)
        except Exception as e:
            self._error("Import failed", f"{os.path.basename(path)}:\n{e}")
            return None
        fp = overview_schema.fingerprint(pack)
        dup = next((r for r in self.providers.store.list_packs()
                    if r.fingerprint == fp), None)
        if dup is not None:
            self._info("Already imported",
                       f"This overview is identical to '{dup.name}' — "
                       f"not imported again.")
            return None
        name = os.path.splitext(os.path.basename(path))[0]
        try:
            rec = self.providers.store.add_pack(
                name, pack, source=f"imported:{os.path.basename(path)}")
        except Exception as e:
            log.exception("[overview] add_pack from %s failed", path)
            self._error("Import failed",
                        f"{os.path.basename(path)}:\nCould not save the pack:\n{e}")
            return None
        self._refresh_pack_list()
        self._select_pack(rec.pack_id)
        return rec

    # ── Duplicate / Delete / Edit ──────────────────────────────────────────────
    def _duplicate_selected(self) -> None:
        rec = self._selected_record()
        if rec is None:
            return
        copy = self.providers.store.duplicate_pack(rec.pack_id)
        self._refresh_pack_list()
        if copy is not None:
            self._select_pack(copy.pack_id)

    def _delete_selected(self) -> None:
        rec = self._selected_record()
        if rec is None:
            return
        if not self._confirm("Delete pack", f"Delete '{rec.name}'?"):
            return
        self.providers.store.delete_pack(rec.pack_id)
        self._selected_pack_id = None
        self._refresh_pack_list()

    def _edit_selected(self) -> None:
        rec = self._selected_record()
        if rec is None:
            return
        try:
            self.providers.open_editor(rec)
        except Exception as e:
            self._error("Editor", f"Could not open the editor:\n{e}")

    # ── Stage to EVE ───────────────────────────────────────────────────────────
    def _stage_pack(self, rec) -> str:
        """Write ``FCTool - <name>.yaml`` into the Overview dir (created if
        missing) and return the path. Raises on I/O failure — caller handles."""
        d = self._overview_dir()
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, _staged_filename(rec.name))
        overview_yaml.write_file(rec.pack, path)
        return path

    def _stage_selected(self) -> None:
        rec = self._selected_record()
        if rec is None:
            return
        try:
            path = self._stage_pack(rec)
        except Exception as e:
            self._error("Stage to EVE", f"Could not stage '{rec.name}':\n{e}")
            return
        self._info("Staged",
                   f"Wrote {os.path.basename(path)} into your Overview folder.\n"
                   f"Import it in-game — see Instructions.")
        self._refresh_distribution_for_selection()

    def _open_overview_folder(self) -> None:
        d = self._overview_dir()
        if not os.path.isdir(d):
            self._info("Overview folder",
                       f"The Overview folder doesn't exist yet:\n{d}\n\n"
                       f"Stage a pack to create it.")
            return
        open_folder(d)

    # ── Import current from account ────────────────────────────────────────────
    def _account_import_menu(self) -> None:
        try:
            accounts = list(self.providers.list_accounts() or [])
        except Exception:
            log.exception("[overview] list_accounts provider failed "
                          "(account-import menu shows no accounts)")
            accounts = []
        m = self._fresh_menu("_acct_menu_widget")
        if not accounts:
            m.add_command(label="(no accounts found)", state="disabled")
        for account_id, path, _mt in accounts:
            label = self._account_label(account_id) or f"Account {account_id}"
            m.add_command(
                label=label,
                command=lambda aid=account_id, p=path: self._import_from_account(aid, p))
        try:
            b = self._import_acct_btn
            m.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())
        finally:
            m.grab_release()

    def _import_from_account(self, account_id, path):
        """Read the live overview from a core_user file into a new pack.
        Per-account failure surfaces as a dialog and never touches other rows."""
        try:
            result = self.providers.read_live(path)
            pack = result[0] if isinstance(result, (tuple, list)) else result
        except Exception as e:
            self._error("Import from account",
                        f"Account {account_id} is unreadable:\n{e}")
            return None
        base = self._account_label(account_id) or f"acct {account_id}"
        try:
            rec = self.providers.store.add_pack(
                f"{base} overview", pack, source=f"dat:{account_id}")
        except Exception as e:
            log.exception("[overview] add_pack from account %s failed", account_id)
            self._error("Import from account",
                        f"Account {account_id}: could not save the pack:\n{e}")
            return None
        self._refresh_pack_list()
        self._select_pack(rec.pack_id)
        return rec

    # ── Instructions ──────────────────────────────────────────────────────────
    def _show_instructions(self) -> None:
        rec = self._selected_record()
        fname = _staged_filename(rec.name) if rec is not None else \
            f"{STAGED_PREFIX}<pack>.yaml"
        win = tk.Toplevel(self)
        win.title("Import an overview in-game")
        win.configure(bg=BG_DARK)
        text = (
            "How to import a staged overview pack in EVE\n"
            "──────────────────────────────────────────\n\n"
            "1. Select a pack and click 'Stage to EVE'. It writes\n"
            f"       {fname}\n"
            "   into Documents\\EVE\\Overview (the only folder the game's\n"
            "   import dialog can see).\n\n"
            "2. In-game: click the three-dot (⋯) menu on the Overview window\n"
            "   → Open Overview Settings → the Misc tab → Import Overview.\n\n"
            "3. Choose the staged file from the list.\n\n"
            "Important notes\n"
            "───────────────\n"
            "• Importing MERGES into the current overview — it does not replace\n"
            "  it. For a clean full install, use 'Reset All Overview Settings'\n"
            "  first, then import.\n"
            "• Overview settings are per-account: repeat the import once per\n"
            "  account (every character on an account then shares it).\n"
            "• CCP-default presets a pack references may not travel with the\n"
            "  file; they still resolve in-game if present."
        )
        tk.Label(win, text=text, bg=BG_DARK, fg=FG_TEXT, justify=tk.LEFT,
                 font=_FONT, anchor="w").pack(fill=tk.BOTH, expand=True,
                                              padx=12, pady=12)
        ttk.Button(win, text="Close", style="Dark.TButton",
                   command=win.destroy).pack(pady=(0, 10))

    # ── distribution: nickname / mark-imported / staged status ─────────────────
    def _save_nickname(self, account_id, value) -> None:
        new = (value or "").strip()
        nicks = self._ov_config()["account_nicknames"]
        if nicks.get(str(account_id), "") == new:
            return  # unchanged (e.g. plain FocusOut) — skip the config write
        nicks[str(account_id)] = new
        self.providers.save_config()

    def _mark_imported(self, account_id) -> None:
        pid = self._selected_pack_id
        if not pid:
            self._info("Mark imported", "Select a pack first.")
            return
        dist_log = self._ov_config()["distribution_log"].setdefault(pid, {})
        dist_log[str(account_id)] = datetime.now().isoformat(timespec="seconds")
        self.providers.save_config()
        self._refresh_distribution_for_selection()

    def _refresh_distribution_for_selection(self) -> None:
        rec = self._selected_record()
        staged_exists = False
        if rec is not None:
            try:
                staged_exists = os.path.isfile(
                    os.path.join(self._overview_dir(), _staged_filename(rec.name)))
            except OSError:
                staged_exists = False
        dist_log = (self._ov_config()["distribution_log"].get(rec.pack_id, {})
                    if rec else {})
        for aid, widgets in self._account_rows.items():
            try:
                widgets["staged"].config(
                    text=("staged: yes" if staged_exists else "staged: no"),
                    fg=(FG_GREEN if staged_exists else FG_DIM))
                ts = dist_log.get(str(aid))
                widgets["last"].config(
                    text=(f"imported {_fmt_iso(ts)}" if ts else "not imported"),
                    fg=(FG_TEXT if ts else FG_DIM))
            except tk.TclError:
                pass

    # ── drift compare (background) ─────────────────────────────────────────────
    def _refresh_drift(self) -> None:
        rec = self._selected_record()
        if rec is None:
            self._info("Drift", "Select a pack first.")
            return
        try:
            accounts = list(self.providers.list_accounts() or [])
        except Exception:
            log.exception("[overview] list_accounts provider failed "
                          "(drift refresh runs over zero accounts)")
            accounts = []
        threading.Thread(target=self._drift_worker,
                         args=(rec.fingerprint, accounts), daemon=True).start()

    def _drift_worker(self, target_fp, accounts) -> None:
        """WORKER THREAD: compare each account's live fingerprint to the selected
        pack. Never touches Tk — badge updates are marshalled via post_ui. Called
        synchronously by tests with an inline post_ui."""
        for account_id, path, _mtime in accounts:
            try:
                fp = self.providers.live_fingerprint(path)
                state = "matches" if fp == target_fp else "differs"
            except Exception:
                state = "unreadable"
            self.providers.post_ui(self._set_drift_badge, account_id, state,
                                   target_fp)

    def _set_drift_badge(self, account_id, state, target_fp="") -> None:
        self._drift_state[account_id] = state
        widgets = self._account_rows.get(account_id)
        if not widgets:
            return
        if state == "matches":
            # Show WHICH pack state matched (short fingerprint, as the pack list).
            short = (target_fp or "")[:8]
            text = f"drift: matches ({short})" if short else "drift: matches"
        else:
            text = {"differs": "drift: differs",
                    "unreadable": "drift: n/a (unreadable)"}.get(state, "drift: —")
        color = {"matches": FG_GREEN, "differs": FG_YELLOW,
                 "unreadable": FG_DIM}.get(state, FG_DIM)
        try:
            widgets["drift"].config(text=text, fg=color)
        except tk.TclError:
            pass

    # ── folder-notice scan (background) ────────────────────────────────────────
    def _stored_fingerprints(self) -> set:
        """Snapshot of every stored pack fingerprint. MAIN THREAD ONLY — the
        store is not thread-safe, so the snapshot is taken here and handed to
        the scan worker as a plain set (fix for the off-thread list_packs race)."""
        try:
            return {r.fingerprint for r in self.providers.store.list_packs()}
        except Exception:
            log.exception("[overview] store.list_packs failed "
                          "(folder scan dedups against an empty set)")
            return set()

    def _scan_folder(self) -> None:
        folder = self._overview_dir()
        stored_fps = self._stored_fingerprints()   # snapshot on the Tk thread
        threading.Thread(target=self._folder_scan_worker,
                         args=(folder, stored_fps), daemon=True).start()

    def _folder_scan_worker(self, folder, stored_fps) -> None:
        """WORKER THREAD: list *.yaml in ``folder`` that aren't FCTool-staged and
        whose fingerprint isn't in ``stored_fps`` (snapshotted by the caller on
        the main thread — this worker never touches the store); parse failures
        are surfaced as 'unimportable'. Never touches Tk — rendering is
        marshalled via post_ui. Called synchronously by tests with an inline
        post_ui."""
        try:
            entries = sorted(n for n in os.listdir(folder)
                             if n.lower().endswith(".yaml"))
        except OSError:
            entries = []
        results = []
        for name in entries:
            if name.startswith(STAGED_PREFIX):
                continue
            path = os.path.join(folder, name)
            try:
                pack = overview_yaml.load_file(path)
                fp = overview_schema.fingerprint(pack)
            except Exception as e:
                results.append({"name": name, "path": path, "error": str(e)})
                continue
            if fp in stored_fps:
                continue
            results.append({"name": name, "path": path, "error": None})
        self.providers.post_ui(self._render_folder_notice, results)

    def _render_folder_notice(self, results) -> None:
        self._folder_notices = results
        try:
            for c in self._notice_rows.winfo_children():
                c.destroy()
        except tk.TclError:
            return
        if not results:
            tk.Label(self._notice_rows, text="No new overview files found.",
                     bg=BG_DARK, fg=FG_DIM, font=_FONT_SM).pack(anchor="w", padx=4)
            return
        for r in results:
            row = tk.Frame(self._notice_rows, bg=BG_DARK)
            row.pack(fill=tk.X, padx=4, pady=1)
            if r.get("error"):
                tk.Label(row, text=f"{r['name']} — unimportable: {r['error']}",
                         bg=BG_DARK, fg=FG_RED, font=_FONT_SM,
                         anchor="w").pack(side=tk.LEFT, fill=tk.X, expand=True)
            else:
                tk.Label(row, text=r["name"], bg=BG_DARK, fg=FG_TEXT,
                         font=_FONT, anchor="w").pack(side=tk.LEFT)
                ttk.Button(row, text="Import", style="Dark.TButton",
                           command=lambda p=r["path"]: self._import_from_notice(p)
                           ).pack(side=tk.RIGHT)

    def _import_from_notice(self, path) -> None:
        if self._import_path(path) is not None:
            self._scan_folder()
