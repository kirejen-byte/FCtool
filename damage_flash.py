"""Per-character damage-flash decision engine (pure). All time is caller-supplied
(`now`) so tests inject a clock; no wall-clock reads here.

Damage CLASSES (2026-09-26, NPC vs player damage flash): every hit is stored
with a class, "player" or "npc". add()'s `source` is the gamelog classifier's
verdict ("player" | "npc" | "unknown"); ONLY the exact string "npc" is stored
as NPC. "unknown", None and anything else are stored and treated as PLAYER —
the fail-safe direction: a hit we cannot classify pulses red (the urgent
colour) instead of being demoted to routine orange or gated by the threshold.

flash_state(char_key, hp, cfg, now) -> (kind, since) is the ONE reader (the
FCPreview tick calls it once per tile per tick); kind is "player" (red),
"npc" (orange) or None:
  - PLAYER damage ALWAYS arms red, in BOTH modes, with NO threshold: any
    windowed player damage > 0 arms it.
  - NPC damage arms orange only while cfg['damage_flash_npc'] is truthy
    (absent -> True), gated by the mode below applied to the NPC sum ALONE
    (player damage never counts toward it). Toggle OFF drops a live orange
    hold AND its cooldown clock (re-ticking the box re-arms at once); NPC
    hits are still recorded (same bounds) — they just never arm.

Two modes (cfg['damage_flash_mode']) — since 2026-09-26 they gate NPC damage
only:
  - 'any' (DEFAULT, and the default when the key is ABSENT): arm whenever
    windowed NPC damage > 0. NO HP, NO ESI — this is the log-only path that
    can never be silently suppressed. Cooldown still applies.
  - 'threshold': arm when windowed NPC damage >= pct% of a reference
    base-HP pool. BUT if HP is None/unknown it DEGRADES to any-damage
    rather than a silent no-flash (the root cause of the missed death flash:
    the ESI-HP gate suppressed the flash whenever HP was unavailable).

Holds: an arm holds its class for window_s (`_until[(char, cls)]`). Cooldowns
(`damage_flash_cooldown_s`, clock `_arm_at[(char, cls)]`) are independent per
(char, class), so an orange arm never delays red: a hostile landing
mid-ratting turns the tile red on the very next read. A read inside a class's
cooldown does NOT extend that class's hold. kind = "player" while the red
hold is live, else "npc" while the orange hold is live, else None — so red
lasts window_s past the last player arm, then falls back to orange if NPC
damage is still arming. ONE pulse clock per character (`_since[char]`)
starts with the first live hold of either class and survives re-arms AND
red<->orange changes (the colour switches, the rhythm doesn't restart); it
is cleared once both holds have expired — and ALSO at the start of any read
that finds no hold live, because the tick does not read a character while
damage flash is off, on the login screen or while its tile is retired, so a
hold can expire unobserved and must not hand its stale clock to the next one.

Since 2026-09-26 the tracker OWNS all hold bookkeeping (fc_gui's former
per-character hold-end / pulse-start dicts are gone) and the legacy
class-agnostic boolean predicate is retired. Because the tick reads this
inside its per-tile try — an exception here retires a live preview tile —
every cfg value is coerced defensively (a hand-edited config must never
raise).

HP values (threshold mode) are BASE dogma hull HP (fitted ships have more) — the
UI labels this as an approximation.

Threading: every caller is on the Tk thread (ingest via fc_gui's _post_ui,
reads via the preview tick), so the unlocked mutations here (_hits, the hold
dicts, _retention_s) are single-threaded BY THAT INVARIANT — a future
off-thread caller must add locking."""
from __future__ import annotations

from collections import defaultdict, deque

# Default border colours, single-sourced here (fc_gui's _PREVIEW_DEFAULTS and
# the Settings swatches reference these): player damage red — the
# long-standing `damage_flash_color` — and NPC damage orange —
# `damage_flash_npc_color`, hue ~30 deg, between the red (~3 deg) and the
# decloak yellow #ffcc00 (~48 deg).
PLAYER_COLOR_DEFAULT = "#ff3b30"
NPC_COLOR_DEFAULT = "#ff8000"

# add()-side retention horizon (seconds) FLOOR, independent of the reader's
# own per-read window prune. This is a floor, not a fixed bound: the reader,
# flash_state(), stretches the per-instance self._retention_s (see
# DamageFlashTracker.__init__ and _note_reader_window below) to at least 2x
# the largest damage_flash_window_s it has actually been asked for, so
# add()'s prune can never undercut a window a read has ever used. The floor
# only ever rises, never falls, for the lifetime of a tracker.
#
# Why a floor is needed at all: while the "Damage flash" toggle is OFF, the
# reader (and therefore neither the stretch above nor its own prune) is
# never called, but GamelogMonitor keeps calling add()
# unconditionally — nothing else would ever prune _hits. This 120s floor
# bounds memory in that regime. It is semantics-free there precisely
# BECAUSE no reader exists yet to have a window opinion.
#
# NOT coupled to the Settings "Window s" spinbox's displayed 1..60 range —
# that Tk from_/to only clamps the spinbox's arrow buttons. The store path
# itself has no clamp (code inspection: fc_gui's _PREVIEW_NATIVE_VARS int
# cast writes the value straight into cfg, unranged); tests/test_preview_
# settings.py proves an out-of-range value parked directly in config (99)
# survives a save/apply round trip untouched — not that typing 300 into
# the spinbox does. That is exactly why retention adapts to the reader
# instead of trusting a UI cap that doesn't actually bind.
#
# Two triggers cause a transient truncated-history read, both self-healing
# within one window span as add() keeps ingesting under the now-stretched
# retention:
#   1. A toggle-off -> on transition with a large stored window: the first
#      read(s) only see what this floor retained (up to 120s of history).
#   2. A mid-session window raise of MORE THAN 2x the largest window ever
#      read: each such raise costs one truncated read episode. Raises
#      within 2x cost nothing at all — that is what the 2.0 multiplier buys.
_RETENTION_S = 120.0

# Hard cap on hits retained per character (deque maxlen), independent of the
# time-based retention above. Storm insurance: a gamelog rotation misdetect
# can reseed a big file from byte 0 and replay thousands of incoming-damage
# lines in a single poll, effectively stamped with the same `now` (events
# are marshaled one at a time, each stamping its own time.monotonic(), so a
# burst really spans micro-to-milliseconds rather than one literal instant —
# the conclusion below is unchanged). The retention prune above cannot drop
# any of those for a full retention window since none are yet "old", so
# this cap bounds that transient. Oldest-first eviction is a floor, not an
# absolute guarantee: once more than _MAX_HITS hits sit inside a single
# window, the windowed sum at the cap is still at least _MAX_HITS x the
# smallest hit amount seen, which exceeds any realistic base-HP threshold
# for realistic hit amounts.
_MAX_HITS = 8192


def _new_hits():
    # Module-level factory (not a lambda) so DamageFlashTracker — whose
    # _hits is a defaultdict built from this — stays picklable.
    return deque(maxlen=_MAX_HITS)


def _hit_class(source) -> str:
    """Stored class for an add() `source`. EXACT match on "npc" only — the
    classifier's "unknown", None, "", "NPC", non-strings ... are all PLAYER
    (the fail-safe direction: never demote an unclassifiable hit)."""
    return "npc" if source == "npc" else "player"


def _coerce_window_s(cfg) -> float:
    """damage_flash_window_s, coerced defensively: a hand-edited config can
    carry a non-numeric value ("5", None, "garbage"). The tracker is the ONE
    owner of this coercion — it sizes both the windowed sums AND each hold
    (fc_gui's tick stopped reading the key when it cut over to
    flash_state()). `or 5` is right HERE (a 0 s window is meaningless) but
    NOT for the cooldown below."""
    try:
        return float(cfg.get("damage_flash_window_s", 5) or 5)
    except (TypeError, ValueError):
        return 5.0


def _coerce_cooldown_s(cfg) -> float:
    """damage_flash_cooldown_s for flash_state(). 0 is a VALID stored cooldown
    (the Settings spinbox starts at 0 = re-arm on every read), so NEVER
    `float(value or 3)` — that would silently turn 0 into 3. Only a value
    float() rejects (garbage string, None, a list ...) falls back to 3.0."""
    try:
        return float(cfg.get("damage_flash_cooldown_s", 3))
    except (TypeError, ValueError):
        return 3.0


def _coerce_pct(cfg) -> float:
    """damage_flash_pct for the NPC threshold. The Settings spinbox stores an
    int, but a hand-edited value ("20", None, "abc", a list ...) must never
    raise here — the tick would retire a live tile on every NPC hit.
    Anything float() rejects falls back to the 10 % default."""
    try:
        return float(cfg.get("damage_flash_pct", 10))
    except (TypeError, ValueError):
        return 10.0


def _reference_pool(hp: dict, reference: str):
    """Return the base-HP number to take pct% of, or None if unknowable.
    A non-string reference (hand-edited config) reads as the default
    'weakest' — an unhashable one would otherwise raise on the dict lookup."""
    if not hp:
        return None
    layers = {k: hp.get(k) for k in ("shield", "armor", "hull")}
    present = {k: v for k, v in layers.items() if isinstance(v, (int, float)) and v > 0}
    if not present:
        return None
    if isinstance(reference, str) and reference in present:
        return present[reference]
    if reference == "total":
        return sum(present.values())
    # "weakest" (default) or an unknown/absent reference → smallest present layer
    return min(present.values())


class DamageFlashTracker:
    def __init__(self):
        # maxlen is the storm cap (_MAX_HITS); the time-based prune in add()
        # below handles the long-session toggle-off case that maxlen alone
        # doesn't bound quickly enough.
        self._hits: dict[str, deque] = defaultdict(_new_hits)  # key -> deque[(t, dmg, cls)]
        # flash_state() bookkeeping, keyed (char_key, cls), cls in
        # {"player", "npc"}: _until = when that class's hold ends; _arm_at =
        # when it last armed (its cooldown clock). _since = the per-char
        # pulse clock. Bounded: _until / _since entries are dropped on
        # expiry; _arm_at keeps at most 2 entries per character ever seen.
        self._until: dict[tuple[str, str], float] = {}
        self._arm_at: dict[tuple[str, str], float] = {}
        self._since: dict[str, float] = {}
        # Adaptive retention floor (see _RETENTION_S above). flash_state()
        # stretches this to at least 2x the largest window_s it has been
        # asked for; it never shrinks for the lifetime of this tracker.
        self._retention_s = _RETENTION_S

    def add(self, char_key: str, amount: int, now: float,
            source="player") -> None:
        if amount and amount > 0:
            dq = self._hits[char_key]
            dq.append((now, amount, _hit_class(source)))
            # Bound memory even when no reader is ever called (flash toggle
            # OFF) — see _RETENTION_S above for the horizon rationale.
            # self._retention_s starts at the _RETENTION_S floor and is
            # stretched by flash_state() once it is read.
            while dq and now - dq[0][0] > self._retention_s:
                dq.popleft()

    def _note_reader_window(self, window_s: float) -> None:
        # Declare this reader's window to add()'s retention prune so it can
        # never undercut a window some reader has actually asked for. The
        # spinbox's 1..60 range binds only its arrow buttons — a typed
        # window can be far larger, so this floor is reader-informed rather
        # than UI-trusted. Only ever rises for this tracker's lifetime.
        self._retention_s = max(self._retention_s, 2.0 * window_s)

    def _windowed_sums(self, char_key, now, window_s):
        """(player_sum, npc_sum) over the window, after a STRICT
        `now - t > window_s` prune (a hit exactly window_s old still counts).
        Reads without creating an entry for a character that never took a
        hit."""
        dq = self._hits.get(char_key)
        if not dq:
            return 0, 0
        while dq and now - dq[0][0] > window_s:
            dq.popleft()
        player = npc = 0
        for _t, dmg, cls in dq:
            if cls == "npc":
                npc += dmg
            else:
                player += dmg
        return player, npc

    @staticmethod
    def _npc_trigger(npc_sum, hp, cfg) -> bool:
        """Does the NPC sum ALONE satisfy the flash mode?"""
        if npc_sum <= 0:
            return False
        # Absent mode key => 'any'. 'threshold' with unknown HP DEGRADES to
        # any-damage — it must never be a silent no-flash.
        if cfg.get("damage_flash_mode", "any") == "threshold":
            pool = _reference_pool(hp, cfg.get("damage_flash_reference", "weakest"))
            if pool is not None:
                return npc_sum >= pool * (_coerce_pct(cfg) / 100.0)
        return True

    def _arm(self, char_key, cls, now, window_s, cooldown_s) -> None:
        """(Re)arm one class's hold unless that class is inside its OWN
        cooldown — a blocked re-arm leaves the live hold un-extended."""
        key = (char_key, cls)
        last = self._arm_at.get(key)
        if last is not None and (now - last) < cooldown_s:
            return
        self._arm_at[key] = now
        self._until[key] = now + window_s

    def _hold_live(self, key, now) -> bool:
        until = self._until.get(key)
        if until is None:
            return False
        if now < until:
            return True
        del self._until[key]                  # expired: drop it
        return False

    def flash_state(self, char_key, hp, cfg, now: float):
        """-> (kind, since): kind in {None, "player", "npc"}; since = the
        character's pulse-clock start (None when kind is None). See the
        module docstring for the per-class rules."""
        # Coerce BEFORE the stretch so both it and the sums see the coerced
        # value — a garbage window must never raise (see the module notes).
        window_s = _coerce_window_s(cfg)
        self._note_reader_window(window_s)
        # No hold live at the START of this read -> the pulse clock is stale:
        # the tick skips a character while damage flash is off, on the login
        # screen or while its tile is retired, so a hold can expire with no
        # read to clear `_since`. Drop it so a new hold starts a FRESH clock.
        # (Evaluating the holds here also drops expired entries; _arm below
        # rewrites any hold it arms, and cooldowns read _arm_at, not _until.)
        if not (self._hold_live((char_key, "player"), now)
                or self._hold_live((char_key, "npc"), now)):
            self._since.pop(char_key, None)
        player_sum, npc_sum = self._windowed_sums(char_key, now, window_s)
        cooldown_s = _coerce_cooldown_s(cfg)
        # Player damage: arms red in BOTH modes, no threshold.
        if player_sum > 0:
            self._arm(char_key, "player", now, window_s, cooldown_s)
        # NPC damage: the toggle, then the mode on the NPC sum alone. Toggle
        # OFF drops the hold AND the cooldown clock, so re-ticking the box
        # re-arms orange on the very next read.
        if not cfg.get("damage_flash_npc", True):
            self._until.pop((char_key, "npc"), None)
            self._arm_at.pop((char_key, "npc"), None)
        elif self._npc_trigger(npc_sum, hp, cfg):
            self._arm(char_key, "npc", now, window_s, cooldown_s)
        # Evaluate BOTH holds so an expired one is always dropped.
        player_live = self._hold_live((char_key, "player"), now)
        npc_live = self._hold_live((char_key, "npc"), now)
        kind = "player" if player_live else ("npc" if npc_live else None)
        if kind is None:
            self._since.pop(char_key, None)
            return None, None
        return kind, self._since.setdefault(char_key, now)
