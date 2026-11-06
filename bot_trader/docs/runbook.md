# Operations runbook

> **Not ready for unattended operation.** `python -m bot.main run` and production
> runtime wiring are not implemented. Do not start the bot against an account.
> Complete demo verification in `../BLOCKED.md` before using any operational
> procedure below.

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
   and deals. Once the production Telegram control path is available and the
   recovered market/account state is verified, send `/resume` only when no
   daily, monthly, or error halt is active.

## Healthcheck email alerts

The heartbeat adapter is implemented but is not yet started by a production
runtime. After runtime wiring, provide the healthchecks.io ping URL through the
`HEALTHCHECK_URL` environment variable only. Configure the healthcheck's missed
ping alert to email the owner, and verify delivery with the manual drill in
[`drills.md`](./drills.md). Never put the URL in settings, logs, screenshots, or
source control.

## Windows scheduled start

Task Scheduler registration and Windows power/update settings are not yet
implemented or tested. Do not create an unattended scheduled task until a
demo-only `run` command, clean shutdown, restart recovery, and failure drills
have passed.
