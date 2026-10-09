# spec_roles_ui.py
"""Custom Specialized Roles - the editor window, and what the Fleet tab needs
to fill a custom section.

The Fleet tab's ten built-in Specialized Role sections stay as they are; a
user may ADD up to ``spec_roles.MAX_CUSTOM`` categories of their own (a title,
hulls and / or doctrine tags, an optional minimum). ``spec_roles`` owns that
shape. This module owns everything a person or the Fleet tab does with it.

Two halves, split at the ``Tk shell`` marker below:

* PURE (above it; imports ``spec_roles`` only): ``Model`` - the list being
  edited, where every refusal is a sentence and nothing the user chose is
  dropped silently - the tag and hull helpers the form is built from,
  ``catalog_search`` (the LOCAL hull-name search the host hands in) and
  ``fleet_section`` (who is in a category, grouped the way the built-in
  sections group their pilots).
* Tk (below it): ``open_editor`` and its controller, hung on the window as
  ``win._sr``.

The editor only RETURNS a list: it writes no file and touches no config. The
host that opened it saves the list and rebuilds its sections.

The window never takes the application input hold (``make_modal(...,
grab=False)``): while one is held every other FCTool window - the FCPreview
tiles included - is dead to clicks. The host owns the one-editor-at-a-time
guard that the hold would otherwise have provided.
"""
from __future__ import annotations

import logging

import spec_roles

log = logging.getLogger(__name__)

# Why a tag is offered but cannot be ticked (shown beside it, and in a refusal).
TAG_TOO_LONG = "too long to use here"
TAG_UNUSABLE = "cannot be used here"

SEARCH_MIN_CHARS = 2      # a shorter search lists nothing
SEARCH_LIMIT = 40         # hits listed; the hint says how many more there are
_ROSTER_LIMIT = 5000      # hull names asked of the catalogue in one call


# -- the form's fields ---------------------------------------------------------

def parse_minimum(text):
    """The Minimum field as ``(value, ok)``: blank is ``(None, True)`` - no
    minimum; a whole number from 1 to ``spec_roles.MAX_THRESHOLD`` is ``(n,
    True)``; anything else is ``(None, False)``. Total."""
    if text is None:
        return None, True
    if not isinstance(text, str):
        return None, False
    text = text.strip()
    if not text:
        return None, True
    if text.isascii() and text.isdigit() and len(text) <= 6:
        value = int(text)
        if 1 <= value <= spec_roles.MAX_THRESHOLD:
            return value, True
    return None, False


def tag_problem(tag) -> str:
    """Why ``tag`` cannot be part of a category, or ``""`` when it can.

    A category tag must equal a doctrine tag character for character, so the
    only usable tag is one ``spec_roles`` stores UNCHANGED. Doctrine tags typed
    locally have no length limit; one over ``spec_roles.MAX_TAG_LEN`` would be
    dropped whole (``TAG_TOO_LONG``), one it would alter - padded with blanks,
    holding a control character - would never match (``TAG_UNUSABLE``)."""
    if not isinstance(tag, str) or not tag.strip():
        return TAG_UNUSABLE
    kept = spec_roles.clean_category({"title": "x", "tags": [tag]})
    if kept is not None and kept["tags"] == [tag]:
        return ""
    if len(tag.strip()) > spec_roles.MAX_TAG_LEN:
        return TAG_TOO_LONG
    return TAG_UNUSABLE


def tag_choices(vocabulary, selected=()):
    """The tick boxes of the form: ``[(tag, problem)]`` - every tag of
    ``vocabulary`` in its own order, then the tags of ``selected`` (the edited
    category's own) that the vocabulary lacks, so editing a category never
    loses a tag this setup does not know. ``problem`` is ``tag_problem(tag)``:
    not blank means "show it, disabled". Total; each tag once."""
    out, seen = [], set()
    for source in (vocabulary, selected):
        if not isinstance(source, (list, tuple)):
            continue
        for tag in source:
            if isinstance(tag, str) and tag.strip() and tag not in seen:
                seen.add(tag)
                out.append((tag, tag_problem(tag)))
    return out


def tag_vocabulary(library, store=None):
    """The tags worth offering: ``library`` (the fittings library's tag list,
    in its order) followed by every tag a doctrine member of ``store`` really
    carries that the library lacks, sorted. Membership is decided by the
    ACTIVE doctrine's member tags, so those must all be on offer.

    ``store`` is duck-typed (``list_doctrines()`` -> objects with ``members``
    -> objects with ``tags``) and may be ``None``. Total: a store that raises
    costs only its own tags."""
    out = []
    if isinstance(library, (list, tuple)):
        for tag in library:
            if isinstance(tag, str) and tag and tag not in out:
                out.append(tag)
    extra = set()
    try:
        for doctrine in store.list_doctrines():
            for member in getattr(doctrine, "members", None) or ():
                for tag in getattr(member, "tags", None) or ():
                    if isinstance(tag, str) and tag and tag not in out:
                        extra.add(tag)
    except Exception:
        pass
    return out + sorted(extra)


# -- hulls ---------------------------------------------------------------------

def catalog_search(catalog):
    """``search_hulls`` for ``open_editor`` over a ``type_catalog.TypeCatalog``:
    ``search(text) -> [(type_id, name)]``, hulls whose name contains ``text``,
    by name; the whole hull roster for blank text.

    LOCAL BY CONSTRUCTION - it runs on the Tk thread at every keystroke. It
    calls only ``ship_type_names`` (a walk of the in-memory table), ``resolve_id``
    (a dict lookup) and ``category_of`` on an id that lookup just returned,
    which is therefore a known type and never reaches the catalogue's ESI
    fallback (that fallback is for UNKNOWN ids only). ``category_of`` also
    keeps out a module or charge that shares a hull's name.

    Total: a missing catalogue, or one that raises, finds nothing."""
    def search(text):
        out = []
        try:
            category_of = getattr(catalog, "category_of", None)
            names = catalog.ship_type_names(
                text if isinstance(text, str) else "", _ROSTER_LIMIT)
            for name in names:
                type_id = catalog.resolve_id(name)
                if type(type_id) is not int or type_id <= 0:
                    continue
                if category_of is not None and category_of(type_id) != "ship":
                    continue
                out.append((type_id, name))
        except Exception:
            return []
        return out
    return search


def clean_hits(raw, limit=SEARCH_LIMIT):
    """What a ``search_hulls`` call returned, as ``[(type_id, name)]``: pairs
    of a positive ``int`` (not a bool) and a non-blank name, each id once, at
    most ``limit`` (``None`` = all). Total."""
    out, seen = [], set()
    if not isinstance(raw, (list, tuple)):
        return out
    for hit in raw:
        if limit is not None and len(out) >= limit:
            break
        if not isinstance(hit, (list, tuple)) or len(hit) < 2:
            continue
        type_id, name = hit[0], hit[1]
        if type(type_id) is not int or type_id <= 0 or type_id in seen:
            continue
        if not isinstance(name, str) or not name.strip():
            continue
        seen.add(type_id)
        out.append((type_id, name))
    return out


def hull_names(search_hulls):
    """``{type_id: name}`` of every hull ``search_hulls("")`` knows - how the
    hulls ALREADY in a category get their names back. Total: a search that
    raises, or lists nothing for a blank text, names nothing."""
    try:
        return dict(clean_hits(search_hulls(""), limit=None))
    except Exception:
        return {}


def hull_label(type_id, names) -> str:
    """The name of ``type_id``, or ``"Type <id>"`` for a hull ``names`` lacks
    (one newer than the bundled table). Never a network lookup."""
    name = names.get(type_id) if isinstance(names, dict) else None
    return name if isinstance(name, str) and name else f"Type {type_id}"


# -- the Fleet tab -------------------------------------------------------------

def fleet_section(category, members, tag_index, name_of):
    """Who is in ``category`` right now: ``(ship_members, count_text)``.

    ``members`` are the fleet's ESI member dicts, pre-filtered EXACTLY as the
    built-in sections filter them - one with no ``ship_type_id`` or no
    ``character_id`` is skipped. ``tag_index`` is ``fleet_composer.
    build_tag_index`` of the active doctrine (hull type id -> its tags).
    ``ship_members`` is what the built-in sections hand their renderer:
    ``{ship_type_id: [(pilot name, str(character_id)), ...]}``, names from
    ``name_of(character_id)``. ``count_text`` is the built-ins' ``"(n)"`` with
    the minimum added: ``"(2 / 3)"``.

    Total: garbage members are skipped, a ``name_of`` that raises falls back
    to the id, anything that is not a category matches nobody."""
    index = tag_index if isinstance(tag_index, dict) else {}
    pilots = []
    try:
        for member in members or ():
            if (isinstance(member, dict) and member.get("ship_type_id", 0)
                    and member.get("character_id", 0)):
                pilots.append(member)
    except Exception:
        pilots = []
    rows = {}
    for member in spec_roles.members(
            category, pilots,
            type_id_of=lambda m: m.get("ship_type_id"),
            tags_of=lambda m: index.get(m.get("ship_type_id"))):
        character_id = member.get("character_id")
        try:
            name = name_of(character_id)
        except Exception:
            name = None
        if not isinstance(name, str) or not name:
            name = str(character_id)
        rows.setdefault(member.get("ship_type_id"), []).append(
            (name, str(character_id)))
    threshold = category.get("threshold") if isinstance(category, dict) else None
    count = sum(len(pilots_of) for pilots_of in rows.values())
    return rows, "(" + spec_roles.count_text(count, threshold) + ")"


# -- the list being edited -----------------------------------------------------

def describe(category) -> str:
    """One line about a category for the editor: ``"2 hulls, 1 tag, min 3"``
    (short enough for the line under the list). Total."""
    if not isinstance(category, dict):
        return ""
    parts = []
    for key, word in (("type_ids", "hull"), ("tags", "tag")):
        count = len(category.get(key) or ())
        if count:
            parts.append(f"{count} {word}" + ("" if count == 1 else "s"))
    if category.get("threshold") is not None:
        parts.append(f"min {category['threshold']}")
    return ", ".join(parts)


def _short(text, limit=24) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


class Model:
    """The categories being edited - always a ``spec_roles.normalize``-clean
    list (``categories``), built from a COPY of what the caller stored.

    A draft is what the form holds: ``{"title": str, "type_ids": [int],
    "tags": [str], "minimum": str}``. ``add`` / ``update`` return ``""`` when
    the draft was taken and otherwise the one sentence saying why not - and
    then change nothing. They refuse what ``spec_roles`` would quietly repair
    (a title or tag over its limit, a 13th tag, a 201st hull, a 13th category):
    the user is told, never short-changed.
    """

    def __init__(self, categories):
        self.categories = spec_roles.normalize(categories)

    def can_add(self) -> bool:
        return len(self.categories) < spec_roles.MAX_CUSTOM

    def check(self, draft, index=None):
        """``(category, "")`` for a draft that can be stored - as a new
        category (``index=None``) or in place of ``categories[index]`` - else
        ``(None, message)``."""
        draft = draft if isinstance(draft, dict) else {}
        if index is None:
            if not self.can_add():
                return None, (f"At most {spec_roles.MAX_CUSTOM} custom "
                              "categories.")
        elif type(index) is not int or not 0 <= index < len(self.categories):
            return None, "That category no longer exists."

        title = draft.get("title")
        key = spec_roles.title_key(title)
        if not key:
            return None, "Give the category a title."
        if len(title.strip()) > spec_roles.TITLE_MAX:
            return None, f"Title: at most {spec_roles.TITLE_MAX} characters."
        for position, other in enumerate(self.categories):
            if position != index and spec_roles.title_key(other["title"]) == key:
                return None, ("There is already a category called "
                              f'"{other["title"]}".')

        type_ids = list(draft.get("type_ids") or ())
        tags = list(draft.get("tags") or ())
        if len(type_ids) > spec_roles.MAX_TYPE_IDS:
            return None, (f"At most {spec_roles.MAX_TYPE_IDS} hulls per "
                          "category.")
        for tag in tags:
            problem = tag_problem(tag)
            if problem:
                verb = "is " if problem == TAG_TOO_LONG else ""
                return None, f'The tag "{_short(str(tag))}" {verb}{problem}.'
        if len(tags) > spec_roles.MAX_TAGS:
            return None, f"At most {spec_roles.MAX_TAGS} tags per category."
        if not type_ids and not tags:
            return None, "Pick at least one hull or one tag."
        minimum, ok = parse_minimum(draft.get("minimum"))
        if not ok:
            return None, ("Minimum: a whole number from 1 to "
                          f"{spec_roles.MAX_THRESHOLD}, or blank.")

        category = spec_roles.clean_category({
            "title": title, "type_ids": type_ids, "tags": tags,
            "threshold": minimum})
        if category is None:
            return None, "That category cannot be saved."
        return category, ""

    def add(self, draft) -> str:
        category, message = self.check(draft)
        if category is not None:
            self.categories.append(category)
        return message

    def update(self, index, draft) -> str:
        category, message = self.check(draft, index)
        if category is not None:
            self.categories[index] = category
        return message

    def remove(self, index) -> bool:
        if type(index) is not int or not 0 <= index < len(self.categories):
            return False
        del self.categories[index]
        return True

    def result(self) -> list:
        """The list to hand back: new, ``spec_roles.normalize``-clean."""
        return spec_roles.normalize(self.categories)


# ── Tk shell ─────────────────────────────────────────────────────────────────
# Nothing above this line uses Tk, and nothing below it is needed by the model.
import tkinter as tk                                              # noqa: E402
from tkinter import ttk                                           # noqa: E402

from ui_helpers import make_modal                                 # noqa: E402
from ui_theme import (                                            # noqa: E402
    BG_DARK, BG_ENTRY, BG_SELECT, BORDER_COLOR, FG_ACCENT, FG_DIM, FG_ORANGE,
    FG_TEXT, FG_WHITE,
)

TITLE = "Custom specialized roles"
NOTE = ("Extra Fleet-tab sections, listed after the built-in ones. A pilot "
        "counts when their hull is picked, or the active doctrine gives it a "
        "ticked tag.")
_FORM_IDLE = "Category - press Add, or select one and press Edit"
_SEARCH_IDLE = f"type {SEARCH_MIN_CHARS}+ letters"

# Positive sizes are POINTS, so every font below follows ``tk scaling``.
_LABEL_FONT = ("Consolas", 10)
_HEAD_FONT = ("Consolas", 10, "bold")
_HINT_FONT = ("Consolas", 9)

_LIST_WIDTH = 28          # characters: a title is at most TITLE_MAX of them
_LIST_ROWS = 12           # one per possible category
_HULL_ROWS = 6
_TAG_LINES = 5            # the tick-box area's height, in text lines
_NOTE_LINES = 2

# key -> (label, ttk style); the keys of ``_Editor.buttons``, in build order.
_BUTTONS = {
    "add": ("Add", "Dark.TButton"), "edit": ("Edit", "Dark.TButton"),
    "remove": ("Remove", "Dark.TButton"),
    "hull_add": ("Add >", "Dark.TButton"),
    "hull_remove": ("< Remove", "Dark.TButton"),
    "apply": ("Apply", "Dark.TButton"), "discard": ("Discard", "Dark.TButton"),
    "save": ("Save", "Green.TButton"), "cancel": ("Cancel", "Dark.TButton"),
}


class _Editor:
    """The editor's controller, hung on the window as ``win._sr``.

      ``model``        the ``Model`` being edited
      ``listbox``      the categories, one title per row, in list order
      ``editing``      ``None`` (the form is closed), ``"new"``, or the index
                       of the category in the form
      ``title_var`` / ``minimum_var`` / ``search_var``   the form's entries
      ``results``      the search hits listed, ``[(type_id, name)]``
      ``chosen``       the form's hulls, ``[type_id]``
      ``tag_vars``     tag -> ``tk.BooleanVar`` (the form's tick boxes)
      ``tag_boxes``    tag -> its ``tk.Checkbutton``
      ``buttons``      key -> ``ttk.Button`` (see ``_BUTTONS``)
      ``select(i)`` / ``begin_add()`` / ``begin_edit(i=None)`` / ``remove(i=
      None)`` / ``add_hull(type_id)`` / ``remove_hull(type_id)`` / ``apply()``
      / ``discard()`` / ``save()`` / ``cancel()``

    THE FORM edits ONE category at a time and is closed (its fields disabled)
    in between: Add opens it blank, Edit opens it on the selected category,
    Apply checks it against the model and - only when it is taken - closes it;
    a refusal stays in the form as a sentence. Save hands the whole list to
    ``on_save``; a form still open with changes in it is applied first, and a
    refusal there keeps the window open, so Save never throws typed work away
    and never saves around an error.

    ``on_save`` IS CALLED AT MOST ONCE, by Save alone, AFTER the window is
    destroyed, so a host that rebuilds its sections never does so under a live
    editor; whatever it raises is logged, not re-raised. Cancel, Escape, the
    window X and a destroy from outside report nothing.

    THE WINDOW ASKS FOR ONE SIZE whatever it shows: every text that changes
    sits in a ``width=1`` label, and the tick boxes live in a fixed-height
    scrolled area, so no button moves under the pointer.
    """

    def __init__(self, win, categories, *, tags, search_hulls, on_save):
        self.win = win
        self.model = Model(categories)
        self.tags = list(tags) if isinstance(tags, (list, tuple)) else []
        self._search_hulls = search_hulls
        self._on_save = on_save
        self._done = False
        self.editing = None
        self._loaded = None         # the draft the open form started from
        self._names = None          # type id -> hull name; built on first need
        self._seen = {}             # ... plus every search hit listed so far
        self.results = []
        self.chosen = []
        self.title_var = tk.StringVar(win)
        self.minimum_var = tk.StringVar(win)
        self.search_var = tk.StringVar(win)
        self._search_trace = None
        self.tag_vars = {}
        self.tag_boxes = {}
        self.buttons = {}
        self.listbox = self.results_box = self.chosen_box = None
        self.title_entry = self.minimum_entry = self.search_entry = None
        self.tag_text = None
        self.list_label = self.detail_label = self.form_label = None
        self.search_hint = self.error_label = None

    # -- build ----------------------------------------------------------------
    def _button(self, parent, key, command):
        label, style = _BUTTONS[key]
        # ttk pads a short label out to 11 characters; a negative width is a
        # MINIMUM in characters, so short buttons stay short.
        btn = ttk.Button(parent, text=label, style=style, command=command,
                         width=-max(6, len(label)))
        self.buttons[key] = btn
        return btn

    def _listbox(self, parent, rows, **kw):
        return tk.Listbox(
            parent, width=_LIST_WIDTH, height=rows, font=_HINT_FONT,
            bg=BG_ENTRY, fg=FG_TEXT, selectbackground=BG_SELECT,
            selectforeground=FG_WHITE, borderwidth=1, relief=tk.RIDGE,
            highlightthickness=0, activestyle="none", exportselection=False,
            **kw)

    def _entry(self, parent, var, width):
        return tk.Entry(
            parent, textvariable=var, width=width, font=_LABEL_FONT,
            bg=BG_ENTRY, fg=FG_WHITE, insertbackground=FG_WHITE,
            disabledbackground=BG_DARK, disabledforeground=FG_DIM,
            relief=tk.FLAT, highlightthickness=1,
            highlightbackground=BORDER_COLOR, highlightcolor=FG_ACCENT)

    def _label(self, parent, text="", *, font=_LABEL_FONT, fg=FG_TEXT, **kw):
        return tk.Label(parent, text=text, font=font, fg=fg, bg=BG_DARK, **kw)

    def build(self):
        win = self.win
        frame = tk.Frame(win, bg=BG_DARK, padx=14, pady=12)
        frame.pack(fill=tk.BOTH, expand=True)
        # width=1 + fill: the note wraps to the window, it never sets its width.
        note = self._label(frame, NOTE, font=_HINT_FONT, fg=FG_DIM, width=1,
                           height=_NOTE_LINES, anchor=tk.NW, justify=tk.LEFT)
        note.pack(fill=tk.X)
        self._wrap_to_width(note)

        body = tk.Frame(frame, bg=BG_DARK)
        body.pack(fill=tk.X, pady=(6, 0))
        self._build_list(body)
        self._build_form(body)

        btn_row = tk.Frame(frame, bg=BG_DARK)
        btn_row.pack(fill=tk.X, pady=(10, 0))
        self._button(btn_row, "save", self.save).pack(side=tk.LEFT)
        self._button(btn_row, "cancel", self.cancel).pack(side=tk.RIGHT)

        self._search_trace = self.search_var.trace_add(
            "write", lambda *_a: self._run_search())
        # add="+": make_modal's <Escape> lives on the toplevel too, and a plain
        # bind() replaces every handler on its sequence.
        win.bind("<Destroy>", self._on_destroy, add="+")
        self._close_form()
        self._fill_list()               # nothing selected: Edit / Remove wait
        self.listbox.focus_set()

    def _build_list(self, body):
        # fill=Y + an expanding list: the column is as tall as the form, so
        # its buttons line up with the form's.
        left = tk.Frame(body, bg=BG_DARK)
        left.pack(side=tk.LEFT, fill=tk.Y)
        self.list_label = self._label(left, font=_HEAD_FONT, fg=FG_ACCENT,
                                      width=1, anchor=tk.W)
        self.list_label.pack(fill=tk.X)
        self.listbox = self._listbox(left, _LIST_ROWS)
        self.listbox.pack(fill=tk.Y, expand=True, pady=(2, 0))
        self.listbox.bind("<<ListboxSelect>>", lambda _e: self._on_select())
        self.listbox.bind("<Double-Button-1>", lambda _e: self.begin_edit())
        self.detail_label = self._label(left, font=_HINT_FONT, fg=FG_DIM,
                                        width=1, anchor=tk.W)
        self.detail_label.pack(fill=tk.X, pady=(2, 0))
        row = tk.Frame(left, bg=BG_DARK)
        row.pack(fill=tk.X, pady=(4, 0))
        for key, command in (("add", self.begin_add), ("edit", self.begin_edit),
                             ("remove", self.remove)):
            self._button(row, key, command).pack(side=tk.LEFT, padx=(0, 6))

    def _build_form(self, body):
        right = tk.Frame(body, bg=BG_DARK)
        right.pack(side=tk.LEFT, anchor=tk.N, padx=(16, 0))
        self.form_label = self._label(right, font=_HEAD_FONT, fg=FG_ACCENT,
                                      width=1, anchor=tk.W)
        self.form_label.pack(fill=tk.X)

        row = tk.Frame(right, bg=BG_DARK)
        row.pack(fill=tk.X, pady=(2, 0))
        self._label(row, "Title").pack(side=tk.LEFT)
        self.title_entry = self._entry(row, self.title_var,
                                       spec_roles.TITLE_MAX + 2)
        self.title_entry.pack(side=tk.LEFT, padx=(6, 14))
        self._label(row, "Minimum").pack(side=tk.LEFT)
        self.minimum_entry = self._entry(row, self.minimum_var, 5)
        self.minimum_entry.pack(side=tk.LEFT, padx=(6, 6))
        self._label(row, "(blank = none)", font=_HINT_FONT,
                    fg=FG_DIM).pack(side=tk.LEFT)

        row = tk.Frame(right, bg=BG_DARK)
        row.pack(fill=tk.X, pady=(8, 0))
        self._label(row, "Hulls").pack(side=tk.LEFT)
        self.search_entry = self._entry(row, self.search_var, 22)
        self.search_entry.pack(side=tk.LEFT, padx=(6, 8))
        self.search_hint = self._label(row, font=_HINT_FONT, fg=FG_DIM,
                                       width=1, anchor=tk.W)
        self.search_hint.pack(side=tk.LEFT, fill=tk.X, expand=True)

        row = tk.Frame(right, bg=BG_DARK)
        row.pack(fill=tk.X, pady=(4, 0))
        self.results_box = self._scrolled_list(row, selectmode=tk.EXTENDED)
        mid = tk.Frame(row, bg=BG_DARK)
        mid.pack(side=tk.LEFT, padx=8)
        self._button(mid, "hull_add", self._add_selected).pack(
            fill=tk.X, pady=(0, 6))
        self._button(mid, "hull_remove", self._remove_selected).pack(fill=tk.X)
        self.chosen_box = self._scrolled_list(row, selectmode=tk.EXTENDED)
        self.results_box.bind("<Double-Button-1>",
                              lambda _e: self._add_selected())
        self.chosen_box.bind("<Double-Button-1>",
                             lambda _e: self._remove_selected())
        # Keyboard: Enter in the search adds the selected hits (or the top
        # one); Enter in the title or the minimum applies the form.
        self.search_entry.bind("<Return>",
                               lambda _e: self._add_selected(or_first=True))
        for entry in (self.title_entry, self.minimum_entry):
            entry.bind("<Return>", lambda _e: self.apply())

        self._label(right, "Tags", anchor=tk.W).pack(fill=tk.X, pady=(8, 0))
        row = tk.Frame(right, bg=BG_DARK)
        row.pack(fill=tk.X, pady=(2, 0))
        bar = ttk.Scrollbar(row, orient=tk.VERTICAL)
        bar.pack(side=tk.RIGHT, fill=tk.Y)
        # The tick boxes are windows embedded in a Text: it wraps them to its
        # width and scrolls them, at a height that never changes. width=1 +
        # fill: the hull row above sets the form's width, not the tags.
        self.tag_text = tk.Text(
            row, width=1, height=_TAG_LINES, font=_HINT_FONT, bg=BG_DARK,
            relief=tk.FLAT, bd=0, highlightthickness=1,
            highlightbackground=BORDER_COLOR, highlightcolor=BORDER_COLOR,
            wrap=tk.CHAR, cursor="arrow", takefocus=0, padx=4, pady=2,
            state=tk.DISABLED, yscrollcommand=bar.set)
        self.tag_text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        bar.configure(command=self.tag_text.yview)

        self.error_label = self._label(right, font=_HINT_FONT, fg=FG_ORANGE,
                                       width=1, height=2, anchor=tk.NW,
                                       justify=tk.LEFT)
        self.error_label.pack(fill=tk.X, pady=(6, 0))
        self._wrap_to_width(self.error_label)

        row = tk.Frame(right, bg=BG_DARK)
        row.pack(fill=tk.X, pady=(2, 0))
        self._button(row, "apply", self.apply).pack(side=tk.LEFT, padx=(0, 6))
        self._button(row, "discard", self.discard).pack(side=tk.LEFT)

    def _scrolled_list(self, row, **kw):
        box = self._listbox(row, _HULL_ROWS, **kw)
        box.pack(side=tk.LEFT)
        bar = ttk.Scrollbar(row, orient=tk.VERTICAL, command=box.yview)
        bar.pack(side=tk.LEFT, fill=tk.Y)
        box.configure(yscrollcommand=bar.set)
        return box

    @staticmethod
    def _wrap_to_width(label):
        """Wrap ``label``'s text to the width the packer gives it."""
        label.bind("<Configure>",
                   lambda event: label.configure(wraplength=event.width),
                   add="+")

    # -- the list -------------------------------------------------------------
    def _selected(self):
        """The selected row of the categories list, or None."""
        try:
            picked = self.listbox.curselection()
        except tk.TclError:
            return None
        index = int(picked[0]) if picked else None
        return index if index is not None and index < len(
            self.model.categories) else None

    def select(self, index):
        """Select row ``index`` (programmatic selection fires no
        ``<<ListboxSelect>>``, so the follow-up is called here)."""
        self.listbox.selection_clear(0, tk.END)
        if type(index) is int and 0 <= index < len(self.model.categories):
            self.listbox.selection_set(index)
            self.listbox.activate(index)
            self.listbox.see(index)
        self._on_select()

    def _fill_list(self, select=None):
        self.listbox.delete(0, tk.END)
        for category in self.model.categories:
            self.listbox.insert(tk.END, category["title"])
        self.select(select)

    def _on_select(self):
        """Re-derive what depends on the selection and the list's length."""
        if self._done:
            return
        index = self._selected()
        categories = self.model.categories
        try:
            self.list_label.configure(
                text=f"Categories ({len(categories)} of {spec_roles.MAX_CUSTOM})")
            self.detail_label.configure(
                text="" if index is None else describe(categories[index]))
            self._enable(("add",), self.model.can_add())
            self._enable(("edit", "remove"), index is not None)
        except tk.TclError:
            pass                        # the window is already gone

    def _enable(self, keys, enabled):
        state = tk.NORMAL if enabled else tk.DISABLED
        for key in keys:
            self.buttons[key].configure(state=state)

    def remove(self, index=None):
        """Remove the category at ``index`` (default: the selected one). An
        open form is discarded first - its index may be the one going."""
        if self._done:
            return
        index = self._selected() if index is None else index
        if index is None:
            return
        self._close_form()
        if self.model.remove(index):
            remaining = len(self.model.categories)
            self._fill_list(min(index, remaining - 1) if remaining else None)

    # -- the form -------------------------------------------------------------
    def begin_add(self):
        """Open the form on a blank category."""
        if self._done or not self.model.can_add():
            return
        self._open_form("new", {"title": "", "type_ids": [], "tags": [],
                                "threshold": None})

    def begin_edit(self, index=None):
        """Open the form on the category at ``index`` (default: the selected
        one). Nothing selected opens nothing."""
        if self._done:
            return
        index = self._selected() if index is None else index
        if type(index) is not int or not 0 <= index < len(self.model.categories):
            return
        self.select(index)
        self._open_form(index, self.model.categories[index])

    def _open_form(self, which, category):
        self.editing = which
        self._set_form_state(True)
        self.title_var.set(category["title"])
        threshold = category["threshold"]
        self.minimum_var.set("" if threshold is None else str(threshold))
        self.chosen = list(category["type_ids"])
        self.search_var.set("")         # its trace clears the hit list
        self._fill_tags(category["tags"])
        self._fill_chosen()
        self._show_error("")
        self.form_label.configure(
            text="New category" if which == "new"
            else f"Editing {category['title']}")
        self._loaded = self._draft()
        self.title_entry.focus_set()

    def _close_form(self):
        self.editing = None
        self._loaded = None
        self.chosen = []
        for var in (self.title_var, self.minimum_var, self.search_var):
            var.set("")
        self._fill_tags(None)
        self._fill_chosen()
        self._run_search()
        self._show_error("")
        self.form_label.configure(text=_FORM_IDLE)
        self._set_form_state(False)

    def _set_form_state(self, enabled):
        state = tk.NORMAL if enabled else tk.DISABLED
        for entry in (self.title_entry, self.minimum_entry, self.search_entry):
            entry.configure(state=state)
        self._enable(("hull_add", "hull_remove", "apply", "discard"), enabled)

    def _draft(self):
        return {"title": self.title_var.get(), "type_ids": list(self.chosen),
                "tags": [tag for tag, var in self.tag_vars.items() if var.get()],
                "minimum": self.minimum_var.get()}

    def _show_error(self, message):
        self.error_label.configure(text=message)

    def apply(self) -> str:
        """Hand the form to the model. Taken: the form closes, the list shows
        the category, ``""`` is returned. Refused: the sentence is shown in the
        form, which stays open, and returned."""
        if self._done or self.editing is None:
            return ""
        which, draft = self.editing, self._draft()
        message = (self.model.add(draft) if which == "new"
                   else self.model.update(which, draft))
        if message:
            self._show_error(message)
            return message
        self._close_form()
        self._fill_list(len(self.model.categories) - 1 if which == "new"
                        else which)
        return ""

    def discard(self):
        """Close the form without taking it."""
        if not self._done:
            self._close_form()

    # -- hulls ----------------------------------------------------------------
    def _hull_name(self, type_id):
        if type_id in self._seen:
            return self._seen[type_id]
        if self._names is None:         # one blank search, on first need
            self._names = hull_names(self._search_hulls)
        return hull_label(type_id, self._names)

    def _run_search(self):
        """List the hulls matching the search field (its var's trace)."""
        if self._done:
            return
        text = self.search_var.get().strip()
        hits = []
        if self.editing is not None and len(text) >= SEARCH_MIN_CHARS:
            try:
                hits = clean_hits(self._search_hulls(text), limit=None)
            except Exception:
                hits = []
        self.results = hits[:SEARCH_LIMIT]
        self._seen.update(self.results)
        if len(text) < SEARCH_MIN_CHARS or self.editing is None:
            hint = _SEARCH_IDLE if self.editing is not None else ""
        elif not hits:
            hint = "no hull found"
        elif len(hits) > len(self.results):
            hint = f"{len(self.results)} of {len(hits)} - keep typing"
        else:
            hint = ""
        try:
            self.results_box.delete(0, tk.END)
            for _type_id, name in self.results:
                self.results_box.insert(tk.END, name)
            self.search_hint.configure(text=hint)
        except tk.TclError:
            pass

    def _fill_chosen(self):
        self.chosen_box.delete(0, tk.END)
        for type_id in self.chosen:
            self.chosen_box.insert(tk.END, self._hull_name(type_id))

    def add_hull(self, type_id):
        """Add one hull to the form (once)."""
        if self._done or self.editing is None:
            return
        if type(type_id) is int and type_id > 0 and type_id not in self.chosen:
            self.chosen.append(type_id)
            self._fill_chosen()

    def remove_hull(self, type_id):
        """Take one hull out of the form."""
        if self._done or self.editing is None or type_id not in self.chosen:
            return
        self.chosen.remove(type_id)
        self._fill_chosen()

    def _add_selected(self, or_first=False):
        """Add the hits selected in the result list - or, with ``or_first``
        and nothing selected, the top hit."""
        rows = [int(row) for row in self.results_box.curselection()]
        if not rows and or_first:
            rows = [0]
        for type_id in [self.results[row][0] for row in rows
                        if row < len(self.results)]:
            self.add_hull(type_id)

    def _remove_selected(self):
        rows = [int(row) for row in self.chosen_box.curselection()]
        for type_id in [self.chosen[row] for row in rows if row < len(self.chosen)]:
            self.remove_hull(type_id)

    # -- tags -----------------------------------------------------------------
    def _fill_tags(self, selected):
        """Rebuild the tick boxes: none for a closed form (``selected`` None),
        else the vocabulary plus the category's own tags, those ticked."""
        for box in self.tag_boxes.values():
            box.destroy()
        self.tag_vars, self.tag_boxes = {}, {}
        text = self.tag_text
        text.configure(state=tk.NORMAL)
        text.delete("1.0", tk.END)
        if selected is not None:
            for tag, problem in tag_choices(self.tags, list(selected)):
                var = tk.BooleanVar(self.win, value=not problem and tag in selected)
                box = tk.Checkbutton(
                    text, text=tag if not problem
                    else f"{_short(tag)} ({problem})",
                    variable=var, font=_HINT_FONT, fg=FG_TEXT, bg=BG_DARK,
                    selectcolor=BG_ENTRY, activebackground=BG_DARK,
                    activeforeground=FG_ACCENT, disabledforeground=FG_DIM,
                    bd=0, highlightthickness=0, cursor="arrow",
                    state=tk.DISABLED if problem else tk.NORMAL)
                # A wheel notch over a tick box goes to the box, not the Text.
                box.bind("<MouseWheel>", self._on_tag_wheel, add="+")
                text.window_create(tk.END, window=box, padx=2)
                self.tag_vars[tag], self.tag_boxes[tag] = var, box
        text.configure(state=tk.DISABLED)

    def _on_tag_wheel(self, event):
        self.tag_text.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"

    # -- the ways out ---------------------------------------------------------
    def save(self):
        """Close the editor and hand the list to ``on_save`` - once. A form
        still open WITH CHANGES is applied first; if it is refused, nothing is
        saved and the window stays (class doc)."""
        if self._done:
            return
        if self.editing is not None and self._draft() != self._loaded:
            if self.apply():
                return
        categories = self.model.result()
        self._done = True
        self._destroy()
        if not callable(self._on_save):
            return
        try:
            self._on_save(categories)
        except Exception:
            # This runs inside a Tk callback: an escaping exception would cost
            # the user a traceback dialog for a window that is already closed.
            log.warning("custom roles editor: on_save failed", exc_info=True)

    def cancel(self):
        """Cancel, Escape and the window X - one path; nothing is reported."""
        if self._done:
            return
        self._done = True
        self._destroy()

    def abandon(self):
        """Drop a window that could not be built."""
        self._done = True
        self._destroy()

    def _destroy(self):
        self._drop_trace()
        try:
            self.win.destroy()
        except tk.TclError:
            pass

    def _drop_trace(self):
        # Removed by hand: a trace keeps its Tcl command (and this object)
        # alive for as long as the variable exists.
        trace, self._search_trace = self._search_trace, None
        if trace is not None:
            try:
                self.search_var.trace_remove("write", trace)
            except tk.TclError:
                pass

    def _on_destroy(self, event):
        # <Destroy> arrives here for every child too; only the window's own
        # counts (a destroy from outside lands here without cancel()).
        if str(event.widget) == str(self.win):
            self._done = True
            self._drop_trace()


def open_editor(parent, categories, *, tags, search_hulls, on_save):
    """Open the custom Specialized Roles editor over ``parent``; returns its
    Toplevel.

    ``categories``    the stored list (``config[spec_roles.CONFIG_KEY]``).
                      Cleaned, never edited.
    ``tags``          the tags to offer as tick boxes, in display order (see
                      ``tag_vocabulary``). One ``spec_roles`` cannot store is
                      shown disabled with the reason.
    ``search_hulls``  ``search(text) -> [(type_id, name)]``, called on the Tk
                      thread at every keystroke of the hull search - it must
                      not block (``catalog_search`` does not). ``search("")``
                      should list every hull: that is where the hulls already
                      in a category get their names; one it does not list is
                      shown as ``"Type <id>"``.
    ``on_save``       called at most once, by Save, with the new list -
                      ``spec_roles.normalize``-clean, possibly empty - after
                      the window is gone.

    The window is created and handed to ``make_modal`` in THIS function - the
    shape ``tests/test_dialog_placement.py`` looks for - without the
    application input hold; the window X is wired to the same cancel as
    Escape. The caller owns the one-editor-at-a-time guard.
    """
    win = tk.Toplevel(parent)
    win.title(TITLE)
    win.resizable(False, False)
    ctl = _Editor(win, categories, tags=tags, search_hulls=search_hulls,
                  on_save=on_save)
    make_modal(win, parent, on_cancel=ctl.cancel, grab=False)
    win.protocol("WM_DELETE_WINDOW", ctl.cancel)
    try:
        ctl.build()
    except Exception:
        ctl.abandon()                   # never leave a half-built window behind
        raise
    win._sr = ctl
    return win
