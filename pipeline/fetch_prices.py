"""Backfill and incrementally update data/prices.parquet.

    python -m pipeline.fetch_prices          # incremental (backfills any new symbols)
    python -m pipeline.fetch_prices --full   # re-download everything

The daily job runs at 3:00 PM IST, before the 3:30 PM close, so today's bar is an intraday
snapshot. It is stored with source "yahoo_provisional" and replaced by the official close on the
next run, which re-downloads the last few sessions anyway.

Yahoo's adjusted history is rewritten whenever a stock goes ex-split/bonus/dividend, so on
each update the overlapping days are compared with what we stored; any symbol whose history
moved gets fully re-downloaded instead of having new rows appended to a stale series.
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import timedelta

import pandas as pd

from .common import (
    BENCHMARK,
    HISTORY_YEARS,
    FETCH_META_JSON,
    MARKET_DATA_FINAL_IST,
    PRICES_PARQUET,
    PROVISIONAL,
    load_taxonomy,
    now_ist,
)
from .sources import fetch_bhavcopy, fetch_broker_closes, fetch_yahoo

log = logging.getLogger("fetch")

OVERLAP_DAYS = 10
READJUST_TOLERANCE = 0.002


def _market_closed_for_today() -> bool:
    now = now_ist()
    return (now.hour, now.minute) >= MARKET_DATA_FINAL_IST


def _label_provisional(df: pd.DataFrame) -> pd.DataFrame:
    """Before the close is final, today's Yahoo bar is an intraday snapshot."""
    if _market_closed_for_today():
        return df
    df = df.copy()
    today = pd.Timestamp(now_ist().date())
    df.loc[(df["date"] == today) & (df["source"] == "yahoo"), "source"] = PROVISIONAL
    return df


def _upsert(base: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    if new.empty:
        return base
    out = pd.concat([base, new], ignore_index=True)
    return out.drop_duplicates(["date", "symbol"], keep="last")


def _fill_gaps_from_fallbacks(prices: pd.DataFrame, symbols: list[str]) -> pd.DataFrame:
    """If Yahoo missed the latest session (or a trading day entirely), fill from bhavcopy, then broker."""
    now = now_ist()
    candidates = {prices["date"].max()}
    if now.weekday() < 5 and _market_closed_for_today():
        candidates.add(pd.Timestamp(now.date()))
    else:
        # bhavcopy for today only exists after the close; a gap in the intraday snapshot is expected
        candidates.discard(pd.Timestamp(now.date()))
    stock_syms = [s for s in symbols if not s.startswith("^")]
    for day in sorted(d for d in candidates if pd.notna(d)):
        have = set(prices.loc[prices["date"] == day, "symbol"])
        missing = [s for s in stock_syms if s not in have]
        if not missing:
            continue
        # Only bother when a meaningful share is missing (a few illiquid names not trading is normal).
        if len(missing) < 0.1 * len(stock_syms) and day in set(prices["date"]):
            continue
        filled = fetch_bhavcopy(day.date())
        source = "bhavcopy"
        if filled is None:
            filled = fetch_broker_closes(missing, day.date())
            source = "broker"
        if filled is None or filled.empty:
            if day in set(prices["date"]):
                log.warning("%s: %d symbols missing and no fallback source available", day.date(), len(missing))
            else:
                log.info("%s: no session data from any source (holiday, or not yet published)", day.date())
            continue
        filled = filled[filled["symbol"].isin(missing)]
        log.info("%s: filled %d/%d missing symbols from %s", day.date(), len(filled), len(missing), source)
        prices = _upsert(prices, filled.assign(source=source))
    return prices


def run(full: bool = False) -> pd.DataFrame:
    tax = load_taxonomy()
    symbols = sorted(set(tax["symbol"])) + [BENCHMARK]
    period = f"{HISTORY_YEARS}y"

    if full or not PRICES_PARQUET.exists():
        existing = pd.DataFrame(columns=["date", "symbol", "close", "volume", "source"])
    else:
        existing = pd.read_parquet(PRICES_PARQUET)
        existing = existing[existing["symbol"].isin(symbols)]

    have = set(existing["symbol"])
    new_syms = [s for s in symbols if s not in have]
    old_syms = [s for s in symbols if s in have]
    prices = existing

    if new_syms:
        log.info("Backfilling %d symbols (%s)", len(new_syms), period)
        back = fetch_yahoo(new_syms, period=period)
        prices = _upsert(prices, back.assign(source="yahoo"))
        missing = sorted(set(new_syms) - set(back["symbol"]))
        if missing:
            log.warning("No Yahoo history for: %s", ", ".join(missing))

    if old_syms:
        start = (existing["date"].max() - timedelta(days=OVERLAP_DAYS)).date()
        log.info("Updating %d symbols from %s", len(old_syms), start)
        recent = fetch_yahoo(old_syms, start=start)
        old = existing[existing["source"] == "yahoo"][["date", "symbol", "close"]]
        overlap = recent.merge(old, on=["date", "symbol"], suffixes=("", "_old"))
        drift = (overlap["close"] / overlap["close_old"] - 1).abs().groupby(overlap["symbol"]).max()
        readjusted = sorted(drift[drift > READJUST_TOLERANCE].index)
        if readjusted:
            log.info("History re-adjusted by Yahoo, refetching: %s", ", ".join(readjusted))
            refetched = fetch_yahoo(readjusted, period=period)
            prices = prices[~prices["symbol"].isin(refetched["symbol"].unique())]
            prices = _upsert(prices, refetched.assign(source="yahoo"))
            recent = recent[~recent["symbol"].isin(readjusted)]
        prices = _upsert(prices, recent.assign(source="yahoo"))

    prices = _label_provisional(prices)
    prices = _fill_gaps_from_fallbacks(prices, symbols)

    cutoff = pd.Timestamp(now_ist().date()) - pd.DateOffset(years=HISTORY_YEARS) - timedelta(days=7)
    prices = prices[prices["date"] >= cutoff]
    prices = prices.astype({"close": "float64", "volume": "float64", "symbol": "string", "source": "string"})
    prices = prices.sort_values(["symbol", "date"]).reset_index(drop=True)

    # Only rewrite when the data changed: different pyarrow versions encode identical data
    # differently, which would otherwise produce a spurious commit on holidays.
    if PRICES_PARQUET.exists() and pd.read_parquet(PRICES_PARQUET).equals(prices):
        log.info("No price changes; %s left untouched", PRICES_PARQUET.name)
    else:
        PRICES_PARQUET.parent.mkdir(parents=True, exist_ok=True)
        prices.to_parquet(PRICES_PARQUET, index=False, compression="zstd")
    latest = prices["date"].max()
    provisional = bool((prices.loc[prices["date"] == latest, "source"] == PROVISIONAL).any())
    meta = {"latest_date": str(latest.date()), "provisional": provisional}
    if provisional:
        meta["snapshot_ist"] = now_ist().strftime("%H:%M")
    FETCH_META_JSON.write_text(json.dumps(meta, indent=1))
    log.info(
        "Saved %d rows, %d symbols, %s → %s",
        len(prices), prices["symbol"].nunique(), prices["date"].min().date(), prices["date"].max().date(),
    )
    return prices


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="re-download full history for all symbols")
    args = ap.parse_args()
    run(full=args.full)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    main()
