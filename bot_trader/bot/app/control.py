from __future__ import annotations

from datetime import datetime, timedelta

from bot.adapters.store import StateStore
from bot.core.clock import Clock

_CLOSE_ALL_CONFIRMATION_WINDOW = timedelta(seconds=30)


class ControlHandler:
    """Apply the safety controls exposed to the private Telegram chat."""

    def __init__(self, store: StateStore, clock: Clock) -> None:
        self._store = store
        self._clock = clock
        self._close_all_deadline: datetime | None = None

    def handle(self, text: str) -> str:
        message = text.strip()
        now = self._clock.now_utc()
        if self._close_all_deadline is not None:
            deadline = self._close_all_deadline
            self._close_all_deadline = None
            if now >= deadline:
                if message == "YES":
                    return "Confirmation expired."
            elif message == "YES":
                self._store.set_flag("stop_new", True)
                self._store.set_flag("close_all_pending", True)
                return "Close-all queued; entries stopped."

        command = message.split(maxsplit=1)[0] if message else ""
        if command == "/status":
            return self._status()
        if command == "/stop_new":
            self._store.set_flag("stop_new", True)
            return "New entries stopped."
        if command == "/resume":
            if any(
                self._store.get_flag(flag)
                for flag in ("halted_daily", "halted_monthly", "halted_error")
            ):
                return "Cannot resume while a safety halt is active."
            self._store.set_flag("stop_new", False)
            return "Entries resumed."
        if command == "/close_all":
            self._close_all_deadline = now + _CLOSE_ALL_CONFIRMATION_WINDOW
            return "Reply YES within 30 seconds to confirm."
        return "Unsupported command."

    def _status(self) -> str:
        flags = (
            "stop_new",
            "close_all_pending",
            "halted_daily",
            "halted_monthly",
            "halted_error",
        )
        return " ".join(
            f"{flag}={'on' if self._store.get_flag(flag) else 'off'}"
            for flag in flags
        )
