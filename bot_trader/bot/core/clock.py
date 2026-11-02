from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now_utc(self) -> datetime: ...


class SystemClock:
    def now_utc(self) -> datetime:
        return datetime.now(tz=UTC)


@dataclass(slots=True)
class SimClock:
    _current: datetime

    def __post_init__(self) -> None:
        self._current = _as_utc(self._current)

    def now_utc(self) -> datetime:
        return self._current

    def set(self, value: datetime) -> None:
        self._current = _as_utc(value)

    def advance(self, delta: timedelta) -> datetime:
        if delta < timedelta(0):
            raise ValueError("SimClock cannot advance by a negative duration")
        self._current += delta
        return self._current


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Clock time must be timezone-aware")
    return value.astimezone(UTC)
