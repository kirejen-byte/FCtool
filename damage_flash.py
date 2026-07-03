"""Per-character damage-flash decision engine (pure). Proposal P1:
flash when windowed incoming damage >= pct% of a reference base-HP pool,
with a cooldown between flashes. All time is caller-supplied (`now`) so tests
inject a clock; no wall-clock reads here.

HP values are BASE dogma hull HP (fitted ships have more) — the UI labels this
as an approximation. Unknown HP → never flash (fail safe, never spurious)."""
from __future__ import annotations

from collections import defaultdict, deque


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
        self._hits: dict[str, deque] = defaultdict(deque)   # key -> deque[(t, dmg)]
        self._last_flash: dict[str, float] = {}

    def add(self, char_key: str, amount: int, now: float) -> None:
        if amount and amount > 0:
            self._hits[char_key].append((now, amount))

    def _windowed_sum(self, char_key, now, window_s):
        dq = self._hits[char_key]
        while dq and now - dq[0][0] > window_s:
            dq.popleft()
        return sum(d for _, d in dq)

    def should_flash(self, char_key, hp, cfg, now: float) -> bool:
        pool = _reference_pool(hp, cfg.get("damage_flash_reference", "weakest"))
        if pool is None:
            return False
        window_s = cfg.get("damage_flash_window_s", 5)
        threshold = pool * (cfg.get("damage_flash_pct", 10) / 100.0)
        if self._windowed_sum(char_key, now, window_s) < threshold:
            return False
        last = self._last_flash.get(char_key)
        cooldown = cfg.get("damage_flash_cooldown_s", 3)
        if last is not None and (now - last) < cooldown:
            return False
        self._last_flash[char_key] = now      # arm cooldown on a real flash
        return True
