# Operations runbook

> **Not ready for unattended operation.** The `run` command and runtime wiring are
> implemented, but no real MT5 terminal, Telegram phone, or scheduled task has
> been verified. The enabled-symbol UTC schedules and owner-verified news data
> are also prerequisites. Complete the demo verification in `../BLOCKED.md`
> before considering unattended operation.

## Weekly manual news-calendar update

The economic calendar is entered and reviewed by hand. No tool in this project
fetches or scrapes an external calendar website. Work from the `bot_trader`
project directory.

1. If Investing.com offers the setting, set its calendar timezone to GMT. Filter
   to the currencies and importance levels you want to monitor.
2. Create the template once, then copy it to the ignored input filename. Replace
   both example rows with manually selected calendar events. Do not copy the
   template over an existing filled input file:

   ```powershell
   python -m tools.news_import template
   if (-not (Test-Path config\news_input.csv)) {
       Copy-Item config\news_input.template.csv config\news_input.csv
   }
   ```

   The template and filled input file are never loaded by the bot. Leave out
   all-day and tentative events. Enter dates as `YYYY-MM-DD` and times as
   `HH:MM`. Explicitly state the timezone used for those times; for a GMT
   calendar, use the default `UTC`, otherwise pass the matching IANA timezone,
   for example `Africa/Johannesburg`.
3. Check and preview the input. The preview is converted to UTC and writes
   nothing:

   ```powershell
   python -m tools.news_import check config\news_input.csv --input-timezone UTC
   python -m tools.news_import preview config\news_input.csv --input-timezone UTC
   ```

4. Merge reviewed rows into the candidate calendar. The default keeps high and
   medium impact; specify another set when appropriate. Review the candidate
   before promotion:

   ```powershell
   python -m tools.news_import merge config\news_input.csv --input-timezone UTC
   Get-Content config\news.candidate.csv
   python -m tools.news_import coverage
   ```

5. Promote only after reviewing the printed added/removed row diff. Promotion
   requires typing `yes`, validates the resulting calendar, and backs up the old
   approved file under the ignored `backups\news-calendar` folder:

   ```powershell
   python -m tools.news_import promote
   python -m tools.news_import coverage
   ```

   The approved runtime calendar remains `config\news.csv`; do not replace it
   manually. If validation or coverage fails, resolve the candidate and repeat
   the review before promoting.

## Manual backup

1. Stop the bot cleanly. Do not close the process while an order outcome is
   uncertain.
2. In MetaTrader mobile, close only positions carrying this bot's magic number.
3. Check the Trade tab and confirm no bot positions remain.
4. Back up the state database with SQLite's backup API:

   ```powershell
   python tools/backup.py --database path\to\bot.sqlite3 --backup-dir backups
   ```

   The tool writes one date-named backup and removes backups older than 14 days.
5. Before any future restart, reconcile the database with the broker positions
   and deals. Once the Telegram control path is configured and the recovered
   market/account state is verified, send `/resume` only when no daily, monthly,
   or error halt is active.

## Healthcheck email alerts

When configured, the runtime starts the heartbeat adapter. Provide the
healthchecks.io ping URL through the `HEALTHCHECK_URL` environment variable
only. Configure the healthcheck's missed ping alert to email the owner, and
verify delivery with the manual drill in [`drills.md`](./drills.md). Never put
the URL in settings, logs, screenshots, or source control.

## Windows scheduled start

The installer is [`scripts/install_task.ps1`](../scripts/install_task.ps1). It
registers `python -m bot.main run` with the project's `.venv\Scripts\python.exe`,
the project working directory, and an at-logon trigger. Review its parameters
and confirm the registration prompt; use `-WhatIf` to preview. The script has
not been tested with Windows Task Scheduler.

Before enabling an unattended start:

1. Review and apply broker-derived UTC schedules; `run` refuses to start while
   an enabled symbol has an empty or invalid schedule.
2. Verify the demo account, order-free startup, restart recovery, Telegram, and
   every drill in [`drills.md`](./drills.md).
3. If Windows automatic logon is required, understand that it exposes the
   account session to anyone with physical access. Do not enable it on an
   unsecured device.
4. Configure sleep to **Never** while plugged in and on battery if appropriate
   for the device's role. Configure Windows active hours to prevent automatic
   restarts during the trading week, and keep updates enabled outside those
   hours.
5. Confirm the scheduled task runs under the intended user and that its
   environment variables are available to that user. The scheduled task must
   never be used with a live account.
