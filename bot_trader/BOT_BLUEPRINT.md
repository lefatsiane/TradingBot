# Bot Trader Blueprint

This document maps the current `bot_trader` workspace for an independent code
review. It describes what is present in the source today; it is not a trading
specification or evidence that the bot is safe or profitable.

The authoritative project requirements, design decisions, phase gates, change
log and traceability matrix are in the [Trading Bot Full SDLC Record](../Trading%20Bot%20Full%20SDLC%20Record.docx).

## Scope and status

`bot_trader` is the separate Stage 4 implementation workspace. The parent
`TradingBot` folder also contains two SDLC/task-list documents and the
`connection_test.py`, `connection_test_v2.py`, and `connection_test_results.csv`
artifacts. Those are outside this Python project and are not part of its package
or test suite.

The current executable entry point is `python -m bot.main`. Its only command is
`validate`: it loads and validates the TOML settings, prints a canonical
settings hash, and exits. Separate tools can retrieve read-only MT5 history and
run a saved Strategy A replay. The `Executor` and `Engine` implement order
journaling/reconciliation, closed-candle entry processing, startup guards, and
management operations against the broker protocol. They have simulator tests
but are not wired to a continuously running command. The demo-only `MT5Broker`
has fake-API coverage only; real demo-terminal verification remains blocked.

The project README identifies production runtime wiring, full-week engine
replay, fault-drill orchestration, multi-symbol replay/statistics, Telegram
controls, heartbeat delivery, and demo smoke/failure drills as not implemented
or enabled. The replay is single-symbol and is not a complete
backtest verdict engine. The 1 November 2026 EURUSD H1 run saved its report, but
its net result was -$226.86 over 401 trades (profit factor 0.935; maximum
drawdown 11.55%). It is a run-completeness checkpoint, not evidence of
profitability or readiness. The project must not be used for live trading.

## Workspace map

```text
TradingBot/                                  Parent workspace
|-- Trading Bot Full SDLC Record.docx         Separate project record
|-- Trading Bot Implementation Task List (incomplete).docx
|-- connection_test.py                       Separate connection-test artifact
|-- connection_test_results.csv               Separate test output
|-- connection_test_v2.py                     Separate connection-test artifact
`-- bot_trader/                                This Python project
    |-- .gitignore                              Local environments, DBs, data/logs
    |-- README.md                              Project status, setup, commands
    |-- BOT_BLUEPRINT.md                       This review map
    |-- pyproject.toml                         Package metadata, Python floor, tooling
    |-- requirements.txt                       Runtime dependencies
    |-- requirements-dev.txt                   Development/test dependencies
    |-- settings.toml                          Demo-only configuration template
    |-- bot/
    |   |-- __init__.py
    |   |-- main.py                            CLI; configuration validation only
    |   |-- adapters/
    |   |   |-- __init__.py
    |   |   |-- broker.py                      Broker protocol and DTOs
    |   |   |-- gateway.py                     Serialized broker access and bounded read retries
    |   |   |-- mt5_broker.py                  Read-only history adapter and demo-only broker
    |   |   |-- sim_broker.py                  Deterministic demo broker/fault model
    |   |   `-- store.py                       SQLite schema, trade audit and alert outbox
    |   |-- app/
    |   |   |-- __init__.py
    |   |   |-- config.py                      Strict TOML settings models/loader
    |   |   |-- data.py                        Closed bars, history validation and Parquet cache
    |   |   |-- backtest.py                    Single-symbol Strategy A replay orchestration
    |   |-- engine.py                      Entry evaluation, entry lane, startup and management lanes
    |   |-- executor.py                    Intent-journaled opens/closes and broker reconciliation
    |   |   |-- logging_setup.py               Rotating UTF-8 file logging
    |   |   |-- news.py                        Calendar parsing, validation, block checks
    |   |   |-- reports.py                     CSV/JSON reports and performance metrics
    |   |-- symbol_validation.py          Demo symbol/history/session validation
    |   |   `-- versioning.py                  Git commit and clean-tree reporting
    |   `-- core/
    |       |-- __init__.py
    |       |-- clock.py                         SystemClock and SimClock
    |       |-- costs.py                         Spread, commission, slippage and risk-cost estimates
    |       |-- gates.py                         Session, rollover, weekend, news, spread and volatility gates
    |       |-- indicators.py                  EMA, ATR, Bollinger, RSI, ADX
    |       |-- replay.py                      Single-symbol candle replay
    |       |-- risk.py                        Loss-limit, margin, cooldown and exposure checks
    |       |-- sizing.py                       Cost-aware lot sizing
    |       |-- strategy_a.py                  Trend/pullback proposal logic
    |       |-- strategy_b.py                  H1 mean-reversion proposal logic
    |       |-- timeutil.py                     Broker-server timestamp conversion
    |       `-- types.py                        Shared immutable domain types
    |-- config/
    |   `-- news.csv                            Example UTC event calendar
    |-- tests/
    |   |-- test_clock.py                       System and simulated clock
    |   |-- test_timeutil.py                    Broker offset and boundaries
    |   |-- test_broker.py                      SimBroker behavior/fault cases
    |   |-- test_config.py                      Environment secrets and startup guards
    |   |-- test_core.py                        Config, core logic, risk, time, replay
    |   |-- test_costs.py                       Cost estimation and sizing gates
    |   |-- test_data.py                        History validation and Parquet cache
    |   |-- test_engine.py                     Pure decisions, closed-bar progress, management limits
    |   |-- test_executor.py                   Intent states, fault outcomes, close and reconcile behavior
    |   |-- test_mt5_broker.py                  Read-only adapter mapping and history chunking
    |   |-- test_indicators.py                  EMA and ATR reference values
    |   |-- test_gateway.py                     Serialization and safe retry behavior
    |   |-- test_news.py                        Calendar and event-block behavior
    |   |-- test_replay.py                      Backtest determinism, holdout and report output
    |   |-- test_store.py                       SQLite durability, migrations and outbox
    |   |-- test_strategy_a.py                  Strategy A rule and boundary scenarios
    |   `-- test_strategy_b.py                  Strategy B scenarios
    `-- tools/
        |-- __init__.py
        |-- cache_history.py                    Read-only MT5 H1/H4 history downloader
        |-- replay.py                           Strategy A replay/report CLI
        |-- validate_symbols.py                 Demo symbol/session validation CLI
        `-- validate_news.py                    Standalone calendar validator
```

Generated local test/lint/property-test caches (for example `.pytest_cache`,
`.ruff_cache`, and `.hypothesis`) are intentionally omitted from the source map.
The `.gitignore` also excludes local environments, databases, data, logs, backups,
and `.env` files.

## Dependency and data-flow map

```text
Runnable CLI
  python -m bot.main --settings settings.toml validate
    -> bot.main.main()
    -> bot.app.config.load_settings()
    -> strict Pydantic models + TOML parse + canonical settings SHA-256
    -> print validation result and exit

Standalone news validation
  python tools/validate_news.py --settings settings.toml
    -> load settings
    -> resolve configured calendar relative to settings file
    -> bot.app.news.validate_news_calendar()
    -> print result and exit

Standalone history and backtest tools
  python tools/cache_history.py --settings settings.toml [--start-utc ...]
    -> read-only MT5 adapter -> closed-bar validation -> Parquet + manifest
  python tools/replay.py --settings settings.toml --symbol EURUSD --timeframe H1
    -> cached history -> Strategy A + sizing/risk -> SimBroker -> CSV/JSON report

Core components (callable independently; not wired into the CLI)
  Candle + SymbolSpec -> strategy_a.propose() / strategy_b.propose()
                       -> StrategyResult / Proposal
  Proposal + SymbolSpec + equity/cost assumptions -> sizing.size_trade()
  Open/pending risk + symbol specs + P&L/equity -> risk.evaluate_entry()
  Equity baselines + current equity -> risk.evaluate_loss_limits()
  Candles + SymbolSpec + strategy callback + ReplaySettings
                       -> replay.replay_symbol() -> ReplayResult
  Market data + configured sessions/windows -> gates.evaluate_entry_gates()
  Broker Protocol <-> BrokerGateway <-> SimBroker (test/development implementation)
  StateStore <-> local SQLite database
  NewsEvent/calendar -> validation and is_news_blocked() helper
  Broker -> app.data.fetch_history()
         -> validated candle series -> Parquet cache/manifest
```

There is deliberately no arrow from `bot.main` to the strategy, replay, broker,
or store in the current runtime path. `Broker` is a `Protocol`;
`MT5ReadOnlyBroker` implements candle, symbol-specification, tick, and
connection/shutdown operations needed by history caching. `MT5Broker` separately
implements account/position/order operations with a non-demo refusal guard.
Its fake-API tests pass, but its real demo-terminal behavior and tick-derived
offset have not been verified; see `../BLOCKED.md` and `../QUESTIONS.md`.
`SimBroker` is the simulated implementation; its deterministic fault plan
supports order and tick failure scenarios for tests. `Engine` accepts a
context factory and processes closed bars; `run_management_lane()` performs
reconciliation and risk/position management. Runtime scheduling and MT5 wiring
remain absent, and the engine's full replay acceptance criteria are incomplete.

## Module notes and review entry points

### Application and configuration

- `bot/main.py`: argparse entry point; accepts only the `validate` command.
  Settings/schedule errors are printed to stderr and return exit code 2.
- `bot/app/config.py`: strict, frozen Pydantic models reject extra fields;
  settings require demo mode and disallow live mode; Strategy A/B values are
  fixed to approved constants; symbol names are restricted to the configured
  forex/metals allowlist. The loader returns the parsed settings plus a
  SHA-256 of canonicalized settings. `build_simulator_costs()` maps configured
  per-symbol commission and slippage assumptions into the simulator.
  `schedule_validation_failures()` rejects enabled symbols with missing Friday
  coverage or overlapping sessions; the current template therefore fails
  `validate` until reviewed broker schedules are applied.
- `bot/app/data.py`: selects closed candles; checks tick freshness, timestamp
  order/uniqueness, interval gaps, and bounded market closures; reads/writes a
  schema-versioned Parquet cache manifest including symbol specifications and
  gap counts. The cache workflow rejects unexplained gaps.
- `bot/app/backtest.py`: runs one symbol/timeframe of Strategy A through closed
  candles, cost-aware sizing, risk checks, and `SimBroker`. The default run
  excludes the latest 12 months; `--final` permits them. Friday flatten and
  live-engine parity are not implemented in this runner.
- `bot/app/symbol_validation.py` and `tools/validate_symbols.py`: validate
  broker specifications, ticks, H1 history, spread, minimum-lot risk, and
  sessions; `--write` updates only the `hours_utc` settings.
- `bot/app/executor.py`: journals entry/close intents before state-changing
  broker calls, resolves uncertain outcomes through deal/position searches,
  reconciles trades and positions, and records closes from broker deals.
- `bot/app/engine.py`: pure `evaluate_candle`, settings-ordered closed-bar entry
  lane, Windows single-instance lock, startup checks, and callable management
  lane. It is not connected to a scheduled runtime loop.
- `bot/app/reports.py`: writes trade and equity-curve CSVs plus a JSON summary
  with net profit, win rate, profit factor, drawdown, and trade count.
- `settings.toml`: demo-oriented template. It contains account/configuration
  identifiers and risk/execution parameters, not credentials. Treat local
  values as private; do not copy them into review prompts or commits.

### Shared core

- `bot/core/types.py`: immutable `Candle`, `SymbolSpec`, `Proposal`,
  `StrategyResult`, `OpenRisk`, `Position`, and `Decision` types; validates basic
  time, price, and proposal-side invariants.
- `bot/core/clock.py`: `Clock` protocol, UTC `SystemClock`, and controllable
  `SimClock` for tests and replay.
- `bot/core/indicators.py`: pure indicator calculations for EMA, true range,
  Wilder smoothing/ATR, Bollinger bands, RSI, and ADX.
- `bot/core/strategy_a.py`: pure Strategy A proposal generation from closed
  candles and explicit `StrategyAParams`; settings convert through
  `StrategyASettings.to_params()`. Defaults use a 600-candle warm-up and 1,000
  candle window. Checks parameterized trend/pullback/trigger rules and builds an
  ATR-based stop and configured R-multiple target.
- `bot/core/strategy_b.py`: fixed H1-only quiet-market mean-reversion proposal;
  warm-up requirement is 300 candles, bounded working window 1,000. Uses
  Bollinger/RSI/ADX/ATR, stop-width checks, and a minimum 1R reward check.
- `bot/core/costs.py`: estimates per-lot stop risk and total spread,
  round-trip commission and two-sided slippage; rejects non-finite inputs.
- `bot/core/sizing.py`: uses ask for long and bid for short quotes, includes
  adverse entry/stop slippage and round-trip commission in risk sizing, applies
  the cost gate, floors volume to broker steps, enforces volume bounds, and
  checks that rounded sizing does not exceed requested risk.
- `bot/core/gates.py`: deterministic entry checks for configured UTC session
  windows, weekend no-entry/flatten windows, rollover, app-supplied news
  freshness/event decisions, spread, and abnormal candle range. An empty
  session schedule fails closed. Configuration validation reports missing and
  overlapping schedules; the current template intentionally remains incomplete.
- `bot/core/risk.py`: checks daily headroom, max open/pending trades, one active
  slot per symbol, net base/quote currency risk exposure, prospective margin use,
  and a six-candle post-stop cooldown. `evaluate_loss_limits()` checks daily and
  monthly equity drawdown, including floating P&L. Daily entry headroom is
  computed against start-of-day equity after realized P&L and full open/pending
  stop risk. The engine consumes these helpers in its callable evaluation and
  management lanes; full replay acceptance remains pending.
- `bot/core/replay.py`: deterministic single-symbol replay. Strategy sees
  closed bars and fills at the next bar open; models spread, commission,
  slippage, stop/target exits, conservative stop-first ordering where both are
  touched, and an optional candle-count time stop. This is a provisional first
  backtest and is not the final parity check against the live engine.
- `bot/core/timeutil.py`: converts broker server wall-clock epochs using the
  broker-reported offset, exposes New York-offset sanity checking, and computes
  server-midnight day/month boundaries. `MT5Broker` derives its offset from a
  fresh tick, checks it against the New York rule, and uses it for position/deal
  timestamps; the live-terminal cross-check remains outstanding.

### Adapters and persistence

- `bot/adapters/broker.py`: typed account/terminal, tick, position, deal, order
  request/result DTOs, and the `Broker` protocol, including margin calculation
  and broker UTC-offset queries. It defines operations, not a concrete network
  connection.
- `bot/adapters/mt5_broker.py`: read-only MT5 history access plus a separate
  `MT5Broker` implementing the broker protocol. It refuses non-demo accounts,
  selects only a symbol-supported fill mode, and converts broker-time position
  and deal epochs using the checked offset. History ranges are requested in
  180-day chunks. Order operations have fake-API tests only; real demo
  verification and visual offset confirmation remain outstanding.
- `bot/adapters/gateway.py`: serializes broker calls, marks transport/lock
  failures degraded, and uses bounded exponential retries only for idempotent
  reads and checks. It does not retry order send, modify, or close operations.
- `bot/adapters/sim_broker.py`: demo-snapshot-only simulator with in-memory
  positions/deals, an injected `SimClock`, bid/ask order entry, estimated margin,
  configurable per-symbol commission/slippage assumptions, unrealized-equity
  accounting, and candle range stop/target exits with stop-first handling when
  both levels are touched.
  Its deterministic fault plan includes timeout outcomes, dropped responses,
  post-send crash, invalid stops, disconnect, stale tick, wrong fill mode, and
  duplicate candles. `build_simulator_costs()` supplies assumptions from
  `settings.toml`, but no trading engine currently instantiates or drives the
  simulator.
- `bot/adapters/store.py`: thread-local SQLite connections configured for WAL
  and FULL synchronous durability; initializes a versioned schema. Includes
  state flags, daily/monthly baselines, candle progress, intents, trades,
  decisions, equity log, Telegram update cursor, and alert outbox tables. Trade
  records retain actual risk and code/settings versions. Exposes operations for
  flags, baselines, atomic candle-decision progress, intent transitions,
  unresolved intents, equity logging, alerts, and Telegram cursor.

### News and tools

- `bot/app/news.py`: parses/normalizes UTC news events, checks required CSV
  columns, sorting, event gaps and minimum future coverage, maps supported
  symbols to currencies, and provides an event-window blocking helper.
- `config/news.csv`: sample calendar data; freshness/coverage is time-dependent.
- `tools/validate_news.py`: standalone validation command using the configured
  calendar. It does not connect to a terminal or trade.
- `tools/cache_history.py`: fetches closed H1/H4 bars for enabled symbols from a
  read-only MT5 connection and writes Parquet plus manifests. Current local
  caches cover six enabled FX pairs from 2 December 2021 through 30 October
  2026, not the six-year target. They start at 16:00 UTC on 2 December 2021
  because an earlier six-hour EURUSD H1 gap is unexplained and remains rejected.
- `tools/replay.py`: runs a single-symbol Strategy A replay from the cache and
  saves reports under `data/reports/`. The 1 November EURUSD H1 report is at
  `data/reports/EURUSD/H1/20261101T043220Z-c9c51be3/`; it recorded 401 trades,
  -$226.86 net profit, 33.92% win rate, 0.935 profit factor, and $354.96
  (11.55%) maximum drawdown. It excluded 6,191 recent bars and had no open trade.

## Test and verification map

| Test file | Main behavior exercised |
|---|---|
| `tests/test_clock.py` | UTC system clock and simulated clock controls |
| `tests/test_timeutil.py` | Broker-reported offset conversion and server day/month boundaries across DST |
| `tests/test_config.py` | Environment-only secrets, startup guards, settings-hash stability/change, unknown-key rejection, Git versioning and rotating logging |
| `tests/test_core.py` | UTC Candle validation; Strategy A warm-up/config and exact 1,000-candle window; sizing; equity loss limits; daily headroom, margin, cooldown and currency exposure; DST conversion; replay fill ordering/time stop/input ordering and closed-candle caller boundary |
| `tests/test_costs.py` | Per-lot spread/commission/slippage estimate, cost-gate rejection and invalid-input handling |
| `tests/test_data.py` | Tick freshness, candle order/gap checks and Parquet/manifest round-trip |
| `tests/test_engine.py` | Entry guard ordering, schedule/news/market gates, closed-candle progress, single-instance lock, monthly flatten |
| `tests/test_executor.py` | Fault outcomes, stop-new lock check, unknown recovery, close intents, adoption, missing-stop handling and reconciliation |
| `tests/test_mt5_broker.py` | Read-only mappings plus demo-only MT5 account, offset, order, retcode, fill-mode, and lifecycle behavior using a fake API |
| `tests/test_replay.py` | Closed-bar holdout, deterministic SimBroker replay, no-lookahead boundary, and saved reports |
| `tests/test_indicators.py` | EMA and ATR outputs against pandas `ewm` reference formulas |
| `tests/test_strategy_a.py` | Strategy A long/short proposal, hand-built H1/H4 fixtures, trend/pullback/trigger conditions, stop sizing and per-candle EMA rules |
| `tests/test_gates.py` | Session/overnight hours, rollover, weekend flatten window, news, spread and volatility gates |
| `tests/test_gateway.py` | Bounded read retries, lock release, degraded state, and no retry of order sends |
| `tests/test_strategy_b.py` | H1 constraint, valid long proposal shape, expected rejection reasons |
| `tests/test_broker.py` | Demo-only simulator, bid/ask fills, configurable commission/slippage, candle stop/target exits with stop-first dual-hit handling, account equity, terminal/margin/time metadata, unknown outcomes, crash-after-send, stale ticks, duplicate candles, explicit rejection codes, disconnect/reconnect |
| `tests/test_store.py` | SQLite durability, schema migration, UTC-only timestamps, alert outbox, versioned trade rows, intent state machine, concurrent writers, duplicate candles, write-once baseline |
| `tests/test_news.py` | Calendar ordering/coverage and currency-based blocking windows |
| `tests/test_validate_symbols.py` | SimBroker coverage/spread/sizing/session checks and LF/CRLF-safe `hours_utc` writes |

The current full suite and lint status are recorded in the project README/checkpoint;
the tests validate components and focused simulator workflows, not the full
replayed-week acceptance, broker offset, complete six-year dataset,
profitability, or demo-trading readiness.

Run the project suite from `bot_trader` with:

```powershell
python -m pytest
```

Validate settings with:

```powershell
python -m bot.main --settings settings.toml validate
```

Validate the news calendar with:

```powershell
python tools/validate_news.py --settings settings.toml
```

The news command's result depends on the dates in the calendar and the current
time. A successful config/calendar check is not a trading readiness or safety
verdict.

## Suggested independent review checklist

1. Trace the validation-only application entry point and separate read-only
   history adapter. Review `MT5Broker`'s demo refusal and note it is not wired
   into a runtime or verified against a real demo terminal.
2. Verify settings constraints against intended requirements, especially the
   strict Strategy A/B constants and the difference between declared settings
   and values consumed directly by core modules.
3. Check closed-bar/no-lookahead assumptions, indicator warm-up/indexing,
   proposal invariants, and stop/target construction in both strategies.
4. Independently validate risk math, cost assumptions, currency exposure
   aggregation, volume-step rounding, and behavior at boundaries.
5. Inspect replay fill semantics, spread side, gaps, ambiguous same-bar
   stop/target hits, commissions/slippage, and time-stop candle counting.
6. Exercise `SimBroker` unknown outcomes and `StateStore` intent transitions
   together; note that the tests cover them separately and do not implement
   restart reconciliation orchestration.
7. Check timezone assumptions and test DST transition boundaries for the
   intended broker's actual server-time convention, including the startup offset
   alert when the live broker value differs from the expected New York rule.
8. Verify the replay parity boundary: provisional replay is okay as an initial
   backtest, but the final parity check requires one fill model across both the
   replay path and the broker/simulated execution path.
9. Treat news calendar validity as time-sensitive; verify event source,
   currency/impact policy, coverage and blocking-window boundary behavior.
10. Keep findings separated into implemented-and-tested behavior, implemented
    but not integrated behavior, and explicitly unimplemented work.
