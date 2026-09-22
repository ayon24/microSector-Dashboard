"""Build taxonomy/universe.csv: every liquid NSE stock with the facts needed to classify it.

    python -m pipeline.build_universe            # uses cached Yahoo profiles where present
    python -m pipeline.build_universe --refresh  # re-fetch all Yahoo profiles

Liquidity uses the same rule as the indices: 20-session median traded value >= ₹1 crore,
computed from NSE bhavcopy. Only companies on NSE's main-board equity list (EQUITY_L) count, which
excludes ETFs, liquid funds and SME-platform stocks. Each stock gets its NSE sector (from the Nifty Total Market and
Microcap 250 constituent lists), its Yahoo sector/industry, market cap and business summary,
and the micro sector(s) it already sits in, if any.

Not part of the nightly job. Run it when expanding or refreshing the taxonomy.
"""
from __future__ import annotations

import io
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pandas as pd
import requests
import yfinance as yf

from .common import LIQUIDITY_WINDOW, MIN_MEDIAN_TRADED_VALUE, ROOT, load_taxonomy, now_ist, yahoo_ticker
from .sources import BHAV_HEADERS, fetch_bhavcopy

log = logging.getLogger("universe")

UNIVERSE_CSV = ROOT / "taxonomy" / "universe.csv"
PROFILE_CACHE = ROOT / "data" / "yahoo_profiles.json"
EQUITY_LIST = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
NSE_SECTOR_LISTS = [
    "https://niftyindices.com/IndexConstituent/ind_niftytotalmarket_list.csv",
    "https://niftyindices.com/IndexConstituent/ind_niftymicrocap250_list.csv",
]
SUMMARY_CHARS = 400


def _csv(url: str) -> pd.DataFrame:
    r = requests.get(url, headers=BHAV_HEADERS, timeout=30)
    r.raise_for_status()
    df = pd.read_csv(io.BytesIO(r.content))
    df.columns = [c.strip() for c in df.columns]
    return df


def liquid_symbols() -> pd.DataFrame:
    """Median daily traded value over the last 20 sessions, from bhavcopy."""
    frames, day = [], now_ist().date()
    tries = 0
    while len(frames) < LIQUIDITY_WINDOW and tries < 45:
        tries += 1
        if day.weekday() < 5:
            b = fetch_bhavcopy(day)
            if b is not None and not b.empty:
                frames.append(b)
        day -= timedelta(days=1)
    bhav = pd.concat(frames)
    bhav["traded_value"] = bhav["close"] * bhav["volume"]
    stats = bhav.groupby("symbol").agg(
        median_tv=("traded_value", "median"), sessions=("date", "nunique"), close=("close", "last")
    )
    # a stock that traded on fewer than half the sessions has a median of zero in practice
    stats.loc[stats["sessions"] < LIQUIDITY_WINDOW / 2, "median_tv"] = 0
    log.info("Bhavcopy: %d sessions, %d symbols", len(frames), len(stats))
    return stats[stats["median_tv"] >= MIN_MEDIAN_TRADED_VALUE]


def yahoo_profile(symbol: str) -> dict:
    for attempt in range(4):
        try:
            info = yf.Ticker(yahoo_ticker(symbol)).info or {}
            return {
                "yahoo_sector": info.get("sector"),
                "yahoo_industry": info.get("industry"),
                "mcap_cr": round(info["marketCap"] / 1e7) if info.get("marketCap") else None,
                "summary": (info.get("longBusinessSummary") or "")[:SUMMARY_CHARS],
            }
        except Exception as e:  # rate limits, missing tickers
            if "Too Many Requests" in str(e) and attempt < 3:
                time.sleep(30 * (attempt + 1))
                continue
            log.warning("%s: no Yahoo profile (%s)", symbol, e)
            return {}
    return {}


def run(refresh: bool = False) -> pd.DataFrame:
    liquid = liquid_symbols()
    names = _csv(EQUITY_LIST).set_index("SYMBOL")["NAME OF COMPANY"]
    excluded = liquid.index.difference(names.index)
    liquid = liquid.loc[liquid.index.intersection(names.index)]
    log.info("Excluded %d liquid symbols not on the equity list (ETFs, funds, SME)", len(excluded))
    nse_sector = pd.concat([_csv(u) for u in NSE_SECTOR_LISTS]).drop_duplicates("Symbol").set_index("Symbol")["Industry"]

    cache = {} if refresh or not PROFILE_CACHE.exists() else json.loads(PROFILE_CACHE.read_text())
    todo = [s for s in liquid.index if s not in cache or not cache[s]]
    if todo:
        log.info("Fetching %d Yahoo profiles (%d cached)", len(todo), len(cache))
        with ThreadPoolExecutor(max_workers=2) as ex:
            for sym, prof in zip(todo, ex.map(yahoo_profile, todo)):
                cache[sym] = prof
        PROFILE_CACHE.write_text(json.dumps(cache, indent=0, sort_keys=True))

    tax = load_taxonomy()
    current = tax.groupby("symbol")["micro_sector"].agg(" | ".join)

    rows = []
    for sym, r in liquid.iterrows():
        p = cache.get(sym, {})
        rows.append({
            "symbol": sym,
            "name": names.get(sym, ""),
            "nse_sector": nse_sector.get(sym, ""),
            "yahoo_sector": p.get("yahoo_sector") or "",
            "yahoo_industry": p.get("yahoo_industry") or "",
            "mcap_cr": p.get("mcap_cr"),
            "median_traded_value_cr": round(r["median_tv"] / 1e7, 2),
            "current_micro_sectors": current.get(sym, ""),
            "summary": " ".join((p.get("summary") or "").split()),
        })
    out = pd.DataFrame(rows).sort_values(["nse_sector", "yahoo_industry", "mcap_cr"], ascending=[True, True, False])
    out.to_csv(UNIVERSE_CSV, index=False)
    log.info(
        "Universe: %d liquid stocks, %d with NSE sector, %d with Yahoo industry, %d already classified",
        len(out), (out["nse_sector"] != "").sum(), (out["yahoo_industry"] != "").sum(),
        (out["current_micro_sectors"] != "").sum(),
    )
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    run(refresh="--refresh" in sys.argv)
