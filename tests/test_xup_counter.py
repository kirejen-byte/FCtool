from datetime import datetime

from chat_monitor import ChatMessage
from xup_counter import XUpCounter


def _msg(sender: str, message: str) -> ChatMessage:
    return ChatMessage(
        timestamp=datetime(2026, 6, 17, 12, 0, 0),
        sender=sender,
        message=message,
        channel="Fleet",
        raw_line=f"{sender} > {message}",
    )


def test_remove_pilot_removes_and_returns_true():
    c = XUpCounter(threshold=30)
    c.process_message(_msg("Alice", "x"))
    c.process_message(_msg("Bob", "x"))
    assert c.state.count == 2
    assert c.remove_pilot("Alice") is True
    assert c.state.count == 1
    assert "Alice" not in c.state.xups
    assert "Bob" in c.state.xups


def test_remove_pilot_unknown_returns_false():
    c = XUpCounter(threshold=30)
    c.process_message(_msg("Alice", "x"))
    assert c.remove_pilot("Ghost") is False
    assert c.state.count == 1


def test_remove_pilot_recomputes_is_ready():
    c = XUpCounter(threshold=2)
    c.process_message(_msg("Alice", "x"))
    c.process_message(_msg("Bob", "x"))
    assert c.state.is_ready is True
    assert c.remove_pilot("Bob") is True
    assert c.state.is_ready is False
