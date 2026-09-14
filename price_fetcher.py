"""OHLC price fetching for each bucket's instrument, via yfinance.

Two things are ported directly from the existing gold basis pipeline
(gold-data-pipelines area), not reinvented:

  1. The curl_cffi impersonated session -- yfinance has previously needed
     this to work around a session/auth error. Every fetch in this module
     goes through the same kind of session for that reason.
  2. get_liquid_gold_contract() -- picks whichever near-term COMEX gold
     contract currently has the highest trading volume, instead of the
     generic "GC=F" continuous ticker. This is Gold-specific and NOT
     generalized to Oil or other commodities here: the month-code and
     exchange-suffix conventions differ per exchange (COMEX vs NYMEX vs
     ICE), and guessing at those for instruments this hasn't been tested
     against would risk silently fetching the wrong contract. Oil, USD,
     the indices, and Bitcoin all use a single static continuous ticker
     instead (see config.py's "ticker_resolution" field per bucket).

This is a lighter-weight tool than the gold pipeline's settlement-aligned
sampling: it takes a best-effort trailing-N-hourly-bars OHLC snapshot for
directional context on a dashboard, not a precisely-timed settlement price
for a basis/arbitrage calculation. Don't treat the numbers here as
settlement-grade -- they aren't trying to be.
"""
from datetime import datetime, timezone

import yfinance as yf
from curl_cffi import requests as curl_requests

HOURS_PER_WINDOW = 4
GOLD_MONTHS_AHEAD = 8


def make_session():
    """A single impersonated session, reused across all fetches in one run."""
    return curl_requests.Session(impersonate="chrome")


def get_liquid_gold_contract(session, months_ahead: int = GOLD_MONTHS_AHEAD) -> str:
    """
    Checks the next several month-specific COMEX gold contracts and returns
    whichever currently has the highest trading volume. Ported from the gold
    basis pipeline's get_most_liquid_gold_contract() -- same logic, same
    fallback to "GC=F" if contract volumes can't be resolved.
    """
    month_codes = ["F", "G", "H", "J", "K", "M", "N", "Q", "U", "V", "X", "Z"]
    today = datetime.now()

    candidates = []
    for i in range(months_ahead):
        offset = today.month - 1 + i
        year = today.year + offset // 12
        month = offset % 12 + 1
        yy = str(year)[-2:]
        candidates.append(f"GC{month_codes[month - 1]}{yy}.CMX")

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
        print("[WARN] Could not resolve gold contract volumes, falling back to GC=F.")
        return "GC=F"

    print(f"[INFO] Most liquid GC contract: {best_symbol} (volume={best_volume:.0f})")
    return best_symbol


def resolve_ticker(bucket: dict, session) -> str:
    """Returns the actual ticker to fetch for this bucket, applying the
    per-bucket resolution strategy set in config.py."""
    if bucket.get("ticker_resolution") == "gold_liquid_contract":
        return get_liquid_gold_contract(session)
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
