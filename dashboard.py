"""Render index.html from the asset sentiment log. Run after scanner.py /
daily_rollup.py.

Layout: one tab per asset under the header, Daily history pinned at the foot
(outside the tabs, always visible). Each asset tab has the existing 24-run and
daily composite charts, a divergence status box, and a full-width daily
composite-vs-price chart.

Self-contained static HTML -- Chart.js is vendored in vendor/chart.umd.js
rather than loaded from a CDN (the geopolitical repo hit a real case of a
CDN being silently blocked on a real network, with the chart just staying
blank and no visible error -- self-hosting removes that failure mode).

Divergence status (descriptive, NOT a validated or contrarian signal):
  * Daily score  = Daily-sheet composite_avg (successful scans only); a date
    missing from Daily is filled from Log with the same rule.
  * Daily price  = last non-null ohlc_close of that date in Log.
  * Trading days only: Mon-Fri for everything except Bitcoin (7 days). A date
    counts only if BOTH a score and a close exist, so both series always share
    the same dates.
  * Change = SIGNAL_WINDOW trading days: score as absolute change, price as %.
  * "Normal range" = SIGNAL_SIGMA x std dev of that asset's own history of
    those changes (computed on the same aligned dates, latest value included).
  * Status is computed in compute_signal(); thresholds live in config.py.
"""
import json
import re
from collections import OrderedDict
from pathlib import Path

import numpy as np
import pandas as pd

from config import (
    BUCKETS,
    SIGNAL_MIN_CHANGES,
    SIGNAL_MIN_CORR_DAYS,
    SIGNAL_MIN_DAYS,
    SIGNAL_SIGMA,
    SIGNAL_WINDOW,
    STATUS_NOTES,
)

LOG_PATH = Path(__file__).parent / "data" / "asset_sentiment_log.xlsx"
OUTPUT_PATH = Path(__file__).parent / "index.html"

BUCKET_COLORS = {
    "Gold": "#C9A227",
    "Oil": "#8A5A3B",
    "US Dollar": "#4C9F70",
    "S&P 500": "#4C8CA8",
    "Nasdaq": "#8B5FBF",
    "Bitcoin": "#E8871E",
}

BUCKET_BY_LABEL = {b["label"]: b for b in BUCKETS.values()}

POINTS_PER_RUN_CHART = 24  # rolling 4 days at 6 runs/day
DAILY_CHART_DAYS = 30  # daily-score chart per bucket -- shows fewer if less history exists yet
PRICE_CHART_DAYS = 30  # calendar-day lookback for the composite-vs-price chart

STATUS_COLORS = {
    "Confirming": "#4C9F70",
    "Diverging": "#E0A030",
    "No signal": "#8A8F9C",
    "Not enough data": "#8A8F9C",
    "No price data": "#C9524A",
}


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")


def _fmt_score(x) -> str:
    return f"{x:+.2f}" if pd.notna(x) else "n/a"


def _fmt_price(x) -> str:
    return f"{x:,.2f}" if pd.notna(x) else "n/a"


def _fmt_pct(x) -> str:
    if x is None or (isinstance(x, float) and pd.isna(x)) or x == "n/a" or x == "":
        return "n/a"
    try:
        return f"{float(x) * 100:+.1f}%"
    except (TypeError, ValueError):
        return "n/a"


def _clean(values, ndigits=4) -> list:
    """JSON-safe list: NaN / None -> null (a gap in the chart), never 0."""
    return [None if (v is None or pd.isna(v)) else round(float(v), ndigits) for v in values]


# ---------------------------------------------------------------------------
# Daily score / price series and the divergence status
# ---------------------------------------------------------------------------

def _log_scores(log_df: pd.DataFrame) -> pd.Series:
    """Daily mean composite per (date, bucket) from Log, successful scans only
    -- same rule as daily_rollup.py. Used to fill dates the Daily sheet does
    not have yet (rollup runs less often than the scanner)."""
    df = log_df.copy()
    df["date"] = df["date"].astype(str).str[:10]
    articles = df["bullish_articles"].fillna(0) + df["bearish_articles"].fillna(0)
    scan_total = articles.groupby(df["timestamp_utc"]).transform("sum")
    good = df[scan_total > 0]
    return good.groupby(["date", "bucket"])["composite_score"].mean().round(3)


def _asset_frame(label: str, log_df: pd.DataFrame, daily_df: pd.DataFrame, log_scores: pd.Series) -> pd.DataFrame:
    """Trading-day frame for one asset: columns score, close, ticker. Index is
    every trading date from the first to the last date in Log (a date with no
    data at all stays in the index as NaN, so 'N trading days back' is exact)."""
    rows = log_df[log_df["bucket"] == label].copy()
    if rows.empty:
        return pd.DataFrame(columns=["score", "close", "ticker"])
    rows["date"] = rows["date"].astype(str).str[:10]
    rows = rows.sort_values("timestamp_utc")

    priced = rows[rows["ohlc_close"].notna()]
    last_px = priced.groupby("date").tail(1).set_index("date")
    close = last_px["ohlc_close"].astype(float)
    ticker = last_px["ticker_used"].astype(str)

    score = log_scores.xs(label, level="bucket") if label in log_scores.index.get_level_values("bucket") else pd.Series(dtype=float)
    if not daily_df.empty:
        d = daily_df[daily_df["bucket"] == label].copy()
        d["date"] = d["date"].astype(str).str[:10]
        d_score = d.set_index("date")["composite_avg"].astype(float).dropna()
        score = d_score.combine_first(score)

    first, last = rows["date"].min(), rows["date"].max()
    weekends = BUCKET_BY_LABEL[label].get("trades_weekends", False)
    idx = pd.date_range(first, last, freq="D") if weekends else pd.bdate_range(first, last)
    keys = idx.strftime("%Y-%m-%d")

    frame = pd.DataFrame(index=idx)
    frame["score"] = score.reindex(keys).to_numpy(dtype=float)
    frame["close"] = close.reindex(keys).to_numpy(dtype=float)
    frame["ticker"] = ticker.reindex(keys).to_numpy(dtype=object)
    return frame


def compute_signal(frame: pd.DataFrame, expected_sign: int) -> dict:
    """Pre-defined divergence status. Returns dict(status, label, as_of, lines)."""
    W = SIGNAL_WINDOW
    k = SIGNAL_SIGMA
    result = {"status": "Not enough data", "label": "Not enough data", "as_of": "n/a", "lines": [], "direction": ""}
    sign_word = "rises" if expected_sign > 0 else "falls"
    sign_line = f"Expected sign {expected_sign:+d} (price {sign_word} with the score)"

    if frame.empty or len(frame) <= W:
        result["lines"] = [f"Need more than {W} trading days of history."]
        return result

    last_d, prior_d = frame.index[-1], frame.index[-1 - W]
    result["as_of"] = last_d.strftime("%Y-%m-%d")

    valid = frame["score"].notna() & frame["close"].notna()
    n_days = int(valid.sum())
    s = frame["score"].where(valid)
    p = frame["close"].where(valid)
    tk = frame["ticker"]

    # 1. Price missing at either end of the latest window -> never a silent "No signal".
    missing = [d.strftime("%Y-%m-%d") for d in (prior_d, last_d) if pd.isna(frame["close"][d])]
    if missing:
        result.update(status="No price data", label="No price data")
        result["lines"] = [f"No close logged for {', '.join(missing)} (yfinance failure, closed market or holiday).", sign_line]
        return result

    # 2. Contract changed between the two window ends -> price change not comparable.
    if tk[prior_d] != tk[last_d]:
        result.update(status="No price data", label="No price data (contract roll)")
        result["lines"] = [
            f"Contract changed inside the window: {tk[prior_d]} on {prior_d:%Y-%m-%d} vs {tk[last_d]} on {last_d:%Y-%m-%d}.",
            sign_line,
        ]
        return result

    # 3. Enough aligned history?
    if n_days < SIGNAL_MIN_DAYS:
        result["lines"] = [f"{n_days} of {SIGNAL_MIN_DAYS} aligned trading days (score + price) needed before a status is shown.", sign_line]
        return result

    if pd.isna(frame["score"][prior_d]) or pd.isna(frame["score"][last_d]):
        gone = [d.strftime("%Y-%m-%d") for d in (prior_d, last_d) if pd.isna(frame["score"][d])]
        result["lines"] = [f"No score for {', '.join(gone)} (all scans failed or not rolled up yet).", sign_line]
        return result

    # 4. Changes on shared trading dates; changes spanning a contract roll are excluded from the history.
    ds = s - s.shift(W)
    dp = p / p.shift(W) - 1
    dp = dp.where(tk == tk.shift(W))
    pairs = ds.notna() & dp.notna()
    if int(pairs.sum()) < SIGNAL_MIN_CHANGES:
        result["lines"] = [f"Only {int(pairs.sum())} comparable {W}-day changes (need {SIGNAL_MIN_CHANGES}).", sign_line]
        return result

    sd_s, sd_p = float(ds[pairs].std()), float(dp[pairs].std())
    ds_now, dp_now = float(ds[last_d]), float(dp[last_d])
    out_s = abs(ds_now) > k * sd_s
    out_p = abs(dp_now) > k * sd_p
    aligned = np.sign(ds_now) * np.sign(dp_now) * expected_sign

    if out_s and out_p and aligned > 0:
        status = "Confirming"
    elif out_s and out_p and aligned < 0:
        status = "Diverging"
        result["direction"] = f"Score {'rising' if ds_now > 0 else 'falling'} while price {'rising' if dp_now > 0 else 'falling'}."
    else:
        status = "No signal"
    result.update(status=status, label=status)

    corr_line = f"Realized correlation insufficient data ({n_days} of {SIGNAL_MIN_CORR_DAYS} days)"
    if n_days >= SIGNAL_MIN_CORR_DAYS:
        pos = np.arange(len(frame) - 1, -1, -W)  # non-overlapping windows, counted back from latest
        sub = pd.DataFrame({"ds": ds, "dp": dp}).iloc[pos].dropna()
        if len(sub) >= 5:
            corr_line = f"Realized correlation {sub['ds'].corr(sub['dp']):+.2f} over {len(sub)} non-overlapping {W}-day windows"

    result["lines"] = [
        f"Score {W}-day change {ds_now:+.2f} (normal range ±{k * sd_s:.2f}) · Price {W}-day change {dp_now * 100:+.2f}% (normal range ±{k * sd_p * 100:.2f}%)",
        f"{sign_line} · {corr_line}",
    ]
    return result


def _signal_html(sig: dict) -> str:
    base = "Diverging" if sig["status"] == "Diverging" else sig["status"]
    color = STATUS_COLORS.get(base, "#8A8F9C")
    note = STATUS_NOTES.get(base, "")
    direction = f" {sig['direction']}" if sig["direction"] else ""
    lines = "".join(f"<div>{ln}</div>" for ln in sig["lines"])
    return f"""
      <div class="signal" style="--sig: {color}">
        <div class="signal-head">
          <span class="status">{sig['label']}</span>
          <span class="muted small">as of {sig['as_of']} · {SIGNAL_WINDOW}-trading-day windows, normal range = {SIGNAL_SIGMA:g} std dev of own history</span>
        </div>
        <div class="signal-note">{note}{direction}</div>
        <div class="signal-lines mono">{lines}</div>
        <div class="muted small signal-caveat">Descriptive context, not a tested or contrarian signal. News tends to trail price, and with this little history the normal range is a rough estimate.</div>
      </div>
    """


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------

def _bucket_panel_html(bucket: str, log_df: pd.DataFrame, daily_df: pd.DataFrame, log_scores: pd.Series, active: bool) -> str:
    rows = log_df[log_df["bucket"] == bucket].sort_values("timestamp_utc")
    if rows.empty:
        return ""

    slug = _slug(bucket)
    latest = rows.iloc[-1]
    prior = rows.iloc[-2] if len(rows) > 1 else None
    color = BUCKET_COLORS.get(bucket, "#8A8F9C")

    delta = None
    if prior is not None:
        delta = latest["composite_score"] - prior["composite_score"]
    delta_html = ""
    if delta is not None:
        arrow = "▲" if delta > 0 else ("▼" if delta < 0 else "—")
        delta_html = f'<span class="delta">{arrow} {delta:+.2f} vs prior run</span>'

    model = latest.get("sentiment_model_used", "finbert")
    model = model if isinstance(model, str) and model in ("vader", "finbert") else "finbert"
    bullish_tone = latest["bullish_vader_avg"] if model == "vader" else latest["bullish_finbert_avg"]
    bearish_tone = latest["bearish_vader_avg"] if model == "vader" else latest["bearish_finbert_avg"]

    ohlc_bars = latest.get("ohlc_bars_used", 0)
    if pd.notna(ohlc_bars) and ohlc_bars and pd.notna(latest.get("ohlc_close")):
        price_line = (
            f"O {_fmt_price(latest['ohlc_open'])} · H {_fmt_price(latest['ohlc_high'])} · "
            f"L {_fmt_price(latest['ohlc_low'])} · C {_fmt_price(latest['ohlc_close'])} "
            f"<span class=\"muted\">({latest['ticker_used']}, {int(ohlc_bars)}h bars)</span>"
        )
    else:
        price_line = f"<span class=\"muted\">No price data this window (market likely closed) &mdash; {latest.get('ticker_used', 'n/a')}</span>"

    def _headline_list(prefix: str) -> str:
        items = []
        for i in (1, 2, 3):
            title = latest.get(f"{prefix}{i}", "")
            url = latest.get(f"{prefix}{i}_url", "")
            if isinstance(title, str) and title.strip():
                items.append(f'<li><a href="{url}" target="_blank" rel="noopener">{title}</a></li>')
        return "\n".join(items) if items else "<li class=\"muted\">No articles this run.</li>"

    bullish_headlines_html = _headline_list("bullish_top_headline_")
    bearish_headlines_html = _headline_list("bearish_top_headline_")

    # 24-run chart: a run with zero articles is a failed fetch -> a gap, not a 0.
    chart_rows = rows.tail(POINTS_PER_RUN_CHART)
    chart_times = pd.to_datetime(chart_rows["timestamp_utc"])
    run_articles = chart_rows["bullish_articles"].fillna(0) + chart_rows["bearish_articles"].fillna(0)
    run_scores = chart_rows["composite_score"].where(run_articles > 0)
    chart_labels = json.dumps(chart_times.dt.strftime("%m-%d %Hh").tolist())
    chart_values = json.dumps(_clean(run_scores.tolist(), 3))
    canvas_id = f"chart-4h-{slug}"

    daily_rows = daily_df[daily_df["bucket"] == bucket].sort_values("date").tail(DAILY_CHART_DAYS) if not daily_df.empty else daily_df
    daily_labels = json.dumps(daily_rows["date"].astype(str).tolist()) if not daily_rows.empty else "[]"
    daily_values = json.dumps(_clean(daily_rows["composite_avg"].tolist(), 3)) if not daily_rows.empty else "[]"
    daily_canvas_id = f"chart-daily-{slug}"

    # Divergence status + composite-vs-price chart (trading days only, shared dates).
    frame = _asset_frame(bucket, log_df, daily_df, log_scores)
    sig = compute_signal(frame, BUCKET_BY_LABEL[bucket].get("expected_sign", 1))
    signal_html = _signal_html(sig)

    if frame.empty:
        win = frame
    else:
        win = frame[frame.index > frame.index[-1] - pd.Timedelta(days=PRICE_CHART_DAYS)]
    px_labels = json.dumps(win.index.strftime("%Y-%m-%d").tolist())
    px_score = json.dumps(_clean(win["score"].tolist(), 3))
    px_close = json.dumps(_clean(win["close"].tolist(), 4))
    px_ticker = json.dumps([None if pd.isna(t) else str(t) for t in win["ticker"].tolist()])
    price_canvas_id = f"chart-price-{slug}"

    init_js = (
        f'TAB_INITS["{slug}"] = function () {{\n'
        f'  lineChart("{canvas_id}", {chart_labels}, {chart_values}, "{color}");\n'
        f'  lineChart("{daily_canvas_id}", {daily_labels}, {daily_values}, "{color}");\n'
        f'  priceChart("{price_canvas_id}", {px_labels}, {px_score}, {px_close}, {px_ticker}, "{color}");\n'
        f'}};\n'
    )

    return f"""
    <section class="panel tab-panel{' active' if active else ''}" id="tab-{slug}" style="--accent: {color}">
      <div class="panel-head">
        <h2>{bucket}</h2>
        <span class="timestamp">as of {latest['timestamp_utc']}</span>
      </div>

      <div class="panel-body">
        <div class="panel-left">
          <div class="score-row">
            <div class="score">{_fmt_score(latest['composite_score'])}</div>
            {delta_html}
          </div>

          <div class="price-line">{price_line}</div>

          <div class="breakdown">
            <div>
              <span class="label">Bullish coverage</span>
              <span class="value">{int(latest['bullish_articles'])} articles, tone {_fmt_score(bullish_tone)} ({model})</span>
            </div>
            <div>
              <span class="label">Bearish coverage</span>
              <span class="value">{int(latest['bearish_articles'])} articles, tone {_fmt_score(bearish_tone)} ({model})</span>
            </div>
          </div>

          <div class="headlines">
            <span class="label">Top bullish headlines this run</span>
            <ul>{bullish_headlines_html}</ul>
          </div>

          <div class="headlines">
            <span class="label">Top bearish headlines this run</span>
            <ul>{bearish_headlines_html}</ul>
          </div>
        </div>

        <div class="panel-right">
          <span class="label chart-label">Composite score, last {len(chart_rows)} runs (~4 days)</span>
          <canvas class="trend-chart" id="{canvas_id}" height="190"></canvas>

          <span class="label chart-label">Daily composite score, last {len(daily_rows)} days</span>
          <canvas class="trend-chart" id="{daily_canvas_id}" height="190"></canvas>
        </div>
      </div>

      {signal_html}

      <div class="price-chart-wrap">
        <span class="label chart-label">Daily composite vs price, last {PRICE_CHART_DAYS} days ({'7-day' if BUCKET_BY_LABEL[bucket].get('trades_weekends') else 'trading days only'})</span>
        <canvas class="price-chart" id="{price_canvas_id}" height="110"></canvas>
      </div>
      <script>
{init_js}      </script>
    </section>
    """


def _grouped_daily_history_html(daily_df: pd.DataFrame, buckets: list) -> str:
    """Nested <details> dropdowns: Quarter > Week, most recent first, with the
    latest date in each week on top. Plain HTML/CSS collapsibles -- no JS
    needed, consistent with the rest of this self-contained page."""
    if daily_df.empty:
        return "<p class=\"muted\">No daily rollup yet.</p>"

    df = daily_df.copy()
    df["date_dt"] = pd.to_datetime(df["date"])
    header_cells = "".join(f"<th>{b}</th>" for b in buckets)

    unique_dates = sorted(df["date_dt"].unique(), reverse=True)  # latest first

    quarters: "OrderedDict[str, OrderedDict[str, list]]" = OrderedDict()
    for d in unique_dates:
        d_ts = pd.Timestamp(d)
        quarter_label = f"{d_ts.year} Q{((d_ts.month - 1) // 3) + 1}"
        week_start = d_ts - pd.Timedelta(days=d_ts.weekday())  # Monday
        week_end = week_start + pd.Timedelta(days=6)
        week_label = f"Week of {week_start:%b %d} \u2013 {week_end:%b %d}"
        quarters.setdefault(quarter_label, OrderedDict()).setdefault(week_label, []).append(d_ts)

    quarter_blocks = []
    for qi, (quarter_label, weeks) in enumerate(quarters.items()):
        week_blocks = []
        for wi, (week_label, dates) in enumerate(weeks.items()):
            row_html = []
            for d_ts in dates:  # already latest-first
                date_str = d_ts.strftime("%Y-%m-%d")
                cells = []
                for bucket in buckets:
                    match = df[(df["date_dt"] == d_ts) & (df["bucket"] == bucket)]
                    if match.empty:
                        cells.append("<td class=\"muted\">\u2014</td>")
                        continue
                    r = match.iloc[0]
                    cells.append(
                        f"<td>{_fmt_score(r['composite_avg'])} "
                        f"<span class=\"muted\">({_fmt_pct(r['composite_pct_change_vs_prev_day'])})</span></td>"
                    )
                row_html.append(f"<tr><td>{date_str}</td>{''.join(cells)}</tr>")

            week_open = " open" if (qi == 0 and wi == 0) else ""
            week_blocks.append(f"""
            <details class="week-group"{week_open}>
              <summary>{week_label}</summary>
              <table class="daily-table">
                <thead><tr><th>Date</th>{header_cells}</tr></thead>
                <tbody>{''.join(row_html)}</tbody>
              </table>
            </details>
            """)

        quarter_open = " open" if qi == 0 else ""
        quarter_blocks.append(f"""
        <details class="quarter-group"{quarter_open}>
          <summary>{quarter_label}</summary>
          {''.join(week_blocks)}
        </details>
        """)

    return "\n".join(quarter_blocks) + """
    <p class="muted small">Value shown is the day's average composite score; the figure in
    parentheses is the change versus the prior day for that bucket.</p>
    """




CSS = """
  :root {
    --bg: #14161c;
    --panel: #1b1e27;
    --text: #e7e5de;
    --muted: #8a8f9c;
    --rule: #2a2e3a;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: "IBM Plex Sans", -apple-system, sans-serif;
    line-height: 1.5;
  }
  .mono { font-family: "IBM Plex Mono", ui-monospace, monospace; }
  header {
    padding: 2rem 1.5rem 1rem;
    max-width: 1280px;
    margin: 0 auto;
    border-bottom: 1px solid var(--rule);
  }
  header h1 { font-size: 1.4rem; font-weight: 600; margin: 0 0 0.3rem; }
  header p { color: var(--muted); margin: 0; font-size: 0.9rem; }
  nav.tabs {
    max-width: 1280px; margin: 0 auto; padding: 1.1rem 1.5rem 0;
    display: flex; gap: 0.5rem; flex-wrap: wrap;
  }
  .tab-btn {
    background: var(--panel); color: var(--muted); border: 1px solid var(--rule);
    border-bottom: 2px solid var(--rule); padding: 0.55rem 1.1rem; cursor: pointer;
    font-family: "IBM Plex Sans", sans-serif; font-size: 0.9rem; font-weight: 500;
  }
  .tab-btn:hover { color: var(--text); }
  .tab-btn.active { color: var(--text); background: #232733; border-bottom-color: var(--accent, var(--muted)); }
  main { max-width: 1280px; margin: 0 auto; padding: 1.5rem; }
  .panel {
    background: var(--panel);
    border-left: 3px solid var(--accent, var(--muted));
    border-top: 1px solid var(--rule);
    border-bottom: 1px solid var(--rule);
    padding: 1.25rem 1.5rem;
    margin-bottom: 1.5rem;
  }
  .tab-panel { display: none; }
  .tab-panel.active { display: block; }
  .panel-head { display: flex; justify-content: space-between; align-items: baseline; flex-wrap: wrap; gap: 0.5rem; }
  .panel-head h2 { font-size: 1.05rem; margin: 0; font-weight: 600; }
  .timestamp { color: var(--muted); font-size: 0.8rem; }
  .panel-body { display: flex; gap: 1.75rem; align-items: flex-start; flex-wrap: wrap; margin-top: 0.75rem; }
  .panel-left { flex: 1 1 280px; min-width: 260px; }
  .panel-right { flex: 1.4 1 460px; min-width: 380px; display: flex; flex-direction: column; }
  .score-row { display: flex; align-items: baseline; gap: 0.75rem; margin: 0 0 0.3rem; }
  .score { font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 2.1rem; font-weight: 600; }
  .delta { color: var(--muted); font-size: 0.85rem; }
  .price-line { font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 0.85rem; margin-bottom: 0.75rem; }
  .breakdown { display: flex; flex-direction: column; gap: 0.25rem; margin-bottom: 0.75rem; font-size: 0.88rem; }
  .breakdown .label { color: var(--muted); margin-right: 0.5rem; }
  .breakdown .value { font-family: "IBM Plex Mono", ui-monospace, monospace; }
  .chart-label { display: block; margin-top: 0.5rem; }
  .trend-chart { width: 100%; max-height: 220px; margin: 0.4rem 0 1.25rem; }
  .price-chart-wrap { margin-top: 1.25rem; padding-top: 0.5rem; border-top: 1px solid var(--rule); }
  .price-chart { width: 100%; max-height: 340px; margin: 0.4rem 0 0.5rem; }
  .signal {
    margin-top: 1.5rem; padding: 0.75rem 1rem; background: #171a21;
    border: 1px solid var(--rule); border-left: 3px solid var(--sig, var(--muted));
  }
  .signal-head { display: flex; align-items: baseline; gap: 0.75rem; flex-wrap: wrap; }
  .signal .status { font-weight: 600; color: var(--sig, var(--text)); }
  .signal-note { font-size: 0.88rem; margin: 0.3rem 0; }
  .signal-lines { font-size: 0.8rem; line-height: 1.6; }
  .signal-caveat { margin-top: 0.4rem; }
  .label { color: var(--muted); font-size: 0.8rem; }
  .headlines { margin-top: 1rem; }
  .headlines .label { color: var(--muted); font-size: 0.8rem; display: block; margin-bottom: 0.3rem; }
  .headlines ul { margin: 0; padding-left: 1.1rem; font-size: 0.9rem; }
  .headlines li { margin-bottom: 0.25rem; }
  .headlines a { color: var(--text); text-decoration: none; border-bottom: 1px solid var(--rule); }
  .headlines a:hover { border-bottom-color: var(--text); }
  .muted { color: var(--muted); }
  .small { font-size: 0.8rem; }
  table.daily-table { width: 100%; border-collapse: collapse; font-size: 0.85rem; margin-top: 0.5rem; }
  table.daily-table th, table.daily-table td {
    text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid var(--rule);
    font-family: "IBM Plex Mono", ui-monospace, monospace;
  }
  table.daily-table th { font-family: "IBM Plex Sans", sans-serif; color: var(--muted); font-weight: 500; }
  details.quarter-group { border-top: 1px solid var(--rule); padding: 0.6rem 0; }
  details.quarter-group summary {
    cursor: pointer; font-weight: 600; font-size: 0.95rem; list-style: none;
    display: flex; align-items: center; gap: 0.4rem;
  }
  details.quarter-group summary::-webkit-details-marker { display: none; }
  details.quarter-group summary::before { content: "▸"; color: var(--muted); font-size: 0.75rem; }
  details.quarter-group[open] > summary::before { content: "▾"; }
  details.week-group { margin: 0.5rem 0 0.5rem 1.25rem; }
  details.week-group summary {
    cursor: pointer; color: var(--muted); font-size: 0.85rem; list-style: none;
    display: flex; align-items: center; gap: 0.4rem; margin-bottom: 0.3rem;
  }
  details.week-group summary::-webkit-details-marker { display: none; }
  details.week-group summary::before { content: "▸"; font-size: 0.7rem; }
  details.week-group[open] > summary::before { content: "▾"; }
  footer { max-width: 1280px; margin: 0 auto; padding: 1rem 1.5rem 2.5rem; color: var(--muted); font-size: 0.78rem; }
"""

# Plain JS (no f-string) so braces need no escaping. Charts are built lazily
# the first time a tab is shown: Chart.js draws a zero-size canvas inside a
# display:none container, so building them all up front would leave the
# hidden tabs blank.
JS = """
const TAB_INITS = {};
const TAB_DONE = {};
const MONO = { family: "IBM Plex Mono", size: 10 };

function lineChart(id, labels, values, color) {
  new Chart(document.getElementById(id), {
    type: "line",
    data: {
      labels: labels,
      datasets: [
        { data: values, borderColor: color, borderWidth: 1.5, pointRadius: 2, pointBackgroundColor: color, tension: 0.15, fill: false, spanGaps: false },
        { data: labels.map(function () { return 0; }), borderColor: "#8a8f9c", borderWidth: 1, borderDash: [4, 4], pointRadius: 0, fill: false },
      ],
    },
    options: {
      responsive: true,
      plugins: { legend: { display: false }, tooltip: { enabled: true, mode: "index", intersect: false } },
      scales: {
        x: { display: true, ticks: { color: "#8a8f9c", maxTicksLimit: 6, font: MONO }, grid: { color: "#2a2e3a" } },
        y: { display: true, ticks: { color: "#8a8f9c", font: MONO }, grid: { color: "#2a2e3a" } },
      },
    },
  });
}

function priceChart(id, labels, score, close, tickers, color) {
  const base = close.find(function (v) { return v !== null; });
  new Chart(document.getElementById(id), {
    type: "line",
    data: {
      labels: labels,
      datasets: [
        { label: "Composite", data: score, yAxisID: "y", borderColor: color, backgroundColor: color, borderWidth: 1.5, pointRadius: 2, tension: 0.15, spanGaps: false },
        { label: "Price", data: close, yAxisID: "y1", borderColor: "#e7e5de", backgroundColor: "#e7e5de", borderWidth: 1.5, borderDash: [5, 4], pointRadius: 2, tension: 0.15, spanGaps: false },
        { label: "zero", data: labels.map(function () { return 0; }), yAxisID: "y", borderColor: "#8a8f9c", borderWidth: 1, borderDash: [2, 4], pointRadius: 0 },
      ],
    },
    options: {
      responsive: true,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { labels: { color: "#8a8f9c", boxWidth: 12, filter: function (item) { return item.text !== "zero"; } } },
        tooltip: {
          filter: function (item) { return item.dataset.label !== "zero"; },
          callbacks: {
            label: function (ctx) {
              const v = ctx.parsed.y;
              if (ctx.dataset.label === "Price") {
                const pct = base ? (v / base - 1) * 100 : 0;
                const t = tickers[ctx.dataIndex] ? ", " + tickers[ctx.dataIndex] : "";
                return "Price " + v.toLocaleString(undefined, { maximumFractionDigits: 2 }) + " (" + (pct >= 0 ? "+" : "") + pct.toFixed(1) + "% since start" + t + ")";
              }
              return "Composite " + v.toFixed(2);
            },
          },
        },
      },
      scales: {
        x: { ticks: { color: "#8a8f9c", maxTicksLimit: 10, font: MONO }, grid: { color: "#2a2e3a" } },
        y: { position: "left", title: { display: true, text: "Daily composite", color: color }, ticks: { color: color, font: MONO }, grid: { color: "#2a2e3a" } },
        y1: { position: "right", title: { display: true, text: "Price (last close of day)", color: "#e7e5de" }, ticks: { color: "#e7e5de", font: MONO }, grid: { drawOnChartArea: false } },
      },
    },
  });
}

function showTab(slug) {
  document.querySelectorAll(".tab-btn").forEach(function (b) { b.classList.toggle("active", b.dataset.tab === slug); });
  document.querySelectorAll(".tab-panel").forEach(function (p) { p.classList.toggle("active", p.id === "tab-" + slug); });
  if (!TAB_DONE[slug] && TAB_INITS[slug]) { TAB_INITS[slug](); TAB_DONE[slug] = true; }
}

document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll(".tab-btn").forEach(function (b) {
    b.addEventListener("click", function () { showTab(b.dataset.tab); });
  });
  const first = document.querySelector(".tab-btn");
  if (first) { showTab(first.dataset.tab); }
});
"""


def build_dashboard():
    if not LOG_PATH.exists():
        print(f"No log file at {LOG_PATH} yet -- run scanner.py first.")
        return

    log_df = pd.read_excel(LOG_PATH, sheet_name="Log")
    try:
        daily_df = pd.read_excel(LOG_PATH, sheet_name="Daily")
    except ValueError:
        daily_df = pd.DataFrame()

    last_updated = log_df["timestamp_utc"].max() if not log_df.empty else "n/a"
    log_scores = _log_scores(log_df)

    panels, tabs = [], []
    for b in BUCKET_COLORS:
        panel = _bucket_panel_html(b, log_df, daily_df, log_scores, active=not panels)
        if not panel:
            continue
        panels.append(panel)
        tabs.append(f'<button class="tab-btn" data-tab="{_slug(b)}" style="--accent: {BUCKET_COLORS[b]}">{b}</button>')
    panels_html = "\n".join(panels)
    tabs_html = "\n    ".join(tabs)
    daily_html = _grouped_daily_history_html(daily_df, list(BUCKET_COLORS.keys()))

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Asset Sentiment & Price</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<script src="vendor/chart.umd.js"></script>
<style>{CSS}</style>
<script>{JS}</script>
</head>
<body>
<header>
  <h1>Asset sentiment &amp; price</h1>
  <p>Last scan: {last_updated} UTC · Positive score = bullish pressure · Negative = bearish pressure</p>
</header>
<nav class="tabs">
    {tabs_html}
</nav>
<main>
  {panels_html}
  <section class="panel" style="--accent: var(--rule)">
    <div class="panel-head"><h2>Daily history</h2></div>
    {daily_html}
  </section>
</main>
<footer>
  Generated automatically from asset_sentiment_log.xlsx. Composite score is a v1
  heuristic (article volume x tone, bullish side minus bearish side) -- treat it
  as a directional read, not a calibrated probability. OHLC is a best-effort
  trailing-hourly-bar snapshot, not a settlement-grade price. Daily price on the
  comparison chart is the last close logged each day.
</footer>
</body>
</html>
"""
    OUTPUT_PATH.write_text(html, encoding="utf-8")
    print(f"Dashboard written -> {OUTPUT_PATH}")


if __name__ == "__main__":
    build_dashboard()
