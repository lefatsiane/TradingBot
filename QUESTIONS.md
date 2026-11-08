# Confirmed decisions and remaining owner actions

## Confirmed decisions

- Strategy A requires 300 closed candles before proposing an entry.
- Convert broker wall-clock timestamps using the deterministic New York DST rule plus seven hours:
  UTC+2 in US winter and UTC+3 in US summer. Tick samples are sanity checks only. Sample advancing
  ticks, round their server-time offset to the nearest half-hour, and compare with the rule; on a
  mismatch, set `stop_new` and send a critical alert. The owner will visually cross-check the MT5
  server clock on an open-market weekday.
- Sessions use weekday strings such as `"mon 00:00-21:55"` in UTC, with exclusive end times. Split
  midnight-crossing schedules into daily windows. `24:00` is valid only as an end time and means the
  exclusive midnight boundary on the following day. Tag each reviewed schedule as `US winter` or
  `US summer`; shift it automatically by the New York-rule offset difference in the opposite season,
  including weekday rollover.
- Friday flatten is based on the reviewed session's close minus
  `flatten_before_weekend_close_min`. The no-entry window starts earlier; if flattening does not
  complete, alert, retry, and block new entries until bot positions are flat.
- Cooldown is measured in closed bars on the active timeframe. Realized profits do not restore daily
  risk headroom. Approved risk caps remain unchanged.

## Remaining owner actions

- Run `validate_symbols.py` on the owner's demo terminal and review the broker-derived schedules
  before applying them to `bot_trader/settings.toml`. Keep enabled-symbol schedules empty until then;
  do not use the unverified `bot_trader/config/sessions.toml` example.
- Supply and verify a real economic-calendar source/path and UTC events. Do not treat the checked-in
  sample calendar as acceptance data.
- Complete the MT5 demo-terminal clock cross-check and remaining demo runtime/restart/control drills.
- Run random-entry and sensitivity acceptance only after the schedules and real news data are
  available; no live orders or unreviewed schedule assumptions are permitted.

## Later question

- Consider a separate verified-through date for quiet periods.
