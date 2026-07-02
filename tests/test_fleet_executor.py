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
