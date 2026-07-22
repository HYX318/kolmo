#!/usr/bin/env python3
"""Fetch Shenzhen tick snapshots from the Tonghuashun iFinD HTTP API."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from kolmo.paths import data_path


TOKEN_URL = "https://quantapi.51ifind.com/api/v1/get_access_token"
SNAPSHOT_URL = "https://quantapi.51ifind.com/api/v1/snap_shot"
DEFAULT_INDICATORS = (
    "tradeDate,tradeTime,preClose,open,high,low,latest,amt,vol,amount,volume,tradeNum,"
    "bid1,ask1,bidSize1,askSize1"
)


def parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as error:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from error


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch per-day iFinD tick snapshot CSV files."
    )
    parser.add_argument("--codes", default="000001.SZ", help="Comma-separated iFinD codes.")
    parser.add_argument("--start-date", type=parse_date, required=True, help="Inclusive YYYY-MM-DD.")
    parser.add_argument("--end-date", type=parse_date, required=True, help="Inclusive YYYY-MM-DD.")
    parser.add_argument(
        "--indicators",
        default=DEFAULT_INDICATORS,
        help="Comma-separated iFinD snapshot fields.",
    )
    parser.add_argument("--start-time", default="09:15:00", help="Session start HH:MM:SS.")
    parser.add_argument("--end-time", default="15:15:00", help="Session end HH:MM:SS.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=data_path("raw", "ashare", "ifind", "tick_snapshot", "sz"),
        help="Directory for per-code, per-day raw CSV files.",
    )
    return parser.parse_args()


def post_json(url: str, headers: dict[str, str], payload: dict[str, object]) -> dict[str, object]:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urlopen(request, timeout=60) as response:
            return json.load(response)
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"iFinD HTTP {error.code}: {detail}") from error
    except URLError as error:
        raise RuntimeError(f"iFinD network error: {error.reason}") from error


def get_access_token(refresh_token: str) -> str:
    response = post_json(TOKEN_URL, {"refresh_token": refresh_token}, {})
    token = response.get("data", {}).get("access_token")
    if not isinstance(token, str) or not token:
        raise RuntimeError(f"iFinD access-token request failed: {response}")
    return token


def write_snapshot_csv(response: dict[str, object], output_path: Path, indicators: list[str]) -> int:
    tables = response.get("tables")
    if not isinstance(tables, list):
        raise RuntimeError(f"iFinD snapshot request failed: {response}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    rows_written = 0
    with output_path.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=["thscode", "time", *indicators])
        writer.writeheader()
        for item in tables:
            if not isinstance(item, dict):
                continue
            values = item.get("table", {})
            times = item.get("time", [])
            if not isinstance(values, dict) or not isinstance(times, list):
                continue
            code = str(item.get("thscode", ""))
            for index, timestamp in enumerate(times):
                row = {"thscode": code, "time": timestamp}
                for indicator in indicators:
                    series = values.get(indicator, [])
                    row[indicator] = series[index] if isinstance(series, list) and index < len(series) else ""
                writer.writerow(row)
                rows_written += 1
    return rows_written


def date_range(start_date: date, end_date: date) -> list[date]:
    if end_date < start_date:
        raise ValueError("end date must not be before start date")
    days: list[date] = []
    current = start_date
    while current <= end_date:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def main() -> int:
    args = parse_args()
    refresh_token = os.environ.get("THS_REFRESH_TOKEN", "")
    if not refresh_token:
        print("THS_REFRESH_TOKEN must be set; do not put it in source code.", file=sys.stderr)
        return 2

    codes = [code.strip() for code in args.codes.split(",") if code.strip()]
    indicators = [field.strip() for field in args.indicators.split(",") if field.strip()]
    if not codes or not indicators:
        print("--codes and --indicators must not be empty.", file=sys.stderr)
        return 2

    access_token = get_access_token(refresh_token)
    for trading_day in date_range(args.start_date, args.end_date):
        for code in codes:
            payload = {
                "codes": code,
                "indicators": ",".join(indicators),
                "starttime": f"{trading_day:%Y-%m-%d} {args.start_time}",
                "endtime": f"{trading_day:%Y-%m-%d} {args.end_time}",
            }
            response = post_json(SNAPSHOT_URL, {"access_token": access_token}, payload)
            output_path = args.output_dir / code / f"{trading_day:%Y%m%d}.csv"
            rows = write_snapshot_csv(response, output_path, indicators)
            print(f"{code} {trading_day:%Y-%m-%d}: {rows} rows -> {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
