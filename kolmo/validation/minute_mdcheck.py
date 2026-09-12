#!/usr/bin/env python3
"""Structural and cross-frequency checks for annual A-share minute archives."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from kolmo.ashare.vendor_minute import DEFAULT_ROOT, FREQUENCIES, REQUIRED_COLUMNS, VendorMinuteDataset
from kolmo.ashare.minute_product import (
    bucket_1m_trade_time,
    expected_bars_per_day,
    expected_minute_of_day,
)
from kolmo.paths import data_path


PRICE_TOLERANCE = 0.005
VOLUME_TOLERANCE = 10.0
AMOUNT_TOLERANCE = 200.0


@dataclass
class Finding:
    symbol: str
    frequency: str
    check: str
    severity: str
    count: int
    total: int
    max_abs_diff: float | None = None
    example: str = ""


def aggregate_daily(frame: pd.DataFrame) -> pd.DataFrame:
    work = frame.assign(date=frame["trade_time"].dt.strftime("%Y-%m-%d"))
    return work.groupby("date", sort=True).agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"),
        close=("close", "last"), vol=("vol", "sum"), amount=("amount", "sum"),
    )


def aggregate_1m(frame: pd.DataFrame, frequency: int) -> pd.DataFrame:
    work = frame.sort_values("trade_time").copy()
    work["trade_time"] = bucket_1m_trade_time(work["trade_time"], frequency)
    return work.groupby("trade_time", as_index=True).agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"),
        close=("close", "last"), vol=("vol", "sum"), amount=("amount", "sum"),
    )


def _add(findings: list[Finding], symbol: str, frequency: int | str, check: str,
         count: int, total: int, severity: str = "error", example: str = "",
         max_abs_diff: float | None = None) -> None:
    if count:
        findings.append(Finding(symbol, str(frequency), check, severity, int(count), int(total), max_abs_diff, example))


def validate_frame(symbol: str, frequency: int, frame: pd.DataFrame) -> tuple[dict[str, Any], list[Finding]]:
    findings: list[Finding] = []
    missing = sorted(set(REQUIRED_COLUMNS).difference(frame.columns))
    if missing:
        _add(findings, symbol, frequency, "schema_missing_columns", len(missing), len(REQUIRED_COLUMNS), example=",".join(missing))
        return {"rows": len(frame)}, findings
    rows = len(frame)
    null_rows = int(frame[list(REQUIRED_COLUMNS)].isna().any(axis=1).sum())
    _add(findings, symbol, frequency, "null_row", null_rows, rows)
    symbol_mismatch = int(frame["ts_code"].ne(symbol).sum())
    _add(findings, symbol, frequency, "symbol_mismatch", symbol_mismatch, rows)
    freq_mismatch = int(frame["freq"].ne(f"{frequency}min").sum())
    _add(findings, symbol, frequency, "frequency_tag_mismatch", freq_mismatch, rows)
    duplicates = int(frame["trade_time"].duplicated().sum())
    _add(findings, symbol, frequency, "duplicate_timestamp", duplicates, rows)
    if not frame["trade_time"].is_monotonic_increasing:
        _add(findings, symbol, frequency, "timestamp_not_sorted", 1, 1)
    price = frame[["open", "high", "low", "close"]]
    invalid_ohlc = (
        frame["low"].gt(price[["open", "close"]].min(axis=1))
        | frame["high"].lt(price[["open", "close"]].max(axis=1))
        | frame["low"].gt(frame["high"])
    )
    _add(findings, symbol, frequency, "invalid_ohlc", int(invalid_ohlc.sum()), rows)
    _add(findings, symbol, frequency, "nonpositive_price", int(price.le(0).any(axis=1).sum()), rows)
    minutes = frame["trade_time"].dt.hour * 60 + frame["trade_time"].dt.minute
    exchange = symbol.rsplit(".", 1)[-1].lower()
    post_close_adjustment = exchange == "bj" and minutes.between(901, 930)
    negative_volume = frame["vol"].lt(0)
    negative_amount = frame["amount"].lt(0)
    allowed_adjustment = (negative_volume | negative_amount) & post_close_adjustment
    _add(findings, symbol, frequency, "negative_volume", int((negative_volume & ~allowed_adjustment).sum()), rows)
    _add(findings, symbol, frequency, "negative_amount", int((negative_amount & ~allowed_adjustment).sum()), rows)
    _add(
        findings, symbol, frequency, "post_close_negative_adjustment",
        int(allowed_adjustment.sum()), rows, "warning",
    )
    invalid_time = (
        ~minutes.isin(expected_minute_of_day(frequency, exchange))
        | frame["trade_time"].dt.second.ne(0)
    )
    _add(findings, symbol, frequency, "invalid_session_timestamp", int(invalid_time.sum()), rows)
    by_date = frame.groupby(frame["trade_time"].dt.strftime("%Y-%m-%d"), sort=True)
    counts = by_date.size()
    irregular = counts.ne(expected_bars_per_day(frequency, exchange))
    _add(
        findings, symbol, frequency, "unexpected_bars_per_day", int(irregular.sum()), len(counts),
        example=",".join(f"{d}:{counts.loc[d]}" for d in counts.index[irregular][:3]),
    )
    daily_volume = by_date["vol"].sum()
    zero_days = int(daily_volume.eq(0).sum())
    _add(findings, symbol, frequency, "zero_volume_day", zero_days, len(daily_volume), "info")
    if len(daily_volume) and zero_days == len(daily_volume):
        _add(findings, symbol, frequency, "all_year_zero_volume_grid", 1, 1, "warning")
    tz = str(frame["trade_time"].dt.tz)
    if tz != "Asia/Shanghai":
        _add(findings, symbol, frequency, "timezone_mismatch", 1, 1, example=tz)
    return {"rows": rows, "dates": len(counts), "zero_volume_days": zero_days}, findings


def tolerance_for(field: str, left: pd.Series) -> pd.Series:
    absolute = {"vol": VOLUME_TOLERANCE, "amount": AMOUNT_TOLERANCE}.get(field, PRICE_TOLERANCE)
    relative = left.abs() * (1e-8 if field in {"vol", "amount"} else 0.0)
    return relative.clip(lower=absolute)


def compare_frames(symbol: str, frequency: int | str, left: pd.DataFrame, right: pd.DataFrame,
                   scope: str) -> tuple[dict[str, Any], list[Finding]]:
    findings: list[Finding] = []
    joined = left.join(right, lsuffix="_1m", rsuffix=f"_{frequency}m", how="outer")
    missing_target = joined[f"close_{frequency}m"].isna()
    extra_target = joined["close_1m"].isna()
    _add(findings, symbol, frequency, f"{scope}_missing_target", int(missing_target.sum()), len(joined))
    _add(findings, symbol, frequency, f"{scope}_extra_target", int(extra_target.sum()), len(joined))
    both = joined.loc[~missing_target & ~extra_target]
    metrics: dict[str, Any] = {
        "rows_1m": len(left), "rows_target": len(right),
        "missing_target": int(missing_target.sum()), "extra_target": int(extra_target.sum()),
    }
    for field in ("open", "high", "low", "close", "vol", "amount"):
        lhs = both[f"{field}_1m"]
        rhs = both[f"{field}_{frequency}m"]
        diff = (lhs - rhs).abs()
        mismatched = diff.gt(tolerance_for(field, lhs))
        count = int(mismatched.sum())
        maximum = float(diff.max()) if len(diff) else 0.0
        metrics[field] = {"mismatch": count, "total": len(diff), "max_abs_diff": maximum}
        _add(
            findings, symbol, frequency, f"{scope}_{field}_mismatch", count, len(diff),
            "warning", max_abs_diff=maximum,
            example=str(diff.index[mismatched][0]) if count else "",
        )
    return metrics, findings


def read_daily_reference(root: Path, symbol: str, dates: list[str]) -> pd.DataFrame:
    exchange = symbol[-2:].lower()
    records: list[dict[str, Any]] = []
    for date in dates:
        base = root / exchange / date[:4] / date[4:6] / date
        path = next((p for p in (base.with_suffix(".csv"), base.with_suffix(".csv.gz")) if p.is_file()), None)
        if path is None:
            continue
        opener = gzip.open if path.name.endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8", newline="") as source:
            for row in csv.DictReader(source):
                if row.get("symbol", "").upper() == symbol:
                    records.append({"date": date, **{name: float(row[name]) for name in ("open", "high", "low", "close", "volume", "amount")}})
                    break
    if not records:
        return pd.DataFrame()
    return pd.DataFrame(records).rename(columns={"volume": "vol"}).set_index("date")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run market-data checks on vendor minute ZIP files.")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--symbols", nargs="*", default=[])
    parser.add_argument("--limit", type=int, default=0, help="Check only the first N selected symbols; 0 means all.")
    parser.add_argument("--daily-root", type=Path, default=data_path("profile", "daily_raw"))
    parser.add_argument("--skip-daily-reference", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--details", type=Path, help="Optional findings CSV; defaults beside --output.")
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> dict[str, Any]:
    archives = {freq: VendorMinuteDataset.discover(args.root, args.year, freq) for freq in FREQUENCIES}
    union = set().union(*(archive.symbols for archive in archives.values()))
    base_symbols = set(archives[1].symbols)
    requested = {s.upper() for s in args.symbols}
    symbols = sorted(requested or base_symbols)
    if args.limit:
        symbols = symbols[:args.limit]
    findings: list[Finding] = []
    report: dict[str, Any] = {
        "year": args.year,
        "archives": {
            str(freq): {
                "paths": [str(path) for path in archive.paths], "symbols": len(archive.symbols),
                "missing_from_union": sorted(union.difference(archive.symbols)),
            } for freq, archive in archives.items()
        },
        "symbols_checked": len(symbols), "symbol_metrics": {},
    }
    try:
        for index, symbol in enumerate(symbols, 1):
            frames: dict[int, pd.DataFrame] = {}
            symbol_metrics: dict[str, Any] = {"frequency": {}, "cross_frequency": {}, "daily_reference": {}}
            for freq, archive in archives.items():
                if symbol not in archive.symbols:
                    _add(findings, symbol, freq, "symbol_missing_from_archive", 1, 1)
                    continue
                frame = archive.read_symbol(symbol)
                frames[freq] = frame
                metrics, issues = validate_frame(symbol, freq, frame)
                symbol_metrics["frequency"][str(freq)] = metrics
                findings.extend(issues)
            if 1 in frames:
                daily_1m = aggregate_daily(frames[1])
                for freq in FREQUENCIES[1:]:
                    if freq not in frames:
                        continue
                    daily_metrics, issues = compare_frames(symbol, freq, daily_1m, aggregate_daily(frames[freq]), "daily")
                    findings.extend(issues)
                    target = frames[freq].set_index("trade_time")[["open", "high", "low", "close", "vol", "amount"]]
                    bar_metrics, issues = compare_frames(symbol, freq, aggregate_1m(frames[1], freq), target, "bar")
                    findings.extend(issues)
                    symbol_metrics["cross_frequency"][str(freq)] = {"daily": daily_metrics, "bar": bar_metrics}
                if not args.skip_daily_reference:
                    dates = [date.replace("-", "") for date in daily_1m.index]
                    reference = read_daily_reference(args.daily_root, symbol, dates)
                    if not reference.empty:
                        reference.index = [f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in reference.index]
                        metrics, issues = compare_frames(symbol, "daily", daily_1m, reference, "reference")
                        findings.extend(issues)
                        symbol_metrics["daily_reference"] = metrics
            report["symbol_metrics"][symbol] = symbol_metrics
            count = sum(f.symbol == symbol and f.severity != "info" for f in findings)
            print(f"[{index}/{len(symbols)}] {symbol} findings={count}", flush=True)
    finally:
        for archive in archives.values():
            archive.close()
    report["findings"] = [asdict(finding) for finding in findings]
    report["summary"] = {
        "errors": sum(f.severity == "error" for f in findings),
        "warnings": sum(f.severity == "warning" for f in findings),
        "info": sum(f.severity == "info" for f in findings),
    }
    return report


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = run(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    details = args.details or args.output.with_suffix(".findings.csv")
    details.parent.mkdir(parents=True, exist_ok=True)
    fields = list(Finding.__dataclass_fields__)
    with details.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(report["findings"])
    print(json.dumps({"output": str(args.output), "details": str(details), **report["summary"]}, ensure_ascii=False))
    return 1 if report["summary"]["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
