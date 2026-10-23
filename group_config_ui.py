"""The "Group configuration" window (spec section 10.2): its view-model, then its Tk shell.

This file has two halves, divided by the ``Tk shell`` marker line.

The PURE half - everything above that line - turns what the engine knows (``group_bundle``,
``group_items``) into what the window shows: the rows of the tree and which of them are
ticked, the options a selection stands for, the "What will be saved" text, the footer, the
own-name sweep, the preview rows and the summary of an import report. It imports no
``tkinter``, no ``ui_theme`` and no ``fc_gui`` - not at the top, not lazily - reads and
writes no file and keeps no state of its own, so all of it is tested without a window.

Rules of the pure half
----------------------
* TOTAL. Every function takes ``None``, a wrong type, an item id the registry does not know,
  a payload of the wrong shape or ten thousand rows, and gives back something sensible. The
  window must not die on a strange file.
* Text is cleaned on its way in (no control, format - zero-width, bidi override - or line
  separator character) and bounded: a string for a row, a label or a cell is at most
  ``MAX_SHOWN`` characters; a sentence of an import report at most ``MAX_LINE``. Text that
  was cut always ends in ``...``. The "What will be
  saved" text is the one place where nothing may be hidden from the builder: there a long
  value is not clipped but RUNS ON over further lines / rows of at most ``MAX_SHOWN``
  characters each (and is cut only at ``MAX_VALUE`` characters).
* The manifest (``manifest_lines``, ``footer_text``) is derived ONLY from the bundle object
  that is written (``bundle.categories``), never from the selection or the state - so it
  cannot differ from the file.
* Nothing here calls ``group_items.preview`` or ``group_apply.plan_import``: a plan costs
  more than a second on a large fittings library. ``preview_rows`` and ``result_summary``
  are GIVEN results; the shell decides when to plan (debounced, never per keystroke).

Row ids (``Node.id``; the shell uses them as Treeview iids)
----------------------------------------------------------
``"*"`` the root; ``"meta.tag"`` the group tag; ``"cat:<category>"`` a category;
``"cat:navigation"`` the Navigation row (nothing to share, no box); ``"<category>.<key>"``
an item (its registry id); ``"<item id>#<record key>"`` a field or record of an item.
``build_nodes`` never returns the same id twice and always lists a parent before its rows.
The first element of a ``preview_rows`` row is one of these ids - or, for a part of the file
this build does not know, the synthetic ``"?<n>"``, which is never a row of the tree: the
text parsed out of such a problem line can be blank, repeated or equal to a real id, so it
is DISPLAY TEXT ONLY and must never be used as an iid.

The example tag in this file is ``ABCD`` ("by ABCD").
"""

import re
import unicodedata
from copy import deepcopy
from dataclasses import dataclass

import group_items
from group_bundle import CATEGORIES, CATEGORY_LABELS, clean_tag, clean_text
from group_items import ABSENT, NOT_SUPPORTED, ItemResult, Options


# The partial glyph is the approved mock's; it is 2-3 px narrower than the ballot boxes in
# the fallback font, and that is accepted.
GLYPH = {"on": "☑", "off": "☐", "partial": "▣"}

ROOT_ID = "*"
TAG_ID = "meta.tag"                     # the same id as group_apply.TAG_ITEM
NAVIGATION_ID = "cat:navigation"
CATEGORY_PREFIX = "cat:"
LEAF_SEP = "#"
UNKNOWN_PREFIX = "?"                    # preview_rows: a part of the file this build ignores
DEPTHS = ("items", "leaves")
NODE_KINDS = ("root", "category", "item", "leaf", "info")
SECTIONS = ("saved", "never", "left_out")       # detail_lines

MAX_SHOWN = 200                         # a row, a label, a cell
MAX_LINE = 500                          # a sentence of an import report
MAX_VALUE = 20_000                      # one value of "What will be saved", over many lines
MAX_LABEL = 80                          # the label in front of such a value
MAX_NOTE_LINES = 50                     # export notes, warnings; then "and <n> more"
MAX_PROBLEM_LINES = 20                  # result_summary: problem lines shown
MAX_UNKNOWN = 50                        # manifest_lines: entries this build does not know
MAX_ITEM_LINES = 200                    # result_summary: item lines
MIN_NAME = 3                            # find_names: shorter names are not searched
_NOTES_CLIP = 120                       # the joined notes of a value item

NOTHING_TO_SHARE = "nothing to share"
NOT_SUPPORTED_TEXT = "not supported by this version"
NOTHING_PERSONAL = "(nothing personal in this item)"
NOT_TICKED = "left as it is"
SAME = "already the same"
VERIFIED = "verified after restart"     # also the engine's own note for a structure
AFTER_RESTART = " - " + VERIFIED
UNUSABLE = "this selection cannot be shared on its own"
EDITED_FILE = "This file was edited after it was exported."
_LEFT_OUT = " of the file could not be read and is left out."
_UNKNOWN_NOTE = "The file has a problem this version of FCTool cannot describe."

_SEPARATOR = " " + chr(0xB7) + " "      # the footer's middle dot
_MORE_LINE = re.compile(r"and ([0-9]{1,12}) more")
_HIDDEN_KINDS = ("Cc", "Cf", "Zl", "Zp")        # never shown: control, format, line separators
_NO_LIMIT = 1 << 40                     # clean_text without a clip (see _show)
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_PREVIEW_WORDS = ("adds {n}", "updates {n}", "removes {n}", "{n} " + SAME, "changes")
_RESULT_WORDS = ("added {n}", "updated {n}", "removed {n}", "{n} " + SAME, "changed")
_CHANGE_LISTS = ("added", "updated", "removed", "unchanged")

_HEADLINES = {
    "applied": "Group configuration applied.",
    "unchanged": "Nothing to change - your settings already match.",
    "restored": "Your previous settings were restored.",
    "interrupted_restored": "The import was interrupted. Your previous settings were restored.",
}
_NOT_APPLIED = {"import": "The import was not applied",
                "restore": "Your previous settings were not restored"}
_WAITING = "The import is waiting."
_UNKNOWN_RESULT = "This import ended in a way this version of FCTool does not know."


# -- small helpers -------------------------------------------------------------------------

def _visible(text, limit) -> str:
    """``text`` without the characters of ``_HIDDEN_KINDS``, stripped. Reads no further
    than it must: once more than ``limit`` characters are certain to remain it stops, and
    what it returns is then longer than ``limit`` and right in its first ``limit``
    characters - all a caller that clips needs."""
    kept = []
    for ch in text:
        if unicodedata.category(ch) in _HIDDEN_KINDS:
            continue
        if not kept and ch.isspace():           # the left strip, after the removal
            continue
        kept.append(ch)
        if len(kept) > limit and not ch.isspace():
            break
    return "".join(kept).rstrip()


def _show(value, limit=MAX_SHOWN) -> str:
    """``value`` as display text: text or a plain number - anything else is blank.

    Cleaned: no control character, no lone surrogate (``group_bundle.clean_text``), and no
    format (zero-width, bidi override) or line / paragraph separator character either -
    the rule ``group_bundle.clean_tag`` has for the tag, here for every label: a record
    name out of a file must not carry an override or a line break into a row.

    Bounded: at most ``limit`` characters, and text that was cut ALWAYS ends in ``...`` -
    the length is judged on the whole cleaned text, never on a slice of it (a slice that
    ends on a blank would be stripped and look as if it had fitted). Total."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    text = clean_text(value, _NO_LIMIT)
    if not text.isascii():                      # ASCII has nothing more to lose
        text = _visible(text, limit)
    if len(text) > limit:
        text = text[:max(0, limit - 3)].rstrip() + "..."
    return text


def _count(n, one, many) -> str:
    return f"{n} {one if n == 1 else many}"


def _as_list(value) -> list:
    return list(value) if isinstance(value, (list, tuple)) else []


def _texts(value, limit=MAX_LINE) -> list:
    """The non-blank lines of a list, cleaned; an entry that is not text is no line. One
    line of text counts as a list of one."""
    if isinstance(value, str):
        value = [value]
    return [line for line in (_show(entry, limit) for entry in _as_list(value)
                              if isinstance(entry, str)) if line]


def _more_of(line):
    """What a count line - ``"and <n> more"``, the line that ends a bounded list - stands
    for: ``n``. ``None`` for every other line, and for anything that is not text. The ONE
    reader of that line in this module."""
    counted = _MORE_LINE.fullmatch(line) if isinstance(line, str) else None
    return int(counted.group(1)) if counted else None


def _capped(lines, limit) -> list:
    """``lines`` cut to ``limit`` lines plus one last ``"and <n> more"``. A count line that
    is cut away is added up, not counted as one line."""
    if len(lines) <= limit:
        return list(lines)
    more = 0
    for line in lines[limit:]:
        counted = _more_of(line)
        more += 1 if counted is None else counted
    return list(lines[:limit]) + [f"and {more} more"]


def _listed(value) -> int:
    """How many entries a list of a result STOOD for. The engine stores bounded lists - the
    first entries, then one last count line - so a list is counted as its entries plus what
    that last line says. Only the LAST entry can be the count: the engine writes it there
    and nowhere else (and a real name spelled that way, stored last, cannot be told from
    it - the engine owns that line)."""
    entries = _as_list(value)
    more = _more_of(entries[-1]) if entries else None
    return len(entries) if more is None else len(entries) - 1 + more


def _merged(lines, limit=MAX_NOTE_LINES) -> list:
    """Lines of SEVERAL sources as one list: each line once, in order, at most ``limit``
    of them, then one last ``"and <n> more"``. The count lines the sources carry (each of
    them is bounded on its own) are not shown where they stood - between the lines of two
    items nobody could tell what they count - but added to that last one."""
    kept, seen, more = [], set(), 0
    for line in lines:
        counted = _more_of(line)
        if counted is not None:
            more += counted
        elif line not in seen:
            seen.add(line)
            if len(kept) < limit:
                kept.append(line)
            else:
                more += 1
    return kept + [f"and {more} more"] if more else kept


def _chunks(text, width) -> list:
    """``text`` as pieces of at most ``width`` characters, broken at a space where one fits.
    Never empty: blank text is one blank piece."""
    width = max(20, width)
    pieces = []
    while len(text) > width:
        cut = text.rfind(" ", 0, width + 1)
        if cut <= 0:
            cut = width
        pieces.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text or not pieces:
        pieces.append(text)
    return pieces


def _registry() -> tuple:
    try:
        return tuple(group_items.registry())
    except Exception:
        return ()


def _item(item_id):
    try:
        return group_items.get_item(item_id)
    except Exception:
        return None


def _payload(payloads, item_id):
    """The payload of an item, or ``ABSENT`` (also for ``None``: nothing to share)."""
    payload = payloads.get(item_id, ABSENT) if isinstance(payloads, dict) else ABSENT
    return ABSENT if payload is None else payload


def _label(item) -> str:
    return _show(item.label) or _show(item.id)


def _category_label(category) -> str:
    return CATEGORY_LABELS.get(category) if isinstance(category, str) \
        and category in CATEGORY_LABELS else _show(category, MAX_LABEL)


def _summary(item, payload) -> str:
    try:
        return _show(item.summary(payload))
    except Exception:
        return ""


def _records(item, payload) -> list:
    """``[(key, label, detail)]`` for the tickable rows of a payload; rows without a text
    key are left out. Total."""
    try:
        rows = list(item.records(payload))
    except Exception:
        return []
    found = []
    for row in rows:
        key = getattr(row, "key", None)
        if isinstance(key, str):
            found.append((key, _show(getattr(row, "label", "")) or _show(key),
                          _show(getattr(row, "detail", ""))))
    return found


def _item_lines(item, payload) -> list:
    """``[(label, value)]`` of ``item.lines(payload)``: cleaned, the label at most
    ``MAX_LABEL`` and the value at most ``MAX_VALUE`` characters. Total."""
    if payload is ABSENT or payload is None:
        return []
    try:
        pairs = list(item.lines(payload))
    except Exception:
        return []
    found = []
    for pair in pairs:
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            continue
        label, value = _show(pair[0], MAX_LABEL), _show(pair[1], MAX_VALUE)
        if label or value:
            found.append((label, value))
    return found


# -- rows ----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Node:
    """One row of the tree. ``parent`` is the id of the row above it, ``""`` for the root.
    ``kind`` is one of ``NODE_KINDS``; a row with ``checkable=False`` has no box."""

    id: str
    parent: str
    label: str
    summary: str = ""
    checkable: bool = True
    kind: str = "item"


def build_nodes(payloads, *, depth, tag="") -> list:
    """The rows for ``payloads`` (``{item id: payload}`` - ``export_all`` on the Create
    page, the validated payloads of a file on the Import page), parents before their rows.

    The root ``"All contents"``; with a non-blank ``tag`` the checkable row ``meta.tag``
    ("Group tag", "by <tag>") as its first child; one category row per
    ``group_bundle.CATEGORIES`` entry, with the Navigation row (no box, "nothing to share")
    after the Jump range category; under each category one row per registered item - with a
    payload: its ``summary``; without one: no box, "nothing to share". A category none of
    whose items has a payload has no box either, and neither has the root when nothing at
    all can be ticked.

    ``depth="leaves"`` adds, under every item with a payload, one row per field or record
    (``item.records``): ``"<item id>#<key>"``, its label and its detail. Any other depth is
    ``"items"``. Ids are unique: a second record with the key of an earlier one gets no row.
    """
    leaves = depth == "leaves"
    by_category = {}
    for item in _registry():
        by_category.setdefault(item.category, []).append(item)
    nodes = [None]                              # the root, made last
    seen = {ROOT_ID, NAVIGATION_ID}
    shown_tag = clean_tag(tag)
    if shown_tag:
        nodes.append(Node(TAG_ID, ROOT_ID, "Group tag", f"by {shown_tag}"))
        seen.add(TAG_ID)
    for category in CATEGORIES:
        items = [item for item in by_category.get(category, ()) if item.id not in seen]
        shared = sum(1 for item in items if _payload(payloads, item.id) is not ABSENT)
        category_id = CATEGORY_PREFIX + category
        seen.add(category_id)
        nodes.append(Node(category_id, ROOT_ID, _category_label(category),
                          _count(shared, "item", "items") if shared else NOTHING_TO_SHARE,
                          checkable=bool(shared), kind="category"))
        for item in items:
            seen.add(item.id)
            payload = _payload(payloads, item.id)
            if payload is ABSENT:
                nodes.append(Node(item.id, category_id, _label(item), NOTHING_TO_SHARE,
                                  checkable=False))
                continue
            nodes.append(Node(item.id, category_id, _label(item), _summary(item, payload)))
            for key, label, detail in _records(item, payload) if leaves else ():
                leaf_id = f"{item.id}{LEAF_SEP}{key}"
                if leaf_id not in seen:
                    seen.add(leaf_id)
                    nodes.append(Node(leaf_id, item.id, label, detail, kind="leaf"))
        if category == "jump":
            nodes.append(Node(NAVIGATION_ID, ROOT_ID, "Navigation", NOTHING_TO_SHARE,
                              checkable=False, kind="info"))
    nodes[0] = Node(ROOT_ID, "", "All contents", kind="root",
                    checkable=any(node.checkable for node in nodes[1:]))
    return nodes


class SelectionTree:
    """Which rows are ticked.

    Only the LEAF-LEVEL rows hold a tick: the checkable rows with no checkable row below
    them (an item in ``items`` depth; a field or record in ``leaves`` depth). Every other
    checkable row shows what is below it: ``on`` when every leaf-level row under it is on,
    ``off`` when none is, else ``partial``. A row without a box ignores ``set`` and
    ``toggle``, is always ``off`` and never counts for the rows above it.

    ``nodes`` lists a parent BEFORE its rows (``build_nodes`` does): a row whose parent has
    not been listed yet - or is not a row at all - is a top-level row, so no input can make
    a cycle. Anything in ``nodes`` that is not a :class:`Node` with a non-blank text id, and
    a second row with an id already taken, is left out. ``checked`` is the first state of
    every row. One ``set`` costs one pass over the rows; ``state`` and ``text`` are lookups.
    """

    def __init__(self, nodes, *, checked=True):
        self._default = bool(checked)
        self._nodes = {}                        # id -> Node, in the order given
        self._parent = {}                       # id -> parent id, or None (top level)
        self._children = {None: []}
        try:
            rows = list(nodes) if nodes is not None else []
        except TypeError:
            rows = []
        for node in rows:
            if not isinstance(node, Node) or not isinstance(node.id, str) or not node.id \
                    or node.id in self._nodes:
                continue
            parent = node.parent if isinstance(node.parent, str) \
                and node.parent in self._nodes else None
            self._nodes[node.id] = node
            self._parent[node.id] = parent
            self._children[node.id] = []
            self._children[parent].append(node.id)
        below = dict.fromkeys(self._nodes, False)       # has a checkable row under it
        for node_id in reversed(self._nodes):
            parent = self._parent[node_id]
            if parent is not None and (self._nodes[node_id].checkable or below[node_id]):
                below[parent] = True
        self._leaves = [node_id for node_id, node in self._nodes.items()
                        if node.checkable and not below[node_id]]
        self._leaf_set = frozenset(self._leaves)
        self._on = set(self._leaves) if self._default else set()
        self._total = {}
        self._ticked = {}
        self._recount()

    def _recount(self) -> None:
        total = dict.fromkeys(self._nodes, 0)
        ticked = dict.fromkeys(self._nodes, 0)
        for node_id in reversed(self._nodes):           # rows before the row above them
            if node_id in self._leaf_set:
                total[node_id] += 1
                ticked[node_id] += node_id in self._on
            parent = self._parent[node_id]
            if parent is not None:
                total[parent] += total[node_id]
                ticked[parent] += ticked[node_id]
        self._total, self._ticked = total, ticked

    def _get(self, node_id):
        return self._nodes.get(node_id) if isinstance(node_id, str) else None

    def _leaves_under(self, node_id) -> list:
        found, stack = [], [node_id]
        while stack:
            current = stack.pop()
            if current in self._leaf_set:
                found.append(current)
            stack.extend(self._children[current])
        return found

    @property
    def nodes(self) -> list:
        """The rows, in order."""
        return list(self._nodes.values())

    def node(self, node_id):
        """The row with that id, or ``None``."""
        return self._get(node_id)

    def __contains__(self, node_id) -> bool:
        return self._get(node_id) is not None

    def __len__(self) -> int:
        return len(self._nodes)

    def state(self, node_id) -> str:
        """``"on"`` | ``"off"`` | ``"partial"``. An unknown row and a row without a box are
        ``"off"``."""
        node = self._get(node_id)
        if node is None or not node.checkable:
            return "off"
        ticked = self._ticked[node_id]
        if ticked and ticked == self._total[node_id]:
            return "on"
        return "partial" if ticked else "off"

    def set(self, node_id, on) -> str:
        """Tick or untick a row and everything under it. -> its state afterwards."""
        node = self._get(node_id)
        if node is not None and node.checkable:
            rows = self._leaves_under(node_id)
            if on:
                self._on.update(rows)
            else:
                self._on.difference_update(rows)
            self._recount()
        return self.state(node_id)

    def toggle(self, node_id) -> str:
        """``on`` -> off; ``off`` and ``partial`` -> on. -> the state afterwards."""
        return self.set(node_id, self.state(node_id) != "on")

    def children(self, node_id) -> list:
        """The ids of the rows directly under a row; ``""`` (or ``None``) gives the
        top-level rows."""
        if node_id is None or node_id == "":
            return list(self._children[None])
        return list(self._children[node_id]) if self._get(node_id) is not None else []

    def checked_leaves(self) -> list:
        """The ids of the ticked leaf-level rows, in row order."""
        return [node_id for node_id in self._leaves if node_id in self._on]

    def text(self, node_id) -> str:
        """What the row shows: glyph, space, label - the label alone for a row without a
        box, ``""`` for an unknown row."""
        node = self._get(node_id)
        if node is None:
            return ""
        label = node.label if isinstance(node.label, str) else ""
        if not node.checkable:
            return label
        return f"{GLYPH[self.state(node_id)]} {label}"

    def _knows(self, node_id):
        """Whether this tree has the row ticked - ``None`` when it has no say: the row is
        not here, or has no box here. A partly ticked row counts as ticked."""
        node = self._nodes.get(node_id)
        if node is None or not node.checkable:
            return None
        return self._ticked[node_id] > 0

    def carry_over(self, new_nodes):
        """A new tree over ``new_nodes`` that keeps what was ticked here; this tree is not
        changed. For the rebuilds of the window: the switch between the two depths, a
        refresh from the settings, a file read again.

        Each leaf-level row of the new tree takes the state of the first of these that
        this tree has a say on (a row it holds, with a box): the row itself, then the rows
        above it in the NEW tree, nearest first - so a row that is new takes its parent's
        state. A row that was only partly ticked here counts as ticked: an item whose
        records were chosen one by one becomes the whole item when the tree goes back to
        ``items`` depth, and a record that is new under a partly ticked item arrives
        ticked. A row nothing here has a say on gets this tree's first state (``checked``).
        """
        new = SelectionTree(new_nodes, checked=self._default)
        ticked = set()
        for leaf in new._leaves:
            current, known = leaf, None
            while current is not None and known is None:
                known = self._knows(current)
                current = new._parent[current]
            if self._default if known is None else known:
                ticked.add(leaf)
        new._on = ticked
        new._recount()
        return new

    def partial_items(self) -> list:
        """The ids of the ITEM rows that are partly ticked, in row order - the items of
        which single fields or records were chosen. ``carry_over`` to ``items`` depth makes
        each of them the whole item again, so while this list is not empty the switch back
        from "edit in detail" to "copy my current settings" changes what is shared and
        wants a confirm. (A category or the root being ``partial`` is no such edit.)"""
        return [node_id for node_id, node in self._nodes.items()
                if node.kind == "item" and self.state(node_id) == "partial"]


def _rows_of(tree, item_id) -> tuple:
    """``(ticked, unticked)``: the field / record rows of an item as ``(key, row id)``."""
    prefix = item_id + LEAF_SEP
    ticked, unticked = [], []
    for row in tree.children(item_id):
        if row.startswith(prefix):
            (ticked if tree.state(row) == "on" else unticked).append((row[len(prefix):], row))
    return ticked, unticked


def options_from_tree(tree, payloads) -> Options:
    """The engine's options for what is ticked: a registered item that has a payload and
    is fully on -> ``True``; partly on -> the keys of its ticked rows; off -> left out.
    Modes and conflicts start empty. ``apply_tag`` is the state of the ``meta.tag`` row -
    ``True`` when the tree has no such row. Not a tree at all: nothing selected, no tag.

    A key list is not a promise: the engine drops an item whose ``subset`` gives nothing
    for it. :func:`unusable_selection` names those items."""
    if not isinstance(tree, SelectionTree):
        return Options(apply_tag=False)
    options = Options(apply_tag=tree.state(TAG_ID) == "on" if TAG_ID in tree else True)
    for item in _registry():
        if item.id not in tree or _payload(payloads, item.id) is ABSENT:
            continue
        state = tree.state(item.id)
        if state == "on":
            options.selected[item.id] = True
        elif state == "partial":
            keys = [key for key, _row in _rows_of(tree, item.id)[0]]
            if keys:
                options.selected[item.id] = keys
    return options


def _travels(item, payload, keys) -> bool:
    """Whether the engine keeps anything of ``payload`` for these keys. ``subset`` gets a
    copy, as it does from the engine: the caller's draft is never changed."""
    try:
        part = item.subset(deepcopy(payload), keys)
    except Exception:
        return False
    return part is not ABSENT and part is not None


def unusable_selection(tree, payloads) -> dict:
    """``{item id: why}`` for every item that is ticked IN PART and whose part the engine
    would drop whole: ``item.subset(payload, <the ticked keys>)`` is ``ABSENT`` - the
    filter row of the intel filter unticked while a coalition of it stays ticked, the
    system row of the market staging unticked while its station stays. Such an item looks
    ticked in the tree and would be missing from the file, or untouched by the import,
    without a word.

    ``why`` is one sentence for that row: ``"needs '<row label>' to be shared"`` when
    ticking ONE further row would mend it (the first such row, in row order), otherwise
    ``"this selection cannot be shared on its own"``.

    Only partly ticked items are looked at - a whole item always travels, an unticked one
    is not meant to - and each costs one copy of its payload, so call this where a plan is
    made, not per keystroke. Total: not a tree, no payloads -> ``{}``."""
    if not isinstance(tree, SelectionTree):
        return {}
    unusable = {}
    for item in _registry():
        payload = _payload(payloads, item.id)
        if payload is ABSENT or tree.state(item.id) != "partial":
            continue
        ticked, unticked = _rows_of(tree, item.id)
        keys = [key for key, _row in ticked]
        if not keys or _travels(item, payload, keys):
            continue
        why = UNUSABLE
        for key, row in unticked:
            if _travels(item, payload, keys + [key]):
                label = _show(getattr(tree.node(row), "label", ""), MAX_LABEL)
                if label:
                    why = f"needs '{label}' to be shared"
                break
        unusable[item.id] = why
    return unusable


# -- what will be saved --------------------------------------------------------------------

def manifest_lines(bundle) -> list:
    """Everything the file of ``bundle`` holds, as lines of text.

    ``"Group tag: by <tag>"`` first when the bundle has a tag. Then, for each registered
    item in the bundle, in registry order: ``"<Category label> > <Item label>"`` and, for
    each ``item.lines(payload)``, ``"  <label>: <value>"`` (a value too long for one line
    runs on over lines indented by four spaces - nothing is hidden). Then one header and
    ``"  (not supported by this version)"`` for every entry no item of this build reads,
    at most ``MAX_UNKNOWN`` of them and ``"and <n> more"``.

    It reads ``bundle.meta.tag`` and ``bundle.categories`` - the object that is written -
    and nothing else. Every line is at most ``MAX_SHOWN`` characters."""
    lines = []
    tag = clean_tag(getattr(getattr(bundle, "meta", None), "tag", None))
    if tag:
        lines.append(f"Group tag: by {tag}")
    categories = getattr(bundle, "categories", None)
    if not isinstance(categories, dict):
        return lines
    known = set()
    for item in _registry():
        entries = categories.get(item.category)
        if not isinstance(entries, dict) or item.key not in entries:
            continue
        known.add((item.category, item.key))
        lines.append(_show(f"{_category_label(item.category)} > {_label(item)}"))
        for label, value in _item_lines(item, entries[item.key]):
            head = f"  {label}: " if label else "  "
            pieces = _chunks(value, MAX_SHOWN - max(len(head), 4))
            lines.append((head + pieces[0]).rstrip())
            lines.extend("    " + piece for piece in pieces[1:])
    unknown, more = [], 0
    for category, entries in categories.items():
        if isinstance(entries, dict):
            headers = [f"{_category_label(category)} > {_show(key, MAX_LABEL)}"
                       for key in entries if (category, key) not in known]
        else:
            headers = [_show(category, MAX_LABEL)]
        for header in headers:
            if len(unknown) < MAX_UNKNOWN:
                unknown.append(header)
            else:
                more += 1
    for header in unknown:
        lines.extend((header, f"  ({NOT_SUPPORTED_TEXT})"))
    if more:
        lines.append(f"and {more} more")
    return lines


def export_note_lines(state, ctx=None, items=None) -> list:
    """What an export of ``state`` LEAVES OUT that the builder would not expect - a fit
    that cannot be shared, a rule naming a pilot, a preset the edit dialog would refuse:
    ``group_items.collect_export_notes(state, ctx, items)`` as display lines, at most
    ``MAX_NOTE_LINES`` and ``"and <n> more"``.

    ``items`` is the engine's own argument: ``None`` for every item, or a list of items /
    item ids - ``export_note_lines(state, ctx, [item_id])`` is what :func:`detail_lines`
    takes as that item's notes. One bare item id counts as a list of one. The lines are
    the items' own wording and do not name their item. Total."""
    if isinstance(items, str):
        items = [items]
    try:
        lines = group_items.collect_export_notes(state, ctx, items)
    except Exception:
        lines = []
    return _capped(_texts(lines, MAX_SHOWN), MAX_NOTE_LINES)


def detail_lines(item_id, payload, notes=()) -> list:
    """``[(section, label, value)]`` for the row of one item - the three sections of
    "What will be saved":

    * ``("saved", label, value)`` per ``item.lines(payload)``; a value too long for one row
      runs on in rows with a blank label. No payload: ``("saved", "(nothing to share)", "")``.
    * ``("never", line, "")`` per ``item.never_saved``, or the one row
      ``("never", "(nothing personal in this item)", "")``.
    * ``("left_out", line, "")`` per line of ``notes`` - that item's export notes
      (``export_note_lines(state, ctx, [item_id])``); none when there are none.

    ``"meta.tag"`` (payload: the tag) gives the tag's two rows. Any other id that is not a
    registered item gives ``[]``."""
    if item_id == TAG_ID:
        tag = clean_tag(payload)
        return [("saved", "Group tag", f"by {tag}" if tag else "(none)"),
                ("never", NOTHING_PERSONAL, "")]
    item = _item(item_id)
    if item is None:
        return []
    rows = []
    for label, value in _item_lines(item, payload):
        pieces = _chunks(value, MAX_SHOWN)
        rows.append(("saved", label, pieces[0]))
        rows.extend(("saved", "", piece) for piece in pieces[1:])
    if not rows:
        rows.append(("saved", f"({NOTHING_TO_SHARE})", ""))
    never = _texts(getattr(item, "never_saved", ()), MAX_SHOWN)
    rows.extend(("never", line, "") for line in never or [NOTHING_PERSONAL])
    rows.extend(("left_out", line, "")
                for line in _capped(_texts(notes, MAX_SHOWN), MAX_NOTE_LINES))
    return rows


def _size_text(size_bytes):
    """``"210 KB"`` / ``"2.2 MB"``, or ``None`` for a size that is not a number."""
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, (int, float)) \
            or not 0 <= size_bytes < float("inf"):
        return None
    kilobytes = size_bytes / 1024
    if kilobytes > 1024:
        return f"{kilobytes / 1024:.1f} MB"
    return f"{max(1, int(kilobytes + 0.5))} KB"


def footer_text(bundle, *, size_bytes, name_hits) -> str:
    """``"<n> items in <c> categories . <names> . about <size>"`` (the dots are middle
    dots), counted on ``bundle.categories`` - the file - alone.

    ``name_hits`` is the result of :func:`find_names` or its length: ``"no personal names
    found"`` / ``"<k> personal name(s) found"``. ``size_bytes`` gives whole KB (at least
    1), or MB with one decimal above 1,024 KB. A part that is not known is LEFT OUT rather
    than guessed: ``name_hits=None`` (the sweep has not run) says nothing about names,
    ``size_bytes=None`` nothing about the size."""
    categories = getattr(bundle, "categories", None)
    filled = [entries for entries in categories.values()
              if isinstance(entries, dict) and entries] if isinstance(categories, dict) else []
    parts = [f"{_count(sum(map(len, filled)), 'item', 'items')} in "
             f"{_count(len(filled), 'category', 'categories')}"]
    if isinstance(name_hits, (list, tuple, set, frozenset)):
        name_hits = len(name_hits)
    if type(name_hits) is int and name_hits >= 0:
        parts.append(f"{_count(name_hits, 'personal name', 'personal names')} found"
                     if name_hits else "no personal names found")
    size = _size_text(size_bytes)
    if size:
        parts.append(f"about {size}")
    return _SEPARATOR.join(parts)


# -- the own-name sweep --------------------------------------------------------------------

_ESCAPE = re.compile(r"\\(?:u([0-9a-fA-F]{4})|(.))", re.DOTALL)
_ESCAPED = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f",
            '"': '"', "\\": "\\", "/": "/"}
_SURROGATE_PAIR = re.compile(r"([\ud800-\udbff])([\udc00-\udfff])")


def _unescaped(text) -> str:
    """``text`` with its JSON string escapes resolved, left to right - so an escaped
    backslash followed by ``u0041`` stays a backslash and five characters."""
    def resolve(match):
        if match.group(1) is not None:
            return chr(int(match.group(1), 16))
        return _ESCAPED.get(match.group(2), match.group(0))

    def join(match):
        return chr(0x10000 + ((ord(match.group(1)) - 0xD800) << 10)
                   + ord(match.group(2)) - 0xDC00)

    return _SURROGATE_PAIR.sub(join, _ESCAPE.sub(resolve, text))


def _folded(text) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _has_word(haystack, needle) -> bool:
    """Whether ``needle`` stands in ``haystack`` as a whole word: where the name begins or
    ends with a letter or digit, the character next to it is not one."""
    left, right = needle[0].isalnum(), needle[-1].isalnum()
    size, end_of_text, start = len(needle), len(haystack), 0
    while True:
        at = haystack.find(needle, start)
        if at < 0:
            return False
        end = at + size
        if not (left and at > 0 and haystack[at - 1].isalnum()) \
                and not (right and end < end_of_text and haystack[end].isalnum()):
            return True
        start = at + 1


def find_names(text, names) -> list:
    """The ``names`` that stand in ``text`` - the privacy sweep before a file is written
    (spec section 8). ``text`` is the exact text of the file (``group_bundle.dumps``).

    It is the ONLY guard for the places no item can know hold a pilot's name: the name of
    a fleet template, wing or squad, a slot tag, a rule value that was retyped, any text
    that merely contains a name, a ``char`` pill placed by hand in a MOTD.

    A name counts from ``MIN_NAME`` characters (blanks at its ends aside). It is searched
    ignoring case, as a whole word - "Ann" is not found in "Annex" or "JoAnn"; only letters
    and digits make a word, so an apostrophe, a hyphen, a dot, a blank or an underscore
    next to the name ends it - and both in the text as it is and with the text's JSON
    escapes resolved: a name with a letter the file writes as ``\\u00e9`` is found, in
    either case, and so is a name right after an escaped line break (``\\nAnn``).

    -> the names found, as they were given, sorted, each once (two spellings that differ
    only in case are one name: the first). Total: no text, no names -> ``[]``."""
    if not isinstance(text, str) or not text:
        return []
    if isinstance(names, str):
        names = [names]
    if not isinstance(names, (list, tuple, set, frozenset)):
        return []
    wanted = {}
    for name in names:
        if not isinstance(name, str):
            continue
        shown = name.strip()
        if len(shown) < MIN_NAME:
            continue
        key = _folded(shown)
        if key:
            wanted.setdefault(key, shown)
    if not wanted:
        return []
    haystacks = [_folded(text)]
    if "\\" in text:
        haystacks.append(_folded(_unescaped(text)))
    found = [shown for key, shown in wanted.items()
             if any(_has_word(haystack, key) for haystack in haystacks)]
    return sorted(found, key=lambda name: (name.casefold(), name))


# -- what will happen, what happened ---------------------------------------------------------

def _as_result(entry):
    """An ``ItemResult`` as it is, a stored one (``to_dict``) rebuilt, anything else
    ``None``."""
    if isinstance(entry, ItemResult):
        return entry
    if isinstance(entry, dict):
        try:
            return ItemResult.from_dict(entry)
        except Exception:
            return None
    return None


def _results(results) -> list:
    return [result for result in map(_as_result, _as_list(results)) if result is not None]


def _notes_of(result) -> list:
    return _texts(getattr(result, "notes", ()))


def _change_text(item, result, words, failed, clip) -> str:
    """One result in words. ``words``: the four count phrases and the word for "something
    changed"; ``failed``: how a failed result starts; ``clip``: how long the joined notes
    of a value item may be. Blank for a status nobody knows."""
    status = result.status if isinstance(result.status, str) else ""
    warnings = _texts(getattr(result, "warnings", ()))
    if status == "applied":
        text = ""
        if item is None or item.kind == "value":    # a value item says "<old> -> <new>"
            text = _show("; ".join(_notes_of(result)), clip)
        if not text:
            sizes = [_listed(getattr(result, name, ())) for name in _CHANGE_LISTS]
            text = ", ".join(word.format(n=size)
                             for word, size in zip(words, sizes) if size) or words[4]
        return text
    if status == "unchanged":
        return SAME
    if status in ("skipped", "failed"):
        lead = "skipped" if status == "skipped" else failed
        return f"{lead}: {warnings[0]}" if warnings else lead
    return ""


def preview_rows(results, payloads, tag="", problems=()) -> list:
    """``[(row id, in the file, what will happen)]`` for the Import page. All arguments
    are GIVEN - the results and problems of ``group_apply.plan_import``, the payloads of
    ``group_items.validate_bundle``; nothing is planned here.

    * With a non-blank ``tag`` (the file's), first ``("meta.tag", "by <tag>", ...)``: the
      note of its result, or "already the same".
    * Then one row per registered item that has a payload or a result, in registry order:
      ``in the file`` is ``item.summary(payload)``; ``what will happen`` by status -
      ``applied``: for a value item its notes joined with ``"; "`` (at most 120
      characters), otherwise the counts ``adds N, updates N, removes N, N already the
      same`` (zero parts left out); ``unchanged``: "already the same"; ``skipped``:
      "skipped: <first warning>"; ``failed``: "cannot be applied: <first warning>"; with
      any task, the suffix " - verified after restart" - unless the text says "verified
      after restart" already (the market staging item writes that note itself). An item
      (or the tag) with no result is not ticked: "left as it is".
    * Then one row per ``problems`` line that starts with ``group_items.NOT_SUPPORTED``:
      ``("?<n>", <the text after it>, "not supported by this version")``, n counting
      from 1. ``"?<n>"`` is never the id of a row of the tree, and the text in the second
      column is for showing only - it can be blank, repeated or equal to a real item id,
      so it must NOT be used as a Treeview iid.

    A cell is a cell: the notes of a list item (and whatever of a value item's notes the
    120 characters cut away) are not here - :func:`result_notes` has all of them, for the
    box beside the warnings.

    The first elements are unique. Every cell is at most ``MAX_SHOWN`` characters."""
    by_id = {}
    for result in _results(results):
        if isinstance(result.item_id, str) and result.item_id:
            by_id.setdefault(result.item_id, result)

    def happens(item, result) -> str:
        if result is None:
            return NOT_TICKED
        text = _change_text(item, result, _PREVIEW_WORDS, "cannot be applied", _NOTES_CLIP)
        if _as_list(getattr(result, "tasks", ())) and VERIFIED not in text:
            text = f"{text}{AFTER_RESTART}" if text else VERIFIED
        return _show(text)

    rows = []
    shown_tag = clean_tag(tag)
    if shown_tag or TAG_ID in by_id:
        rows.append((TAG_ID, f"by {shown_tag}" if shown_tag else "",
                     happens(None, by_id.get(TAG_ID))))
    for item in _registry():
        payload, result = _payload(payloads, item.id), by_id.get(item.id)
        if payload is ABSENT and result is None:
            continue
        rows.append((item.id, "" if payload is ABSENT else _summary(item, payload),
                     happens(item, result)))
    unknown = 0
    for line in _as_list(problems):
        if isinstance(line, str) and line.startswith(NOT_SUPPORTED):
            unknown += 1
            rows.append((f"{UNKNOWN_PREFIX}{unknown}", _show(line[len(NOT_SUPPORTED):]),
                         NOT_SUPPORTED_TEXT))
    return rows


def result_notes(results) -> list:
    """Every note of every result - ``ItemResult`` or stored (``to_dict``) - as one list:
    each note once, in order, cleaned, at most ``MAX_LINE`` characters, at most
    ``MAX_NOTE_LINES`` of them and ``"and <n> more"``.

    The notes are what the items SAY about a change beyond its counts - "AAA-11 moved from
    hostile to friendly", "Coalition 'North Bloc': replaced by the group's definition",
    "3 fits added, 0 already in your library" - and no cell of ``preview_rows`` has room
    for them. The Import page shows this list beside the warnings; ``result_summary`` has
    the same notes in its own block. Total."""
    return _merged([note for result in _results(results) for note in _notes_of(result)])


def _read_note_line(note) -> str:
    """One note of ``group_bundle.read_file`` / ``loads`` as a sentence. The three kinds
    are told apart by the WHOLE string (``"checksum"``) or by the text before the first
    colon (``"dropped:<category>"``, ``"more:<n>"``) - a category may itself be named
    "checksum" or "more"."""
    if not isinstance(note, str):
        return ""
    if note == "checksum":
        return EDITED_FILE
    kind, colon, rest = note.partition(":")
    if colon and kind == "dropped":
        if rest in CATEGORY_LABELS:
            return f"The {CATEGORY_LABELS[rest]} part{_LEFT_OUT}"
        name = _show(rest, MAX_LABEL)
        return f"The part '{name}'{_LEFT_OUT}" if name else f"A part{_LEFT_OUT}"
    if colon and kind == "more":
        if rest.isascii() and rest.isdigit() and len(rest) <= 12:
            if int(rest) == 1:
                return f"1 more part{_LEFT_OUT}"
            return f"{int(rest)} more parts of the file could not be read and are left out."
        return "More parts of the file could not be read and are left out."
    return _UNKNOWN_NOTE


def import_warnings(read_notes, problems, results) -> list:
    """The lines of the Import page's warnings box, for a file that was just read:

    1. the reader's notes (``group_bundle.read_file`` / ``loads``) in words - ``checksum``:
       "This file was edited after it was exported."; ``dropped:<category>``: that part of
       the file could not be read and is left out; ``more:<n>``: n more parts were;
    2. the validation ``problems`` (``group_items.validate_bundle`` / ``plan_import``);
    3. every warning of every result.

    Each line once, in that order, at most ``MAX_NOTE_LINES`` and one last
    ``"and <n> more"`` - which also counts what the sources' own count lines stood for.
    One line of text for ``read_notes`` or ``problems`` counts as a list of one. Total."""
    if isinstance(read_notes, str):
        read_notes = [read_notes]
    lines = [line for line in map(_read_note_line, _as_list(read_notes)) if line]
    lines.extend(_texts(problems))
    for result in _results(results):
        lines.extend(_texts(getattr(result, "warnings", ())))
    return _merged(lines)


def import_header(bundle) -> list:
    """What the Import page shows above the tree for a file that was just read, from
    ``bundle.meta`` alone: ``"Name: ..."``, ``"Revision: <n>"``, ``"Author: ..."``,
    ``"Created: <date>"`` (the date of an ISO stamp; anything else as it is),
    ``"Made with FCTool <version>"``, ``"Group tag: by <tag>"``, ``"Notes: ..."`` - in
    that order, a part that is blank or unusable left out. Every line is cleaned and at
    most ``MAX_SHOWN`` characters (the notes: ``MAX_LINE``). Total: no meta -> ``[]``."""
    meta = getattr(bundle, "meta", None)
    if meta is None:
        return []

    def part(field, limit=MAX_SHOWN):
        return _show(getattr(meta, field, None), limit)

    lines = []
    if part("name"):
        lines.append(f"Name: {part('name')}")
    revision = getattr(meta, "revision", None)
    if type(revision) is int and revision >= 1:
        lines.append(f"Revision: {revision}")
    if part("author"):
        lines.append(f"Author: {part('author')}")
    created = part("created", MAX_LABEL)
    if created:
        date = _ISO_DATE.match(created)
        lines.append(f"Created: {date.group(0) if date else created}")
    if part("app_version", MAX_LABEL):
        lines.append(f"Made with FCTool {part('app_version', MAX_LABEL)}")
    tag = clean_tag(getattr(meta, "tag", None))
    if tag:
        lines.append(f"Group tag: by {tag}")
    lines = [_show(line) for line in lines]
    notes = part("notes", MAX_LINE)
    if notes:
        lines.append(_show(f"Notes: {notes}", MAX_LINE))
    return lines


def _title(item_id) -> str:
    """How a result line names its item: "<Category label> > <Item label>"."""
    if item_id == TAG_ID:
        return "Group tag"
    item = _item(item_id)
    if item is None:
        return _show(item_id, MAX_LABEL) or "(unnamed)"
    return f"{_category_label(item.category)} > {_label(item)}"


def _attention_line(section, reason) -> str:
    section = _show(section, MAX_LABEL) if isinstance(section, str) else ""
    reason = _show(reason, MAX_LINE) if isinstance(reason, str) else ""
    return _show(f"{section}: {reason}" if section and reason else reason or section,
                 MAX_LINE)


def result_summary(report) -> list:
    """An import report (``group_apply``) as lines for the History page.

    1. The headline. The report's own ``message`` is the truth and is shown as given -
       for ``applied``, ``unchanged``, ``restored`` and ``interrupted_restored`` as it is
       (a report without one gets the standard sentence of its status), for ``deferred``
       as it is (without one: "The import is waiting."), for ``failed`` behind "The import
       was not applied: " - "Your previous settings were not restored: " when the report
       is of the kind ``restore``. A status this build does not know shows its message.
    2. One line per item that is not ``unchanged`` (at most ``MAX_ITEM_LINES``):
       ``"<Category label> > <Item label>: <what happened>"`` - the notes of a value item
       joined with ``"; "``, the counts ``added N, updated N, removed N, N already the
       same`` otherwise, "skipped: <first warning>", "not applied: <first warning>". The
       tag is "Group tag". Only ``MAX_LINE`` bounds such a line: what changed is not cut
       to the width of a preview cell.
    3. The notes (:func:`result_notes`) that no line of 2 shows whole: every note of a
       list item and of an unchanged one, and those of a value item whose line had to be
       cut. Nothing an item said is hidden, and nothing is said twice.
    4. Every warning of every item (the one a skipped or failed line already shows is not
       repeated).
    5. Every attention reason, as ``"<Settings section>: <reason>"`` - those of the items
       and those of a top-level ``attention`` list.
    6. For a non-empty ``problems`` list: ``"<n> thing(s) in the file could not be used:"``
       and the lines, at most ``MAX_PROBLEM_LINES`` and "and <n> more".

    The blocks 3, 4 and 5 hold each line once and at most ``MAX_NOTE_LINES`` lines, then
    "and <n> more". ``log`` is for the application log and is never shown. Total: anything
    that is not a dict gives ``[]``; a report with keys missing, of the wrong type or
    unknown (written by an older or newer build) gives the lines that can be made of it.
    The items are read from ``items`` (``results`` in its absence), stored or as
    ``ItemResult``. Every line is at most ``MAX_LINE`` characters."""
    if not isinstance(report, dict):
        return []
    status, message = report.get("status"), report.get("message")
    message = _show(message, MAX_LINE) if isinstance(message, str) else ""
    if status == "failed":
        lead = _NOT_APPLIED["restore" if report.get("kind") == "restore" else "import"]
        headline = f"{lead}: {message}" if message else f"{lead}."
    elif status == "deferred":
        headline = message or _WAITING
    elif isinstance(status, str) and status in _HEADLINES:
        headline = message or _HEADLINES[status]
    else:
        headline = message or _UNKNOWN_RESULT
    lines = [_show(headline, MAX_LINE)]

    stored = report.get("items")
    if not isinstance(stored, (list, tuple)):
        stored = report.get("results")
    results = _results(stored)

    changed, shown, warnings, attention = [], set(), [], []
    for result in results:
        item_id = result.item_id if isinstance(result.item_id, str) else ""
        said = _texts(getattr(result, "warnings", ()))
        warnings.extend(said[1:] if result.status in ("skipped", "failed") else said)
        attention.extend(_attention_line(getattr(flag, "section", None),
                                         getattr(flag, "reason", None))
                         for flag in _as_list(getattr(result, "attention", ())))
        if result.status == "unchanged" or not item_id:
            continue
        text = _change_text(None if item_id == TAG_ID else _item(item_id), result,
                            _RESULT_WORDS, "not applied", MAX_LINE)
        if text:
            line = _show(f"{_title(item_id)}: {text}", MAX_LINE)
            changed.append(line)
            notes = _notes_of(result)
            if notes and line.endswith("; ".join(notes)):
                shown.update(notes)                 # all of them, whole, in this line
    attention.extend(_attention_line(flag.get("section"), flag.get("reason"))
                     for flag in _as_list(report.get("attention")) if isinstance(flag, dict))
    lines.extend(_capped(changed, MAX_ITEM_LINES))
    lines.extend(_merged([note for result in results for note in _notes_of(result)
                          if note not in shown]))
    lines.extend(_merged(warnings))
    lines.extend(_merged([line for line in attention if line]))

    problems = _texts(report.get("problems"))
    if problems:
        total = 0
        for line in problems:
            counted = _MORE_LINE.fullmatch(line)
            total += int(counted.group(1)) if counted else 1
        lines.append(f"{_count(total, 'thing', 'things')} in the file could not be used:")
        lines.extend(_capped(problems, MAX_PROBLEM_LINES))
    return lines


# -- doctrine pairs (spec "doctrine merge" 5, 9) --------------------------------------------
#
# A doctrine of the file that looks like one the importer has (``group_apply.plan_matches``
# -> ``doctrine_match.Pair``) is offered a choice of its own. A pair of the same name
# follows the item's "An entry of the same name" radio (D4); a pair of different names is
# merged into the importer's doctrine (D8, the owner, 2026-10-23) - and that default is
# SENT, the engine never decides it at boot.

DOCTRINES_ITEM = "fittings.doctrines"   # the one item whose records are paired
PAIR_CHOICES_EXACT = (("theirs", "Use the group's version"), ("mine", "Keep mine"),
                      ("merge", "Merge"))
PAIR_CHOICES_OTHER = (("merge", "Merge"),
                      ("both", "Keep both"),
                      ("theirs", "Use the group's version (keeps your name)"),
                      ("mine", "Keep mine"))
PAIR_DEFAULT_OTHER = "merge"            # D8: a pair of different names nobody chose for
# The "This doctrine:" line of the detail text says more than the radio for these.
PAIR_CHOICE_LINES = {"merge": "Merge - adds the group's refits, slots and tags to yours"}
MAX_HULLS_SHOWN = 8                     # pair_detail_lines: hull names per list


def _exact(pair) -> bool:
    return getattr(pair, "exact", None) is True


def pair_choices(pair) -> tuple:
    """``((action, label), ...)``: what a pair offers - "keep both" only to a pair of
    different names (one of the same name would make a name twin), "merge" to both."""
    return PAIR_CHOICES_EXACT if _exact(pair) else PAIR_CHOICES_OTHER


def pair_default(pair, conflict="theirs") -> str:
    """The choice a pair gets when none is made for it: a pair of the same name follows
    the item's ``conflict`` (``theirs`` | ``mine`` | ``merge``; anything else reads as
    ``theirs``), a pair of different names is merged (D8)."""
    if not _exact(pair):
        return PAIR_DEFAULT_OTHER
    return conflict if conflict in dict(PAIR_CHOICES_EXACT) else "theirs"


def doctrine_header_line(count) -> str:
    """The Import header's line for ``count`` paired doctrines; ``""`` for none."""
    if type(count) is not int or count < 1:
        return ""
    if count == 1:
        return ("1 doctrine in this file looks like one you already have - choose what "
                "happens to it.")
    return (f"{count} doctrines in this file look like ones you already have - choose "
            "what happens to each.")


def _hull_list(names) -> str:
    names = names if isinstance(names, (list, tuple)) else ()
    shown = [name for name in map(_show, names) if name]
    text = ", ".join(shown[:MAX_HULLS_SHOWN])
    if len(shown) > MAX_HULLS_SHOWN:
        text += f" and {len(shown) - MAX_HULLS_SHOWN} more"
    return text


def pair_detail_lines(pair, action) -> list:
    """The lines a paired record row shows first: ``"Looks like your <local> - <why>"``,
    the hulls only one side flies (at most ``MAX_HULLS_SHOWN`` names each, then
    ``"and <n> more"``; a list with nothing in it is left out) and ``"This doctrine:
    <label of action>"`` (``PAIR_CHOICE_LINES`` says what a merge adds; left out for an
    action the pair does not offer). Total: no pair -> ``[]``."""
    local = getattr(pair, "local_name", None)
    try:
        why = _show(pair.reason())
    except Exception:
        return []
    if not isinstance(local, str):
        return []
    lines = [f"Looks like your {_show(local) or '(no name)'} - {why}"]
    for lead, names in (("Only in yours: ", getattr(pair, "mine_only", ())),
                        ("Only in the group's: ", getattr(pair, "theirs_only", ()))):
        text = _hull_list(names)
        if text:
            lines.append(lead + text)
    label = dict(pair_choices(pair)).get(action) if isinstance(action, str) else None
    if label:
        lines.append(f"This doctrine: {PAIR_CHOICE_LINES.get(action, label)}")
    return lines


# -- the host ------------------------------------------------------------------------------

@dataclass
class GroupHost:
    """What the window needs from the app. ``fc_gui`` builds one (``_group_host``); a test
    hands in a recording fake. Nothing is optional: a host without ``own_names`` would
    switch the privacy sweep off without a word.

    ``app_dir``       the data folder (text)
    ``app_version``   this build's version (text), written into a new file
    ``flush_config()``                      write the app's config to disk now
    ``restart() -> bool``                   restart the app; False when it cannot right now
    ``confirm(title, message, buttons)``    ask; ``buttons`` is a list of ``(label, value)``
                                            -> the chosen value, or ``None``
    ``own_names() -> list[str]``            the user's own character names (never logged)
    ``open_settings(section)``              show a Settings section (its TOC title)
    ``update_state() -> str``               the updater's state (``"installing"`` blocks)
    ``writable() -> bool``                  whether the data folder can be written
    ``edit_palette(parent, roles, on_done)``  open the palette editor on a draft
    ``has_characters() -> bool``            whether any character is signed in
    """

    app_dir: object
    app_version: object
    flush_config: object
    restart: object
    confirm: object
    own_names: object
    open_settings: object
    update_state: object
    writable: object
    edit_palette: object
    has_characters: object


# ── Tk shell ──
# Nothing above this line uses Tk, and nothing below it is needed by the view-model.
#
# The window is ONE controller - ``win._gc`` - and the widgets that show its state. Every
# behaviour is a method of the controller; a widget only calls one and repaints.
#
# Rules of the shell
# ------------------
# * It never takes the application input hold (``make_modal(..., grab=False)``): while one
#   is held every other FCTool window - the client previews included - is dead to clicks.
#   The host owns the one-window-at-a-time guard.
# * Every effect goes through the host (``GroupHost``) or through ``group_apply`` /
#   ``group_state`` / ``group_bundle``. The window holds no reference to the app's config
#   and never imports the main module: it reads the files, after ``host.flush_config()``.
# * Everything it shows comes out of the view-model above, cleaned and clipped. The item
#   ids of a Treeview are SYNTHETIC (``n0``, ``n1`` ...; ``x0`` ... for the parts of a file
#   this build does not know; ``h0`` ... for the history): text out of a file is never an
#   item id, a widget name, a format string or a list this code takes apart.
# * Nothing here runs on another thread and nothing waits: the two things that cost time -
#   the own-name sweep and the plan of an import - run behind one ``after`` job
#   (``DEBOUNCE_MS``), which is cancelled when the window goes.
# * No error path ends the window: a file that cannot be read, a write that fails and a
#   staging that is refused each end as a sentence in ``status_var``.
import dataclasses                                                # noqa: E402
import logging                                                    # noqa: E402
import math                                                       # noqa: E402
import os                                                         # noqa: E402
import tkinter as tk                                              # noqa: E402
import tkinter.font as tkfont                                     # noqa: E402
from tkinter import filedialog, messagebox, ttk                   # noqa: E402

import group_apply                                                # noqa: E402
import group_bundle                                               # noqa: E402
import group_state                                                # noqa: E402
from ui_helpers import attach_tooltip, make_modal, parent_work_area  # noqa: E402
from ui_theme import (                                            # noqa: E402
    BG_DARK, BG_ENTRY, BG_PANEL, BG_SELECT, BORDER_COLOR, FG_ACCENT, FG_DIM, FG_GREEN,
    FG_ORANGE, FG_RED, FG_TEXT, FG_WHITE,
)

log = logging.getLogger(__name__)

TITLE = "Group configuration"
GEOMETRY = "980x640"
MIN_SIZE = (820, 520)                   # at 100% display scaling; see min_size
BASE_SCALING = 96 / 72                  # tk scaling at 100% (96 dpi): 1.333...
MIN_GROWTH = 1.35                       # the smallest height grows this much faster
SCREEN_ROOM = 80                        # title bar + frame + margins, off the screen
TREE_STYLE = "Group.Treeview"
# The window's buttons: the app's "Dark" and "Green" styles (see _configure_styles).
BUTTON_STYLES = {"dark": "Group.Dark.TButton", "green": "Group.Green.TButton"}
PALETTE_ITEM = "palette.colors"         # its editor is the palette editor, through the host
SIGN_IN_SECTION = "SSO Characters"      # the Settings section the result page points at
DEBOUNCE_MS = 150                       # the name sweep and the plan wait this long

PAGES = (("create", "Create"), ("import", "Import"), ("history", "History"))
MODES = (("copy", "Copy my current settings"), ("granular", "Granular editing"))
MODE_HINTS = {
    "copy": "Everything shareable from your setup is included. Untick what you want left out.",
    "granular": ("Pick single entries and edit the values that go into the file. "
                 "Your own settings are not changed."),
}
MODE_CHOICES = (("merge", "Add to mine"), ("replace", "Replace mine"))
CONFLICT_CHOICES = (("theirs", "Use the group's version"), ("mine", "Keep mine"))
# The labels of every word an item's ``conflict_choices`` may hold (the doctrines add "merge").
CONFLICT_LABELS = {"theirs": "Use the group's version", "mine": "Keep mine", "merge": "Merge"}


def item_conflict_choices(item) -> tuple:
    """``((value, label), ...)`` of an item's "An entry of the same name:" radio: its
    ``conflict_choices`` that have a label, in its order - the first is its default. The
    doctrines offer three, every other item that can meet a same-named entry the two of
    ``CONFLICT_CHOICES``; an item that cannot (or no item) offers none."""
    if item is None or not getattr(item, "conflicts", False):
        return ()
    words = getattr(item, "conflict_choices", None)
    choices = tuple((word, CONFLICT_LABELS[word]) for word in
                    (words if isinstance(words, tuple) else ())
                    if isinstance(word, str) and word in CONFLICT_LABELS)
    return choices or CONFLICT_CHOICES


FILE_TYPES = [("FCTool group configuration", "*" + group_bundle.EXTENSION),
              ("All files", "*.*")]

CAUTION = ("Share this file only with your group - it holds your stagings, doctrines and "
           "channels.")
HINT_UNSAVED = "Unsaved changes in Settings are not included - press Save Settings first."
LINE_BACKUP = "Applying restarts FCTool. A backup of your settings is kept (History > Restore)."
LINE_SCOPE = ("Only the ticked items change. Sign-ins, characters, previews, hotkeys and EVE "
              "settings are never touched.")
HEAD_SAVED, HEAD_NEVER, HEAD_LEFT_OUT = "What will be saved", "Never saved", "Left out of the file"
HEAD_IN_FILE, HEAD_HAPPENS = "In the file", "What will happen"

MSG_CHECKING = "Checking the file..."
MSG_SAME = _HEADLINES["unchanged"]
MSG_NOT_WRITABLE = "FCTool cannot write to its data folder."
MSG_INSTALLING = "An update is installing - try again in a moment."
MSG_BUSY = "FCTool's settings are busy - try again in a moment."
MSG_NOTHING_TICKED = "Nothing is ticked."
MSG_NOTHING_USABLE = "Nothing would change - see the warnings for what could not be applied."
MSG_UNREADABLE = "Your settings file could not be read - there is nothing to share from it."
MSG_NOT_SAVED = "The group file was not saved."
MSG_READ_BACK = "The file could not be read back as it was written, so it was removed."
MSG_TOO_BIG = ("This selection is too large for one group file - untick the largest items "
               "(usually fits).")
MSG_STAGED = "The import is staged - it is applied the next time FCTool starts."
MSG_RESTORE_STAGED = "The restore is staged - it is applied the next time FCTool starts."
MSG_RESTART_REFUSED = ("An update is installing - FCTool cannot restart right now. What is "
                       "staged is applied at the next start.")
MSG_UPDATE_FIRST = ("An update is ready: this restart installs it first, and the import is "
                    "applied the next time FCTool starts after that.")
MSG_CANCELLED = "The pending {what} was cancelled."
MSG_NOTHING_PENDING = "Nothing is pending."
MSG_NOT_CANCELLED = ("The pending {what} could not be cancelled - it is still applied at the "
                     "next start.")
CANCEL_PENDING = "Cancel pending {what}"    # the banner's button: "import" or "restore"
MSG_STAYS_OPEN = ("Settings was not opened - this window stays open for the group file you "
                  "are editing.")
MSG_NO_BACKUP = "That backup no longer exists."
MSG_ITEM_OFF = "not ticked - nothing of this item goes into the file"
MSG_NOT_EDITABLE = "this value cannot be edited here"
MSG_PALETTE_REFUSED = "Those colours could not be used."
MSG_DISCARD = ("Going back to a copy of your settings discards the values you edited, and an "
               "item of which you picked single entries is shared whole again.")
MSG_DISCARD_CLOSE = "Close the window and discard what you edited for this group file?"
MSG_REPLACE = ("{what} is already waiting for the next start of FCTool.\n\n"
               "Replace it with this one?")
MSG_RESTORE = ("Put back the settings from before the import of {when}?\n\nEvery change made "
               "to those settings since then is lost. FCTool restarts to do it.")
MSG_NAMES = ("The file would contain: {names}.\n\nThose are your own character names. Go "
             "back and take them out of the text they stand in (a MOTD, the name of a "
             "template, a tag), or save the file as it is.")
EDIT_ELSEWHERE = "Change these in their own tab, then press Refresh from my settings."
EDIT_PALETTE = "The colours are edited as one palette. Your own colours are not changed."
NO_FILE = "Choose a group file to see what it holds and what it would change."
NEVER_ANYWHERE = "Sign-ins, characters, previews, hotkeys and EVE settings - in no group file"
NO_FILE_CHOSEN = "No file chosen."
NOT_A_GROUP_FILE = "This file cannot be imported."     # when the reader gave no reason
NO_HISTORY = "No group file has been imported yet."
PICK_AN_IMPORT = "Select an import to see what it changed."

# Positive sizes are POINTS, so every font below follows ``tk scaling``.
_LABEL_FONT = ("Consolas", 10)
_HEAD_FONT = ("Consolas", 10, "bold")
_HINT_FONT = ("Consolas", 9)
_HINT_BOLD = ("Consolas", 9, "bold")
_TREE_FONT = ("Consolas", 9)

_PAD = 14                               # the window's side padding, in pixels
_TREE_ROWS = 6                          # rows a tree ASKS for; the window gives it the rest
_TREE_CHARS = 30                        # the tree column's starting width, in characters
_DETAIL_CHARS = 30                      # the right pane's starting width
_IMPORT_TREE_ROWS = 3                   # the Import page's: its warnings box asks for more
_HEADER_LINES = (2, 4)                  # the Import page's header box: whole lines, within
_WARN_LINES = (2, 6)                    # ... and its warnings box: as tall as its text, within
_WARN_LINES_SHORT = 4                   # ... its most on a page shorter than _ROOMY_PAGE lines
_ROOMY_PAGE = 28
_ATTENTION_LINES = (1, 4)               # the History page's attention list: whole lines, within
_DETAIL_LINES = 4                       # a page's text box, as it asks
_EDITOR_DETAIL_LINES = (3, 5)           # the Create page's, under an editor: five lines, three
_ROOMY_PANE = 18                        # ... where the pane is shorter than this many lines
_NAMES_LINES = 3                        # a "names" field of the editor
_FIELD_LABEL_CHARS = 20                 # a field's label column is at most this wide ...
_FIELD_LABEL_SHARE = 0.45               # ... and at most this share of the editor's width
_FIELD_LABEL_MIN_CHARS = 8
_LABEL_COLUMN = 24                      # the longest label that still shares a line with its value
_INDICATOR_PX = 20                      # the fold arrow of a tree row, when it cannot be asked
_OPEN_KINDS = ("root", "category")      # the rows that start unfolded
_STATUS_COLOURS = {"info": FG_DIM, "ok": FG_GREEN, "warn": FG_ORANGE}
_PLACEHOLDER_ID = "0" * 32              # as long as a real bundle id: for a size judged early


def _configure_styles(win) -> None:
    """The ttk styles of this window: its tree (``rowheight`` is the font's line + 6, so
    it follows ``tk scaling``) and its two button styles."""
    style = ttk.Style(win)
    linespace = tkfont.Font(root=win, font=_TREE_FONT).metrics("linespace")
    style.configure(TREE_STYLE, background=BG_ENTRY, fieldbackground=BG_ENTRY,
                    foreground=FG_TEXT, bordercolor=BORDER_COLOR, borderwidth=0,
                    font=_TREE_FONT, rowheight=linespace + 6)
    style.map(TREE_STYLE, background=[("selected", BG_SELECT)],
              foreground=[("selected", FG_WHITE)])
    style.configure(TREE_STYLE + ".Heading", background=BG_PANEL, foreground=FG_ACCENT,
                    font=_HINT_BOLD, relief="flat")
    style.map(TREE_STYLE + ".Heading", background=[("active", BG_ENTRY)])
    # The app's two button styles, with ONE thing added: a disabled button looks disabled.
    # Here that is a state the user has to read - "Apply and restart" while nothing would
    # change - and the app's own styles draw it like an enabled one. Everything else is
    # inherited ("Group.Dark.TButton" falls back to "Dark.TButton"), the app's own state
    # colours included: they are read from the parent style, not repeated here.
    for name in BUTTON_STYLES.values():
        parent = name[len("Group."):]
        for option, colour in (("foreground", FG_DIM), ("background", BG_PANEL)):
            try:
                inherited = [tuple(spec) for spec in style.map(parent, option)
                             if "disabled" not in spec[:-1]]
            except tk.TclError:
                inherited = []
            style.map(name, **{option: [("disabled", colour)] + inherited})


def _when(stamp) -> str:
    """A stored UTC stamp as ``"2026-10-08 18:00"``; anything else cleaned, as it is."""
    text = _show(stamp, MAX_LABEL) if isinstance(stamp, str) else ""
    date = _ISO_DATE.match(text)
    if not date:
        return text
    clock = re.match(r"[T ]([0-9]{2}:[0-9]{2})", text[date.end():])
    return f"{date.group(0)} {clock.group(1)}" if clock else date.group(0)


def _day(stamp) -> str:
    return _when(stamp)[:10] if isinstance(stamp, str) and _ISO_DATE.match(stamp) else ""


def _file_bytes(text) -> int:
    """How large the file of ``text`` (``group_bundle.dumps``) is on disk: UTF-8, and each
    line break as this platform writes one - what the writer's own size limit counts."""
    return len(text.encode("utf-8")) + text.count("\n") * (len(os.linesep) - 1)


def _item_count(bundle) -> int:
    categories = getattr(bundle, "categories", None)
    if not isinstance(categories, dict):
        return 0
    return sum(len(entries) for entries in categories.values() if isinstance(entries, dict))


def _category_lines(lines, label) -> list:
    """The part of ``manifest_lines`` that belongs to one category: its item headers and
    the lines under them."""
    kept, inside = [], False
    for line in lines:
        if not line.startswith(" "):
            inside = line.startswith(label + " > ")
        if inside:
            kept.append(line)
    return kept


def _warn_height(lines, most=_WARN_LINES[1]) -> int:
    """The lines the warnings box asks for when its text has ``lines`` of them: one for a
    single line ("No warnings."), otherwise as many as there are - at least two, at most
    ``most`` (six). Whole lines: the box never ends in half of one."""
    return 1 if lines <= 1 else max(_WARN_LINES[0], min(most, lines))


def _box_height(lines, least, most) -> int:
    """The lines a text box asks for when its text has ``lines`` of them: that many, within
    ``least`` .. ``most``. Whole lines: the box never ends in half of one."""
    return max(least, min(most, lines))


class _TreeView:
    """One page's tree: a ``SelectionTree`` drawn in a ``ttk.Treeview``.

    THE ITEM IDS OF THE WIDGET ARE SYNTHETIC - ``n<index>`` for a row of the tree,
    ``x<index>`` for an extra row (a part of a file this build does not know) - with a
    two-way map to the row ids. A row id holds a record key out of a file (any text at
    all: blanks, braces, a name that looks like an item id), so it is never handed to Tk
    as an id.

    ``tree``     the ``SelectionTree`` shown (``None``: nothing)
    ``sel``      the id of the selected row, or ``None``
    ``order``    the row ids in display order, the extra rows last
    ``widget``   the Treeview; ``frame`` holds it and its scrollbar

    Input: a click in the box of a row, a double-click and Space toggle (``on_toggle``);
    any click selects (``on_select``, through the widget's own selection event).
    """

    def __init__(self, parent, columns, *, cells, tags, on_select, on_toggle,
                 tree_chars=_TREE_CHARS, rows=_TREE_ROWS):
        self.tree = None
        self.sel = None
        self.order = []
        self._extra = {}                # extra row id -> its label
        self._cells, self._tags = cells, tags
        self._on_select, self._on_toggle = on_select, on_toggle
        self._iid, self._row, self._open = {}, {}, {}
        self._columns = [key for key, _heading, _chars in columns]
        self.frame = tk.Frame(parent, bg=BG_DARK)
        font = tkfont.Font(root=parent, font=_TREE_FONT)
        unit = max(1, font.measure("0"))
        self._glyph_px = font.measure(GLYPH["on"] + " ")
        widget = ttk.Treeview(self.frame, style=TREE_STYLE, columns=self._columns,
                              show="tree headings", selectmode="browse", height=rows)
        widget.heading("#0", text="Contents", anchor=tk.W)
        widget.column("#0", width=tree_chars * unit, minwidth=14 * unit, stretch=True)
        for key, heading, chars in columns:
            widget.heading(key, text=heading, anchor=tk.W)
            widget.column(key, width=chars * unit, minwidth=8 * unit, stretch=True,
                          anchor=tk.W)
        bar = ttk.Scrollbar(self.frame, orient=tk.VERTICAL, command=widget.yview)
        widget.configure(yscrollcommand=bar.set)
        bar.pack(side=tk.RIGHT, fill=tk.Y)
        widget.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        widget.tag_configure("dim", foreground=FG_DIM)
        widget.tag_configure("warn", foreground=FG_ORANGE)
        widget.tag_configure("head", foreground=FG_WHITE)
        widget.tag_configure("similar", foreground=FG_ACCENT)     # a paired doctrine
        widget.bind("<Button-1>", self._on_press, add="+")
        widget.bind("<Double-Button-1>", self._on_double, add="+")
        widget.bind("<space>", self._on_space, add="+")
        widget.bind("<<TreeviewSelect>>", self._on_picked, add="+")
        self.widget = widget

    # -- the rows -------------------------------------------------------------------
    def iid_of(self, row):
        """The widget's item id of a row id, or ``None``."""
        return self._iid.get(row) if isinstance(row, str) else None

    def row_of(self, iid):
        """The row id behind an item id of the widget, or ``None``."""
        return self._row.get(iid) if isinstance(iid, str) else None

    def label_of(self, row):
        """The label of an extra row; ``None`` for any other row."""
        return self._extra.get(row) if isinstance(row, str) else None

    def _is_open(self, iid, default) -> bool:
        try:
            return bool(self.widget.tk.getboolean(self.widget.item(iid, "open")))
        except (tk.TclError, TypeError, ValueError):
            return default

    def fill(self, tree, extra=()) -> None:
        """Show ``tree``, and after it the ``extra`` rows (``[(row id, label)]``). The rows
        are rebuilt only when the list of row ids changed - otherwise they are repainted
        in place, so a tick never moves the list under the pointer. What was unfolded, what
        was selected and how far the list was scrolled survive a rebuild."""
        widget = self.widget
        extra = [(row, label) for row, label in extra]
        order = ([node.id for node in tree.nodes] if tree is not None else []) \
            + [row for row, _label in extra]
        self.tree, self._extra = tree, dict(extra)
        if order != self.order:
            for row, iid in self._iid.items():
                self._open[row] = self._is_open(iid, False)
            try:
                top = widget.yview()[0]
                widget.delete(*widget.get_children(""))
            except tk.TclError:
                return
            self._iid, self._row, self.order = {}, {}, order
            for index, node in enumerate(tree.nodes if tree is not None else ()):
                iid = f"n{index}"
                self._iid[node.id], self._row[iid] = iid, node.id
                widget.insert(self._iid.get(node.parent, ""), "end", iid=iid,
                              open=self._open.get(node.id, node.kind in _OPEN_KINDS))
            for index, (row, _label) in enumerate(extra):
                iid = f"x{index}"
                self._iid[row], self._row[iid] = iid, row
                widget.insert("", "end", iid=iid)
            self._open = {row: value for row, value in self._open.items() if row in self._iid}
            if self.sel not in self._iid:
                self.sel = None
            else:
                self.select(self.sel)
            try:
                widget.yview_moveto(top)
            except tk.TclError:
                pass
        self.repaint()

    def repaint(self) -> None:
        """Write every row again: its box and label, its cells, its colour."""
        tree, widget = self.tree, self.widget
        try:
            for row, iid in self._iid.items():
                text = self._extra[row] if row in self._extra else tree.text(row)
                widget.item(iid, text=text, values=list(self._cells(row)),
                            tags=list(self._tags(row)))
        except tk.TclError:
            pass                        # the window is already gone

    def shown(self, row):
        """What the widget shows for a row, read back from it: ``(text, [cell, ...])``."""
        iid = self._iid.get(row)
        if iid is None:
            return None
        return (str(self.widget.item(iid, "text")),
                [str(self.widget.set(iid, column)) for column in self._columns])

    def select(self, row) -> bool:
        """Select a row (and scroll it into view). False for a row that is not shown. The
        row also becomes the widget's FOCUS item - the one the arrow keys move from: a
        selection alone leaves them dead until a click, and again after every rebuild."""
        iid = self._iid.get(row) if isinstance(row, str) else None
        if iid is None:
            return False
        self.sel = row
        try:
            if tuple(self.widget.selection()) != (iid,):
                self.widget.selection_set(iid)
            self.widget.focus(iid)
            self.widget.see(iid)
        except tk.TclError:
            pass
        return True

    # -- input ----------------------------------------------------------------------
    def _on_picked(self, _event=None):
        try:
            picked = self.widget.selection()
        except tk.TclError:
            return
        row = self._row.get(picked[0]) if picked else None
        if row is not None and row != self.sel:
            self.sel = row
            self._on_select(row)

    def _on_fold_arrow(self, x, y) -> bool:
        try:
            return "indicator" in str(self.widget.identify_element(x, y))
        except tk.TclError:
            return False

    def _in_box(self, iid, x, y) -> bool:
        """Whether a press at ``(x, y)`` is on the tick box of the row: from where the
        row's text begins - asked of the widget, pixel by pixel - to the end of the glyph."""
        widget = self.widget
        try:
            if widget.identify_region(x, y) != "tree" or self._on_fold_arrow(x, y):
                return False
            box = widget.bbox(iid, "#0")
            if not box:
                return False
            start = None
            for probe in range(int(box[0]), int(x) + 1):
                if "text" in str(widget.identify_element(probe, y)):
                    start = probe
                    break
        except (tk.TclError, TypeError, ValueError):
            return False
        if start is None:
            start = int(box[0]) + _INDICATOR_PX
        return start - 2 <= x <= start + self._glyph_px

    def _on_press(self, event):
        iid = self.widget.identify_row(event.y)
        if iid and self._in_box(iid, event.x, event.y):
            self._on_toggle(self._row.get(iid))
        return None                     # the widget's own binding selects, and folds

    def _on_double(self, event):
        iid = self.widget.identify_row(event.y)
        if not iid or self._on_fold_arrow(event.x, event.y):
            return None
        self._on_toggle(self._row.get(iid))
        return "break"                  # a double-click ticks; it does not fold as well

    def _on_space(self, _event=None):
        if self.sel is not None:
            self._on_toggle(self.sel)
        return "break"


class _Controller:
    """The window's controller, hung on the window as ``win._gc``.

    State (read it, do not write it):

      ``page``         ``create`` | ``import`` | ``history``
      ``mode``         the Create page's switch: ``copy`` | ``granular``
      ``tree``         the ``SelectionTree`` of the page that shows (``None`` on History)
      ``sel``          the id of its selected row (on History: the index of the import)
      ``detail``       the read-only text box of the page that shows
      ``view``         the page's tree widget (``_TreeView``), ``None`` on History
      ``name_var`` / ``notes_var`` / ``tag_var``   the Create page's entries
                       (``name_entry`` / ``tag_entry``: two of their widgets)
      ``footer_var``   "<n> items in <c> categories ..."; ``status_var`` the status line
      ``save_btn`` / ``apply_btn`` / ``later_btn`` / ``signin_btn``; ``buttons`` (by key:
                       each page has its ``Close`` - ``close``, ``close_import``,
                       ``close_history``),
      ``page_buttons``, ``mode_buttons``, ``lines`` (the fixed sentences, by key)
      ``payloads``     the Create draft, ``{item id: payload}``; ``draft`` the bundle it
                       gives - what a save would write; ``unusable`` (see below)
      ``bundle`` / ``read_notes`` / ``file_payloads`` / ``problems`` / ``results`` /
      ``header`` / ``warnings`` / ``notes`` / ``item_modes`` / ``item_conflicts``
                       the Import page: the file, and the plan of it
      ``doctrine_pairs``   ``{record key: doctrine_match.Pair}`` - the file's doctrines that
                       look like the importer's (``group_apply.plan_matches``, ONCE per file)
      ``doctrine_choices`` ``{record key: action}`` - the choices MADE for them (clicked,
                       or pre-set from the remembered ones); a pair without one has its
                       default (``pair_default``). What is sent while its row is ticked
                       (D8): a pair of different names ALWAYS - its choice, else a merge;
                       a pair of the same name only once a choice is made for it
                       (clicked or remembered), else it follows the item's radio
      ``imports`` / ``attention_flags`` / ``pending``    the History page

    Methods: ``show_page(name)``, ``set_mode(mode)``, ``toggle(row)``, ``select(row)``,
    ``refresh()``, ``save_file()``, ``set_field(item_id, key, value)``, ``edit_palette()``,
    ``choose_file(path=None)``, ``set_item_option(item_id, mode=None, conflict=None)``,
    ``set_doctrine_choice(record_key, action)``, ``apply_import(when)``, ``show_details(index)``, ``show_report(report)``,
    ``ask_restore(index=None)``, ``restore(backup_name, when)``, ``restart_now()``,
    ``cancel_pending()``, ``open_attention(index=None)``, ``dismiss(section)``,
    ``first_control()``, ``focus_first()``, ``flush()``, ``close()``;
    for a look at what the tree shows: ``row_ids()``, ``shown(row)``.
    Every one of them returns quietly once the window is gone; ``close()`` answers
    ``True`` (the window is closed) or ``False`` (the user kept it) on every path.

    THE CREATE DRAFT is the export of the files plus what was edited here: ``refresh``
    reads the files again and lays the edits back over them, so pressing it never costs
    typed work; only going back to "copy my current settings" discards them, after a
    question. ``draft`` is rebuilt (``group_items.build_bundle``) at every tick and - behind
    the debounce - after an edit, and everything the page says about the file - the
    manifest, the item texts, the footer - is read from that bundle, never from the ticks.

    WHAT WAITS (``DEBOUNCE_MS``, one ``after`` job): the rebuild of the draft after a typed
    value (the value itself is in ``payloads`` at once), the own-name sweep of the Create
    page - until it has run the footer keeps what the sweep before it found - and the plan
    of the Import page, behind "Checking the file...". ``flush()`` runs what waits at once;
    a save, the switch, a change of page, ``close`` and ``apply_import`` do so themselves,
    so nothing typed is lost and an import is never staged on a stale plan.

    GOING TO SETTINGS: this window stands above the main window and would cover the
    Settings tab it sends the user to. Every road to ``host.open_settings`` therefore
    closes the window first (``close()`` - the polite one) and goes only when that worked.

    WHAT IT LEAVES BEHIND: nothing. Tcl holds the callback of every variable trace until
    the trace is removed, and with it this controller, the files it read and the window;
    ``_release`` removes them all when the window goes (``_on_destroy``, ``abandon``).
    """

    def __init__(self, win, host, *, report=None):
        self.win, self.host = win, host
        self.app_dir = host.app_dir
        self.ctx = None                 # None: the engine's own (the bundled system table)
        self.page = None
        self.mode = "copy"
        self._closed = False
        self._picking = False           # the file question of choose_file is open
        self._job, self._waiting, self._idle = None, set(), None
        self._building = False          # the editor is being filled: its vars edit nothing
        self._meta_traces = []          # (variable, trace name): removed when the window goes

        self.name_var = tk.StringVar(win)
        self.notes_var = tk.StringVar(win)
        self.tag_var = tk.StringVar(win)
        self.footer_var = tk.StringVar(win)
        self.status_var = tk.StringVar(win)
        self.hint_var = tk.StringVar(win, value=MODE_HINTS["copy"])
        self.file_var = tk.StringVar(win)
        self.banner_var = tk.StringVar(win)

        # Create
        self.payloads = {}
        self.draft = group_bundle.Bundle(meta=None, categories={})
        self.unusable = {}
        self._edits = {}                # item id -> {"fields": {key: value}} | {"payload": p}
        self._state = None              # the five files as read last (group_state.load_state)
        self._notes = {}                # export notes, by what they were asked for
        self._loaded = False
        self._hits = self._size = None  # the sweep's result; None: it has not run
        self._swept = (None, None)      # what the sweep before found: shown while one waits
        self.editor = {"kind": None, "item": None}
        self._editor_traces = []

        # Import
        self.bundle = None
        self.read_notes, self.problems, self.results = [], [], None
        self.file_payloads = {}
        self.header, self.warnings, self.notes = [], [], []
        self.item_modes, self.item_conflicts = {}, {}
        self.doctrine_pairs, self.doctrine_choices = {}, {}
        self.option_buttons, self._option_vars, self._option_item = {}, {}, None
        self._option_pair = None        # the record key of the pair the radios are for
        self._option_rows = {}          # group kind -> the grid row of its caption
        self._option_widths = {}        # (kind, value) -> its radio's width on one line
        self._preview, self._extra_rows, self._import_unusable = {}, [], {}
        self._refusal = ""              # why the chosen file was refused (the reader's words)
        self._warn_count = 0            # the lines the warnings box holds
        self._header_count = 1          # ... the Import header box
        self._attention_count = 1       # ... the History attention box

        # History
        self.report = report if isinstance(report, dict) else None
        self._report_showing = self.report is not None
        self._report_seen = False
        self.imports, self.attention_flags, self.pending = [], [], None
        self._backups = set()
        self._history_sel = None
        self._attention_sel = 0

        self.pages, self.page_buttons, self.mode_buttons = {}, {}, {}
        self.buttons, self.lines = {}, {}
        self._views, self._details, self._subjects = {}, {}, {}
        self._wrapped = []
        self._hint_font = tkfont.Font(root=win, font=_HINT_FONT)

    # -- what the next task and the tests read ------------------------------------------
    @property
    def view(self):
        return self._views.get(self.page)

    @property
    def tree(self):
        view = self.view
        return view.tree if view is not None else None

    @property
    def sel(self):
        if self.page == "history":
            return self._history_sel
        view = self.view
        return view.sel if view is not None else None

    @property
    def detail(self):
        return self._details.get(self.page)

    def row_ids(self) -> list:
        """The ids of the rows the tree shows, in display order."""
        view = self.view
        return list(view.order) if view is not None else []

    def shown(self, row):
        """``(text, [cell, ...])`` as the tree widget shows that row, or ``None``."""
        view = self.view
        return view.shown(row) if view is not None else None

    # -- small tools ----------------------------------------------------------------------
    def _ctx(self):
        return self.ctx if self.ctx is not None else group_items.default_ctx()

    def _ask_host(self, name, *args, default=None):
        """``host.<name>(*args)``. A host that raises answers ``default``: the window must
        not die of its host."""
        try:
            return getattr(self.host, name)(*args)
        except Exception as exc:
            log.warning("group window: host.%s failed (%s)", name, type(exc).__name__)
            return default

    def _confirm(self, title, message, buttons, default=None):
        return self._ask_host("confirm", title, message, list(buttons), default=default)

    def _say(self, text="", kind="info") -> None:
        try:
            self.status_var.set(text)
            self._status.configure(fg=_STATUS_COLOURS.get(kind, FG_DIM))
        except tk.TclError:
            pass

    def _write(self, widget, parts, subject=None) -> None:
        """Replace what a read-only text box shows by ``parts`` - ``[(text, tags)]``. A box
        that shows the same ``subject`` as before stays scrolled where it was."""
        try:
            same = subject is not None and self._subjects.get(widget) == subject
            keep = widget.yview()[0] if same else 0.0
            widget.configure(state=tk.NORMAL)   # a delete on a disabled Text does nothing
            widget.delete("1.0", tk.END)
            for text, tags in parts:
                widget.insert(tk.END, text, tags)
            widget.configure(state=tk.DISABLED)
            widget.yview_moveto(keep)
            self._subjects[widget] = subject
        except tk.TclError:
            pass

    def _schedule(self, what) -> None:
        self._waiting.add(what)
        self._cancel_job()
        try:
            self._job = self.win.after(DEBOUNCE_MS, self.flush)
        except tk.TclError:
            self._job = None

    def _cancel_job(self) -> None:
        job, self._job = self._job, None
        if job is not None:
            try:
                self.win.after_cancel(job)
            except (tk.TclError, ValueError):
                pass

    def flush(self) -> None:
        """Run what waits behind the debounce - the rebuild of the draft after a typed
        value, the name sweep, the plan - now."""
        self._cancel_job()
        if self._closed:
            self._waiting = set()
            return
        if "draft" in self._waiting:
            self._draft_changed(rebuild=True)       # it asks for a sweep of its own ...
            self._cancel_job()                      # ... which runs here, not later
        waiting, self._waiting = self._waiting, set()
        if "sweep" in waiting:
            self._run_sweep()
        if "plan" in waiting:
            self._run_plan()

    # -- widgets ----------------------------------------------------------------------------
    def _label(self, parent, text="", *, font=_LABEL_FONT, fg=FG_TEXT, **options):
        return tk.Label(parent, text=text, font=font, fg=fg, bg=BG_DARK, **options)

    def _line(self, parent, key, text, fg):
        """One of the fixed sentences of a page. It wraps to the window (``_on_configure``)
        rather than be cut where the window is narrow."""
        label = self._label(parent, text, font=_HINT_FONT, fg=fg, anchor=tk.W, justify=tk.LEFT)
        self.lines[key] = label
        self._wrapped.append(label)
        return label

    def _entry(self, parent, var, width, check=None):
        entry = tk.Entry(parent, textvariable=var, width=width, font=_LABEL_FONT, bg=BG_ENTRY,
                         fg=FG_WHITE, insertbackground=FG_WHITE, relief=tk.FLAT,
                         highlightthickness=1, highlightbackground=BORDER_COLOR,
                         highlightcolor=FG_ACCENT)
        if check is not None:
            entry.configure(validate="key", validatecommand=(self.win.register(check), "%P"))
        return entry

    def _button(self, parent, key, text, command, style="dark"):
        # ttk pads a short label out to 11 characters; a negative width is a MINIMUM.
        button = ttk.Button(parent, text=text, style=BUTTON_STYLES[style], command=command,
                            width=-max(6, len(text)))
        self.buttons[key] = button
        return button

    def _segment(self, parent, text, command):
        """One button of a segmented control (the page buttons, the switch)."""
        return tk.Button(parent, text=text, command=command, font=_LABEL_FONT, relief=tk.RIDGE,
                         bd=1, padx=12, pady=1, bg=BG_ENTRY, fg=FG_TEXT,
                         activebackground=BG_ENTRY, activeforeground=FG_TEXT, cursor="hand2")

    @staticmethod
    def _paint_segments(buttons, active) -> None:
        for value, button in buttons.items():
            try:
                if value == active:     # the settings category-button style
                    button.configure(relief=tk.SUNKEN, bg=FG_ACCENT, fg=BG_DARK,
                                     activebackground=FG_ACCENT, activeforeground=BG_DARK)
                else:
                    button.configure(relief=tk.RIDGE, bg=BG_ENTRY, fg=FG_TEXT,
                                     activebackground=BG_ENTRY, activeforeground=FG_TEXT)
            except tk.TclError:
                pass

    @staticmethod
    def _wrapping(label):
        """Make a label wrap to the width it is given (pack it with ``fill=tk.X``). With
        ``width=1`` it asks for no width of its own: it never widens its pane."""
        label.configure(width=1, anchor=tk.W, justify=tk.LEFT)
        label.bind("<Configure>", lambda event: label.configure(
            wraplength=max(120, event.width - 4)), add="+")
        return label

    def _check(self, parent, **options):
        return tk.Checkbutton(parent, font=_HINT_FONT, fg=FG_TEXT, bg=BG_DARK,
                              selectcolor=BG_ENTRY, activebackground=BG_DARK,
                              activeforeground=FG_ACCENT, highlightthickness=0, bd=0,
                              **options)

    def _text_box(self, parent, height=_DETAIL_LINES):
        """A read-only, scrolled text box. -> ``(its frame, the Text)``."""
        frame = tk.Frame(parent, bg=BG_DARK)
        bar = ttk.Scrollbar(frame, orient=tk.VERTICAL)
        text = tk.Text(frame, width=_DETAIL_CHARS, height=height, font=_HINT_FONT,
                       bg=BG_PANEL, fg=FG_TEXT, wrap=tk.WORD, relief=tk.FLAT, bd=0,
                       highlightthickness=1, highlightbackground=BORDER_COLOR,
                       highlightcolor=BORDER_COLOR, padx=8, pady=5, cursor="arrow",
                       takefocus=0, state=tk.DISABLED, yscrollcommand=bar.set,
                       selectbackground=BG_SELECT, selectforeground=FG_WHITE,
                       inactiveselectbackground=BG_SELECT)
        bar.configure(command=text.yview)
        bar.pack(side=tk.RIGHT, fill=tk.Y)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        text.tag_configure("title", foreground=FG_ACCENT, font=_HEAD_FONT, spacing3=3)
        text.tag_configure("head", foreground=FG_WHITE, font=_HINT_BOLD, spacing1=7,
                           spacing3=2)
        text.tag_configure("sub", foreground=FG_ACCENT, spacing1=5)
        text.tag_configure("dim", foreground=FG_DIM)
        text.tag_configure("warn", foreground=FG_ORANGE)
        text.tag_configure("label", foreground=FG_DIM)
        text.tag_configure("row")
        text.tag_configure("para", lmargin2=14, spacing1=3)
        # A line of a list that wraps goes on under its own text, not at the box's edge.
        text.tag_configure("hang", lmargin2=4 * max(1, self._hint_font.measure("0")))
        return frame, text

    @staticmethod
    def _paned(page):
        paned = tk.PanedWindow(page, orient=tk.HORIZONTAL, bg=BG_DARK, bd=0, sashwidth=6,
                               sashrelief=tk.FLAT, opaqueresize=True)
        paned.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        return paned

    # -- build ------------------------------------------------------------------------------
    def build(self) -> None:
        win = self.win
        _configure_styles(win)
        outer = tk.Frame(win, bg=BG_DARK, padx=_PAD, pady=10)
        outer.pack(fill=tk.BOTH, expand=True)
        # BOTTOM ROWS ARE PACKED FIRST, here and on every page: when the window is short
        # the tree of the page gives way, never a button row or a line of the footer.
        self._status = self._label(outer, font=_HINT_FONT, fg=FG_DIM, anchor=tk.W,
                                   justify=tk.LEFT, width=1, textvariable=self.status_var)
        self._status.pack(side=tk.BOTTOM, fill=tk.X, pady=(6, 0))
        self._wrapped.append(self._status)
        top = tk.Frame(outer, bg=BG_DARK)
        top.pack(side=tk.TOP, fill=tk.X)
        for name, text in PAGES:
            button = self._segment(top, text, lambda n=name: self.show_page(n))
            button.pack(side=tk.LEFT, padx=(0, 4))
            self.page_buttons[name] = button
        tk.Frame(outer, bg=BORDER_COLOR, height=1).pack(side=tk.TOP, fill=tk.X, pady=(6, 8))
        body = tk.Frame(outer, bg=BG_DARK)
        body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        for name, _text in PAGES:
            self.pages[name] = tk.Frame(body, bg=BG_DARK)
        self._build_create(self.pages["create"])
        self._build_import(self.pages["import"])
        self._build_history(self.pages["history"])
        self.tag_var.set(group_apply.get_tag(self.app_dir))
        # Each trace is kept by name: Tcl holds its callback - this controller - until the
        # trace is removed, whatever becomes of the window (_release).
        for var in (self.name_var, self.notes_var, self.tag_var):
            self._meta_traces.append((var, var.trace_add("write", self._meta_changed)))
        # add="+" on the toplevel, always: make_modal's <Escape> lives there too, and a
        # plain bind() replaces every handler on its sequence.
        win.bind("<MouseWheel>", self._on_wheel, add="+")
        win.bind("<Destroy>", self._on_destroy, add="+")
        win.bind("<Configure>", self._on_configure, add="+")

    def _build_create(self, page) -> None:
        buttons = tk.Frame(page, bg=BG_DARK)
        buttons.pack(side=tk.BOTTOM, fill=tk.X, pady=(8, 0))
        refresh = self._button(buttons, "refresh", "Refresh from my settings", self.refresh)
        refresh.pack(side=tk.LEFT)
        attach_tooltip(refresh, "Read your settings again. What you ticked and edited here "
                                "is kept.")
        # Made in the order Tab walks them - left to right - and packed from the right.
        close = self._button(buttons, "close", "Close", self.close)
        self.save_btn = self._button(buttons, "save", "Save group file...", self.save_file,
                                     "green")
        self.save_btn.pack(side=tk.RIGHT)
        close.pack(side=tk.RIGHT, padx=(0, 8))
        self._line(page, "unsaved", HINT_UNSAVED, FG_DIM).pack(side=tk.BOTTOM, fill=tk.X)
        # The plain text colour: on this page orange means "names found" (the footer).
        self._line(page, "caution", CAUTION, FG_TEXT).pack(side=tk.BOTTOM, fill=tk.X)
        self._footer = self._label(page, font=_HINT_BOLD, fg=FG_WHITE, anchor=tk.W,
                                   justify=tk.LEFT, width=1, textvariable=self.footer_var)
        self._footer.pack(side=tk.BOTTOM, fill=tk.X, pady=(6, 1))
        self._wrapped.append(self._footer)

        meta = tk.Frame(page, bg=BG_DARK)
        meta.pack(side=tk.TOP, fill=tk.X)
        self._label(meta, "Name").grid(row=0, column=0, sticky=tk.W)
        name = self._entry(meta, self.name_var, 20,
                           lambda text: len(text) <= group_bundle.MAX_NAME)
        name.grid(row=0, column=1, sticky=tk.EW, padx=(6, 14))
        self.name_entry = name
        self._label(meta, "Notes").grid(row=0, column=2, sticky=tk.W)
        notes = self._entry(meta, self.notes_var, 20,
                            lambda text: len(text) <= group_bundle.MAX_NOTES_TEXT)
        notes.grid(row=0, column=3, sticky=tk.EW, padx=(6, 14))
        self._label(meta, "Shown as: FCTool by").grid(row=0, column=4, sticky=tk.W)
        self.tag_entry = self._entry(meta, self.tag_var, 12, self.tag_ok)
        self.tag_entry.grid(row=0, column=5, sticky=tk.W, padx=(6, 0))
        attach_tooltip(self.tag_entry, "The group tag: it shows under the FCTool title of "
                                       "everyone who imports this file. Blank = none. "
                                       "Your own tag is not changed.")
        meta.columnconfigure(1, weight=2)
        meta.columnconfigure(3, weight=3)

        switch = tk.Frame(page, bg=BG_DARK)
        switch.pack(side=tk.TOP, fill=tk.X, pady=(8, 0))
        self._label(switch, "Contents:").pack(side=tk.LEFT, padx=(0, 8))
        for mode, text in MODES:
            button = self._segment(switch, text, lambda m=mode: self.set_mode(m))
            button.pack(side=tk.LEFT, padx=(0, 4))
            self.mode_buttons[mode] = button
        self._paint_segments(self.mode_buttons, self.mode)
        hint = self._label(page, font=_HINT_FONT, fg=FG_DIM, anchor=tk.W, justify=tk.LEFT,
                           textvariable=self.hint_var)
        hint.pack(side=tk.TOP, fill=tk.X, pady=(3, 6))
        self.lines["hint"] = hint
        self._wrapped.append(hint)

        paned = self._paned(page)
        view = _TreeView(paned, (("summary", "What it holds", 20),),
                         cells=self._create_cells, tags=self._create_tags,
                         on_select=lambda _row: self._selected(), on_toggle=self.toggle)
        right = tk.Frame(paned, bg=BG_DARK)
        paned.add(view.frame, stretch="always", minsize=240)
        # height=1: the right pane is as tall as the tree beside it (a pane fills its
        # row), but what it ASKS for no longer sets the height of the page - an editor
        # with its list, its hint and the text under it would ask for more than the tree.
        paned.add(right, stretch="always", minsize=200, height=1)
        # The editor of the selected item (Granular editing) shares the pane with the text:
        # while it shows, the pane's spare height is the editor's (_show_editor).
        self.editor_frame = tk.Frame(right, bg=BG_DARK)
        frame, text = self._text_box(right)
        frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self._create_detail_frame, self._create_right = frame, right
        self._views["create"], self._details["create"] = view, text
        right.bind("<Configure>", self._fit_detail, add="+")
        buttons.lift()                  # Tab follows the stacking order: the actions last

    def _build_import(self, page) -> None:
        buttons = tk.Frame(page, bg=BG_DARK)
        buttons.pack(side=tk.BOTTOM, fill=tk.X, pady=(8, 0))
        # Made in the order Tab walks them - Close, later, now - and packed from the right:
        # the button that applies is the LAST stop of the page, never the first.
        close = self._button(buttons, "close_import", "Close", self.close)
        self.later_btn = self._button(buttons, "later", "Apply at next launch",
                                      lambda: self.apply_import("later"))
        self.apply_btn = self._button(buttons, "apply", "Apply and restart",
                                      lambda: self.apply_import("now"), "green")
        self.apply_btn.pack(side=tk.RIGHT)
        self.later_btn.pack(side=tk.RIGHT, padx=(0, 8))
        close.pack(side=tk.RIGHT, padx=(0, 8))
        # What blocks an import can pass (an update finishes): look again when the pointer
        # comes to the buttons - a disabled button cannot be asked by a click.
        buttons.bind("<Enter>", lambda _event: self._paint_apply(quiet=True), add="+")
        self._line(page, "scope", LINE_SCOPE, FG_DIM).pack(side=tk.BOTTOM, fill=tk.X)
        self._line(page, "backup", LINE_BACKUP, FG_TEXT).pack(side=tk.BOTTOM, fill=tk.X,
                                                              pady=(6, 0))
        # Made now, packed (above the backup line) only while an update waits and a file is
        # chosen (_paint_update_notice).
        self._line(page, "update_first", MSG_UPDATE_FIRST, FG_ORANGE)
        # The warnings box: packed only while it has something to say (_paint_import), and
        # as tall as that is. Its headings have no spacing of their own, so that the box
        # holds WHOLE lines; where a line wraps, the box is measured again (_size_warnings).
        self._warn_frame, self.warn_text = self._text_box(page, height=_WARN_LINES[0])
        self.warn_text.tag_configure("head", spacing1=0, spacing3=0)
        self.warn_text.bind("<Configure>", self._size_warnings, add="+")
        page.bind("<Configure>", self._size_warnings, add="+")

        # The file: the button, and beside it what the chosen file says about itself.
        row = tk.Frame(page, bg=BG_DARK)
        row.pack(side=tk.TOP, fill=tk.X, pady=(0, 6))
        self._button(row, "choose", "Choose file...", self.choose_file).pack(
            side=tk.LEFT, anchor=tk.N, padx=(0, 8))
        frame, self.header_text = self._text_box(row, height=_HEADER_LINES[1])
        frame.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.header_text.tag_configure("title", spacing3=0)     # whole lines (_size_header)
        self.header_text.tag_configure("accent", foreground=FG_ACCENT)  # the paired doctrines
        self.header_text.bind("<Configure>", self._size_header, add="+")

        paned = self._paned(page)
        view = _TreeView(paned, (("in_file", HEAD_IN_FILE, 18), ("happens", HEAD_HAPPENS, 22)),
                         cells=self._import_cells, tags=self._import_tags, tree_chars=26,
                         rows=_IMPORT_TREE_ROWS,
                         on_select=lambda _row: self._selected(), on_toggle=self.toggle)
        right = tk.Frame(paned, bg=BG_DARK)
        paned.add(view.frame, stretch="always", minsize=280)
        # height=1, as on the Create page: the tree's side sets what the page asks for.
        paned.add(right, stretch="always", minsize=200, height=1)
        # The choices of the selected item (how a list is merged, who wins a name): side
        # by side where the pane is wide enough, else one under the other.
        self._options_frame = tk.Frame(right, bg=BG_DARK)
        # A spare column right of the item's radios takes what is left - and what the
        # doctrine's own row (spanning it) is wider by - so those radios never shift.
        self._options_frame.grid_columnconfigure(2, weight=1)
        self._options_frame.bind("<Configure>",
                                 lambda event: self._place_options(event.width), add="+")
        frame, text = self._text_box(right)
        frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self._import_detail_frame = frame
        self._views["import"], self._details["import"] = view, text
        buttons.lift()                  # Tab follows the stacking order: the actions last
        self._paint_import()

    def _build_history(self, page) -> None:
        banner = tk.Frame(page, bg=BG_PANEL, highlightthickness=1,
                          highlightbackground=FG_ORANGE, highlightcolor=FG_ORANGE,
                          padx=8, pady=5)
        # Made in the order Tab walks them - left to right - and packed from the right.
        restart = self._button(banner, "restart", "Restart now", self.restart_now, "green")
        self._button(banner, "cancel_pending", CANCEL_PENDING.format(what="import"),
                     self.cancel_pending).pack(side=tk.RIGHT)
        restart.pack(side=tk.RIGHT, padx=(8, 8))
        self._wrapping(tk.Label(banner, textvariable=self.banner_var, font=_HINT_FONT,
                                fg=FG_WHITE, bg=BG_PANEL)).pack(side=tk.LEFT, fill=tk.X,
                                                                expand=True)
        # Under the text and the buttons while an update waits (_paint_history).
        self.banner_notice = self._wrapping(tk.Label(
            banner, text=MSG_UPDATE_FIRST, font=_HINT_FONT, fg=FG_ORANGE, bg=BG_PANEL))
        self.banner = banner            # packed above the lists while something is staged

        paned = self._paned(page)
        self._history_paned = paned
        left = tk.Frame(paned, bg=BG_DARK)
        right = tk.Frame(paned, bg=BG_DARK)
        unit = max(1, tkfont.Font(root=self.win, font=_LABEL_FONT).measure("0"))
        paned.add(left, stretch="always", minsize=62 * unit)    # its two buttons, side by side
        paned.add(right, stretch="always", minsize=200)

        attention_row = row = tk.Frame(left, bg=BG_DARK)
        row.pack(side=tk.BOTTOM, fill=tk.X, pady=(4, 0))
        self._button(row, "open_settings", "Open in Settings",
                     self.open_attention).pack(side=tk.LEFT, padx=(0, 6))
        self._button(row, "dismiss", "Dismiss", self._dismiss_selected).pack(side=tk.LEFT)
        # One paragraph per flag, wrapped: a reason is a whole sentence, and a list box
        # would cut it at the pane's edge. A click picks the paragraph.
        frame, self.attention_text = self._text_box(left, height=_ATTENTION_LINES[1])
        frame.pack(side=tk.BOTTOM, fill=tk.X)
        # No spacing between paragraphs: the box holds WHOLE lines (_size_attention).
        self.attention_text.tag_configure("flag", foreground=FG_ORANGE, lmargin2=14,
                                          spacing1=0, spacing3=0)
        self.attention_text.bind("<Configure>", self._size_attention, add="+")
        self.attention_text.tag_configure("picked", background=BG_SELECT,
                                          foreground=FG_WHITE)
        self.attention_text.bind("<Button-1>", self._on_attention_press, add="+")
        self._label(left, "Needs your attention", font=_HEAD_FONT,
                    fg=FG_ACCENT).pack(side=tk.BOTTOM, anchor=tk.W, pady=(10, 2))
        details_row = row = tk.Frame(left, bg=BG_DARK)
        row.pack(side=tk.BOTTOM, fill=tk.X, pady=(4, 0))
        self._button(row, "details", "Details", self.show_details).pack(side=tk.LEFT,
                                                                        padx=(0, 6))
        self._button(row, "restore", "Restore the settings from before this import...",
                     self.ask_restore).pack(side=tk.LEFT)
        self._label(left, "Imports", font=_HEAD_FONT, fg=FG_ACCENT).pack(
            side=tk.TOP, anchor=tk.W, pady=(0, 2))
        wrap = tk.Frame(left, bg=BG_DARK)
        wrap.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        columns = (("when", "When (UTC)", 17), ("name", "Group file", 16),
                   ("revision", "Rev", 5), ("result", "Result", 18))
        listed = ttk.Treeview(wrap, style=TREE_STYLE, columns=[key for key, _h, _c in columns],
                              show="headings", selectmode="browse", height=4)
        unit = max(1, self._hint_font.measure("0"))
        for key, heading, chars in columns:
            listed.heading(key, text=heading, anchor=tk.W)
            listed.column(key, width=chars * unit, minwidth=4 * unit, anchor=tk.W,
                          stretch=key in ("name", "result"))
        bar = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=listed.yview)
        listed.configure(yscrollcommand=bar.set)
        bar.pack(side=tk.RIGHT, fill=tk.Y)
        listed.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        listed.bind("<<TreeviewSelect>>", self._history_picked, add="+")
        self.history_list = listed

        # Under the text: the next step (packed while it is offered) and Close.
        row = tk.Frame(right, bg=BG_DARK)
        row.pack(side=tk.BOTTOM, fill=tk.X, pady=(4, 0))
        self.signin_btn = self._button(row, "signin", "Sign in your characters",
                                       self._sign_in, "green")
        self._button(row, "close_history", "Close", self.close).pack(side=tk.RIGHT)
        frame, text = self._text_box(right)
        frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self._history_detail_frame = frame
        self._details["history"] = text
        # Tab follows the stacking order: the list, its two buttons, the attention list's
        # two, the next step and Close - and the banner, whose buttons act, last.
        details_row.lift()
        attention_row.lift()
        banner.lift()

    # -- the window ---------------------------------------------------------------------------
    def show_page(self, name) -> bool:
        """Show one of the three pages. The Create page reads the files the first time it
        shows; the History page reads its side file every time."""
        if self._closed or name not in self.pages:
            return False
        self.flush()                    # a value typed on the page that goes is in its draft
        for page, frame in self.pages.items():
            if page != name:
                frame.pack_forget()
        self.pages[name].pack(fill=tk.BOTH, expand=True)
        changed, self.page = self.page != name, name
        self._paint_segments(self.page_buttons, name)
        self._say("")
        if name == "create":
            if not self._loaded:
                self.refresh()
            else:
                self._say_unusable()
        elif name == "import":
            self._paint_apply()
        else:
            self._load_history()
        if changed and self._focus_lost():
            self.focus_first()          # the keyboard is never left on a page that is gone
        return True

    def _focus_lost(self) -> bool:
        """Whether the widget that holds the keyboard in this window is none at all, or one
        of a page that does not show. (A page button that was reached with Tab keeps it.)"""
        try:
            held = self.win.focus_lastfor()
        except (tk.TclError, KeyError):
            return True
        if held is None or held is self.win:
            return True
        path = str(held)
        return any(path.startswith(str(frame) + ".")
                   for name, frame in self.pages.items() if name != self.page)

    def first_control(self):
        """Where the keyboard starts on the page that shows: the name on Create, ``Choose
        file...`` on Import, the list of imports on History (``Close`` while there is
        none). Never a button that applies something."""
        if self.page == "create":
            return self.name_entry
        if self.page == "import":
            return self.buttons["choose"]
        return self.history_list if self.imports else self.buttons["close_history"]

    def focus_first(self) -> None:
        """Give the keyboard to ``first_control()``."""
        if self._closed:
            return
        try:
            self.first_control().focus_set()
        except tk.TclError:
            pass

    def toggle(self, row) -> str:
        """Tick or untick a row of the page's tree, with everything under it. -> its state
        afterwards (``on`` | ``off`` | ``partial``). A row without a box is left alone."""
        view = self.view
        tree = view.tree if view is not None else None
        if self._closed or tree is None:
            return "off"
        node = tree.node(row)
        if node is None or not node.checkable:
            return tree.state(row)
        state = tree.toggle(row)
        self._ticks_changed()
        return state

    def _ticks_changed(self) -> None:
        if self.page == "create":
            self._draft_changed()
        else:
            self._views["import"].repaint()
            self._replan()

    def select(self, row) -> bool:
        """Select a row of the page's tree and show it in the right pane."""
        view = self.view
        if self._closed or view is None or not view.select(row):
            return False
        self._selected()
        return True

    def _selected(self) -> None:
        if self.page == "create":
            self._show_editor()
            self._render_create()
        elif self.page == "import":
            self._show_options()
            self._render_import()

    @staticmethod
    def _item_of(view, row):
        """The id of the item a row belongs to - the row itself, or the item above a field
        or record row - or ``None``."""
        tree = view.tree
        node = tree.node(row) if tree is not None else None
        if node is None:
            return None
        if node.kind == "leaf":
            return node.parent
        return node.id if node.kind == "item" and node.id != TAG_ID else None

    def _on_wheel(self, _event=None):
        # A tree, a list or a text box under the pointer has scrolled already (their class
        # bindings run before the toplevel's). "break" keeps the wheel inside this window:
        # the app's own router would scroll the tab behind it as well.
        return "break"

    def _on_configure(self, event) -> None:
        if event.widget is not self.win:
            return
        width = max(240, event.width - 2 * _PAD - 4)
        for label in self._wrapped:
            try:
                label.configure(wraplength=width)
            except tk.TclError:
                pass

    def _on_destroy(self, event) -> None:
        # <Destroy> arrives here for every child too; only the window's own counts.
        if event.widget is self.win:
            self._closed = True
            self._cancel_job()
            self._cancel_idle()
            self._release()

    def _release(self) -> None:
        """Remove every variable trace this controller made - the three of the Create
        page's entries and those of an editor that is still open. Tcl holds a trace's
        callback until the trace is removed, and the callback holds this controller: left
        in place, each window that was ever opened would stay in memory with everything it
        read. Safe to call twice, and after the window is gone."""
        traces = self._meta_traces + self._editor_traces
        self._meta_traces, self._editor_traces = [], []
        for var, name in traces:
            try:
                var.trace_remove("write", name)
            except (tk.TclError, ValueError, RuntimeError):
                pass

    def _cancel_idle(self) -> None:
        idle, self._idle = self._idle, None
        if idle is not None:
            try:
                self.win.after_cancel(idle)
            except (tk.TclError, ValueError):
                pass

    def _edited(self) -> bool:
        """Whether the Create draft holds something a rebuild from the files would lose: an
        edited value, or an item of which single entries were picked."""
        tree = self._views["create"].tree
        return bool(self._edits) or bool(tree is not None and tree.partial_items())

    def close(self) -> bool:
        """Close the window - after a question when the Create draft was edited. The Close
        button, Escape and the window X are this one road."""
        if self._closed:
            return True
        buttons = [("Discard", True), ("Keep editing", None)]
        if self._edited():
            self.flush()                # the page behind the question shows what was typed
            if self._confirm("Discard edits", MSG_DISCARD_CLOSE, buttons,
                             default=True) is not True:
                return False
        self.abandon()
        return True

    def abandon(self) -> None:
        """Destroy the window without a question."""
        self._closed = True
        self._cancel_job()
        self._cancel_idle()
        self._release()
        try:
            self.win.destroy()
        except tk.TclError:
            pass

    # -- Create: the draft ----------------------------------------------------------------------
    def refresh(self) -> None:
        """Read the files again (after the host has flushed its config) and rebuild the
        Create page from them. Ticks are kept, and so is what was edited here."""
        if self._closed:
            return
        self._ask_host("flush_config")
        try:
            state = group_state.load_state(self.app_dir)
        except Exception as exc:
            log.warning("group window: the settings could not be read (%s)",
                        type(exc).__name__)
            state = None
        self._state, self._notes, self._loaded = state, {}, True
        self._load_draft()
        if state is None or not isinstance(state.config, dict):
            self._say(MSG_UNREADABLE, "warn")

    def _load_draft(self) -> None:
        """The draft: what the files export, with the edits laid over it."""
        payloads = {}
        if self._state is not None:
            try:
                payloads = group_items.export_all(self._state, self._ctx())
            except Exception as exc:
                log.warning("group window: the export failed (%s)", type(exc).__name__)
        for item_id, edit in list(self._edits.items()):
            payload = payloads.get(item_id)
            if payload is None:
                del self._edits[item_id]            # the item has nothing to share any more
            elif "payload" in edit:
                payloads[item_id] = deepcopy(edit["payload"])
            elif isinstance(payload, dict):
                for key, value in edit.get("fields", {}).items():
                    if key in payload:
                        payload[key] = deepcopy(value)
        self.payloads = payloads
        self._draft_changed(rebuild=True, editor=True)

    def _meta(self, bundle_id="", revision=1):
        return group_bundle.BundleMeta(
            id=bundle_id, revision=revision, name=clean_text(self.name_var.get()),
            author="",                  # never a name: the file says what, not who
            notes=clean_text(self.notes_var.get(), group_bundle.MAX_NOTES_TEXT),
            created=group_state.utc_now_iso(),
            app_version=clean_text(self.host.app_version, 40),
            tag=clean_tag(self.tag_var.get()))

    def _draft_changed(self, rebuild=False, editor=False) -> None:
        """A tick or a value of the draft changed: build the bundle it gives ONCE and
        repaint what reads it. ``rebuild``: the rows changed too (the depth, a value, a
        refresh); ``editor``: the values under the editor changed, build it again."""
        view = self._views["create"]
        tree = view.tree
        if rebuild or tree is None:
            self._waiting.discard("draft")          # this IS the rebuild that waited
            nodes = build_nodes(self.payloads, depth="leaves" if self.mode == "granular"
                                else "items")
            tree = tree.carry_over(nodes) if tree is not None else SelectionTree(nodes)
        self.unusable = unusable_selection(tree, self.payloads) \
            if self.mode == "granular" else {}
        options = options_from_tree(tree, self.payloads)
        try:
            self.draft = group_items.build_bundle(self.payloads, options, self._meta())
        except Exception as exc:
            log.warning("group window: the draft could not be built (%s)", type(exc).__name__)
            self.draft = group_bundle.Bundle(meta=self._meta(), categories={})
        view.fill(tree)
        if view.sel is None:
            view.select(ROOT_ID)
        self._hits = self._size = None
        self._paint_footer()
        self._paint_save()
        self._schedule("sweep")
        self._show_editor(force=editor)
        self._render_create()
        self._say_unusable()

    def _say_unusable(self) -> None:
        for item_id, why in self.unusable.items():
            item = _item(item_id)
            self._say(f"{_label(item) if item is not None else item_id}: {why}", "warn")
            return
        self._say("")

    def _meta_changed(self, *_args) -> None:
        """The name, the notes or the tag was typed in. Nothing of the file is built per
        keystroke: the sweep (and with it the size) waits behind the debounce."""
        if self._closed or not self._loaded:
            return
        self._hits = self._size = None
        self._paint_footer()
        self._paint_save()
        self._schedule("sweep")

    def _run_sweep(self) -> None:
        """The own-name sweep, on the text of the file as it would be written."""
        bundle = group_bundle.Bundle(meta=self._meta(), categories=self.draft.categories)
        try:
            text = group_bundle.dumps(bundle)
        except Exception:
            text = ""
        if text:
            names = self._ask_host("own_names", default=[])
            self._hits = find_names(text, names)
            self._size = _file_bytes(text)
        self._swept = (self._hits, self._size)
        self._paint_footer()
        view = self._views["create"]
        node = view.tree.node(view.sel) if view.tree is not None else None
        if node is not None and node.kind == "root":
            self._render_create()       # the manifest starts with the tag that was typed

    def _paint_footer(self) -> None:
        """The footer. Its count is the draft's, at once. Its names and its size are the
        sweep's: while a sweep waits (``_hits`` and ``_size`` are ``None``) the footer
        keeps what the sweep before it found, rather than drop half its text for the
        length of the debounce at every tick and every keystroke."""
        hits, size = self._hits, self._size
        if hits is None and size is None:
            hits, size = self._swept
        try:
            self.footer_var.set(footer_text(self.draft, size_bytes=size, name_hits=hits))
            self._footer.configure(fg=FG_ORANGE if hits else FG_WHITE)
        except tk.TclError:
            pass

    def _can_save(self) -> bool:
        return bool(self._loaded and clean_text(self.name_var.get())
                    and _item_count(self.draft))

    def _paint_save(self) -> None:
        try:
            self.save_btn.configure(state=tk.NORMAL if self._can_save() else tk.DISABLED)
        except tk.TclError:
            pass

    def _create_cells(self, row):
        """What a row of the Create tree holds. A row that is ticked IN PART says what of
        it is in the draft - what will be saved - not what the setup has: an item, the
        summary of its part; a category, how many of its items."""
        if row in self.unusable:
            return (self.unusable[row],)
        tree = self._views["create"].tree
        node = tree.node(row)
        if node is None:
            return ("",)
        if tree.state(row) == "partial":
            if node.kind == "item":
                item = _item(row)
                entries = self.draft.categories.get(item.category) if item is not None else None
                if isinstance(entries, dict) and item.key in entries:
                    return (_summary(item, entries[item.key]) or node.summary,)
            elif node.kind == "category":
                entries = self.draft.categories.get(row[len(CATEGORY_PREFIX):])
                saved = len(entries) if isinstance(entries, dict) else 0
                shared = sum(1 for child in tree.children(row) if tree.node(child).checkable)
                if saved != shared:     # (all of them, some in part: the count stands)
                    return (f"{saved} of {node.summary}",)
        return (node.summary,)

    @staticmethod
    def _row_tags(tree, row, unusable) -> tuple:
        """The colour of a row: dim without a box and when it is not ticked, the warning
        colour when its ticks cannot travel, white for the root and the categories."""
        node = tree.node(row) if tree is not None else None
        if node is None or not node.checkable or tree.state(row) == "off":
            return ("dim",)
        if row in unusable:
            return ("warn",)
        return ("head",) if node.kind in _OPEN_KINDS else ()

    def _create_tags(self, row):
        return self._row_tags(self._views["create"].tree, row, self.unusable)

    def set_mode(self, mode) -> bool:
        """The switch: ``copy`` (my current settings, whole items) or ``granular`` (single
        fields and records, values editable). Going back to ``copy`` asks first when a
        value was edited or single entries of an item were picked - both are lost."""
        if self._closed or mode not in dict(MODES):
            return False
        if mode == self.mode:
            return True
        self.flush()                    # a value that was just typed is in the draft first
        discard = mode == "copy" and self._edited()
        if discard and self._confirm("Discard edits", MSG_DISCARD,
                                     [("Discard", True), ("Keep editing", None)]) is not True:
            return False
        self.mode = mode
        self.hint_var.set(MODE_HINTS[mode])
        self._paint_segments(self.mode_buttons, mode)
        if discard:
            self._edits = {}
            self._load_draft()
        else:
            self._draft_changed(rebuild=True)
        return True

    # -- Create: what the right pane says ---------------------------------------------------
    def _export_notes(self, items=None) -> list:
        """What the export leaves out - of everything, of one item (its id) or of several."""
        key = items if items is None or isinstance(items, str) else tuple(items)
        if key not in self._notes:
            wanted = None if items is None else [items] if isinstance(items, str) \
                else list(items)
            self._notes[key] = export_note_lines(self._state, self._ctx(), wanted) \
                if self._state is not None else []
        return self._notes[key]

    def _detail_parts(self, widget, rows, saved_head) -> list:
        """``detail_lines`` rows as text parts, under their headings. The values of the
        first section stand in a column of their own, set by the longest label - of those
        that are short: a label of more than ``_LABEL_COLUMN`` characters gets a line to
        itself and its value the next one, so one long label never squeezes every value
        into a sliver."""
        heads = {"saved": saved_head, "never": HEAD_NEVER, "left_out": HEAD_LEFT_OUT}
        unit = max(1, self._hint_font.measure("0"))
        limit = (_LABEL_COLUMN + 2) * unit
        widths = [self._hint_font.measure("  " + label)
                  for section, label, value in rows if section == "saved" and value]
        tab = max([width for width in widths if width <= limit] or [6 * unit]) + 2 * unit
        try:
            widget.tag_configure("row", tabs=(tab,), lmargin2=tab)
        except tk.TclError:
            pass
        parts, current = [], None
        for section, label, value in rows:
            if section != current:
                parts.append((heads.get(section, "") + "\n", "head"))
                current = section
            if section != "saved":
                parts.append((f"  {label}\n",
                              ("warn" if section == "left_out" else "dim", "hang")))
            elif not value:
                parts.append((f"  {label}\n", "row"))
            elif self._hint_font.measure("  " + label) > limit:
                parts.append((f"  {label}\n", ("row", "label")))
                parts.append((f"\t{value}\n", "row"))
            else:
                parts.append((f"  {label}", ("row", "label")))
                parts.append((f"\t{value}\n", "row"))
        return parts

    @staticmethod
    def _manifest_parts(lines) -> list:
        return [(line + "\n", "" if line.startswith(" ") else "sub") for line in lines]

    def _never_lines(self, category=None) -> list:
        """What the items IN THE FILE (of one category, or of all) never save, as
        ``"<item>: <what>"`` - the lines each item's own page shows under "Never saved"."""
        lines = []
        for item in _registry():
            entries = self.draft.categories.get(item.category)
            if category not in (None, item.category) or not isinstance(entries, dict) \
                    or item.key not in entries:
                continue
            lines += [f"{_label(item)}: {line}"
                      for line in _texts(getattr(item, "never_saved", ()), MAX_SHOWN)]
        return lines

    def _render_create(self) -> None:
        view, text = self._views["create"], self._details["create"]
        node = view.tree.node(view.sel) if view.tree is not None else None
        if node is None:
            self._write(text, [])
            return
        if node.kind in _OPEN_KINDS:                        # the root, a category
            lines = manifest_lines(group_bundle.Bundle(meta=self._meta(),
                                                       categories=self.draft.categories))
            notes, never = self._export_notes(), [NEVER_ANYWHERE] + self._never_lines()
            if node.kind == "category":
                lines = _category_lines(lines, node.label)
                notes = self._export_notes(view.tree.children(node.id))
                never = self._never_lines(node.id[len(CATEGORY_PREFIX):])
            # What is NOT in the file comes first: under a manifest of a hundred lines -
            # which opens with nine of palette colours - nobody would find it.
            parts = [(node.label + "\n", "title")]
            if notes:
                parts.append((HEAD_LEFT_OUT + "\n", "head"))
                parts += [(f"  {line}\n", ("warn", "hang")) for line in notes]
            if never:
                parts.append((HEAD_NEVER + "\n", "head"))
                parts += [(f"  {line}\n", ("dim", "hang")) for line in never]
            parts.append((HEAD_SAVED + "\n", "head"))
            parts += self._manifest_parts(lines) or [
                ("  (nothing is ticked here - nothing of it goes into the file)\n", "warn")]
        elif node.kind == "info":
            parts = [(node.label + "\n", "title"), (f"({node.summary})\n", "dim")]
        else:
            parts = self._create_item_parts(self._item_of(view, node.id), text)
        self._write(text, parts, subject=("create", view.sel))

    def _create_item_parts(self, item_id, widget) -> list:
        """One item: what of it is IN THE FILE (read from the bundle that would be
        written), what it never saves, what the export left out of it."""
        item = _item(item_id)
        if item is None:
            return []
        parts = [(f"{_category_label(item.category)} > {_label(item)}\n", "title")]
        whole = _payload(self.payloads, item_id)
        entries = self.draft.categories.get(item.category)
        in_file = entries.get(item.key, ABSENT) if isinstance(entries, dict) else ABSENT
        notes = self._export_notes(item_id)
        if whole is not ABSENT and in_file is ABSENT:       # it has something; none is ticked
            parts += [(HEAD_SAVED + "\n", "head"),
                      (f"  ({self.unusable.get(item_id, MSG_ITEM_OFF)})\n", "warn")]
            return parts + self._detail_parts(widget, detail_lines(item_id, whole, notes=notes),
                                              "If it is ticked")
        return parts + self._detail_parts(widget, detail_lines(item_id, in_file, notes=notes),
                                          HEAD_SAVED)

    # -- Create: granular editing ---------------------------------------------------------------
    def _drop_editor(self) -> None:
        for var, name in self._editor_traces:
            try:
                var.trace_remove("write", name)
            except (tk.TclError, ValueError):
                pass
        self._editor_traces = []
        for child in self.editor_frame.winfo_children():
            child.destroy()
        self.editor = {"kind": None, "item": None}

    def _show_editor(self, force=False) -> None:
        """Give the selected item its editor (Granular editing only), or take the editor
        away. An editor that already shows the item is only brought up to date - it is
        never rebuilt under the caret."""
        view = self._views["create"]
        item_id = self._item_of(view, view.sel) if self.mode == "granular" else None
        if item_id is not None and _payload(self.payloads, item_id) is ABSENT:
            item_id = None
        try:
            if not force and item_id == self.editor["item"]:
                self._sync_editor()
                return
            self._drop_editor()
            detail, text = self._create_detail_frame, self._details["create"]
            if item_id is None:
                self.editor_frame.pack_forget()
                text.configure(height=_DETAIL_LINES)
                detail.pack_configure(side=tk.TOP, fill=tk.BOTH, expand=True)
                return
            self._building = True
            try:
                self._build_editor(_item(item_id), view.tree)
            finally:
                self._building = False
            # The pane's spare height is the EDITOR's: the text under it keeps about five
            # lines (it scrolls), instead of taking half the pane from the fields.
            self._fit_detail()
            detail.pack_configure(side=tk.BOTTOM, fill=tk.X, expand=False)
            self.editor_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(0, 6),
                                   before=detail)
        except tk.TclError:
            pass

    def _fit_detail(self, _event=None) -> None:
        """Under an editor the text box keeps five lines - three while the pane is short
        (the window at its smallest), where five would leave the fields two rows. Looked
        at again whenever the pane's size changes (its ``<Configure>``)."""
        if self.editor["kind"] is None:
            return
        low, high = _EDITOR_DETAIL_LINES
        try:
            roomy = self._create_right.winfo_height() \
                >= _ROOMY_PANE * self._hint_font.metrics("linespace")
            text = self._details["create"]
            if int(text.cget("height")) != (high if roomy else low):
                text.configure(height=high if roomy else low)
        except (tk.TclError, TypeError, ValueError):
            pass

    def _build_editor(self, item, tree) -> None:
        frame = self.editor_frame
        head = tk.Frame(frame, bg=BG_DARK)
        head.pack(side=tk.TOP, fill=tk.X)
        self._label(head, f"Edit: {_label(item)}", font=_HEAD_FONT, fg=FG_ACCENT,
                    anchor=tk.W, width=1).pack(side=tk.LEFT, fill=tk.X, expand=True)
        payload = self.payloads[item.id]
        fields = [fld for fld in getattr(item, "fields", ())
                  if isinstance(payload, dict) and getattr(fld, "key", None) in payload
                  and f"{item.id}{LEAF_SEP}{fld.key}" in tree]
        editor = {"item": item.id, "rows": list(tree.children(item.id))}
        if item.id == PALETTE_ITEM:
            editor["kind"] = "palette"
            # The editor's buttons come and go with it: they are not in ``buttons``.
            editor["palette"] = ttk.Button(frame, text="Edit colours...",
                                           style=BUTTON_STYLES["dark"],
                                           command=self.edit_palette)
            editor["palette"].pack(side=tk.TOP, anchor=tk.W, pady=(6, 4))
            self._wrapping(self._label(frame, EDIT_PALETTE, font=_HINT_FONT, fg=FG_DIM)).pack(
                side=tk.TOP, fill=tk.X)
        elif fields:
            editor["kind"] = "fields"
            editor["fields"] = self._build_fields(frame, item, fields, payload, tree)
        else:
            editor["kind"] = "records"
            for key, text, on in (("none", "None", False), ("all", "All", True)):
                editor[key] = ttk.Button(head, text=text, style=BUTTON_STYLES["dark"], width=-5,
                                         command=lambda o=on: self._set_all(item.id, o))
                editor[key].pack(side=tk.RIGHT, padx=(6, 0))
            # Records - doctrines, templates, the parts of a value - are ticked here, not
            # typed: where they are changed is said for every item of this kind.
            editor["hint"] = self._wrapping(self._label(frame, EDIT_ELSEWHERE,
                                                        font=_HINT_FONT, fg=FG_DIM))
            editor["hint"].pack(side=tk.BOTTOM, fill=tk.X)
            wrap = tk.Frame(frame, bg=BG_DARK)
            wrap.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(4, 0))
            box = tk.Listbox(wrap, height=3, width=1, font=_HINT_FONT, bg=BG_ENTRY,
                             fg=FG_TEXT, selectbackground=BG_ENTRY, selectforeground=FG_TEXT,
                             borderwidth=1, relief=tk.RIDGE, highlightthickness=0,
                             activestyle="none", exportselection=False)
            bar = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=box.yview)
            box.configure(yscrollcommand=bar.set)
            bar.pack(side=tk.RIGHT, fill=tk.Y)
            box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            box.bind("<Button-1>", self._on_record_press, add="+")
            editor["list"] = box
        self.editor = editor
        self._sync_editor()

    def _build_fields(self, frame, item, fields, payload, tree) -> dict:
        """One row per field: a box (is it in the file), its label, an editor of its kind
        and - under them - what the engine says about the value. The rows are one grid in a
        scrolled canvas: as wide as the pane, and scrolling when the pane is short. The
        label column has a width of its OWN - ``_FIELD_LABEL_SHARE`` of the pane, at most
        ``_FIELD_LABEL_CHARS`` characters - and a label wraps inside it; the value column
        takes the rest, so a long label never pushes into the values."""
        wrap = tk.Frame(frame, bg=BG_DARK)
        wrap.pack(side=tk.TOP, fill=tk.BOTH, expand=True, pady=(4, 0))
        bar = ttk.Scrollbar(wrap, orient=tk.VERTICAL)
        # width / height 1: left alone, a Canvas asks for 10 cm by 7 cm.
        canvas = tk.Canvas(wrap, bg=BG_DARK, highlightthickness=0, bd=0, width=1, height=1,
                           yscrollcommand=bar.set)
        bar.configure(command=canvas.yview)
        bar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        grid = tk.Frame(canvas, bg=BG_DARK)
        window = canvas.create_window(0, 0, anchor=tk.NW, window=grid)
        grid.columnconfigure(2, weight=1)
        unit = max(1, self._hint_font.measure("0"))
        label_px = _FIELD_LABEL_CHARS * unit
        grid.columnconfigure(1, minsize=label_px + 8)
        hints, labels = [], []

        def fit(event):                 # the grid is as wide as the pane gives
            canvas.itemconfigure(window, width=event.width)
            column = max(_FIELD_LABEL_MIN_CHARS * unit,
                         min(label_px, int(event.width * _FIELD_LABEL_SHARE)))
            grid.columnconfigure(1, minsize=column + 8)
            for label in labels:
                label.configure(wraplength=column)
            for hint in hints:
                hint.configure(wraplength=max(120, event.width - 40))

        def scroll(event):              # the rows are children: the wheel is theirs to pass
            if canvas.yview() != (0.0, 1.0):
                canvas.yview_scroll(-1 if getattr(event, "delta", 0) > 0 else 1, "units")
            return "break"

        canvas.bind("<Configure>", fit, add="+")
        grid.bind("<Configure>", lambda event: canvas.configure(
            scrollregion=(0, 0, event.width, event.height)), add="+")
        built = {}
        for index, fld in enumerate(fields):
            leaf = f"{item.id}{LEAF_SEP}{fld.key}"
            included = tk.BooleanVar(self.win, value=tree.state(leaf) == "on")
            box = self._check(grid, variable=included, command=lambda r=leaf: self.toggle(r))
            box.grid(row=2 * index, column=0, sticky=tk.NW, pady=(2, 0))
            label = self._label(grid, _show(fld.label, MAX_LABEL), font=_HINT_FONT,
                                anchor=tk.W, justify=tk.LEFT, wraplength=label_px)
            label.grid(row=2 * index, column=1, sticky=tk.NW, padx=(0, 8), pady=(4, 0))
            labels.append(label)
            widget, var = self._field_editor(grid, item, fld, payload[fld.key])
            stretches = widget.winfo_class() in ("Entry", "TCombobox", "Text")
            # At the TOP of its row: beside the first line of a label that wraps.
            widget.grid(row=2 * index, column=2, sticky="new" if stretches else tk.NW,
                        pady=(3, 1))
            hint = self._label(grid, font=_HINT_FONT, fg=FG_ORANGE, anchor=tk.W,
                               justify=tk.LEFT, wraplength=260)
            hint.grid(row=2 * index + 1, column=1, columnspan=2, sticky=tk.W)
            hint.grid_remove()
            hints.append(hint)
            for child in (box, label, hint) + (() if fld.kind == "names" else (widget,)):
                child.bind("<MouseWheel>", scroll, add="+")
            built[fld.key] = {"include": box, "included": included, "editor": widget,
                              "var": var, "hint": hint, "kind": fld.kind}
        for child in (canvas, grid):
            child.bind("<MouseWheel>", scroll, add="+")
        return built

    def _field_editor(self, row, item, fld, value):
        """The editor of one field, by its kind. -> ``(widget, its variable or None)``."""
        item_id, key = item.id, fld.key

        def typed(new):
            if not self._building and not self._closed:
                self.set_field(item_id, key, new)

        def traced(var):
            name = var.trace_add("write", lambda *_args: typed(var.get()))
            self._editor_traces.append((var, name))
            return var

        entry_options = dict(font=_HINT_FONT, bg=BG_ENTRY, fg=FG_WHITE,
                             insertbackground=FG_WHITE, relief=tk.FLAT, highlightthickness=1,
                             highlightbackground=BORDER_COLOR, highlightcolor=FG_ACCENT)
        if fld.kind == "bool":
            var = tk.BooleanVar(self.win, value=value is True)
            return self._check(row, text="on", variable=var,
                               command=lambda: typed(bool(var.get()))), var
        if fld.kind == "enum":
            choices = [choice for choice in fld.choices if isinstance(choice, str)]
            var = tk.StringVar(self.win, value=value if isinstance(value, str) else "")
            box = ttk.Combobox(row, textvariable=var, state="readonly", values=choices,
                               width=10, font=_HINT_FONT)
            box.bind("<<ComboboxSelected>>", lambda _event: typed(var.get()), add="+")
            # ttk changes a read-only combobox with the wheel: a stray notch would change a
            # value of the file. Here the wheel scrolls the rows instead (_build_fields).
            return box, var
        if fld.kind == "int":
            var = tk.StringVar(self.win, value=str(value))
            low = fld.lo if type(fld.lo) is int else -(2 ** 31)
            high = fld.hi if type(fld.hi) is int else 2 ** 31 - 1
            spin = tk.Spinbox(row, textvariable=var, from_=low, to=high, width=8,
                              buttonbackground=BG_ENTRY, **entry_options)
            return spin, traced(var)
        if fld.kind == "names":
            box = tk.Text(row, width=10, height=_NAMES_LINES, wrap=tk.NONE, undo=False,
                          **entry_options)
            box.insert("1.0", "\n".join(name for name in value if isinstance(name, str))
                       if isinstance(value, list) else "")
            for sequence in ("<KeyRelease>", "<FocusOut>"):
                box.bind(sequence, lambda _event: typed(box.get("1.0", "end-1c")), add="+")
            return box, None
        var = tk.StringVar(self.win, value=value if isinstance(value, str) else "")
        return tk.Entry(row, textvariable=var, width=10, **entry_options), traced(var)

    def _sync_editor(self) -> None:
        """Bring the editor's boxes up to date with the ticks."""
        tree = self._views["create"].tree
        editor = self.editor
        if tree is None or editor["kind"] is None:
            return
        try:
            for key, row in editor.get("fields", {}).items():
                row["included"].set(tree.state(f"{editor['item']}{LEAF_SEP}{key}") == "on")
            box = editor.get("list")
            if box is not None:
                top = box.yview()[0]
                box.delete(0, tk.END)
                for row in editor["rows"]:
                    node = tree.node(row)
                    summary = node.summary if node is not None else ""
                    box.insert(tk.END, tree.text(row) + (f"   {summary}" if summary else ""))
                box.yview_moveto(top)
        except tk.TclError:
            pass

    def _on_record_press(self, event):
        box = self.editor.get("list")
        if box is None:
            return "break"
        index = box.nearest(event.y)
        bounds = box.bbox(index) if index >= 0 else None
        if bounds and event.y <= bounds[1] + bounds[3] and index < len(self.editor["rows"]):
            self.toggle(self.editor["rows"][index])
        return "break"                  # a tick list has no selection to move

    def _set_all(self, item_id, on) -> None:
        tree = self._views["create"].tree
        if self._closed or tree is None:
            return
        tree.set(item_id, on)
        self._draft_changed()

    def _field_hint(self, item_id, key, text, bad) -> str:
        row = self.editor.get("fields", {}).get(key) if self.editor["item"] == item_id else None
        if row is not None:
            try:
                row["hint"].configure(text=text, fg=FG_RED if bad else FG_ORANGE)
                if text:
                    row["hint"].grid()
                else:
                    row["hint"].grid_remove()
            except tk.TclError:
                pass
        return text if bad else ""

    def set_field(self, item_id, key, value) -> str:
        """Granular editing: put ``value`` into the draft as field ``key`` of a value item.
        -> ``""`` when the draft took it, else why it did not (the draft is unchanged; the
        reason also stands under the field). The ENGINE decides: the value goes through the
        item's own ``validate``, so nothing can be typed here that a file could not hold.
        A whole number is clamped to the field's range; a system must be known."""
        item = _item(item_id)
        payload = self.payloads.get(item_id) if isinstance(item_id, str) else None
        fld = next((entry for entry in getattr(item, "fields", ())
                    if getattr(entry, "key", None) == key), None)
        if self._closed or self.mode != "granular" or fld is None \
                or not isinstance(payload, dict) or key not in payload:
            return MSG_NOT_EDITABLE
        ctx = self._ctx()
        if fld.kind == "int" and isinstance(value, str):
            try:
                value = int(value.strip())
            except ValueError:
                return self._field_hint(item_id, key, "is not a whole number", True)
        if fld.kind == "int" and type(value) is int:
            if type(fld.lo) is int:
                value = max(fld.lo, value)
            if type(fld.hi) is int:
                value = min(fld.hi, value)
        elif fld.kind == "names" and isinstance(value, str):
            value = [line.strip() for line in value.splitlines() if line.strip()]
            if not value:
                return self._field_hint(item_id, key, "needs at least one name - untick it "
                                        "to leave it out of the file", True)
        elif fld.kind == "system":
            try:
                known = isinstance(value, str) and ctx.system_ids(value.strip()) is not None
            except Exception:
                known = False
            if not known:
                return self._field_hint(item_id, key, "is not a known system", True)
        try:
            clean, problems = item.validate({key: value}, ctx)
        except Exception:
            clean, problems = ABSENT, []
        lead = f"{item.label} - {fld.label}: "
        said = [line[len(lead):] if line.startswith(lead) else line
                for line in _texts(problems, MAX_SHOWN)]
        if not isinstance(clean, dict) or key not in clean:
            return self._field_hint(item_id, key, said[0] if said else "is not a usable value",
                                    True)
        new = clean[key]
        if type(new) is not type(payload[key]) or new != payload[key]:
            # The value is the draft's AT ONCE (payloads, _edits: a save, a refresh and a
            # close all see it). What is rebuilt from it - the bundle, the rows, the right
            # pane - waits behind the debounce: a keystroke builds nothing.
            payload[key] = new
            self._edits.setdefault(item_id, {}).setdefault("fields", {})[key] = deepcopy(new)
            self._schedule("draft")
        self._field_hint(item_id, key, "; ".join(said), False)
        return ""

    def edit_palette(self) -> None:
        """Open the palette editor on the draft's colours, through the host."""
        payload = self.payloads.get(PALETTE_ITEM)
        roles = payload.get("roles") if isinstance(payload, dict) else None
        if self._closed or not isinstance(roles, dict):
            return
        self._ask_host("edit_palette", self.win, dict(roles), self._palette_done)

    def _palette_done(self, result) -> None:
        # The editor reports every way out; only "use" carries colours for the draft.
        if self._closed or not isinstance(result, dict) or result.get("action") != "use" \
                or PALETTE_ITEM not in self.payloads:
            return
        item = _item(PALETTE_ITEM)
        try:
            clean, _problems = item.validate({"name": result.get("name"),
                                              "roles": result.get("roles")}, self._ctx())
        except Exception:
            clean = ABSENT
        if not isinstance(clean, dict):
            self._say(MSG_PALETTE_REFUSED, "warn")
            return
        self.payloads[PALETTE_ITEM] = clean
        self._edits[PALETTE_ITEM] = {"payload": deepcopy(clean)}
        self._draft_changed(rebuild=True)

    # -- Create: saving --------------------------------------------------------------------------
    def tag_ok(self, text) -> bool:
        """The tag entry's validator: at most ``group_bundle.MAX_TAG`` characters."""
        return isinstance(text, str) and len(text) <= group_bundle.MAX_TAG

    def _next_revision(self, name) -> int:
        """The revision ``remember_export`` will hand out for ``name`` - looked up only,
        for the file name the dialog proposes. Nothing is consumed."""
        try:
            known = group_state.read_sidecar(self.app_dir)["exports"].get(name)
        except Exception:
            known = None
        revision = known.get("revision") if isinstance(known, dict) else None
        return revision + 1 if type(revision) is int and revision >= 1 else 1

    def _save_failed(self, reason, path=None) -> bool:
        """A save that came to nothing: say why, in a box and in the status line. ``reason``
        is a sentence, or the exception of the writer / the reader. With a ``path``, the
        file there is removed: one that cannot be imported is no group file."""
        if isinstance(reason, group_bundle.BundleError):
            reason = MSG_TOO_BIG if getattr(reason, "code", None) == "too_big" else str(reason)
        elif isinstance(reason, Exception):     # OSError: the folder, the disk, a lock
            reason = getattr(reason, "strerror", None) or type(reason).__name__
        if path is not None:
            try:
                os.remove(path)
            except OSError:
                pass
        messagebox.showerror(TITLE, f"{MSG_NOT_SAVED}\n\n{_show(reason, MAX_LINE)}",
                             parent=self.win)
        self._say(reason if reason == MSG_TOO_BIG else MSG_NOT_SAVED, "warn")
        return False

    def save_file(self) -> bool:
        """Write the draft to a group file the user picks. True when a file was written
        AND read back whole. Order: the size (a selection too large for one file is
        refused before anything is asked) -> the own-name sweep (a question when it finds
        one) -> the file dialog -> the revision is taken (only now: a refusal, a cancelled
        dialog or a declined question costs none) -> write -> read back.

        WHAT IS WRITTEN IS WHAT WAS SWEPT: the bundle the sweep and the question saw, with
        its id and its revision filled in and nothing else touched - the entries are not
        read a second time after the dialogs."""
        if self._closed:
            return False
        self.flush()                    # a value that was just typed is in the draft
        self._say_unusable()            # what an earlier attempt said is not about this one
        if not self._can_save():
            return False
        name = clean_text(self.name_var.get())
        options = options_from_tree(self._views["create"].tree, self.payloads)
        proposed = self._meta(_PLACEHOLDER_ID, self._next_revision(name))
        try:
            bundle = group_items.build_bundle(self.payloads, options, proposed)
            text = group_bundle.dumps(bundle)
        except Exception as exc:
            return self._save_failed(exc)
        if _file_bytes(text) > group_bundle.MAX_FILE_BYTES:
            return self._save_failed(MSG_TOO_BIG)
        hits = find_names(text, self._ask_host("own_names", default=[]))
        if hits:
            names = ", ".join(_show(hit, MAX_LABEL) for hit in hits)
            if self._confirm("Personal names found", MSG_NAMES.format(names=names),
                             [("Go back", None), ("Save anyway", True)]) is not True:
                return False
        try:
            path = filedialog.asksaveasfilename(
                parent=self.win, title="Save group file",
                defaultextension=group_bundle.EXTENSION,
                initialfile=group_bundle.default_filename(proposed), filetypes=FILE_TYPES)
        except tk.TclError:
            path = ""
        if not path or not isinstance(path, str):
            return False
        remembered = group_apply.remember_export(self.app_dir, name)
        if not remembered:
            self._say(MSG_BUSY, "warn")
            return False
        bundle_id, revision = remembered
        bundle = group_bundle.Bundle(
            meta=dataclasses.replace(bundle.meta, id=bundle_id, revision=revision),
            categories=bundle.categories)
        try:
            group_bundle.write_file(path, bundle)
        except Exception as exc:        # the writer refused it, or the disk did
            return self._save_failed(exc)
        try:
            _written, notes = group_bundle.read_file(path)
        except group_bundle.BundleError as exc:
            return self._save_failed(exc, path)
        if notes:
            return self._save_failed(MSG_READ_BACK, path)
        self._say(f"Saved {_show(os.path.basename(path))} (revision {revision})", "ok")
        return True

    # -- Import -----------------------------------------------------------------------------------
    def pick_soon(self) -> None:
        """Open the file dialog as soon as the window is idle - no second click."""
        try:
            self._idle = self.win.after_idle(self._pick)
        except tk.TclError:
            self._idle = None

    def _pick(self) -> None:
        self._idle = None
        if self._closed:
            return
        try:
            self.win.update_idletasks()     # the page is drawn before the dialog covers it
        except tk.TclError:
            return
        self.choose_file()

    def choose_file(self, path=None) -> bool:
        """Read a group file and show what it holds; the plan of it follows behind the
        debounce. Without a ``path`` the user is asked for one (a cancelled dialog changes
        nothing). False when no file was taken - then the reason is the status line."""
        if self._closed:
            return False
        if path is None:
            # One question at a time. The native dialog disables THIS window only: the
            # main window stays enabled, and its "Import a group file..." buttons call in
            # here again while the question is up. That call returns quietly.
            if self._picking:
                return False
            self._picking = True
            try:
                path = filedialog.askopenfilename(parent=self.win, title="Import a group file",
                                                  filetypes=FILE_TYPES)
            except tk.TclError:
                path = ""
            finally:
                self._picking = False
            if self._closed:            # the window was closed while the question was up
                return False
            if not path or not isinstance(path, str):
                return False
        if self.page != "import":
            self.show_page("import")
        self._waiting.discard("plan")
        self.bundle, self.results = None, None
        self.read_notes, self.problems, self.file_payloads = [], [], {}
        self.header, self.warnings, self.notes = [], [], []
        self.item_modes, self.item_conflicts = {}, {}
        self.doctrine_pairs, self.doctrine_choices = {}, {}
        self._preview, self._extra_rows, self._import_unusable = {}, [], {}
        self._refusal = ""
        view = self._views["import"]
        view.sel = None
        try:
            self.file_var.set(_show(os.path.basename(os.fspath(path))))
        except TypeError:
            self.file_var.set("")
        try:
            bundle, notes = group_bundle.read_file(path)
        except group_bundle.BundleError as exc:
            # Why not stands where the file is named - the header - and in the status line.
            self._refusal = _show(str(exc), MAX_LINE)
            view.fill(None)
            self._paint_import()
            self._paint_apply()
            self._say(self._refusal, "warn")
            return False
        try:
            payloads, problems = group_items.validate_bundle(bundle, self._ctx())
        except Exception as exc:        # validate_bundle is total; this is the backstop
            log.warning("group window: the file could not be checked (%s)", type(exc).__name__)
            payloads, problems = {}, []
        self.bundle, self.read_notes = bundle, list(notes)
        self.file_payloads, self.problems = payloads, list(problems)
        self._ask_host("flush_config")  # the plan reads the files, as the apply will
        self._find_pairs(payloads)
        self.header = self._header_lines()
        self._set_preview(None)
        view.fill(SelectionTree(build_nodes(payloads, depth="leaves",
                                            tag=getattr(bundle.meta, "tag", ""))),
                  self._extra_rows)
        view.select(ROOT_ID)
        self._paint_import()
        self._replan()
        return True

    def _header_lines(self) -> list:
        """The header of the file: its name (and "by <tag>") with the facts beside it, its
        notes, and when this group file was imported before."""
        name = tag = notes = ""
        facts = []
        for line in import_header(self.bundle):     # our own lines: told apart by their lead
            if line.startswith("Name: "):
                name = line[len("Name: "):]
            elif line.startswith("Group tag: "):
                tag = line[len("Group tag: "):]
            elif line.startswith("Notes: "):
                notes = line
            else:
                facts.append(line)
        lines = [_SEPARATOR.join([" ".join(part for part in (name or "(no name)", tag)
                                           if part)] + facts)]
        # The paired doctrines right under the title: the box shows four lines at most,
        # and long notes must never scroll this line out of sight.
        paired = doctrine_header_line(len(self.doctrine_pairs))
        if paired:
            lines.append(paired)
        if notes:
            lines.append(notes)
        before = self._imported_before()
        if before:
            lines.append(before)
        return lines

    def _find_pairs(self, payloads) -> None:
        """The file's doctrines that look like the importer's, and the choices an earlier
        import of this group honoured for them - asked ONCE per file read (it costs a few
        tenths of a second on a large library), never per plan. Only a pair of a record row
        the tree shows is kept, with a remembered action that pair offers."""
        try:
            found = group_apply.plan_matches(self.app_dir, self.bundle, ctx=self.ctx)
            pairs, remembered = found.get("pairs"), found.get("remembered")
        except Exception as exc:        # plan_matches is total; this is the backstop
            log.warning("group window: the doctrines could not be paired (%s)",
                        type(exc).__name__)
            pairs, remembered = {}, {}
        item = _item(DOCTRINES_ITEM)
        keys = {key for key, _label, _detail in _records(item, _payload(payloads,
                                                                        DOCTRINES_ITEM))} \
            if item is not None else set()
        self.doctrine_pairs = {key: pair for key, pair in pairs.items()
                               if key in keys and isinstance(getattr(pair, "local_id", None),
                                                             str)} \
            if isinstance(pairs, dict) else {}
        self.doctrine_choices = {}
        for key, choice in remembered.items() if isinstance(remembered, dict) else ():
            pair = self.doctrine_pairs.get(key)
            action = choice.get("action") if isinstance(choice, dict) else None
            if pair is not None and action in dict(pair_choices(pair)):
                self.doctrine_choices[key] = action

    def _imported_before(self) -> str:
        bundle_id = getattr(self.bundle.meta, "id", "")
        if not isinstance(bundle_id, str) or not bundle_id:
            return ""
        try:
            entries = group_apply.history(self.app_dir)
        except Exception:
            entries = []
        for entry in entries:                       # newest first
            meta = entry.get("bundle")
            if entry.get("status") != "applied" or not isinstance(meta, dict) \
                    or meta.get("id") != bundle_id:
                continue
            revision, day = meta.get("revision"), _day(entry.get("at"))
            if type(revision) is int and revision >= 1:
                return f"You imported revision {revision}" + (f" on {day}" if day else "")
        return ""

    def _set_preview(self, results) -> None:
        """The two cells of every item row (and the rows of what this build does not
        know) from the results of a plan; ``None``: no plan yet, nothing is said to
        happen."""
        rows = preview_rows(results or [], self.file_payloads,
                            getattr(self.bundle.meta, "tag", ""), self.problems)
        self._preview, self._extra_rows = {}, []
        for row, in_file, happens in rows:
            if row.startswith(UNKNOWN_PREFIX):
                self._extra_rows.append((row, in_file or "(unnamed)"))
                self._preview[row] = ("", happens)
            else:
                self._preview[row] = (in_file, happens if results is not None else "")

    def _import_cells(self, row):
        if row in self._preview:
            in_file, happens = self._preview[row]
            return (in_file, self._import_unusable.get(row, happens))
        tree = self._views["import"].tree
        node = tree.node(row) if tree is not None else None
        return (node.summary if node is not None and node.kind != "root" else "", "")

    def _import_tags(self, row):
        tags = self._row_tags(self._views["import"].tree, row, self._import_unusable)
        # A paired doctrine that is ticked is marked; unticked it is dim like any other row.
        return ("similar",) if not tags and self._pair_key(row) is not None else tags

    def _pair_key(self, row):
        """The record key of a paired doctrine's row, or ``None`` for any other row."""
        prefix = DOCTRINES_ITEM + LEAF_SEP
        if not isinstance(row, str) or not row.startswith(prefix):
            return None
        key = row[len(prefix):]
        return key if key in self.doctrine_pairs else None

    def _pair_default(self, key) -> str:
        return pair_default(self.doctrine_pairs.get(key),
                            self.item_conflicts.get(DOCTRINES_ITEM, CONFLICT_CHOICES[0][0]))

    def doctrine_choice(self, key):
        """What happens to a paired doctrine: the choice made for it, else its default
        (``pair_default``: merge for a pair of different names, the item radio's for one of
        the same name); ``None`` for a record key that is no pair."""
        if key not in self.doctrine_pairs:
            return None
        return self.doctrine_choices.get(key) or self._pair_default(key)

    def _import_options(self) -> Options:
        """What is ticked, with the choices made per item - and per paired doctrine: every
        choice MADE for it is sent - clicked, or pre-set from what an earlier import of this
        group honoured (so it is remembered again) - even one that equals its default. A
        pair of different names nobody chose for is sent as its default, a merge (D8); one
        of the same name nobody chose for is not sent: it follows the item's radio, as
        before (D4). A doctrine whose row is not ticked is not imported: nothing is sent
        for it, its choice stays here."""
        view = self._views["import"]
        options = options_from_tree(view.tree, self.file_payloads)
        options.modes.update(self.item_modes)
        options.conflicts.update(self.item_conflicts)
        chosen = {}
        for key, pair in self.doctrine_pairs.items():
            if view.tree is None or view.tree.state(DOCTRINES_ITEM + LEAF_SEP + key) != "on":
                continue
            action = self.doctrine_choices.get(key)
            if action is None and not _exact(pair):
                action = pair_default(pair)
            if action is not None:
                chosen[key] = {"action": action, "local_id": pair.local_id}
        if chosen:
            options.resolutions[DOCTRINES_ITEM] = chosen
        return options

    def _replan(self) -> None:
        """What is ticked or chosen changed: plan again, behind the debounce. Until the
        plan is there nothing can be applied."""
        if self.bundle is None:
            return
        self.results = None
        self._paint_apply()
        self._schedule("plan")

    def _run_plan(self) -> None:
        """Plan the import on the files as they are now - the real apply code, run on a
        copy - and show what it would do. It runs on this thread: about a second and a
        half for a five-megabyte library, behind "Checking the file..."."""
        if self.bundle is None:
            return
        view = self._views["import"]
        try:
            results, problems = group_apply.plan_import(self.app_dir, self.bundle,
                                                        self._import_options(), ctx=self.ctx)
            results, problems = list(results), list(problems)
        except Exception as exc:
            log.warning("group window: the plan failed (%s)", type(exc).__name__)
            results = []
            problems = list(self.problems) + ["This file could not be checked against your "
                                              "settings."]
        self.results, self.problems = results, problems
        self._import_unusable = unusable_selection(view.tree, self.file_payloads)
        self._set_preview(results)
        self.warnings = import_warnings(self.read_notes, problems, results)
        self.notes = result_notes(results)
        view.fill(view.tree, self._extra_rows)
        self._paint_import()
        self._paint_apply()

    def _paint_import(self) -> None:
        """The header, the warnings box and the right pane of the Import page."""
        lines = self.header
        paired = doctrine_header_line(len(self.doctrine_pairs))
        parts = [(line + ("\n" if index < len(lines) - 1 else ""),
                  "title" if index == 0 else "dim" if line.startswith("You imported")
                  else "accent" if paired and line == paired else "")
                 for index, line in enumerate(lines)]
        if self.bundle is None:                     # no file yet, or one that was refused
            name = self.file_var.get()
            parts = [(name + "\n", "title")] if name else []
            if self._refusal or name:               # refused: the reader's own sentence
                parts.append((self._refusal or NOT_A_GROUP_FILE, "warn"))
            else:
                parts.append((NO_FILE_CHOSEN, "dim"))
        self._write(self.header_text, parts)
        self._header_count = 1 + sum(text.count("\n") for text, _tags in parts)
        self._size_header()
        self._paint_update_notice(repack=False)
        lines = []
        if self.warnings:
            lines.append(("Warnings", "head"))
            lines += [(f"  {line}", ("warn", "hang")) for line in self.warnings]
        if self.notes:
            lines.append(("What changes, in detail", "head"))
            lines += [(f"  {line}", "hang") for line in self.notes]
        if not lines and self.results is not None:
            lines = [("No warnings.", "dim")]
        # No line break after the last line: the box is as tall as its lines, to the line.
        self._write(self.warn_text, [(text + ("\n" if index < len(lines) - 1 else ""), tags)
                                     for index, (text, tags) in enumerate(lines)])
        self._warn_count = len(lines)
        try:
            if lines:                               # an empty box is no box at all
                self._size_warnings()
                self._warn_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=(6, 0),
                                      after=self._bottom_anchor())
            else:
                self._warn_frame.pack_forget()
        except tk.TclError:
            pass
        self._show_options()
        self._render_import()

    def _size_warnings(self, _event=None) -> None:
        """Make the warnings box as tall as its text: ``_warn_height`` of its lines. Where
        the box is on screen and a long line wraps, the lines counted are the ones SHOWN
        (the widget is asked), so the box still ends on a whole line; it is measured again
        whenever its width or the page's height changes (their ``<Configure>``). On a
        short page - the window at its smallest - it stops at four lines and scrolls: six
        would leave the tree and the text beside it next to nothing."""
        box, lines, most = self.warn_text, self._warn_count, _WARN_LINES[1]
        try:
            if lines and box.winfo_ismapped() and box.winfo_width() > 1:
                lines = self._shown_lines(box, lines)
                page = self.pages["import"].winfo_height()
                if page < _ROOMY_PAGE * self._hint_font.metrics("linespace"):
                    most = _WARN_LINES_SHORT
            self._set_height(box, _warn_height(lines, most))
        except (tk.TclError, TypeError, ValueError):
            pass

    @staticmethod
    def _shown_lines(box, lines) -> int:
        """``lines`` or - where a long line wraps in the mapped ``box`` - the lines SHOWN
        (the widget is asked)."""
        if lines and box.winfo_ismapped() and box.winfo_width() > 1:
            shown = box.tk.call(box._w, "count", "-displaylines", "1.0", "end-1c")
            return max(lines, int(shown) + 1)
        return lines

    @staticmethod
    def _set_height(box, wanted) -> None:
        if int(box.cget("height")) != wanted:
            box.configure(height=wanted)

    def _size_header(self, _event=None) -> None:
        """The Import header box, like the warnings box (``_size_warnings``): as tall as its
        text in whole lines, two to four."""
        try:
            lines = self._shown_lines(self.header_text, self._header_count)
            self._set_height(self.header_text, _box_height(lines, *_HEADER_LINES))
        except (tk.TclError, TypeError, ValueError):
            pass

    def _size_attention(self, _event=None) -> None:
        """The History attention box: whole lines, one to four."""
        try:
            lines = self._shown_lines(self.attention_text, self._attention_count)
            self._set_height(self.attention_text, _box_height(lines, *_ATTENTION_LINES))
        except (tk.TclError, TypeError, ValueError):
            pass

    def _bottom_anchor(self):
        """The widget the warnings box sits directly above: the update notice while it
        shows, else the backup line."""
        notice = self.lines["update_first"]
        return notice if notice.winfo_manager() == "pack" else self.lines["backup"]

    def _update_ready(self) -> bool:
        return self._ask_host("update_state", default="") == "ready"

    def _paint_update_notice(self, repack=True) -> None:
        """Show the update sentence on the Import page - above the backup line - while an
        update waits and a file is chosen, else hide it. The warnings box is kept above it."""
        notice = self.lines["update_first"]
        show = self.bundle is not None and self._update_ready()
        try:
            if show == (notice.winfo_manager() == "pack"):
                return
            if show:
                notice.pack(side=tk.BOTTOM, fill=tk.X, after=self.lines["backup"])
            else:
                notice.pack_forget()
            if repack and self._warn_frame.winfo_manager() == "pack":
                self._warn_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=(6, 0),
                                      after=self._bottom_anchor())
        except tk.TclError:
            pass

    def _render_import(self) -> None:
        view, text = self._views["import"], self._details["import"]
        row, tree = view.sel, view.tree
        if self.bundle is None or tree is None:
            self._write(text, [(NO_FILE, "dim")])
            return
        node = tree.node(row)
        if node is None and view.label_of(row) is None:
            parts = []
        elif node is None:                          # a part this build does not know
            parts = [(view.label_of(row) + "\n", "title"),
                     (f"({self._import_cells(row)[1]})\n", "warn"),
                     ("This version of FCTool does not know this part of the file. It is "
                      "left out; nothing else is affected.\n", "dim")]
        elif node.kind in _OPEN_KINDS:
            lines = manifest_lines(self.bundle)
            if node.kind == "category":
                lines = _category_lines(lines, node.label)
            parts = [(node.label + "\n", "title")] + (self._manifest_parts(lines) or [
                ("(the file holds nothing of this)\n", "dim")])
        elif node.kind == "info":
            parts = [(node.label + "\n", "title"), (f"({node.summary})\n", "dim")]
        else:
            item_id = TAG_ID if node.id == TAG_ID else self._item_of(view, node.id)
            item = _item(item_id)
            title = "Group tag" if item is None \
                else f"{_category_label(item.category)} > {_label(item)}"
            payload = getattr(self.bundle.meta, "tag", "") if item is None \
                else _payload(self.file_payloads, item_id)
            rows = [entry for entry in detail_lines(item_id, payload) if entry[0] == "saved"]
            happens = self._import_cells(item_id)[1]
            result = next((entry for entry in self.results or ()
                           if entry.item_id == item_id), None)
            parts = [(title + "\n", "title")]
            key = self._pair_key(node.id)
            if key is not None:                     # a paired doctrine: what it looks like
                lines = pair_detail_lines(self.doctrine_pairs[key], self.doctrine_choice(key))
                parts += [(line + "\n", "sub" if index == 0 else ("hang",))
                          for index, line in enumerate(lines)]
            parts += self._detail_parts(text, rows, HEAD_IN_FILE)
            parts.append((HEAD_HAPPENS + "\n", "head"))
            parts.append((f"  {happens or (MSG_CHECKING if self.results is None else NOT_TICKED)}"
                          "\n", ""))
            if result is not None:
                parts += [(f"  {line}\n", "dim") for line in result_notes([result])]
                parts += [(f"  {line}\n", "warn")
                          for line in import_warnings([], [], [result])]
        self._write(text, parts, subject=("import", row))

    def _show_options(self) -> None:
        """The choices of the selected row, above the text: on an item's row (and any
        record of it but a paired doctrine) how its list is merged (only an item that
        knows both ways) and who wins a same-named entry (only an item that can have one);
        on a paired doctrine's row ONLY "This doctrine:" - the item's radios stay on the
        item's row, so the text under them keeps its room."""
        frame, view = self._options_frame, self._views["import"]
        try:
            for child in frame.winfo_children():
                child.destroy()
            self.option_buttons, self._option_vars, self._option_item = {}, {}, None
            self._option_pair, self._option_widths = None, {}
            item_id = self._item_of(view, view.sel) if self.bundle is not None else None
            item = _item(item_id) if item_id in self.file_payloads else None
            groups = []
            # A paired doctrine's own choice (spec "doctrine merge" 9) stands alone.
            pair_key = self._pair_key(view.sel) if item is not None else None
            if pair_key is not None:
                groups.append(("doctrine", "This doctrine:",
                               pair_choices(self.doctrine_pairs[pair_key]),
                               self.doctrine_choice(pair_key)))
            else:
                if item is not None \
                        and all(mode in item.modes for mode, _text in MODE_CHOICES):
                    groups.append(("mode", "Your own entries:", MODE_CHOICES,
                                   self.item_modes.get(item_id, item.modes[0])))
                conflicts = item_conflict_choices(item)     # three for the doctrines
                if conflicts:
                    groups.append(("conflict", "An entry of the same name:", conflicts,
                                   self.item_conflicts.get(item_id, conflicts[0][0])))
            if not groups:
                frame.pack_forget()
                return
            self._option_item, self._option_pair = item_id, pair_key
            self._option_rows = {}
            base = 0
            for kind, caption, choices, current in groups:
                parent, row = frame, base
                if kind == "doctrine":
                    # Its own frame, gridded at its group's first row across all three
                    # columns: its choices (four for a pair of different names) make
                    # columns of their own and never set the item radios' widths.
                    parent, row = tk.Frame(frame, bg=BG_DARK), 0
                    parent.grid(row=base, column=0, columnspan=3, sticky=tk.W)
                # The caption, then one row per radio: _place_options may stack them all.
                base += 1 + len(choices)
                self._option_rows[kind] = row
                self._label(parent, caption, font=_HINT_FONT, fg=FG_DIM, anchor=tk.W).grid(
                    row=row, column=0, columnspan=len(choices), sticky=tk.W)
                var = tk.StringVar(self.win, value=current)
                self._option_vars[kind] = var
                for value, text in choices:
                    if kind == "doctrine":
                        command = (lambda k=pair_key, v=value: self.set_doctrine_choice(k, v))
                    else:
                        command = (lambda k=kind, v=value, i=item_id:
                                   self.set_item_option(i, **{k: v}))
                    button = tk.Radiobutton(
                        parent, text=text, value=value, variable=var, font=_HINT_FONT,
                        fg=FG_TEXT, bg=BG_DARK, selectcolor=BG_ENTRY, activebackground=BG_DARK,
                        activeforeground=FG_ACCENT, highlightthickness=0, bd=0, anchor=tk.W,
                        justify=tk.LEFT, command=command)
                    self.option_buttons[(kind, value)] = button
                    self._option_widths[(kind, value)] = button.winfo_reqwidth()  # one line
            self._place_options(frame.winfo_width())
            frame.pack(side=tk.TOP, fill=tk.X, pady=(0, 6), before=self._import_detail_frame)
        except tk.TclError:
            pass

    def _place_options(self, width) -> None:
        """Lay the choices of each group side by side where ``width`` (the pane's) has
        room for them, else one under the other - each then wrapped to the pane, so a long
        one (at 150% scaling) is never cut at its edge. Every ``grid`` call passes its full
        set of options: the ones left out would be kept from the layout before. The widths
        are the radios' on ONE line (``_option_widths``, taken when they were made): a
        wrapped radio asks for less, and must not be thought to fit beside the others."""
        rows = self._option_rows
        try:
            for kind in ("mode", "conflict", "doctrine"):
                if kind not in self._option_vars or kind not in rows:
                    continue
                group = [(key, button) for key, button in self.option_buttons.items()
                         if key[0] == kind]
                widths = [self._option_widths.get(key) or button.winfo_reqwidth()
                          for key, button in group]
                wanted = sum(widths) + 10 + 14 * max(1, len(group) - 1)
                beside = width <= 1 or wanted <= width
                for position, ((_key, button), one_line) in enumerate(zip(group, widths)):
                    wrap = 0
                    if not beside:              # the text's room: the pane less the indicator
                        text = button.cget("text")
                        wrap = max(1, width - 10 - (one_line - self._hint_font.measure(text)))
                    if int(str(button.cget("wraplength")) or 0) != wrap:
                        button.configure(wraplength=wrap)
                    if beside:
                        button.grid(row=rows[kind] + 1, column=position, columnspan=1,
                                    sticky=tk.W, padx=(10 if position == 0 else 14, 0), pady=0)
                    else:
                        button.grid(row=rows[kind] + 1 + position, column=0,
                                    columnspan=len(group), sticky=tk.W, padx=(10, 0), pady=0)
        except (tk.TclError, ValueError):
            pass

    def set_item_option(self, item_id, mode=None, conflict=None) -> bool:
        """Choose how one item of the file comes in: ``mode`` (one of the item's modes -
        ``merge`` adds to mine, ``replace`` replaces mine) and / or ``conflict``
        (``theirs`` | ``mine``, and ``merge`` for the doctrines - ``item_conflict_choices`` -
        for an item that can meet an entry of the same name). A choice the item does not
        have is ignored. True when something changed: the plan then runs again, behind the
        debounce."""
        item = _item(item_id)
        if self._closed or self.bundle is None or item is None \
                or item_id not in self.file_payloads:
            return False
        changed = False
        if isinstance(mode, str) and len(item.modes) > 1 and mode in item.modes \
                and mode != self.item_modes.get(item_id, item.modes[0]):
            self.item_modes[item_id] = mode
            if mode == item.modes[0]:
                del self.item_modes[item_id]        # the item's own default: nothing to say
            changed = True
        conflicts = item_conflict_choices(item)
        default = conflicts[0][0] if conflicts else CONFLICT_CHOICES[0][0]
        if isinstance(conflict, str) and conflicts \
                and conflict in dict(conflicts) \
                and conflict != self.item_conflicts.get(item_id, default):
            self.item_conflicts[item_id] = conflict
            if conflict == default:
                del self.item_conflicts[item_id]
            changed = True
        if changed:
            if self._option_item == item_id:        # the choice may not have come from them
                for kind, value in (("mode", self.item_modes.get(item_id, item.modes[0])),
                                    ("conflict", self.item_conflicts.get(item_id, default)),
                                    # a pair of the same name follows the item's radio
                                    ("doctrine", self.doctrine_choice(self._option_pair))):
                    if kind in self._option_vars and value is not None:
                        self._option_vars[kind].set(value)
            self._replan()
        return changed

    def set_doctrine_choice(self, record_key, action) -> bool:
        """Choose what happens to one paired doctrine of the file (``record_key``, its row's
        record key): one of the actions its pair offers (``pair_choices``). It overrides the
        item's "An entry of the same name" radio for that doctrine, and it is SENT
        (``_import_options``) even when it equals the pair's default. Anything else is
        refused (False), and so is the choice already made for it (False: nothing changed).
        Otherwise every explicit click is recorded and True: the plan runs again, behind
        the debounce - though what is sent may be unchanged (a pair of different names
        clicked on its default, the merge it was already sent as). The choice is kept
        while the row is unticked; it is only not sent."""
        pair = self.doctrine_pairs.get(record_key) if isinstance(record_key, str) else None
        if self._closed or self.bundle is None or pair is None \
                or not isinstance(action, str) or action not in dict(pair_choices(pair)) \
                or self.doctrine_choices.get(record_key) == action:
            return False
        before = self.doctrine_choice(record_key)
        self.doctrine_choices[record_key] = action
        if action != before:                    # what is shown for it changed too
            if self._option_pair == record_key and "doctrine" in self._option_vars:
                self._option_vars["doctrine"].set(action)   # it may not have come from it
            self._views["import"].repaint()
            if self._pair_key(self._views["import"].sel) == record_key:
                self._render_import()           # "This doctrine: ..." at once
        self._replan()
        return True

    def _apply_state(self):
        """``(can it be applied, why not, how to say it)``."""
        if self.bundle is None:
            return False, None, "info"
        if self.results is None:
            return False, MSG_CHECKING, "info"
        statuses = {result.status for result in self.results}
        if "applied" not in statuses:
            tree = self._views["import"].tree
            root = tree.node(ROOT_ID) if tree is not None else None
            ticked = tree is not None and bool(tree.checked_leaves())
            tickable = root is not None and root.checkable
            # No result at all, and problems: the plan failed, or nothing in the file can
            # be used (then there is nothing to tick either). "Nothing is ticked" would
            # blame the user and "already match" would be wrong - unless the user did
            # untick everything.
            if not self.results and self.problems and (ticked or not tickable):
                return False, MSG_NOTHING_USABLE, "warn"
            if not ticked:
                return False, MSG_NOTHING_TICKED, "info"
            if statuses & {"failed", "skipped"}:
                return False, MSG_NOTHING_USABLE, "warn"
            return False, MSG_SAME, "info"
        if self._ask_host("update_state", default="") == "installing":
            return False, MSG_INSTALLING, "warn"
        if not self._ask_host("writable", default=False):
            return False, MSG_NOT_WRITABLE, "warn"
        return True, "", "info"

    def _paint_apply(self, quiet=False) -> bool:
        """Enable or disable the two apply buttons and say why. ``quiet``: only when the
        answer changed - the status line is not touched otherwise."""
        enabled, reason, kind = self._apply_state()
        self._paint_update_notice()
        try:
            if quiet and enabled == (str(self.apply_btn.cget("state")) == tk.NORMAL):
                return enabled
            for button in (self.apply_btn, self.later_btn):
                button.configure(state=tk.NORMAL if enabled else tk.DISABLED)
        except tk.TclError:
            return False
        if reason is not None and self.page == "import":
            for item_id, why in self._import_unusable.items() if enabled else ():
                item = _item(item_id)       # it can be applied - but not all that is ticked
                reason, kind = f"{_label(item) if item is not None else item_id}: {why}", "warn"
                break
            self._say(reason, kind)
        return enabled

    def _replace_pending_ok(self) -> bool:
        """Nothing is staged, or the user said the staged work may be replaced."""
        try:
            pending = group_state.read_pending(self.app_dir)
        except Exception:
            pending = None
        if pending is None:
            return True
        what = self._pending_text(pending, "An import", "A restore of your earlier settings")
        return self._confirm("Replace the pending import?", MSG_REPLACE.format(what=what),
                             [("Replace", True), ("Keep the pending one", None)]) is True

    def _restart(self) -> bool:
        """Ask the host to restart. False: it cannot right now - what is staged stays
        staged, and the History page shows it."""
        if self._ask_host("restart", default=False):
            return True
        if not self._closed:
            self.show_page("history")
            self._say(MSG_RESTART_REFUSED, "warn")
        return False

    def apply_import(self, when) -> bool:
        """Stage the import of what is ticked: ``when`` = ``now`` (then restart) or
        ``later`` (it is applied at the next start). No question is asked - unless
        something is staged already. True when the import is staged."""
        if self._closed or when not in ("now", "later") or self.bundle is None:
            return False
        self.flush()                    # a plan that still waits runs first
        if self.results is None:
            self._run_plan()
        if not self._paint_apply():
            return False
        if not self._replace_pending_ok():
            return False
        try:
            group_apply.stage_import(self.app_dir, self.bundle, self._import_options())
        except Exception as exc:        # StateWriteError and its kinds, by contract
            self._say(group_apply.stage_error_text(exc), "warn")
            return False
        if when == "now":
            self._restart()
        else:
            self.show_page("history")
            self._say(MSG_STAGED, "ok")
        return True

    # -- History ---------------------------------------------------------------------------
    @staticmethod
    def _pending_text(pending, an_import, a_restore) -> str:
        if not isinstance(pending, dict):
            return an_import
        if pending.get("kind") == "restore":
            return a_restore
        envelope = pending.get("bundle")
        meta = envelope.get("bundle") if isinstance(envelope, dict) else None
        name = _show(meta.get("name"), MAX_LABEL) if isinstance(meta, dict) else ""
        revision = meta.get("revision") if isinstance(meta, dict) else None
        if not name:
            return an_import
        return f"{an_import} of '{name}'" + (f" r{revision}" if type(revision) is int else "")

    @staticmethod
    def _pending_word(pending) -> str:
        """What is staged, in one word: ``"restore"`` or ``"import"``."""
        return "restore" if isinstance(pending, dict) and pending.get("kind") == "restore" \
            else "import"

    def _load_history(self) -> None:
        """Read the side file, the staged work and the backups, and show them."""
        try:
            self.pending = group_state.read_pending(self.app_dir)
            self.imports = [entry for entry in group_apply.history(self.app_dir)
                            if isinstance(entry, dict)]
            self._backups = {backup.get("name")
                             for backup in group_state.list_backups(self.app_dir)}
            self.attention_flags = list(group_apply.attention(self.app_dir))
        except Exception as exc:
            log.warning("group window: the history could not be read (%s)",
                        type(exc).__name__)
        self._paint_history()

    def _paint_history(self) -> None:
        try:
            if self.pending is None:
                self.banner.pack_forget()
            else:
                self.banner_var.set(self._pending_text(
                    self.pending, "An import", "A restore of your earlier settings")
                    + " is waiting - it is applied when FCTool restarts.")
                label = CANCEL_PENDING.format(what=self._pending_word(self.pending))
                self.buttons["cancel_pending"].configure(text=label, width=-len(label))
                self.banner.pack(side=tk.TOP, fill=tk.X, pady=(0, 8),
                                 before=self._history_paned)
                if self._update_ready():
                    self.banner_notice.pack(side=tk.BOTTOM, fill=tk.X,
                                            before=self.buttons["cancel_pending"])
                else:
                    self.banner_notice.pack_forget()
            listed = self.history_list
            listed.delete(*listed.get_children(""))
            for index, entry in enumerate(self.imports):
                meta = entry.get("bundle") if isinstance(entry.get("bundle"), dict) else {}
                revision = meta.get("revision")
                summary = result_summary(entry)
                headline = summary[0] if summary else ""
                if entry.get("restored"):
                    headline = "Restored since. " + headline
                listed.insert("", "end", iid=f"h{index}", values=[
                    _when(entry.get("at")), _show(meta.get("name"), MAX_LABEL),
                    f"r{revision}" if type(revision) is int and revision >= 1 else "",
                    _show(headline)])
            for key in ("open_settings", "dismiss"):
                self.buttons[key].configure(
                    state=tk.NORMAL if self.attention_flags else tk.DISABLED)
        except tk.TclError:
            return
        self._paint_attention()
        if self._history_sel is None or self._history_sel >= len(self.imports):
            self._history_sel = 0 if self.imports else None     # the newest, without a click
        if self._report_showing:
            self._show_report()
        else:
            self.show_details(self._history_sel)

    def _paint_attention(self) -> None:
        """The attention flags, one paragraph each; the picked one is marked."""
        flags = self.attention_flags
        if not 0 <= self._attention_sel < len(flags):
            self._attention_sel = 0
        lines = [_attention_line(flag.get("section"), flag.get("reason")) for flag in flags]
        self._attention_count = max(1, len(lines))
        parts = [(line + ("\n" if index < len(lines) - 1 else ""),
                  ("flag", "picked") if index == self._attention_sel else "flag")
                 for index, line in enumerate(lines)]
        self._write(self.attention_text, parts or [("Nothing needs your attention.", "dim")],
                    subject="attention")
        self._size_attention()

    def select_attention(self, index) -> bool:
        """Pick one of the attention flags - the one ``Open in Settings`` and ``Dismiss``
        act on."""
        if self._closed or type(index) is not int \
                or not 0 <= index < len(self.attention_flags):
            return False
        self._attention_sel = index
        self._paint_attention()
        return True

    def _on_attention_press(self, event):
        try:                            # "<line>.<char>" is the Text's own index
            line = int(str(self.attention_text.index(f"@{event.x},{event.y}")).split(".")[0])
        except (tk.TclError, ValueError):
            return "break"
        self.select_attention(line - 1)
        return "break"

    def show_report(self, report) -> bool:
        """Show the result of the import that was applied at this start (a report of
        ``group_apply``): switch to the History page, write the report into the right
        pane, select the import it is about and - while no character is signed in - offer
        the next step. It is what ``open_window(view="history", report=...)`` does, for a
        window that is already open.

        The report is marked as seen ONCE - the first time it is shown - however often it
        or the History page is shown afterwards. False for anything that is not a report,
        and once the window is gone."""
        if self._closed or not isinstance(report, dict):
            return False
        if self.report is None or report != self.report:
            self._report_seen = False               # another report: not seen yet
        self.report, self._report_showing = report, True
        return self.show_page("history")

    def _show_report(self) -> None:
        """The result of the import that was applied at this start, and the next step. The
        import it is about is the selected one, so that its Restore is at hand."""
        report = self.report
        stamp, backup = report.get("at"), report.get("backup")
        self._history_sel = next(
            (index for index, entry in enumerate(self.imports)
             if isinstance(stamp, str) and stamp and entry.get("at") == stamp
             and entry.get("backup") == backup), None)
        try:
            listed = self.history_list
            wanted = () if self._history_sel is None else (f"h{self._history_sel}",)
            if tuple(listed.selection()) != wanted:
                listed.selection_set(wanted)
            if wanted:
                listed.focus(wanted[0])             # the row the arrow keys move from
        except tk.TclError:
            pass
        self._write(self._details["history"], self._summary_parts(result_summary(report)))
        if not self._report_seen:
            self._report_seen = True
            try:
                group_apply.mark_report_seen(self.app_dir)
            except Exception as exc:
                log.warning("group window: the report could not be marked (%s)",
                            type(exc).__name__)
        offer = report.get("status") == "applied" \
            and not self._ask_host("has_characters", default=True)
        self._offer_sign_in(offer)
        self._paint_history_buttons()

    @staticmethod
    def _summary_parts(lines) -> list:
        """``result_summary`` lines: the headline, then one paragraph per line."""
        return [(line + ("\n" if index < len(lines) - 1 else ""),
                 "title" if index == 0 else "para") for index, line in enumerate(lines)]

    def _offer_sign_in(self, offer) -> None:
        try:
            if offer:
                self.signin_btn.pack(side=tk.LEFT)      # in its row, left of Close
            else:
                self.signin_btn.pack_forget()
        except tk.TclError:
            pass

    def _open_settings(self, section) -> bool:
        """Show a Settings section - with this window out of the way first: it stands
        above the main window and would cover the very tab it sends the user to, so the
        step would look as if it did nothing. ``close()`` is the polite one; when the user
        keeps the window (edits on the Create page), nothing is opened and the status line
        says why."""
        if not self.close():
            self._say(MSG_STAYS_OPEN, "warn")
            return False
        self._ask_host("open_settings", section)
        return True

    def _sign_in(self) -> bool:
        return False if self._closed else self._open_settings(SIGN_IN_SECTION)

    def _history_entry(self, index=None):
        index = self._history_sel if index is None else index
        if type(index) is int and 0 <= index < len(self.imports):
            return index, self.imports[index]
        return None, None

    def _can_restore(self, entry) -> bool:
        """An import can be taken back while its backup is there - also one that failed
        half way and still names its backup."""
        backup = entry.get("backup") if isinstance(entry, dict) else None
        return isinstance(backup, str) and bool(backup) and backup in self._backups \
            and entry.get("status") in ("applied", "failed")

    def _paint_history_buttons(self) -> None:
        _index, entry = self._history_entry()
        try:
            self.buttons["details"].configure(
                state=tk.NORMAL if entry is not None else tk.DISABLED)
            self.buttons["restore"].configure(
                state=tk.NORMAL if self._can_restore(entry) else tk.DISABLED)
        except tk.TclError:
            pass

    def _history_picked(self, _event=None) -> None:
        try:
            picked = self.history_list.selection()
        except tk.TclError:
            return
        index = int(picked[0][1:]) if picked else None      # our own "h<index>"
        if index is not None and index != self._history_sel:
            self.show_details(index)

    def show_details(self, index=None) -> bool:
        """Show what one import did (``index`` into ``imports``, newest first; ``None``:
        the selected one). False when there is no such import."""
        if self._closed:
            return False
        index, entry = self._history_entry(index)
        text = self._details["history"]
        if entry is None:
            self._history_sel = None
            self._write(text, [(PICK_AN_IMPORT if self.imports else NO_HISTORY, "dim")])
            self._paint_history_buttons()
            return False
        self._history_sel, self._report_showing = index, False
        try:
            if tuple(self.history_list.selection()) != (f"h{index}",):
                self.history_list.selection_set(f"h{index}")
            self.history_list.focus(f"h{index}")    # the row the arrow keys move from
        except tk.TclError:
            pass
        self._write(text, self._summary_parts(result_summary(entry)), subject=("entry", index))
        self._offer_sign_in(False)
        self._paint_history_buttons()
        return True

    def ask_restore(self, index=None) -> bool:
        """Ask how to put back the settings from before an import - now, at the next
        launch, or not at all - and stage it."""
        _index, entry = self._history_entry(index)
        if self._closed or not self._can_restore(entry):
            return False
        when = _when(entry.get("at")) or "that day"
        choice = self._confirm("Restore settings", MSG_RESTORE.format(when=when),
                               [("Restore and restart", "now"),
                                ("Restore at next launch", "later"), ("Cancel", None)])
        return self.restore(entry["backup"], choice) if choice in ("now", "later") else False

    def restore(self, backup_name, when) -> bool:
        """Stage putting a backup back: ``when`` = ``now`` (then restart) or ``later``.
        True when it is staged."""
        if self._closed or when not in ("now", "later"):
            return False
        try:
            known = {backup.get("name") for backup in group_state.list_backups(self.app_dir)}
        except Exception:
            known = set()
        if not isinstance(backup_name, str) or backup_name not in known:
            self._say(MSG_NO_BACKUP, "warn")
            return False
        if not self._replace_pending_ok():
            return False
        try:
            group_apply.stage_restore(self.app_dir, backup_name)
        except Exception as exc:
            self._say(group_apply.stage_error_text(exc), "warn")
            return False
        if when == "now" and self._restart():
            return True
        if not self._closed and when == "later":
            self._load_history()
            self._say(MSG_RESTORE_STAGED, "ok")
        return True

    def restart_now(self) -> bool:
        """The banner's button: restart, so that what is staged is applied."""
        return False if self._closed else self._restart()

    def cancel_pending(self) -> bool:
        """Take back what is staged. True when something was."""
        if self._closed:
            return False
        what = self._pending_word(self.pending)     # as the button that was pressed says
        try:
            done = bool(group_state.cancel_pending(self.app_dir))
        except group_state.StateApplyingError as exc:
            self._say(group_apply.stage_error_text(exc), "warn")
            return False
        except Exception:               # it is there and could not be removed
            self._say(MSG_NOT_CANCELLED.format(what=what), "warn")
            return False
        self._load_history()
        self._say(MSG_CANCELLED.format(what=what) if done else MSG_NOTHING_PENDING,
                  "ok" if done else "info")
        return done

    def _attention_section(self, index=None):
        index = self._attention_sel if index is None else index
        if type(index) is int and 0 <= index < len(self.attention_flags):
            return self.attention_flags[index].get("section")
        return None

    def open_attention(self, index=None) -> bool:
        """Show the Settings section of an attention flag (the selected one). This window
        closes first (see ``_open_settings``); False when there is no such flag, or the
        user kept the window."""
        section = self._attention_section(index)
        if self._closed or not isinstance(section, str):
            return False
        return self._open_settings(section)

    def _dismiss_selected(self) -> bool:
        return self.dismiss(self._attention_section())

    def dismiss(self, section) -> bool:
        """Drop the attention flags of a Settings section. False when nothing was dropped
        - and when it could not be saved (another FCTool is applying an import): the row
        then stays as it is, and the status line says so."""
        if self._closed or not isinstance(section, str):
            return False
        listed = any(flag.get("section") == section for flag in self.attention_flags)
        try:
            done = group_apply.clear_attention(self.app_dir, section) is True
        except Exception:
            done = False
        if done:
            self._load_history()
            self._say("")
        elif listed:
            self._say(MSG_BUSY, "warn")
        return done


def min_size(scaling):
    """The window's smallest (width, height) at ``tk scaling`` ``scaling``.

    The text is sized in points, so it grows with the scaling, and above 100%
    (``BASE_SCALING``) the height grows ``MIN_GROWTH`` times as fast: the Import page's
    four stacked doctrine choices and the two lines of text under them grow faster than
    the scaling (the long choice wraps to more lines). Measured on the hidden desktop,
    narrowest pane: 611 px needed at 1.51, 633 at 1.6, 696 at 1.75, 792 at 2.0. The
    width stays ``MIN_SIZE``'s (the long choice wraps instead). Up to 100% it is
    ``MIN_SIZE`` exactly; a scaling that is not a positive number counts as 100%."""
    try:
        scaling = float(scaling)
    except (TypeError, ValueError):
        scaling = BASE_SCALING
    if not scaling > 0 or math.isinf(scaling):
        scaling = BASE_SCALING
    width, height = MIN_SIZE
    grown = math.ceil(round(height * (1 + MIN_GROWTH * (scaling / BASE_SCALING - 1)), 6))
    return width, max(height, grown)


def fit_to_screen(height, screen_height):
    """``height`` no taller than a screen (or work area) ``screen_height`` tall can show:
    at most ``screen_height - SCREEN_ROOM`` (the title bar, the frame and the placement
    margins), never under ``MIN_SIZE``'s height - what the window always allowed. A
    ``screen_height`` that is not a positive number caps nothing."""
    try:
        screen_height = int(screen_height)
    except (TypeError, ValueError, OverflowError):
        return height
    if screen_height <= 0:
        return height
    return max(MIN_SIZE[1], min(height, screen_height - SCREEN_ROOM))


def _screen_height(win, parent):
    """The height of the work area (no taskbar) of the monitor the window opens on, else
    the primary screen's height, else None. Never raises."""
    area = parent_work_area(win, parent)
    if area:
        return area[3] - area[1]
    try:
        return int(win.winfo_screenheight())
    except (tk.TclError, ValueError):
        return None


def open_window(parent, host, *, view="create", report=None, pick=False):
    """Open the Group configuration window over ``parent``; returns its Toplevel, which
    carries the controller as ``win._gc`` (see ``_Controller``).

    ``host``    a ``GroupHost``: everything the window needs from the app.
    ``view``    the page to start on: ``create`` | ``import`` | ``history``.
    ``report``  an import report (``group_apply``): with ``view="history"`` the page opens
                on its result, the report is marked as seen and - while no character is
                signed in - the next step "Sign in your characters" is offered. For a
                window that is ALREADY open, ``win._gc.show_report(report)`` does the same.
    ``pick``    with ``view="import"``: the file dialog opens as soon as the window is idle.

    The keyboard starts on the first control of the page (``first_control``). "Sign in
    your characters" and "Open in Settings" CLOSE this window before they call
    ``host.open_settings`` - it would cover the Settings tab otherwise - so the host needs
    no special case for them; ``win._gc.close()`` is the polite close for the host's own
    use and answers ``True`` / ``False``.

    The window is created and handed to ``make_modal`` in THIS function - the shape
    ``tests/test_dialog_placement.py`` looks for - without the application input hold; the
    window X is wired to the same ``close`` as Escape. The caller owns the one-window guard.
    """
    win = tk.Toplevel(parent)
    try:
        win.title(TITLE)
        ctl = _Controller(win, host, report=report)
        make_modal(win, parent, on_cancel=ctl.close, grab=False)
        win.protocol("WM_DELETE_WINDOW", ctl.close)
        # The smallest size grows with the scaling, never past what the screen shows;
        # the window opens no smaller than it (and no taller than the screen either).
        width, low = min_size(win.tk.call("tk", "scaling"))
        screen = _screen_height(win, parent)
        opening_width, opening = (int(n) for n in GEOMETRY.split("x"))
        win.geometry("%dx%d" % (opening_width,
                                fit_to_screen(max(opening, low), screen)))
        win.minsize(width, fit_to_screen(low, screen))
        ctl.build()
        if view == "history" and ctl.report is not None:
            ctl.show_report(ctl.report)     # the one road to a report, new window or old
        else:
            ctl.show_page(view if view in dict(PAGES) else "create")
    except Exception:
        try:
            win.destroy()               # never leave a half-built window behind
        except tk.TclError:
            pass
        raise
    win._gc = ctl
    if pick:
        ctl.pick_soon()
    return win
