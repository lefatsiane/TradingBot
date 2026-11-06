# Prompt for the VS Code AI (paste this whole file into the chat)

You are implementing the remaining work on my forex trading bot (Python 3, MetaTrader 5, IC Markets US$3,000 Raw Spread DEMO account). The repo is `TradingBot`; the Python project is in `bot_trader/`. The full task list is `IMPLEMENTATION_TASKS.md` in the repo root (77 tasks, sections 0-14). Read these first, completely, before writing any code:

1. `IMPLEMENTATION_TASKS.md` (your work order)
2. `BOT_BLUEPRINT.md` and `README.md`
3. The SDLC record, if it is in the repo (requirements F1-F26, design decisions)
4. `bot_trader/settings.toml`
5. Existing code in `bot/core/`, `bot/adapters/`, `bot/app/`, and the tests

Then reply with a short summary (max 15 lines) of what you understood and the order you will work in. Do not start coding until you have posted it. After that, keep going without asking me for permission on each step.

## Your goal

Do as much of `IMPLEMENTATION_TASKS.md` as you can, in the order of the "order table" (section 13), one task at a time, with tests. Anything that needs something only I can provide (see "Things you cannot do") gets built against a fake or a stub and listed in `BLOCKED.md` for me.

## Hard rules (never break these)

1. **Demo only.** Never add a code path that can trade a live account. Keep the account-mode check that refuses anything but a demo account.
2. **No secrets in the repo.** Tokens, passwords, MT5 login and emails are read from environment variables only. Never write a real value in code, tests, settings, logs, comments, commit messages or examples. Use placeholders such as `YOUR_TOKEN_HERE` in docs. Make sure `.env` and the database and log folders are in `.gitignore`.
3. **Do not change strategy rules or risk values.** Strategy A and B parameters, 0.5% risk per trade, 1% daily and 3% monthly limits, 3 trades, 1.0% net currency exposure, the 15% cost gate and `$3.50/lot/side` stay exactly as they are. If a task seems to require changing one, stop and put it in `QUESTIONS.md`.
4. **Architecture boundaries.** `bot/core/` is pure logic: no `MetaTrader5`, `sqlite3`, `requests`, file access or wall-clock calls (time is passed in). Only `bot/adapters/mt5_broker.py` may import `MetaTrader5`. Only `bot/adapters/store.py` may touch SQLite. Implement task 0.1 (the architecture test) first and keep it green.
5. **Never guess a decision.** The six open decisions in section 14 (cooldown unit, server-offset method, `hours_utc` format, Friday flatten time, whether profit credits headroom, whether limits may be raised) are mine. For each, implement the safest reading, make it a clearly named constant or setting, add a `# DECISION-PENDING` comment, and add the question to `QUESTIONS.md`. Do not silently pick one.
6. **Safety first on orders.** A position never exists without a stop-loss. Use the intent journal (PENDING, SENT, CONFIRMED/FAILED/UNKNOWN) and write the intent before sending the order. After any uncertain result, reconcile with the broker before doing anything else. One bot instance only (single-instance lock).
7. **Tests with everything.** Every task gets tests in the same change. Short side mirrors long side. Use the simulator (`sim_broker` with `FaultPlan`) for failure cases. Do not weaken, delete or skip an existing test to make something pass. If an existing test seems wrong, stop and tell me.
8. **Smallest change that satisfies the task.** No refactors, renames or new dependencies unless the task says so. If you need a new package, add it to the requirements file and say so in the commit message.

## How to work

- Create one git branch per section: `impl/s01-schedule`, `impl/s02-store`, and so on. One commit per task, message format `T3.2: short description`. Do not commit to `main`. Do not push; I will push and open pull requests myself.
- Before each task: re-read its entry, name the files you will touch, then write the tests first (they should fail), then the code.
- After each task run `ruff check .` and `pytest -q` from `bot_trader/` (MetaTrader5 tests are skipped or faked on non-Windows; do not delete them). Only move on when both are clean. If something stays red after two honest attempts, stop that task, record it in `BLOCKED.md` with the exact error, and continue with the next task that does not depend on it.
- Update the task's checkbox in `IMPLEMENTATION_TASKS.md` when it is done, tests included. Do not tick a task that is only partly done; write what is missing next to it.
- Keep `README.md` and the blueprint in step with what you build (layout, commands, settings names). If the code and the SDLC record disagree, tell me in `QUESTIONS.md`; do not edit the record.

## Review stops (mandatory)

Stop and wait for me after finishing each of these points. Post a summary of what changed, the test results and anything in `QUESTIONS.md` or `BLOCKED.md`:

- **Stop A:** after sections 0-2 (housekeeping, schedule helpers, store schema v3)
- **Stop B:** after sections 3-6 (live MT5 broker, symbol validation, executor and reconcile, engine)
- **Stop C:** after sections 7-10 (replay parity, news wiring, Telegram and control, main and runtime)

Sections 11-12 (drills, statistics harness) can run after Stop C, with a final summary at the end.

## Things you cannot do (build against fakes, record in BLOCKED.md)

- Connect to the real MT5 terminal or place real or demo orders. Write the live broker code against the broker protocol and the simulator, and mark it "needs demo verification".
- Create the real news calendar (`config/news.csv` must have real UTC events from me). Build the validator and loader; use clearly fake sample events in tests only.
- Create Telegram or healthchecks.io accounts, or use real tokens. Use fake HTTP clients in tests.
- Set up Windows Task Scheduler or the laptop. Write the script or XML and the instructions, marked "untested on Windows".
- Run anything against real money. Never.

## Finish

When you run out of tasks you can safely do, give me: (1) a table of every task as DONE / PARTIAL / BLOCKED / DECISION-PENDING, (2) the contents of `QUESTIONS.md`, (3) the contents of `BLOCKED.md`, (4) the exact commands I should run on my Windows laptop to verify everything, and (5) the list of branches to merge, in order.

Start now with the summary of what you understood.
