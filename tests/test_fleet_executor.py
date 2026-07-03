# tests/test_fleet_executor.py — Phase B: pure ledger + executor tests (no Tk/net)
import pytest

import fleet_executor
from fleet_executor import FleetTokenLedger, cost_for_status


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def test_cost_for_status_table():
    assert cost_for_status(204) == 2
    assert cost_for_status(200) == 2
    assert cost_for_status(304) == 1
    assert cost_for_status(404) == 5
    assert cost_for_status(420) == 5
    assert cost_for_status(500) == 0
    assert cost_for_status(503) == 0


def test_ledger_remaining_starts_full_and_decrements():
    clk = _Clock()
    led = FleetTokenLedger(budget=1800, window_s=900, now=clk)
    assert led.remaining() == 1800
    led.spend(2)
    led.spend(5)
    assert led.remaining() == 1800 - 7


def test_ledger_evicts_entries_older_than_window():
    clk = _Clock()
    led = FleetTokenLedger(budget=1800, window_s=900, now=clk)
    led.spend(100)
    assert led.remaining() == 1700
    clk.advance(901)          # first spend now outside the 900s window
    assert led.remaining() == 1800


def test_ledger_reconcile_trusts_header():
    clk = _Clock()
    led = FleetTokenLedger(budget=1800, window_s=900, now=clk)
    led.spend(10)             # local estimate 1790
    led.reconcile(300)        # header says only 300 left → trust it
    assert led.remaining() == 300
    led.spend(2)
    assert led.remaining() == 298


def test_ledger_reconcile_ignores_non_int():
    clk = _Clock()
    led = FleetTokenLedger(now=clk)
    led.spend(10)
    led.reconcile(None)
    led.reconcile("nope")
    assert led.remaining() == 1790


# append — FleetExecutor happy-path (pacing, burst/settle, role-preserve)
import types
from fleet_executor import FleetExecutor, MoveJob


def _job(pid, *, wing=1, squad=10, role="squad_member", source="drag", name=None):
    return MoveJob(pilot_id=pid, pilot_name=name or f"P{pid}",
                   wing_id=wing, squad_id=squad, role=role, source=source)


def _make_executor(on_move, **kw):
    """Build a NON-autostarting executor driven synchronously via drain().

    session is a SimpleNamespace(last_headers={}) so error-path tests can set
    last_headers (Retry-After etc.) as attributes. No real thread is started.
    """
    sleeps = []
    logs = []
    ledger = FleetTokenLedger(now=_Clock())
    ex = FleetExecutor(
        session=types.SimpleNamespace(last_headers={}),
        on_move=on_move,
        post=lambda fn, *a: fn(*a),     # inline
        sleep=lambda s: sleeps.append(s),
        ledger=ledger,
        move_spacing_ms=kw.get("move_spacing_ms", 400),
        burst_cap=kw.get("burst_cap", 25),
        settle_s=kw.get("settle_s", 3),
        on_log=lambda line: logs.append(line),
        on_repoll=kw.get("on_repoll"),
        remaining_needed=kw.get("remaining_needed", lambda job: True),
        autostart=False,               # no worker thread; tests drive drain()
    )
    ex._sleeps = sleeps
    ex._logs = logs
    return ex


def test_two_writes_paced_once_between_no_trailing_sleep():
    seen = []
    ex = _make_executor(lambda job: (seen.append(job.pilot_id), 204)[1])
    ex.submit(_job(1))
    ex.submit(_job(2))
    ex.drain()
    assert seen == [1, 2]
    # exactly one inter-move spacing sleep (0.4s), NO trailing sleep after job 2
    assert ex._sleeps.count(0.4) == 1


def test_ledger_spent_per_write():
    ex = _make_executor(lambda job: 204)
    ex.submit(_job(1))
    ex.submit(_job(2))
    ex.drain()
    assert ex.ledger.remaining() == 1800 - 4   # two 204s @ 2 tokens


def test_three_writes_in_one_run_pace_between_only():
    # Persistent-worker semantics: three jobs submitted before draining form ONE
    # continuous run — spacing is paid BETWEEN consecutive writes (2 gaps for 3
    # writes), never before the first and never trailing after the last.
    seen = []
    ex = _make_executor(lambda job: (seen.append(job.pilot_id), 204)[1])
    for i in (1, 2, 3):
        ex.submit(_job(i))
    ex.drain()
    assert seen == [1, 2, 3]
    assert ex._sleeps.count(0.4) == 2    # 3 writes → exactly 2 inter-move gaps


def test_idle_resets_run_so_next_drain_starts_fresh():
    # A fully drained queue is idle; the next drain is a NEW run and its first
    # write pays no leading spacing (mirrors the worker parking on an empty get).
    ex = _make_executor(lambda job: 204)
    ex.submit(_job(1))
    ex.drain()
    assert ex._sleeps.count(0.4) == 0    # single write, no spacing
    ex.submit(_job(2))
    ex.drain()
    assert ex._sleeps.count(0.4) == 0    # fresh run after idle → still no spacing


def test_burst_cap_triggers_settle_and_repoll():
    repolls = []
    ex = _make_executor(lambda job: 204, burst_cap=3, settle_s=3,
                        on_repoll=lambda: repolls.append(1))
    for i in range(4):
        ex.submit(_job(i))
    ex.drain()
    # after 3 writes: one settle sleep (3.0) + one re-poll before the 4th
    assert 3.0 in ex._sleeps
    assert len(repolls) == 1


def test_repoll_drops_already_correct_remaining_jobs():
    written = []
    # remaining_needed returns False for pilot 99 → dropped after the settle re-poll
    ex = _make_executor(lambda job: (written.append(job.pilot_id), 204)[1],
                        burst_cap=1,
                        remaining_needed=lambda job: job.pilot_id != 99)
    ex.submit(_job(1))
    ex.submit(_job(99))
    ex.drain()
    assert written == [1]           # 99 dropped at the post-burst re-verify


# append — FleetExecutor gates + error paths
import fleet_esi


def test_autosort_gate_blocks_below_360_but_manual_runs():
    written = []
    ex = _make_executor(lambda job: (written.append(job.pilot_id), 204)[1])
    ex.ledger.reconcile(300)                 # remaining 300 (< 360, >= 180)
    ex.submit(_job(1, source="autosort"))    # blocked
    ex.submit(_job(2, source="drag"))        # manual → runs
    ex.drain()
    assert written == [2]
    assert any("skipped" in l.lower() or "blocked" in l.lower() for l in ex._logs)


def test_hard_gate_below_180_blocks_all_but_manual_warns_and_runs():
    written = []
    ex = _make_executor(lambda job: (written.append(job.pilot_id), 204)[1])
    ex.ledger.reconcile(150)                 # remaining 150 (< 180)
    ex.submit(_job(1, source="autosort"))    # blocked
    ex.submit(_job(2, source="apply"))       # manual → warn + run
    ex.drain()
    assert written == [2]


def test_429_sleeps_retry_after_and_freezes(monkeypatch):
    calls = {"n": 0}

    def on_move(job):
        calls["n"] += 1
        return 429

    ex = _make_executor(on_move)
    ex.session.last_headers = {"Retry-After": "7"}   # SimpleNamespace attr
    ex.submit(_job(1))
    ex.submit(_job(2))
    ex.drain()
    # 429 froze fleet ops: only the first job attempted, Retry-After slept
    assert calls["n"] == 1
    assert 7.0 in ex._sleeps
    assert any("429" in l for l in ex._logs)


def test_three_consecutive_4xx_aborts_queue_with_names():
    attempted = []

    def on_move(job):
        attempted.append(job.pilot_name)
        return 400

    ex = _make_executor(on_move)
    for i in (1, 2, 3, 4):
        ex.submit(_job(i, name=f"Pilot{i}"))
    ex.drain()
    assert attempted == ["Pilot1", "Pilot2", "Pilot3"]   # 4th never attempted
    assert any("Pilot3" in l for l in ex._logs)
    assert any("abort" in l.lower() for l in ex._logs)


def test_boss_lost_aborts_and_notifies():
    notified = []

    def on_move(job):
        raise fleet_esi.FleetESIError("boss_lost", status=403)

    ledger = FleetTokenLedger(now=_Clock())
    ex = FleetExecutor(
        session=types.SimpleNamespace(last_headers={}), on_move=on_move,
        post=lambda fn, *a: fn(*a), sleep=lambda s: None, ledger=ledger,
        on_log=lambda l: None, on_boss_lost=lambda: notified.append(1),
        autostart=False)
    ex.submit(_job(1))
    ex.submit(_job(2))
    ex.drain()
    assert notified == [1]


# append — hardening: an unexpected on_move error must not kill the worker/queue
def test_worker_survives_unexpected_on_move_error():
    """A non-FleetESIError raised by on_move for one job is downgraded to a
    non-fatal per-job failure: the run continues, the next job executes normally,
    an 'unexpected error' line is logged, and nothing escapes drain().

    Regression for the shadowing-import bug: an UnboundLocalError in the real
    _executor_on_move escaped _perform → killed the persistent worker → every
    later queued job was stranded forever."""
    attempted = []

    def on_move(job):
        attempted.append(job.pilot_id)
        if job.pilot_id == 1:
            raise RuntimeError("boom in the ESI writer")
        return 204

    ex = _make_executor(on_move)
    ex.submit(_job(1))
    ex.submit(_job(2))
    # Must NOT raise — the unexpected error is contained inside _perform.
    ex.drain()

    assert attempted == [1, 2]                       # both jobs were attempted
    assert any("unexpected error" in l for l in ex._logs)
    assert any("RuntimeError" in l for l in ex._logs)
    # The first job's failure was non-fatal: no abort, counter untouched, and the
    # second job spent its normal 2-token 204 cost (the failed job spent nothing).
    assert ex._aborted is False
    assert ex._consecutive_4xx == 0
    assert ex.ledger.remaining() == 1800 - 2         # only job 2's 204 charged


def test_submit_restarts_dead_worker(monkeypatch):
    """Self-healing submit(): if the worker was started but its thread has died,
    submit() restarts it before enqueueing (start() no-ops when alive)."""
    ex = _make_executor(lambda job: 204)   # drain-only, _worker is None

    started = []
    monkeypatch.setattr(ex, "start", lambda: started.append(1))

    # Simulate a previously-started-but-now-dead worker (thread-free).
    ex._worker = types.SimpleNamespace(is_alive=lambda: False)
    ex.submit(_job(1))
    assert started == [1]                   # dead worker → start() called
    assert ex._q.qsize() == 1               # job still enqueued

    # A live worker must NOT trigger a restart.
    started.clear()
    ex._worker = types.SimpleNamespace(is_alive=lambda: True)
    ex.submit(_job(2))
    assert started == []
    assert ex._q.qsize() == 2
