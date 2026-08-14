"""Per-character damage-flash decision engine (pure). All time is caller-supplied
(`now`) so tests inject a clock; no wall-clock reads here.

Two modes (cfg['damage_flash_mode']):
  - 'any' (DEFAULT, and the default when the key is ABSENT): flash whenever any
    windowed incoming damage > 0 within the window. NO HP, NO ESI — this is the
    log-only path that can never be silently suppressed. Cooldown still applies.
  - 'threshold': flash when windowed incoming damage >= pct% of a reference
    base-HP pool. BUT if HP is None/unknown it DEGRADES to any-damage rather
    than returning a silent False (the root cause of the missed death flash:
    the ESI-HP gate suppressed the flash whenever HP was unavailable).

HP values (threshold mode) are BASE dogma hull HP (fitted ships have more) — the
UI labels this as an approximation."""
from __future__ import annotations

from collections import defaultdict, deque

# add()-side retention horizon (seconds) FLOOR, independent of
# should_flash()'s own per-read window prune. This is a floor, not a fixed
# bound: should_flash() stretches the per-instance self._retention_s (see
# DamageFlashTracker.__init__/should_flash below) to at least 2x the
# largest damage_flash_window_s any reader has actually asked for, so
# add()'s prune can never undercut a window a reader has ever used. The
# floor only ever rises, never falls, for the lifetime of a tracker.
#
# Why a floor is needed at all: while the "Damage flash" toggle is OFF,
# should_flash() (and therefore the stretch above, and _windowed_sum's own
# prune) is never called, but GamelogMonitor keeps calling add()
# unconditionally — nothing else would ever prune _hits. This 120s floor
# bounds memory in that regime. It is semantics-free there precisely
# BECAUSE no reader exists yet to have a window opinion.
#
# NOT coupled to the Settings "Window s" spinbox's displayed 1..60 range —
# that Tk from_/to only clamps the spinbox's arrow buttons; a typed/
# hand-edited value stores unclamped (e.g. 300 stores as 300, see
# tests/test_preview_settings.py). That is exactly why retention adapts to
# the reader instead of trusting a UI cap that doesn't actually bind.
#
# On a toggle-off -> on transition with a large stored window, the first
# read(s) only see what this floor retained (up to 120s of history);
# behavior self-heals within one window span as add() keeps ingesting
# under the now-stretched retention.
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


def _reference_pool(hp: dict, reference: str):
    """Return the base-HP number to take pct% of, or None if unknowable."""
    if not hp:
        return None
    layers = {k: hp.get(k) for k in ("shield", "armor", "hull")}
    present = {k: v for k, v in layers.items() if isinstance(v, (int, float)) and v > 0}
    if not present:
        return None
    if reference in present:
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
        self._hits: dict[str, deque] = defaultdict(_new_hits)  # key -> deque[(t, dmg)]
        self._last_flash: dict[str, float] = {}
        # Adaptive retention floor (see _RETENTION_S above). should_flash()
        # stretches this to at least 2x the largest window_s any reader has
        # asked for; it never shrinks for the lifetime of this tracker.
        self._retention_s = _RETENTION_S

    def add(self, char_key: str, amount: int, now: float) -> None:
        if amount and amount > 0:
            dq = self._hits[char_key]
            dq.append((now, amount))
            # Bound memory even when should_flash() is never called (flash
            # toggle OFF) — see _RETENTION_S above for the horizon rationale.
            # self._retention_s starts at the _RETENTION_S floor and is
            # stretched by should_flash() once a reader exists.
            while dq and now - dq[0][0] > self._retention_s:
                dq.popleft()

    def _windowed_sum(self, char_key, now, window_s):
        dq = self._hits[char_key]
        while dq and now - dq[0][0] > window_s:
            dq.popleft()
        return sum(d for _, d in dq)

    def _cooldown_ok(self, char_key, cfg, now: float) -> bool:
        """True if the per-char cooldown has elapsed; arms it on True."""
        last = self._last_flash.get(char_key)
        cooldown = cfg.get("damage_flash_cooldown_s", 3)
        if last is not None and (now - last) < cooldown:
            return False
        self._last_flash[char_key] = now      # arm cooldown on a real flash
        return True

    def should_flash(self, char_key, hp, cfg, now: float) -> bool:
        window_s = cfg.get("damage_flash_window_s", 5)
        # Declare this reader's window to add()'s retention prune so it can
        # never undercut a window some reader has actually asked for. The
        # spinbox's 1..60 range binds only its arrow buttons — a typed
        # window can be far larger, so this floor is reader-informed rather
        # than UI-trusted. Only ever rises for this tracker's lifetime.
        self._retention_s = max(self._retention_s, 2.0 * window_s)
        windowed = self._windowed_sum(char_key, now, window_s)
        # Absent mode key => 'any' (the new default). 'threshold' with unknown HP
        # DEGRADES to any-damage — it must never silently return False.
        mode = cfg.get("damage_flash_mode", "any")
        pool = None
        if mode == "threshold":
            pool = _reference_pool(hp, cfg.get("damage_flash_reference", "weakest"))
        if mode == "threshold" and pool is not None:
            threshold = pool * (cfg.get("damage_flash_pct", 10) / 100.0)
            if windowed < threshold:
                return False
        else:
            # 'any' mode, or 'threshold' degraded (HP unknown): flash on any dmg.
            if windowed <= 0:
                return False
        return self._cooldown_ok(char_key, cfg, now)
