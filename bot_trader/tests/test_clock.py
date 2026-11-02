from datetime import UTC, datetime, timedelta

import pytest

from bot.core.clock import SimClock, SystemClock


def test_system_clock_returns_aware_utc_time() -> None:
    now = SystemClock().now_utc()
    assert now.tzinfo is UTC


def test_sim_clock_normalizes_time_and_can_set_and_advance() -> None:
    clock = SimClock(datetime(2026, 10, 28, 12, tzinfo=UTC))
    clock.set(datetime(2026, 10, 28, 15, tzinfo=UTC))

    assert clock.advance(timedelta(minutes=30)) == datetime(2026, 10, 28, 15, 30, tzinfo=UTC)
    assert clock.now_utc() == datetime(2026, 10, 28, 15, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    "value",
    [datetime(2026, 10, 28, 12), datetime(2026, 10, 28, 12, tzinfo=UTC)],
)
def test_sim_clock_rejects_naive_set_or_initial_time(value: datetime) -> None:
    if value.tzinfo is None:
        with pytest.raises(ValueError, match="timezone-aware"):
            SimClock(value)
    else:
        clock = SimClock(value)
        with pytest.raises(ValueError, match="timezone-aware"):
            clock.set(datetime(2026, 10, 28, 12))


def test_sim_clock_cannot_advance_backwards() -> None:
    clock = SimClock(datetime(2026, 10, 28, 12, tzinfo=UTC))
    with pytest.raises(ValueError, match="negative"):
        clock.advance(timedelta(seconds=-1))
