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

| Need                                                                                           | Already in the repo                                                                                                                                                                                                                                                              |
| ---------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Domain types, `ReasonCode`, `Side`, `Candle`, `SymbolSpec`, `Proposal`, `Decision`, `OpenRisk` | `bot/core/types.py`                                                                                                                                                                                                                                                              |
| Clock                                                                                          | `bot/core/clock.py` (`Clock`, `SystemClock`, `SimClock`)                                                                                                                                                                                                                         |
| Broker offset to UTC, server midnight/month boundaries                                         | `bot/core/timeutil.py` (`broker_to_utc`, `check_broker_offset`, `trading_day_start_utc`, `month_start_utc`, `next_trading_day_boundary_utc`, `next_month_boundary_utc`)                                                                                                          |
| Strategies                                                                                     | `strategy_a.propose(candles, params)`, `strategy_b.propose(candles, timeframe=)`                                                                                                                                                                                                 |
| Costs, sizing, risk, gates                                                                     | `costs.estimate_costs`, `sizing.size_trade`, `risk.evaluate_entry` / `evaluate_loss_limits` / `RiskLimits`, `gates.evaluate_entry_gates` / `SessionWindow` / `GateDecision`                                                                                                      |
| Broker interface and DTOs                                                                      | `bot/adapters/broker.py` (`Broker` protocol, `OrderRequest`, `BrokerResult`, `BrokerPosition`, `BrokerDeal`, `BrokerAccount`, `Tick`)                                                                                                                                            |
| Serialized broker access                                                                       | `bot/adapters/gateway.py` (`BrokerGateway`, `.degraded`)                                                                                                                                                                                                                         |
| Simulator with fault injection                                                                 | `bot/adapters/sim_broker.py` (`SimBroker`, `FaultPlan`, `Fault`, `SimCosts`, `SimulatedCrash`, `process_candle`)                                                                                                                                                                 |
| Persistence                                                                                    | `bot/adapters/store.py` (`StateStore`: flags `stop_new`, `close_all_pending`, `halted_daily`, `halted_monthly`, `halted_error`; baselines; intents with states PENDING/SENT/CONFIRMED/FAILED/UNKNOWN; `commit_candle_decisions`; alert outbox; Telegram cursor; `record_equity`) |
| Data                                                                                           | `bot/app/data.py` (`fetch_history`, `last_closed_candles`, `validate_candles`, Parquet cache read/write)                                                                                                                                                                         |
| News                                                                                           | `bot/app/news.py` (`load_news_calendar`, `validate_news_calendar`, `is_news_blocked`, `calendar_has_minimum_coverage`, `affected_currencies_for_symbol`)                                                                                                                         |
| Config and guards                                                                              | `bot/app/config.py` (`load_settings` returns `(settings, sha256)`, `startup_guard`, `build_simulator_costs`)                                                                                                                                                                     |
| Versioning, logging                                                                            | `bot/app/versioning.py`, `bot/app/logging_setup.py`                                                                                                                                                                                                                              |
| Single-symbol Strategy A backtest                                                              | `bot/app/backtest.py`, `bot/core/replay.py`, `bot/app/reports.py`, `tools/replay.py`                                                                                                                                                                                             |
| History download (read-only)                                                                   | `bot/adapters/mt5_broker.py::MT5ReadOnlyBroker`, `tools/cache_history.py`                                                                                                                                                                                                        |

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
      one entry per window; `24:00` is an exclusive end-only boundary, and other midnight-crossing
      windows are split into daily entries). Reject bad strings at load.
      Add `parse_sessions(hours: tuple[str, ...]) -> tuple[SessionWindow, ...]` (in `bot/app/config.py` or
      `bot/core/schedule.py`). Each reviewed schedule is tagged `US winter` or `US summer`; runtime shifts
      it by the New York-rule offset difference in the opposite season, including weekday rollover.
- [x] **1.2 `next_weekend_close_utc(now_utc, sessions) -> datetime`** in `bot/core/` (pure): the next
      Friday session end in UTC at or after `now_utc`. Raise if `sessions` has no Friday window.
- [x] **1.3 `next_rollover_utc(now_utc, offset_hours)`**: just wrap the existing
      `next_trading_day_boundary_utc` (server midnight) with a clear name and test it across both DST
      shifts (server UTC+3 to UTC+2 around 1 Nov).
- [x] **1.4 Startup schedule validation:** a function returning failures if an enabled symbol has an
      empty `hours_utc`, no Friday window, or overlapping windows. `bot.main validate` must report these.
- [x] **Tests:** window edges for every helper, DST weeks, `weekend_close < now` rejected.
- [x] **Owner-confirmed timezone and flatten rules:** use New York server-wall time plus seven hours;
      ticks are sanity checks only. Friday flatten uses the reviewed session close minus the configured
      flatten window, retries and alerts if bot positions remain, and blocks entries until flat.

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

**Status:** Adapter implementation and fake-API tests are complete. Manual demo-terminal verification remains blocked; see `BLOCKED.md` for the required owner visual clock cross-check.

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
- [x] **3.3 Server offset.** Use the confirmed deterministic New York DST rule plus seven hours
      (UTC+2 in US winter, UTC+3 in US summer) as the timestamp-conversion source. Sample advancing,
      fresh ticks and compare the half-hour-rounded offset with the rule as a sanity check only. A
      mismatch sets `stop_new` and alerts; the owner still needs to visually cross-check the displayed
      server clock on an open-market weekday.
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

- [x] `evaluate_candle(...) -> Decision` is a function with no I/O of its own; its inputs are passed
      in through `EntryContext`.
- [x] Reuse `evaluate_entry_gates`, the strategies, `size_trade`, `evaluate_entry` and `is_news_blocked`;
      Strategy A is preferred when both strategies propose on the same symbol.
- [x] Compute `bars_since_stop` from `store.last_stop_out_utc` and the timeframe.
- [x] Follow the **14 checks in the design record's order-of-checks table, cheapest first** (verified
      against the SDLC record's entry-check table).
      The design order is `STOP_NEW`, `DAILY_HALT`, `MONTHLY_HALT`, `ERROR_HALT`, `WEEKEND_WINDOW`,
      `DATA_ERROR`, `SYMBOL_DISABLED`, `OUTSIDE_HOURS`, `NEWS_WINDOW` / `NEWS_CALENDAR_STALE`,
      `ROLLOVER`, `SPREAD_WIDE`, `SKIP_VOLATILITY`, `SLOT_TAKEN`, `COOLDOWN`,
      strategy skip, `COST_TOO_HIGH`, `BELOW_MIN_LOT`, `NO_MARGIN`, `MAX_TRADES`, `CURRENCY_EXPOSURE`,
      `DAILY_HEADROOM`, `CHECK_FAILED`.
- [x] Add skip-reason tests for each evaluator and execution outcome, with precedence cases for
      competing reasons.

### 6b. Entry lane

- [x] For each enabled symbol and configured timeframe (A: H1 and H4, B: H1 only), in settings-file
      order: fetch candles through the gateway, retain closed candles, cap the window at 1,000, and
      process only bars newer than persisted progress.
- [x] After each accepted trade, rebuild account/position/margin context and re-evaluate loss limits
      and exposure before the next symbol.
- [x] Persist each valid-candle decision with progress through `commit_candle_decisions`; duplicate
      candles are ignored.
- [x] Unexpected entry-lane exceptions set `halted_error`, enqueue a critical alert, and leave the
      management lane callable.

### 6c. Management lane

- [x] Wire the lane to run every `fast_tick_s`, independently of entry eligibility (scheduled by the
      runtime loop in section 10).
- [x] On each call, update the heartbeat and read account, positions and ticks. A failed health check
      marks the gateway degraded, alerts once, blocks entries and can recover on a later management call;
      serialized gateway reads retry with backoff.
- [x] Run `reconcile()` and evaluate loss limits on broker equity, including floating P&L. Daily halt
      retains protected positions; monthly halt closes bot positions through `close_position`.
- [x] Flatten each symbol inside `flatten_before_weekend_close_min` of its configured Friday close
      (`ExitReason.WEEKEND_FLATTEN`).
- [x] Close Strategy B H1 trades at the configured 24-closed-candle time stop (`TIME_STOP`); no
      strategy-driven modifications are sent for Strategy A.
- [x] Apply `close_all_pending` and `stop_new`; store equity snapshots once per minute and roll daily
      and monthly baselines on broker-server boundaries.

### 6d. Startup recovery (nine steps, in the order given in the design record)

- [x] `Engine.start()` performs demo/startup guards, records the settings version, loads safety flags,
      reads positions, resolves unresolved intents, reconciles, and checks every position has a stop-loss.
- [x] Check and create current day/month baselines from post-reconciliation equity; record and alert
      `baseline_late` when a baseline is missing or its period was missed. Run management once before
      returning from startup. Startup failures halt and alert.
- [x] **Tests (`tests/test_engine.py`):** deterministic multi-session `SimBroker` / `SimClock` acceptance
      covers Friday flatten, a news window, rollover, daily-limit trip with protected-position retention,
      monthly-cap flatten, and same-database restart without duplicate decisions.

**Done when:** a week of replayed candles runs on `SimBroker` with no unexpected errors and no trade
without a stop-loss.

**Progress note:** the pure evaluator, dependency reuse, stored-stop cooldown calculation, and
SDLC-ordered skip handling are checked off above. The default engine context now reads account and
market state, open positions, unresolved intents, protective-stop risk, realized daily P&L, and
current margin through the gateway/store. Candidate margin is queried for the sized order before the
final risk decision; unavailable margin data fails closed as `NO_MARGIN`. Tests cover context
construction, candidate-margin limits, evaluator and execution skip reasons, and gate/risk precedence.
The entry lane refreshes this context for each new symbol/timeframe and overlays same-pass accepted
or unresolved risk. Periodic management scheduling is wired by the runtime loop; full replayed
day/week acceptance remains open. Startup now detects missing or stale daily/monthly baselines, records `baseline_late`,
and initializes from post-reconciliation equity. Unexpected entry-lane exceptions set `halted_error`,
enqueue a critical alert, and leave management callable. A same-database midday restart test verifies
position reconciliation and that persisted candle progress prevents duplicate decisions. The lane
also derives rollover and weekend-close timestamps from the broker server offset and configured UTC
schedule.

---

## 7. Unify replay with the engine (parity)

`bot/app/backtest.py` is currently a separate Strategy A loop: single symbol, no Friday flatten, no
Strategy B, no news/session gates. The design requires one fill model and one decision path.

- [x] Build `bot/app/replay_runner.py`: feed cached closed candles for all enabled symbols in time
      order into the real `Engine` + `Executor` + `SimBroker` with a `SimClock` and a temporary
      `StateStore`. Candle open times drive `SimClock`; `SimBroker.process_candle` applies stop/target fills
      (stop-first on a dual hit).
- [x] Include: Friday flatten, rollover and news gates (calendar file may be empty for history: then
      gate off via an explicit `--no-news` flag that is recorded in the report), Strategy B and its time
      stop, the 6-candle cooldown, currency exposure and daily/monthly limits. The legacy parity fixture
      excludes news and schedule/rollover edge cases; those gates remain active in normal replay.
- [x] Keep the 12-month holdout rule: refuse the last 12 months unless `--final` (the lock-file part is
      in task 12).
- [x] Parity test: for a fixed fixture, `replay_runner` and the old `run_strategy_a_backtest` give
      the same Strategy A entries with news disabled; the test measures side, entry time and price and
      requires 100% (above the 95% minimum).
- [x] Update `tools/replay.py` to call the runner (multi-symbol, `--strategy a|b|both`).

**Implemented:** one command loads all enabled-symbol H1/H4 caches and writes decisions, trades,
equity curve, and summary under `data/reports/`. A useful full-portfolio run still requires reviewed
broker UTC schedules and complete, validated caches; these are not guessed or generated by replay.

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

- [x] **`telegram.py`** (plain thread + `requests`): long-poll `getUpdates`; delete any webhook at
      startup; accept only `settings.control.telegram_chat_id`; ignore edited messages and group chats;
      ignore commands older than `command_max_age_s` (60) with a reply saying so; persist the last update
      ID via the store; backoff 1, 2, 4 s up to 30 on network failure; alert on a 409 conflict; compare the
      laptop clock with the reply's `Date` header and alert if off by more than 5 s.
- [x] **Alert sender:** `send_alert(severity, text)`: critical never suppressed, warnings with identical
      text merged within 10 minutes, info as it happens; on failure write to the outbox and retry
      (`pending_alerts`, `mark_alert_sent` exist).
- [x] **`control.py`** commands: `/status`, `/stop_new`, `/resume`, `/close_all`.
  - `/resume` clears only `stop_new` and refuses while `halted_daily`, `halted_monthly` or
    `halted_error` is set.
  - `/close_all` needs the reply `YES` within 30 s; then set `stop_new` and `close_all_pending`, take the
    gateway lock (5 s timeout, retry), close every bot position through the executor as close intents,
    verify, retry every 3 s; if the market is closed, say the stop-losses remain and keep
    `close_all_pending` set. Closes only positions with the bot's magic number.
  - The control thread never imports MetaTrader; it acts through gateway, executor and store.
- [x] **`heartbeat.py`:** ping `HEALTHCHECK_URL` every 60 s from its own thread; never crash the bot on
      failure.
- [x] **Tests (`tests/test_control.py` and `tests/test_telegram.py`):** fake Telegram HTTP server (or an injected fake session) plus
      `SimBroker`: stale command, wrong chat ID, command replay after restart, `/close_all` while the lock
      is held 20 s, partial failure, market closed, `/close_all` racing an in-flight entry (the send gate
      must stop it), `/resume` refused during a halt.

**Progress note:** Telegram polling/filtering, cursor persistence, expiry replies, retry handling,
409/clock-drift alerts, alert-outbox delivery, safety commands, close-all lock handoff and 3-second
retry coordination, and a stoppable heartbeat thread have focused fake-session/simulator tests.
The production run command starts and stops the control and heartbeat threads; real-phone demo
acceptance remains owner work.

**Done when:** tests pass and a real `/status` from your phone gets an answer from the demo setup.

---

## 10. `bot/main.py`, runtime wiring and operations

- [x] Add commands to `bot.main`: `run` (the bot), keep `validate`. `run` initializes logging and
      settings, acquires **single-instance file lock `runtime/bot.lock`**, initializes the store and records
      the crash-loop start, loads secrets, connects the demo `MT5Broker` through `BrokerGateway`, runs
      `startup_guard` and engine recovery, then starts control/heartbeat threads and engine loops.
- [x] Startup report: list any enabled symbol that can never be traded at current stop distances and
      balance (sizing returns `BELOW_MIN_LOT`).
- [x] Clean shutdown on Ctrl+C or a service stop: set the clean-shutdown marker, stop threads, release
      the lock, leave trades open.
- [x] **`tools/backup.py`:** copy the SQLite DB into `backups/` once a day (use SQLite's backup API, not
      a file copy while it is open); keep 14 days.
- [x] **`scripts/install_task.ps1`:** registers a Windows Task Scheduler task that starts
      `python -m bot.main run` at logon with the venv interpreter and working directory set. Document, do
      not automate silently: Windows auto-logon, sleep set to Never, active hours set to block automatic
      restarts for updates during the trading week. Script and settings are documented in the runbook;
      Task Scheduler and laptop settings remain untested.
- [x] **`docs/runbook.md`:** the manual backup route (stop the bot first, close the bot's positions in
      the MetaTrader mobile app, check the Trade tab is empty, `/resume` once back) and the healthchecks.io
      email alert path.
- [x] **Tests:** second-instance lock refusal, crash-loop guard sets `stop_new`, clean shutdown leaves
      positions untouched.

**Progress note:** `bot.main run` now wires the file lock, crash-loop guard, secrets, demo-only MT5
connection, engine startup/recovery, Telegram and heartbeat threads, and periodic engine lanes.
Startup sizing diagnostics, shutdown signal handling, protected-position preservation, and lock
refusal have focused tests. SQLite backup/retention and the Task Scheduler installer/runbook are
implemented. Real demo restart acceptance and Windows Task Scheduler/laptop verification remain
owner tasks; the runtime must not be treated as production-ready until those pass.

**Done when:** you can start the bot, see it connect to the demo account, kill it, restart it, and watch
it recover without duplicates.

---

## 11. Failure drills (automated, then manual)

- [x] **Automated (SimBroker only):** dropped responses, rejected stops, stale ticks, wrong fill mode,
      duplicate candle, replayed Telegram command, crash right after send. Covered by focused cases in
      `tests/test_executor.py`, `tests/test_engine.py`, `tests/test_broker.py`, `tests/test_data.py`, and
      `tests/test_telegram.py`; order-outcome cases assert one protected position or none.
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

- [x] Bootstrap confidence intervals: 10,000 resamples of whole calendar weeks.
- [x] Mark-to-market drawdown at every candle close.
- [ ] Random-entry control: `tools/replay.py --random-control` now implements the 1,000-run
      harness, matching per-instrument counts and applying the engine gates, cooldown, position limits
      and strategy-specific exits. The 1,000-run acceptance result remains pending reviewed schedules
      and validated caches.
- [x] Cost stress: rerun with spreads and slippage doubled (`tools/replay.py --cost-multiplier 2`)
      and compare matching reports with `tools/stats.py --cost-stress-report`.
- [x] Per-instrument, per-timeframe and combined A+B reports (`portfolio_breakdown.csv` records
      symbol/timeframe strategy groups, instrument-timeframe totals, and the overall combined total).
- [x] Holdout lock: the last 12 months load only with `--final`, and the run writes a lock file holding
      the code version so it can be run once.
- [x] Variant counter: record unique tried variants in the persistent JSON ledger, rejecting duplicate
      IDs.
- [ ] Sensitivity check: rerun each strategy with each fixed value moved about 20% (construct
      `StrategyAParams` / `StrategyBParams` directly); a `--sensitivity` replay mode now produces
      per-variant reports and an index, but acceptance runs still require reviewed schedules and caches.
- [x] **Scorecard** file: each available measure, value, interval, threshold, and PASS / FAIL /
      INCONCLUSIVE, plus date ranges and versions. Missing cost-drift, parity, and restart evidence is
      explicitly INCONCLUSIVE; thresholds are taken from the task/design record, not invented.

**Progress note:** `tools/stats.py` writes candle-close drawdown metrics, calendar-week bootstrap
confidence intervals, and a versioned/date-ranged scorecard, including variant counts, news-gate
blocked signal counts, and scheduled-news trade overlap outcomes when a replay has a valid news
calendar snapshot. The replay cost multiplier supports
doubled spreads and slippage; the scorecard compares only matching baseline/stress reports. The
one-time `--final` lock is exclusive and records the code version. Replay reports now include trade
attribution and per-instrument/timeframe plus combined A+B totals. Variant trials can be recorded in
`data/reports/variants.json`. `tools/replay.py --random-control` now generates random time/side
controls, reruns through the engine, retries to match baseline instrument counts, and compares mean
closed-trade R expectancy with the random 95th percentile. The 1,000-run acceptance result remains
blocked until the owner applies reviewed schedules and supplies validated caches.
`tools/replay.py --sensitivity` now varies each numeric A/B strategy
parameter by about +/-20%, passes the parameter objects directly into the engine-backed replay,
writes variant summaries, and records completed runs. The sensitivity acceptance runs remain open
until run against reviewed schedules and validated caches. Neither control result should be inferred
from the current scorecard.

---

## 13. Suggested order and effort

| Order | Tasks                   | Why this order                                        |
| ----- | ----------------------- | ----------------------------------------------------- |
| 1     | 0                       | Cheap, protects the layering rules                    |
| 2     | 1, 2                    | Gates cannot work without schedules and store queries |
| 3     | 3, 4                    | Real broker access and symbol hours                   |
| 4     | 5                       | Safe order handling before any engine                 |
| 5     | 6 (6a, 6c, 6d, then 6b) | Management lane and recovery before entries           |
| 6     | 7, 8                    | Replay and news share the engine                      |
| 7     | 9, 10                   | Phone control and running as a service                |
| 8     | 11                      | Drills on the demo                                    |
| 9     | 12                      | Runs in parallel with the demo window                 |

Safety parts (tasks 4, 5, 6, 9, 10) are never cut. Strategy B wiring slips first. Today is 3 Nov and the plan
target is 9 Nov; the remaining work is large (the original estimate was about 70 hours total), so tell
the AI at the start of each task how many hours you have and cut per the plan's order, not the safety work.

## 14. Questions only you can answer

1. **Server-offset method** (task 3.3): is deriving it from a fresh tick's server timestamp acceptable, given the current code comment?
2. **`hours_utc` format** (task 1.1): the `"mon 00:00-21:55"` style is a proposal; change it if you prefer another.
3. **Friday flatten time** comes from each symbol's last Friday session end plus `flatten_before_weekend_close_min`; confirm that matches your intent.
4. **Strategy A result:** profit factor 0.935 is below 1. The plan says don't tune on it. Bring `strategy_a.py` back for the planned review for bugs, before the counted backtests.
