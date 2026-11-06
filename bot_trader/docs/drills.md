# Demo failure drills

> **Not yet operationally approved.** The current `bot.main` has no `run` command,
> and Telegram/heartbeat threads are not wired into a production runtime. Do not
> treat this checklist as evidence that the bot is ready to trade.

Run these drills manually only after the owner has completed the demo-terminal
checks in `../BLOCKED.md`. Use the demo account only; never use a live account.
Before each drill, confirm that every bot position has a broker-held stop-loss
and record the current positions and tickets.

## Recovery and connectivity

- [ ] Restart the bot with an open, protected demo position; verify recovery does
  not create a duplicate and the original broker stop remains in place.
- [ ] Disconnect the network, restore it, and verify entries stop while
  management/reconciliation resumes after connectivity returns.
- [ ] Close MetaTrader while the bot is running; reopen it and verify connection
  recovery without duplicate orders.
- [ ] Put the laptop to sleep and wake it; verify stale data blocks entries and
  management recovers only after fresh broker data returns.
- [ ] Move the laptop clock by a small, known amount, then restore it; verify a
  clock-drift alert and that no stale/future tick can authorize an entry.
- [ ] Kill the process immediately after a demo order is sent; restart and verify
  the intent is reconciled before any new send.

## Controls and alerts

- [ ] Request `/close_all` while the market is closed; verify the request remains
  pending, entries remain stopped, and broker-held stop-losses remain in place.
- [ ] Start a second bot instance intentionally; verify it refuses to start.
- [ ] Stop the bot and verify the missed-heartbeat alert reaches both Telegram
  and the configured email route.
- [ ] Send a test critical alert and verify delivery within 60 seconds.
- [ ] After a small demo open/close cycle, compare the broker's charged
  commission with the configured per-side commission and record any drift.

Do not continue a drill if any position is missing its stop-loss or an order
outcome is uncertain. Reconcile manually in the demo terminal before resuming.
