"""Data-quality checks on data/prices.parquet. Writes data/validation_report.json.

Warnings (published anyway, listed in the report):
  - single-day moves over 20% not yet reviewed (possible split, bonus or demerger not adjusted)
  - symbols with gaps or stale prices
  - latest close differing from NSE bhavcopy by more than 1%
Errors (exit code 1, so the nightly job does not publish bad data):
  - benchmark missing on the latest date
  - more than 25% of symbols missing on the latest date

Mark a genuine big move as reviewed in taxonomy/reviewed_moves.csv (symbol,date,note), or fix a
bad one with a price factor in taxonomy/corporate_actions.csv (symbol,ex_date,factor,note).
"""
from __future__ import annotations

import json
import logging
import sys

import pandas as pd

from .common import (
    BENCHMARK,
    BIG_MOVE_THRESHOLD,
    PROVISIONAL,
    ROOT,
    STALE_DAYS,
    VALIDATION_JSON,
    load_prices,
    load_taxonomy,
    wide_prices,
)
from .sources import fetch_bhavcopy

log = logging.getLogger("validate")

REVIEWED_CSV = ROOT / "taxonomy" / "reviewed_moves.csv"
BHAV_TOLERANCE = 0.01
# NSE's widest circuit is 20%, so a move of exactly 20% is a circuit hit, not a data error. Yahoo's
# adjusted closes put such moves a hair either side of 20%, so leave a small margin.
CIRCUIT_TOLERANCE = 0.001
GAP_LOOKBACK = 60


def _reviewed() -> set[tuple[str, pd.Timestamp]]:
    if not REVIEWED_CSV.exists():
        return set()
    r = pd.read_csv(REVIEWED_CSV, comment="#", dtype=str)
    return {(s, pd.Timestamp(d)) for s, d in zip(r["symbol"], r["date"])}


def run(check_bhavcopy: bool = True) -> dict:
    tax = load_taxonomy()
    prices = load_prices()
    close, _ = wide_prices(prices)
    calendar = close[BENCHMARK].dropna().index if BENCHMARK in close else close.index
    latest = close.index.max()
    stocks = sorted(set(tax["symbol"]))
    errors, warnings = [], []

    # Missing entirely
    no_data = [s for s in stocks if s not in close.columns or close[s].dropna().empty]
    if no_data:
        warnings.append({"check": "no_data", "symbols": no_data})

    # Benchmark and coverage on latest date
    if BENCHMARK not in close or pd.isna(close.at[latest, BENCHMARK]):
        errors.append({"check": "benchmark_missing", "date": str(latest.date())})
    present = [s for s in stocks if s in close.columns and pd.notna(close.at[latest, s])]
    missing_latest = sorted(set(stocks) - set(present) - set(no_data))
    if len(missing_latest) > 0.25 * len(stocks):
        errors.append({"check": "coverage", "date": str(latest.date()), "missing": len(missing_latest)})

    # Stale and gappy symbols
    recent_cal = calendar[-GAP_LOOKBACK:]
    for s in stocks:
        if s in no_data:
            continue
        series = close[s].dropna()
        last = series.index.max()
        behind = int((calendar > last).sum())
        if behind > STALE_DAYS:
            warnings.append({"check": "stale", "symbol": s, "last_date": str(last.date()), "sessions_behind": behind})
            continue
        listed = recent_cal[recent_cal >= series.index.min()]
        gaps = int(close.loc[listed, s].isna().sum())
        if gaps > 3:
            warnings.append({"check": "gaps", "symbol": s, "missing_sessions": gaps, "of": len(listed)})

    # Big single-day moves
    reviewed = _reviewed()
    moves = []
    for s in [c for c in stocks if c in close.columns]:
        series = close[s].dropna()
        ret = series.pct_change()
        for d, r in ret[ret.abs() > BIG_MOVE_THRESHOLD + CIRCUIT_TOLERANCE].items():
            if (s, d) in reviewed:
                continue
            moves.append({"symbol": s, "date": str(d.date()), "return": round(float(r), 4)})
    if moves:
        warnings.append({"check": "big_moves", "threshold": BIG_MOVE_THRESHOLD, "moves": moves})

    # Cross-check the latest official closes against bhavcopy. An intraday (provisional) bar has no
    # bhavcopy yet, so check the last completed session instead.
    if check_bhavcopy:
        final = prices.loc[prices["source"] != PROVISIONAL, "date"].max()
        bhav = fetch_bhavcopy(final.date())
        if bhav is None:
            warnings.append({"check": "bhavcopy_unavailable", "date": str(final.date())})
        else:
            b = bhav.drop_duplicates("symbol").set_index("symbol")["close"]
            diffs = []
            for s in stocks:
                if s in b.index and s in close.columns and pd.notna(close.at[final, s]):
                    d = close.at[final, s] / b[s] - 1
                    if abs(d) > BHAV_TOLERANCE:
                        diffs.append({"symbol": s, "ours": round(float(close.at[final, s]), 2), "nse": float(b[s])})
            if diffs:
                warnings.append({"check": "bhavcopy_mismatch", "date": str(final.date()), "symbols": diffs})

    report = {
        "latest_date": str(latest.date()),
        "symbols_expected": len(stocks),
        "symbols_on_latest_date": len(present),
        "errors": errors,
        "warnings": warnings,
    }
    VALIDATION_JSON.write_text(json.dumps(report, indent=2))

    log.info("Latest date %s: %d/%d symbols present", latest.date(), len(present), len(stocks))
    for w in warnings:
        n = len(w.get("moves", w.get("symbols", [w.get("symbol")])))
        log.warning("%s (%d)", w["check"], n)
    for e in errors:
        log.error("%s", e)
    return report


def main() -> None:
    report = run(check_bhavcopy="--no-bhavcopy" not in sys.argv)
    if report["errors"]:
        sys.exit(1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    main()
