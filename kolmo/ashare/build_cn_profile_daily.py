#!/usr/bin/env python3
"""Build company-style A-share daily profile files with one command."""

from __future__ import annotations

import argparse
import subprocess
import sys
import uuid
from datetime import date
from pathlib import Path

from kolmo.paths import data_path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_end_date() -> str:
    return date.today().strftime("%Y%m%d")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch real A-share daily bars and store them as "
            "profile/daily/{exchange}/YYYY/MM/YYYYMMDD.csv files."
        )
    )
    parser.add_argument(
        "--exchange",
        default="all",
        choices=["sz", "sh", "all"],
        help="Exchange to build: sz=SZSE, sh=SSE, all=both.",
    )
    parser.add_argument("--start-date", default="20170101", help="Inclusive YYYYMMDD start date.")
    parser.add_argument(
        "--end-date",
        default=default_end_date(),
        help="Inclusive YYYYMMDD end date. Defaults to today.",
    )
    parser.add_argument(
        "--adjust",
        default="qfq",
        choices=["qfq", "hfq", "raw"],
        help="qfq=front adjusted, hfq=back adjusted, raw=unadjusted.",
    )
    parser.add_argument(
        "--output-dir",
        default="",
        help="Company-style daily profile output directory.",
    )
    parser.add_argument(
        "--combined-output",
        default="",
        help="Intermediate combined CSV path. Defaults under KOLMO_DATA_ROOT/work/ashare/.",
    )
    parser.add_argument(
        "--raw-dir",
        default="",
        help="Per-symbol raw cache directory.",
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
        help="Optional symbol limit for smoke testing. Omit for full universe.",
    )
    parser.add_argument(
        "--extension",
        choices=["csv", "txt"],
        default="csv",
        help="Daily profile file extension.",
    )
    parser.add_argument(
        "--delimiter",
        default=",",
        help="Daily profile delimiter. Use '\\t' with --extension txt if needed.",
    )
    parser.add_argument(
        "--no-include-delisted",
        action="store_true",
        help="Only fetch currently active A shares for the selected exchange.",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Do not reuse existing per-symbol raw CSV cache.",
    )
    parser.add_argument(
        "--partition-on-fetch-failure",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Explicit recovery override: partition partial combined rows even when some "
            "symbols fail. Disabled by default so production partitions remain unchanged."
        ),
    )
    parser.add_argument(
        "--run-id",
        default="",
        help="Optional immutable fetch run identifier. Defaults to a UUID per exchange build.",
    )
    return parser.parse_args()


def compact_date(value: str) -> str:
    return value.replace("-", "")


def exchange_suffix(exchange: str) -> str:
    return exchange.upper()


def run(command: list[str], cwd: Path) -> int:
    print("+ " + " ".join(command), flush=True)
    return subprocess.run(command, cwd=str(cwd), check=False).returncode


def build_exchange(args: argparse.Namespace, root: Path, exchange: str) -> int:
    start_date = compact_date(args.start_date)
    end_date = compact_date(args.end_date)
    output_dir = args.output_dir or str(data_path("profile", "daily", exchange))
    raw_dir = args.raw_dir or str(data_path("raw", "ashare", "baostock", exchange, "daily"))
    combined_output = Path(
        args.combined_output
        or data_path(
            "work",
            "ashare",
            f"{exchange}_daily_bars_{start_date}_{end_date}_{args.adjust}_baostock.csv",
        )
    )
    run_id = args.run_id.strip() or uuid.uuid4().hex
    evidence_output = data_path(
        "raw", "ashare", "baostock", exchange, "runs", f"fetch_{exchange}_{run_id}.json"
    )
    failures_output = data_path(
        "raw", "ashare", "baostock", exchange, "runs", f"failures_{exchange}_{run_id}.csv"
    )

    fetch_command = [
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
        raw_dir,
        "--run-id",
        run_id,
        "--evidence-output",
        str(evidence_output),
        "--failures-output",
        str(failures_output),
        "--clean-output",
        str(combined_output),
        "--sleep",
        str(args.sleep),
    ]
    if args.limit > 0:
        fetch_command.extend(["--limit", str(args.limit)])
    if args.no_include_delisted:
        fetch_command.append("--no-include-delisted")
    if args.no_resume:
        fetch_command.append("--no-resume")

    fetch_code = run(fetch_command, root)
    if fetch_code != 0 and not args.partition_on_fetch_failure:
        return fetch_code
    if not combined_output.exists() or combined_output.stat().st_size == 0:
        return fetch_code if fetch_code != 0 else 1

    partition_command = [
        sys.executable,
        "-m",
        "kolmo.ashare.partition_daily_bars_by_date",
        "--input",
        str(combined_output),
        "--output-dir",
        output_dir,
        "--layout",
        "year-month",
        "--extension",
        args.extension,
        "--delimiter",
        args.delimiter,
        "--fetch-evidence",
        str(evidence_output),
    ]
    partition_code = run(partition_command, root)
    if partition_code != 0:
        return partition_code

    if fetch_code != 0:
        print(
            "warning: fetch reported failures; daily profile files were built from available rows. "
            f"Check {data_path('raw', 'ashare', 'baostock', exchange)}/failures_*.csv.",
            file=sys.stderr,
        )
    return fetch_code


def main() -> int:
    args = parse_args()
    root = repo_root()
    exchanges = ["sz", "sh"] if args.exchange == "all" else [args.exchange]

    if args.exchange == "all" and (args.output_dir or args.raw_dir or args.combined_output):
        print(
            "--output-dir, --raw-dir, and --combined-output are only valid for a single exchange",
            file=sys.stderr,
        )
        return 2

    exit_code = 0
    for exchange in exchanges:
        code = build_exchange(args, root, exchange)
        exit_code = exit_code or code
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
