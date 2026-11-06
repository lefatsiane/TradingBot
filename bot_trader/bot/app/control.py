from __future__ import annotations

from datetime import datetime, timedelta

from bot.adapters.gateway import BrokerGateway
from bot.adapters.store import StateStore
from bot.app.executor import Executor
from bot.core.clock import Clock
from bot.core.types import ExitReason

_CLOSE_ALL_CONFIRMATION_WINDOW = timedelta(seconds=30)
_CLOSE_ALL_LOCK_TIMEOUT_S = 5.0


class ControlHandler:
    """Apply the safety controls exposed to the private Telegram chat."""

    def __init__(
        self,
        store: StateStore,
        clock: Clock,
        *,
        gateway: BrokerGateway | None = None,
        executor: Executor | None = None,
        magic_number: int | None = None,
    ) -> None:
        if (gateway is None) != (executor is None):
            raise ValueError("gateway and executor must be provided together")
        if gateway is not None and (magic_number is None or magic_number <= 0):
            raise ValueError("A positive magic_number is required for broker controls")
        self._store = store
        self._clock = clock
        self._gateway = gateway
        self._executor = executor
        self._magic_number = magic_number
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
                return self.retry_close_all()

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

    def retry_close_all(self) -> str:
        if not self._store.get_flag("close_all_pending"):
            return "No close-all operation is pending."
        if self._gateway is None or self._executor is None or self._magic_number is None:
            return "Close-all queued; entries stopped."

        self._store.set_meta(
            "close_all_last_attempt_utc",
            self._clock.now_utc().isoformat(),
        )
        try:
            self._gateway.run_serialized(
                lambda _: None,
                lock_timeout_s=_CLOSE_ALL_LOCK_TIMEOUT_S,
            )
            positions = self._gateway.positions(self._magic_number)
            for position in positions:
                self._executor.close_position(position.ticket, ExitReason.CLOSE_ALL)
            remaining = self._gateway.positions(self._magic_number)
        except (ConnectionError, TimeoutError):
            return "Close-all remains pending; broker unavailable, retrying."

        if not remaining:
            self._store.set_flag("close_all_pending", False)
            return "All bot positions closed; new entries remain stopped."
        return "Close-all remains pending; protected positions will be retried."

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
