# Trading Bot: Remaining Implementation Tasks

Written 3 Nov 2026 from a read of the `lefatsiane/TradingBot` repo (`bot_trader/`) against
`BOT_BLUEPRINT.md`, the Implementation Task List and the SDLC Record. It lists only what is
**not built yet**. Everything marked "exists" below is already in the repo and tested; reuse it,
do not rewrite it.

Give your AI assistant **one task at a time**, plus the matching section of
`Trading Bot Full SDLC Record.docx` (the design record is the source of truth).

---

## 0. Rules for the AI (paste these at the top of every session)

1. Python 3.14, Windows 10, run everything from the `bot_trader/` folder. Before every commit
   `python -m pytest` and `ruff check .` must both pass. Tests live in `tests/`, CLI tools in `tools/`.
2. Write the tests first, then the smallest code that passes them. Tests are deterministic: use
   `SimClock` and `SimBroker`, never the real clock or a real terminal. Use `hypothesis` for
   property tests where a task says so.
3. **Layering (enforced by `tests/test_architecture.py`, task 0.1):**
   - Only `bot/adapters/mt5_broker.py` may import `MetaTrader5`.
   - `bot/core/` must not import `MetaTrader5`, `sqlite3`, `requests`, `bot.adapters` or `bot.app`,
     and must not call `datetime.now`, `datetime.utcnow` or `time.time`. Time is always an argument
     or comes from a `Clock`.
   - Anything with threads, HTTP or SQLite lives in `bot/adapters/` or `bot/app/`.
4. **Safety invariants (never weaken):**
   - Demo only. `allow_live = false`, `mode = "demo"`. Refuse to start otherwise (`startup_guard` exists).
   - Every order carries a stop-loss and take-profit. No position may exist without a stop-loss;
     if one does, set it or close the position.
   - The intent row is committed as `PENDING` **before** an order is sent. State-changing broker
     calls (send, modify, close) are never auto-retried by `BrokerGateway`.
   - Never resend an order whose outcome is unknown until a search of positions and deals by magic
     number and intent ID proves it is absent.
   - Shutting the bot down never closes trades; broker-held stops protect them.
   - Reasons are `ReasonCode` members (`bot/core/types.py`), never free text.
5. **Do not change** any value under `[risk]`, `[strategy_a]`, `[strategy_b]` or the approved
   constants enforced in `bot/app/config.py`. Do not tune strategy rules to improve backtest results
   (Strategy A's 1 Nov result is negative: 401 trades, profit factor 0.935. The plan allows bug
   fixes only).
6. Secrets come from environment variables only: `MT5_PASSWORD`, `TELEGRAM_TOKEN`,
   `HEALTHCHECK_URL` (`load_runtime_secrets` exists). Never print, log, commit or put them in a prompt.
7. Check what exists before writing: read the file, search for the function name, extend rather than
   duplicate.

### Existing building blocks to reuse

| Need | Already in the repo |
|---|---|
| Domain types, `ReasonCode`, `Side`, `Candle`, `SymbolSpec`, `Proposal`, `Decision`, `OpenRisk` | `bot/core/types.py` |
| Clock | `bot/core/clock.py` (`Clock`, `SystemClock`, `SimClock`) |
| Broker offset to UTC, server midnight/month boundaries | `bot/core/timeutil.py` (`broker_to_utc`, `check_broker_offset`, `trading_day_start_utc`, `month_start_utc`, `next_trading_day_boundary_utc`, `next_month_boundary_utc`) |
| Strategies | `strategy_a.propose(candles, params)`, `strategy_b.propose(candles, timeframe=)` |
| Costs, sizing, risk, gates | `costs.estimate_costs`, `sizing.size_trade`, `risk.evaluate_entry` / `evaluate_loss_limits` / `RiskLimits`, `gates.evaluate_entry_gates` / `SessionWindow` / `GateDecision` |
| Broker interface and DTOs | `bot/adapters/broker.py` (`Broker` protocol, `OrderRequest`, `BrokerResult`, `BrokerPosition`, `BrokerDeal`, `BrokerAccount`, `Tick`) |
| Serialized broker access | `bot/adapters/gateway.py` (`BrokerGateway`, `.degraded`) |
| Simulator with fault injection | `bot/adapters/sim_broker.py` (`SimBroker`, `FaultPlan`, `Fault`, `SimCosts`, `SimulatedCrash`, `process_candle`) |
| Persistence | `bot/adapters/store.py` (`StateStore`: flags `stop_new`, `close_all_pending`, `halted_daily`, `halted_monthly`, `halted_error`; baselines; intents with states PENDING/SENT/CONFIRMED/FAILED/UNKNOWN; `commit_candle_decisions`; alert outbox; Telegram cursor; `record_equity`) |
| Data | `bot/app/data.py` (`fetch_history`, `last_closed_candles`, `validate_candles`, Parquet cache read/write) |
| News | `bot/app/news.py` (`load_news_calendar`, `validate_news_calendar`, `is_news_blocked`, `calendar_has_minimum_coverage`, `affected_currencies_for_symbol`) |
| Config and guards | `bot/app/config.py` (`load_settings` returns `(settings, sha256)`, `startup_guard`, `build_simulator_costs`) |
| Versioning, logging | `bot/app/versioning.py`, `bot/app/logging_setup.py` |
| Single-symbol Strategy A backtest | `bot/app/backtest.py`, `bot/core/replay.py`, `bot/app/reports.py`, `tools/replay.py` |
| History download (read-only) | `bot/adapters/mt5_broker.py::MT5ReadOnlyBroker`, `tools/cache_history.py` |

---

## 0. Housekeeping and small gaps (do first, about an hour)

- [x] **0.1 `tests/test_architecture.py`** (planned in the task list but missing). Walk every `.py`
  file under `bot/` with `ast`. Fail if any file other than `bot/adapters/mt5_broker.py` imports
  `MetaTrader5`. Fail if any file in `bot/core/` imports `MetaTrader5`, `sqlite3`, `requests`,
  `bot.adapters` or `bot.app`, or calls `datetime.now`, `datetime.utcnow`, `date.today` or `time.time`.
  Run it; fix any real violation it finds (do not weaken the test).
- [x] **0.2 Repo hygiene.** Add a root-level `.gitignore` (parent of `bot_trader/`) containing
  `__pycache__/`, `*.pyc`, `.venv/`; run `git rm -r --cached __pycache__`. Optional: move the real
  `expected_login` out of the public repo by committing `settings.example.toml` and git-ignoring
  `settings.toml`. Update README setup text if you do this.
- [x] **0.3 Strategy B parameter object.** `strategy_b.py` hard-codes its constants while
  `StrategyBSettings` exists. Add a frozen `StrategyBParams` dataclass (mirroring `StrategyAParams`),
  `StrategyBSettings.to_params()`, and change the signature to
  `propose(candles, params, *, timeframe="H1")`. Behaviour must be identical for default params
  (existing `tests/test_strategy_b.py` must pass unchanged apart from the call). Needed so the
  sensitivity check in task 12 can move values by about 20% (that tool builds params directly; the
  approved-constant check stays in settings validation).

**Done when:** architecture test passes, `git status` no longer shows `__pycache__`, tests and ruff green.

---

## 1. Scheduling helpers and `hours_utc` (needed by gates and engine)

`evaluate_entry_gates` needs `rollover_at_utc`, `weekend_close_utc` and a `SessionWindow` list, and an
empty session list **fails closed**. All `hours_utc` in `settings.toml` are empty today, so the bot can
never trade until this and task 4 are done.

- [x] **1.1 `hours_utc` format.** In `bot/app/config.py` define and validate the string format for
  `SymbolSettings.hours_utc`, e.g. `"mon 00:00-21:55"`, `"fri 00:00-21:55"` (weekday `mon..sun`, 24h UTC,
  one entry per window; a window that crosses midnight is two entries). Reject bad strings at load.
  Add `parse_sessions(hours: tuple[str, ...]) -> tuple[SessionWindow, ...]` (in `bot/app/config.py` or
  `bot/core/schedule.py`).
- [x] **1.2 `next_weekend_close_utc(now_utc, sessions) -> datetime`** in `bot/core/` (pure): the next
  Friday session end in UTC at or after `now_utc`. Raise if `sessions` has no Friday window.
- [x] **1.3 `next_rollover_utc(now_utc, offset_hours)`**: just wrap the existing
  `next_trading_day_boundary_utc` (server midnight) with a clear name and test it across both DST
  shifts (server UTC+3 to UTC+2 around 1 Nov).
- [x] **1.4 Startup schedule validation:** a function returning failures if an enabled symbol has an
  empty `hours_utc`, no Friday window, or overlapping windows. `bot.main validate` must report these.
- [x] **Tests:** window edges for every helper, DST weeks, `weekend_close < now` rejected.

**Done when:** given a settings file and `now`, you can compute everything `evaluate_entry_gates` needs.

---

## 2. State store additions (`bot/adapters/store.py`, migration to `SCHEMA_VERSION = 3`)

Read the existing `CREATE TABLE` statements first; add only what is missing via a **numbered migration**
(migrations must run twice safely, existing tests stay green).

- [x] **2.1 Trade closing:** `close_trade(ticket, closed_at_utc, close_price, profit, commission, exit_reason)`;
  add an `ExitReason` `StrEnum` in `types.py`: `STOP_LOSS`, `TAKE_PROFIT`, `TIME_STOP`, `WEEKEND_FLATTEN`,
  `MONTHLY_CAP`, `CLOSE_ALL`, `MANUAL`, `UNKNOWN`. Store it on the trade row.
- [x] **2.2 Queries:** `open_trades() -> tuple[TradeRecord, ...]`; `get_bar_progress(symbol, timeframe) -> str | None`;
  `realized_pnl_between(start_utc, end_utc) -> float` (net of commission); `last_stop_out_utc(symbol, timeframe) -> str | None`
  (for the 6-candle cooldown); `get_intent(intent_id)`, `find_intent_by_key(key)`.
- [x] **2.3 Start log for the crash-loop guard:** table `starts(started_at_utc, planned INTEGER)`,
  `record_start(at_utc, planned)`, `unplanned_starts_since(since_utc) -> int`. A clean shutdown writes
  `planned=1` on the next start by clearing a `clean_shutdown` marker.
- [x] **2.4 Heartbeat snapshot:** `set_meta(key, value)` / `get_meta(key)` for small values (last
  engine heartbeat UTC, clean_shutdown marker).
- [x] **Tests (`tests/test_store.py`):** each new method, migration 2 to 3 on a populated DB,
  two threads writing, UTC-only timestamps enforced.

---

## 3. Live MetaTrader adapter (`bot/adapters/mt5_broker.py`)

**Status:** Adapter implementation and fake-API tests are complete. Manual demo-terminal verification remains blocked; see `BLOCKED.md` and the pending offset question in `QUESTIONS.md`.

Keep `MT5ReadOnlyBroker` untouched (history tooling uses it). Add a second class `MT5Broker` that
implements the **full** `Broker` protocol. It stays the only file that imports `MetaTrader5`.

- [x] **3.1 Extend the injected `_MT5API` protocol** with `login`, `account_info`, `positions_get`,
  `history_deals_get`, `order_check`, `order_send`, `order_calc_margin`, `symbol_select` and the
  needed `TRADE_ACTION_*`, `ORDER_TYPE_*`, `ORDER_FILLING_*`, `TRADE_RETCODE_*` constants, so every test
  uses a fake API and no real terminal.
- [x] **3.2 Reads:** `account()` (login, server, currency, balance, equity, `is_demo` from trade mode,
  `margin_mode` as `"hedging"`/`"netting"`/`"exchange"`, `trade_allowed`, leverage), `terminal_info()`,
  `positions(magic)`, `history_deals(since_utc, magic)`, `calculate_margin(request)`. Convert all MT5
  server-time epochs with `broker_to_utc(epoch, offset)`; never assume an offset.
- [x] **3.3 Server offset.** The standard API has no offset property. Implement a provider that reads
  the latest tick's server timestamp for a liquid symbol (EURUSD), requires the tick to be fresh
  (under 60 s old), subtracts real UTC from the injected clock, rounds to whole hours, then passes the
  result through `check_broker_offset`. Refuse (raise) when the market is closed or the tick is stale.
  *Decision for Lefa:* the current code comment says not to infer the offset from UTC ticks; this
  approach is the one `connection_test.py` already used, but it must be run on a weekday with open
  markets and cross-checked once by eye against MT5's server clock before relying on it.
- [x] **3.4 `check(request)` and `send_market(request)`:** build a `TRADE_ACTION_DEAL` request with
  symbol, volume, type from `Side`, price from the current tick (ask for long, bid for short), `sl`,
  `tp`, `deviation` (from `settings.execution.deviation_points`), `magic`, `comment = intent_id`
  (numeric, at most 31 chars), and a **fill mode picked from the symbol's allowed modes**
  (never assume). Map every `retcode` to the `BrokerResult.code` strings the simulator already uses
  (`DONE`, `INVALID_STOPS`, `INVALID_FILL`, `PRICE_OFF`, `CONNECTION`, plus `REQUOTE`, `REJECTED`,
  `NO_MONEY`, `MARKET_CLOSED`, `TIMEOUT`, `UNKNOWN`). Return `None` when no result came back
  (outcome unknown). Fill `ticket`, `price`, `lots`, and `commission` when known.
- [x] **3.5 `modify_sl_tp(ticket, sl, tp)` and `close(ticket)`** (opposite-side market order against
  that ticket; on a hedging account use `position=` in the request). Same retcode mapping.
- [x] **3.6 Terminal state:** `connect()` initialises with login/server and reads the password from the
  environment value passed in by the caller (never from settings); `shutdown()` on exit.
- [x] **Tests (`tests/test_mt5_broker.py`):** fake API; one test per retcode mapping, comment length,
  fill-mode selection, hedging vs netting close, `None` result when the fake raises a timeout.

**Done when:** with a fake API every `Broker` protocol method works, and a manual one-off script
(outside the repo) can call `account()`, `positions()` and `candles()` against the real demo terminal.

The fake-API implementation is complete. The manual real-demo check has not been run; do not treat
section 3 as fully verified until the blocker in `BLOCKED.md` is cleared.

---

## 4. `tools/validate_symbols.py`

- [x] For each symbol in `settings.toml`: fetch spec, tick, 6 months of H1 history and the broker's
  session times; check volume min/step against sizing at `risk_per_trade_pct` (reports
  `BELOW_MIN_LOT` risk), spread behaviour against `max_spread_points`, history length, and that the
  symbol is selectable. Print a table and exit non-zero if any enabled symbol fails.
- [x] Write the weekly trading hours in UTC (format from task 1.1) into the `hours_utc` of each
  passing symbol. Do not edit risk or strategy sections. Do it as a printed suggestion plus a
  `--write` flag that edits only `hours_utc`.
- [x] **Tests:** run against `SimBroker`; a symbol with too-large minimum lot is reported and not enabled.

**Code/tests done; owner acceptance blocked:** the six pairs have not been checked against the
demo terminal, `settings.toml` hours remain unchanged, and `python -m bot.main validate` correctly
reports the six enabled symbols with empty schedules.
(XAUUSD stays disabled until its minimum size passes the risk check, as already noted in the config.)

---

## 5. Executor and reconcile (`bot/app/executor.py`)

All order traffic goes through this class, which uses `BrokerGateway`, `StateStore` and a `Clock`.

- [x] **5.1 Return-code policy table** (module-level dict, one place only). For every code
  `SimBroker` or `MT5Broker` can return: `DONE` to CONFIRMED; `INVALID_STOPS`, `INVALID_FILL`,
  `REJECTED`, `NO_MONEY`, `MARKET_CLOSED` to FAILED, no retry; `REQUOTE`, `PRICE_OFF` to one retry
  only if `max_send_retries` allows, after a fresh `check`; `TIMEOUT`, `CONNECTION`, `UNKNOWN`, `None`
  to UNKNOWN. **Any code not in the table is treated as UNKNOWN (fail closed).** Test that every code
  the simulator can emit is in the table.
- [x] **5.2 `open_trade(...)`:** create the intent `PENDING` and commit; take the gateway lock; re-read
  `stop_new` **while holding the lock** and abort (mark FAILED, reason `STOP_NEW`) if set; `check()`;
  mark `SENT`; `send_market()`; map the result via the table; on CONFIRMED write the trade row with
  `actual_risk`, code version and settings version.
- [x] **5.3 Unknown outcome:** wait `unknown_outcome_wait_s` (through the injected clock so tests do
  not sleep), then search `positions(magic)` and `history_deals(...)` by ticket, then by intent ID
  in the comment. Found: CONFIRMED. Not found after the search: FAILED. Never resend before the search.
- [x] **5.4 `close_position(ticket, reason: ExitReason)`:** goes through the intent journal as a close
  intent, then `gateway.close`, then `store.close_trade(...)` using the deal history for the real
  price, profit and commission.
- [x] **5.5 `reconcile()`:** compare `positions(magic)` with `store.open_trades()`:
  position with no row: adopt it (write a trade row `strategy="ADOPTED"`), alert; row with no
  position: close it in the store from deal history; position with no stop-loss: set it from the
  intent record, else close it. Returns a report of actions for the caller to alert on.
- [x] **5.6 Commission check:** after each close compare charged commission with the table in
  settings; if drift exceeds 20% enqueue a warning alert.
- [x] **Tests (`tests/test_executor.py`) using `FaultPlan`:** `timeout_unfilled`, `timeout_filled`,
  `dropped_response`, `crash_after_send` (`SimulatedCrash`, then a fresh executor on the same DB),
  `invalid_stops`, `wrong_fill_mode`, `disconnect`, plus `stop_new` set while the lock is held.
  Assert after every case: **exactly one position or none, always with a stop-loss, no duplicate
  intents.**

**Done when:** every fault test leaves exactly one position or none, always protected.

---

## 6. Engine (`bot/app/engine.py`) in three parts

### 6a. Entry decision pipeline
- [ ] `evaluate_candle(...) -> Decision` as a function (no I/O of its own: all inputs passed in).
  Follow the **14 checks in the design record's order-of-checks table, cheapest first** (open the
  SDLC Record and follow it word for word). Rough order: `STOP_NEW`, `DAILY_HALT`, `MONTHLY_HALT`,
  `ERROR_HALT`, `WEEKEND_WINDOW`, `DATA_ERROR`, `SYMBOL_DISABLED`, `OUTSIDE_HOURS`, `NEWS_WINDOW` /
  `NEWS_CALENDAR_STALE`, `ROLLOVER`, `SPREAD_WIDE`, `SKIP_VOLATILITY`, `SLOT_TAKEN`, `COOLDOWN`,
  strategy skip, `COST_TOO_HIGH`, `BELOW_MIN_LOT`, `NO_MARGIN`, `MAX_TRADES`, `CURRENCY_EXPOSURE`,
  `DAILY_HEADROOM`, `CHECK_FAILED`.
- [ ] Reuse `evaluate_entry_gates`, the strategies, `size_trade`, `evaluate_entry` and `is_news_blocked`;
  do not re-implement their logic. If both strategies propose on the same symbol, Strategy A wins the slot.
- [ ] Compute `bars_since_stop` from `store.last_stop_out_utc` and the timeframe.
- [ ] **Tests:** one test per `ReasonCode` skip, in order, plus a case where two reasons apply and the
  earlier one in the table wins.

### 6b. Entry lane
- [ ] For each enabled symbol and each configured timeframe (A: H1 and H4, B: H1 only), in the
  settings-file order: fetch candles via the gateway, keep only closed candles with `last_closed_candles`,
  take the **last 1,000** (`window_candles`), and process only if the newest closed candle's open time
  is later than `store.get_bar_progress`.
- [ ] After each accepted trade, re-evaluate loss limits and exposure before the next symbol.
- [ ] Persist every skip and entry in one transaction with `commit_candle_decisions` (progress,
  decision rows, optional alert). A duplicate candle returns False and is ignored.
- [ ] An unexpected exception in the entry lane sets `halted_error`, sends a critical alert, and
  leaves the management lane running.

### 6c. Management lane (every `fast_tick_s`, runs even when entries are blocked)
- [ ] Update the heartbeat timestamp; read account, positions, ticks (failure marks the gateway
  degraded, alert once, entries blocked, retry with backoff).
- [ ] Run `reconcile()`; run `evaluate_loss_limits` on equity incl. floating P&L; set `halted_daily`
  (keep stop-losses on open trades) or `halted_monthly` (flatten everything via `close_position`).
- [ ] Friday flatten: inside `flatten_before_weekend_close_min` of each symbol's `next_weekend_close_utc`,
  close that symbol's bot positions (`ExitReason.WEEKEND_FLATTEN`).
- [ ] Strategy B time stop: close a Strategy B trade still open after 24 H1 candles (`TIME_STOP`).
  Strategy A sends no modifications, ever.
- [ ] Apply control flags (`close_all_pending`, `stop_new`); `record_equity` once a minute; roll the
  day baseline at server midnight and month baseline at server month start (`new_baseline`, write-once).

### 6d. Startup recovery (nine steps, in the order given in the design record)
- [ ] `Engine.start()`: guards (`startup_guard`) → settings hash → flags and baselines → read positions
  → resolve unresolved intents → reconcile → check every position has a stop-loss → check baselines
  → run the management lane once. Any failure: do not trade, critical alert.
- [ ] **Tests (`tests/test_engine.py`):** a full replayed day on `SimBroker` with `SimClock` including a
  Friday flatten, a news window, a rollover window, a daily-limit trip, a monthly-cap flatten, and a
  restart mid-day (new Engine on the same DB resumes without duplicates).

**Done when:** a week of replayed candles runs on `SimBroker` with no unexpected errors and no trade
without a stop-loss.

**Progress note:** evaluator, closed-bar entry processing, startup guards, and a callable management
lane are implemented with focused simulator tests. Tests cover strategy/size/risk reason propagation,
SDLC-ordered gate and risk precedence (including slot/cooldown before strategy evaluation),
stored-stop cooldown counting, daily-halt stop retention, Friday flattening, and invalid
closed-candle data without incorrectly halting management. Server-clock schedule/context wiring,
end-to-end replayed day/week, and restart-midday acceptance are still incomplete; keep section 6
open.

---

## 7. Unify replay with the engine (parity)

`bot/app/backtest.py` is currently a separate Strategy A loop: single symbol, no Friday flatten, no
Strategy B, no news/session gates. The design requires one fill model and one decision path.

- [ ] Build `bot/app/replay_runner.py`: feed cached closed candles for all enabled symbols in time
  order into the real `Engine` + `Executor` + `SimBroker` with a `SimClock` and a temporary
  `StateStore`. Candle open times drive `SimClock`; `SimBroker.process_candle` applies stop/target fills
  (stop-first on a dual hit, as today).
- [ ] Include: Friday flatten, rollover and news gates (calendar file may be empty for history: then
  gate off via an explicit `--no-news` flag that is recorded in the report), Strategy B and its time
  stop, the 6-candle cooldown, currency exposure and daily/monthly limits.
- [ ] Keep the 12-month holdout rule: refuse the last 12 months unless `--final` (the lock-file part is
  in task 12).
- [ ] Parity test: for a fixed fixture, `replay_runner` and the old `run_strategy_a_backtest` must give
  the same Strategy A trades when the new gates are disabled; record the parity percentage (design
  requires at least 95%).
- [ ] Update `tools/replay.py` to call the runner (multi-symbol, `--strategy a|b|both`).

**Done when:** one command replays all six pairs with A and B and writes trades, equity curve and
summary under `data/reports/`.

---

## 8. News calendar wiring (`config/news.csv`, `bot/app/news.py`)

- [x] Wire the existing calendar checks into the engine gate inputs: `news_calendar_valid` =
  `calendar_has_minimum_coverage(... 7 days ...)`, `news_blocked` = `is_news_blocked(...)` using
  `affected_currencies_for_symbol`. Stale calendar means no new entries (fail closed) and one alert.
- [x] Run `validate_news_calendar` at startup; warning alert when coverage drops under 10 days.
- [ ] **Data entry is yours, not the AI's:** extend `config/news.csv` from a real economic calendar
  (central-bank decisions, NFP, CPI for USD/EUR/GBP/JPY/CHF/AUD/NZD), in UTC. The AI must not invent events.
  Add a recurring Sunday reminder to extend it.
- [x] **Tests:** entry blocked from 30 min before to 30 min after an event (each edge), a stale file
  blocks everything, a pair is blocked by either of its two currencies.

**Progress note:** startup calendar validation, fail-closed entry gating, one-time stale-calendar
alerting, and the under-10-day warning are implemented and covered with deterministic tests. The
calendar contents and recurring Sunday reminder remain owner responsibilities; the existing CSV was
not edited or treated as verified real-world data.

---

## 9. Telegram, control commands, heartbeat (`bot/adapters/telegram.py`, `heartbeat.py`, `bot/app/control.py`)

- [ ] **`telegram.py`** (plain thread + `requests`): long-poll `getUpdates`; delete any webhook at
  startup; accept only `settings.control.telegram_chat_id`; ignore edited messages and group chats;
  ignore commands older than `command_max_age_s` (60) with a reply saying so; persist the last update
  ID via the store; backoff 1, 2, 4 s up to 30 on network failure; alert on a 409 conflict; compare the
  laptop clock with the reply's `Date` header and alert if off by more than 5 s.
- [x] **Alert sender:** `send_alert(severity, text)`: critical never suppressed, warnings with identical
  text merged within 10 minutes, info as it happens; on failure write to the outbox and retry
  (`pending_alerts`, `mark_alert_sent` exist).
- [ ] **`control.py`** commands: `/status`, `/stop_new`, `/resume`, `/close_all`.
  - `/resume` clears only `stop_new` and refuses while `halted_daily`, `halted_monthly` or
    `halted_error` is set.
  - `/close_all` needs the reply `YES` within 30 s; then set `stop_new` and `close_all_pending`, take the
    gateway lock (5 s timeout, retry), close every bot position through the executor as close intents,
    verify, retry every 3 s; if the market is closed, say the stop-losses remain and keep
    `close_all_pending` set. Closes only positions with the bot's magic number.
  - The control thread never imports MetaTrader; it acts through gateway, executor and store.
- [x] **`heartbeat.py`:** ping `HEALTHCHECK_URL` every 60 s from its own thread; never crash the bot on
  failure.
- [ ] **Tests (`tests/test_control.py`):** fake Telegram HTTP server (or an injected fake session) plus
  `SimBroker`: stale command, wrong chat ID, command replay after restart, `/close_all` while the lock
  is held 20 s, partial failure, market closed, `/close_all` racing an in-flight entry (the send gate
  must stop it), `/resume` refused during a halt.

**Progress note:** Telegram polling/filtering, cursor persistence, expiry replies, retry handling,
409/clock-drift alerts, alert-outbox delivery, basic safety commands, and a stoppable heartbeat thread
have focused fake-session tests. The command/heartbeat threads are not wired into a production
`bot.main run`; `/close_all` execution/retry details and the full control failure matrix remain open.

**Done when:** tests pass and a real `/status` from your phone gets an answer from the demo setup.

---

## 10. `bot/main.py`, runtime wiring and operations

- [ ] Add commands to `bot.main`: `run` (the bot), keep `validate`. `run` order: logging → settings →
  **single-instance file lock `runtime/bot.lock`** (refuse to start if held) → record start → crash-loop
  guard (3 unplanned restarts within an hour: start with `stop_new` set and alert) → secrets → connect
  `MT5Broker` through `BrokerGateway` → `startup_guard` → store → engine startup recovery → control thread
  → heartbeat thread → engine loops.
- [ ] Startup report: list any enabled symbol that can never be traded at current stop distances and
  balance (sizing returns `BELOW_MIN_LOT`).
- [ ] Clean shutdown on Ctrl+C or a service stop: set the clean-shutdown marker, stop threads, release
  the lock, leave trades open.
- [x] **`tools/backup.py`:** copy the SQLite DB into `backups/` once a day (use SQLite's backup API, not
  a file copy while it is open); keep 14 days.
- [ ] **`scripts/install_task.ps1`:** registers a Windows Task Scheduler task that starts
  `python -m bot.main run` at logon with the venv interpreter and working directory set. Document, do
  not automate silently: Windows auto-logon, sleep set to Never, active hours set to block automatic
  restarts for updates during the trading week.
- [x] **`docs/runbook.md`:** the manual backup route (stop the bot first, close the bot's positions in
  the MetaTrader mobile app, check the Trade tab is empty, `/resume` once back) and the healthchecks.io
  email alert path.
- [ ] **Tests:** second-instance lock refusal, crash-loop guard sets `stop_new`, clean shutdown leaves
  positions untouched.

**Progress note:** SQLite online backup/retention, manual operations runbook, and a simulator-tested
crash-loop guard helper exist. The guard, backup scheduling, Engine, Telegram adapter, heartbeat,
single-instance lock, and clean-shutdown behavior are not wired into `bot.main run`; Task 10 remains
open.

**Done when:** you can start the bot, see it connect to the demo account, kill it, restart it, and watch
it recover without duplicates.

---

## 11. Failure drills (automated, then manual)

- [ ] **Automated (`tests/test_drills.py`, SimBroker only):** dropped responses, rejected stops, stale
  ticks, wrong fill mode, duplicate candle, replayed Telegram command, crash right after send. Same
  assertion each time: one protected position or none.
- [x] **Manual checklist** in `docs/drills.md` (you run these on the demo): restart, network
  disconnect, MetaTrader killed, laptop sleep and wake, clock jump, process killed right after an
  order, `/close_all` with the market closed, a second instance started on purpose, stop the bot and
  confirm the missed-heartbeat alert reaches Telegram **and** email, send a test critical alert and
  confirm it arrives within 60 s, open and close small demo trades and compare actual commission with
  the table.

---

## 12. Statistics harness (`tools/stats.py`), built while the demo runs

All of this runs on the replay runner output (task 7). Cut order if time is short: backtesting.py
cross-check first, then sensitivity, then the rest stays.

- [ ] Bootstrap confidence intervals: 10,000 resamples of whole calendar weeks.
- [ ] Mark-to-market drawdown at every candle close.
- [ ] Random-entry control: 1,000 runs per strategy with the same trade counts, gates, cooldown,
  one-position rule and exits.
- [ ] Cost stress: rerun with spreads and slippage doubled.
- [ ] Per-instrument, per-timeframe and combined A+B reports.
- [ ] Holdout lock: the last 12 months load only with `--final`, and the run writes a lock file holding
  the code version so it can be run once.
- [ ] Variant counter: record how many strategy variants were tried.
- [ ] Sensitivity check: rerun each strategy with each fixed value moved about 20% (construct
  `StrategyAParams` / `StrategyBParams` directly).
- [ ] **Scorecard** file: each measure, value, interval, threshold, and PASS / FAIL / INCONCLUSIVE, plus
  date ranges and versions. Pass numbers come from the design record item 6 (worst day no worse than
  -1.5%, worst month no worse than -4.0%, cost drift within 20%, replay parity at least 95%, at most 3
  unplanned restarts). Do not invent thresholds.

---

## 13. Suggested order and effort

| Order | Tasks | Why this order |
|---|---|---|
| 1 | 0 | Cheap, protects the layering rules |
| 2 | 1, 2 | Gates cannot work without schedules and store queries |
| 3 | 3, 4 | Real broker access and symbol hours |
| 4 | 5 | Safe order handling before any engine |
| 5 | 6 (6a, 6c, 6d, then 6b) | Management lane and recovery before entries |
| 6 | 7, 8 | Replay and news share the engine |
| 7 | 9, 10 | Phone control and running as a service |
| 8 | 11 | Drills on the demo |
| 9 | 12 | Runs in parallel with the demo window |

Safety parts (tasks 4, 5, 6, 9, 10) are never cut. Strategy B wiring slips first. Today is 3 Nov and the plan
target is 9 Nov; the remaining work is large (the original estimate was about 70 hours total), so tell
the AI at the start of each task how many hours you have and cut per the plan's order, not the safety work.

## 14. Questions only you can answer

1. **Server-offset method** (task 3.3): is deriving it from a fresh tick's server timestamp acceptable, given the current code comment?
2. **`hours_utc` format** (task 1.1): the `"mon 00:00-21:55"` style is a proposal; change it if you prefer another.
3. **Friday flatten time** comes from each symbol's last Friday session end plus `flatten_before_weekend_close_min`; confirm that matches your intent.
4. **Strategy A result:** profit factor 0.935 is below 1. The plan says don't tune on it. Bring `strategy_a.py` back for the planned review for bugs, before the counted backtests.
