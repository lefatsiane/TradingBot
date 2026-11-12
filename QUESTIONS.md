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

- Run `validate_symbols.py` on the owner's demo terminal and review the H1/time-offset report before
  applying the configured server-time schedule to `bot_trader/settings.toml`. Keep enabled-symbol
  UTC schedules empty until review; the tool does not read sessions from MT5.
- Supply and verify a real economic-calendar source/path and UTC events. Do not treat the checked-in
  sample calendar as acceptance data.
- Complete the MT5 demo-terminal clock cross-check and remaining demo runtime/restart/control drills.
- Run random-entry and sensitivity acceptance only after the schedules and real news data are
  available; no live orders or unreviewed schedule assumptions are permitted.

## Later question

- Consider a separate verified-through date for quiet periods.
- Should missing expected H1 candle slots on broker-declared market holidays be treated as
  validation failures, or reported separately from unexpected data gaps?
- Does the existing five-day stale-history threshold mean five accumulated days of scheduled
  market-open time? The validator currently uses that interpretation for failure and reports the
  elapsed age of the last closed H1 candle separately.
- How should the repeated server-clock H1 timestamp during the autumn DST rollback be resolved?
  The wall-clock hour occurs twice and a raw server-wall timestamp alone cannot identify which UTC
  hour it represents; confirm whether MT5 returns an additional disambiguating field or omits one
  of the repeated bars.
- `config/known_gaps.csv` contains the owner-reported single-candle DRAFT rows. They remain
  unexpected until both approval fields are populated by the owner. Approved two-candle exceptions
  remain limited to Tuesday through Thursday.
- `config/broker_holidays.csv` contains DRAFT date windows from the owner-reported common holiday
  gaps. The listed bounds are date-level placeholders; confirm exact UTC gap bounds against the
  complete report and IC Markets' holiday notice before approving them. The cache accepts no
  holiday window while either approval field is empty.
- `--report-gaps` was run by the owner and reported 229 gaps. The implementation now groups report
  output by holiday window, clock-change weekend, known single gap, and unexpected; do not infer
  that any DRAFT gap is accepted.
- The default cache start is `2021-12-03T00:00:00+00:00`, excluding the 2 December 2021 broker
  outage. From that start through 2026-11-11 is about 4.939 years against the six-year design
  target (about 1.061 years short); actual cache manifests calculate coverage from their first and
  last candles.
- Before the next year-end, the live bot must treat broker holiday closures as market-closed:
  no stale-data halts and no trading during the closure. Confirm the broker's exact holiday trading
  hours against IC Markets' notice before enabling that behavior.
