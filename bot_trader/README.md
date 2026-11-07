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
  fail-closed news integration are implemented; approved broker schedules
  remain an owner prerequisite.
- A serialized broker gateway with bounded exponential retries for idempotent
  reads/checks only; state-changing order operations are not retried.
- A broker executor that journals orders before sending, resolves uncertain
  sends against broker positions/deals, closes from broker deal data, reconciles
  stored and broker positions, adopts protected positions, and alerts on
  commission drift.
- Engine components for closed-bar entry processing, candle de-duplication,
  startup guards, loss-limit management, Friday flattening, Strategy B's H1
  time stop, heartbeat persistence, and a Windows single-instance lock. These
  paths are simulator-tested and wired to `bot.main run`.
- Telegram polling/control primitives, warning/critical alert outbox delivery,
  a stoppable healthcheck pinger, and a SQLite backup utility are implemented
  with fake-session or local-database tests. A simulator-tested crash-loop
  guard and safe runtime shutdown are also implemented. Runtime delivery and
  restart recovery still require owner demo acceptance.
- A deterministic, single-symbol candle replay with next-bar fills, spread,
  commission, slippage, conservative stop-first intrabar fills, and Strategy B's
  candle-count time stop. The original Strategy A-only backtest remains available
  for its fixed-fixture parity comparison.
- An engine-backed multi-symbol replay runner that feeds cached H1/H4 candles
  through `Engine`, `Executor`, and `SimBroker`, with session/rollover/news gates,
  daily/monthly limits, stop-first fills, and auditable CSV/JSON output. It
  excludes the latest 12 months unless `--final` is supplied. Its fixed Strategy A
  fixture matches the legacy runner's entry side, time, and price at 100%;
  reviewed UTC schedules and complete caches are still required for a useful
  six-pair replay.
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

Still pending: a full replayed-week acceptance, portfolio statistics, automated
failure-drill orchestration, real-demo smoke/restart/control verification, and
Task Scheduler/laptop verification. The `run` command connects only to a
configured demo account and refuses invalid or missing enabled-symbol schedules;
it has not been run against a real terminal and is not approved for unattended
operation. The separate `validate` command never connects to MT5 or places
orders. This project must not be used for live trading.

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
[docs/drills.md](docs/drills.md); manual runtime/demo acceptance remains pending.

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

Copy `settings.example.toml` to `settings.toml`. Keep its `expected_login` as
the placeholder value; provide the demo login and password through
`MT5_LOGIN` and `MT5_PASSWORD` environment variables only. Do not store an
account login, password, Telegram token, or healthcheck URL in settings or
source control. Keep the local settings file git-ignored.

## Validate configuration

```powershell
python -m bot.main --settings settings.toml validate
```

This validates the TOML and enabled-symbol schedules. The checked-in template
has empty `hours_utc` entries, so validation reports those missing schedules.
The broker-derived suggestions must be reviewed on the owner's demo terminal
before applying them. This command never connects to MT5 or places orders.

## Run the demo runtime

The runtime command is implemented but has not been verified against the
owner's demo terminal. Do not start it until the broker-derived schedules,
news calendar, demo credentials, and required owner checks are reviewed. It
refuses missing/invalid enabled-symbol schedules, validates demo account mode,
and uses a single-instance lock:

```powershell
python -m bot.main --settings settings.toml run
```

Runtime credentials are environment-only. Shutdown via Ctrl+C or the supported
service stop path leaves broker positions open with their protective stops.
Read [docs/runbook.md](docs/runbook.md) before considering Task Scheduler; its
installer and Windows settings have not been tested on the owner's laptop.

Retrieve demo-terminal symbol/session data without changing settings:

```powershell
$env:MT5_LOGIN = "<demo login>"
$env:MT5_PASSWORD = "<demo password>"
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
multi-symbol trades, decisions, equity curve, and summary:

```powershell
python tools/replay.py --settings settings.toml --strategy both --no-news
```

Replay requires reviewed `hours_utc` schedules and validated caches for every
enabled symbol/timeframe. The checked-in template intentionally has empty
schedules, so the owner must review/apply broker-derived hours before running it.
News gating stays enabled by default; `--no-news` is explicit and recorded in the
report. By default, the most recent 12 months are excluded. Use `--final` only
for the final evaluation run; it creates `data/reports/final-holdout.lock` with
the code version and prevents a second final run. The replay uses only
`SimBroker`; it never connects to MT5 or places orders, and the saved negative
checkpoint result is not a strategy approval.

Analyze a replay report and write its drawdown, 10,000-resample calendar-week
confidence interval, and scorecard:

```powershell
python tools/stats.py data/reports/<run-directory>
```

For doubled spread/slippage stress, rerun the replay with the same settings,
caches, strategy, and news/final flags, then compare the two report directories:

```powershell
python tools/replay.py --settings settings.toml --strategy both --no-news --cost-multiplier 2
python tools/stats.py data/reports/<baseline-run> --cost-stress-report data/reports/<stress-run>
```

The scorecard computes cost drift only when the baseline net profit is positive;
otherwise it remains `INCONCLUSIVE` rather than assigning a misleading
percentage.

The scorecard leaves missing cost-stress, parity, and restart evidence
`INCONCLUSIVE`. Run parameter sensitivity on both strategies with:

```powershell
python tools/replay.py --settings settings.toml --strategy both --no-news --sensitivity
```

This writes one replay report per valid +/-20 percent parameter variant, a
`sensitivity-*.csv` index, and records completed variants in
`data/reports/variants.json`. Invalid parameter combinations are listed rather
than silently skipped. The engine's 1,000-candle input cap still applies.
Random-entry controls remain unimplemented. Replay `portfolio_breakdown.csv` reports strategy,
symbol/timeframe, and overall combined totals.

Record a unique strategy variant before running it so the attempt count stays
auditable:

```powershell
python tools/stats.py --record-variant "strategy-a-ema-fast-24" --variant-description "Strategy A ema_fast=24"
```

The ledger is `data/reports/variants.json`; reusing a variant identifier is
rejected.

## Run tests

```powershell
python -m pytest
```
