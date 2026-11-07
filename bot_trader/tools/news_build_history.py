"""Build a UTC high-impact news file in the bot's news.csv format from the Forex Factory history CSV.

Usage: python build_news_history.py INPUT.csv OUTPUT.csv
"""
import sys

import pandas as pd

CURRENCIES = {"USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD"}


def main(src: str, dst: str) -> None:
    df = pd.read_csv(src, usecols=["DateTime", "Currency", "Impact", "Event"])
    df["Impact"] = df["Impact"].astype(str).str.strip().str.lower()
    df["Currency"] = df["Currency"].astype(str).str.strip().str.upper()
    df = df[(df["Impact"] == "high") & (
        df["Currency"].isin(CURRENCIES))].copy()
    df["time_utc"] = pd.to_datetime(df["DateTime"], utc=True, errors="coerce")
    bad = int(df["time_utc"].isna().sum())
    df = df.dropna(subset=["time_utc"])
    out = pd.DataFrame(
        {
            "time_utc": df["time_utc"].dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "currency": df["Currency"],
            "impact": "high",
            "title": df["Event"].astype(str).str.replace(",", " ", regex=False).str.strip(),
        }
    )
    out = out.drop_duplicates().sort_values(["time_utc", "currency", "title"])
    out.to_csv(dst, index=False)
    weekend = int(pd.to_datetime(
        out["time_utc"]).dt.dayofweek.isin([5, 6]).sum())
    print(
        f"rows written: {len(out)}; unparseable times dropped: {bad}; rows on Sat/Sun (UTC): {weekend}")
    print(
        f"first: {out['time_utc'].iloc[0]}  last: {out['time_utc'].iloc[-1]}")

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
