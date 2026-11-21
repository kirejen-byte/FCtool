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

import time
from typing import NamedTuple, Optional

MAX_CONSECUTIVE_FAILURES = 3
#: Disband re-reads the roster at most this many times per run (each one may
#: wait out MEMBERS_CACHE_S), which bounds the run's length.
MAX_RECHECKS = 5
#: GET /fleets/{id}/members is server-cached ~5 s and the ETag layer replays
#: the same body inside that window: a re-read waits this long after the
#: previous read so it sees a fresh roster.
MEMBERS_CACHE_S = 5.1

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
                   *, select=None, recheck=False, clock=None, sleep=None):
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

    ``recheck`` (Disband only; Kick Pods leaves it False - and then `clock` /
    `sleep` are never called): see ``_kick_run``. ``disband`` calls that
    directly, so it can tell the pilots who left from the ones never tried.
    """
    kicked, failed, skipped, ok, left = _kick_run(
        auth, fleet_id, confirmed_ids, capsule_ids, own_id, select=select,
        recheck=recheck, clock=clock, sleep=sleep)
    return kicked, failed, skipped + left, ok


def _kick_run(auth, fleet_id, confirmed_ids, capsule_ids, own_id, *, select,
              recheck, clock, sleep):
    """``kick_confirmed``'s body. Returns (kicked, failed, skipped, reread_ok,
    left): `left` counts the confirmed pilots found no longer candidates (for
    Disband: gone from the fleet), `skipped` the rest not kicked (never
    tried, or `own_id`). kicked + failed + skipped + left ==
    len(confirmed_ids) whenever reread_ok.

    ``recheck`` (Disband only): a failure streak re-reads the roster instead
    of stopping at once, because pilots leave on their own once the MOTD says
    the fleet is over and a DELETE for a pilot who already left fails. The
    members GET is server-cached (~5 s) and the ETag layer replays the cached
    body, so each re-read first waits until MEMBERS_CACHE_S have passed since
    the previous read (``clock`` / ``sleep``, default time.monotonic /
    time.sleep). An unreadable re-read stops (the boss role is probably
    gone). A readable one moves the streak's pilots who are no longer
    candidates from failed to left, drops the untried ones who left (left
    too), and goes on with the rest - only ever a subset of the first list.
    A re-read where every streak pilot is still there means real failures:
    stop. At most MAX_RECHECKS re-reads; past that a streak stops the run.
    ESI's status code is never consulted.
    """
    members = auth.get_fleet_members(fleet_id=fleet_id)
    if not isinstance(members, list):
        return 0, 0, 0, False, 0
    if recheck:
        clock = clock or time.monotonic
        sleep = sleep or time.sleep
        read_at = clock()

    def candidates(ms):
        if select is None:
            return pod_ids(ms, capsule_ids, own_id)
        out = set(select(ms) or ())
        out.discard(own_id)
        return out

    still = candidates(members)
    todo = [c for c in confirmed_ids if c in still and c != own_id]
    left = sum(1 for c in confirmed_ids if c != own_id and c not in still)
    skipped = len(confirmed_ids) - len(todo) - left
    rereads = MAX_RECHECKS if recheck else 0
    queue = list(todo)
    kicked = failed = 0
    streak = []                    # ids of the current run of failed DELETEs
    while queue:
        cid = queue.pop(0)
        if auth.esi_delete(f"/fleets/{fleet_id}/members/{cid}/"):
            kicked += 1
            streak = []
            continue
        failed += 1
        streak.append(cid)
        if len(streak) < MAX_CONSECUTIVE_FAILURES:
            continue
        if rereads <= 0:
            break
        rereads -= 1
        wait = MEMBERS_CACHE_S - (clock() - read_at)
        if wait > 0:
            sleep(wait)            # else the re-read replays the cached roster
        members = auth.get_fleet_members(fleet_id=fleet_id)
        read_at = clock()
        if not isinstance(members, list):
            break
        now = candidates(members)
        gone = [c for c in streak if c not in now]
        if not gone:
            break                  # all still there: real failures
        failed -= len(gone)
        left += len(gone)
        kept = [c for c in queue if c in now]
        left += len(queue) - len(kept)
        queue = kept
        streak = []
    skipped += len(queue)          # the pilots never tried
    return kicked, failed, skipped, True, left


class DisbandResult(NamedTuple):
    """``disband``'s return. `skipped`: confirmed pilots never tried (the run
    stopped early); `left`: confirmed pilots who left the fleet on their own
    before their kick. kicked + failed + skipped + left == the confirmed
    count whenever reread_ok."""
    motd_ok: Optional[bool]
    kicked: int
    failed: int
    skipped: int
    reread_ok: bool
    left: int = 0


def disband(auth, fleet_id, confirmed_ids, own_id, *, clock=None, sleep=None):
    """Disband Fleet's run: MOTD DISBAND_MOTD, then kick the confirmed pilots.

    Returns a ``DisbandResult`` (motd_ok, kicked, failed, skipped, reread_ok,
    left). `motd_ok` is None when the MOTD was never attempted, else whether
    ESI accepted it.

    1. The roster is read first. Unreadable (None / not a list - ESI answers
       403 once the FC is no longer the boss) touches NOTHING: no MOTD, no
       kick. No `own_id` touches nothing either (it could not be kept safe).
    2. The MOTD is written. A failure (False or a raise) does not stop the
       kicks; it is reported through `motd_ok`.
    3. ``_kick_run`` re-reads the roster and kicks every confirmed pilot
       still in the fleet, never `own_id`. Pilots leave on their own once the
       MOTD is up, so a failure streak re-reads the roster (``recheck``,
       after the members cache expires - `clock` / `sleep`) instead of
       stopping: the leavers count as `left`, not failed.
    """
    if not own_id:
        return DisbandResult(None, 0, 0, 0, False)
    if not isinstance(auth.get_fleet_members(fleet_id=fleet_id), list):
        return DisbandResult(None, 0, 0, 0, False)
    try:
        motd_ok = bool(auth.set_fleet_motd(fleet_id, DISBAND_MOTD))
    except Exception:
        motd_ok = False
    kicked, failed, skipped, reread_ok, left = _kick_run(
        auth, fleet_id, confirmed_ids, (), own_id,
        select=lambda members: member_ids(members, own_id), recheck=True,
        clock=clock, sleep=sleep)
    return DisbandResult(motd_ok, kicked, failed, skipped, reread_ok, left)


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
#: The same risk when the disband message itself was never accepted.
MOTD_FAILED_RISK_LINE = ('The MOTD could not be set, and an "Auto-update '
                         'MOTD" push was still running. Check the MOTD in '
                         "game.")


def disband_result_text(res, *, link_off=False, motd_risk=False):
    """The result modal for ``disband``'s return (None = the run raised; a
    bare 5-tuple without `left` reads as left 0).

    "One-man fleet" only when everyone not kicked left on their own (nothing
    failed, nobody untried).

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
    motd_ok, kicked, failed, skipped, reread_ok = tuple(res)[:5]
    left = res[5] if len(res) > 5 else 0
    if motd_ok is None:
        head = ("Could not read the fleet members: the MOTD was not set and "
                "nobody was kicked." if link_off else
                "Could not read the fleet members, so nothing was changed: "
                "the MOTD was not set and nobody was kicked.")
        return "\n".join([head] + tail), "error"
    if motd_risk:
        motd_line = MOTD_RISK_LINE if motd_ok else MOTD_FAILED_RISK_LINE
    elif motd_ok:
        motd_line = f'The fleet MOTD was set to "{DISBAND_MOTD}".'
    else:
        motd_line = "The MOTD could not be set."
    if not reread_ok:
        return "\n".join([motd_line, "Could not re-read the fleet members, "
                           "so nobody was kicked."] + tail), "error"
    lines = [f"Kicked {pilots(kicked)} from the fleet.", motd_line]
    tone = "ok" if motd_ok and not motd_risk else "warn"
    if left:
        lines.append(f"{left} left on their own.")
    if skipped:
        lines.append(f"{skipped} not kicked (the run stopped after repeated "
                     "failures).")
        tone = "warn"
    if failed:
        lines.append(f"{failed} could not be kicked (they may have left, or "
                     "you lost the boss role).")
        tone = "warn"
    lines += tail
    if failed or skipped:
        lines.append("You are still in the fleet, with any pilots who were "
                     "not kicked: ESI has no disband call.")
    else:
        lines.append("You are still in a one-man fleet: ESI has no disband "
                     "call.")
    return "\n".join(lines), tone
