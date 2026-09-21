"""Price sources.

Primary:   yfinance (adjusted for splits/bonuses/dividends, no login).
Fallback:  NSE bhavcopy (official EOD file, no login; can be blocked from non-Indian IPs).
Last:      broker API (needs credentials; see fetch_broker_closes).
"""
from __future__ import annotations

import io
import logging
import os
import zipfile
from datetime import date

import pandas as pd
import requests
import yfinance as yf

from .common import yahoo_ticker

log = logging.getLogger(__name__)
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

CHUNK = 50


def fetch_yahoo(symbols: list[str], start: date | None = None, period: str | None = None) -> pd.DataFrame:
    """Download daily adjusted close and volume. Returns long frame: date, symbol, close, volume."""
    frames = []
    for i in range(0, len(symbols), CHUNK):
        chunk = symbols[i : i + CHUNK]
        tickers = [yahoo_ticker(s) for s in chunk]
        raw = yf.download(
            tickers,
            start=start.isoformat() if start else None,
            period=None if start else period,
            auto_adjust=True,
            progress=False,
            group_by="ticker",
            threads=True,
        )
        if raw is None or raw.empty:
            continue
        for sym, tic in zip(chunk, tickers):
            if tic not in raw.columns.get_level_values(0):
                continue
            df = raw[tic][["Close", "Volume"]].dropna(subset=["Close"])
            if df.empty:
                continue
            frames.append(
                pd.DataFrame(
                    {
                        "date": pd.to_datetime(df.index).tz_localize(None).normalize(),
                        "symbol": sym,
                        "close": df["Close"].astype(float).values,
                        "volume": df["Volume"].fillna(0).astype(float).values,
                    }
                )
            )
    if not frames:
        return pd.DataFrame(columns=["date", "symbol", "close", "volume"])
    return pd.concat(frames, ignore_index=True)


BHAV_URL = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
BHAV_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "*/*", "Referer": "https://www.nseindia.com/"}


def fetch_bhavcopy(day: date) -> pd.DataFrame | None:
    """Unadjusted NSE closes for one day (EQ/BE/BZ series). None if unavailable or blocked."""
    try:
        r = requests.get(BHAV_URL.format(d=day), headers=BHAV_HEADERS, timeout=20)
        if r.status_code != 200 or not r.content.startswith(b"PK"):
            return None
        z = zipfile.ZipFile(io.BytesIO(r.content))
        df = pd.read_csv(z.open(z.namelist()[0]))
    except Exception as e:  # network errors, blocks, bad zips
        log.warning("bhavcopy %s unavailable: %s", day, e)
        return None
    df = df[df["SctySrs"].isin(["EQ", "BE", "BZ", "SM", "ST"])]
    return pd.DataFrame(
        {
            "date": pd.Timestamp(day),
            "symbol": df["TckrSymb"].astype(str).values,
            "close": df["ClsPric"].astype(float).values,
            "volume": df["TtlTradgVol"].astype(float).values,
        }
    )


def fetch_broker_closes(symbols: list[str], day: date) -> pd.DataFrame | None:
    """Last-resort source. Not wired up yet.

    To enable, implement a login with your broker's SDK here (Angel SmartAPI supports
    TOTP login from code: env vars ANGEL_API_KEY, ANGEL_CLIENT_ID, ANGEL_PIN, ANGEL_TOTP_SECRET)
    and return a long frame (date, symbol, close, volume) for `day`.
    """
    if not os.environ.get("ANGEL_API_KEY"):
        return None
    log.warning("Broker fallback credentials present but fetch_broker_closes is not implemented")
    return None
