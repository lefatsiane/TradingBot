from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bot.app.news import (
    NewsEvent,
    calendar_has_minimum_coverage,
    is_news_blocked,
    validate_news_calendar,
)


def test_news_calendar_is_sorted_and_has_enough_future_coverage(tmp_path) -> None:
    calendar_path = tmp_path / "news.csv"
    base = datetime.now(UTC).replace(microsecond=0) + timedelta(days=1)
    rows = [
        "time_utc,currency,impact,title",
        f"{(base + timedelta(days=0)).isoformat().replace('+00:00', 'Z')},USD,high,FOMC",
        f"{(base + timedelta(days=2)).isoformat().replace('+00:00', 'Z')},EUR,high,ECB",
        f"{(base + timedelta(days=9)).isoformat().replace('+00:00', 'Z')},GBP,high,BOE",
    ]
    calendar_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    events = validate_news_calendar(calendar_path, min_days_covered=7)
    assert [event.currency for event in events] == ["USD", "EUR", "GBP"]


def test_calendar_rejects_unsorted_source_rows(tmp_path) -> None:
    now = datetime(2026, 11, 1, tzinfo=UTC)
    calendar_path = tmp_path / "unsorted-news.csv"
    calendar_path.write_text(
        "time_utc,currency,impact,title\n"
        f"{(now + timedelta(days=8)).isoformat()},USD,high,Fake later\n"
        f"{(now + timedelta(days=1)).isoformat()},EUR,high,Fake earlier\n",
        encoding="utf-8",
    )

    try:
        validate_news_calendar(calendar_path, now_utc=now)
    except ValueError as error:
        assert "not sorted" in str(error)
        return
    raise AssertionError("Calendar source rows out of order should fail validation")


def test_calendar_rejects_short_coverage(tmp_path) -> None:
    calendar_path = tmp_path / "news.csv"
    base = datetime.now(UTC).replace(microsecond=0) + timedelta(days=1)
    rows = [
        "time_utc,currency,impact,title",
        f"{(base + timedelta(days=1)).isoformat().replace('+00:00', 'Z')},USD,high,Short term",
        f"{(base + timedelta(days=2)).isoformat().replace('+00:00', 'Z')},EUR,high,Short term 2",
    ]
    calendar_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    try:
        validate_news_calendar(calendar_path, min_days_covered=7)
    except ValueError:
        return
    raise AssertionError("Calendar with less than 7 days coverage should fail")


def test_news_blocking_uses_both_currencies_on_a_pair() -> None:
    now = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    event = NewsEvent(now + timedelta(minutes=15), "USD", "high", "CPI")
    assert is_news_blocked("EURUSD", now, [event], before_min=30, after_min=30) is True
    assert is_news_blocked("GBPUSD", now, [event], before_min=30, after_min=30) is True
    assert is_news_blocked("AUDUSD", now, [event], before_min=30, after_min=30) is True
    assert is_news_blocked("USDJPY", now + timedelta(hours=3), [event], before_min=30, after_min=30) is False

    base_currency_event = NewsEvent(now, "EUR", "high", "ECB")
    assert is_news_blocked("EURUSD", now, [base_currency_event]) is True
    assert is_news_blocked("GBPUSD", now, [base_currency_event]) is False


def test_news_block_window_includes_both_30_minute_edges() -> None:
    event_time = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    event = NewsEvent(event_time, "EUR", "high", "fake test event")

    assert is_news_blocked(
        "EURUSD", event_time - timedelta(minutes=30), [event]
    )
    assert is_news_blocked(
        "EURUSD", event_time + timedelta(minutes=30), [event]
    )
    assert not is_news_blocked(
        "EURUSD", event_time - timedelta(minutes=30, seconds=1), [event]
    )
    assert not is_news_blocked(
        "EURUSD", event_time + timedelta(minutes=30, seconds=1), [event]
    )


def test_calendar_coverage_is_false_for_missing_or_stale_files(tmp_path) -> None:
    now = datetime(2026, 11, 1, tzinfo=UTC)
    missing = tmp_path / "missing.csv"
    assert not calendar_has_minimum_coverage(missing, now_utc=now)

    stale = tmp_path / "stale.csv"
    stale.write_text(
        "time_utc,currency,impact,title\n"
        f"{(now + timedelta(days=1)).isoformat()},USD,high,Fake event\n",
        encoding="utf-8",
    )
    assert not calendar_has_minimum_coverage(stale, now_utc=now)


def test_calendar_validation_accepts_injected_utc_time(tmp_path) -> None:
    now = datetime(2026, 11, 1, tzinfo=UTC)
    path = tmp_path / "future.csv"
    path.write_text(
        "time_utc,currency,impact,title\n"
        f"{(now + timedelta(days=1)).isoformat()},USD,high,Fake one\n"
        f"{(now + timedelta(days=7)).isoformat()},EUR,high,Fake two\n"
        f"{(now + timedelta(days=9)).isoformat()},GBP,high,Fake three\n",
        encoding="utf-8",
    )

    events = validate_news_calendar(path, now_utc=now)

    assert len(events) == 3
