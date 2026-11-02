# Bot Trader readme

This is the separate Stage 4 implementation workspace. The existing connection-test
scripts and their CSV output remain unchanged.

For an architecture/file map and an independent-review checklist, see
[BOT_BLUEPRINT.md](BOT_BLUEPRINT.md).
The authoritative requirements, design decisions, phase gates, change log and
traceability record are in the [Trading Bot Full SDLC Record](../Trading%20Bot%20Full%20SDLC%20Record.docx).

## Current checkpoint

Implemented so far:

- Pure EMA/ATR, Bollinger, RSI, and ADX indicator functions.
- Pure, parameter-driven Strategy A (H1/H4, 600-candle warm-up, 1,000-candle
  working window) and Strategy B (H1 only) proposal logic.
- A shared cost estimator for per-lot spread, commission, slippage and stop risk;
  cost-aware position sizing and independently tested risk checks for daily and
  monthly equity loss, daily headroom, margin use, trade/symbol limits,
  currency exposure, and the six-candle post-stop cooldown. The risk checks are
  not yet wired into an engine or broker-management loop.
- Standalone fail-closed entry gates for configured sessions, weekend and
  rollover windows, news-calendar freshness/events, spread, and abnormal
  candle range. Gate configuration/startup checks and engine integration remain
  open.
- A serialized broker gateway with bounded exponential retries for idempotent
  reads/checks only; state-changing order operations are not retried.
- A deterministic, single-symbol candle replay with next-bar fills, spread,
  commission, slippage, conservative stop-first intrabar fills, and Strategy B's
  candle-count time stop. This remains a provisional first backtest rather than a
  full live-engine parity verification.
- A Strategy A backtest runner that uses closed candles, `SimBroker`, configured
  sizing/cost assumptions, and daily/monthly risk checks; it excludes the latest
  12 months unless `--final` is supplied. The report writer saves trades, the
  equity curve, and metrics as CSV/JSON. This is single-symbol and not live-engine
  parity.
- A simulated broker with bid/ask entry, estimated margin, configurable per-symbol
  commission/slippage assumptions, candle-triggered protective exits with
  stop-first same-candle handling, and deterministic timeout, lost-response,
  crash-after-send, invalid-stop, disconnect, stale-tick, wrong-fill-mode, and
  duplicate-candle fault injection. A settings helper builds the cost map, but
  no trading engine currently instantiates or drives the simulator.
- A thread-local SQLite state store with WAL/FULL durability, schema migration,
  baselines, safety flags, candle de-duplication, decision rows, Telegram cursor,
  an alert outbox, versioned trade audit rows, and guarded intent transitions.
- A read-only MT5 adapter for candles, symbol specifications, and ticks, plus a
  history-cache CLI that writes closed H1/H4 candles and schema-versioned Parquet
  manifests. Local caches cover six enabled FX pairs from 2 December 2021 through
  30 October 2026. They do not meet the six-year target: the cache begins after
  an unexplained six-hour EURUSD H1 gap on 2 December 2021, which remains rejected
  rather than silently filled.
- Strict TOML configuration validation with a canonical settings SHA-256.
- Environment-only runtime-secret loading, startup guards, Git version reporting,
  and rotating UTF-8 file logging helpers.
- A demo-only configuration template with XAUUSD disabled until its minimum size
  passes the risk check.
- A simulated/system clock and broker-offset-aware time conversion helpers,
  including broker-server day/month boundaries.

Still not implemented or enabled: full MT5 account/position/order operations,
order execution, the runtime engine, restart reconciliation against broker
positions/deals, orchestration of the full fault-injection drills, multi-symbol
portfolio replay/statistics, integrated news/time/session gates, Telegram
controls, heartbeat alerts, and demo smoke/failure drills. The MT5 adapter is
read-only for the history workflow; the standard MT5 Python API does not provide
the server UTC offset used by this project, so offset lookup requires an explicit
provider/terminal property and has not been verified against the broker. The
backtest is single-symbol, provisional, and not a complete parity verdict. The
`bot.main` validation command does not connect to MT5 or place orders. This
project must not be used for live trading.

Strategy A uses a 600-candle warm-up. The 1 November 2026 H1 EURUSD checkpoint
ran to completion and saved its report: 401 trades, net profit -$226.86, win
rate 33.92%, profit factor 0.935, and maximum drawdown $354.96 (11.55%). It
excluded 6,191 recent bars and ended with no open position. This satisfies the
checkpoint's run-and-save criterion only; the result is negative and does not
meet the design's profitability or drawdown goals. The report is under
`data/reports/EURUSD/H1/20261101T043220Z-c9c51be3/`. Design decisions and gates
remain in the authoritative SDLC record.

## News calendar validation

The project includes a future-facing forex calendar under [config/news.csv](config/news.csv) and a validator at [tools/validate_news.py](tools/validate_news.py). The validator ensures the file is sorted in UTC, has no large gaps, and provides at least 7 days of coverage ahead of the current time before trading can proceed.

```powershell
python tools/validate_news.py --settings settings.toml
```

## Setup

Use the Python 3.14 interpreter used by the existing connection test:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements-dev.txt
```

The versions recorded for this environment are Python 3.14.7 and MetaTrader5
5.0.6231. Runtime and development package versions are pinned in the requirement
files and `pyproject.toml`.

Set `expected_login` in `settings.toml` to the intended demo login before any future
terminal integration. Do not add passwords, Telegram tokens, or healthcheck URLs to
the settings file or source control.

## Validate configuration

```powershell
python -m bot.main --settings settings.toml validate
```

This only validates the file. There is no terminal connection or order execution in
the current code.

## Cache history and run a provisional replay

With the intended MT5 terminal already available, cache the enabled symbols:

```powershell
python tools/cache_history.py --settings settings.toml --start-utc 2021-12-02T16:00:00+00:00
```

The cache command validates closed bars and fails on unexplained gaps. The start
above avoids the currently unresolved EURUSD H1 gap; it also means this dataset
does not cover six full years. Run a single-symbol Strategy A H1 replay and save
its trades, equity curve, and summary:

```powershell
python tools/replay.py --settings settings.toml --symbol EURUSD --timeframe H1
```

By default, the most recent 12 months are excluded. Use `--final` only for the
final evaluation run. Neither command places live orders, and the saved negative
checkpoint result is not a strategy approval.

## Run tests

```powershell
python -m pytest
```
