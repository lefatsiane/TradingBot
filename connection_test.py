"""
connection_test.py - read-only connection test for the IC Markets MetaTrader 5 demo account.

What it does
  1. Connects to your MetaTrader 5 terminal (already logged in to the DEMO account).
  2. Refuses to continue unless the account is a demo account.
  3. Reports account details and the broker server time offset.
  4. For each candidate symbol (forex majors, gold, indices) reports:
       spread, smallest trade size, trade size step, history depth (M15, H1, H4)
       and whether the smallest trade fits the per-trade risk limit.
  5. Saves everything to connection_test_results.csv.

It NEVER places, changes or closes an order.

How to run (Windows, with MetaTrader 5 open and logged in to your demo account)
  pip install MetaTrader5 numpy
  python connection_test.py

Run it on a weekday while the market is open, so spreads and server time are real.
"""

import csv
import sys
from datetime import datetime, timezone

import numpy as np

try:
    import MetaTrader5 as mt5
except ImportError:
    sys.exit("The MetaTrader5 package is missing. Run: pip install MetaTrader5 numpy")

# ---- Settings (these will move into the bot's settings file later) ----------
RISK_PER_TRADE_PCT = 0.5          # your per-trade risk limit, as % of the account
STOP_ATR_MULTIPLE = 1.5           # assumed stop-loss distance = 1.5 x H1 ATR(14)
MAX_SPREAD_PCT_OF_STOP = 20.0     # flag a symbol if spread is above 20% of the stop distance

FOREX_MAJORS = [
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD",
]
METALS = ["XAUUSD"]
# Index names differ by broker, so indices are also found through the symbol path.
INDEX_NAME_HINTS = [
    "US500", "US30", "USTEC", "DE40", "UK100", "JP225", "AUS200", "EU50", "F40", "ES35",
]

TIMEFRAMES = {
    "M15": mt5.TIMEFRAME_M15,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
}
MAX_BARS = 100000
# -----------------------------------------------------------------------------


def find_candidates():
    """Return the list of symbol names to test."""
    all_symbols = mt5.symbols_get() or []
    by_name = {s.name: s for s in all_symbols}
    wanted = []

    for name in FOREX_MAJORS + METALS + INDEX_NAME_HINTS:
        if name in by_name:
            wanted.append(name)

    # Also pick up indices by path (e.g. a path containing "Indices"), excluding crypto.
    for s in all_symbols:
        path = (s.path or "").lower()
        if ("indices" in path or "index" in path) and "crypto" not in path:
            if s.name not in wanted:
                wanted.append(s.name)

    missing = [n for n in FOREX_MAJORS + METALS if n not in by_name]
    return wanted, missing


def history_info(symbol, timeframe):
    """Return (bar_count, first_bar_date) for a timeframe, or (0, None)."""
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, MAX_BARS)
    if rates is None or len(rates) == 0:
        return 0, None
    first = datetime.fromtimestamp(int(rates[0]["time"]), tz=timezone.utc).strftime("%Y-%m-%d")
    return len(rates), first


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

    print("=== Account ===")
    print(f"Server:   {account.server}")
    print(f"Login:    {account.login}")
    print(f"Currency: {account.currency}")
    print(f"Balance:  {account.balance:,.2f}")
    print(f"Leverage: 1:{account.leverage}")
    print(f"Algo trading allowed in terminal: {terminal.trade_allowed}")
    print(f"Connected to broker: {terminal.connected}")

    risk_money = account.balance * RISK_PER_TRADE_PCT / 100.0
    print(f"Per-trade risk limit: {RISK_PER_TRADE_PCT}% = {risk_money:,.2f} {account.currency}")

    candidates, missing = find_candidates()
    if missing:
        print("\nNot found on this server (names may differ): " + ", ".join(missing))

    rows = []
    server_offsets = []

    for name in candidates:
        if not mt5.symbol_select(name, True):
            print(f"Could not add {name} to Market Watch, skipping")
            continue
        info = mt5.symbol_info(name)
        tick = mt5.symbol_info_tick(name)
        if info is None or tick is None:
            print(f"No data for {name}, skipping")
            continue

        server_offsets.append(tick.time - datetime.now(timezone.utc).timestamp())

        spread_price = info.spread * info.point
        atr = h1_atr(name)
        stop_distance = atr * STOP_ATR_MULTIPLE if atr else None

        tick_value = info.trade_tick_value_loss or info.trade_tick_value
        if stop_distance and info.trade_tick_size and tick_value:
            loss_at_min = stop_distance / info.trade_tick_size * tick_value * info.volume_min
            fits = loss_at_min <= risk_money
            spread_ok = spread_price <= stop_distance * MAX_SPREAD_PCT_OF_STOP / 100.0
        else:
            loss_at_min, fits, spread_ok = None, None, None

        row = {
            "symbol": name,
            "path": info.path,
            "digits": info.digits,
            "spread_points": info.spread,
            "volume_min": info.volume_min,
            "volume_step": info.volume_step,
            "volume_max": info.volume_max,
            "contract_size": info.trade_contract_size,
            "stops_level_points": info.trade_stops_level,
            "swap_long": info.swap_long,
            "swap_short": info.swap_short,
            "h1_atr": round(atr, info.digits) if atr else None,
            "assumed_stop_distance": round(stop_distance, info.digits) if stop_distance else None,
            f"loss_at_min_lot_{account.currency}": round(loss_at_min, 2) if loss_at_min else None,
            "fits_risk_limit": fits,
            "spread_ok": spread_ok,
        }
        for label, tf in TIMEFRAMES.items():
            bars, first = history_info(name, tf)
            row[f"{label}_bars"] = bars
            row[f"{label}_first_bar"] = first
        rows.append(row)

    if server_offsets:
        offset_hours = round(float(np.median(server_offsets)) / 3600.0)
        print(f"\nBroker server time is about UTC{offset_hours:+d} "
              "(run on a weekday while markets are open for a reliable value)")

    print("\n=== Symbols ===")
    header = f"{'symbol':<10}{'spread':>8}{'min lot':>9}{'loss@min':>10}{'fits?':>7}{'spr ok?':>9}{'H1 bars':>9}{'from':>12}"
    print(header)
    print("-" * len(header))
    loss_key = f"loss_at_min_lot_{account.currency}"
    for r in rows:
        loss = r[loss_key]
        print(f"{r['symbol']:<10}{r['spread_points']:>8}{r['volume_min']:>9}"
              f"{(loss if loss is not None else 'n/a'):>10}"
              f"{str(r['fits_risk_limit']):>7}{str(r['spread_ok']):>9}"
              f"{r['H1_bars']:>9}{str(r['H1_first_bar']):>12}")

    if rows:
        with open("connection_test_results.csv", "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print("\nSaved connection_test_results.csv in this folder.")

    mt5.shutdown()


if __name__ == "__main__":
    main()
