"""OHLC price fetching for each bucket's instrument, via yfinance.

Two things are ported directly from the existing gold basis pipeline
(gold-data-pipelines area), not reinvented:

  1. The curl_cffi impersonated session -- yfinance has previously needed
     this to work around a session/auth error. Every fetch in this module
     goes through the same kind of session for that reason.
  2. get_liquid_contract() -- picks whichever near-term contract currently
     has the highest trading volume, instead of a generic continuous ticker.
     Originally Gold-only; now driven by a per-bucket "contract" spec in
     config.py (root + Yahoo exchange suffix + listed month codes) and used
     for Gold, Oil, S&P 500 and Nasdaq. US Dollar (DX-Y.NYB is a spot index
     with no contract) and Bitcoin use their single static ticker. The Oil /
     S&P / Nasdaq suffixes are UNVERIFIED against Yahoo: if no candidate
     returns data the fetch falls back to the bucket's continuous ticker and
     prints a [WARN] -- check "ticker_used" in the Log after the first run.

This is a lighter-weight tool than the gold pipeline's settlement-aligned
sampling: it takes a best-effort trailing-N-hourly-bars OHLC snapshot for
directional context on a dashboard, not a precisely-timed settlement price
for a basis/arbitrage calculation. Don't treat the numbers here as
settlement-grade -- they aren't trying to be.
"""
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf
from curl_cffi import requests as curl_requests

from config import MAX_PRICE_BAR_AGE_HOURS

HOURS_PER_WINDOW = 4
GOLD_MONTHS_AHEAD = 8


def make_session():
    """A single impersonated session, reused across all fetches in one run."""
    return curl_requests.Session(impersonate="chrome")


def get_liquid_contract(session, spec: dict, fallback: str, months_ahead: int = GOLD_MONTHS_AHEAD) -> str:
    """
    Checks the next several month-specific contracts listed in spec["months"]
    (root + month code + 2-digit year + exchange suffix, e.g. GCZ26.CMX) and
    returns whichever currently has the highest trading volume. Generalized
    from the gold basis pipeline's get_most_liquid_gold_contract() -- same
    logic, same fallback to the bucket's continuous ticker if contract
    volumes can't be resolved. No stickiness: the pick is re-made every scan.
    """
    month_codes = ["F", "G", "H", "J", "K", "M", "N", "Q", "U", "V", "X", "Z"]
    today = datetime.now()

    candidates = []
    for i in range(months_ahead):
        offset = today.month - 1 + i
        year = today.year + offset // 12
        month = offset % 12 + 1
        code = month_codes[month - 1]
        if code not in spec["months"]:
            continue
        yy = str(year)[-2:]
        candidates.append(f"{spec['root']}{code}{yy}{spec['suffix']}")

    best_symbol, best_volume = None, -1
    for symbol in candidates:
        try:
            hist = yf.Ticker(symbol, session=session).history(period="5d")
            if hist.empty:
                continue
            volume = hist.iloc[-1]["Volume"]
            if volume > best_volume:
                best_volume = volume
                best_symbol = symbol
        except Exception:
            continue

    if best_symbol is None:
        print(f"[WARN] Could not resolve {spec['root']} contract volumes (tried {candidates}), falling back to {fallback}.")
        return fallback

    print(f"[INFO] Most liquid {spec['root']} contract: {best_symbol} (volume={best_volume:.0f})")
    return best_symbol


def resolve_ticker(bucket: dict, session) -> str:
    """Returns the actual ticker to fetch for this bucket, applying the
    per-bucket resolution strategy set in config.py."""
    if bucket.get("ticker_resolution") == "liquid_contract":
        return get_liquid_contract(session, bucket["contract"], bucket["ticker"])
    return bucket["ticker"]


def fetch_ohlc_window(ticker: str, session, hours: int = HOURS_PER_WINDOW) -> dict:
    """
    Best-effort OHLC for the trailing `hours` of trading, built from hourly
    bars (Yahoo Finance has no native 4-hour interval, so this resamples
    from 1h bars: open = first bar's open, high/low = max/min across bars,
    close = last bar's close, volume = summed).

    Returns all-None values (never raises) if the market was closed the
    whole window -- expected and common for futures outside their active
    session, not a bug to retry around.
    """
    empty = {
        "ticker_used": ticker, "ohlc_open": None, "ohlc_high": None,
        "ohlc_low": None, "ohlc_close": None, "ohlc_volume": None,
        "ohlc_bars_used": 0,
    }
    try:
        hist = yf.Ticker(ticker, session=session).history(period="2d", interval="1h")
    except Exception as e:
        print(f"[WARN] OHLC fetch failed for {ticker}: {e}")
        return empty

    if hist.empty:
        print(f"[INFO] No hourly bars returned for {ticker} -- market likely closed this window.")
        return empty

    # Stale-bar guard: with period="2d", a closed market (weekend, holiday,
    # outage) still returns the last bars it had. Without this check those
    # old bars would be logged as if they were the current window.
    last_bar = pd.Timestamp(hist.index[-1])
    if last_bar.tzinfo is None:
        last_bar = last_bar.tz_localize("UTC")
    age_hours = (pd.Timestamp.now(tz="UTC") - last_bar).total_seconds() / 3600
    if age_hours > MAX_PRICE_BAR_AGE_HOURS:
        print(f"[INFO] Newest {ticker} bar is {age_hours:.1f}h old (limit {MAX_PRICE_BAR_AGE_HOURS}h) -- treating as no price data.")
        return empty

    window = hist.tail(hours)
    if window.empty:
        return empty

    return {
        "ticker_used": ticker,
        "ohlc_open": round(float(window["Open"].iloc[0]), 4),
        "ohlc_high": round(float(window["High"].max()), 4),
        "ohlc_low": round(float(window["Low"].min()), 4),
        "ohlc_close": round(float(window["Close"].iloc[-1]), 4),
        "ohlc_volume": float(window["Volume"].sum()) if "Volume" in window else None,
        "ohlc_bars_used": len(window),
    }


def fetch_bucket_price(bucket: dict, session) -> dict:
    """One call per bucket: resolves the ticker (Gold's liquid-contract logic
    or a static ticker), then fetches its trailing OHLC window."""
    ticker = resolve_ticker(bucket, session)
    return fetch_ohlc_window(ticker, session)
