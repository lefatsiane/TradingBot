"""
connection_test_v2.py - read-only connection test for the IC Markets MetaTrader 5 DEMO account.

What changed from v1
  - History depth is read one year at a time. v1 asked for 100,000 bars in one go and got 0.
  - Results are percentages of your balance, so they hold for any account size.
  - The balance is saved in the CSV, so the results can be read on their own.
  - Spreads are sampled 10 times from live prices, and prices that are not moving are flagged.
  - Only forex, metals and spot indices are tested (no stocks or futures).

What it does
  1. Connects to your MetaTrader 5 terminal (already logged in to the DEMO account).
  2. Refuses to continue unless the account is a demo account.
  3. Reports account details and the broker server time offset.
  4. For each symbol reports: spread, smallest trade size, cost of the smallest trade
     as a % of your balance, the balance you would need for it to fit your risk limit,
     and how many months of M15, H1 and H4 history are available.
  5. Saves everything to connection_test_results.csv.

It NEVER places, changes or closes an order.

How to run (Windows, with MetaTrader 5 open and logged in to your demo account)
  pip install MetaTrader5 numpy
  python connection_test_v2.py

Run it on a weekday while the market is open (London or New York hours are best).
It can take a few minutes, because MetaTrader 5 may need to download history first.
"""

import csv
import sys
import time
from datetime import datetime, timedelta, timezone

import numpy as np

try:
    import MetaTrader5 as mt5
except ImportError:
    sys.exit("The MetaTrader5 package is missing. Run: pip install MetaTrader5 numpy")

# ---- Settings (these will move into the bot's settings file later) ----------
RISK_PER_TRADE_PCT = 0.5          # per-trade risk limit, as % of the account balance
STOP_ATR_MULTIPLE = 1.5           # assumed stop-loss distance = 1.5 x H1 ATR(14)
MAX_SPREAD_PCT_OF_STOP = 20.0     # flag a symbol if its spread is above 20% of the stop distance
SPREAD_SAMPLES = 10               # how many times to read each price
SPREAD_SAMPLE_GAP_SECONDS = 2     # pause between rounds of reading
HISTORY_YEARS_BACK = 6            # how far back to look for history
MIN_MONTHS_FOR_BACKTEST = 12      # history needed for a backtest

FOREX_MAJORS = [
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD",
]
METALS = ["XAUUSD"]
# Symbols whose path starts with this are treated as spot indices.
INDEX_PATH_PREFIX = "indices\\indices spot"
# Anything with one of these words in its path is skipped.
EXCLUDED_PATH_WORDS = ("crypto", "stock", "futures")

TIMEFRAMES = {
    "M15": mt5.TIMEFRAME_M15,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
}
# -----------------------------------------------------------------------------


def norm_path(symbol):
    return (symbol.path or "").replace("/", "\\").lower()


def find_candidates():
    """Return (symbols to test, expected symbols that were not found)."""
    all_symbols = mt5.symbols_get() or []
    by_name = {s.name: s for s in all_symbols}
    wanted = [n for n in FOREX_MAJORS + METALS if n in by_name]

    for s in all_symbols:
        if s.name in wanted:
            continue
        path = norm_path(s)
        if any(word in path for word in EXCLUDED_PATH_WORDS):
            continue
        if path.startswith(INDEX_PATH_PREFIX):
            wanted.append(s.name)

    missing = [n for n in FOREX_MAJORS + METALS if n not in by_name]
    return wanted, missing


def count_history(symbol, timeframe):
    """Read history one year at a time. Returns (bar_count, earliest_bar_datetime or None)."""
    now = datetime.now(timezone.utc)
    total = 0
    earliest = None
    empty_in_a_row = 0
    for years_back in range(HISTORY_YEARS_BACK):
        end = now - timedelta(days=365 * years_back)
        start = end - timedelta(days=365)
        attempts = 3 if years_back == 0 else 1   # recent data may need a moment to download
        rates = None
        for _ in range(attempts):
            rates = mt5.copy_rates_range(symbol, timeframe, start, end)
            if rates is not None and len(rates) > 0:
                break
            time.sleep(1)
        if rates is None or len(rates) == 0:
            empty_in_a_row += 1
            if empty_in_a_row >= 2:
                break
            continue
        empty_in_a_row = 0
        total += len(rates)
        first = datetime.fromtimestamp(int(rates[0]["time"]), tz=timezone.utc)
        if earliest is None or first < earliest:
            earliest = first
    return total, earliest


def months_between(earliest):
    if earliest is None:
        return 0.0
    return round((datetime.now(timezone.utc) - earliest).days / 30.4, 1)


def h1_atr(symbol, period=14):
    """Average true range on H1 over the last ~200 bars, in price units."""
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, 200)
    if rates is None or len(rates) < period + 2:
        return None
    high = rates["high"]
    low = rates["low"]
    close = rates["close"]
    prev_close = close[:-1]
    tr = np.maximum.reduce([
        high[1:] - low[1:],
        np.abs(high[1:] - prev_close),
        np.abs(low[1:] - prev_close),
    ])
    return float(np.mean(tr[-period:]))


def sample_prices(names, infos):
    """Read prices several times. Returns spreads in points, last tick times and distinct tick counts."""
    spreads = {n: [] for n in names}
    tick_times = {n: set() for n in names}
    for i in range(SPREAD_SAMPLES):
        for n in names:
            tick = mt5.symbol_info_tick(n)
            if tick is None or tick.ask <= 0 or tick.bid <= 0:
                continue
            point = infos[n].point
            spreads[n].append((tick.ask - tick.bid) / point)
            tick_times[n].add(tick.time)
        if i < SPREAD_SAMPLES - 1:
            time.sleep(SPREAD_SAMPLE_GAP_SECONDS)
    return spreads, tick_times


def fmt(value, decimals=2):
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{decimals}f}"
    return str(value)


def main():
    if not mt5.initialize():
        sys.exit(f"Could not connect to MetaTrader 5: {mt5.last_error()}. "
                 "Open the terminal, log in to your demo account and try again.")

    account = mt5.account_info()
    terminal = mt5.terminal_info()
    if account is None:
        mt5.shutdown()
        sys.exit("No account found. Log in to your demo account in MetaTrader 5.")

    if account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
        mt5.shutdown()
        sys.exit("STOP: this is not a demo account. This test only runs on a demo account.")

    if account.balance <= 0:
        mt5.shutdown()
        sys.exit("The account balance is zero, so percentages cannot be worked out.")

    print("=== Account ===")
    print(f"Server:   {account.server}")
    print(f"Currency: {account.currency}")
    print(f"Balance:  {account.balance:,.2f}")
    print(f"Leverage: 1:{account.leverage}")
    print(f"Algo trading allowed in terminal: {terminal.trade_allowed}")
    print(f"Connected to broker: {terminal.connected}")
    print(f"Per-trade risk limit: {RISK_PER_TRADE_PCT}% of balance")

    candidates, missing = find_candidates()
    if missing:
        print("\nNot found on this server (names may differ): " + ", ".join(missing))

    infos = {}
    for name in candidates:
        if not mt5.symbol_select(name, True):
            print(f"Could not add {name} to Market Watch, skipping")
            continue
        info = mt5.symbol_info(name)
        if info is not None:
            infos[name] = info
    names = list(infos.keys())

    print(f"\nReading prices for {len(names)} symbols "
          f"({SPREAD_SAMPLES} readings, about {SPREAD_SAMPLES * SPREAD_SAMPLE_GAP_SECONDS} seconds)...")
    spreads, tick_times = sample_prices(names, infos)

    live_count = sum(1 for n in names if len(tick_times[n]) >= 2)
    market_looks_open = live_count >= max(1, len(names) // 2)
    if not market_looks_open:
        print("WARNING: most prices did not move while reading. The market may be closed or quiet. "
              "Run this again on a weekday during London or New York hours.")

    if market_looks_open:
        freshest = max((max(t) for t in tick_times.values() if t), default=None)
        if freshest is not None:
            offset_hours = round((freshest - time.time()) / 3600.0)
            print(f"Broker server time is about UTC{offset_hours:+d} "
                  "(IC Markets is usually UTC+2 in winter and UTC+3 in summer)")

    rows = []
    print("\nReading history (this can take a few minutes)...")
    for name in names:
        info = infos[name]
        sp = spreads[name]
        spread_median_pts = float(np.median(sp)) if sp else None
        spread_max_pts = float(max(sp)) if sp else None
        spread_median_price = spread_median_pts * info.point if sp else None

        atr = h1_atr(name)
        stop_distance = atr * STOP_ATR_MULTIPLE if atr else None

        tick_value = info.trade_tick_value_loss or info.trade_tick_value
        loss_at_min = loss_pct = min_balance = fits = None
        if stop_distance and info.trade_tick_size and tick_value:
            loss_at_min = stop_distance / info.trade_tick_size * tick_value * info.volume_min
            loss_pct = loss_at_min / account.balance * 100.0
            min_balance = loss_at_min / (RISK_PER_TRADE_PCT / 100.0)
            fits = loss_pct <= RISK_PER_TRADE_PCT

        spread_pct_of_stop = spread_ok = None
        if stop_distance and spread_median_price is not None:
            spread_pct_of_stop = spread_median_price / stop_distance * 100.0
            spread_ok = spread_pct_of_stop <= MAX_SPREAD_PCT_OF_STOP

        row = {
            "account_server": account.server,
            "account_currency": account.currency,
            "account_balance": round(account.balance, 2),
            "symbol": name,
            "path": info.path,
            "digits": info.digits,
            "spread_median_points": round(spread_median_pts, 1) if sp else None,
            "spread_max_points": round(spread_max_pts, 1) if sp else None,
            "spread_reported_points": info.spread,
            "prices_moving": len(tick_times[name]) >= 2,
            "volume_min": info.volume_min,
            "volume_step": info.volume_step,
            "volume_max": info.volume_max,
            "contract_size": info.trade_contract_size,
            "stops_level_points": info.trade_stops_level,
            "swap_long": info.swap_long,
            "swap_short": info.swap_short,
            "h1_atr": round(atr, info.digits) if atr else None,
            "assumed_stop_distance": round(stop_distance, info.digits) if stop_distance else None,
            "loss_at_min_lot": round(loss_at_min, 2) if loss_at_min is not None else None,
            "loss_at_min_lot_pct_of_balance": round(loss_pct, 3) if loss_pct is not None else None,
            "min_balance_for_risk_limit": round(min_balance, 0) if min_balance is not None else None,
            "fits_risk_limit": fits,
            "spread_pct_of_stop": round(spread_pct_of_stop, 1) if spread_pct_of_stop is not None else None,
            "spread_ok": spread_ok,
        }
        months_by_tf = {}
        for label, tf in TIMEFRAMES.items():
            bars, earliest = count_history(name, tf)
            months = months_between(earliest)
            months_by_tf[label] = months
            row[f"{label}_bars"] = bars
            row[f"{label}_first_bar"] = earliest.strftime("%Y-%m-%d") if earliest else None
            row[f"{label}_months"] = months
        row["has_enough_history"] = all(m >= MIN_MONTHS_FOR_BACKTEST for m in months_by_tf.values())
        rows.append(row)
        print(f"  {name}: H1 history {months_by_tf['H1']} months")

    print("\n=== Symbols ===")
    header = (f"{'symbol':<10}{'spread':>8}{'min lot':>9}{'risk %':>8}{'min bal':>9}"
              f"{'fits?':>7}{'spr ok?':>9}{'H1 months':>11}")
    print(header)
    print("-" * len(header))
    for r in rows:
        print(f"{r['symbol']:<10}{fmt(r['spread_median_points'], 1):>8}{r['volume_min']:>9}"
              f"{fmt(r['loss_at_min_lot_pct_of_balance'], 2):>8}"
              f"{fmt(r['min_balance_for_risk_limit'], 0):>9}"
              f"{str(r['fits_risk_limit']):>7}{str(r['spread_ok']):>9}{r['H1_months']:>11}")

    not_fit = [r["symbol"] for r in rows if r["fits_risk_limit"] is False]
    short_history = [r["symbol"] for r in rows if not r["has_enough_history"]]
    print(f"\nTested {len(rows)} symbols on a balance of {account.balance:,.2f} {account.currency}.")
    print("Smallest trade is above the risk limit for: " + (", ".join(not_fit) if not_fit else "none"))
    print(f"Less than {MIN_MONTHS_FOR_BACKTEST} months of history for: "
          + (", ".join(short_history) if short_history else "none"))

    if rows:
        with open("connection_test_results.csv", "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print("\nSaved connection_test_results.csv in this folder.")

    mt5.shutdown()


if __name__ == "__main__":
    main()
