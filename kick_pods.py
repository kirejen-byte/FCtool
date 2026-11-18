# kick_pods.py
"""Pure logic behind the fleet "Kick Pods" button (no Tk).

Kicking is destructive, so every rule here errs toward kicking fewer:
the boss's own character is never a candidate, the confirmed list is
re-checked against a fresh roster right before each kick run, and an
unreadable roster kicks nobody.
"""
from __future__ import annotations

MAX_CONSECUTIVE_FAILURES = 3


def pod_ids(members, capsule_ids, own_id=None) -> set:
    """Character ids of `members` currently in a capsule, minus `own_id`."""
    out = set()
    for m in members or []:
        cid = m.get("character_id")
        if cid and cid != own_id and m.get("ship_type_id") in capsule_ids:
            out.add(cid)
    return out


def kick_confirmed(auth, fleet_id, confirmed_ids, capsule_ids, own_id=None):
    """Kick the confirmed pilots who are STILL in a capsule on a fresh roster.

    Returns (kicked, failed, skipped, reread_ok). A failed re-read (None / not
    a list) kicks nobody and returns reread_ok False. `skipped` counts
    confirmed pilots no longer in a capsule (left, swapped ship, or gone).
    Stops after MAX_CONSECUTIVE_FAILURES DELETE failures in a row; the pilots
    not tried count as skipped.
    """
    members = auth.get_fleet_members(fleet_id=fleet_id)
    if not isinstance(members, list):
        return 0, 0, 0, False
    still = pod_ids(members, capsule_ids, own_id)
    todo = [c for c in confirmed_ids if c in still]
    skipped = len(confirmed_ids) - len(todo)
    kicked = failed = streak = 0
    for i, cid in enumerate(todo):
        if auth.esi_delete(f"/fleets/{fleet_id}/members/{cid}/"):
            kicked += 1
            streak = 0
        else:
            failed += 1
            streak += 1
            if streak >= MAX_CONSECUTIVE_FAILURES:
                skipped += len(todo) - i - 1
                break
    return kicked, failed, skipped, True
