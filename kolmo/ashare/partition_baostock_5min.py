#!/usr/bin/env python3
"""Partition per-symbol BaoStock 5-minute caches into per-day gzip CSV files."""

from __future__ import annotations

import argparse
import csv
import gzip
import os
import shutil
import tempfile
from collections import OrderedDict
from pathlib import Path

from kolmo.ashare.board_rules import board_for_symbol
from kolmo.ashare.fetch_baostock_daily import normalize_date
from kolmo.paths import data_path


OUTPUT_FIELDS = [
    "date", "datetime", "symbol", "exchange", "board", "open", "high", "low",
    "close", "volume", "volume_unit", "amount", "adjust", "source",
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build date-partitioned BaoStock 5-minute profiles.")
    parser.add_argument("--exchange", choices=["sz", "sh"], required=True)
    parser.add_argument("--adjust", choices=["raw", "qfq", "hfq"], default="raw")
    parser.add_argument("--input-dir", default="")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--work-dir", default="", help="Temporary uncompressed partition workspace.")
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--max-open-files", type=int, default=64)
    return parser.parse_args(argv)


def timestamp(date_value: str, time_value: str) -> str:
    digits = "".join(character for character in time_value if character.isdigit())
    hhmmss = digits[8:14] if len(digits) >= 14 else digits[-6:].rjust(6, "0")
    return f"{normalize_date(date_value)} {hhmmss[:2]}:{hhmmss[2:4]}:{hhmmss[4:6]}"


def output_path(root: Path, exchange: str, trade_date: str) -> Path:
    compact = trade_date.replace("-", "")
    return root / exchange / compact[:4] / compact[4:6] / f"{compact}.csv.gz"


def normalized_row(row: dict[str, str], symbol: str, exchange: str, adjust: str) -> dict[str, str]:
    return {
        "date": normalize_date(row["date"]),
        "datetime": timestamp(row["date"], row["time"]),
        "symbol": symbol,
        "exchange": exchange.upper(),
        "board": board_for_symbol(symbol),
        "open": row.get("open", ""), "high": row.get("high", ""),
        "low": row.get("low", ""), "close": row.get("close", ""),
        "volume": row.get("volume", ""), "volume_unit": "share",
        "amount": row.get("amount", ""), "adjust": adjust, "source": "baostock",
    }


class DayWriters:
    def __init__(self, root: Path, max_open: int) -> None:
        self.root = root
        self.max_open = max_open
        self.opened: OrderedDict[str, tuple[object, csv.DictWriter]] = OrderedDict()
        self.seen: set[str] = set()
        self.rows = 0

    def writer(self, trade_date: str) -> csv.DictWriter:
        entry = self.opened.pop(trade_date, None)
        if entry is not None:
            self.opened[trade_date] = entry
            return entry[1]
        if len(self.opened) >= self.max_open:
            _, (file, _) = self.opened.popitem(last=False)
            file.close()
        path = self.root / f"{trade_date}.csv"
        first = trade_date not in self.seen
        file = path.open("w" if first else "a", encoding="utf-8", newline="")
        writer = csv.DictWriter(file, fieldnames=OUTPUT_FIELDS, lineterminator="\n")
        if first:
            writer.writeheader()
            self.seen.add(trade_date)
        self.opened[trade_date] = (file, writer)
        return writer

    def close(self) -> None:
        for file, _ in self.opened.values():
            file.close()
        self.opened.clear()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.max_open_files < 1:
        raise ValueError("--max-open-files must be positive")
    input_dir = Path(
        args.input_dir
        or data_path("raw", "ashare", "baostock", args.exchange, "minute", "5m", args.adjust)
    )
    output_root = Path(
        args.output_dir or data_path("profile", "minute", "5m", args.adjust)
    )
    paths = sorted(input_dir.glob(f"*.{args.exchange.upper()}.csv.gz"))
    if not paths:
        print(f"no per-symbol 5-minute files found: {input_dir}")
        return 1
    start = normalize_date(args.start_date) if args.start_date else ""
    end = normalize_date(args.end_date) if args.end_date else "9999-12-31"
    work_root = Path(args.work_dir or data_path("work", "ashare", "minute_5m"))
    work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"partition_{args.exchange}_", dir=work_root) as temporary_name:
        temporary_root = Path(temporary_name)
        writers = DayWriters(temporary_root, args.max_open_files)
        try:
            for index, path in enumerate(paths, start=1):
                symbol = path.name.removesuffix(".csv.gz")
                with gzip.open(path, "rt", encoding="utf-8", newline="") as source:
                    reader = csv.DictReader(source)
                    required = {"date", "time", "open", "high", "low", "close", "volume", "amount"}
                    if not reader.fieldnames or not required.issubset(reader.fieldnames):
                        raise ValueError(f"invalid 5-minute cache schema: {path}")
                    for row in reader:
                        trade_date = normalize_date(row.get("date", ""))
                        if start <= trade_date <= end:
                            writers.writer(trade_date).writerow(
                                normalized_row(row, symbol, args.exchange, args.adjust)
                            )
                            writers.rows += 1
                if index % 100 == 0 or index == len(paths):
                    print(f"[{index}/{len(paths)}] rows={writers.rows}", flush=True)
        finally:
            writers.close()

        for source_path in sorted(temporary_root.glob("*.csv")):
            trade_date = source_path.stem
            destination = output_path(output_root, args.exchange, trade_date)
            destination.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(
                prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
            )
            os.close(descriptor)
            temporary_output = Path(name)
            try:
                with source_path.open("rb") as source, gzip.open(temporary_output, "wb") as output:
                    shutil.copyfileobj(source, output)
                os.replace(temporary_output, destination)
            finally:
                temporary_output.unlink(missing_ok=True)
    print(f"done symbols={len(paths)} dates={len(writers.seen)} rows={writers.rows} output={output_root / args.exchange}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
