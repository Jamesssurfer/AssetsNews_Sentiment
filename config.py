"""Bucket + query + ticker configuration for the asset sentiment scanner.

Each bucket tracks ONE tradable instrument via a bullish/bearish query split,
plus OHLC price data for that same instrument. This is deliberately a
different shape from the geopolitical-risk scanner's buckets: there, a single
bucket ("Middle East Oil Risk") had no clean 1:1 mapping to a single price
series. Here, every bucket IS a single instrument, so pairing it with that
instrument's own price is a coherent design, not the DXY-conflation mistake
that got rejected for the other repo.

Why bullish_queries AND bearish_queries, not just one: this is the same
problem the risk/deescalation split solved before. A bullish-only query set
would just go quiet during a genuine bear market -- reading as "no news"
instead of "bearish." The bearish side is what lets an actual downturn
register as negative rather than as silence.

Each bucket also sets "sentiment_model" (finbert or vader), same mechanism as
the other repo. Default is "finbert" for every bucket here, on purpose: this
is financial-market language (analyst notes, ETF flows, rally/selloff
reporting), which is FinBERT's actual training domain -- unlike militarized
geopolitical language, where FinBERT read events backwards. That said, this
default has NOT been empirically checked against real output the way the
other repo's model choice was (see its README's model-comparison table).
Do that check once you have a few real runs -- Bitcoin coverage in particular
mixes analyst tone with more informal/social crypto-native language and is
the one most likely to need a second look.
"""

MAX_ARTICLES_PER_QUERY = 8

BUCKETS = {
    "gold": {
        "label": "Gold",
        "sentiment_model": "finbert",
        "ticker": "GC=F",  # placeholder -- price_fetcher resolves the actual
                            # most-liquid COMEX contract at fetch time, same
                            # logic ported from the gold basis pipeline.
        "ticker_resolution": "gold_liquid_contract",
        "bullish_queries": [
            "gold price rally",
            "gold hits record high",
            "gold surges safe haven demand",
            "gold breaks resistance",
            "central bank gold buying",
        ],
        "bearish_queries": [
            "gold price drop",
            "gold sell-off",
            "gold falls profit taking",
            "gold breaks support",
            "gold ETF outflows",
        ],
    },
    "oil": {
        "label": "Oil",
        "sentiment_model": "finbert",
        "ticker": "CL=F",
        "ticker_resolution": "static",
        "bullish_queries": [
            "oil price rally",
            "oil surges supply cut",
            "OPEC production cut",
            "crude oil rises demand",
            "oil breaks resistance",
        ],
        "bearish_queries": [
            "oil price drop",
            "oil falls oversupply",
            "crude oil sell-off",
            "OPEC production increase",
            "oil breaks support",
        ],
    },
    "us_dollar": {
        "label": "US Dollar",
        "sentiment_model": "finbert",
        "ticker": "DX-Y.NYB",  # ICE US Dollar Index. "DX=F" (originally used
                                # here) doesn't exist as a Yahoo symbol at all
                                # (confirmed via a live 404: "Quote not found
                                # for symbol: DX=F") -- switched to this after
                                # checking Yahoo Finance directly. The
                                # cash-hours-gap concern that motivated using
                                # a futures ticker for the two equity indices
                                # below doesn't apply to DXY the same way: it's
                                # calculated from a continuously-traded FX
                                # basket, not tied to a single exchange's cash
                                # session, and shows active intraday quotes.
        "ticker_resolution": "static",
        "bullish_queries": [
            "dollar strengthens",
            "DXY rally",
            "dollar index rises",
            "dollar safe haven demand",
            "Fed hawkish dollar",
        ],
        "bearish_queries": [
            "dollar weakens",
            "DXY falls",
            "dollar index drops",
            "Fed dovish dollar",
            "dollar sell-off",
        ],
    },
    "sp500": {
        "label": "S&P 500",
        "sentiment_model": "finbert",
        "ticker": "ES=F",  # E-mini S&P futures, not ^GSPC/SPY -- the cash
                            # index only trades ~13:30-20:00 UTC, so most of
                            # this scanner's 4-hourly windows would otherwise
                            # show empty OHLC outside that one daily window.
        "ticker_resolution": "static",
        "bullish_queries": [
            "S&P 500 record high",
            "S&P 500 rally",
            "stock market surges",
            "Wall Street rally",
            "stocks break resistance",
        ],
        "bearish_queries": [
            "S&P 500 falls",
            "S&P 500 sell-off",
            "stock market decline",
            "Wall Street drops",
            "stocks break support",
        ],
    },
    "nasdaq": {
        "label": "Nasdaq",
        "sentiment_model": "finbert",
        "ticker": "NQ=F",  # E-mini Nasdaq futures, same reasoning as ES=F
                            # above -- covers QQQ-adjacent sentiment without
                            # the cash-hours gap.
        "ticker_resolution": "static",
        "bullish_queries": [
            "Nasdaq record high",
            "Nasdaq rally",
            "tech stocks surge",
            "Nasdaq breaks resistance",
            "tech stocks rally",
        ],
        "bearish_queries": [
            "Nasdaq falls",
            "Nasdaq sell-off",
            "tech stocks decline",
            "Nasdaq breaks support",
            "tech stocks drop",
        ],
    },
    "bitcoin": {
        "label": "Bitcoin",
        "sentiment_model": "finbert",
        "ticker": "BTC-USD",  # trades 24/7 -- no session-gap issue at all,
                               # simplest case of the six.
        "ticker_resolution": "static",
        "bullish_queries": [
            "bitcoin price rally",
            "bitcoin surges",
            "bitcoin all-time high",
            "crypto market rally",
            "bitcoin institutional buying",
        ],
        "bearish_queries": [
            "bitcoin price drop",
            "bitcoin crashes",
            "bitcoin sell-off",
            "crypto market decline",
            "bitcoin liquidations",
        ],
    },
}
