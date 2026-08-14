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

# add()-side retention horizon (seconds), independent of should_flash()'s own
# per-read window prune. MUST stay >= the largest configurable
# damage_flash_window_s — the settings spinbox caps that at 60s (fc_gui.py
# Spinbox(from_=1, to=60)); 120 gives 2x margin. Because _windowed_sum()
# already discards anything older than window_s on every read, entries
# beyond this horizon can never influence a flash decision, making this
# prune semantics-free for should_flash. It exists purely to bound memory
# when the "Damage flash" toggle is OFF: should_flash() (and therefore
# _windowed_sum's prune) is then never called, but GamelogMonitor keeps
# calling add() unconditionally, so nothing else would ever prune _hits. A
# hand-edited config window > 120s would be silently truncated to 120s of
# retained history (acceptable, documented here).
_RETENTION_S = 120.0

# Hard cap on hits retained per character (deque maxlen), independent of the
# time-based retention above. Storm insurance: a gamelog rotation misdetect
# can reseed a big file from byte 0 and replay thousands of incoming-damage
# lines in a single poll, all stamped with the SAME `now` — the retention
# prune above cannot drop any of those for a full 120s since none are yet
# "old". This cap bounds that transient. Oldest-first eviction only matters
# once more than _MAX_HITS hits sit inside a single window, at which point
# any real damage/HP threshold is already exceeded by orders of magnitude,
# so no flash decision can be missed by evicting the oldest.
_MAX_HITS = 8192


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
        self._hits: dict[str, deque] = defaultdict(lambda: deque(maxlen=_MAX_HITS))  # key -> deque[(t, dmg)]
        self._last_flash: dict[str, float] = {}

    def add(self, char_key: str, amount: int, now: float) -> None:
        if amount and amount > 0:
            dq = self._hits[char_key]
            dq.append((now, amount))
            # Bound memory even when should_flash() is never called (flash
            # toggle OFF) — see _RETENTION_S above for the horizon rationale.
            while dq and now - dq[0][0] > _RETENTION_S:
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
