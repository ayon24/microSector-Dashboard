"""Build equal-weight micro-sector indices.

Methodology
  - Rebalanced to equal weight at the close of the first trading day of Jan/Apr/Jul/Oct.
    Between rebalances weights drift with prices, like a buy-and-hold portfolio.
  - Eligible at a rebalance: has a close that day, and 20-day median traded value
    (close x volume, measured up to the previous session) of at least ₹1 crore.
    New listings therefore join at the first rebalance after ~20 sessions of trading.
  - A sector's index starts at the first rebalance with 3+ eligible stocks (base 100). If it later
    drops below 3 it continues with whatever is eligible, and the count is reported.
  - A stock with no close on a day is left out of that day's average (its weight is held until
    it trades again; its return is then measured from its last close).

Writes data/indices.parquet (date x sector_id levels, plus the benchmark) and
data/index_meta.json (per-sector start date, rebalance history and current constituents).
"""
from __future__ import annotations

import json
import logging

import numpy as np
import pandas as pd

from .common import (
    BASE_VALUE,
    BENCHMARK,
    INDICES_PARQUET,
    LIQUIDITY_WINDOW,
    MIN_CONSTITUENTS,
    MIN_MEDIAN_TRADED_VALUE,
    REBALANCE_MONTHS,
    ROOT,
    load_prices,
    load_taxonomy,
    wide_prices,
)

log = logging.getLogger("build")

INDEX_META_JSON = ROOT / "data" / "index_meta.json"


def trading_calendar(close: pd.DataFrame) -> pd.DatetimeIndex:
    """Sessions where at least half the stocks traded (robust to a missing benchmark print)."""
    stocks = close.drop(columns=[BENCHMARK], errors="ignore")
    return stocks.index[stocks.notna().sum(axis=1) >= 0.5 * stocks.shape[1]]


def rebalance_dates(calendar: pd.DatetimeIndex) -> list[pd.Timestamp]:
    first = calendar[LIQUIDITY_WINDOW]  # need a liquidity history before the first rebalance
    cal = pd.Series(calendar, index=calendar)
    quarter_starts = cal[cal.dt.month.isin(REBALANCE_MONTHS)].groupby([cal.dt.year, cal.dt.month]).min()
    return sorted(d for d in quarter_starts if d > first)


def build_sector(
    members: list[str],
    close: pd.DataFrame,
    liquidity: pd.DataFrame,
    calendar: pd.DatetimeIndex,
    rebals: list[pd.Timestamp],
) -> tuple[pd.Series, list[dict]]:
    members = [m for m in members if m in close.columns]
    px = close.loc[calendar, members].to_numpy()
    liq = liquidity.loc[calendar, members].shift(1).to_numpy()  # measured up to previous session
    pos = {d: i for i, d in enumerate(calendar)}

    levels = np.full(len(calendar), np.nan)
    history: list[dict] = []
    level = None
    bounds = [pos[r] for r in rebals] + [len(calendar) - 1]

    for k, r_i in enumerate(bounds[:-1]):
        eligible = np.where((liq[r_i] >= MIN_MEDIAN_TRADED_VALUE) & ~np.isnan(px[r_i]))[0]
        if level is None:
            if len(eligible) < MIN_CONSTITUENTS:
                continue
            level = BASE_VALUE
            levels[r_i] = level
        history.append({"date": str(calendar[r_i].date()), "constituents": [members[j] for j in eligible]})

        end_i = bounds[k + 1]
        if len(eligible) == 0:
            levels[r_i + 1 : end_i + 1] = level
            continue
        weights = np.full(len(eligible), 1.0 / len(eligible))
        last = px[r_i, eligible].copy()
        for t in range(r_i + 1, end_i + 1):
            p = px[t, eligible]
            ok = ~np.isnan(p)
            if ok.any():
                rets = p[ok] / last[ok] - 1.0
                day_ret = float(np.dot(weights[ok], rets) / weights[ok].sum())
                weights[ok] *= 1.0 + rets
                last[ok] = p[ok]
                level *= 1.0 + day_ret
            levels[t] = level

    return pd.Series(levels, index=calendar), history


def run() -> pd.DataFrame:
    tax = load_taxonomy()
    close, volume = wide_prices(load_prices())
    calendar = trading_calendar(close)
    liquidity = (close * volume).rolling(LIQUIDITY_WINDOW, min_periods=15).median()
    rebals = rebalance_dates(calendar)
    log.info("%d sessions %s → %s, %d rebalances", len(calendar), calendar[0].date(), calendar[-1].date(), len(rebals))

    series, meta = {}, {}
    for (sector_id, name, broad), grp in tax.groupby(["sector_id", "micro_sector", "broad_sector"], sort=False):
        members = grp["symbol"].tolist()
        levels, history = build_sector(members, close, liquidity, calendar, rebals)
        latest_liq = liquidity.iloc[-1].reindex(members)
        meta[sector_id] = {
            "name": name,
            "broad_sector": broad,
            "members": members,
            "indexed": bool(history),
            "start": str(levels.first_valid_index().date()) if history else None,
            "constituents": history[-1]["constituents"] if history else [],
            "last_rebalance": history[-1]["date"] if history else None,
            "rebalances": history,
            "median_traded_value_cr": {s: (None if pd.isna(v) else round(float(v) / 1e7, 2)) for s, v in latest_liq.items()},
        }
        if history:
            series[sector_id] = levels
            log.info("%-40s start %s, %2d/%2d constituents", name, meta[sector_id]["start"], len(history[-1]["constituents"]), len(members))
        else:
            log.info("%-40s NOT INDEXED (fewer than %d eligible stocks)", name, MIN_CONSTITUENTS)

    out = pd.DataFrame(series)
    out[BENCHMARK] = close[BENCHMARK].reindex(calendar)
    out.index.name = "date"
    out.to_parquet(INDICES_PARQUET, compression="zstd")
    INDEX_META_JSON.write_text(json.dumps(meta, indent=1))
    return out


def main() -> None:
    run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    main()
