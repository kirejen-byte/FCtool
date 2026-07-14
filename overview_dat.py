"""Phase-2 read-only decode of live EVE overview settings from ``core_user_*.dat``.

Reads the account-scoped overview out of the blue-marshal settings blob and
translates it into the canonical :class:`overview_schema.OverviewPack` — the
same model the YAML import path produces — so a live account can be compared
against a stored pack (drift) or imported as a new pack.

Read-only: files are copied to a temp location before decoding (the client may
be mid-flush) and the source ``.dat`` is never written. Facts:
``docs/superpowers/research/2026-07-12-overview-manager.md`` §B.4 (live shapes +
the live↔export correspondence table).

Public surface:
    list_accounts()                         -> [(account_id, path, mtime)]
    read_overview(core_user_path)           -> (OverviewPack, LiveNotes)
    live_fingerprint(core_user_path)        -> sha1 hex of the translated pack
    OverviewDatError(account_id, detail)    -- one bad account never blocks others
    PALETTE                                 -- RGBA -> color-name (5-name floor)
"""
from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass, field

import blue_marshal
import eve_paths
import overview_schema


# --- RGBA -> color-name palette -------------------------------------------
# PROVENANCE: derived at dev time (2026-07-13) by joining the golden in-game
# export ``docs/superpowers/spikes/2026-07-12-overview-golden/SecFCTool.yaml``
# (stateColorsNameList: state-key -> color NAME) against the live ``stateColors``
# (state-key -> RGBA 4-tuple) decoded from the owner's real core_user files.
# All 15 golden keys joined with ZERO conflicts and the RGBA values were
# IDENTICAL across three independent accounts (core_user_2499436 / _12447433 /
# _7335517), so these five are a stable, cross-account-verified mapping. This is
# the {blue, darkBlue, orange, red, white} floor the research pinned (R §U6);
# extend it as future packs surface more names. Unknown RGBA falls back to the
# nearest-Euclidean name and the exact RGBA is recorded in LiveNotes.
PALETTE = {
    "blue":     (0.2, 0.5, 1.0, 1.0),
    "darkBlue": (0.0, 0.15, 0.6, 1.0),
    "orange":   (1.0, 0.35, 0.0, 1.0),
    "red":      (0.75, 0.0, 0.0, 1.0),
    "white":    (0.7, 0.7, 0.7, 1.0),
}
_PALETTE_ITEMS = tuple(PALETTE.items())
# squared-distance threshold under which a live RGBA counts as an exact palette
# hit (guards against float-repr drift; real hits are distance 0).
_COLOR_TOL2 = 1e-6

_PRESET_INNER_ORDER = ("alwaysShownStates", "filteredStates", "groups")


class OverviewDatError(Exception):
    """A single account's overview could not be decoded/translated. Carries the
    account id so callers can render a per-account 'unreadable' row without
    letting one bad file block the others."""

    def __init__(self, account_id, detail):
        self.account_id = account_id
        self.detail = detail
        super().__init__(f"account {account_id}: {detail}")


@dataclass
class LiveNotes:
    """Live facts that don't map into the portable canonical pack: misc boolean
    settings (which the export folds into userSettings but the owner's golden
    leaves empty) and the exact RGBA of any state color with no palette name."""
    account_id: int | None = None
    active_preset: str | None = None
    booleans: dict = field(default_factory=dict)
    unknown_state_colors: dict = field(default_factory=dict)


# --- decode helpers --------------------------------------------------------
def _dec(v):
    """bytes/Buffer -> str (utf-8, latin-1 fallback); str/None pass through."""
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8")
        except UnicodeDecodeError:
            return v.decode("latin-1")
    return v


def _name(v):
    """Decode a name/label and repeat-unescape the CCP HTML-entity export bug
    until stable (live data carries singly- and doubly-escaped markup)."""
    s = _dec(v)
    if not isinstance(s, str):
        return s
    prev = None
    for _ in range(10):                       # bounded; converges in <=2 in practice
        if s == prev:
            break
        prev = s
        s = overview_schema.unescape_markup(s)
    return s


def _unwrap(v):
    """Strip one FILETIME wrapper. Every direct ``overview[k]`` child is a
    ``(win_filetime_100ns, value)`` 2-tuple (R §B.4); nested values are not
    re-wrapped, so unwrap exactly once."""
    if (isinstance(v, tuple) and len(v) == 2
            and isinstance(v[0], int) and not isinstance(v[0], bool)):
        return v[1]
    return v


def _str_keyed(d):
    """New dict with keys decoded to str (tab/preset inner dicts have MIXED
    bytes/str keys). Non-dict input -> {}."""
    if not isinstance(d, dict):
        return {}
    return {_dec(k): v for k, v in d.items()}


def _rgb(v):
    """Copy an [r,g,b] float list (or return None)."""
    if isinstance(v, (list, tuple)):
        return [float(c) for c in v]
    return None


def _label_value(v):
    """Decode one ship-label pair value: bytes -> str, lists copied (with any
    bytes elements decoded), everything else — ints, bools, floats, None —
    verbatim so the wire shapes match the client's own export exactly."""
    if isinstance(v, bytes):
        return _dec(v)
    if isinstance(v, (list, tuple)):
        return [_dec(x) if isinstance(x, bytes) else x for x in v]
    return v


def _dist2(a, b):
    n = min(len(a), len(b))
    return sum((a[i] - b[i]) ** 2 for i in range(n))


def _rgba_to_name(rgba):
    """Return (color_name, exact_rgba_or_None). Nearest-Euclidean palette match;
    within tolerance -> a clean hit (exact=None); otherwise the nearest name
    plus the exact RGBA to record in LiveNotes."""
    rgba = tuple(float(c) for c in rgba)
    best_name, best_d2 = None, None
    for name, val in _PALETTE_ITEMS:
        d2 = _dist2(val, rgba)
        if best_d2 is None or d2 < best_d2:
            best_name, best_d2 = name, d2
    if best_d2 is not None and best_d2 <= _COLOR_TOL2:
        return best_name, None
    return best_name, rgba


def _sorted_state_pairs(mapping, value_fn):
    """Translate a tuple-keyed {(kind, id): x} dict into wire ``[["kind_id", v]]``
    pairs, deterministically sorted lexicographically by the composite key (the
    order the client itself emits — verified against restoreData.data)."""
    out = []
    for key, val in (mapping or {}).items():
        if not (isinstance(key, tuple) and len(key) == 2):
            continue
        kind, sid = key
        wire_key = f"{_dec(kind)}_{sid}"
        out.append((wire_key, value_fn(wire_key, val)))
    out.sort(key=lambda p: p[0])
    return [[k, v] for k, v in out]


# --- account discovery -----------------------------------------------------
def _account_id_from_path(path):
    stem = os.path.basename(path)
    if stem.startswith("core_user_") and stem.endswith(".dat"):
        mid = stem[len("core_user_"):-len(".dat")]
        if mid.isdigit():
            return int(mid)
    return None


def list_accounts(localappdata=None):
    """All account overview files across Tranquility settings profiles.

    Returns ``[(account_id, path, mtime_or_None)]`` sorted by (account_id, path).
    ``mtime`` is a POSIX timestamp (float) or ``None`` if the file vanished.
    """
    out = []
    for settings_dir in eve_paths.tranquility_settings_dirs(localappdata):
        for account_id, path in eve_paths.list_core_user_files(settings_dir):
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = None
            out.append((account_id, path, mtime))
    out.sort(key=lambda t: (t[0], t[1]))
    return out


# --- decode + translate ----------------------------------------------------
def _read_copy(path):
    """Snapshot ``path`` to a temp file and return its bytes. The client may be
    mid-flush; copying first gives a stable read and never touches the source."""
    fd, tmp = tempfile.mkstemp(prefix="fctool_ov_", suffix=".dat")
    try:
        os.close(fd)
        shutil.copy2(path, tmp)
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def read_overview(core_user_path, account_id=None):
    """Decode + translate one account's live overview into ``(OverviewPack, LiveNotes)``.

    Raises :class:`OverviewDatError` (never a bare decode exception) on any
    failure — unreadable/hard-opcode/no-overview — so callers can show a
    per-account error row.
    """
    if account_id is None:
        account_id = _account_id_from_path(core_user_path)

    try:
        data = _read_copy(core_user_path)
    except OSError as e:
        raise OverviewDatError(account_id, f"could not read file: {e}") from e

    try:
        obj, reader = blue_marshal.loads(data)
    except blue_marshal.HardOpcode as e:
        raise OverviewDatError(
            account_id,
            f"unsupported opcode {blue_marshal.opcode_name(e.ty)} at byte {e.pos}",
        ) from e
    except Exception as e:                      # malformed marshal -> unreadable row
        raise OverviewDatError(account_id, f"decode failed: {e}") from e

    if not isinstance(obj, dict):
        raise OverviewDatError(account_id, "top-level settings object is not a dict")
    ov = _unwrap(obj.get(b"overview"))
    if not isinstance(ov, dict):
        raise OverviewDatError(
            account_id, "no 'overview' settings in this account file")

    try:
        pack, notes = _translate(ov, account_id)
    except OverviewDatError:
        raise
    except Exception as e:                      # translation bug -> unreadable, not crash
        raise OverviewDatError(account_id, f"translate failed: {e}") from e
    return pack, notes


def _translate(ov, account_id):
    notes = LiveNotes(account_id=account_id)
    wire = {}

    def child(key):
        return _unwrap(ov.get(key))

    # -- presets: overviewProfilePresets {name: {groups, filtered, alwaysShown}}
    presets_src = child(b"overviewProfilePresets")
    if isinstance(presets_src, dict):
        entries = []
        for raw_name, raw_val in presets_src.items():
            inner = _str_keyed(raw_val)
            pairs = []
            for k in _PRESET_INNER_ORDER:
                if k in inner:
                    pairs.append([k, list(inner[k] or [])])
            # preserve any unexpected inner keys verbatim (defensive; none seen)
            for k, v in inner.items():
                if k not in _PRESET_INNER_ORDER:
                    pairs.append([k, v])
            entries.append([_name(raw_name), pairs])
        wire["presets"] = entries

    # -- tabs: tabsettings_new {int_index: {bracket,color,name,overview,tabColumns}}
    tabs_src = child(b"tabsettings_new")
    if isinstance(tabs_src, dict):
        entries = []
        for idx in sorted(tabs_src, key=lambda k: (0, k) if isinstance(k, int)
                          else (1, str(k))):
            tab = _str_keyed(tabs_src[idx])
            pairs = [
                ["bracket", _name(tab.get("bracket")) or overview_schema.BRACKET_SHOW_ALL],
                ["color", _rgb(tab.get("color"))],
                ["name", _name(tab.get("name")) or ""],
                ["overview", _name(tab.get("overview")) or ""],
            ]
            if tab.get("tabColumns") is not None:
                pairs.append(["tabColumns", [_dec(c) for c in tab["tabColumns"]]])
            entries.append([idx, pairs])
        wire["tabSetup"] = entries

    # -- appearance order/state lists: live keys carry a '2' suffix
    for live_key, wire_key in (
        (b"flagOrder2", "flagOrder"), (b"flagStates2", "flagStates"),
        (b"backgroundOrder2", "backgroundOrder"), (b"backgroundStates2", "backgroundStates"),
    ):
        src = child(live_key)
        if isinstance(src, (list, tuple)):
            wire[wire_key] = list(src)

    # -- stateBlinks: tuple keys (kind, id) -> bool ; deterministic lexo sort
    blinks = child(b"stateBlinks")
    if isinstance(blinks, dict):
        wire["stateBlinks"] = _sorted_state_pairs(
            blinks, lambda k, v: bool(v))

    # -- stateColors: tuple keys (kind, id) -> RGBA ; map to palette name
    colors = child(b"stateColors")
    if isinstance(colors, dict):
        def color_val(wire_key, rgba):
            name, exact = _rgba_to_name(rgba)
            if exact is not None:
                notes.unknown_state_colors[wire_key] = exact
            return name
        wire["stateColorsNameList"] = _sorted_state_pairs(colors, color_val)

    # -- columns: bytes -> str
    col_order = child(b"overviewColumnOrder")
    if isinstance(col_order, (list, tuple)):
        wire["columnOrder"] = [_dec(c) for c in col_order]
    cols = child(b"overviewColumns")
    if isinstance(cols, (list, tuple)):
        wire["overviewColumns"] = [_dec(c) for c in cols]

    # -- ship labels: list-of-dicts -> shipLabelOrder (live slot order) +
    #    shipLabels entries carrying EVERY pair key present on the live label
    #    dict, alphabetically — styling (bold/color/fontsize/italic/underline)
    #    lives IN the pack, exactly as the client's own export emits it (golden
    #    F9: styling keys on 5 of 7 entries; per-label key sets == sorted live
    #    keys). Keeping styling in the pack makes .dat->pack->YAML lossless and
    #    keeps drift fingerprints comparable with YAML-sourced packs.
    labels_src = child(b"shipLabels")
    if isinstance(labels_src, (list, tuple)):
        order = []
        wire_labels = []
        for raw in labels_src:
            d = _str_keyed(raw)
            ltype = _name(d.get("type"))
            order.append(ltype)
            pairs = [[k, ltype if k == "type" else _label_value(d[k])]
                     for k in sorted(d)]
            wire_labels.append([ltype, pairs])
        # The export re-sorts the shipLabels SECTION (None first, then by type
        # name) while slot order rides shipLabelOrder — verified against the
        # golden (entry order None, alliance, ..., ship type). Match it so a
        # .dat-sourced pack fingerprints identically to its YAML twin.
        wire_labels.sort(key=lambda e: (e[0] is not None, str(e[0])))
        wire["shipLabelOrder"] = order
        wire["shipLabels"] = wire_labels

    # -- misc booleans -> LiveNotes (NOT userSettings; the golden's is empty)
    for k, v in ov.items():
        uv = _unwrap(v)
        if isinstance(uv, bool):
            notes.booleans[_dec(k)] = uv
    active = child(b"activeOverviewPreset")
    if active is not None:
        notes.active_preset = _name(active)

    pack = overview_schema.from_wire(wire)
    pack.user_settings = None      # live misc bools live in LiveNotes, not here
    return pack, notes


def live_fingerprint(core_user_path, account_id=None):
    """``overview_schema.fingerprint`` of the translated live pack — compare
    against a stored pack's fingerprint to detect drift."""
    pack, _ = read_overview(core_user_path, account_id=account_id)
    return overview_schema.fingerprint(pack)
