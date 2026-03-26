"""
X-Up Counter Module
Counts unique character "x-ups" in fleet chat and fires alerts at threshold.
Resets when the FC types a configurable fire command.
"""

from dataclasses import dataclass, field
from datetime import datetime
from chat_monitor import ChatMessage


@dataclass
class XUpState:
    """Current state of the x-up counter."""
    xups: dict[str, datetime] = field(default_factory=dict)  # sender -> timestamp
    is_ready: bool = False
    fire_count: int = 0  # how many times FIRE has been called this session

    @property
    def count(self) -> int:
        return len(self.xups)


class XUpCounter:
    """
    Tracks x-ups from fleet chat.

    Rules:
    - A message that is exactly the trigger_word (default "x") or starts with
      the trigger_word followed by a space counts as an x-up
    - Each character can only x-up once per cycle (deduped by sender name)
    - When count >= threshold, state.is_ready becomes True
    - When someone types the fire_word (default "FIRE"), counter resets
    """

    def __init__(self, trigger_word: str = "x", fire_word: str = "FIRE",
                 threshold: int = 30, case_sensitive: bool = False,
                 on_ready=None, on_fire=None, on_update=None):
        self.trigger_word = trigger_word
        self.fire_word = fire_word
        self.threshold = threshold
        self.case_sensitive = case_sensitive
        self.on_ready = on_ready    # callback(state)
        self.on_fire = on_fire      # callback(state)
        self.on_update = on_update  # callback(state)
        self.state = XUpState()

    def _normalize(self, text: str) -> str:
        if self.case_sensitive:
            return text.strip()
        return text.strip().lower()

    def _is_xup(self, message: str) -> bool:
        msg = self._normalize(message)
        trigger = self._normalize(self.trigger_word)
        # Exact match or starts with trigger + space (e.g., "x Maelstrom")
        return msg == trigger or msg.startswith(trigger + " ")

    def _is_fire(self, message: str) -> bool:
        msg = self._normalize(message)
        fire = self._normalize(self.fire_word)
        return msg == fire or msg.startswith(fire + " ") or msg.startswith(fire + "!")

    def process_message(self, msg: ChatMessage):
        """Process a chat message and update x-up state."""
        if self._is_fire(msg.message):
            self.state.fire_count += 1
            self.state.xups.clear()
            self.state.is_ready = False
            if self.on_fire:
                self.on_fire(self.state)
            if self.on_update:
                self.on_update(self.state)
            return

        if self._is_xup(msg.message):
            was_ready = self.state.is_ready
            self.state.xups[msg.sender] = msg.timestamp
            self.state.is_ready = self.state.count >= self.threshold

            if self.state.is_ready and not was_ready:
                if self.on_ready:
                    self.on_ready(self.state)

            if self.on_update:
                self.on_update(self.state)

    def reset(self):
        """Manual reset."""
        self.state = XUpState()
