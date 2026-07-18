#!/usr/bin/env python3
"""Incrementally update company-style A-share daily profile files."""

from __future__ import annotations

import argparse
import subprocess
import sys
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
    if args.limit > 0:
        command.extend(["--limit", str(args.limit)])
    if args.no_include_delisted:
        command.append("--no-include-delisted")

    return run(command, root)


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
