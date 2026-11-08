from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bot.app.news import load_news_calendar
from tools import news_import

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
INPUT_HEADER = "date,time,currency,impact,title\n"


def input_file(tmp_path: Path, rows: list[str]) -> Path:
    path = tmp_path / "input.csv"
    path.write_text(INPUT_HEADER + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def test_timezone_conversion_utc_and_johannesburg(tmp_path: Path) -> None:
    path = input_file(
        tmp_path,
        [
            "2026-11-03,09:15,USD,high,UTC event",
            "2026-11-03,09:15,EUR,medium,Johannesburg event",
        ],
    )

    utc_events = news_import.check_input(path, now_utc=NOW).events
    johannesburg_events = news_import.check_input(
        path,
        input_timezone="Africa/Johannesburg",
        now_utc=NOW,
    ).events

    utc_event = next(event for event in utc_events if event.currency == "USD")
    johannesburg_event = next(event for event in johannesburg_events if event.currency == "EUR")
    assert utc_event.time_utc.isoformat() == "2026-11-03T09:15:00+00:00"
    assert johannesburg_event.time_utc.isoformat() == "2026-11-03T07:15:00+00:00"


@pytest.mark.parametrize(
    ("local_date", "local_time", "timezone", "expected"),
    [
        ("2026-11-03", "01:30", "Africa/Johannesburg", "2026-11-02T23:30:00+00:00"),
        ("2026-11-03", "02:30", "Africa/Johannesburg", "2026-11-03T00:30:00+00:00"),
    ],
)
def test_conversion_handles_utc_date_boundary(
    tmp_path: Path,
    local_date: str,
    local_time: str,
    timezone: str,
    expected: str,
) -> None:
    path = input_file(tmp_path, [f"{local_date},{local_time},USD,high,Boundary event"])
    result = news_import.check_input(
        path,
        input_timezone=timezone,
        now_utc=NOW,
    )
    assert result.events[0].time_utc.isoformat() == expected


@pytest.mark.parametrize(
    ("local_date", "local_time", "expected_utc"),
    [
        ("2026-11-08", "00:30", "2026-11-07T22:30:00+00:00"),
        ("2026-11-09", "00:30", "2026-11-08T22:30:00+00:00"),
    ],
)
def test_weekends_are_rejected_after_timezone_conversion(
    tmp_path: Path,
    local_date: str,
    local_time: str,
    expected_utc: str,
) -> None:
    path = input_file(tmp_path, [f"{local_date},{local_time},USD,high,Weekend event"])
    with pytest.raises(news_import.InputValidationError, match="Saturday or Sunday in UTC"):
        news_import.check_input(path, input_timezone="Africa/Johannesburg", now_utc=NOW)
    converted = news_import._local_to_utc(
        datetime.strptime(f"{local_date} {local_time}", "%Y-%m-%d %H:%M"),
        news_import.ZoneInfo("Africa/Johannesburg"),
        row_number=2,
    )
    assert converted.isoformat() == expected_utc


def test_check_rejects_all_invalid_rows_with_row_numbers(tmp_path: Path) -> None:
    path = input_file(
        tmp_path,
        [
            ",09:00,USD,high,Blank date",
            "2026-11-03,All Day,USD,high,All day event",
            "2026-11-03,09:00,CAD,high,Unsupported currency",
            "2026-11-03,09:00,USD,urgent,Unsupported impact",
            "2026-11-03,09:00,USD,high,",
            "2026-11-03,09:00,USD,high,Duplicate",
            "2026-11-03,09:00,USD,high,Duplicate",
            "2026-11-01,09:00,USD,high,Past event",
            "2026-02-30,09:00,USD,high,Invalid date",
            "2026-11-03,25:00,USD,high,Invalid time",
        ],
    )
    with pytest.raises(news_import.InputValidationError) as captured:
        news_import.check_input(path, now_utc=NOW)

    messages = "\n".join(captured.value.problems)
    for expected in (
        "row 2:",
        "row 3:",
        "leave this row out",
        "currency must be one of",
        "impact must be high, medium, or low",
        "title is blank",
        "duplicate event",
        "more than 1 day in the past",
        "date must use YYYY-MM-DD",
        "time must use HH:MM",
    ):
        assert expected in messages


@pytest.mark.parametrize("blank_column", range(5))
def test_check_rejects_each_blank_field(
    tmp_path: Path,
    blank_column: int,
) -> None:
    values = ["2026-11-03", "09:00", "USD", "high", "Event title"]
    values[blank_column] = ""
    path = input_file(tmp_path, [",".join(values)])

    with pytest.raises(news_import.InputValidationError) as captured:
        news_import.check_input(path, now_utc=NOW)

    assert f"{news_import.INPUT_COLUMNS[blank_column]} is blank" in str(captured.value)


def test_check_rejects_tentative_and_bad_timezone(tmp_path: Path) -> None:
    path = input_file(tmp_path, ["2026-11-03,Tentative,USD,high,Test"])
    with pytest.raises(news_import.InputValidationError, match="leave this row out"):
        news_import.check_input(path, now_utc=NOW)
    with pytest.raises(ValueError, match="Unknown input timezone"):
        news_import.check_input(path, input_timezone="Not/A_Zone", now_utc=NOW)


def test_check_warns_about_events_more_than_21_days_ahead(tmp_path: Path) -> None:
    path = input_file(tmp_path, ["2026-12-01,09:00,USD,high,Far event"])
    result = news_import.check_input(path, now_utc=NOW)
    assert result.warnings == ("row 2: event is more than 21 days ahead",)


def test_merge_keeps_old_events_and_removes_duplicate_keys(tmp_path: Path) -> None:
    candidate = tmp_path / "news.candidate.csv"
    candidate.write_text(
        "time_utc,currency,impact,title\n2026-11-03T09:00:00Z,USD,high,Existing event\n",
        encoding="utf-8",
    )
    new_event = news_import.NewsEvent(
        datetime(2026, 11, 3, 9, tzinfo=UTC),
        "USD",
        "medium",
        "Existing event",
    )
    another_event = news_import.NewsEvent(
        datetime(2026, 11, 4, 9, tzinfo=UTC),
        "EUR",
        "low",
        "Filtered event",
    )

    merged = news_import.merge_events(
        (new_event, another_event),
        candidate_path=candidate,
        impacts=frozenset({"high", "medium"}),
    )

    assert len(merged) == 1
    assert merged[0].impact == "high"
    assert load_news_calendar(candidate) == merged


def test_merge_sorts_new_events_and_output_passes_existing_loader(tmp_path: Path) -> None:
    candidate = tmp_path / "news.candidate.csv"
    old_event = news_import.NewsEvent(datetime(2026, 11, 5, 9, tzinfo=UTC), "GBP", "high", "Old")
    new_event = news_import.NewsEvent(datetime(2026, 11, 4, 9, tzinfo=UTC), "USD", "medium", "New")
    news_import._write_events(candidate, [old_event])

    merged = news_import.merge_events((new_event,), candidate_path=candidate)

    assert [event.title for event in merged] == ["New", "Old"]
    assert load_news_calendar(candidate) == merged


def test_promote_refuses_candidate_that_fails_loader_and_does_not_backup(
    tmp_path: Path,
) -> None:
    approved = tmp_path / "config" / "news.csv"
    candidate = tmp_path / "config" / "news.candidate.csv"
    backup_dir = tmp_path / "backups"
    approved.parent.mkdir()
    approved.write_text("time_utc,currency,impact,title\n", encoding="utf-8")
    candidate.write_text("wrong,header\nbad,row\n", encoding="utf-8")

    with pytest.raises(ValueError, match="missing required columns"):
        news_import.promote_candidate(
            approved_path=approved,
            candidate_path=candidate,
            backup_dir=backup_dir,
            min_days=7,
            now_utc=NOW,
            confirmation="yes",
        )

    assert list(backup_dir.glob("*")) == []
    assert approved.read_text(encoding="utf-8") == "time_utc,currency,impact,title\n"


def test_promote_copies_after_backup_and_validates_calendar(tmp_path: Path) -> None:
    approved = tmp_path / "config" / "news.csv"
    candidate = tmp_path / "config" / "news.candidate.csv"
    backup_dir = tmp_path / "backups"
    approved.parent.mkdir()
    approved.write_text(
        "time_utc,currency,impact,title\n2026-11-03T12:00:00Z,USD,high,Old\n",
        encoding="utf-8",
    )
    calendar_events = [
        news_import.NewsEvent(
            NOW + timedelta(days=1),
            "USD",
            "high",
            "Soon",
        ),
        news_import.NewsEvent(
            NOW + timedelta(days=8),
            "EUR",
            "high",
            "Later",
        ),
    ]
    news_import._write_events(candidate, calendar_events)
    candidate_content = candidate.read_text(encoding="utf-8")

    backup = news_import.promote_candidate(
        approved_path=approved,
        candidate_path=candidate,
        backup_dir=backup_dir,
        min_days=7,
        now_utc=NOW,
        confirmation="yes",
    )

    assert backup.exists()
    assert backup.parent == backup_dir
    assert "Old" in backup.read_text(encoding="utf-8")
    assert approved.read_text(encoding="utf-8") == candidate_content
    assert len(load_news_calendar(approved)) == 2


def test_coverage_command_exit_code_and_report(tmp_path: Path, monkeypatch, capsys) -> None:
    approved = tmp_path / "news.csv"
    events = [
        news_import.NewsEvent(NOW + timedelta(days=1), "USD", "high", "Soon"),
        news_import.NewsEvent(NOW + timedelta(days=8), "EUR", "medium", "Later"),
    ]
    news_import._write_events(approved, events)
    monkeypatch.setattr(news_import, "APPROVED_CALENDAR", approved)
    monkeypatch.setattr(news_import, "_load_min_days_covered", lambda: 7)
    monkeypatch.setattr(news_import, "datetime", FrozenDateTime)

    assert news_import.main(["coverage"]) == 0
    output = capsys.readouterr().out
    assert "Coverage ahead: 8.0 days" in output
    assert "Meets min_days_covered (7): yes" in output

    news_import._write_events(
        approved,
        [news_import.NewsEvent(NOW + timedelta(days=2), "USD", "high", "Short")],
    )
    assert news_import.main(["coverage"]) == 1


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return NOW.replace(tzinfo=None)
        return NOW.astimezone(tz)


def test_template_is_explicitly_example_only(tmp_path: Path) -> None:
    template = tmp_path / "news_input.template.csv"
    news_import.write_template(template)
    with template.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert list(rows[0]) == list(news_import.INPUT_COLUMNS)
    assert len(rows) == 2
    assert all("EXAMPLE ONLY" in " ".join(row.values()) for row in rows)
