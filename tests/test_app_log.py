import logging

from app_log import get_logger


def test_returns_logger():
    assert isinstance(get_logger(), logging.Logger)


def test_idempotent_handlers():
    get_logger()
    before = len(logging.getLogger().handlers)
    get_logger()
    after = len(logging.getLogger().handlers)
    assert after == before
