# Asset Sentiment & Price Scanner

Companion to the geopolitical-risk news scanner, but asset-native: six
buckets, each tracking bullish/bearish news sentiment AND a trailing 4-hour
OHLC price snapshot for one specific tradable instrument.

- **Gold** (COMEX, most-liquid contract auto-resolved)
- **Oil** (`CL=F`)
- **US Dollar** (`DX=F`, dollar index futures)
- **S&P 500** (`ES=F`, E-mini futures)
- **Nasdaq** (`NQ=F`, E-mini futures)
- **Bitcoin** (`BTC-USD`)

## Why this repo is separate from the geopolitical-risk scanner

That repo's buckets (Middle East Oil Risk, Macro Interventions, Trade Wars)
don't map cleanly to a single price series each — DXY alone is driven by Fed
policy, BoJ policy, *and* trade tensions at once, so treating its price as a
stand-in for any one of those buckets would conflate three different things
into one number. This repo exists specifically because it doesn't have that
problem: every bucket here IS one instrument, so pairing it with that
instrument's own price is coherent instead of a proxy hack.

**Important limit to keep in mind: adding price doesn't make this a
validated trading signal.** With a handful of assets and a few weeks of
data, "the score went positive and the price went up the next day" is an
anecdote, not a backtest. Treat the price column as descriptive context to
look back on in a few months, not confirmation that the system works after
a couple of good calls.

## How it works

- `config.py` — the six buckets, their `bullish_queries` / `bearish_queries`
  query sets, sentiment model choice, and ticker. Same reasoning as the other
  repo's risk/de-escalation split: a bullish-only query set would just go
  quiet in a real downturn, reading as "no news" instead of "bearish" — the
  bearish side is what lets an actual downturn register as negative.
- `price_fetcher.py` — OHLC price fetching via yfinance. Two things ported
  directly from the existing gold basis pipeline (`gold-data-pipelines`):
  the `curl_cffi` impersonated session (yfinance has previously needed this
  to work around a session/auth error), and the most-liquid-COMEX-contract
  resolution logic, used for Gold only. See "Ticker choices" below for why
  it isn't generalized to the other five instruments.
- `news_fetcher.py`, `sentiment_engine.py` — identical to the geopolitical
  repo, copied over unchanged (Google News RSS + trafilatura lede extraction,
  dual FinBERT/VADER scoring).
- `scanner.py` — one scan cycle: fetch + score both query groups, fetch the
  OHLC window, compute a composite score, append one row per bucket.
- `daily_rollup.py` — rebuilds a `Daily` sheet: one row per (date, bucket),
  with day-over-day % change as a live Excel formula, same pattern as the
  other repo.
- `dashboard.py` — renders `index.html`: per-bucket composite score, OHLC
  price line, a 4-day/24-run trend chart, top 3 bullish headlines, top 3
  bearish headlines, and a daily history table.

## Composite score

```
bullish_weight = bullish_articles * (0.5 + bullish_score_avg / 2)
bearish_weight = bearish_articles * (0.5 - bearish_score_avg / 2)
composite      = bullish_weight - bearish_weight
```

Positive = net bullish pressure. Negative = net bearish pressure. Zero = quiet
on both sides. Same v1-heuristic caveat as the other repo — reasonable
starting weights, not a validated model.

## Which sentiment model, per bucket

All six buckets default to **FinBERT**, not VADER — the opposite default from
the geopolitical repo, and on purpose. That repo's buckets are militarized/
policy language, which FinBERT reads backwards (it's trained on financial-
phrasebank/analyst-report tone). This repo's queries — rallies, sell-offs,
ETF flows, Fed commentary — are much closer to FinBERT's actual training
domain.

**This has not been empirically checked against real output**, unlike the
other repo's model table, which was built by looking at actual disagreement
between FinBERT and VADER on real headlines. Do that same check once you
have a few real runs: pull up `bullish_finbert_avg` vs `bullish_vader_avg` in
the log and see if they broadly agree. Bitcoin is the one most likely to need
a second look — crypto coverage swings between analyst tone and more
informal, social-media-adjacent language more than the other five.

## Ticker choices

| Bucket | Ticker | Why |
|---|---|---|
| Gold | Auto-resolved COMEX contract | Ported from the gold basis pipeline: whichever near-term contract has the highest volume right now, not a generic continuous ticker. |
| Oil | `CL=F` | Static continuous ticker — the liquid-contract resolution logic is Gold-specific (COMEX month codes and exchange suffix) and wasn't generalized here to avoid guessing at conventions for NYMEX that haven't been verified. |
| US Dollar | `DX=F` | Futures, not the cash `DX-Y.NYB` index — same reasoning as the index futures below. |
| S&P 500 | `ES=F` | E-mini futures, not `^GSPC`/`SPY` — the cash index only trades ~13:30–20:00 UTC, so most of this scanner's 4-hourly windows would otherwise show empty OHLC. |
| Nasdaq | `NQ=F` | Same reasoning as `ES=F`. |
| Bitcoin | `BTC-USD` | Trades 24/7 — no session-gap issue at all. |

## Known limitations

- **Yahoo Finance has no native 4-hour interval.** `price_fetcher.py` pulls
  hourly bars and resamples the trailing 4 into one OHLC bar (open = first
  hour's open, high/low = max/min across the four, close = last hour's
  close). This is a best-effort directional snapshot, **not** the
  settlement-grade precision the gold basis pipeline uses for its actual
  basis/arbitrage calculation — don't hold these OHLC numbers to that bar.
- **Futures still have session gaps, just far fewer than cash equities.**
  Even `ES=F`/`NQ=F`/`DX=F` have brief daily maintenance windows. A run that
  lands in one will show `ohlc_bars_used` less than 4, or all-`None` if the
  window was fully closed — the dashboard shows "No price data this window"
  rather than a misleading zero.
- **This deliberately does NOT include the holiday-awareness fix
  (`pandas_market_calendars`) agreed for the gold basis pipeline.** That fix
  addresses a different problem — a multi-day rolling *cache* silently
  losing or endlessly re-attempting historical days. This scanner only ever
  takes a single trailing-4-hour snapshot per run with no cache/backfill
  logic, so a holiday or early-close here just means `ohlc_bars_used` comes
  back low or zero for that one run — expected, not silently wrong. That fix
  is explicitly out of scope for this repo and was agreed to be handled
  separately, for the gold pipeline itself, in a different conversation.
- **`yfinance` is an unofficial library that scrapes Yahoo's backend**, the
  same caveat that motivated the `curl_cffi` session fix in the first place —
  it can break when Yahoo changes something, with no advance notice. Worth
  knowing as a standing maintenance risk, same as Google News scraping in
  the other repo.
- **New columns only ever get added at the end of the schema**, same
  append-only migration discipline as the geopolitical repo (`scanner.py`'s
  `_migrate_headers`) — a lesson learned there the hard way.
- Everything else — Excel-append mechanics, GitHub Actions setup, the
  4-hour/daily schedule, HF model caching — mirrors the geopolitical repo
  exactly. See that repo's README for the reasoning if anything here is
  unclear.

## Setting it up

Same steps as the other repo:

1. Push this repo to GitHub on `main`.
2. Settings → Actions → General → Workflow permissions → **Read and write**.
   Without this, the workflows run the scan but fail silently on the
   `git push` step.
3. Trigger a first run manually: **Actions → Scan asset sentiment → Run
   workflow**. Confirm `data/asset_sentiment_log.xlsx` and `index.html` show
   up afterward.
4. (Optional) Enable GitHub Pages pointed at the root of `main`.

No secrets or API keys needed — Google News, article pages, and Yahoo
Finance are all fetched anonymously.
