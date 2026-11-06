from __future__ import annotations

from datetime import UTC, timedelta

from bot.adapters.store import AlertPayload, StateStore
from bot.core.clock import Clock

_CRASH_LOOP_WINDOW = timedelta(hours=1)
_CRASH_LOOP_LIMIT = 3


class CrashLoopGuard:
    def __init__(self, store: StateStore, clock: Clock) -> None:
        self._store = store
        self._clock = clock

    def record_start(self, *, planned: bool) -> bool:
        now = self._clock.now_utc()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Clock must return a timezone-aware timestamp")
        now = now.astimezone(UTC)
        self._store.record_start(now.isoformat(), planned=int(planned))
        if planned:
            return False

        cutoff = (now - _CRASH_LOOP_WINDOW).isoformat()
        if self._store.unplanned_starts_since(cutoff) < _CRASH_LOOP_LIMIT:
            return False
        self._store.set_flag("stop_new", True)
        self._store.enqueue_alert(
            AlertPayload(
                now.isoformat(),
                "CRITICAL",
                "Three unplanned starts occurred within one hour; new entries stopped",
            )
        )
        return True
