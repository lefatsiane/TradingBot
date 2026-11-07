from datetime import UTC, datetime, timedelta

import pytest

from bot.core.timeutil import (
    broker_to_utc,
    broker_wall_time_to_utc,
    month_start_utc,
    next_month_boundary_utc,
    next_rollover_utc,
    next_trading_day_boundary_utc,
    rounded_server_offset_from_tick,
    trading_day_start_utc,
)


@pytest.mark.parametrize(
    ("utc_time", "offset"),
    [
        (datetime(2026, 10, 25, 12, tzinfo=UTC), 3),
        (datetime(2026, 11, 1, 12, tzinfo=UTC), 2),
        (datetime(2027, 3, 14, 12, tzinfo=UTC), 3),
    ],
)
def test_broker_time_conversion_uses_reported_offset(utc_time: datetime, offset: int) -> None:
    server_epoch = int((utc_time + timedelta(hours=offset)).timestamp())
    assert broker_to_utc(server_epoch, offset) == utc_time


@pytest.mark.parametrize(
    ("utc_time", "offset"),
    [
        (datetime(2026, 10, 25, 12, tzinfo=UTC), 3),
        (datetime(2026, 11, 1, 12, tzinfo=UTC), 2),
        (datetime(2027, 3, 14, 12, tzinfo=UTC), 3),
    ],
)
def test_broker_wall_time_conversion_uses_new_york_dst_rule(
    utc_time: datetime, offset: int
) -> None:
    server_epoch = int((utc_time + timedelta(hours=offset)).timestamp())

    assert broker_wall_time_to_utc(server_epoch) == utc_time


def test_broker_wall_time_conversion_rejects_ambiguous_dst_wall_time() -> None:
    ambiguous_server_epoch = int(
        datetime(2026, 11, 1, 8, 30, tzinfo=UTC).timestamp()
    )

    with pytest.raises(ValueError, match="ambiguous"):
        broker_wall_time_to_utc(ambiguous_server_epoch)


def test_tick_offset_rounds_to_nearest_half_hour() -> None:
    utc_time = datetime(2026, 1, 5, 13, 40, tzinfo=UTC)
    tick_time_msc = int((utc_time.timestamp() + 2.26 * 60 * 60) * 1000)

    assert rounded_server_offset_from_tick(tick_time_msc, utc_time) == 2.5


def test_trading_day_boundary_does_not_split_when_offset_changes_on_dst_day() -> None:
    before_offset_change = datetime(2026, 10, 31, 21, 30, tzinfo=UTC)
    after_offset_change = datetime(2026, 10, 31, 22, 30, tzinfo=UTC)

    before_start = trading_day_start_utc(before_offset_change, 3)
    after_start = trading_day_start_utc(after_offset_change, 2)

    assert before_start == after_start == datetime(2026, 10, 31, 21, tzinfo=UTC)


def test_day_and_month_boundaries_use_server_midnight() -> None:
    at_utc = datetime(2026, 11, 15, 12, tzinfo=UTC)
    assert trading_day_start_utc(at_utc, 2) == datetime(2026, 11, 14, 22, tzinfo=UTC)
    assert next_trading_day_boundary_utc(at_utc, 2) == datetime(2026, 11, 15, 22, tzinfo=UTC)
    assert month_start_utc(at_utc, 2) == datetime(2026, 10, 31, 21, tzinfo=UTC)
    assert next_month_boundary_utc(at_utc, 2) == datetime(2026, 11, 30, 22, tzinfo=UTC)


def test_next_trading_day_boundary_is_after_the_input() -> None:
    at_utc = datetime(2026, 11, 1, 21, 30, tzinfo=UTC)
    boundary = next_trading_day_boundary_utc(at_utc, 2)
    assert boundary > at_utc
    assert boundary == datetime(2026, 11, 1, 22, tzinfo=UTC)


@pytest.mark.parametrize(
    ("at_utc", "offset", "expected"),
    [
        (
            datetime(2026, 3, 8, 6, 30, tzinfo=UTC),
            2,
            datetime(2026, 3, 8, 21, tzinfo=UTC),
        ),
        (
            datetime(2026, 3, 8, 7, 30, tzinfo=UTC),
            3,
            datetime(2026, 3, 8, 21, tzinfo=UTC),
        ),
        (
            datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
            3,
            datetime(2026, 11, 1, 22, tzinfo=UTC),
        ),
        (
            datetime(2026, 11, 1, 6, 30, tzinfo=UTC),
            2,
            datetime(2026, 11, 1, 22, tzinfo=UTC),
        ),
    ],
)
def test_next_rollover_uses_the_next_dst_aware_server_midnight(
    at_utc: datetime,
    offset: int,
    expected: datetime,
) -> None:
    assert next_rollover_utc(at_utc, offset) == expected
