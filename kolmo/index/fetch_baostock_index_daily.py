#!/usr/bin/env python3
"""Fetch daily index bars from BaoStock."""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date
from pathlib import Path

from kolmo.paths import data_path


FIELDS = ["date", "code", "open", "high", "low", "close", "preclose", "volume", "amount", "pctChg"]
OUTPUT_FIELDS = ["date", "symbol", "open", "high", "low", "close", "preclose", "volume", "amount", "pct_change", "source"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch BaoStock daily index bars.")
    parser.add_argument(
        "--symbol",
        action="append",
        default=[],
        help="Index symbol, e.g. 399006.SZ or 000922.SH. Can be repeated.",
    )
    parser.add_argument("--start-date", default="2017-01-01")
    parser.add_argument("--end-date", default=date.today().strftime("%Y-%m-%d"))
    parser.add_argument("--output-dir", default="", help="Default: $KOLMO_DATA_ROOT/raw/ashare/baostock/index/daily.")
    return parser.parse_args()


def require_dependencies():
    try:
        import baostock as bs  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        print("missing Python dependency: baostock", file=sys.stderr)
        raise SystemExit(2) from exc
    return bs


def normalize_date(value: str) -> str:
    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text[:10]


def compact_date(value: str) -> str:
    return normalize_date(value).replace("-", "")


def baostock_code(symbol: str) -> str:
    code, exchange = symbol.upper().split(".", 1)
    if exchange == "SZ":
        return f"sz.{code}"
    if exchange == "SH":
        return f"sh.{code}"
    raise ValueError(f"unsupported index symbol: {symbol}")


def fetch_one(bs, symbol: str, start_date: str, end_date: str, output_dir: Path) -> Path:
    result = bs.query_history_k_data_plus(
        baostock_code(symbol),
        ",".join(FIELDS),
        start_date=normalize_date(start_date),
        end_date=normalize_date(end_date),
        frequency="d",
    )
    if result.error_code != "0":
        raise RuntimeError(f"{symbol}: {result.error_code} {result.error_msg}")

    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{symbol.upper()}.csv"
    with output.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=OUTPUT_FIELDS, lineterminator="\n")
        writer.writeheader()
        rows = 0
        while result.next():
            row = dict(zip(result.fields, result.get_row_data()))
            if not row.get("date"):
                continue
            writer.writerow(
                {
                    "date": compact_date(row.get("date", "")),
                    "symbol": symbol.upper(),
                    "open": row.get("open", ""),
                    "high": row.get("high", ""),
                    "low": row.get("low", ""),
                    "close": row.get("close", ""),
                    "preclose": row.get("preclose", ""),
                    "volume": row.get("volume", ""),
                    "amount": row.get("amount", ""),
                    "pct_change": row.get("pctChg", ""),
                    "source": "baostock.query_history_k_data_plus",
                }
            )
            rows += 1
    print(f"{symbol.upper()} rows={rows} output={output}", flush=True)
    return output


def main() -> int:
    args = parse_args()
    symbols = args.symbol or ["399006.SZ", "000922.SH"]
    output_dir = Path(args.output_dir or data_path("raw", "ashare", "baostock", "index", "daily"))
    bs = require_dependencies()
    login = bs.login()
    if login.error_code != "0":
        print(f"baostock login failed: {login.error_code} {login.error_msg}", file=sys.stderr)
        return 2
    try:
        for symbol in symbols:
            fetch_one(bs, symbol, args.start_date, args.end_date, output_dir)
    finally:
        bs.logout()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
