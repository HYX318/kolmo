#!/usr/bin/env python3
"""Normalize daily A-share profile txt files into canonical bar CSV.

This script intentionally uses only the Python standard library so the folder
can become a standalone preprocessing project without dependency setup.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


OUTPUT_COLUMNS = [
    "date",
    "symbol",
    "exchange",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
]


@dataclass(frozen=True)
class NormalizeConfig:
    delimiter: str
    encoding: str
    has_header: bool
    date_from_filename: bool
    filename_date_regex: str
    columns: dict[str, list[str]]
    exchange_suffixes: dict[str, list[str]]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Normalize daily A-share profile txt files into canonical bar CSV."
    )
    parser.add_argument("--input-dir", required=True, help="Directory with daily profile txt/csv files.")
    parser.add_argument("--output", required=True, help="Output CSV path.")
    parser.add_argument("--config", required=True, help="Column mapping JSON config.")
    parser.add_argument("--exchange", choices=["SZ", "SH", "ALL"], default="ALL")
    parser.add_argument("--start-date", help="Inclusive YYYYMMDD lower bound.")
    parser.add_argument("--end-date", help="Inclusive YYYYMMDD upper bound.")
    parser.add_argument(
        "--symbol",
        action="append",
        help="Optional symbol filter. Can be repeated, e.g. --symbol 000001.SZ.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail on malformed rows instead of skipping them.",
    )
    return parser.parse_args()


def load_config(path: Path) -> NormalizeConfig:
    with path.open("r", encoding="utf-8") as file:
        raw = json.load(file)

    return NormalizeConfig(
        delimiter=raw.get("delimiter", "auto"),
        encoding=raw.get("encoding", "utf-8"),
        has_header=bool(raw.get("has_header", True)),
        date_from_filename=bool(raw.get("date_from_filename", True)),
        filename_date_regex=raw.get("filename_date_regex", r"(\d{8})"),
        columns={key: list(value) for key, value in raw.get("columns", {}).items()},
        exchange_suffixes={
            key: list(value) for key, value in raw.get("exchange_suffixes", {}).items()
        },
    )


def normalize_header(value: str) -> str:
    return value.strip().lower().replace("\ufeff", "")


def resolve_header_map(header: Sequence[str], config: NormalizeConfig) -> dict[str, int]:
    normalized = {normalize_header(name): idx for idx, name in enumerate(header)}
    result: dict[str, int] = {}

    for canonical, aliases in config.columns.items():
        for alias in aliases:
            idx = normalized.get(normalize_header(alias))
            if idx is not None:
                result[canonical] = idx
                break

    return result


def detect_delimiter(sample: str, configured: str) -> str | None:
    if configured != "auto":
        if configured in ("space", "whitespace"):
            return None
        return configured

    candidates = [",", "\t", "|", ";"]
    counts = [(sample.count(candidate), candidate) for candidate in candidates]
    count, delimiter = max(counts)
    return delimiter if count > 0 else None


def split_line(line: str, delimiter: str | None) -> list[str]:
    if delimiter is None:
        return line.strip().split()
    return next(csv.reader([line], delimiter=delimiter))


def iter_input_files(input_dir: Path) -> Iterable[Path]:
    suffixes = {".txt", ".csv", ".tsv", ".dat"}
    for path in sorted(input_dir.iterdir()):
        if path.is_file() and path.suffix.lower() in suffixes:
            yield path


def date_from_filename(path: Path, config: NormalizeConfig) -> str | None:
    match = re.search(config.filename_date_regex, path.name)
    return match.group(1) if match else None


def normalize_date(value: str) -> str:
    stripped = value.strip()
    if re.fullmatch(r"\d{8}", stripped):
        return stripped
    match = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", stripped)
    if match:
        year, month, day = match.groups()
        return f"{year}{int(month):02d}{int(day):02d}"
    raise ValueError(f"invalid date: {value}")


def normalize_symbol(raw_symbol: str) -> str:
    symbol = raw_symbol.strip().upper()
    symbol = symbol.replace(".SZE", ".SZ").replace(".SSE", ".SH")
    symbol = symbol.replace(".XSHE", ".SZ").replace(".XSHG", ".SH")

    if re.fullmatch(r"\d{6}", symbol):
        if symbol.startswith(("0", "2", "3")):
            return f"{symbol}.SZ"
        if symbol.startswith(("6", "9")):
            return f"{symbol}.SH"
    return symbol


def infer_exchange(symbol: str, row_exchange: str | None, config: NormalizeConfig) -> str:
    if row_exchange:
        market = row_exchange.strip().upper()
        for exchange, aliases in config.exchange_suffixes.items():
            if market == exchange or market in {alias.upper().lstrip(".") for alias in aliases}:
                return exchange

    upper_symbol = symbol.upper()
    for exchange, aliases in config.exchange_suffixes.items():
        for alias in aliases:
            if upper_symbol.endswith(alias.upper()):
                return exchange

    if re.fullmatch(r"\d{6}\.SZ", upper_symbol):
        return "SZ"
    if re.fullmatch(r"\d{6}\.SH", upper_symbol):
        return "SH"
    return ""


def read_field(row: Sequence[str], header_map: dict[str, int], field: str) -> str:
    idx = header_map.get(field)
    if idx is None or idx >= len(row):
        return ""
    return row[idx].strip()


def normalize_number(value: str, field: str) -> str:
    stripped = value.strip().replace(",", "")
    if stripped in ("", "NA", "N/A", "NULL", "None", "nan"):
        raise ValueError(f"missing {field}")
    float(stripped)
    return stripped


def within_date_range(date: str, start_date: str | None, end_date: str | None) -> bool:
    if start_date and date < start_date:
        return False
    if end_date and date > end_date:
        return False
    return True


def normalize_file(
    path: Path,
    writer: csv.DictWriter,
    config: NormalizeConfig,
    exchange_filter: str,
    symbol_filter: set[str],
    start_date: str | None,
    end_date: str | None,
    strict: bool,
) -> tuple[int, int]:
    written = 0
    skipped = 0
    filename_date = date_from_filename(path, config) if config.date_from_filename else None

    with path.open("r", encoding=config.encoding, errors="replace", newline="") as file:
        sample = file.readline()
        if not sample:
            return written, skipped

        delimiter = detect_delimiter(sample, config.delimiter)
        first_row = split_line(sample, delimiter)

        if config.has_header:
            header_map = resolve_header_map(first_row, config)
            rows = file
        else:
            raise ValueError("headerless input is not implemented; set has_header=true")

        required = ["symbol", "open", "high", "low", "close", "volume"]
        missing = [field for field in required if field not in header_map]
        if missing:
            raise ValueError(f"{path}: missing required columns: {', '.join(missing)}")

        for line_number, line in enumerate(rows, start=2):
            if not line.strip():
                continue
            try:
                row = split_line(line, delimiter)
                raw_date = read_field(row, header_map, "date") or filename_date or ""
                date = normalize_date(raw_date)
                if not within_date_range(date, start_date, end_date):
                    skipped += 1
                    continue

                symbol = normalize_symbol(read_field(row, header_map, "symbol"))
                exchange = infer_exchange(symbol, read_field(row, header_map, "exchange"), config)
                if exchange_filter != "ALL" and exchange != exchange_filter:
                    skipped += 1
                    continue
                if symbol_filter and symbol not in symbol_filter:
                    skipped += 1
                    continue

                output_row = {
                    "date": date,
                    "symbol": symbol,
                    "exchange": exchange,
                    "open": normalize_number(read_field(row, header_map, "open"), "open"),
                    "high": normalize_number(read_field(row, header_map, "high"), "high"),
                    "low": normalize_number(read_field(row, header_map, "low"), "low"),
                    "close": normalize_number(read_field(row, header_map, "close"), "close"),
                    "volume": normalize_number(read_field(row, header_map, "volume"), "volume"),
                    "amount": "",
                }
                amount = read_field(row, header_map, "amount")
                if amount:
                    output_row["amount"] = normalize_number(amount, "amount")

                writer.writerow(output_row)
                written += 1
            except Exception as exc:
                if strict:
                    raise ValueError(f"{path}:{line_number}: {exc}") from exc
                skipped += 1

    return written, skipped


def main() -> int:
    args = parse_args()
    input_dir = Path(args.input_dir)
    output = Path(args.output)
    config = load_config(Path(args.config))

    if not input_dir.is_dir():
        print(f"input directory does not exist: {input_dir}", file=sys.stderr)
        return 1

    start_date = normalize_date(args.start_date) if args.start_date else None
    end_date = normalize_date(args.end_date) if args.end_date else None
    symbol_filter = {normalize_symbol(symbol) for symbol in args.symbol or []}

    output.parent.mkdir(parents=True, exist_ok=True)

    total_written = 0
    total_skipped = 0
    file_count = 0

    with output.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=OUTPUT_COLUMNS, lineterminator="\n")
        writer.writeheader()

        for path in iter_input_files(input_dir):
            written, skipped = normalize_file(
                path=path,
                writer=writer,
                config=config,
                exchange_filter=args.exchange,
                symbol_filter=symbol_filter,
                start_date=start_date,
                end_date=end_date,
                strict=args.strict,
            )
            total_written += written
            total_skipped += skipped
            file_count += 1

    print(
        f"files={file_count} rows_written={total_written} rows_skipped={total_skipped} output={output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
