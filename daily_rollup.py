"""Rebuild the 'Daily' summary sheet from the 'Log' sheet: one row per
(date, bucket), with a live Excel formula for day-over-day % change. Run
once a day via .github/workflows/daily_rollup.yml.

The Daily sheet is fully rebuilt each run from Log -- it's a derived view,
not a place to hand-edit.

Failed scans: a scan where EVERY bucket returned zero articles is a failed
news fetch (e.g. 2026-10-26), not a quiet market. Those scans are dropped from
the composite average, so the daily score is the mean over successful scans
only (total / number of successful scans, not a fixed divisor). Price is
averaged from every scan that has a price, independent of the news result,
because the two can fail separately. A date where every scan failed keeps its
row with a blank composite instead of a fake 0.
"""
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook

LOG_PATH = Path(__file__).parent / "data" / "asset_sentiment_log.xlsx"
LOG_SHEET = "Log"
DAILY_SHEET = "Daily"

DAILY_HEADERS = [
    "date", "bucket", "bullish_articles", "bearish_articles",
    "composite_avg", "composite_pct_change_vs_prev_day",
    "ohlc_close_avg",
]


def build_daily_frame() -> pd.DataFrame:
    df = pd.read_excel(LOG_PATH, sheet_name=LOG_SHEET)

    # A scan = one timestamp_utc (all six buckets share it). Failed news fetch
    # = zero articles across every bucket in that scan.
    df["_articles"] = df["bullish_articles"].fillna(0) + df["bearish_articles"].fillna(0)
    scan_articles = df.groupby("timestamp_utc")["_articles"].transform("sum")
    good = df[scan_articles > 0]
    dropped = df["timestamp_utc"].nunique() - good["timestamp_utc"].nunique()
    if dropped:
        print(f"[INFO] Dropped {dropped} failed scan(s) (zero articles in every bucket) from the composite average.")

    news = (
        good.groupby(["date", "bucket"], as_index=False)
        .agg(
            bullish_articles=("bullish_articles", "sum"),
            bearish_articles=("bearish_articles", "sum"),
            composite_avg=("composite_score", "mean"),
        )
    )
    price = (
        df.groupby(["date", "bucket"], as_index=False)
        .agg(ohlc_close_avg=("ohlc_close", "mean"))
    )
    grouped = news.merge(price, on=["date", "bucket"], how="outer")
    grouped["bullish_articles"] = grouped["bullish_articles"].fillna(0)
    grouped["bearish_articles"] = grouped["bearish_articles"].fillna(0)
    grouped["composite_avg"] = grouped["composite_avg"].round(3)
    grouped["ohlc_close_avg"] = grouped["ohlc_close_avg"].round(4)
    grouped = grouped.sort_values(["bucket", "date"]).reset_index(drop=True)
    return grouped


def _blank_if_nan(x):
    return None if pd.isna(x) else x


def write_daily_sheet(grouped: pd.DataFrame):
    wb = load_workbook(LOG_PATH)
    if DAILY_SHEET in wb.sheetnames:
        del wb[DAILY_SHEET]
    ws = wb.create_sheet(DAILY_SHEET)
    ws.append(DAILY_HEADERS)

    for i, row in enumerate(grouped.itertuples(index=False), start=2):  # row 1 = header
        ws.append([
            row.date, row.bucket, row.bullish_articles, row.bearish_articles,
            _blank_if_nan(row.composite_avg), None, _blank_if_nan(row.ohlc_close_avg),
        ])
        # ISNUMBER guards: a blank composite (all scans failed that day) must
        # read "n/a", not be treated as 0 and show a fake -100% change.
        formula = (
            f'=IF(B{i-1}=B{i}, IF(AND(ISNUMBER(E{i}), ISNUMBER(E{i-1})), '
            f'IFERROR((E{i}-E{i-1})/ABS(E{i-1}), "n/a"), "n/a"), "")'
        )
        ws[f"F{i}"] = formula

    wb.save(LOG_PATH)


def run_rollup():
    if not LOG_PATH.exists():
        print(f"No log file yet at {LOG_PATH} -- run scanner.py at least once first.")
        return
    grouped = build_daily_frame()
    write_daily_sheet(grouped)
    print(f"Daily sheet rebuilt: {len(grouped)} (date, bucket) rows.")


if __name__ == "__main__":
    run_rollup()
