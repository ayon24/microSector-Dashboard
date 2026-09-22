"""Write the static JSON the dashboard reads.

site/data/summary.json          one row per micro sector: returns per timeframe, breadth, MA status
site/data/benchmark.json        NIFTY 500 daily closes
site/data/indices/<id>.json     daily index levels + constituent table (loaded on demand)
site/data/ema.json              distance from EMA and streak, per index and per stock, for each preset period

Output is deterministic (no timestamps beyond the data date), so a night with no new prices
produces no diff and the workflow skips publishing.
"""
from __future__ import annotations

import json
import logging
import shutil

import numpy as np
import pandas as pd

from .common import (
    BENCHMARK,
    BENCHMARK_NAME,
    FETCH_META_JSON,
    INDICES_PARQUET,
    MIN_CONSTITUENTS,
    MIN_MEDIAN_TRADED_VALUE,
    SITE_DATA,
    load_prices,
    load_taxonomy,
    wide_prices,
)
from .build_indices import INDEX_META_JSON

log = logging.getLogger("export")

TIMEFRAMES = ["1D", "1W", "1M", "3M", "6M", "YTD", "1Y", "3Y", "5Y"]
EXPORT_YEARS = 5
EMA_PERIODS = [5, 9, 10, 20, 21, 30, 50, 100, 150, 200]
OFFSETS = {
    "1W": pd.DateOffset(weeks=1),
    "1M": pd.DateOffset(months=1),
    "3M": pd.DateOffset(months=3),
    "6M": pd.DateOffset(months=6),
    "1Y": pd.DateOffset(years=1),
    "3Y": pd.DateOffset(years=3),
    "5Y": pd.DateOffset(years=5),
}


def timeframe_start(dates: pd.DatetimeIndex, tf: str) -> pd.Timestamp | None:
    """Reference date for a timeframe: the last session on or before the target date.

    Mirrors startIndex() in site/app.js so charts and return figures agree.
    """
    end = dates[-1]
    if tf == "1D":
        return dates[-2] if len(dates) > 1 else None
    target = pd.Timestamp(end.year - 1, 12, 31) if tf == "YTD" else end - OFFSETS[tf]
    prior = dates[dates <= target]
    return prior[-1] if len(prior) else None


def returns(series: pd.Series) -> dict[str, float | None]:
    s = series.dropna()
    out = {}
    for tf in TIMEFRAMES:
        start = timeframe_start(s.index, tf)
        out[tf] = None if start is None else round(float(s.iloc[-1] / s[start] - 1) * 100, 2)
    return out


def above_ma(series: pd.Series, window: int) -> bool | None:
    s = series.dropna()
    if len(s) < window:
        return None
    return bool(s.iloc[-1] > s.iloc[-window:].mean())


def ema_stats(series: pd.Series) -> dict[str, list]:
    """{period: [% distance of last close from its EMA, streak]} for each preset period.

    The streak counts consecutive sessions on the current side of the EMA, positive when above
    and negative when below. Periods longer than the available history are left out.
    """
    s = series.dropna()
    out = {}
    for p in EMA_PERIODS:
        if len(s) < p:
            continue
        ema = s.ewm(span=p, adjust=False).mean()
        above = (s > ema).to_numpy()[::-1]
        flips = np.flatnonzero(above != above[0])
        streak = int(flips[0]) if len(flips) else len(above)
        out[str(p)] = [round(float(s.iloc[-1] / ema.iloc[-1] - 1) * 100, 2), streak if above[0] else -streak]
    return out


def pct(values: list[bool | None]) -> float | None:
    v = [x for x in values if x is not None]
    return round(100 * sum(v) / len(v), 1) if v else None


def _num(x: float | None, nd: int = 2) -> float | None:
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), nd)


def run() -> dict:
    tax = load_taxonomy()
    names = dict(zip(tax["symbol"], tax["name"]))
    close, _ = wide_prices(load_prices())
    idx = pd.read_parquet(INDICES_PARQUET)
    meta = json.loads(INDEX_META_JSON.read_text())
    as_of = idx.index[-1]
    export_from = as_of - pd.DateOffset(years=EXPORT_YEARS) - pd.Timedelta(days=10)

    if (SITE_DATA / "indices").exists():
        shutil.rmtree(SITE_DATA / "indices")
    (SITE_DATA / "indices").mkdir(parents=True)

    bench = idx[BENCHMARK].dropna()
    bench_x = bench[bench.index >= export_from]
    (SITE_DATA / "benchmark.json").write_text(json.dumps({
        "name": BENCHMARK_NAME,
        "dates": [d.strftime("%Y-%m-%d") for d in bench_x.index],
        "values": [round(float(v), 2) for v in bench_x],
    }, separators=(",", ":")))

    stock_rows = {}
    for sym in tax["symbol"].unique():
        if sym not in close.columns or close[sym].dropna().empty:
            continue
        s = close[sym].dropna()
        stock_rows[sym] = {
            "symbol": sym,
            "name": names[sym],
            "close": _num(s.iloc[-1]),
            "returns": returns(s),
            "above50": above_ma(s, 50),
            "above200": above_ma(s, 200),
            "traded_on_latest": bool(s.index[-1] == as_of),
        }

    sectors = []
    for sector_id, m in meta.items():
        members = [s for s in m["members"] if s in stock_rows]
        constituents = set(m["constituents"])
        in_idx = [stock_rows[s] for s in members if s in constituents]
        day = [r["returns"]["1D"] for r in in_idx if r["traded_on_latest"] and r["returns"]["1D"] is not None]
        row = {
            "id": sector_id,
            "name": m["name"],
            "broad_sector": m["broad_sector"],
            "indexed": m["indexed"],
            "members": len(members),
            "constituents": len(in_idx),
            "start": m["start"],
            "advancers": sum(1 for r in day if r > 0),
            "decliners": sum(1 for r in day if r < 0),
            "above50_pct": pct([r["above50"] for r in in_idx]),
            "above200_pct": pct([r["above200"] for r in in_idx]),
        }
        if m["indexed"]:
            level = idx[sector_id].dropna()
            row.update({
                "level": _num(level.iloc[-1]),
                "returns": returns(level),
                "index_above50": above_ma(level, 50),
                "index_above200": above_ma(level, 200),
            })
            level_x = level[level.index >= export_from]
            detail = {
                "id": sector_id,
                "name": m["name"],
                "broad_sector": m["broad_sector"],
                "start": m["start"],
                "last_rebalance": m["last_rebalance"],
                "dates": [d.strftime("%Y-%m-%d") for d in level_x.index],
                "values": [round(float(v), 2) for v in level_x],
            }
        else:
            row.update({"level": None, "returns": {tf: None for tf in TIMEFRAMES},
                        "index_above50": None, "index_above200": None})
            detail = {"id": sector_id, "name": m["name"], "broad_sector": m["broad_sector"],
                      "start": None, "last_rebalance": None, "dates": [], "values": []}
        detail["stocks"] = [
            {**stock_rows[s], "in_index": s in constituents,
             "median_traded_value_cr": m["median_traded_value_cr"].get(s)}
            for s in members
        ]
        (SITE_DATA / "indices" / f"{sector_id}.json").write_text(json.dumps(detail, separators=(",", ":")))
        sectors.append(row)

    ema = {
        "as_of": as_of.strftime("%Y-%m-%d"),
        "periods": EMA_PERIODS,
        "sectors": [
            {
                "id": sid, "name": m["name"], "broad_sector": m["broad_sector"], "indexed": m["indexed"],
                "members": [s for s in m["members"] if s in stock_rows], "constituents": m["constituents"],
                "r1": returns(idx[sid])["1D"] if m["indexed"] else None,
                "ema": ema_stats(idx[sid]) if m["indexed"] else {},
            }
            for sid, m in meta.items()
        ],
        "stocks": {
            sym: {"n": r["name"], "c": r["close"], "r1": r["returns"]["1D"], "ema": ema_stats(close[sym])}
            for sym, r in stock_rows.items()
        },
    }
    fetch = json.loads(FETCH_META_JSON.read_text()) if FETCH_META_JSON.exists() else {}
    provisional = bool(fetch.get("provisional")) and fetch.get("latest_date") == as_of.strftime("%Y-%m-%d")
    ema["provisional"] = provisional
    (SITE_DATA / "ema.json").write_text(json.dumps(ema, separators=(",", ":")))

    summary = {
        "as_of": as_of.strftime("%Y-%m-%d"),
        "provisional": provisional,
        "snapshot_ist": fetch.get("snapshot_ist") if provisional else None,
        "timeframes": TIMEFRAMES,
        "benchmark": {"name": BENCHMARK_NAME, "returns": returns(bench)},
        "methodology": {
            "weighting": "Equal weight, rebalanced quarterly (Jan/Apr/Jul/Oct)",
            "min_median_traded_value_cr": MIN_MEDIAN_TRADED_VALUE / 1e7,
            "min_constituents": MIN_CONSTITUENTS,
        },
        "sectors": sectors,
    }
    (SITE_DATA / "summary.json").write_text(json.dumps(summary, separators=(",", ":")))
    log.info("Exported %d sectors (%d indexed) as of %s", len(sectors), sum(s["indexed"] for s in sectors), as_of.date())
    return summary


def main() -> None:
    run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    main()
