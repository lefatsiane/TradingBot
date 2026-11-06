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
  currency exposure, and the six-candle post-stop cooldown. A pure candle
  evaluator now combines these checks with configured gates and strategies.
- Standalone fail-closed entry gates for configured sessions, weekend and
  rollover windows, news-calendar freshness/events, spread, and abnormal
  candle range. Calendar validation, startup coverage alerts, and engine
  fail-closed news integration are implemented; approved broker schedules and
  production runtime wiring remain open.
- A serialized broker gateway with bounded exponential retries for idempotent
  reads/checks only; state-changing order operations are not retried.
- A broker executor that journals orders before sending, resolves uncertain
  sends against broker positions/deals, closes from broker deal data, reconciles
  stored and broker positions, adopts protected positions, and alerts on
  commission drift.
- Engine components for closed-bar entry processing, candle de-duplication,
  startup guards, loss-limit management, Friday flattening, Strategy B's H1
  time stop, heartbeat persistence, and a Windows single-instance lock. These
  paths are simulator-tested but are not yet wired to a continuously running
  command.
- Telegram polling/control primitives, warning/critical alert outbox delivery,
  a stoppable healthcheck pinger, and a SQLite backup utility are implemented
  with fake-session or local-database tests. A simulator-tested crash-loop
  guard helper is also present. These are not wired into a production process.
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
  duplicate-candle fault injection.
- A thread-local SQLite state store with WAL/FULL durability, schema migration,
  baselines, safety flags, candle de-duplication, decision rows, Telegram cursor,
  an alert outbox, versioned trade audit rows, and guarded intent transitions.
- A read-only MT5 adapter for candles, symbol specifications, and ticks, plus a
  history-cache CLI that writes closed H1/H4 candles and schema-versioned Parquet
  manifests. Local caches cover six enabled FX pairs from 2 December 2021 through
  30 October 2026. They do not meet the six-year target: the cache begins after
  an unexplained six-hour EURUSD H1 gap on 2 December 2021, which remains rejected
  rather than silently filled.
- A separate demo-only `MT5Broker` implementing account, terminal, positions,
  history, margin, check/send, stop modification and close operations. Its
  injected fake-API tests pass, including refusal of non-demo accounts; it has
  not been connected to a real demo terminal or used to place orders. The
  tick-derived broker offset still needs the manual cross-check in
  [`../BLOCKED.md`](../BLOCKED.md).
- Strict TOML configuration validation with a canonical settings SHA-256.
- Environment-only runtime-secret loading, startup guards, Git version reporting,
  and rotating UTF-8 file logging helpers.
- A demo-only configuration template with XAUUSD disabled until its minimum size
  passes the risk check.
- A simulated/system clock and broker-offset-aware time conversion helpers,
  including broker-server day/month boundaries.

Still not implemented or enabled: production runtime wiring/loop, the full
replayed-week acceptance test, orchestration of the complete fault-injection
drills, multi-symbol portfolio replay/statistics, Telegram/healthcheck
runtime delivery, and demo smoke/failure drills. The engine components are
callable and simulator-tested, but `bot.main` does not connect `MT5Broker`
to them; real-demo verification remains blocked, and the backtest is
single-symbol, provisional, and not a complete parity verdict. The
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

The project includes [config/news.csv](config/news.csv) and a validator at
[tools/validate_news.py](tools/validate_news.py). It checks sorting, gaps, and
minimum future coverage. The engine validates the calendar at startup, blocks
entries when it is stale or invalid, alerts once on stale data, and warns below
10 days of coverage. The events in the CSV still require owner verification
against a real economic calendar; they are not certified by these tests.

Operational procedures are tracked in [docs/runbook.md](docs/runbook.md) and
[docs/drills.md](docs/drills.md); both explicitly remain pending runtime and
demo acceptance.

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

Create the ignored local settings file only if it does not already exist:

```powershell
if (-not (Test-Path settings.toml)) { Copy-Item settings.example.toml settings.toml }
```

The versions recorded for this environment are Python 3.14.7 and MetaTrader5
5.0.6231. Runtime and development package versions are pinned in the requirement
files and `pyproject.toml`.

Copy `settings.example.toml` to `settings.toml`, then set the expected demo login
locally before any future terminal integration. The real settings file is
git-ignored. Do not add passwords, Telegram tokens, or healthcheck URLs to the
settings file or source control; read those secrets from environment variables.

## Validate configuration

```powershell
python -m bot.main --settings settings.toml validate
```

This validates the TOML and enabled-symbol schedules. The checked-in template
has empty `hours_utc` entries, so validation reports those missing schedules.
The broker-derived suggestions must be reviewed on the owner's demo terminal
before applying them. This command never connects to MT5 or places orders.

Retrieve demo-terminal symbol/session data without changing settings:

```powershell
python tools/validate_symbols.py --settings settings.toml
```

Only after reviewing passing suggestions, `--write` updates `hours_utc` lines:

```powershell
python tools/validate_symbols.py --settings settings.toml --write
```

The tool has not been run against the owner terminal. Do not write schedules
based on simulated or guessed market hours.

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
