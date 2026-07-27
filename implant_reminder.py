"""Implant-removal reminder — trigger/state engine. No Tk, no direct network.

Research: ``docs/superpowers/spikes/2026-07-25-implant-reminder/RESEARCH.md``.

The feature reminds a pilot who is carrying expensive implants to pull them
after docking back at the staging structure following a fleet. It is
**default ON** (``config['implant_reminder']['enabled']``; flipped from OFF to
ON 2026-07-26 by owner request, now that the toast has been seen working over
a live client). Defaulting on is safe rather than merely convenient: the
feature needs ``esi-clones.read_implants.v1``, a scope no pre-existing
character token carries until the owner re-authorises it in Settings, so on
an upgrading install the reminder is "on" but genuinely inert for every
not-yet-reauthorised character — no ESI call, no toast, nothing logged (see
the scope check in the poller hook). While ``enabled`` is explicitly
``False`` — still the one master gate — nothing in here runs at all, no ESI
call is made and the scope is never exercised.

Split of responsibility:

* everything above ``ImplantReminder`` is **pure** — plain data in, plain data
  out, no threads, no I/O (``load_implant_table`` reads one bundled JSON file
  and is the single exception; every classifier takes the table as an argument
  so the tests never touch disk);
* ``ImplantReminder`` is the thin orchestrator the ESI location poller calls.
  It is Tk-free: the ESI adapter and the "show the toast" callback are both
  injected, and the caller is responsible for marshalling the callback onto the
  Tk thread (``FCToolGUI._post_ui``).

Design notes that are load-bearing:

* **The dock signal is the existing per-character ESI location poll.** No second
  poller. ``/characters/{id}/location`` already returns ``station_id`` /
  ``structure_id``; the poller used to collapse them into a bool. Keeping the
  ids is the whole trigger. Marginal ESI cost of dock detection: zero.
* **Fire once per dock.** ``ReminderState`` latches per character on the
  undocked→docked-at-staging edge. That is also what stops the 120 s
  ``/implants`` server cache from re-nagging a pilot who just pulled their
  implants. The latch re-arms on an observed undock — but NOT only on that:
  it also re-arms on a poll showing a different dock, and on a poll arriving
  after a blackout longer than ``ReminderState.blind_gap_s``. The dock signal
  is sampled every ~10 s, and requiring the poller to catch the undocked
  moment silently ate reminders whenever it did not (owner report,
  2026-07-26). See ``ReminderState``'s docstring for the full account,
  including the one case that remains undetectable.
* **A character seen for the FIRST TIME while already docked is primed, not
  fired.** Otherwise starting FCTool while parked in staging would nag
  immediately, which is not "you just got back from a fleet".
* **Do NOT copy the decloak alert's focus suppression.** ``_preview_on_decloak``
  suppresses when the character's own client is foreground because the pilot is
  already looking at it. Here that is inverted: the docked character's client
  being foreground is the normal case and exactly when the pilot must see the
  toast.
* **The staging is whatever system sits under Settings > Staging System**
  (``zkillboard.staging_system`` — the same key that drives route-from-staging
  on kill alerts and the MOTD leave-staging guard). Docked at ANY structure or
  station inside that system counts; the reminder is deliberately NOT narrowed
  down to one exact citadel within it. The market/seeding-citadel config lives
  in a wholly separate block and, on a real install, frequently names a
  DIFFERENT system (owner correction, 2026-07-25). Two earlier cuts both
  leaned on that other block anyway — first outright, then as a same-system
  "precision" borrow — and both were silently wrong the moment it named a
  different system: either one narrows "docked anywhere in staging" down to
  "docked in that one structure", which is not what the Settings field
  promises. ``resolve_staging`` reads nothing from that other block at all any
  more — see ``test_module_reads_no_market_config_key`` for the drift guard —
  and its own docstring for the (now much shorter) rung order.
* **A resolved staging target is logged exactly once** (``ImplantReminder``,
  INFO). The failure above was undiagnosable precisely because nothing was ever
  logged; an unconfigured/never-fires resolution must always leave a trace.
* Scope is ``esi-clones.read_implants.v1`` ONLY. ``/clones/`` returns JUMP-clone
  implants — i.e. the ones explicitly NOT plugged in — which answers the wrong
  question, costs a second scope and shares the location poller's rate bucket.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field

from app_log import get_logger
from app_path import bundle_dir, resolve_data_file

log = get_logger(__name__)


# ── config ───────────────────────────────────────────────────────────────────

#: Shape + defaults of ``config['implant_reminder']``. Mirrored in
#: ``default_config.DEFAULT_CONFIG``; kept here too so the engine self-heals a
#: partially-written block (house per-key defaulting, never a deep merge).
DEFAULTS = {
    "enabled": True,             # MASTER GATE (default ON since 2026-07-26) — explicit False = fully inert, zero ESI
    "toast_seconds": 12.0,       # auto-dismiss hold before the fade
    "match_mode": "valuable",    # "valuable" | "any" | "custom"
    "match_names": [],           # substrings, used when match_mode == "custom"
    "min_hardwiring_grade": 5,   # 1..6+, used by "valuable" (see grade_of)
    "staging_scope": "ladder",   # "ladder" | "structure" | "system"
    "staging_structure_id": 0,   # explicit staging OVERRIDE (see resolve_staging)
    "staging_system": "",        # explicit staging OVERRIDE — a system NAME
    "disabled_chars": [],        # lowercased char keys that never get reminded
}

_MATCH_MODES = ("valuable", "any", "custom")
_STAGING_SCOPES = ("ladder", "structure", "system")

#: Hard floor/ceiling on the toast hold so a corrupt config can neither flash
#: the toast for 0 s nor leave it pinned over the client forever.
TOAST_SECONDS_MIN = 3.0
TOAST_SECONDS_MAX = 60.0


def is_enabled(block) -> bool:
    """Is the reminder switched on, given a RAW ``config['implant_reminder']``?

    **The single answer to the master-gate question.** Every caller — the
    Characters-tab tick, the poller hook that decides whether to build the
    engine at all, and ``normalize_config`` itself — routes through here, so
    the UI and the engine can never disagree about whether the feature is on.
    They once did: the tick read ``normalize_config(...)["enabled"]`` (which
    inherits ``DEFAULTS["enabled"]`` for an unspecified block) while the poller
    gate did its own raw ``isinstance(cfg, dict) and cfg.get("enabled")``. The
    moment the default flipped to True, an install whose config.json carries no
    ``implant_reminder`` block at all — the ordinary upgrade case — showed a
    ticked box over a reminder that could never fire. Two independent
    implementations of one predicate is the bug; one is the fix.

    Absent / ``None`` / malformed (a string, a list, an int) and a dict with no
    ``enabled`` key all inherit ``DEFAULTS["enabled"]``; only an explicit,
    falsy ``enabled`` turns it off. Never raises.

    **Cheap by requirement.** This runs on the ESI poller thread for EVERY
    character on EVERY poll, and while the feature is off it is the whole of
    the work done — one isinstance, one dict lookup on ``DEFAULTS`` and one
    ``dict.get``, no allocation. It is deliberately NOT ``normalize_config``,
    which copies a dict, rebuilds two lists and runs ten coercions to answer a
    question that is one lookup wide."""
    if not isinstance(block, dict):
        return bool(DEFAULTS["enabled"])
    return bool(block.get("enabled", DEFAULTS["enabled"]))


def normalize_config(raw) -> dict:
    """Coerce ``config['implant_reminder']`` into a fully-populated, sane dict.

    Never raises and never returns a partial shape: a missing block, a wrong
    type, or a hostile value all degrade to the documented default for that one
    key. Pure — the caller's dict is not mutated.

    One coercion is more than a type fix: ``match_mode == "custom"`` with an
    EMPTY ``match_names`` is downgraded to ``"valuable"``. Custom mode with
    nothing to match against can never fire, and a silent never-fires is the
    exact failure class this module is hardened against. The alternative reading
    ("empty list => any implant") is rejected deliberately: it turns a
    half-finished edit into the loudest possible setting, against the module's
    stated error direction — a missed reminder is cheap, a spurious nag is what
    gets the feature switched off. The stored config is untouched; only the
    effective mode changes, and the normalized dict says so honestly."""
    src = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULTS)
    out["match_names"] = []
    out["disabled_chars"] = []

    # Delegated, not duplicated: is_enabled is THE master-gate predicate and the
    # poller's cheap gate calls it directly (see its docstring for the
    # UI-says-on/engine-says-off bug that two implementations caused).
    out["enabled"] = is_enabled(src)

    try:
        secs = float(src.get("toast_seconds", DEFAULTS["toast_seconds"]))
    except (TypeError, ValueError):
        secs = float(DEFAULTS["toast_seconds"])
    out["toast_seconds"] = max(TOAST_SECONDS_MIN, min(secs, TOAST_SECONDS_MAX))

    names = src.get("match_names")
    if isinstance(names, (list, tuple)):
        out["match_names"] = [str(n).strip() for n in names if str(n).strip()]

    mode = str(src.get("match_mode", DEFAULTS["match_mode"]) or "").strip().lower()
    if mode not in _MATCH_MODES:
        mode = DEFAULTS["match_mode"]
    if mode == "custom" and not out["match_names"]:
        mode = DEFAULTS["match_mode"]
    out["match_mode"] = mode

    scope = str(src.get("staging_scope", DEFAULTS["staging_scope"]) or "").strip().lower()
    out["staging_scope"] = scope if scope in _STAGING_SCOPES else DEFAULTS["staging_scope"]

    try:
        grade = int(src.get("min_hardwiring_grade", DEFAULTS["min_hardwiring_grade"]))
    except (TypeError, ValueError):
        grade = int(DEFAULTS["min_hardwiring_grade"])
    out["min_hardwiring_grade"] = max(0, min(grade, 9))

    out["staging_structure_id"] = _int_or_zero(src.get("staging_structure_id"))
    out["staging_system"] = str(src.get("staging_system") or "").strip()

    chars = src.get("disabled_chars")
    if isinstance(chars, (list, tuple)):
        out["disabled_chars"] = [str(c).strip().lower() for c in chars if str(c).strip()]

    return out


# ── staging resolution ladder ────────────────────────────────────────────────

#: Which config key produced a ``StagingTarget``. Purely diagnostic — it exists
#: so the one-shot INFO line can name the rung that won, because the way this
#: resolution fails is by quietly picking the WRONG staging and then never
#: firing (see the module docstring).
RUNG_NONE = "nothing configured"
RUNG_OVERRIDE_STRUCTURE = "implant_reminder.staging_structure_id (override)"
RUNG_OVERRIDE_SYSTEM = "implant_reminder.staging_system (override)"
RUNG_FC_SYSTEM = "zkillboard.staging_system (Settings > Staging System)"


@dataclass(frozen=True)
class StagingTarget:
    """What "docked at staging" resolves to for the current config.

    ``kind`` is ``"structure"`` | ``"system"`` | ``"none"`` (``at_staging`` also
    understands a ``"station"`` kind for a hand-built target, but
    ``resolve_staging`` itself never produces one — the only exact-dock source
    left is the structure override). ``"none"`` means nothing is configured —
    the feature stays inert rather than firing on every dock anywhere. ``rung``
    names the config key it came from and is diagnostic only — never compare
    on it. ``label`` is the human system name when the target came from one
    (both staging-system rungs do); it exists purely for the log line."""
    kind: str = "none"
    value: int = 0
    rung: str = RUNG_NONE
    label: str = ""

    @property
    def configured(self) -> bool:
        return self.kind != "none" and self.value > 0

    def describe(self) -> str:
        """One-line human summary for the log."""
        if not self.configured:
            return "NOT CONFIGURED — the reminder can never fire"
        ident = f"{self.label} ({self.value})" if self.label else str(self.value)
        return f"{self.kind} {ident} via {self.rung}"


def _int_or_zero(value) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return 0
    return out if out > 0 else 0


def _system_id_of(name, resolve_system_name) -> int:
    """A system NAME -> id through the injected resolver; 0 for blank/unknown/raising."""
    text = str(name or "").strip()
    if not text or resolve_system_name is None:
        return 0
    try:
        return _int_or_zero(resolve_system_name(text))
    except Exception:
        return 0


def resolve_staging(config, resolve_system_name=None,
                    scope: str = "ladder") -> StagingTarget:
    """Resolve the staging identity from the app config.

    **The staging is the system named in Settings > Staging System
    (``zkillboard.staging_system``) — nothing else.** Docked at ANY structure
    or station inside that system counts as "at staging"; the reminder is
    deliberately NOT narrowed down to one exact citadel within it, even when
    one happens to be configured elsewhere (owner correction, 2026-07-25). Two
    earlier cuts both leaned on the separate market/seeding-citadel config
    block instead — first outright, then as a same-system "precision" borrow —
    and both were silently wrong the moment that other block named a different
    system, which it frequently does. This function reads nothing from that
    other block at all any more (drift-guarded by
    ``test_module_reads_no_market_config_key``).

    Rungs, in order:

    1. **Explicit override** — ``implant_reminder.staging_structure_id`` (exact
       dock) or ``implant_reminder.staging_system`` (a NAME). Either one set
       means the owner has stated the answer outright: the normal resolution
       below is skipped entirely. If the chosen ``scope`` cannot express the
       override that IS set, the result is unconfigured rather than a
       fall-through to the normal system — once an override is set it IS the
       staging. A system NAME the resolver cannot resolve counts as UNSET, so a
       typo degrades to the normal resolution instead of stranding the
       feature.
    2. **Settings > Staging System, system-wide** —
       ``zkillboard.staging_system`` resolved through ``resolve_system_name``.
       Nothing narrows it further: any structure or station dock inside that
       system satisfies it (see ``at_staging``).

    With nothing configured at all (no override, and ``zkillboard.staging_system``
    empty or unresolvable), the result is unconfigured — never a fallback to
    any other staging-shaped config block.

    ``scope`` narrows what the override rung may express: ``"structure"``
    accepts only an exact-structure override (and, since rung 2 is system-only,
    can therefore NEVER be satisfied by the normal resolution); ``"system"``
    accepts only a system override or the normal resolution. ``"ladder"`` (the
    default) accepts whichever the override actually is, then falls through to
    rung 2. Pure apart from the injected name resolver; never raises."""
    cfg = config if isinstance(config, dict) else {}
    zkill = cfg.get("zkillboard") if isinstance(cfg.get("zkillboard"), dict) else {}
    own = cfg.get("implant_reminder") if isinstance(cfg.get("implant_reminder"), dict) else {}
    scope = scope if scope in _STAGING_SCOPES else "ladder"
    want_exact = scope in ("ladder", "structure")
    want_system = scope in ("ladder", "system")

    # 1 ── an explicit override wins outright.
    own_structure = _int_or_zero(own.get("staging_structure_id"))
    own_system_name = str(own.get("staging_system") or "").strip()
    own_system = _system_id_of(own_system_name, resolve_system_name)
    if own_structure or own_system:
        if own_structure and want_exact:
            return StagingTarget("structure", own_structure, RUNG_OVERRIDE_STRUCTURE)
        if own_system and want_system:
            return StagingTarget("system", own_system, RUNG_OVERRIDE_SYSTEM,
                                 own_system_name)
        return StagingTarget()

    # 2 ── Settings > Staging System, system-wide. No other config block is
    # ever consulted here (see the docstring above).
    if want_system:
        fc_name = str(zkill.get("staging_system") or "").strip()
        fc_system = _system_id_of(fc_name, resolve_system_name)
        if fc_system:
            return StagingTarget("system", fc_system, RUNG_FC_SYSTEM, fc_name)
    return StagingTarget()


# ── location reading ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DockState:
    """The three fields of ``GET /characters/{id}/location`` that matter here.

    ``station_id`` is present for NPC stations, ``structure_id`` for player
    structures, neither while in space. All three are normalized to ints
    (0 = absent) so downstream comparisons never mix None and 0."""
    solar_system_id: int = 0
    station_id: int = 0
    structure_id: int = 0

    @property
    def docked(self) -> bool:
        return bool(self.station_id or self.structure_id)


def dock_state(loc) -> DockState:
    """Build a ``DockState`` from a raw ESI location dict (or anything else —
    a None/garbage payload yields the in-space zero state)."""
    src = loc if isinstance(loc, dict) else {}
    return DockState(
        solar_system_id=_int_or_zero(src.get("solar_system_id")),
        station_id=_int_or_zero(src.get("station_id")),
        structure_id=_int_or_zero(src.get("structure_id")),
    )


def dock_identity(state) -> tuple:
    """The identity of the dock in ``state``, or ``()`` when it is not known.

    ``()`` means "no information" — an in-space state, a caller that passed no
    ``DockState`` at all — and is deliberately never equal to a real identity
    AND never *unequal* in a way the latch acts on (see ``ReminderState.observe``).
    Pure."""
    if not isinstance(state, DockState) or not state.docked:
        return ()
    return (state.solar_system_id, state.station_id, state.structure_id)


def at_staging(state: DockState, target: StagingTarget) -> bool:
    """True only when the character is DOCKED and that dock matches ``target``.

    The system-level rung still requires an actual dock — sitting in space in
    the staging system is not "docked back at staging"."""
    if not isinstance(state, DockState) or not target.configured:
        return False
    if not state.docked:
        return False
    if target.kind == "structure":
        return state.structure_id == target.value
    if target.kind == "station":
        return state.station_id == target.value
    if target.kind == "system":
        return state.solar_system_id == target.value
    return False


# ── implant classification (fully offline) ───────────────────────────────────
#
# Implant type NAMES resolve with zero ESI calls: the bundled SDE type table
# (fit_types.json) carries category 20 (Implant) in full, deliberately — see
# tools/gen_fit_types.py. The classifier therefore costs nothing and needs no
# network, which is what lets the reminder filter on WHAT is plugged in rather
# than nagging every pilot who permanently flies cheap +3 attribute implants.

IMPLANT_CATEGORY_ID = 20
#: SDE groupIDs inside category 20 (names from the bundled inv_groups.json).
GROUP_CYBERIMPLANT = 300      # "Cyberimplant" — the High-/Mid-/Low-grade SETS
GROUP_BOOSTER = 303           # "Booster" — combat boosters + cerebral accelerators
GROUP_CYBER_LEARNING = 745    # "Cyber Learning" — the attribute implants

#: Never worth a reminder under ANY setting. Boosters are not implants at all
#: (they are consumed, not unplugged). "Cyber Learning" is NOT in here: it is
#: gated by name tier instead — see ``_LEARNING_TOP_TIER``.
NEVER_GROUPS = frozenset({GROUP_BOOSTER})

#: The one tier of "Cyber Learning" (745) that IS worth pulling. Read off the
#: bundled SDE's 25 group-745 names, not assumed: the group runs
#: ``Limited X`` (+1) -> ``Limited X - Beta`` (+2) -> ``X - Basic`` (+3) ->
#: ``X - Standard`` (+4) -> ``X - Improved`` (+5), five families deep (Ocular
#: Filter / Memory Augmentation / Neural Boost / Cybernetic Subprocessor /
#: Social Adaptation Chip). Only the ``- Improved`` line is the +5 — e.g.
#: typeID 10217 "Ocular Filter - Improved" at ~110M ISK, so a full +5 set is
#: ~550M-1B ISK in the pilot's head, close to the worst case this feature
#: exists for. Excluding the whole group (the first cut) meant that pilot was
#: NEVER reminded in the default "valuable" mode. Everything below +5 stays
#: excluded: +4s and under are the fly-them-permanently kit whose reminder
#: would be pure noise. ``min_hardwiring_grade`` deliberately does not reach
#: this group — these names carry no hardwiring code.
_LEARNING_TOP_TIER = re.compile(r"-\s*improved\s*$", re.IGNORECASE)

#: Trailing hardwiring code, e.g. "... Shield Upgrades SU-606" -> "606".
_HARDWIRING_CODE = re.compile(r"\b[A-Z]{2,4}-(\d{3,4})$")
#: Same code, DASH-LESS: the legacy Zainou 'Sharpshooter' missile line ships as
#: "... ZMX10" / "ZMX11" / "ZMX100" / "ZMX110" / "ZMX1000" / "ZMX1100" (group
#: 746, typeIDs 3149-3151 + 27204-27206). Without this they parse as code-LESS
#: and are therefore treated as named rares — six cheap implants nagging in the
#: default mode, which is the expensive error direction. Kept as a SEPARATE
#: pattern rather than making the dash optional so the dashed form's behaviour
#: is bit-for-bit unchanged: verified against the bundled SDE, these two
#: patterns together reclassify exactly those 6 types and nothing else (the
#: dash-optional variant additionally swallowed "Genolution 'Auroral' AU-79").
_HARDWIRING_CODE_NODASH = re.compile(r"\b[A-Z]{2,4}(\d{2,4})$")


def grade_of(name: str):
    """Crude offline value proxy for a HARDWIRING: the last digit of its code.

    Most hardwiring families are numbered ``XX-N01 … XX-N06`` where the final
    digit is the tier and the 5/6 tiers are the expensive ones, so this ranks
    them without any price lookup. Returns ``None`` when the name carries no
    parseable code — which is deliberate and is treated as VALUABLE by
    ``is_valuable``, because the code-less implants are the named/faction rares
    (Zor's, Numon Family Heirloom, Ogdin's Eye, Michi's, Pashan's, Genolution,
    the Mindlinks, the Special Ops Field Enhancers).

    Known false negatives, accepted: a handful of families encode a percentage
    in the last TWO digits instead of a tier (``AB-610``/``AB-612``,
    ``AP-610``), so their top members score low and are not matched. The error
    direction is deliberate — a missed reminder is cheap, a spurious nag is
    what gets the feature switched off. ``match_mode: "any"`` and the custom
    substring list are the escape hatches. Pure."""
    text = str(name or "").strip()
    m = _HARDWIRING_CODE.search(text) or _HARDWIRING_CODE_NODASH.search(text)
    if not m:
        return None
    return int(m.group(1)[-1])


def is_valuable(name: str, group_id, min_grade: int = 5) -> bool:
    """True when this implant is worth pulling, judged entirely offline.

    * group 303 (Booster) → never (not an implant, and consumed not unplugged);
    * group 745 (Cyber Learning) → only the ``- Improved`` (+5) tier; every
      cheaper attribute implant is the fly-it-permanently kit;
    * group 300 (Cyberimplant) → ALWAYS: this is the High-/Mid-/Low-grade set
      family (Snake, Crystal, Amulet — CCP's rename of the old "Slave" set, no
      type is called Slave any more — Halo, Talisman, Asklepian, Ascendancy,
      Hydra, Nirvana, Savior, Mimesis, …) — the "oh god, my pod" implants;
    * anything else (the hardwirings) → its ``grade_of`` must reach
      ``min_grade``, or carry no code at all (named rares).

    Pure."""
    try:
        gid = int(group_id)
    except (TypeError, ValueError):
        gid = -1
    if gid in NEVER_GROUPS:
        return False
    if gid == GROUP_CYBER_LEARNING:
        return bool(_LEARNING_TOP_TIER.search(str(name or "").strip()))
    if gid == GROUP_CYBERIMPLANT:
        return True
    grade = grade_of(name)
    if grade is None:
        return True
    return grade >= int(min_grade)


def _name_matches_any(name: str, needles) -> bool:
    hay = str(name or "").casefold()
    return any(str(n).strip().casefold() in hay for n in (needles or ()) if str(n).strip())


def classify(type_ids, table, cfg) -> list:
    """The implants from ``type_ids`` that warrant a reminder, as NAMES.

    ``table`` maps ``type_id -> (name, group_id)`` (see ``load_implant_table``);
    an id missing from it is skipped rather than guessed at — a brand-new
    implant the bundled SDE has not seen simply does not nag. Order follows
    ``type_ids`` with duplicates collapsed. Pure."""
    cfg = cfg if isinstance(cfg, dict) else DEFAULTS
    mode = cfg.get("match_mode", "valuable")
    min_grade = cfg.get("min_hardwiring_grade", 5)
    needles = [n for n in (cfg.get("match_names") or []) if str(n).strip()]
    if mode == "custom" and not needles:
        # Mirrors normalize_config: custom mode with nothing to match against can
        # never fire, so fall back to the default rather than be a silent no-op.
        # Repeated here because classify accepts a RAW cfg dict too.
        mode = DEFAULTS["match_mode"]
    out: list = []
    seen: set = set()
    for raw in (type_ids or ()):
        try:
            tid = int(raw)
        except (TypeError, ValueError):
            continue
        if tid in seen:
            continue
        seen.add(tid)
        entry = (table or {}).get(tid)
        if not entry:
            continue
        name, gid = entry[0], entry[1]
        if mode == "custom":
            hit = _name_matches_any(name, needles)
        elif mode == "any":
            # "Any implant" still excludes boosters — they are not implants and
            # cannot be unplugged.
            try:
                hit = int(gid) != GROUP_BOOSTER
            except (TypeError, ValueError):
                hit = True
        else:
            hit = is_valuable(name, gid, min_grade)
        if hit:
            out.append(name)
    return out


# ── bundled SDE implant table ────────────────────────────────────────────────

_TABLE_LOCK = threading.Lock()
_TABLE_CACHE: "dict | None" = None


def _table_path() -> str:
    """Resolve the bundled SDE type table exactly like the other SDE consumers
    (``prefer="bundle"`` so a stray writable-dir copy cannot shadow it)."""
    return (resolve_data_file("fit_types.json", prefer="bundle")
            or os.path.join(bundle_dir(), "fit_types.json"))


def load_implant_table(path: str | None = None) -> dict:
    """``{type_id: (name, group_id)}`` for every category-20 type in the SDE.

    Lazily built and memoized at module scope — the parse happens at most once
    per process, and only when the feature is actually enabled. A missing or
    corrupt table degrades to ``{}`` (the reminder then simply never matches)
    rather than raising into the poller."""
    global _TABLE_CACHE
    if path is None and _TABLE_CACHE is not None:
        return _TABLE_CACHE
    src = path or _table_path()
    out: dict = {}
    try:
        with open(src, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        log.warning("[implant] type table unreadable at %s — reminder inert", src)
        data = None
    if isinstance(data, dict):
        for raw_id, entry in data.items():
            if not isinstance(entry, dict) or entry.get("c") != IMPLANT_CATEGORY_ID:
                continue
            try:
                tid = int(raw_id)
            except (TypeError, ValueError):
                continue
            name = entry.get("n")
            if isinstance(name, str) and name:
                out[tid] = (name, entry.get("g"))
    if path is None:
        with _TABLE_LOCK:
            if _TABLE_CACHE is None:
                _TABLE_CACHE = out
            return _TABLE_CACHE
    return out


# ── per-character latch / snooze state machine ───────────────────────────────

FIRE = "fire"              # undocked -> docked-at-staging edge: remind now
HOLD = "hold"              # still docked at staging, already reminded
PRIMED = "primed"          # first sighting, already docked: arm, do NOT nag
CLEAR = "clear"            # not at staging: latch released, re-armed
SUPPRESSED = "suppressed"  # snoozed / per-character disabled


@dataclass
class _CharLatch:
    latched: bool = False
    attempts: int = 0
    #: The dock identity the latch was set at (``dock_identity``), and the
    #: ``now`` of the most recent observation. Together they are what lets the
    #: latch say "this is still the SAME unbroken dock" instead of merely "I
    #: have not seen an undock" — see ``ReminderState.observe``.
    dock: tuple = ()
    seen: float = 0.0


@dataclass
class ReminderState:
    """Latch/snooze bookkeeping for every tracked character. Pure + Tk-free.

    ``observe`` is edge-detecting: it is called on every poll with the current
    at-staging answer and returns ``FIRE`` only when the poll is evidence of a
    NEW dock. It is not, however, a pure False→True edge — that was the
    2026-07-26 defect, and it is worth stating plainly because the docstring
    here used to claim the opposite ("a restart, a dropped poll or a missing
    prior state [is] harmless"):

    **The dock signal is SAMPLED, not streamed.** fc_gui's ESI poller reads
    ``/location`` once per ``FCToolGUI._OVERLAY_LOCSHIP_EVERY`` (10 s) per
    character, and the round-robin over a dozen characters stretches the gap to
    ~11-14 s. A latch that is released only by a poll which literally *sees*
    the character away from staging therefore loses every undock that fits
    between two samples — and an undock → re-dock round trip at a citadel fits
    easily. The pilot got no toast and no log line; the failure was invisible.
    The same thing happens over a much longer window whenever the engine is not
    called at all: the box switched off, a token still missing
    ``esi-clones.read_implants.v1``, an unresolvable staging, an empty
    ``/location`` payload, an untracked client.

    So the latch records **what** it latched on (``dock_identity``) and **when**
    it was last confirmed, and releases when either stops vouching for it:

    * a poll that sees the character away from staging — the original edge;
    * a poll that sees a DIFFERENT dock — they cannot have moved between
      structures without undocking, whether or not anyone watched;
    * a poll that arrives after a gap longer than ``blind_gap_s`` — over a
      blackout the engine learned nothing, and a latch cannot vouch for a
      window it never saw.

    What it deliberately still cannot detect: a re-dock into the SAME structure
    where every intervening poll was missed. Nothing in ESI distinguishes that
    from never having undocked, so the only lever is the sampling rate."""

    #: Hard cap on FIRE retries per dock when the implant fetch itself fails
    #: (a None from ESI). Without it a broken token would re-fetch every poll
    #: for as long as the pilot stays docked.
    max_fetch_attempts: int = 3

    #: How long the engine may go without observing a character before its
    #: latch stops being trustworthy. Chosen at ~6x the 10 s poll cadence: poll
    #: jitter, a slow round-robin and a couple of dropped location payloads all
    #: stay well inside it, so reaching it means the engine was genuinely not
    #: running (feature off, scope missing, client gone). The error direction is
    #: deliberate and is the one the owner asked for — at worst ONE extra toast
    #: after a minute-long blackout, against a silently-eaten reminder.
    blind_gap_s: float = 60.0

    _latches: dict = field(default_factory=dict)
    _snoozed: set = field(default_factory=set)

    # -- queries ------------------------------------------------------------
    def is_snoozed(self, key: str) -> bool:
        return str(key or "").strip().lower() in self._snoozed

    def snooze(self, key: str) -> None:
        """Session-only 'not this session' suppression. Never persisted."""
        k = str(key or "").strip().lower()
        if k:
            self._snoozed.add(k)

    def unsnooze(self, key: str) -> None:
        self._snoozed.discard(str(key or "").strip().lower())

    def forget(self, key: str) -> None:
        """Drop all state for a character (client closed / logged out)."""
        self._latches.pop(str(key or "").strip().lower(), None)

    # -- the machine --------------------------------------------------------
    def observe(self, key: str, staging: bool, disabled=(), dock=None,
                now=None) -> str:
        """Advance one character by one poll; returns one of the module verbs.

        ``disabled`` is the config's per-character opt-out list (show-oriented
        UX, disabled-oriented storage — a brand-new character defaults to ON,
        matching ``preview.disabled_chars``).

        ``dock`` is this poll's ``DockState`` and ``now`` its monotonic
        timestamp; both are optional and both only ever RELEASE a latch, never
        set one, so a caller that omits them gets exactly the pre-2026-07-26
        behaviour (see the class docstring for why the caller should not)."""
        k = str(key or "").strip().lower()
        if not k:
            return CLEAR
        ts = time.monotonic() if now is None else float(now)
        ident = dock_identity(dock)
        off = {str(d).strip().lower() for d in (disabled or ())}
        first_sighting = k not in self._latches
        latch = self._latches.setdefault(k, _CharLatch())
        gap = ts - latch.seen
        latch.seen = ts

        if not staging:
            latch.latched = False
            latch.attempts = 0
            latch.dock = ()
            return CLEAR
        if k in off or k in self._snoozed:
            # Still latch so un-snoozing mid-dock doesn't fire retroactively.
            latch.latched = True
            latch.dock = ident
            return SUPPRESSED
        if first_sighting:
            # Already parked at staging when we first saw this character —
            # that is not "just got back from a fleet".
            latch.latched = True
            latch.dock = ident
            return PRIMED
        if latch.latched:
            # A latch only holds while it can still vouch for the dock it was
            # set at. A different structure/station means they undocked whether
            # or not a poll caught it; a gap longer than blind_gap_s means the
            # engine was not watching and cannot claim they stayed put.
            moved = bool(ident) and bool(latch.dock) and ident != latch.dock
            if not (moved or gap > self.blind_gap_s):
                return HOLD
            # A genuinely new dock cycle, so the fetch-retry budget resets with
            # it — exactly as the CLEAR branch above does for the observed edge.
            latch.attempts = 0
        latch.latched = True
        latch.dock = ident
        # INCREMENT, never reset: retry_fetch re-arms the latch after a failed
        # implant fetch, so the next FIRE is attempt N+1 of the SAME dock. A
        # reset here would make max_fetch_attempts unreachable and re-poll ESI
        # every 10 s for as long as the pilot stayed docked. The counter is
        # cleared only when the dock cycle genuinely ends (CLEAR, or the
        # re-arm above).
        latch.attempts += 1
        return FIRE

    def retry_fetch(self, key: str) -> bool:
        """Release the latch after a FAILED implant fetch so the next poll tries
        again, up to ``max_fetch_attempts`` per dock. Returns True when a retry
        was armed. A successful fetch (even one matching nothing) never calls
        this — one reminder per dock is the contract."""
        k = str(key or "").strip().lower()
        latch = self._latches.get(k)
        if latch is None:
            return False
        if latch.attempts >= self.max_fetch_attempts:
            return False
        latch.latched = False
        return True


# ── toast copy ───────────────────────────────────────────────────────────────

TOAST_TITLE = "Implants still plugged in"

#: A group-300 set member, e.g. "Mid-grade Amulet Delta". Read off the bundled
#: SDE rather than assumed: all 330 category-20 group-300 types are exactly
#: ``<High|Mid|Low>-grade <Family> <Greek>`` across 55 grade/family sets, with
#: zero exceptions — so the set NAME the owner asked for ("Mid-grade Amulets")
#: is derivable from the type name alone, with no extra table lookup and no
#: hand-written list of set names to go stale when CCP adds one.
#: ``test_the_every_set_family_in_the_bundle_parses_into_a_set_label`` is the
#: drift guard against the SDE itself.
_SET_NAME = re.compile(r"^(high|mid|low)-grade\s+(.+?)\s+"
                       r"(alpha|beta|gamma|delta|epsilon|omega)$", re.IGNORECASE)

#: The five "Cyber Learning" attribute families. Matched on the name because
#: ``toast_body`` is handed NAMES only — it never sees the group ids ``classify``
#: worked from. The quoted-infix strip below is what lets the two group-1730
#: oddities ("Neural 'Source' Boost", "Cybernetic 'Source' Subprocessor") land
#: in the same bucket as their plain siblings.
_ATTRIBUTE_FAMILIES = ("ocular filter", "memory augmentation", "neural boost",
                       "cybernetic subprocessor", "social adaptation chip")
_QUOTED_INFIX = re.compile(r"'[^']*'")

#: Plural pairs for the non-set buckets. Sets pluralise structurally instead
#: ("Mid-grade Amulet" -> "Mid-grade Amulets").
_BUCKET_NOUNS = {
    "mindlink": ("Mindlink", "Mindlinks"),
    "attribute": ("attribute implant", "attribute implants"),
    "hardwiring": ("hardwiring", "hardwirings"),
    "other": ("implant", "implants"),
}
_GRADE_RANK = {"high": 3, "mid": 2, "low": 1}


def _set_of(name):
    """``(Grade, Family)`` for a High-/Mid-/Low-grade set member, else None. Pure."""
    m = _SET_NAME.match(str(name or "").strip())
    if not m:
        return None
    return (m.group(1).capitalize(), m.group(2).strip())


def _is_attribute_implant(name) -> bool:
    plain = " ".join(_QUOTED_INFIX.sub(" ", str(name or "")).split()).casefold()
    return any(fam in plain for fam in _ATTRIBUTE_FAMILIES)


def _bucket_of(name) -> tuple:
    """Which kind of thing this implant is, as a hashable bucket key. Pure.

    Order matters: a set member is a set even though it has no code, and an
    attribute implant must be claimed before the code-less fall-through so it
    is not reported as a generic "implant"."""
    grade_family = _set_of(name)
    if grade_family:
        return ("set",) + grade_family
    text = str(name or "").strip()
    if text.casefold().endswith("mindlink"):
        return ("mindlink",)
    if _is_attribute_implant(text):
        return ("attribute",)
    if grade_of(text) is not None:
        return ("hardwiring",)
    return ("other",)


def _bucket_label(key: tuple, count: int) -> str:
    if key[0] == "set":
        return f"{key[1]}-grade {key[2]}" + ("s" if count > 1 else "")
    one, many = _BUCKET_NOUNS[key[0]]
    return f"{count} {one if count == 1 else many}"


def toast_body(char_name: str, names) -> str:
    """One-line body for the toast: who, and the SET most of it belongs to.

    Owner ask, 2026-07-26: name the dominant set ("Mid-grade Amulets") rather
    than listing implants. Listing three names plus "+7 more" was a wall of
    text over the client and buried the one fact that matters — which pod
    you are about to lose.

    The rule, in order:

    * nothing to report -> the plain "remember to unplug" nudge;
    * exactly one implant -> name it outright (it is already short, and the
      exact name is the useful thing for a lone Mindlink or a named rare);
    * any High-/Mid-/Low-grade SET present -> the dominant set names the
      toast, even when hardwirings outnumber it. A set is why this feature
      exists; the count should not let five cheap hardwirings outvote a
      Talisman set;
    * otherwise -> the largest remaining bucket, counted rather than listed
      ("4 hardwirings", "2 Mindlinks", "3 attribute implants", "6 implants").

    Anything outside the named bucket is summarised as "+N more", so the copy
    stays honest about how full the head is without listing it.

    Tie-break, so the copy is stable for a given clone instead of dictionary
    order: most members, then the higher grade (High > Mid > Low), then the one
    that appears first in ``names`` (i.e. the pilot's own implant-slot order).

    Pure."""
    listed = [str(n).strip() for n in (names or ()) if str(n or "").strip()]
    who = str(char_name or "").strip() or "This character"
    if not listed:
        return f"{who} — remember to unplug before the next fleet."
    if len(listed) == 1:
        return f"{who} — {listed[0]}"

    counts: dict = {}
    first_seen: dict = {}
    for index, name in enumerate(listed):
        key = _bucket_of(name)
        counts[key] = counts.get(key, 0) + 1
        first_seen.setdefault(key, index)

    sets = [k for k in counts if k[0] == "set"]
    pool = sets or list(counts)
    best = max(pool, key=lambda k: (counts[k],
                                    _GRADE_RANK.get(k[1].casefold(), 0)
                                    if k[0] == "set" else 0,
                                    -first_seen[k]))
    head = _bucket_label(best, counts[best])
    extra = len(listed) - counts[best]
    if extra > 0:
        head = f"{head} +{extra} more"
    return f"{who} — {head}"


# ── orchestrator (poller-thread side; Tk-free) ───────────────────────────────

class ImplantReminder:
    """Glue between the ESI location poller and the toast. Tk-free by design.

    Everything that touches the outside world is injected:

    ``config_provider()``   -> the whole app config dict (read live each poll so
                               a config edit takes effect without a restart);
    ``resolve_system_name`` -> name -> solar_system_id (``system_coords``);
    ``implants_provider(a)``-> list[int] of the ACTIVE clone's implant type ids
                               for the ESIAuth ``a``, or None on failure;
    ``on_remind(key, name, implant_names)`` -> show the toast. The CALLER is
                               responsible for marshalling this onto the Tk
                               thread; ``observe`` runs on the poller thread.

    ``observe`` is the only method the poller calls, and it never raises."""

    def __init__(self, config_provider, implants_provider, on_remind,
                 resolve_system_name=None, table=None, clock=None):
        self._config_provider = config_provider
        self._implants_provider = implants_provider
        self._on_remind = on_remind
        self._resolve_system_name = resolve_system_name
        self._table = table                 # None -> lazily loaded from the SDE
        # Monotonic source for the latch's blackout check. Injectable so the
        # tests can run a poll timeline without sleeping; never wall clock.
        self._clock = clock or time.monotonic
        self._state = ReminderState()
        self._lock = threading.Lock()       # guards snooze across Tk/poller
        # Last staging resolution announced to the log, as (kind, value, rung).
        # See _log_target_once — this is what makes a wrong/absent staging
        # diagnosable instead of a silent no-op.
        self._logged_target = None
        # key -> last reminded implant names. Written by the poller thread, read
        # by the Tk thread; lock-free on purpose — a plain dict set of a freshly
        # built list is GIL-atomic and a reader can only ever see the old list or
        # the new one, never a half-built one.
        self._last_names: dict = {}

    # -- config ------------------------------------------------------------
    def config(self) -> dict:
        try:
            root = self._config_provider() or {}
        except Exception:
            return normalize_config(None)
        return normalize_config(root.get("implant_reminder")
                                if isinstance(root, dict) else None)

    def _root_config(self) -> dict:
        try:
            root = self._config_provider() or {}
        except Exception:
            return {}
        return root if isinstance(root, dict) else {}

    def enabled(self) -> bool:
        return bool(self.config().get("enabled"))

    def table(self) -> dict:
        if self._table is None:
            self._table = load_implant_table()
        return self._table

    # -- dismissal ---------------------------------------------------------
    def snooze(self, key: str) -> None:
        """'Not this session' — in-memory only, never written to config.
        Safe to call from the Tk thread while the poller runs."""
        with self._lock:
            self._state.snooze(key)

    def forget(self, key: str) -> None:
        with self._lock:
            self._state.forget(key)

    def last_names(self, key: str) -> list:
        return list(self._last_names.get(str(key or "").strip().lower(), ()))

    # -- diagnostics -------------------------------------------------------
    def _log_target_once(self, target: StagingTarget) -> bool:
        """Announce the resolved staging the FIRST time the enabled feature sees
        it — including the "nothing configured" answer, which is the one that
        used to be invisible.

        Only ever runs behind the enabled gate, so an off feature still logs
        nothing. Keyed on the resolution itself rather than on a plain "have I
        logged?" flag, so a live config edit that moves staging re-announces
        (still at most one line per distinct target, never per poll). Returns
        True when it actually logged — for tests."""
        key = (target.kind, target.value, target.rung)
        with self._lock:
            if key == self._logged_target:
                return False
            self._logged_target = key
        if target.configured:
            log.info("[implant] reminder enabled — staging = %s", target.describe())
        else:
            log.info("[implant] reminder enabled but staging is %s. Set the "
                     "Settings > Staging System field (zkillboard.staging_system) "
                     "— or implant_reminder.staging_system / "
                     "staging_structure_id to override it explicitly.",
                     target.describe())
        return True

    # -- the poller hook ---------------------------------------------------
    def observe(self, char_key: str, char_name: str, loc, auth) -> str:
        """Advance one character with a freshly-polled ESI location payload.

        Called from the ESI poller thread once per location poll per character.
        Returns the state verb (for tests/logging). Never raises: any failure
        degrades to ``CLEAR`` so a broken reminder can never take the poller
        down with it."""
        try:
            cfg = self.config()
            if not cfg.get("enabled"):
                return CLEAR
            target = resolve_staging(self._root_config(),
                                     self._resolve_system_name,
                                     cfg.get("staging_scope", "ladder"))
            self._log_target_once(target)
            if not target.configured:
                return CLEAR
            state = dock_state(loc)
            staging = at_staging(state, target)
            with self._lock:
                verb = self._state.observe(char_key, staging,
                                           cfg.get("disabled_chars", ()),
                                           dock=state, now=self._clock())
            if verb != FIRE:
                return verb

            ids = None
            try:
                ids = self._implants_provider(auth)
            except Exception:
                log.exception("[implant] implants fetch failed for %s", char_key)
            if ids is None:
                with self._lock:
                    self._state.retry_fetch(char_key)
                return CLEAR

            names = classify(ids, self.table(), cfg)
            if not names:
                return HOLD          # nothing worth pulling; stay latched
            self._last_names[str(char_key or "").strip().lower()] = list(names)
            # One line per dock, not per poll. "It did not fire" was
            # unfalsifiable while the only thing this module ever logged was
            # the staging resolution; a fire that reached the toast layer must
            # leave a trace. ASCII only -- this box's console is cp1252.
            log.info("[implant] reminding %s: %d implant(s) worth pulling",
                     char_key, len(names))
            self._on_remind(char_key, char_name, names)
            return FIRE
        except Exception:
            log.exception("[implant] observe failed for %s", char_key)
            return CLEAR
