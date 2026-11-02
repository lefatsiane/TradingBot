from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Literal

import pandas as pd

from bot.adapters.broker import Broker, Tick
from bot.core.types import Candle, SymbolSpec

Timeframe = Literal["H1", "H4"]
_INTERVALS = {"H1": timedelta(hours=1), "H4": timedelta(hours=4)}
_WEEKEND_GAP_MAX = timedelta(hours=108)
_MAX_HISTORY_END_AGE = timedelta(days=5)


class DataQualityError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class HistoryManifest:
    symbol: str
    timeframe: Timeframe
    start_utc: datetime
    end_utc: datetime
    bar_count: int
    downloaded_at_utc: datetime
    symbol_spec: SymbolSpec | None = None
    gap_count: int = 0


def fetch_history(
    broker: Broker,
    symbol: str,
    timeframe: Timeframe,
    *,
    now_utc: datetime,
    max_tick_age: timedelta = timedelta(minutes=10),
) -> tuple[Candle, ...]:
    now = _require_utc(now_utc, "now_utc")
    if max_tick_age <= timedelta(0):
        raise ValueError("max_tick_age must be positive")
    tick = broker.tick(symbol)
    _validate_tick(tick, now, max_tick_age)
    candles = broker.candles(symbol, timeframe)
    validate_candles(candles, timeframe)
    if candles[-1].time > now:
        raise DataQualityError("History contains a candle newer than now_utc")
    return candles


def last_closed_candles(
    candles: tuple[Candle, ...],
    timeframe: Timeframe,
    *,
    now_utc: datetime,
) -> tuple[Candle, ...]:
    """Return bars whose full timeframe interval has elapsed at now_utc."""
    now = _require_utc(now_utc, "now_utc")
    validate_candles(candles, timeframe)
    interval = _INTERVALS[timeframe]
    closed = tuple(candle for candle in candles if candle.time + interval <= now)
    if not closed:
        raise DataQualityError("History contains no fully closed candles")
    validate_candles(closed, timeframe)
    return closed


def validate_history_end(
    candles: tuple[Candle, ...],
    timeframe: Timeframe,
    *,
    end_utc: datetime,
    max_age: timedelta = _MAX_HISTORY_END_AGE,
) -> None:
    end = _require_utc(end_utc, "end_utc")
    if max_age <= timedelta(0):
        raise ValueError("max_age must be positive")
    validate_candles(candles, timeframe)
    latest_close = candles[-1].time + _INTERVALS[timeframe]
    age = end - latest_close
    if age < timedelta(0):
        raise DataQualityError("Latest history candle closes after end_utc")
    if age > max_age:
        raise DataQualityError(
            f"History ends {age} before end_utc; maximum allowed is {max_age}"
        )


def validate_candles(candles: tuple[Candle, ...], timeframe: Timeframe) -> None:
    if timeframe not in _INTERVALS:
        raise ValueError(f"Unsupported history timeframe: {timeframe}")
    if not candles:
        raise DataQualityError("History contains no candles")
    interval = _INTERVALS[timeframe]
    for previous, current in pairwise(candles):
        difference = current.time - previous.time
        if difference == interval:
            continue
        if difference > interval and _is_expected_market_closure(
            previous,
            current,
            difference,
            timeframe,
        ):
            continue
        if difference <= timedelta(0):
            raise DataQualityError("Candles must be strictly increasing and unique")
        raise DataQualityError(
            f"Unexpected {timeframe} candle gap from {previous.time.isoformat()} "
            f"to {current.time.isoformat()}"
        )


def _is_expected_market_closure(
    previous: Candle,
    current: Candle,
    difference: timedelta,
    timeframe: Timeframe,
) -> bool:
    if (
        timeframe == "H1"
        and difference == timedelta(hours=2)
        and previous.time.date() == current.time.date()
        and previous.time.weekday() < 5
    ):
        return True
    if (
        difference <= _WEEKEND_GAP_MAX
        and previous.time.weekday() in (3, 4)
        and current.time.weekday() in (6, 0, 1)
    ):
        return True
    crosses_year_end_closure = (
        previous.time.month == 12
        and previous.time.day in (24, 25, 26, 31)
        and current.time.month in (12, 1)
        and current.time.day in (1, 2, 25, 26)
    )
    if crosses_year_end_closure and difference <= timedelta(hours=48):
        return True
    return (
        difference <= timedelta(hours=3)
        and previous.time.hour >= 21
        and current.time.hour <= 2
    )


def write_history_cache(
    cache_dir: str | Path,
    symbol: str,
    timeframe: Timeframe,
    candles: tuple[Candle, ...],
    *,
    downloaded_at_utc: datetime,
    symbol_spec: SymbolSpec | None = None,
) -> HistoryManifest:
    validate_candles(candles, timeframe)
    downloaded = _require_utc(downloaded_at_utc, "downloaded_at_utc")
    directory = Path(cache_dir)
    directory.mkdir(parents=True, exist_ok=True)
    data_path, manifest_path = _cache_paths(directory, symbol, timeframe)
    manifest = HistoryManifest(
        symbol,
        timeframe,
        candles[0].time,
        candles[-1].time,
        len(candles),
        downloaded,
        symbol_spec,
        _count_gap_intervals(candles, timeframe),
    )
    frame = pd.DataFrame(
        {
            "time": [candle.time for candle in candles],
            "open": [candle.open for candle in candles],
            "high": [candle.high for candle in candles],
            "low": [candle.low for candle in candles],
            "close": [candle.close for candle in candles],
            "spread": [candle.spread for candle in candles],
            "tick_volume": [candle.tick_volume for candle in candles],
        }
    )
    temp_data = data_path.with_name(f"{data_path.name}.tmp")
    temp_manifest = manifest_path.with_name(f"{manifest_path.name}.tmp")
    try:
        frame.to_parquet(temp_data, index=False, engine="pyarrow")
        temp_manifest.write_text(
            json.dumps(_manifest_dict(manifest), indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temp_data, data_path)
        os.replace(temp_manifest, manifest_path)
    finally:
        temp_data.unlink(missing_ok=True)
        temp_manifest.unlink(missing_ok=True)
    return manifest


def read_history_cache(
    cache_dir: str | Path, symbol: str, timeframe: Timeframe
) -> tuple[tuple[Candle, ...], HistoryManifest]:
    data_path, manifest_path = _cache_paths(Path(cache_dir), symbol, timeframe)
    try:
        raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        frame = pd.read_parquet(data_path, engine="pyarrow")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise DataQualityError(f"Could not read history cache for {symbol} {timeframe}") from error
    required_columns = {
        "time",
        "open",
        "high",
        "low",
        "close",
        "spread",
        "tick_volume",
    }
    if set(frame.columns) != required_columns:
        raise DataQualityError("History Parquet columns do not match the cache schema")
    candles = tuple(
        Candle(
            time=_to_utc(row.time),
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            spread=float(row.spread),
            tick_volume=int(row.tick_volume),
        )
        for row in frame.itertuples(index=False)
    )
    validate_candles(candles, timeframe)
    manifest = _manifest_from_dict(raw_manifest)
    if (
        manifest.symbol != symbol
        or manifest.timeframe != timeframe
        or manifest.start_utc != candles[0].time
        or manifest.end_utc != candles[-1].time
        or manifest.bar_count != len(candles)
        or manifest.gap_count != _count_gap_intervals(candles, timeframe)
    ):
        raise DataQualityError("History manifest does not match the cached candle data")
    if manifest.symbol_spec is not None and manifest.symbol_spec.name != symbol:
        raise DataQualityError("History manifest symbol specification does not match symbol")
    return candles, manifest


def _validate_tick(
    tick: Tick | None, now: datetime, max_tick_age: timedelta
) -> None:
    if tick is None:
        raise DataQualityError("Broker returned no current tick")
    tick_time = _require_utc(tick.time_utc, "tick.time_utc")
    if tick.bid <= 0 or tick.ask < tick.bid:
        raise DataQualityError("Broker tick has invalid bid/ask prices")
    age = now - tick_time
    if age < timedelta(0) or age > max_tick_age:
        raise DataQualityError("Broker tick is stale or newer than now_utc")


def _require_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be in UTC")
    normalized = value.astimezone(UTC)
    if normalized != value:
        raise ValueError(f"{name} must be in UTC")
    return normalized


def _to_utc(value: object) -> datetime:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        raise DataQualityError("Cached candle timestamps must be timezone-aware")
    return timestamp.to_pydatetime().astimezone(UTC)


def _cache_paths(
    directory: Path, symbol: str, timeframe: Timeframe
) -> tuple[Path, Path]:
    if not symbol.isalnum():
        raise ValueError("Symbol must contain only letters and digits")
    if timeframe not in _INTERVALS:
        raise ValueError(f"Unsupported history timeframe: {timeframe}")
    stem = f"{symbol}_{timeframe}"
    return directory / f"{stem}.parquet", directory / f"{stem}.manifest.json"


def _manifest_dict(manifest: HistoryManifest) -> dict[str, object]:
    return {
        "schema_version": 3,
        "symbol": manifest.symbol,
        "timeframe": manifest.timeframe,
        "start_utc": manifest.start_utc.isoformat(),
        "end_utc": manifest.end_utc.isoformat(),
        "bar_count": manifest.bar_count,
        "downloaded_at_utc": manifest.downloaded_at_utc.isoformat(),
        "gap_count": manifest.gap_count,
        "symbol_spec": (
            {
                "name": manifest.symbol_spec.name,
                "base_currency": manifest.symbol_spec.base_currency,
                "quote_currency": manifest.symbol_spec.quote_currency,
                "point": manifest.symbol_spec.point,
                "tick_size": manifest.symbol_spec.tick_size,
                "tick_value_loss": manifest.symbol_spec.tick_value_loss,
                "volume_min": manifest.symbol_spec.volume_min,
                "volume_step": manifest.symbol_spec.volume_step,
                "volume_max": manifest.symbol_spec.volume_max,
                "digits": manifest.symbol_spec.digits,
                "contract_size": manifest.symbol_spec.contract_size,
            }
            if manifest.symbol_spec is not None
            else None
        ),
    }


def _manifest_from_dict(value: object) -> HistoryManifest:
    if not isinstance(value, dict) or value.get("schema_version") != 3:
        raise DataQualityError("History manifest has an unsupported schema")
    try:
        symbol = value["symbol"]
        timeframe = value["timeframe"]
        if not isinstance(symbol, str) or timeframe not in _INTERVALS:
            raise TypeError("Invalid manifest identity")
        bar_count = value["bar_count"]
        if not isinstance(bar_count, int) or bar_count < 1:
            raise TypeError("Invalid manifest bar count")
        gap_count = value["gap_count"]
        if not isinstance(gap_count, int) or gap_count < 0:
            raise TypeError("Invalid manifest gap count")
        spec_data = value.get("symbol_spec")
        if spec_data is None:
            symbol_spec = None
        elif isinstance(spec_data, dict):
            symbol_spec = SymbolSpec(**spec_data)
        else:
            raise TypeError("Invalid symbol specification")
        return HistoryManifest(
            symbol=symbol,
            timeframe=timeframe,
            start_utc=_require_utc(datetime.fromisoformat(value["start_utc"]), "start_utc"),
            end_utc=_require_utc(datetime.fromisoformat(value["end_utc"]), "end_utc"),
            bar_count=bar_count,
            downloaded_at_utc=_require_utc(
                datetime.fromisoformat(value["downloaded_at_utc"]),
                "downloaded_at_utc",
            ),
            symbol_spec=symbol_spec,
            gap_count=gap_count,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise DataQualityError("History manifest is invalid") from error


def _count_gap_intervals(candles: tuple[Candle, ...], timeframe: Timeframe) -> int:
    interval = _INTERVALS[timeframe]
    return sum(current.time - previous.time > interval for previous, current in pairwise(candles))
