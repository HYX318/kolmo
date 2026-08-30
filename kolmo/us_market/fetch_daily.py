#!/usr/bin/env python3
"""Fetch and incrementally maintain canonical US daily bars."""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from kolmo.paths import data_path
from kolmo.us_market.daily import (
    UniverseEntry,
    daily_path,
    default_universe_path,
    load_universe,
    merge_daily_rows,
    normalize_asset_type,
    normalize_iso_date,
    normalize_symbol,
    read_daily_rows,
    validate_daily_rows,
    write_daily_rows_atomic,
    write_json_atomic,
)
from kolmo.us_market.tiingo import fetch_tiingo_daily


FetchFunction = Callable[[UniverseEntry, str, str, str], list[dict[str, str]]]


@dataclass(frozen=True)
class FetchResult:
    symbol: str
    asset_type: str
    status: str
    output: str
    request_start: str
    request_end: str
    fetched_rows: int
    total_rows: int
    error: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch Tiingo US stock and ETF daily bars.")
    parser.add_argument("--universe", default="", help="CSV with symbol and asset_type columns.")
    parser.add_argument(
        "--symbol",
        action="append",
        default=[],
        help="One asset as SYMBOL:STK or SYMBOL:ETF. Can be repeated.",
    )
    parser.add_argument("--start-date", default="1962-01-01", help="Inclusive YYYY-MM-DD.")
    parser.add_argument("--end-date", default=date.today().isoformat(), help="Inclusive YYYY-MM-DD.")
    parser.add_argument("--output-root", default="", help="Default: $KOLMO_DATA_ROOT/US.")
    parser.add_argument("--token", default="", help="Tiingo token; prefer TIINGO_API_TOKEN.")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent requests. Default: 4.")
    parser.add_argument(
        "--overlap-days",
        type=int,
        default=10,
        help="Refetch this many calendar days before the latest cached row.",
    )
    parser.add_argument("--refresh", action="store_true", help="Ignore existing files and rebuild.")
    return parser.parse_args()


def parse_symbol_args(values: list[str]) -> list[UniverseEntry]:
    entries: list[UniverseEntry] = []
    seen: set[tuple[str, str]] = set()
    for value in values:
        try:
            raw_symbol, raw_asset_type = value.rsplit(":", 1)
        except ValueError as exc:
            raise ValueError(f"--symbol must be SYMBOL:STK or SYMBOL:ETF: {value!r}") from exc
        symbol = normalize_symbol(raw_symbol)
        asset_type = normalize_asset_type(raw_asset_type)
        key = (asset_type, symbol)
        if key in seen:
            raise ValueError(f"duplicate --symbol: {asset_type}/{symbol}")
        seen.add(key)
        # Tiingo uses a dash for share classes while Kolmo keeps the familiar dot form.
        entries.append(UniverseEntry(symbol, asset_type, symbol.replace(".", "-")))
    return entries


def request_start_date(
    existing_rows: list[dict[str, str]], configured_start: str, overlap_days: int
) -> str:
    if not existing_rows:
        return configured_start
    latest = date.fromisoformat(existing_rows[-1]["date"])
    overlap_start = (latest - timedelta(days=overlap_days)).isoformat()
    return max(configured_start, overlap_start)


def corporate_action_changed(
    existing_rows: list[dict[str, str]], incoming_rows: list[dict[str, str]]
) -> bool:
    existing_by_date = {row["date"]: row for row in existing_rows}
    for row in incoming_rows:
        previous = existing_by_date.get(row["date"])
        has_action = float(row["div_cash"]) != 0.0 or float(row["split_factor"]) != 1.0
        previous_had_action = previous is not None and (
            float(previous["div_cash"]) != 0.0 or float(previous["split_factor"]) != 1.0
        )
        if (has_action or previous_had_action) and (
            previous is None
            or float(previous["div_cash"]) != float(row["div_cash"])
            or float(previous["split_factor"]) != float(row["split_factor"])
        ):
            return True
    return False


def update_one(
    entry: UniverseEntry,
    us_root: Path,
    token: str,
    start_date: str,
    end_date: str,
    overlap_days: int,
    refresh: bool,
    fetch: FetchFunction = fetch_tiingo_daily,
) -> FetchResult:
    output = daily_path(us_root, entry)
    existing = [] if refresh else read_daily_rows(output)
    request_start = request_start_date(existing, start_date, overlap_days)
    if request_start > end_date:
        return FetchResult(
            entry.symbol, entry.asset_type, "current", str(output), request_start, end_date,
            0, len(existing),
        )
    incoming = fetch(entry, token, request_start, end_date)
    if existing and request_start > start_date and corporate_action_changed(existing, incoming):
        # 新分红或拆股会改写供应商的全部复权历史，因此此时自动做一次全量刷新。
        request_start = start_date
        incoming = fetch(entry, token, request_start, end_date)
        existing = []
    merged = merge_daily_rows(existing, incoming)
    errors = validate_daily_rows(merged, entry.symbol, entry.asset_type)
    if errors:
        preview = "; ".join(errors[:5])
        raise ValueError(f"validation failed for {entry.asset_type}/{entry.symbol}: {preview}")
    # 空响应时保留已有文件；首次抓取空响应则视为数据错误。
    if incoming or not output.exists():
        write_daily_rows_atomic(output, merged)
    return FetchResult(
        entry.symbol, entry.asset_type, "updated", str(output), request_start, end_date,
        len(incoming), len(merged),
    )


def run_fetch(
    entries: list[UniverseEntry],
    us_root: Path,
    token: str,
    start_date: str,
    end_date: str,
    overlap_days: int,
    refresh: bool,
    workers: int,
    fetch: FetchFunction = fetch_tiingo_daily,
) -> list[FetchResult]:
    results: list[FetchResult] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                update_one, entry, us_root, token, start_date, end_date,
                overlap_days, refresh, fetch,
            ): entry
            for entry in entries
        }
        for future in as_completed(futures):
            entry = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # 每个标的隔离失败，完整结果记录在运行证据中。
                result = FetchResult(
                    entry.symbol, entry.asset_type, "failed", str(daily_path(us_root, entry)),
                    start_date, end_date, 0, 0, str(exc),
                )
            results.append(result)
            detail = f" rows={result.total_rows}" if result.status != "failed" else f" error={result.error}"
            print(f"{result.asset_type}/{result.symbol} status={result.status}{detail}", flush=True)
    return sorted(results, key=lambda item: (item.asset_type, item.symbol))


def main() -> int:
    args = parse_args()
    try:
        start_date = normalize_iso_date(args.start_date)
        end_date = normalize_iso_date(args.end_date)
        if start_date > end_date:
            raise ValueError("start date must not be after end date")
        if args.workers < 1 or args.workers > 16:
            raise ValueError("workers must be between 1 and 16")
        if args.overlap_days < 0:
            raise ValueError("overlap-days must be non-negative")
        if args.symbol:
            entries = parse_symbol_args(args.symbol)
        else:
            universe = Path(args.universe) if args.universe else default_universe_path()
            entries = load_universe(universe)
        token = args.token or os.environ.get("TIINGO_API_TOKEN", "")
        if not token:
            raise ValueError("missing Tiingo token; set TIINGO_API_TOKEN or pass --token")
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    us_root = Path(args.output_root) if args.output_root else data_path("US")
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    started_at = datetime.now(timezone.utc).isoformat()
    results = run_fetch(
        entries, us_root, token, start_date, end_date, args.overlap_days,
        args.refresh, args.workers,
    )
    failed = sum(result.status == "failed" for result in results)
    evidence = {
        "run_id": run_id,
        "provider": "tiingo.eod",
        "status": "completed" if failed == 0 else "completed_with_failures",
        "started_at": started_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "request": {
            "start_date": start_date,
            "end_date": end_date,
            "overlap_days": args.overlap_days,
            "refresh": bool(args.refresh),
            "workers": args.workers,
        },
        "assets": {"requested": len(results), "succeeded": len(results) - failed, "failed": failed},
        "results": [result.__dict__ for result in results],
    }
    evidence_path = us_root / "_meta" / "fetch_runs" / f"{run_id}.json"
    write_json_atomic(evidence_path, evidence)
    print(f"evidence={evidence_path}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
