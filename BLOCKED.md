# Blocked verification

## Task 3: MT5 demo-terminal verification

- The MT5 adapter is implemented and tested against an injected fake API; no real terminal was
  connected and no orders were placed.
- Still required on the owner's demo terminal: manually verify `account()`, `positions()` and
  `candles()`, then compare the inferred UTC offset against the displayed server clock during an
  open-market weekday. Until then, do not rely on the inferred offset for trading.

## Task 4: Symbol schedule acceptance

- The validator and `--write` path are implemented and tested with `SimBroker`, but the tool has not
  been run against the owner's demo terminal.
- The six enabled symbols still have empty `hours_utc` values in `bot_trader/settings.toml`. No
  broker-derived hours were guessed or written. Run the validator, review its output, then decide
  whether to apply the schedule suggestions.

## Task 8: News calendar owner acceptance

- Startup validation, fail-closed entry gating, one-time stale-calendar alerting, and the under-10-day
  coverage warning are implemented and tested with synthetic test events.
- The checked-in `bot_trader/config/news.csv` has not been verified against a real economic calendar.
  The owner must replace/confirm its events in UTC and set a recurring Sunday reminder to extend it.

## Tasks 6 and 10: Runtime acceptance

- The engine's entry and management methods have focused simulator tests, but a full replayed week,
  restart mid-day acceptance, periodic runtime loop, and production MT5 wiring are incomplete.
- The normal `bot.main` command remains configuration/schedule validation only. It cannot start a
  trading runtime and must not be used to infer readiness.
- The Telegram/control and heartbeat components, crash-loop helper, SQLite backup utility, and
  runbook/drill documents exist, but none is connected to a production `run` command.

## Tasks 7 and 12: Replay/statistics implementation

- The multi-symbol replay runner, live-engine parity acceptance, portfolio statistics harness, and
  holdout lock are not implemented. A parity or performance result must not be inferred from the
  existing single-symbol Strategy A backtest.
- The empty enabled-symbol schedules in `settings.toml` prevent a meaningful gate-enabled replay
  until the owner reviews and applies broker-derived UTC hours. Do not invent session hours.

## Git workflow

- The parent workspace is a Git repository. The task changes were already uncommitted when this
  continuation began; the working tree is now on `impl/s05-executor-engine`.
- The prior sections' dirty changes could not be split into their requested per-task branches and
  commits without staging/committing an unsplit set of accumulated work. No commits were created.
