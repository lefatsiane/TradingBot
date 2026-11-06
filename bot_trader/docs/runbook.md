# Operations runbook

> **Not ready for unattended operation.** The `run` command and runtime wiring are
> implemented, but no real MT5 terminal, Telegram phone, or scheduled task has
> been verified. The enabled-symbol UTC schedules and owner-verified news data
> are also prerequisites. Complete the demo verification in `../BLOCKED.md`
> before considering unattended operation.

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
