from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bot.app.news import NewsEvent, is_news_blocked, validate_news_calendar


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
