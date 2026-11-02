from __future__ import annotations

from datetime import UTC, datetime, time

import pytest

from bot.core.gates import SessionWindow, evaluate_entry_gates
from bot.core.types import ReasonCode

TUESDAY = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
TUESDAY_HOURS = (SessionWindow(1, time(8), time(17)),)


def gate_inputs(**updates: object) -> dict[str, object]:
    values: dict[str, object] = {
        "at_utc": TUESDAY,
        "symbol_enabled": True,
        "sessions": TUESDAY_HOURS,
        "spread_price": 0.0001,
        "point": 0.00001,
        "max_spread_points": 10.0,
        "candle_range": 0.003,
        "atr": 0.001,
        "max_candle_range_atr": 3.0,
        "rollover_at_utc": datetime(2026, 10, 27, 17, 0, tzinfo=UTC),
        "rollover_block_before_min": 10,
        "rollover_block_after_min": 30,
        "news_calendar_valid": True,
        "news_blocked": False,
        "weekend_close_utc": datetime(2026, 10, 30, 23, 0, tzinfo=UTC),
    }
    values.update(updates)
    return values


def test_gate_accepts_values_at_spread_and_volatility_limits() -> None:
    result = evaluate_entry_gates(**gate_inputs())

    assert result.allowed
    assert result.reason is None


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"symbol_enabled": False}, ReasonCode.SYMBOL_DISABLED),
        ({"sessions": ()}, ReasonCode.OUTSIDE_HOURS),
        ({"spread_price": 0.00011}, ReasonCode.SPREAD_WIDE),
        ({"candle_range": 0.00301}, ReasonCode.SKIP_VOLATILITY),
        ({"news_calendar_valid": False}, ReasonCode.NEWS_CALENDAR_STALE),
    ],
)
def test_gate_rejects_disabled_or_unsafe_conditions(
    updates: dict[str, object], reason: ReasonCode
) -> None:
    result = evaluate_entry_gates(**gate_inputs(**updates))

    assert not result.allowed
    assert result.reason is reason


def test_gate_blocks_both_sides_of_rollover_window() -> None:
    before_window = evaluate_entry_gates(
        **gate_inputs(
            at_utc=datetime(2026, 10, 27, 16, 49, tzinfo=UTC),
            sessions=(SessionWindow(1, time(8), time(18)),),
        )
    )
    before = evaluate_entry_gates(
        **gate_inputs(
            at_utc=datetime(2026, 10, 27, 16, 50, tzinfo=UTC),
            sessions=(SessionWindow(1, time(8), time(18)),),
        )
    )
    after_window = evaluate_entry_gates(
        **gate_inputs(
            at_utc=datetime(2026, 10, 27, 17, 31, tzinfo=UTC),
            sessions=(SessionWindow(1, time(8), time(18)),),
        )
    )
    after = evaluate_entry_gates(
        **gate_inputs(
            at_utc=datetime(2026, 10, 27, 17, 30, tzinfo=UTC),
            sessions=(SessionWindow(1, time(8), time(18)),),
        )
    )

    assert before_window.allowed
    assert before.reason is ReasonCode.ROLLOVER
    assert after.reason is ReasonCode.ROLLOVER
    assert after_window.allowed


def test_news_gate_blocks_when_calendar_reports_an_event_window() -> None:
    result = evaluate_entry_gates(**gate_inputs(news_blocked=True))

    assert result.reason is ReasonCode.NEWS_WINDOW


def test_weekend_gate_blocks_entries_then_requests_flattening() -> None:
    close = datetime(2026, 10, 30, 23, 0, tzinfo=UTC)
    friday_hours = (SessionWindow(4, time(8), time(23)),)
    blocked = evaluate_entry_gates(
        **gate_inputs(
            at_utc=datetime(2026, 10, 30, 21, 0, tzinfo=UTC),
            sessions=friday_hours,
            rollover_at_utc=datetime(2026, 10, 30, 17, 0, tzinfo=UTC),
            weekend_close_utc=close,
        )
    )
    just_before = evaluate_entry_gates(
        **gate_inputs(
            at_utc=datetime(2026, 10, 30, 20, 59, tzinfo=UTC),
            sessions=friday_hours,
            rollover_at_utc=datetime(2026, 10, 30, 17, 0, tzinfo=UTC),
            weekend_close_utc=close,
        )
    )
    no_flatten = evaluate_entry_gates(
        **gate_inputs(
            at_utc=datetime(2026, 10, 30, 22, 29, tzinfo=UTC),
            sessions=friday_hours,
            rollover_at_utc=datetime(2026, 10, 30, 17, 0, tzinfo=UTC),
            weekend_close_utc=close,
        )
    )
    flatten = evaluate_entry_gates(
        **gate_inputs(
            at_utc=datetime(2026, 10, 30, 22, 30, tzinfo=UTC),
            sessions=friday_hours,
            rollover_at_utc=datetime(2026, 10, 30, 17, 0, tzinfo=UTC),
            weekend_close_utc=close,
        )
    )

    assert just_before.allowed
    assert blocked.reason is ReasonCode.WEEKEND_WINDOW
    assert not blocked.flatten_all
    assert no_flatten.reason is ReasonCode.WEEKEND_WINDOW
    assert not no_flatten.flatten_all
    assert flatten.reason is ReasonCode.WEEKEND_WINDOW
    assert flatten.flatten_all


def test_overnight_session_matches_previous_weekday_after_midnight() -> None:
    session = SessionWindow(1, time(20), time(3))

    before_open = evaluate_entry_gates(
        **gate_inputs(
            at_utc=datetime(2026, 10, 27, 19, 59, tzinfo=UTC),
            sessions=(session,),
        )
    )
    evening = evaluate_entry_gates(
        **gate_inputs(
            at_utc=datetime(2026, 10, 27, 20, 0, tzinfo=UTC),
            sessions=(session,),
        )
    )
    early_morning = evaluate_entry_gates(
        **gate_inputs(
            at_utc=datetime(2026, 10, 28, 2, 59, tzinfo=UTC),
            sessions=(session,),
        )
    )
    session_close = evaluate_entry_gates(
        **gate_inputs(
            at_utc=datetime(2026, 10, 28, 3, 0, tzinfo=UTC),
            sessions=(session,),
        )
    )

    assert not before_open.allowed
    assert before_open.reason is ReasonCode.OUTSIDE_HOURS
    assert evening.allowed
    assert early_morning.allowed
    assert session_close.reason is ReasonCode.OUTSIDE_HOURS


def test_gate_rejects_non_finite_measurements() -> None:
    with pytest.raises(ValueError, match="finite"):
        evaluate_entry_gates(**gate_inputs(spread_price=float("nan")))


def test_gate_rejects_stale_weekend_close_time() -> None:
    with pytest.raises(ValueError, match="next upcoming"):
        evaluate_entry_gates(
            **gate_inputs(
                weekend_close_utc=datetime(2026, 10, 23, 23, 0, tzinfo=UTC)
            )
        )
