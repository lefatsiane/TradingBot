# Questions requiring the owner's decision

1. **MT5 server-offset verification (task 3.3):** Is deriving the broker offset from a fresh EURUSD
   server timestamp acceptable? The adapter follows the task's tick-derived method and checks
   freshness/expected offset, but it still needs a weekday visual cross-check against the MT5 server
   clock before the inferred value is relied on.
2. **`hours_utc` format (task 1.1):** The implementation uses the proposed weekday-window strings
   such as `"mon 00:00-21:55"`. Please confirm this format before applying any broker-derived
   schedules to `settings.toml`.
3. **Friday flatten timing (task 6c):** The engine derives each symbol's Friday close from the
   final Friday `hours_utc` session and subtracts `flatten_before_weekend_close_min`. Please confirm
   that this is the intended close reference.
4. **Strategy A review (task 14):** The 1 November 2026 run had profit factor 0.935. Review
   `strategy_a.py` for implementation bugs before any counted backtests; do not tune parameters to
   improve this result.
5. **Cooldown unit:** The implementation interprets cooldown as a count of closed bars on the
   active strategy timeframe. Confirm that H1/H4 cooldown counts, rather than elapsed time, are
   intended.
6. **Realized-profit headroom:** May realized profits restore daily risk headroom? Until confirmed,
   realized profits do not increase the approved daily headroom; realized losses still reduce it.
7. **Risk-limit increases:** May any approved risk limits be raised above their current values?
   Until confirmed, configuration validation rejects increases to the approved caps.

The prompt's hard rules refer to six pending decisions, but its current section 14 lists only four
items and includes the separate Strategy A review. The three decision questions above fill the
missing cooldown, realized-profit, and risk-limit items.
