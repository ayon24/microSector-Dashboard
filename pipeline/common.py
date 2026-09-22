"""Shared paths, constants and loaders for the pipeline."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
TAXONOMY_CSV = ROOT / "taxonomy" / "micro_sectors.csv"
CORP_ACTIONS_CSV = ROOT / "taxonomy" / "corporate_actions.csv"
PRICES_PARQUET = ROOT / "data" / "prices.parquet"
VALIDATION_JSON = ROOT / "data" / "validation_report.json"
INDICES_PARQUET = ROOT / "data" / "indices.parquet"
SITE_DATA = ROOT / "site" / "data"

BENCHMARK = "^CRSLDX"          # NIFTY 500 on Yahoo
BENCHMARK_NAME = "NIFTY 500"
HISTORY_YEARS = 6              # 5 years shown + 1 year warm-up for 200-DMA / liquidity

# Index methodology
MIN_MEDIAN_TRADED_VALUE = 1e7  # ₹1 crore, 20-day median
LIQUIDITY_WINDOW = 20
MIN_CONSTITUENTS = 3
REBALANCE_MONTHS = (1, 4, 7, 10)
BASE_VALUE = 100.0

# Validation
BIG_MOVE_THRESHOLD = 0.20
STALE_DAYS = 5

IST = timezone(timedelta(hours=5, minutes=30))
MARKET_DATA_FINAL_IST = (16, 30)  # a bar for "today" is treated as final only after this time
PROVISIONAL = "yahoo_provisional"  # source label for an intraday (pre-close) bar
FETCH_META_JSON = ROOT / "data" / "fetch_meta.json"


def now_ist() -> datetime:
    return datetime.now(IST)


def yahoo_ticker(symbol: str) -> str:
    return symbol if symbol.startswith("^") else f"{symbol}.NS"


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def load_taxonomy() -> pd.DataFrame:
    t = pd.read_csv(TAXONOMY_CSV, dtype=str).apply(lambda c: c.str.strip())
    t["sector_id"] = t["micro_sector"].map(slugify)
    return t


def load_corporate_actions() -> pd.DataFrame:
    """Manual price fixes; see the header of taxonomy/corporate_actions.csv."""
    if not CORP_ACTIONS_CSV.exists():
        return pd.DataFrame(columns=["symbol", "date", "action", "factor", "note"])
    ca = pd.read_csv(CORP_ACTIONS_CSV, comment="#", dtype={"symbol": str, "action": str, "note": str})
    ca["date"] = pd.to_datetime(ca["date"])
    ca["factor"] = ca["factor"].astype(float)
    return ca


def load_prices() -> pd.DataFrame:
    return pd.read_parquet(PRICES_PARQUET)


def wide_prices(prices: pd.DataFrame, apply_corp_actions: bool = True) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (close, volume) as date x symbol frames, with manual corporate-action fixes applied."""
    close = prices.pivot(index="date", columns="symbol", values="close").sort_index()
    volume = prices.pivot(index="date", columns="symbol", values="volume").sort_index()
    if apply_corp_actions:
        for ca in load_corporate_actions().itertuples():
            if ca.symbol not in close.columns:
                continue
            if ca.action == "adjust":
                mask = close.index < ca.date
                close.loc[mask, ca.symbol] *= ca.factor
                volume.loc[mask, ca.symbol] /= ca.factor
            elif ca.action == "drop_before":
                mask = close.index < ca.date
                close.loc[mask, ca.symbol] = float("nan")
                volume.loc[mask, ca.symbol] = float("nan")
            elif ca.action == "drop_day":
                close.loc[close.index == ca.date, ca.symbol] = float("nan")
                volume.loc[volume.index == ca.date, ca.symbol] = float("nan")
            else:
                raise ValueError(f"Unknown corporate action {ca.action!r} for {ca.symbol}")
    return close, volume
