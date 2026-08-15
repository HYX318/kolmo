#!/usr/bin/env python3
"""Fetch a versioned A-share trading-calendar observation from BaoStock."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from kolmo.data_products import TRADING_CALENDAR_COLUMNS, normalize_date
from kolmo.paths import data_path
from kolmo.reference.common import write_csv_atomic


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch BaoStock A-share trading-calendar observations.")
    parser.add_argument("--start-date", default="20170101", help="Inclusive YYYYMMDD or YYYY-MM-DD.")
    parser.add_argument("--end-date", default=date.today().strftime("%Y%m%d"), help="Inclusive YYYYMMDD or YYYY-MM-DD.")
    parser.add_argument("--output", default="", help="Default: $KOLMO_DATA_ROOT/reference/ashare/trading_calendar/YYYY.csv.")
    return parser.parse_args()


def rows_from_result(result) -> Iterable[Mapping[str, str]]:
    fields = list(getattr(result, "fields", []))
    while result.next():
        yield dict(zip(fields, result.get_row_data()))


def normalize_rows(rows: Iterable[Mapping[str, str]], observed_at: str) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for row in rows:
        trade_date = normalize_date(str(row.get("calendar_date", row.get("date", ""))))
        is_trading_day = str(row.get("is_trading_day", "")).strip()
        if is_trading_day not in {"0", "1"}:
            raise ValueError(f"invalid BaoStock is_trading_day for {trade_date}: {is_trading_day!r}")
        normalized.append(
            {
                "date": trade_date,
                "market": "ashare",
                "is_trading_day": is_trading_day,
                "source": "baostock.query_trade_dates",
                "observed_at": observed_at,
            }
        )
    return sorted(normalized, key=lambda item: item["date"])


def output_groups(rows: Iterable[dict[str, str]], explicit_output: str) -> list[tuple[Path, list[dict[str, str]]]]:
    materialized = list(rows)
    if explicit_output:
        return [(Path(explicit_output), materialized)]
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in materialized:
        grouped.setdefault(row["date"][:4], []).append(row)
    return [
        (data_path("reference", "ashare", "trading_calendar", f"{year}.csv"), grouped[year])
        for year in sorted(grouped)
    ]


def require_baostock():
    try:
        import baostock as bs  # type: ignore
    except ModuleNotFoundError as exc:
        raise RuntimeError("baostock is required; install project dependencies first") from exc
    return bs


def main() -> int:
    args = parse_args()
    start_date = normalize_date(args.start_date)
    end_date = normalize_date(args.end_date)
    if start_date > end_date:
        raise ValueError("start-date must be on or before end-date")
    bs = require_baostock()
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"baostock login failed: {login.error_code} {login.error_msg}")
    try:
        result = bs.query_trade_dates(
            start_date=f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}",
            end_date=f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}",
        )
        if result.error_code != "0":
            raise RuntimeError(f"query_trade_dates failed: {result.error_code} {result.error_msg}")
        rows = normalize_rows(rows_from_result(result), utc_now())
    finally:
        bs.logout()
    groups = output_groups(rows, args.output)
    for output, year_rows in groups:
        write_csv_atomic(output, TRADING_CALENDAR_COLUMNS, year_rows)
    print(f"rows={len(rows)} outputs={','.join(str(path) for path, _ in groups)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
