# kick_pods.py
"""Pure logic behind the fleet "Kick" menu: Kick Pods and Disband Fleet (no Tk).

Kicking is destructive, so every rule here errs toward kicking fewer:
the boss's own character is never a candidate, the confirmed list is
re-checked against a fresh roster right before each kick run, and an
unreadable roster kicks nobody.

Disband Fleet (``disband``) is "set the MOTD to DISBAND_MOTD, then kick
everyone but the FC". ESI has no disband call, so the FC is left alone in a
one-man fleet. The caller disarms the "Auto-update MOTD" link BEFORE the run
(else the next autopush writes the template back over the message).
"""
from __future__ import annotations

MAX_CONSECUTIVE_FAILURES = 3

#: The MOTD Disband Fleet writes before it kicks anyone.
DISBAND_MOTD = "Fleet is over"


def pod_ids(members, capsule_ids, own_id=None) -> set:
    """Character ids of `members` currently in a capsule, minus `own_id`."""
    out = set()
    for m in members or []:
        cid = m.get("character_id")
        if cid and cid != own_id and m.get("ship_type_id") in capsule_ids:
            out.add(cid)
    return out


def member_ids(members, own_id=None) -> set:
    """Character ids of every member of `members` (any ship), minus `own_id`.

    Rows that are not dicts and falsy / missing ids are skipped."""
    out = set()
    for m in members or []:
        if not isinstance(m, dict):
            continue
        cid = m.get("character_id")
        if cid and cid != own_id:
            out.add(cid)
    return out


def kick_confirmed(auth, fleet_id, confirmed_ids, capsule_ids=(), own_id=None,
                   *, select=None):
    """Kick the confirmed pilots who are STILL candidates on a fresh roster.

    `select` is a ``(members) -> set`` candidate function; None (the default)
    means "in a capsule" (``pod_ids`` over `capsule_ids`), the Kick Pods rule.
    Whatever `select` returns, `own_id` is never kicked, and nobody who is not
    in `confirmed_ids` is kicked (a pilot who joined after the confirmation
    stays).

    Returns (kicked, failed, skipped, reread_ok). A failed re-read (None / not
    a list) kicks nobody and returns reread_ok False. `skipped` counts
    confirmed pilots who are no longer candidates (left, swapped ship, or
    gone). Stops after MAX_CONSECUTIVE_FAILURES DELETE failures in a row; the
    pilots not tried count as skipped.
    """
    members = auth.get_fleet_members(fleet_id=fleet_id)
    if not isinstance(members, list):
        return 0, 0, 0, False
    if select is None:
        still = pod_ids(members, capsule_ids, own_id)
    else:
        still = set(select(members) or ())
        still.discard(own_id)
    todo = [c for c in confirmed_ids if c in still and c != own_id]
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


def disband(auth, fleet_id, confirmed_ids, own_id):
    """Disband Fleet's run: MOTD DISBAND_MOTD, then kick the confirmed pilots.

    Returns (motd_ok, kicked, failed, skipped, reread_ok). `motd_ok` is None
    when the MOTD was never attempted, else whether ESI accepted it.

    1. The roster is read first. Unreadable (None / not a list - ESI answers
       403 once the FC is no longer the boss) touches NOTHING: no MOTD, no
       kick. No `own_id` touches nothing either (it could not be kept safe).
    2. The MOTD is written. A failure (False or a raise) does not stop the
       kicks; it is reported through `motd_ok`.
    3. ``kick_confirmed`` re-reads the roster and kicks every confirmed pilot
       still in the fleet, never `own_id`.
    """
    if not own_id:
        return None, 0, 0, 0, False
    if not isinstance(auth.get_fleet_members(fleet_id=fleet_id), list):
        return None, 0, 0, 0, False
    try:
        motd_ok = bool(auth.set_fleet_motd(fleet_id, DISBAND_MOTD))
    except Exception:
        motd_ok = False
    kicked, failed, skipped, reread_ok = kick_confirmed(
        auth, fleet_id, confirmed_ids, own_id=own_id,
        select=lambda members: member_ids(members, own_id))
    return motd_ok, kicked, failed, skipped, reread_ok


def pilots(n: int) -> str:
    """'1 pilot' / 'N pilots'."""
    return f"{n} pilot" if n == 1 else f"{n} pilots"


#: Added to the result whenever Disband turned "Auto-update MOTD" off.
LINK_OFF_LINE = ('"Auto-update MOTD" was turned off; tick it again to '
                 "resume it.")
#: Replaces the MOTD line when an autopush PUT was still in flight after the
#: run, so the template may have landed AFTER the disband message.
MOTD_RISK_LINE = ('An "Auto-update MOTD" push was still running and may have '
                  f'written the template over "{DISBAND_MOTD}". Check the '
                  "MOTD in game.")


def disband_result_text(res, *, link_off=False, motd_risk=False):
    """The result modal for ``disband``'s return (None = the run raised).

    ``link_off``: the run turned "Auto-update MOTD" off first (so something
    DID change even when nothing else did). ``motd_risk``: an autopush PUT
    was still in flight after the run - never claim the MOTD was set.

    Returns (text, tone) with tone "ok" / "warn" / "error"."""
    tail = [LINK_OFF_LINE] if link_off else []
    if res is None:
        return "\n".join(
            ["The disband run failed; the MOTD may have been set and some "
             "pilots may already have been kicked. See fctool.log."] + tail
        ), "error"
    motd_ok, kicked, failed, skipped, reread_ok = res
    if motd_ok is None:
        head = ("Could not read the fleet members: the MOTD was not set and "
                "nobody was kicked." if link_off else
                "Could not read the fleet members, so nothing was changed: "
                "the MOTD was not set and nobody was kicked.")
        return "\n".join([head] + tail), "error"
    if motd_risk:
        motd_line = MOTD_RISK_LINE
    elif motd_ok:
        motd_line = f'The fleet MOTD was set to "{DISBAND_MOTD}".'
    else:
        motd_line = "The MOTD could not be set."
    if not reread_ok:
        return "\n".join([motd_line, "Could not re-read the fleet members, "
                           "so nobody was kicked."] + tail), "error"
    lines = [f"Kicked {pilots(kicked)} from the fleet.", motd_line]
    tone = "ok" if motd_ok and not motd_risk else "warn"
    if skipped:
        lines.append(f"{skipped} not kicked (left the fleet, or the run "
                     "stopped after repeated failures).")
        tone = "warn"
    if failed:
        lines.append(f"{failed} could not be kicked (they may have left, or "
                     "you lost the boss role).")
        tone = "warn"
    lines += tail
    lines.append("You are still in a one-man fleet: ESI has no disband call.")
    return "\n".join(lines), tone
