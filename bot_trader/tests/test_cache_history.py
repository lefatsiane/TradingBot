from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from bot.app.config import load_settings
from bot.app.data import read_history_cache
from bot.app.history_gaps import KnownHistoryGap
from bot.core.types import Candle, SymbolSpec
from tools import cache_history


def test_report_gaps_downloads_and_never_writes_cache(monkeypatch, tmp_path, capsys) -> None:
    settings, version = load_settings(Path(__file__).parents[1] / "settings.toml")
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(
        cache_history,
        "load_settings",
        lambda _path: (settings, version),
    )

    class FakeBroker:
        shutdown_called = False

        def __init__(self, **_kwargs) -> None:
            pass

        def connect(self) -> None:
            pass

        def symbol_info(self, symbol: str) -> SymbolSpec:
            return SymbolSpec(
                symbol, "EUR", "USD", 0.00001, 0.0001, 10, 0.01, 0.01, 100,
                contract_size=100_000,
            )

        def candles(self, _symbol: str, _timeframe: str) -> tuple[Candle, ...]:
            return tuple(
                Candle(datetime(2021, 12, 2, hour, tzinfo=UTC), 1.1, 1.101, 1.099, 1.1)
                for hour in (7, 14, 15)
            )

        def shutdown(self) -> None:
            self.shutdown_called = True

    broker = FakeBroker()
    monkeypatch.setattr(cache_history, "MT5ReadOnlyBroker", lambda **_kwargs: broker)
    result = cache_history.main(
        [
            "--symbols",
            "EURUSD",
            "--timeframes",
            "H1",
            "--start-utc",
            "2021-12-02T00:00:00+00:00",
            "--end-utc",
            "2021-12-02T16:00:00+00:00",
            "--cache-dir",
            str(cache_dir),
            "--report-gaps",
        ]
    )

    assert result == 1
    output = capsys.readouterr().out
    assert "UNEXPECTED GAP EURUSD H1" in output
    assert "2021-12-02T08:00:00+00:00 to 2021-12-02T14:00:00+00:00" in output
    assert "6 missing candle(s)" in output
    assert "Thursday" in output
    assert "no cache files were written" in output
    assert not cache_dir.exists()
    assert broker.shutdown_called


def test_cache_accepts_only_approved_short_midweek_gap(monkeypatch, tmp_path) -> None:
    settings, version = load_settings(Path(__file__).parents[1] / "settings.toml")
    cache_dir = tmp_path / "cache"
    approved = KnownHistoryGap(
        "EURUSD",
        "H1",
        datetime(2026, 6, 30, 9, tzinfo=UTC),
        datetime(2026, 6, 30, 11, tzinfo=UTC),
        2,
        "broker outage",
        "owner",
        "2026-11-11",
    )
    monkeypatch.setattr(cache_history, "load_settings", lambda _path: (settings, version))
    monkeypatch.setattr(cache_history, "load_known_gaps", lambda _path: (approved,))

    class FakeBroker:
        def __init__(self, **_kwargs) -> None:
            pass

        def connect(self) -> None:
            pass

        def symbol_info(self, symbol: str) -> SymbolSpec:
            return SymbolSpec(
                symbol, "EUR", "USD", 0.00001, 0.0001, 10, 0.01, 0.01, 100,
                contract_size=100_000,
            )

        def candles(self, _symbol: str, _timeframe: str) -> tuple[Candle, ...]:
            return tuple(
                Candle(datetime(2026, 6, 30, hour, tzinfo=UTC), 1.1, 1.101, 1.099, 1.1)
                for hour in (8, 11)
            )

        def shutdown(self) -> None:
            pass

    monkeypatch.setattr(cache_history, "MT5ReadOnlyBroker", FakeBroker)
    result = cache_history.main(
        [
            "--symbols", "EURUSD",
            "--timeframes", "H1",
            "--start-utc", "2026-06-30T00:00:00+00:00",
            "--end-utc", "2026-06-30T12:00:00+00:00",
            "--cache-dir", str(cache_dir),
        ]
    )

    assert result == 0
    _, manifest = read_history_cache(
        cache_dir,
        "EURUSD",
        "H1",
        owner_approved_gaps=(approved.as_dict(),),
    )
    assert len(manifest.accepted_gaps) == 1
    assert manifest.accepted_gaps[0]["approved_by"] == "owner"
    assert manifest.design_years == 6
    assert 0 < manifest.years_covered < 0.001
