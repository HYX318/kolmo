#!/usr/bin/env python3
"""Fetch resumable BaoStock 5-minute A-share bars into per-symbol gzip files."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from kolmo.ashare.fetch_baostock_daily import (
    StockInfo,
    close_baostock_socket,
    latest_cached_universe,
    load_universe,
    login_baostock_with_retry,
    normalize_date,
    read_universe,
    run_with_reconnect,
    write_universe,
)
from kolmo.paths import data_path


FIELDS = [
    "date", "time", "code", "open", "high", "low", "close",
    "volume", "amount", "adjustflag",
]
ADJUST_FLAGS = {"hfq": "1", "qfq": "2", "raw": "3"}


@dataclass(frozen=True)
class FetchResult:
    symbol: str
    state: str
    rows: int
    path: Path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch BaoStock A-share 5-minute bars.")
    parser.add_argument("--exchange", choices=["sz", "sh"], required=True)
    parser.add_argument("--start-date", default="2017-01-01")
    parser.add_argument("--end-date", default=date.today().strftime("%Y-%m-%d"))
    parser.add_argument("--adjust", choices=["raw", "qfq", "hfq"], default="raw")
    parser.add_argument("--raw-dir", default="", help="Base directory above the adjustment folder.")
    parser.add_argument("--universe-input", default="")
    parser.add_argument("--universe-output", default="")
    parser.add_argument("--universe-fallback", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--max-universe-age-days", type=float, default=7.0)
    parser.add_argument("--symbol", action="append", help="Exact symbol, repeatable; e.g. 600519.SH.")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--overlap-days", type=int, default=5)
    parser.add_argument("--chunk-days", type=int, default=366)
    parser.add_argument("--sleep", type=float, default=2.0, help="Pause after every provider query chunk.")
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--retry-backoff-seconds", type=float, default=2.0)
    parser.add_argument("--max-rows-per-chunk", type=int, default=30_000)
    parser.add_argument("--max-consecutive-failures", type=int, default=10)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--evidence-output", default="")
    parser.add_argument("--failures-output", default="")
    return parser.parse_args(argv)


def require_baostock():
    try:
        import baostock as bs  # type: ignore
    except ModuleNotFoundError as exc:
        raise SystemExit("missing dependency: install baostock") from exc
    return bs


def compact(value: str) -> str:
    return normalize_date(value).replace("-", "")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=True, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_cache(path: Path) -> tuple[list[str], dict[tuple[str, str], dict[str, str]]]:
    if not path.is_file():
        return list(FIELDS), {}
    with gzip.open(path, "rt", encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source)
        if not reader.fieldnames or not set(FIELDS).issubset(reader.fieldnames):
            return list(FIELDS), {}
        rows = {
            (row["date"], row["time"]): row
            for row in reader
            if row.get("date") and row.get("time")
        }
        return list(reader.fieldnames), rows


def write_cache(path: Path, fieldnames: list[str], rows: dict[tuple[str, str], dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        with gzip.open(temporary, "wt", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
            for key in sorted(rows):
                writer.writerow(rows[key])
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def coverage_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.coverage.json")


def read_coverage(path: Path) -> dict[str, object] | None:
    metadata = coverage_path(path)
    if not metadata.is_file():
        return None
    try:
        payload = json.loads(metadata.read_text(encoding="utf-8"))
        if payload.get("schema_version") != "1.0.0" or payload.get("product_id") != "baostock_5min_symbol_coverage":
            return None
        return payload
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def date_chunks(start: str, end: str, chunk_days: int):
    current = datetime.strptime(normalize_date(start), "%Y-%m-%d").date()
    final = datetime.strptime(normalize_date(end), "%Y-%m-%d").date()
    while current <= final:
        chunk_end = min(final, current + timedelta(days=chunk_days - 1))
        yield current.isoformat(), chunk_end.isoformat()
        current = chunk_end + timedelta(days=1)


def result_rows(result, max_rows: int) -> list[dict[str, str]]:
    if result.error_code != "0":
        raise RuntimeError(f"BaoStock query failed: {result.error_code} {result.error_msg}")
    rows: list[dict[str, str]] = []
    while result.next():
        if len(rows) >= max_rows:
            raise RuntimeError(f"5-minute result exceeded {max_rows} rows in one chunk")
        rows.append(dict(zip(result.fields, result.get_row_data())))
    return rows


def fetch_chunk(bs, stock: StockInfo, start: str, end: str, adjust: str, max_rows: int):
    result = bs.query_history_k_data_plus(
        stock.bs_code,
        ",".join(FIELDS),
        start_date=start,
        end_date=end,
        frequency="5",
        adjustflag=ADJUST_FLAGS[adjust],
    )
    return result_rows(result, max_rows)


def effective_bounds(stock: StockInfo, start: str, end: str) -> tuple[str, str] | None:
    lower = max(normalize_date(start), normalize_date(stock.listing_date)) if stock.listing_date else normalize_date(start)
    upper = min(normalize_date(end), normalize_date(stock.delisting_date)) if stock.delisting_date else normalize_date(end)
    return (lower, upper) if lower <= upper else None


def fetch_symbol(bs, stock: StockInfo, args: argparse.Namespace, raw_dir: Path) -> FetchResult:
    path = raw_dir / args.adjust / f"{stock.symbol}.csv.gz"
    fieldnames, cached = read_cache(path) if args.resume else (list(FIELDS), {})
    bounds = effective_bounds(stock, args.start_date, args.end_date)
    if bounds is None:
        return FetchResult(stock.symbol, "cached", len(cached), path)
    requested_start, requested_end = bounds
    dates = [key[0] for key in cached]
    coverage = read_coverage(path) if args.resume else None
    if coverage is not None:
        covered_start = str(coverage.get("start_date", ""))
        covered_end = str(coverage.get("end_date", ""))
        if covered_start <= requested_start and covered_end >= requested_end:
            return FetchResult(stock.symbol, "cached", len(cached), path)
    if dates and min(dates) <= requested_start and max(dates) >= requested_end:
        return FetchResult(stock.symbol, "cached", len(cached), path)

    fetch_start = requested_start
    if dates and min(dates) <= requested_start:
        latest = datetime.strptime(max(dates), "%Y-%m-%d").date()
        fetch_start = max(
            datetime.strptime(requested_start, "%Y-%m-%d").date(),
            latest - timedelta(days=args.overlap_days),
        ).isoformat()

    incoming: dict[tuple[str, str], dict[str, str]] = {}
    for chunk_start, chunk_end in date_chunks(fetch_start, requested_end, args.chunk_days):
        rows = run_with_reconnect(
            bs,
            lambda start=chunk_start, end=chunk_end: fetch_chunk(
                bs, stock, start, end, args.adjust, args.max_rows_per_chunk
            ),
            context=f"fetch 5m {stock.symbol} {chunk_start}:{chunk_end}",
            timeout_seconds=args.timeout_seconds,
            retries=args.retries,
            retry_backoff_seconds=args.retry_backoff_seconds,
        )
        incoming.update({(row["date"], row["time"]): row for row in rows})
        if args.sleep:
            time.sleep(args.sleep)
    cached.update(incoming)
    write_cache(path, fieldnames, cached)
    write_json_atomic(
        coverage_path(path),
        {
            "schema_version": "1.0.0",
            "product_id": "baostock_5min_symbol_coverage",
            "symbol": stock.symbol,
            "adjust": args.adjust,
            "start_date": requested_start,
            "end_date": requested_end,
            "rows": len(cached),
            "completed_at": utc_now(),
        },
    )
    state = "empty" if not cached else "fetched"
    return FetchResult(stock.symbol, state, len(cached), path)


def select_universe(bs, args: argparse.Namespace, run_id: str) -> tuple[list[StockInfo], Path]:
    default_output = data_path(
        "raw", "ashare", "baostock", args.exchange, "minute", "5m",
        f"universe_{args.exchange}_{run_id}.csv",
    )
    output = Path(args.universe_output or default_output)
    if args.universe_input:
        stocks = read_universe(Path(args.universe_input), args.exchange)
    else:
        try:
            stocks = run_with_reconnect(
                bs,
                lambda: load_universe(
                    bs, normalize_date(args.start_date), normalize_date(args.end_date), True, args.exchange
                ),
                context=f"load {args.exchange} universe",
                timeout_seconds=args.timeout_seconds,
                retries=args.retries,
                retry_backoff_seconds=args.retry_backoff_seconds,
            )
        except Exception:
            if not args.universe_fallback:
                raise
            stocks = read_universe(
                latest_cached_universe(args.exchange, args.max_universe_age_days), args.exchange
            )
    if args.symbol:
        wanted = {item.upper() for item in args.symbol}
        stocks = [stock for stock in stocks if stock.symbol.upper() in wanted]
        missing = wanted - {stock.symbol.upper() for stock in stocks}
        if missing:
            raise ValueError(f"symbols not found in universe: {sorted(missing)}")
    if args.limit > 0:
        stocks = stocks[: args.limit]
    write_universe(output, stocks)
    return stocks, output


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.chunk_days < 1 or args.max_rows_per_chunk < 1 or args.max_consecutive_failures < 1:
        raise ValueError("chunk, row, and consecutive-failure limits must be positive")
    if args.overlap_days < 0 or args.sleep < 0:
        raise ValueError("overlap days and sleep must not be negative")
    run_id = args.run_id.strip() or uuid.uuid4().hex
    raw_dir = Path(
        args.raw_dir
        or data_path("raw", "ashare", "baostock", args.exchange, "minute", "5m")
    )
    evidence_path = Path(
        args.evidence_output
        or data_path("raw", "ashare", "baostock", args.exchange, "minute", "5m", "runs", f"fetch_{run_id}.json")
    )
    failures_path = Path(
        args.failures_output
        or data_path("raw", "ashare", "baostock", args.exchange, "minute", "5m", "runs", f"failures_{run_id}.csv")
    )
    evidence: dict[str, object] = {
        "schema_version": "1.0.0", "product_id": "baostock_5min_fetch",
        "run_id": run_id, "exchange": args.exchange, "adjust": args.adjust,
        "start_date": compact(args.start_date), "end_date": compact(args.end_date),
        "status": "running", "started_at": utc_now(), "completed_at": None,
        "symbols": {"total": 0, "fetched": 0, "empty": 0, "cached": 0, "failed": 0},
        "rows": 0,
    }
    write_json_atomic(evidence_path, evidence)
    bs = require_baostock()
    try:
        login_baostock_with_retry(bs, args.timeout_seconds, args.retries, args.retry_backoff_seconds)
        stocks, universe_path = select_universe(bs, args, run_id)
        evidence["symbols"] = {
            "total": len(stocks), "fetched": 0, "empty": 0, "cached": 0, "failed": 0
        }
        failures: list[dict[str, str]] = []
        consecutive_failures = 0
        total_rows = 0
        for index, stock in enumerate(stocks, start=1):
            try:
                result = fetch_symbol(bs, stock, args, raw_dir)
                counts = evidence["symbols"]
                assert isinstance(counts, dict)
                counts[result.state] = int(counts[result.state]) + 1
                total_rows += result.rows
                consecutive_failures = 0
                print(f"[{index}/{len(stocks)}] {stock.symbol} {result.state} rows={result.rows}", flush=True)
            except Exception as exc:
                counts = evidence["symbols"]
                assert isinstance(counts, dict)
                counts["failed"] = int(counts["failed"]) + 1
                failures.append({"symbol": stock.symbol, "status": stock.status, "error": repr(exc)})
                consecutive_failures += 1
                print(f"[{index}/{len(stocks)}] {stock.symbol} failed: {exc}", flush=True)
                if consecutive_failures >= args.max_consecutive_failures:
                    break
        failures_path.parent.mkdir(parents=True, exist_ok=True)
        with failures_path.open("w", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=["symbol", "status", "error"], lineterminator="\n")
            writer.writeheader()
            writer.writerows(failures)
        evidence["rows"] = total_rows
        evidence["universe_path"] = str(universe_path.resolve())
        evidence["failure_manifest_path"] = str(failures_path.resolve())
        evidence["status"] = "completed" if not failures else "completed_with_failures"
        evidence["completed_at"] = utc_now()
        write_json_atomic(evidence_path, evidence)
        print(f"done evidence={evidence_path} failures={len(failures)} rows={total_rows}")
        return 0 if not failures else 1
    except Exception as exc:
        evidence["status"] = "failed"
        evidence["completed_at"] = utc_now()
        evidence["error"] = repr(exc)
        write_json_atomic(evidence_path, evidence)
        print(f"fatal: {exc}")
        return 2
    finally:
        close_baostock_socket()


if __name__ == "__main__":
    raise SystemExit(main())
