#!/usr/bin/env python3
"""Fetch real Shenzhen A-share daily bars from AKShare.

The script downloads per-symbol raw CSV files and also writes one normalized
combined CSV for downstream research. It is intentionally kept outside CMake
and the C++ modules.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable

from kolmo.paths import data_path


NORMALIZED_COLUMNS = [
    "date",
    "symbol",
    "exchange",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "volume_unit",
    "amount",
    "amplitude",
    "pct_change",
    "change",
    "turnover_rate",
    "adjust",
    "source",
]


@dataclass(frozen=True)
class StockInfo:
    code: str
    symbol: str
    name: str
    board: str
    listing_date: str
    delisting_date: str
    status: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch real Shenzhen A-share daily bars from AKShare."
    )
    parser.add_argument("--start-date", default="20170101", help="Inclusive YYYYMMDD start date.")
    parser.add_argument(
        "--end-date",
        default=date.today().strftime("%Y%m%d"),
        help="Inclusive YYYYMMDD end date.",
    )
    parser.add_argument(
        "--adjust",
        default="qfq",
        choices=["", "qfq", "hfq"],
        help="AKShare adjustment flag. Empty string means unadjusted.",
    )
    parser.add_argument(
        "--raw-dir",
        default="data/raw/ashare/akshare/sz/daily",
        help="Directory for per-symbol raw CSV cache.",
    )
    parser.add_argument(
        "--clean-output",
        default="",
        help="Combined normalized CSV path. Default is under KOLMO_DATA_ROOT/work/ashare/.",
    )
    parser.add_argument(
        "--universe-output",
        default="",
        help="Universe CSV path. Default is under KOLMO_DATA_ROOT/raw/ashare/akshare/sz/.",
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
        help="Include SZSE terminated/suspended listings when building the universe.",
    )
    parser.add_argument(
        "--universe-source",
        choices=["auto", "szse", "eastmoney"],
        default="auto",
        help="Universe source. auto tries SZSE first, then Eastmoney current quotes.",
    )
    parser.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Reuse existing per-symbol raw CSV files.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Optional symbol limit for testing.")
    parser.add_argument("--sleep", type=float, default=0.2, help="Seconds to sleep between symbols.")
    parser.add_argument("--timeout", type=float, default=30.0, help="AKShare request timeout.")
    parser.add_argument("--no-combine", action="store_true", help="Only write raw per-symbol files.")
    return parser.parse_args()


def require_dependencies():
    try:
        import akshare as ak  # type: ignore
        import pandas as pd  # type: ignore
    except ModuleNotFoundError as exc:
        missing = exc.name or "dependency"
        print(
            f"missing Python dependency: {missing}\n"
            "Install with: python3 -m pip install -r requirements.txt",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
    return ak, pd


def today_yyyymmdd() -> str:
    return date.today().strftime("%Y%m%d")


def normalize_code(value: object) -> str:
    code = str(value).strip()
    if code.endswith(".0"):
        code = code[:-2]
    return code.zfill(6)


def normalize_date(value: object) -> str:
    text = str(value).strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10].replace("-", "")
    return text.replace("-", "").replace("/", "")[:8]


def load_active_universe_from_szse(ak) -> dict[str, StockInfo]:
    result: dict[str, StockInfo] = {}

    current = ak.stock_info_sz_name_code(symbol="A股列表")
    for _, row in current.iterrows():
        code = normalize_code(row.get("A股代码", ""))
        if not code:
            continue
        result[code] = StockInfo(
            code=code,
            symbol=f"{code}.SZ",
            name=str(row.get("A股简称", "")).strip(),
            board=str(row.get("板块", "")).strip(),
            listing_date=normalize_date(row.get("A股上市日期", "")),
            delisting_date="",
            status="active",
        )

    return result


def load_active_universe_from_eastmoney(ak) -> dict[str, StockInfo]:
    result: dict[str, StockInfo] = {}
    current = ak.stock_zh_a_spot_em()
    for _, row in current.iterrows():
        code = normalize_code(row.get("代码", ""))
        if not code.startswith(("0", "3")):
            continue
        result[code] = StockInfo(
            code=code,
            symbol=f"{code}.SZ",
            name=str(row.get("名称", "")).strip(),
            board="",
            listing_date="",
            delisting_date="",
            status="active",
        )
    return result


def load_universe(ak, include_delisted: bool, universe_source: str) -> list[StockInfo]:
    result: dict[str, StockInfo] = {}

    if universe_source in ("auto", "szse"):
        try:
            result = load_active_universe_from_szse(ak)
        except Exception as exc:
            if universe_source == "szse":
                raise
            print(
                f"warning: SZSE universe failed, falling back to Eastmoney current A-share list: {exc}",
                file=sys.stderr,
            )

    if not result and universe_source in ("auto", "eastmoney"):
        result = load_active_universe_from_eastmoney(ak)

    if include_delisted:
        for status_name, ak_symbol in [
            ("terminated", "终止上市公司"),
            ("suspended", "暂停上市公司"),
        ]:
            try:
                delisted = ak.stock_info_sz_delist(symbol=ak_symbol)
            except Exception as exc:
                print(f"warning: could not fetch {ak_symbol}: {exc}", file=sys.stderr)
                continue

            for _, row in delisted.iterrows():
                code = normalize_code(row.get("证券代码", ""))
                if not code or not code.startswith(("0", "3")):
                    continue
                existing = result.get(code)
                result[code] = StockInfo(
                    code=code,
                    symbol=f"{code}.SZ",
                    name=str(row.get("证券简称", "")).strip() or (existing.name if existing else ""),
                    board=existing.board if existing else "",
                    listing_date=normalize_date(row.get("上市日期", ""))
                    or (existing.listing_date if existing else ""),
                    delisting_date=normalize_date(row.get("终止上市日期", "")),
                    status=status_name,
                )

    return [result[code] for code in sorted(result)]


def write_universe(path: Path, stocks: Iterable[StockInfo]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "code",
                "symbol",
                "name",
                "board",
                "listing_date",
                "delisting_date",
                "status",
            ],
            lineterminator="\n",
        )
        writer.writeheader()
        for stock in stocks:
            writer.writerow(stock.__dict__)


def adjustment_dir_name(adjust: str) -> str:
    return adjust if adjust else "raw"


def fetch_symbol(ak, stock: StockInfo, args: argparse.Namespace, raw_dir: Path):
    path = raw_dir / adjustment_dir_name(args.adjust) / f"{stock.symbol}.csv"
    if args.resume and path.exists() and path.stat().st_size > 0:
        return path, "cached"

    path.parent.mkdir(parents=True, exist_ok=True)
    df = ak.stock_zh_a_hist(
        symbol=stock.code,
        period="daily",
        start_date=args.start_date,
        end_date=args.end_date,
        adjust=args.adjust,
        timeout=args.timeout,
    )
    df.to_csv(path, index=False, encoding="utf-8")
    return path, "fetched"


def scalar(row, column: str) -> str:
    value = row.get(column, "")
    if value is None:
        return ""
    return str(value).strip()


def append_normalized(pd, output: Path, raw_path: Path, stock: StockInfo, adjust: str) -> int:
    df = pd.read_csv(raw_path, dtype={"股票代码": str})
    if df.empty:
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)
    write_header = not output.exists() or output.stat().st_size == 0

    with output.open("a", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=NORMALIZED_COLUMNS, lineterminator="\n")
        if write_header:
            writer.writeheader()

        for _, row in df.iterrows():
            writer.writerow(
                {
                    "date": normalize_date(row.get("日期", "")),
                    "symbol": stock.symbol,
                    "exchange": "SZ",
                    "open": scalar(row, "开盘"),
                    "high": scalar(row, "最高"),
                    "low": scalar(row, "最低"),
                    "close": scalar(row, "收盘"),
                    "volume": scalar(row, "成交量"),
                    "volume_unit": "hand",
                    "amount": scalar(row, "成交额"),
                    "amplitude": scalar(row, "振幅"),
                    "pct_change": scalar(row, "涨跌幅"),
                    "change": scalar(row, "涨跌额"),
                    "turnover_rate": scalar(row, "换手率"),
                    "adjust": adjustment_dir_name(adjust),
                    "source": "akshare.stock_zh_a_hist",
                }
            )

    return len(df)


def main() -> int:
    args = parse_args()
    ak, pd = require_dependencies()

    run_date = today_yyyymmdd()
    raw_dir = Path(args.raw_dir or data_path("raw", "ashare", "akshare", "sz", "daily"))
    clean_output = Path(
        args.clean_output
        or data_path(
            "work",
            "ashare",
            f"sz_daily_bars_{args.start_date}_{args.end_date}_{adjustment_dir_name(args.adjust)}.csv",
        )
    )
    universe_output = Path(
        args.universe_output
        or data_path("raw", "ashare", "akshare", "sz", f"universe_sz_{run_date}.csv")
    )
    failures_output = Path(
        args.failures_output
        or data_path("raw", "ashare", "akshare", "sz", f"failures_sz_daily_{run_date}.csv")
    )

    stocks = load_universe(
        ak,
        include_delisted=args.include_delisted,
        universe_source=args.universe_source,
    )
    if args.limit > 0:
        stocks = stocks[: args.limit]
    write_universe(universe_output, stocks)

    if clean_output.exists() and not args.no_combine:
        clean_output.unlink()

    failures_output.parent.mkdir(parents=True, exist_ok=True)
    total_rows = 0
    fetched = 0
    cached = 0
    failed = 0

    with failures_output.open("w", encoding="utf-8", newline="") as failure_file:
        failure_writer = csv.DictWriter(
            failure_file,
            fieldnames=["symbol", "status", "error"],
            lineterminator="\n",
        )
        failure_writer.writeheader()

        for index, stock in enumerate(stocks, start=1):
            try:
                raw_path, source_state = fetch_symbol(ak, stock, args, raw_dir)
                fetched += 1 if source_state == "fetched" else 0
                cached += 1 if source_state == "cached" else 0
                rows = 0
                if not args.no_combine:
                    rows = append_normalized(pd, clean_output, raw_path, stock, args.adjust)
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


if __name__ == "__main__":
    raise SystemExit(main())
