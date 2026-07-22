#!/usr/bin/env python3
"""Incrementally update company-style A-share daily profile files."""

from __future__ import annotations

import argparse
import csv
import gzip
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

from kolmo.paths import data_path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_end_date() -> str:
    return date.today().strftime("%Y%m%d")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Incrementally update A-share profile/daily/{exchange}/YYYY/MM/YYYYMMDD.csv files. "
            "The update starts from the latest local profile date minus a lookback window."
        )
    )
    parser.add_argument(
        "--exchange",
        default="all",
        choices=["sz", "sh", "all"],
        help="Exchange to update: sz=SZSE, sh=SSE, all=both.",
    )
    parser.add_argument(
        "--target",
        default="all",
        choices=["profile", "raw", "all"],
        help="profile=update daily profile files, raw=update per-symbol gzip caches, all=both.",
    )
    parser.add_argument(
        "--output-dir",
        default="",
        help="Company-style daily profile output directory.",
    )
    parser.add_argument(
        "--end-date",
        default=default_end_date(),
        help="Inclusive YYYYMMDD end date. Defaults to today.",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=10,
        help="Calendar days to re-fetch before the latest local date.",
    )
    parser.add_argument(
        "--adjust",
        default="qfq",
        choices=["qfq", "hfq", "raw"],
        help="qfq=front adjusted, hfq=back adjusted, raw=unadjusted.",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.0,
        help="Seconds to sleep between symbols while fetching.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Parallel workers for merging independent per-symbol gzip caches.",
    )
    parser.add_argument(
        "--refresh-raw",
        action="store_true",
        help="With --target raw, fetch a new window before merging instead of reusing an existing window.",
    )
    parser.add_argument(
        "--raw-window",
        default="",
        help="With --target raw, reuse this daily_incremental window (YYYYMMDD_YYYYMMDD).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Optional symbol limit for testing.",
    )
    parser.add_argument(
        "--full-start-date",
        default="20170101",
        help="Start date to use when no local profile files exist.",
    )
    parser.add_argument(
        "--no-include-delisted",
        action="store_true",
        help="Only fetch currently active A shares for the selected exchange.",
    )
    return parser.parse_args()


def compact_date(value: str) -> str:
    return value.replace("-", "")


def latest_profile_date(output_dir: Path) -> str | None:
    latest: str | None = None
    for path in output_dir.glob("*/*/*.csv"):
        stem = path.stem
        if len(stem) == 8 and stem.isdigit():
            latest = stem if latest is None or stem > latest else latest
    for path in output_dir.glob("*/*/*.txt"):
        stem = path.stem
        if len(stem) == 8 and stem.isdigit():
            latest = stem if latest is None or stem > latest else latest
    return latest


def subtract_days(yyyymmdd: str, days: int) -> str:
    parsed = datetime.strptime(yyyymmdd, "%Y%m%d").date()
    return (parsed - timedelta(days=days)).strftime("%Y%m%d")


def run(command: list[str], cwd: Path) -> int:
    print("+ " + " ".join(command), flush=True)
    return subprocess.run(command, cwd=str(cwd), check=False).returncode


def read_gzip_rows(path: Path) -> tuple[list[str], dict[str, dict[str, str]]]:
    with gzip.open(path, "rt", encoding="utf-8", newline="") as input_file:
        reader = csv.DictReader(input_file)
        if not reader.fieldnames or "date" not in reader.fieldnames:
            raise ValueError(f"raw cache must contain a date column: {path}")
        return list(reader.fieldnames), {row["date"]: row for row in reader if row.get("date")}


def merge_raw_cache(incremental_path: Path, canonical_path: Path) -> None:
    """Merge one incremental gzip cache into the full per-symbol cache by date."""
    fieldnames, rows = read_gzip_rows(canonical_path) if canonical_path.exists() else ([], {})
    incremental_fieldnames, incremental_rows = read_gzip_rows(incremental_path)
    # 上游新增估值列时，允许增量文件升级旧 OHLCV 缓存；旧日期对应的新列留空，
    # 避免一次字段扩展让正常的增量更新整体失败。
    fieldnames = list(fieldnames)
    for field in incremental_fieldnames:
        if field not in fieldnames:
            fieldnames.append(field)
    rows.update(incremental_rows)  # 增量窗口覆盖同一交易日的旧上游数据。

    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{canonical_path.name}.", suffix=".tmp", dir=canonical_path.parent
    )
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)
    try:
        with gzip.open(temporary_path, "wt", encoding="utf-8", newline="") as output_file:
            writer = csv.DictWriter(output_file, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
            for trade_date in sorted(rows):
                writer.writerow(rows[trade_date])
        os.replace(temporary_path, canonical_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def sync_incremental_raw_cache(raw_dir: Path, canonical_dir: Path, adjust: str, workers: int) -> int:
    incremental_dir = raw_dir / adjust
    if not incremental_dir.exists():
        return 0

    paths = sorted(incremental_dir.glob("*.csv.gz"))
    if workers < 1:
        raise ValueError("--workers must be at least 1")

    # 每个任务只读写一只股票，线程间没有共享目标文件；gzip 的压缩工作可并行执行。
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(merge_raw_cache, path, canonical_dir / adjust / path.name)
            for path in paths
        ]
        for future in futures:
            future.result()
    return len(paths)


def existing_incremental_raw_dir(exchange: str, adjust: str, raw_window: str) -> Path:
    root = data_path("raw", "ashare", "baostock", exchange, "daily_incremental")
    if raw_window:
        candidate = root / raw_window
        if (candidate / adjust).is_dir():
            return candidate
        raise FileNotFoundError(f"incremental raw window not found: {candidate / adjust}")

    candidates = [path for path in root.iterdir() if path.is_dir() and (path / adjust).is_dir()] if root.exists() else []
    if not candidates:
        raise FileNotFoundError(
            f"no incremental raw cache found under {root}; rerun with --refresh-raw or use --target all"
        )
    return max(candidates, key=lambda path: path.stat().st_mtime)


def update_exchange(args: argparse.Namespace, root: Path, exchange: str) -> int:
    output_dir = Path(
        args.output_dir or data_path("profile", "daily", exchange)
    )
    end_date = compact_date(args.end_date)
    latest = latest_profile_date(output_dir)

    if latest is None:
        start_date = compact_date(args.full_start_date)
        print(f"no local profile files found; running full build from {start_date}")
    else:
        start_date = subtract_days(latest, args.lookback_days)
        print(f"latest local profile date={latest}; updating from {start_date} to {end_date}")

    # Incremental raw cache is date-window partitioned. Do not reuse full-run
    # per-symbol cache files whose date coverage may be different.
    raw_dir = (
        data_path("raw", "ashare", "baostock", exchange, "daily_incremental", f"{start_date}_{end_date}")
    )
    combined_output = data_path(
        "work",
        "ashare",
        "incremental",
        f"{exchange}_daily_bars_{start_date}_{end_date}_{args.adjust}_baostock.csv",
    )

    if args.target == "raw" and not args.refresh_raw:
        raw_dir = existing_incremental_raw_dir(exchange, args.adjust, args.raw_window)
        print(f"reusing incremental raw cache={raw_dir}")
        command: list[str] | None = None
    elif args.target == "raw":
        command = [
            sys.executable,
            "-m",
            "kolmo.ashare.fetch_baostock_daily",
            "--exchange",
            exchange,
            "--start-date",
            start_date,
            "--end-date",
            end_date,
            "--adjust",
            args.adjust,
            "--raw-dir",
            str(raw_dir),
            "--no-combine",
            "--sleep",
            str(args.sleep),
        ]
    else:
        command = [
            sys.executable,
            "-m",
            "kolmo.ashare.build_cn_profile_daily",
            "--exchange",
            exchange,
            "--start-date",
            start_date,
            "--end-date",
            end_date,
            "--adjust",
            args.adjust,
            "--output-dir",
            str(output_dir),
            "--combined-output",
            str(combined_output),
            "--raw-dir",
            str(raw_dir),
            "--sleep",
            str(args.sleep),
        ]
    if command is not None:
        if args.limit > 0:
            command.extend(["--limit", str(args.limit)])
        if args.no_include_delisted:
            command.append("--no-include-delisted")

        code = run(command, root)
        if code != 0:
            return code

    if args.target != "profile":
        canonical_raw_dir = data_path("raw", "ashare", "baostock", exchange, "daily")
        merged = sync_incremental_raw_cache(raw_dir, canonical_raw_dir, args.adjust, args.workers)
        print(f"synced full per-symbol raw caches={merged}")
    return 0


def main() -> int:
    args = parse_args()
    root = repo_root()
    exchanges = ["sz", "sh"] if args.exchange == "all" else [args.exchange]

    if args.exchange == "all" and args.output_dir:
        print("--output-dir is only valid for a single exchange", file=sys.stderr)
        return 2

    exit_code = 0
    for exchange in exchanges:
        code = update_exchange(args, root, exchange)
        exit_code = exit_code or code
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
