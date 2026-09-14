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
LOGIN = "login"            # login edge carrying valuable implants

#: How long the engine must have been WATCHING before a character's FIRST
#: sighting may read as a login (seconds, on the same monotonic clock the
#: latch uses).
#:
#: The login trigger has exactly the problem "Watch my ozone" already solved
#: in fc_gui, and is answered the same way on purpose (one notion of "login",
#: not two): the ESI poller's roster is built from CONNECTED clients, so a
#: client sitting at the login screen is never polled and the strict
#: offline->online transition is nearly unreachable -- a character logging in
#: mid-session simply APPEARS. A first sighting is therefore the real login
#: signal, and this window is what stops the whole roster reading as a login
#: burst in the first seconds after FCTool starts (or after the feature is
#: switched on). Mirrors fc_gui's _OZONE_LOGIN_GRACE_S; the error direction is
#: the owner's standing one for this feature -- at worst one extra toast,
#: never a silently eaten reminder.
LOGIN_GRACE_S = 120.0


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

    #: Grace window for the LOGIN machine's first-sighting rule (see
    #: ``LOGIN_GRACE_S`` and ``observe_login``).
    login_grace_s: float = LOGIN_GRACE_S

    _latches: dict = field(default_factory=dict)
    _snoozed: set = field(default_factory=set)

    # -- LOGIN machine state (2026-09-14) -----------------------------------
    # Deliberately NOT folded into _CharLatch: the dock machine reads 'have I
    # ever seen this character' as ``key not in self._latches`` and answers a
    # first sighting with PRIMED (arm, do NOT nag). A login observation that
    # created the latch entry first would turn that first sighting into an
    # ordinary poll and could FIRE the dock toast on top of the login one --
    # the exact double-toast this feature must not produce.
    #: key -> the last ``online`` sample seen (True / False / None while the
    #: poller has never answered for that character).
    _online: dict = field(default_factory=dict)
    #: keys whose CURRENT login has already been answered. Cleared by an
    #: observed logout, by ``prune`` (the client is gone) and by
    #: ``reset_logins``; that is what makes the reminder fire again after a
    #: logout -> login round trip, and only then.
    _login_done: set = field(default_factory=set)
    #: Monotonic stamp of the first ``observe_login`` call = 'the engine
    #: started watching'. The grace window is measured from here, not from
    #: process start, so switching the feature on mid-session gets the same
    #: protection a cold start gets.
    _started_at: float = None

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

    # -- the LOGIN machine --------------------------------------------------
    def observe_login(self, key: str, online, disabled=(), now=None) -> str:
        """Advance ONE character's LOGIN machine by one poll; returns a verb.

        ``online`` is this pass's ``/online/`` answer (``None`` = the poller did
        not ask on this pass, which carries the previous sample forward -- an
        ESI hiccup must never fabricate an edge, the rule the dock side follows
        too). A login is:

        * a strict ``False -> True`` online transition; OR
        * this character's FIRST sighting, once the engine has been watching for
          longer than ``login_grace_s`` (see ``LOGIN_GRACE_S`` for why the
          strict rule alone is nearly unreachable in practice).

        FIRE is returned at most once per login: the answer is remembered until
        an observed logout (``True -> False``), a ``prune`` (the client closed)
        or ``reset_logins``. A snoozed or per-character-disabled pilot is marked
        answered rather than left pending, so un-snoozing mid-session cannot
        fire retroactively -- the dock machine's own rule."""
        k = str(key or "").strip().lower()
        if not k:
            return CLEAR
        ts = time.monotonic() if now is None else float(now)
        if self._started_at is None:
            self._started_at = ts
        first_sighting = k not in self._online
        prev = self._online.get(k)
        cur = prev if online is None else bool(online)
        self._online[k] = cur
        if prev is True and cur is False:
            # An observed logout re-arms the trigger, and does nothing else.
            self._login_done.discard(k)
            return CLEAR
        logged_in = ((prev is False and cur is True)
                     or (first_sighting
                         and (ts - self._started_at) > self.login_grace_s))
        if not logged_in:
            return CLEAR
        off = {str(d).strip().lower() for d in (disabled or ())}
        if k in off or k in self._snoozed:
            self._login_done.add(k)
            return SUPPRESSED
        if k in self._login_done:
            # Belt-and-braces: unreachable given the arithmetic above. Every
            # path that can set membership in _login_done (SUPPRESSED, or the
            # FIRE below) also returns immediately, and the only way OUT of
            # _login_done is the True -> False CLEAR branch, which returns
            # before reaching here too -- so a poll can never fall through to
            # find its own key already marked. Kept as a defensive floor
            # rather than deleted, in case a future edit adds a path in.
            return HOLD
        self._login_done.add(k)
        return FIRE

    def mark_reminded(self, key: str, dock=None, now=None) -> None:
        """Tell the DOCK machine this character has already been reminded for
        whatever dock it is sitting in right now.

        The login path calls it so a pilot who logs in already docked at staging
        gets ONE toast, not two: the dock machine then finds a latch set at this
        very dock identity and answers HOLD on its next poll instead of FIRE.
        Harmless when the character is in space -- the identity is empty and the
        first not-at-staging poll releases the latch anyway."""
        k = str(key or "").strip().lower()
        if not k:
            return
        latch = self._latches.setdefault(k, _CharLatch())
        latch.latched = True
        latch.attempts = 0
        latch.dock = dock_identity(dock)
        latch.seen = time.monotonic() if now is None else float(now)

    def prune(self, keys) -> list:
        """Drop the LOGIN state of every character not in ``keys`` (the poller's
        current roster); returns the keys dropped.

        A closed client simply stops being polled -- ESI never reports the
        logout -- so without this the "already answered" mark would outlive the
        session it belongs to and a genuine re-login would be silent. The dock
        machine's latches are deliberately left alone: they are released by
        their own staleness rules, and dropping them here would only widen this
        method's blast radius.

        The cost of a roster blip (a character briefly missing from one pass) is
        one extra toast on the next sighting -- this feature's chosen error
        direction, and the same trade ``_ozone_prune`` makes."""
        live = {str(k or "").strip().lower() for k in (keys or ())}
        gone = [k for k in self._online if k not in live]
        for k in gone:
            self._online.pop(k, None)
            self._login_done.discard(k)
        return gone

    def reset_logins(self) -> None:
        """Forget every login sample and the grace anchor with it.

        Switching the feature ON is the startup case: the whole roster is
        already there and none of it just logged in. Clearing the samples
        without restarting the grace clock would trade a stale sample for a
        login burst -- the lesson "Watch my ozone" paid for."""
        self._online.clear()
        self._login_done.clear()
        self._started_at = None

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
#: ("Mid-grade Amulet" -> "Mid-grade Amulets"), and so does a mindlink that
#: carries a discipline ("Skirmish Mindlink" -> "Skirmish Mindlinks", handled in
#: ``_bucket_label``); the "mindlink" pair below is only the generic fallback
#: for a name with no discipline word.
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


#: The command-burst DISCIPLINE a "* Mindlink" name can carry, mapped to the
#: short word shown in the tooltip/toast. Read off the bundled SDE's category-20
#: "* Mindlink" types (``tools/gen_fit_types.py`` output, 15 in the 2026-06 SDE),
#: not assumed: the boost mindlinks are ``<Discipline> Command Mindlink``
#: (Skirmish / Armored / Information / Shield), ``Mining Foreman Mindlink``, and
#: faction/ORE/Sisters variants that put the discipline word later ("ORE Mining
#: Director Mindlink", "Sisters Expedition Command Mindlink"). ``siege`` is the
#: pre-2016 name for the Shield line and ``warfare`` the pre-2016 connector
#: ("Siege Warfare Mindlink" / "Skirmish Warfare Mindlink"), both still
#: recognised so old data and muscle-memory names resolve. ``expedition`` is the
#: mining-fleet family. The keys are matched whole-word and case-insensitively,
#: so word order and any faction prefix are irrelevant.
#:
#: Deliberately NOT here: a faction->discipline table for the faction-navy
#: mindlinks (Caldari Navy, Federation Navy, Imperial Navy, Republic Fleet) and
#: the named ones (Guri Malakim, Pashan's turret set). Their names carry no
#: discipline word, so ``_mindlink_discipline`` returns "" and they fall back to
#: the generic "Mindlink" rather than lean on a hand-written faction list that
#: goes stale when CCP adds one (the same reason ``_SET_NAME`` derives set names
#: from the type name instead of a fixed list).
_MINDLINK_DISCIPLINES = {
    "skirmish": "Skirmish",
    "armored": "Armored",
    "armor": "Armored",
    "information": "Information",
    "shield": "Shield",
    "siege": "Siege",
    "mining": "Mining",
    "expedition": "Expedition",
}


def _mindlink_discipline(name) -> str:
    """The command-burst discipline a "* Mindlink" name carries, e.g. "Skirmish"
    for "Skirmish Command Mindlink" / "Skirmish Warfare Mindlink" and "Mining"
    for "Mining Foreman Mindlink" / "ORE Mining Director Mindlink".

    Returns "" when the name carries no recognised discipline word — the
    faction-navy variants (Caldari Navy / Federation Navy / Imperial Navy /
    Republic Fleet) and the named ones (Guri Malakim, Pashan's turret set) — so
    the caller words them as the generic "Mindlink" rather than guess. Pure;
    never raises."""
    for word in str(name or "").casefold().split():
        disc = _MINDLINK_DISCIPLINES.get(word)
        if disc:
            return disc
    return ""


def _bucket_of(name) -> tuple:
    """Which kind of thing this implant is, as a hashable bucket key. Pure.

    Order matters: a set member is a set even though it has no code, and an
    attribute implant must be claimed before the code-less fall-through so it
    is not reported as a generic "implant".

    A mindlink carries a SECOND element — its command-burst discipline, e.g.
    ``("mindlink", "Skirmish")`` — or ``("mindlink", "")`` when the name has no
    discipline word (the faction-navy / named variants). Each discipline is its
    own bucket, so ``summary_labels`` lists them separately, exactly as it lists
    each set separately (see ``_mindlink_discipline``)."""
    grade_family = _set_of(name)
    if grade_family:
        return ("set",) + grade_family
    text = str(name or "").strip()
    if text.casefold().endswith("mindlink"):
        return ("mindlink", _mindlink_discipline(text))
    if _is_attribute_implant(text):
        return ("attribute",)
    if grade_of(text) is not None:
        return ("hardwiring",)
    return ("other",)


def _bucket_label(key: tuple, count: int) -> str:
    if key[0] == "set":
        return f"{key[1]}-grade {key[2]}" + ("s" if count > 1 else "")
    if key[0] == "mindlink" and len(key) > 1 and key[1]:
        # A discipline-bearing mindlink names its discipline and pluralises
        # STRUCTURALLY like a set ("Skirmish Mindlink" / "Skirmish Mindlinks"),
        # not by count. One command mindlink fills the slot, so count > 1 is
        # only reachable across an odd clone, but the label stays sane if it is.
        # A disciplineless mindlink ((..., "")) falls through to the generic
        # "1 Mindlink" / "N Mindlinks" noun pair below, unchanged.
        return f"{key[1]} Mindlink" + ("s" if count > 1 else "")
    one, many = _BUCKET_NOUNS[key[0]]
    return f"{count} {one if count == 1 else many}"


def _bucket_census(names) -> tuple:
    """``(listed, counts, first_seen)`` for a head of implant NAMES. Pure.

    The ONE census both summaries are built from — ``toast_body``'s dominant
    bucket and ``summary_labels``' ordered list. Extracted rather than copied:
    two classifiers of the same taxonomy is the drift this module already paid
    for once (see ``_bucket_of``'s ordering comment). Blank / non-string
    entries are dropped, so a garbage-only head censuses as empty."""
    listed = [str(n).strip() for n in (names or ()) if str(n or "").strip()]
    counts: dict = {}
    first_seen: dict = {}
    for index, name in enumerate(listed):
        key = _bucket_of(name)
        counts[key] = counts.get(key, 0) + 1
        first_seen.setdefault(key, index)
    return listed, counts, first_seen


#: Order the non-set buckets appear in ``summary_labels``: the things a pilot
#: would name first. Sets come before all of them (grade-ranked), because a set
#: is the reason this feature exists.
_SUMMARY_BUCKET_ORDER = ("mindlink", "attribute", "hardwiring", "other")


def summary_labels(names) -> tuple:
    """The same head as ``toast_body``, as an ORDERED list of short labels.

    The FCPreview tile icon's tooltip source ("Major implants: Mid-grade
    Amulets · 2 hardwirings"): unlike the toast — which has one line over the
    game client and so names only the dominant bucket — the tooltip has room
    for every bucket, but still never lists individual components.

    Built from ``_bucket_census`` / ``_bucket_label``, so a name is bucketed
    and worded EXACTLY as the toast words it. Order is deterministic:

    * the High-/Mid-/Low-grade SETS first, ranked by grade (High > Mid > Low),
      then by how many members are plugged in, then by the pilot's own
      implant-slot order — the same tie-break ``toast_body`` uses, so the copy
      is stable for a given clone rather than dictionary-order roulette;
    * then Mindlinks — each named by its command-burst discipline ("Skirmish
      Mindlink"), or a generic "Mindlink" when the name carries none — then
      attribute implants, hardwirings, and anything else (the named/faction
      rares), counted rather than listed.

    Empty / garbage input answers ``()`` — the caller reads that as "no icon".
    Returns a TUPLE: the poller publishes it into a dict the Tk tick reads
    without a lock, and an immutable value is what makes that safe. Pure."""
    return _labels_of(names)


def _ordered_buckets(counts, first_seen, kinds=None):
    """The deterministic bucket order both label builders use. Pure.

    Sets first (grade, then member count, then the pilot's own slot order),
    then ``_SUMMARY_BUCKET_ORDER``. ``kinds`` narrows the result WITHOUT
    changing the order, so a narrowed list is always a subsequence of the full
    one — the login toast can never word a bucket differently from the tooltip
    that names the same clone."""
    sets = sorted((k for k in counts if k[0] == "set"),
                  key=lambda k: (-_GRADE_RANK.get(k[1].casefold(), 0),
                                 -counts[k], first_seen[k]))
    rest = [k for kind in _SUMMARY_BUCKET_ORDER
            for k in counts if k[0] == kind]
    keys = sets + rest
    if kinds is not None:
        keys = [k for k in keys if k[0] in kinds]
    return keys


def _labels_of(names, kinds=None) -> tuple:
    _listed, counts, first_seen = _bucket_census(names)
    if not counts:
        return ()
    return tuple(_bucket_label(k, counts[k])
                 for k in _ordered_buckets(counts, first_seen, kinds))


def login_labels(names) -> tuple:
    """The login toast's word list. Pure.

    Owner spec, 2026-09-14: the login toast should name the clone the same way
    the tooltip does -- "the EXISTING summary labels" -- so a pilot never sees
    the dock/tooltip flavour call a clone one thing and the login flavour call
    it another. This is exactly ``summary_labels``, kept as its own name so
    the login call sites read as "the login toast's words" rather than an
    unexplained reuse of the tooltip's function. ``()`` when the clone carries
    nothing this module buckets."""
    return summary_labels(names)


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
    * otherwise -> the largest remaining bucket: named when it has a proper
      name ("Skirmish Mindlink"), else counted ("4 hardwirings", "3 attribute
      implants", "6 implants").

    Anything outside the named bucket is summarised as "+N more", so the copy
    stays honest about how full the head is without listing it.

    Tie-break, so the copy is stable for a given clone instead of dictionary
    order: most members, then the higher grade (High > Mid > Low), then the one
    that appears first in ``names`` (i.e. the pilot's own implant-slot order).

    Pure."""
    listed, counts, first_seen = _bucket_census(names)
    who = str(char_name or "").strip() or "This character"
    if not listed:
        return f"{who} — remember to unplug before the next fleet."
    if len(listed) == 1:
        return f"{who} — {listed[0]}"

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


def login_toast_body(char_name: str, names) -> str:
    """One-line body for the LOGIN toast: "<who> logged in - <what>".

    Owner ask, 2026-09-14: "implement the save my implants pop-up at character
    login if the character logs in with valuable implants". The wording keeps
    the dock toast's shape (who first, one line, no list) and swaps its "you
    just docked" framing for the login one, so the two are told apart at a
    glance over the client.

    CAPPED like the dock toast: only the first ``login_labels`` entry is named
    outright, with an "+N more" tail for the rest, never every label joined by
    " + ". ``ClientToast`` is a fixed-width single-line body (no wrap) and the
    old join-everything copy measured 420-630 px against a ~412 px usable
    width for ordinary multi-bucket clones -- it clipped. The toast TITLE
    ("Implants still plugged in") already tells the pilot what to do about it,
    so the body no longer repeats the instruction. Pure."""
    who = str(char_name or "").strip() or "This character"
    labels = login_labels(names)
    if not labels:
        return f"{who} logged in with implants."
    body = f"{who} logged in - {labels[0]}"
    extra = len(labels) - 1
    if extra > 0:
        body += f" +{extra} more"
    return body


# ── orchestrator (poller-thread side; Tk-free) ───────────────────────────────

class ImplantReminder:
    """Glue between the ESI location poller and the toast. Tk-free by design.

    Everything that touches the outside world is injected:

    ``config_provider()``   -> the whole app config dict (read live each poll so
                               a config edit takes effect without a restart);
    ``resolve_system_name`` -> name -> solar_system_id (``system_coords``);
    ``implants_provider(a)``-> list[int] of the ACTIVE clone's implant type ids
                               for the ESIAuth ``a``, or None on failure;
    ``on_remind(key, name, implant_names)`` -> show the DOCK toast. The CALLER
                               is responsible for marshalling this onto the Tk
                               thread; ``observe`` runs on the poller thread.
    ``on_login_remind(...)``-> the same, for the LOGIN toast, which words its
                               body differently (``login_toast_body``). Optional
                               and defaults to ``on_remind``: a caller that does
                               not care about the flavour keeps the old
                               three-argument contract exactly, which is why the
                               flavour is a second callback and not a fourth
                               argument on the first one.

    ``observe`` is the only method the poller calls per pass (``prune`` is
    called once per pass for the whole roster), and it never raises."""

    def __init__(self, config_provider, implants_provider, on_remind,
                 resolve_system_name=None, table=None, clock=None,
                 on_login_remind=None):
        self._config_provider = config_provider
        self._implants_provider = implants_provider
        self._on_remind = on_remind
        self._on_login_remind = on_login_remind or on_remind
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

    def prune(self, keys) -> list:
        """Drop the login state of every character not on the poller's current
        roster. Called once per poll pass, on the poller thread; never raises.

        This is what makes "log out, log back in, get reminded again" work: a
        closed client is never polled again, so the logout itself is invisible
        to ESI and only its absence from the roster reports it."""
        try:
            with self._lock:
                return self._state.prune(keys)
        except Exception:
            log.exception("[implant] roster prune failed")
            return []

    def reset_logins(self) -> None:
        """Forget every login sample (the feature was just switched ON)."""
        try:
            with self._lock:
                self._state.reset_logins()
        except Exception:
            log.exception("[implant] login reset failed")

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
    def _valuable_names(self, char_key: str, auth, cfg):
        """Fetch + classify this character's implants. ``None`` = the fetch
        itself failed (the caller decides whether that is worth a retry); ``[]``
        = it answered, and nothing in the head is worth a toast."""
        ids = None
        try:
            ids = self._implants_provider(auth)
        except Exception:
            log.exception("[implant] implants fetch failed for %s", char_key)
        if ids is None:
            return None
        return classify(ids, self.table(), cfg)

    def _announce(self, char_key: str, char_name: str, names, login=False):
        """Record what was reminded and hand it to the flavour's callback.

        One line per REMINDER, not per poll. "It did not fire" was unfalsifiable
        while the only thing this module ever logged was the staging
        resolution; a fire that reached the toast layer must leave a trace.
        ASCII only -- this box's console is cp1252."""
        self._last_names[str(char_key or "").strip().lower()] = list(names)
        log.info("[implant] reminding %s (%s): %d implant(s) worth pulling",
                 char_key, "login" if login else "dock", len(names))
        cb = self._on_login_remind if login else self._on_remind
        cb(char_key, char_name, names)

    def observe(self, char_key: str, char_name: str, loc, auth,
                online=None, now=None) -> str:
        """Advance one character with a freshly-polled ESI location payload.

        Called from the ESI poller thread once per location poll per character.
        Returns the state verb (for tests/logging). Never raises: any failure
        degrades to ``CLEAR`` so a broken reminder can never take the poller
        down with it.

        TWO triggers share this one call, in this order:

        * the LOGIN edge (owner, 2026-09-14) -- "logs in with valuable
          implants", wherever the character is, docked or not, staging or not.
          It needs no staging resolution and no location at all, only
          ``online``; see ``ReminderState.observe_login``. When it fires it
          MARKS the dock latch (``mark_reminded``) and returns ``LOGIN``
          without running the dock machine on that pass, so a pilot who logs in
          already docked at staging gets exactly one toast -- the login one
          wins, and the dock trigger stays quiet for that docking UNLESS a
          ``/location`` blackout longer than ``blind_gap_s`` (60 s) follows:
          the dock machine's own staleness rule (a gap that long "means the
          engine was not watching and cannot claim they stayed put") then
          re-arms the same latch and lets it FIRE once more, the pre-existing
          blind-gap trade-off this docstring used to gloss over;
        * the DOCK edge -- the original trigger, unchanged. It runs only when
          this pass actually carried a location: a failed ``/location`` must
          never read as "not at staging" and release a latch (the caller used
          to enforce that by not calling at all, and the login edge is the
          reason the call is now unconditional).

        ``online`` is this pass's ``/online/`` answer or ``None`` when the
        poller did not ask; ``now`` is a monotonic stamp (both optional, so the
        pre-login four-argument call still behaves exactly as it did)."""
        try:
            cfg = self.config()
            if not cfg.get("enabled"):
                return CLEAR
            ts = self._clock() if now is None else float(now)

            with self._lock:
                verb = self._state.observe_login(
                    char_key, online, cfg.get("disabled_chars", ()), now=ts)
            if verb == FIRE:
                names = self._valuable_names(char_key, auth, cfg)
                # A failed fetch is NOT retried on the login edge: the edge is
                # gone by the next pass, and the dock trigger still covers the
                # pilot the moment they come home. Nothing worth pulling is the
                # ordinary answer for most clones and is simply silent.
                if names:
                    self._announce(char_key, char_name, names, login=True)
                    with self._lock:
                        self._state.mark_reminded(
                            char_key, dock_state(loc) if loc else None, now=ts)
                    return LOGIN

            if not loc:
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
                                           dock=state, now=ts)
            if verb != FIRE:
                return verb

            names = self._valuable_names(char_key, auth, cfg)
            if names is None:
                with self._lock:
                    self._state.retry_fetch(char_key)
                return CLEAR
            if not names:
                return HOLD          # nothing worth pulling; stay latched
            self._announce(char_key, char_name, names)
            return FIRE
        except Exception:
            log.exception("[implant] observe failed for %s", char_key)
            return CLEAR
