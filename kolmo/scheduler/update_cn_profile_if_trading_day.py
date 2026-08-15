#!/usr/bin/env python3
"""Run the A-share daily profile updater only on exchange trading days."""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def compact_date(value: str) -> str:
    return value.replace("-", "")


def dashed_date(value: str) -> str:
    compact = compact_date(value)
    if len(compact) != 8 or not compact.isdigit():
        raise ValueError("date must be YYYYMMDD or YYYY-MM-DD")
    return f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"


def today_compact() -> str:
    return date.today().strftime("%Y%m%d")


def is_weekday(compact: str) -> bool:
    parsed = datetime.strptime(compact, "%Y%m%d").date()
    return parsed.weekday() < 5


def baostock_trading_day(compact: str) -> bool:
    """Return whether BaoStock marks `compact` as an exchange trading day."""
    try:
        import baostock as bs  # type: ignore[import-not-found]
    except ImportError as error:
        raise RuntimeError("baostock is required for exact trading-day checks") from error

    login = bs.login()
    if getattr(login, "error_code", "0") != "0":
        raise RuntimeError(f"baostock login failed: {getattr(login, 'error_msg', '')}")
    try:
        result = bs.query_trade_dates(start_date=dashed_date(compact), end_date=dashed_date(compact))
        if getattr(result, "error_code", "0") != "0":
            raise RuntimeError(f"query_trade_dates failed: {getattr(result, 'error_msg', '')}")
        while result.next():
            row = result.get_row_data()
            if len(row) >= 2:
                return row[1] == "1"
        return False
    finally:
        bs.logout()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check A-share trading day status before running kolmo.ashare.update_cn_profile_daily."
    )
    parser.add_argument("--date", default=today_compact(), help="Date to check, YYYYMMDD or YYYY-MM-DD.")
    parser.add_argument(
        "--calendar",
        choices=["baostock", "weekday"],
        default="baostock",
        help="baostock is holiday-aware; weekday is only a local fallback for dry runs.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print the update command without executing it.")
    parser.add_argument("update_args", nargs=argparse.REMAINDER, help="Arguments passed to update_cn_profile_daily.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    trade_date = compact_date(args.date)
    if args.update_args and args.update_args[0] == "--":
        args.update_args = args.update_args[1:]

    trading_day = baostock_trading_day(trade_date) if args.calendar == "baostock" else is_weekday(trade_date)
    if not trading_day:
        print(f"{trade_date} is not an A-share trading day; skip update.", flush=True)
        return 0

    command = [sys.executable, "-m", "kolmo.ashare.update_cn_profile_daily", "--end-date", trade_date]
    command.extend(args.update_args)
    print("+ " + " ".join(command), flush=True)
    if args.dry_run:
        return 0
    return subprocess.run(command, cwd=str(project_root()), check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
