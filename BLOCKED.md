# Blocked verification

## Task 3: MT5 demo-terminal verification

- The MT5 adapter is implemented and tested against an injected fake API; no real terminal was
  connected and no orders were placed.
- Raw server timestamps use the confirmed New York DST rule plus seven hours (UTC+2 in US winter,
  UTC+3 in US summer). Advancing tick samples are sanity checks only; a mismatch sets `stop_new` and
  queues a critical alert without stopping position management.
- Still required on the owner's demo terminal: verify `account()`, `positions()` and `candles()`,
  then visually compare the displayed server clock with the rule on an open-market weekday. No real
  terminal or orders have been used.

## Task 4: Symbol schedule acceptance

- The validator and `--write` path are implemented and tested with `SimBroker`, but the tool has not
  been run against the owner's demo terminal.
- The six enabled symbols still have empty `hours_utc` values in `bot_trader/settings.toml`. No
  broker-derived hours were guessed or written. Run the validator, review its output, then decide
  whether to apply the schedule suggestions. `--write` now persists both the session strings and the
  US season in which they were reviewed; runtime shifts those windows by the New York-rule offset
  difference in the opposite season, including weekday rollover.
- `bot_trader/config/sessions.toml` is an unverified example, not broker-reviewed data. The owner
  explicitly directed that it remain unused. Obtain schedules with `validate_symbols.py` on the
  owner's demo terminal and have the owner review them before applying them.

## Task 8: News calendar owner acceptance

- Startup validation, fail-closed entry gating, one-time stale-calendar alerting, and the under-10-day
  coverage warning are implemented and tested with synthetic test events.
- The checked-in `bot_trader/config/news.csv` has not been verified against a real economic calendar.
  The owner must replace/confirm its events in UTC and set a recurring Sunday reminder to extend it.

## Tasks 6 and 10: Runtime acceptance

- The demo-only `bot.main run` path now wires the Windows file lock, crash-loop guard, MT5 broker,
  gateway, startup guard and recovery, periodic management/entry lanes, optional Telegram/heartbeat
  threads, sizing diagnostics, shutdown signals, and cleanup. Focused `SimBroker` tests cover the
  lock, crash-loop flag, and clean shutdown preserving a protected position.
- The runtime has not been run against the owner's MT5 demo terminal. Do not infer demo account
  compatibility, restart recovery, phone control, or order safety from simulator results.
- Enabled symbols in `bot_trader/settings.toml` still have empty `hours_utc`; runtime refuses to start
  until the owner applies reviewed broker-derived schedules. The news calendar also remains
  owner-verified data.
- The Task Scheduler installer and manual Windows settings are documented, but Task Scheduler and
  laptop configuration have not been tested.

## Tasks 7, 11, and 12: Replay/statistics acceptance

- The multi-symbol engine-backed replay runner and fixed-fixture Strategy A parity test are
  implemented. A useful full-portfolio replay still requires reviewed UTC schedules and complete,
  validated caches; no full six-pair portfolio result should be inferred.
- Simulator failure cases are covered across focused tests. Manual restart, connectivity, phone,
  heartbeat/email, and demo-trade drills remain owner tasks.
- `tools/stats.py` computes candle-close drawdown, 10,000-resample calendar-week confidence
  intervals, doubled-cost stress comparisons, news-gate counts and scheduled-news trade overlap
  outcomes (when the replay calendar snapshot is valid), variant counts, and a date/versioned
  scorecard. Replay reports
  contain instrument/timeframe and combined A+B trade totals. The `--final` replay lock is exclusive
  and records the code version. The variant ledger records unique trial IDs.
  `tools/replay.py --random-control` implements random time/side candidates with strategy exits and
  retries to match per-instrument baseline counts; the required 1,000-run acceptance result cannot
  be produced until reviewed schedules and validated caches are available.
  `tools/replay.py --sensitivity` runs +/-20 percent numeric-parameter
  variants and records result indexes, but acceptance runs remain blocked until the owner applies
  reviewed schedules and supplies validated caches. Absent scorecard evidence is `INCONCLUSIVE`.

## Git workflow

- The parent workspace is a Git repository. The task changes were already uncommitted when this
  continuation began; the working tree is now on `impl/s05-executor-engine`.
- The prior sections' dirty changes could not be split into their requested per-task branches and
  commits without staging/committing an unsplit set of accumulated work. No commits were created.
