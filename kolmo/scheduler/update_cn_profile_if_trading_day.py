#!/usr/bin/env python3
"""Run the A-share daily profile updater only on exchange trading days."""

from __future__ import annotations

import argparse
import socket
import sys
from datetime import date, datetime
from pathlib import Path

from kolmo.ashare.fetch_baostock_daily import (
    close_baostock_socket,
    hard_deadline,
    login_baostock,
)
from kolmo.scheduler.process_timeout import run_with_timeout


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


def baostock_trading_day(compact: str, timeout_seconds: float = 15.0) -> bool:
    """Return whether BaoStock marks `compact` as an exchange trading day."""
    try:
        import baostock as bs  # type: ignore[import-not-found]
    except ImportError as error:
        raise RuntimeError("baostock is required for exact trading-day checks") from error

    previous_socket_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout_seconds)
    try:
        login_baostock(bs, timeout_seconds)
        with hard_deadline(timeout_seconds):
            result = bs.query_trade_dates(
                start_date=dashed_date(compact), end_date=dashed_date(compact)
            )
            if getattr(result, "error_code", "0") != "0":
                raise RuntimeError(f"query_trade_dates failed: {getattr(result, 'error_msg', '')}")
            while result.next():
                row = result.get_row_data()
                if len(row) >= 2:
                    return row[1] == "1"
            return False
    finally:
        close_baostock_socket()
        socket.setdefaulttimeout(previous_socket_timeout)


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
    parser.add_argument(
        "--calendar-timeout-seconds",
        type=float,
        default=15.0,
        help="Hard deadline for the BaoStock calendar check. Default: 15 seconds.",
    )
    parser.add_argument(
        "--update-timeout-seconds",
        type=float,
        default=4000.0,
        help="Maximum wall time for the complete SZ/SH update. Default: 4000 seconds.",
    )
    parser.add_argument(
        "--skip-exit-code",
        type=int,
        default=0,
        help="Exit code for a non-trading-day skip. The scheduled wrapper uses 20.",
    )
    parser.add_argument("update_args", nargs=argparse.REMAINDER, help="Arguments passed to update_cn_profile_daily.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.calendar_timeout_seconds <= 0:
        raise ValueError("--calendar-timeout-seconds must be positive")
    if args.update_timeout_seconds <= 0:
        raise ValueError("--update-timeout-seconds must be positive")
    trade_date = compact_date(args.date)
    if args.update_args and args.update_args[0] == "--":
        args.update_args = args.update_args[1:]

    trading_day = (
        baostock_trading_day(trade_date, args.calendar_timeout_seconds)
        if args.calendar == "baostock"
        else is_weekday(trade_date)
    )
    if not trading_day:
        print(f"{trade_date} is not an A-share trading day; skip update.", flush=True)
        return args.skip_exit_code

    command = [sys.executable, "-m", "kolmo.ashare.update_cn_profile_daily", "--end-date", trade_date]
    command.extend(args.update_args)
    print("+ " + " ".join(command), flush=True)
    if args.dry_run:
        return 0
    return run_with_timeout(
        command,
        cwd=project_root(),
        timeout_seconds=args.update_timeout_seconds,
        label="complete A-share update",
    )


if __name__ == "__main__":
    raise SystemExit(main())
