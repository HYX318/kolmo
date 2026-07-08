#!/usr/bin/env python3
"""Fetch real A-share daily bars from BaoStock."""

from __future__ import annotations

import argparse
import csv
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable


FIELDS = [
    "date",
    "code",
    "open",
    "high",
    "low",
    "close",
    "preclose",
    "volume",
    "amount",
    "adjustflag",
    "turn",
    "tradestatus",
    "pctChg",
    "isST",
]

NORMALIZED_COLUMNS = [
    "date",
    "symbol",
    "exchange",
    "open",
    "high",
    "low",
    "close",
    "preclose",
    "volume",
    "volume_unit",
    "amount",
    "turnover_rate",
    "pct_change",
    "trade_status",
    "is_st",
    "adjust",
    "source",
]


@dataclass(frozen=True)
class StockInfo:
    code: str
    bs_code: str
    symbol: str
    name: str
    listing_date: str
    delisting_date: str
    status: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch real A-share daily bars from BaoStock.")
    parser.add_argument(
        "--exchange",
        default="sz",
        choices=["sz", "sh"],
        help="Exchange to fetch: sz=SZSE, sh=SSE.",
    )
    parser.add_argument("--start-date", default="2017-01-01", help="Inclusive YYYY-MM-DD/YYYMMDD.")
    parser.add_argument(
        "--end-date",
        default=date.today().strftime("%Y-%m-%d"),
        help="Inclusive YYYY-MM-DD/YYYMMDD.",
    )
    parser.add_argument(
        "--adjust",
        default="qfq",
        choices=["qfq", "hfq", "raw"],
        help="qfq=front adjusted, hfq=back adjusted, raw=unadjusted.",
    )
    parser.add_argument(
        "--raw-dir",
        default="",
        help="Directory for per-symbol raw CSV cache.",
    )
    parser.add_argument(
        "--clean-output",
        default="",
        help="Combined normalized CSV path. Default is under data/clean/ashare/.",
    )
    parser.add_argument(
        "--universe-output",
        default="",
        help="Universe CSV path. Default is under data/raw/ashare/baostock/{exchange}/.",
    )
    parser.add_argument(
        "--failures-output",
        default="",
        help="Failure manifest path. Default is beside the universe CSV.",
    )
    parser.add_argument(
        "--include-delisted",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include stocks delisted after the start date.",
    )
    parser.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Reuse existing per-symbol raw CSV files.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Optional symbol limit for testing.")
    parser.add_argument("--sleep", type=float, default=0.05, help="Seconds to sleep between symbols.")
    parser.add_argument("--no-combine", action="store_true", help="Only write raw per-symbol files.")
    return parser.parse_args()


def require_dependencies():
    try:
        import baostock as bs  # type: ignore
    except ModuleNotFoundError as exc:
        print(
            "missing Python dependency: baostock\n"
            "Install with: python3 -m pip install -r requirements.txt",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
    return bs


def normalize_date(value: str) -> str:
    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return text[:10]


def compact_date(value: str) -> str:
    return normalize_date(value).replace("-", "")


def today_yyyymmdd() -> str:
    return date.today().strftime("%Y%m%d")


def adjustflag(adjust: str) -> str:
    if adjust == "hfq":
        return "1"
    if adjust == "qfq":
        return "2"
    return "3"


def adjustment_dir_name(adjust: str) -> str:
    return adjust


def rows_from_result(result) -> list[list[str]]:
    rows: list[list[str]] = []
    while result.next():
        rows.append(result.get_row_data())
    return rows


def query_or_raise(result, context: str):
    if result.error_code != "0":
        raise RuntimeError(f"{context}: {result.error_code} {result.error_msg}")
    return result


def exchange_prefix(exchange: str) -> str:
    return f"{exchange}."


def exchange_suffix(exchange: str) -> str:
    return exchange.upper()


def load_universe(
    bs,
    start_date: str,
    end_date: str,
    include_delisted: bool,
    exchange: str,
) -> list[StockInfo]:
    result = query_or_raise(bs.query_stock_basic(), "query_stock_basic")
    fields = result.fields
    stocks: list[StockInfo] = []
    prefix = exchange_prefix(exchange)
    suffix = exchange_suffix(exchange)

    for values in rows_from_result(result):
        row = dict(zip(fields, values))
        bs_code = row.get("code", "")
        if not bs_code.startswith(prefix):
            continue
        if row.get("type", "") != "1":
            continue

        listing_date = row.get("ipoDate", "")
        delisting_date = row.get("outDate", "")
        if listing_date and listing_date > end_date:
            continue
        if delisting_date and delisting_date < start_date and include_delisted:
            continue
        if delisting_date and not include_delisted:
            continue

        code = bs_code.split(".", 1)[1]
        stocks.append(
            StockInfo(
                code=code,
                bs_code=bs_code,
                symbol=f"{code}.{suffix}",
                name=row.get("code_name", ""),
                listing_date=listing_date,
                delisting_date=delisting_date,
                status="delisted" if delisting_date else "active",
            )
        )

    return sorted(stocks, key=lambda stock: stock.code)


def write_universe(path: Path, stocks: Iterable[StockInfo]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "code",
                "bs_code",
                "symbol",
                "name",
                "listing_date",
                "delisting_date",
                "status",
            ],
            lineterminator="\n",
        )
        writer.writeheader()
        for stock in stocks:
            writer.writerow(stock.__dict__)


def fetch_symbol(bs, stock: StockInfo, args: argparse.Namespace, raw_dir: Path) -> tuple[Path, str]:
    path = raw_dir / adjustment_dir_name(args.adjust) / f"{stock.symbol}.csv"
    if args.resume and path.exists() and path.stat().st_size > 0:
        return path, "cached"

    result = query_or_raise(
        bs.query_history_k_data_plus(
            stock.bs_code,
            ",".join(FIELDS),
            start_date=normalize_date(args.start_date),
            end_date=normalize_date(args.end_date),
            frequency="d",
            adjustflag=adjustflag(args.adjust),
        ),
        f"query_history_k_data_plus {stock.bs_code}",
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file, lineterminator="\n")
        writer.writerow(result.fields)
        writer.writerows(rows_from_result(result))

    return path, "fetched"


def append_normalized(
    output: Path,
    raw_path: Path,
    stock: StockInfo,
    adjust: str,
    exchange: str,
) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    write_header = not output.exists() or output.stat().st_size == 0
    rows_written = 0

    with raw_path.open("r", encoding="utf-8", newline="") as input_file:
        reader = csv.DictReader(input_file)
        with output.open("a", encoding="utf-8", newline="") as output_file:
            writer = csv.DictWriter(
                output_file, fieldnames=NORMALIZED_COLUMNS, lineterminator="\n"
            )
            if write_header:
                writer.writeheader()

            for row in reader:
                if not row.get("date"):
                    continue
                writer.writerow(
                    {
                        "date": compact_date(row.get("date", "")),
                        "symbol": stock.symbol,
                        "exchange": exchange_suffix(exchange),
                        "open": row.get("open", ""),
                        "high": row.get("high", ""),
                        "low": row.get("low", ""),
                        "close": row.get("close", ""),
                        "preclose": row.get("preclose", ""),
                        "volume": row.get("volume", ""),
                        "volume_unit": "share",
                        "amount": row.get("amount", ""),
                        "turnover_rate": row.get("turn", ""),
                        "pct_change": row.get("pctChg", ""),
                        "trade_status": row.get("tradestatus", ""),
                        "is_st": row.get("isST", ""),
                        "adjust": adjust,
                        "source": "baostock.query_history_k_data_plus",
                    }
                )
                rows_written += 1

    return rows_written


def main() -> int:
    args = parse_args()
    bs = require_dependencies()

    login = bs.login()
    if login.error_code != "0":
        print(f"baostock login failed: {login.error_code} {login.error_msg}", file=sys.stderr)
        return 2

    try:
        run_date = today_yyyymmdd()
        start_date = normalize_date(args.start_date)
        end_date = normalize_date(args.end_date)
        raw_dir = Path(args.raw_dir or f"data/raw/ashare/baostock/{args.exchange}/daily")
        clean_output = Path(
            args.clean_output
            or f"data/clean/ashare/{args.exchange}_daily_bars_{compact_date(start_date)}_{compact_date(end_date)}_{args.adjust}_baostock.csv"
        )
        universe_output = Path(
            args.universe_output
            or f"data/raw/ashare/baostock/{args.exchange}/universe_{args.exchange}_{run_date}.csv"
        )
        failures_output = Path(
            args.failures_output
            or f"data/raw/ashare/baostock/{args.exchange}/failures_{args.exchange}_daily_{run_date}.csv"
        )

        stocks = load_universe(
            bs,
            start_date=start_date,
            end_date=end_date,
            include_delisted=args.include_delisted,
            exchange=args.exchange,
        )
        if args.limit > 0:
            stocks = stocks[: args.limit]
        write_universe(universe_output, stocks)

        if clean_output.exists() and not args.no_combine:
            clean_output.unlink()

        failures_output.parent.mkdir(parents=True, exist_ok=True)
        fetched = 0
        cached = 0
        failed = 0
        total_rows = 0

        with failures_output.open("w", encoding="utf-8", newline="") as failure_file:
            failure_writer = csv.DictWriter(
                failure_file,
                fieldnames=["symbol", "status", "error"],
                lineterminator="\n",
            )
            failure_writer.writeheader()

            for index, stock in enumerate(stocks, start=1):
                try:
                    raw_path, source_state = fetch_symbol(bs, stock, args, raw_dir)
                    fetched += 1 if source_state == "fetched" else 0
                    cached += 1 if source_state == "cached" else 0
                    rows = 0
                    if not args.no_combine:
                        rows = append_normalized(
                            clean_output, raw_path, stock, args.adjust, args.exchange
                        )
                        total_rows += rows

                    print(
                        f"[{index}/{len(stocks)}] {stock.symbol} {source_state} rows={rows}",
                        flush=True,
                    )
                except Exception as exc:
                    failed += 1
                    failure_writer.writerow(
                        {"symbol": stock.symbol, "status": stock.status, "error": repr(exc)}
                    )
                    print(f"[{index}/{len(stocks)}] {stock.symbol} failed: {exc}", file=sys.stderr)

                if args.sleep > 0:
                    time.sleep(args.sleep)

        print(
            "done "
            f"symbols={len(stocks)} fetched={fetched} cached={cached} failed={failed} "
            f"rows={total_rows} universe={universe_output} failures={failures_output} "
            f"clean={'' if args.no_combine else clean_output}"
        )
        return 0 if failed == 0 else 1
    finally:
        bs.logout()


if __name__ == "__main__":
    raise SystemExit(main())
