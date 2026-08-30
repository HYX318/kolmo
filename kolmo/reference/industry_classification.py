#!/usr/bin/env python3
"""Fetch a date-effective A-share industry classification from BaoStock."""

from __future__ import annotations

import argparse
import math
import signal
import socket
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from kolmo.data_products import (
    INDUSTRY_CLASSIFICATION_COLUMNS,
    is_a_share_symbol,
    normalize_date,
)
from kolmo.paths import data_path
from kolmo.reference.common import write_csv_atomic


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch BaoStock A-share industry classification as of one date."
    )
    parser.add_argument(
        "--as-of-date", default=date.today().strftime("%Y%m%d"),
        help="Classification date, YYYYMMDD or YYYY-MM-DD.",
    )
    parser.add_argument(
        "--output", default="",
        help="Default: $KOLMO_DATA_ROOT/reference/ashare/industry/as_of/YYYY/MM/YYYYMMDD.csv.",
    )
    parser.add_argument(
        "--timeout-seconds", type=float, default=30.0,
        help="Socket timeout for BaoStock connect and receive operations.",
    )
    return parser.parse_args()


def rows_from_result(result) -> Iterable[Mapping[str, str]]:
    fields = list(getattr(result, "fields", []))
    while result.next():
        yield dict(zip(fields, result.get_row_data()))


def normalize_rows(
    rows: Iterable[Mapping[str, str]], classification_date: str, retrieved_at: str
) -> list[dict[str, str]]:
    normalized = []
    seen = set()
    for row in rows:
        bs_code = str(row.get("code", "")).strip().lower()
        if "." not in bs_code:
            continue
        exchange, code = bs_code.split(".", 1)
        suffix = {"sz": "SZ", "sh": "SH", "bj": "BJ"}.get(exchange, "")
        if not suffix:
            continue
        symbol = f"{code.zfill(6)}.{suffix}"
        if not is_a_share_symbol(symbol):
            continue
        industry = str(row.get("industry", "")).strip()
        classification = str(row.get("industryClassification", "")).strip()
        if not industry or not classification:
            continue
        if symbol in seen:
            raise ValueError(f"duplicate industry symbol: {symbol}")
        seen.add(symbol)
        update = str(row.get("updateDate", "")).strip()
        normalized.append(
            {
                "classification_date": classification_date,
                "provider_update_date": normalize_date(update) if update else "",
                "retrieved_at": retrieved_at,
                "symbol": symbol,
                "exchange": suffix,
                "name": str(row.get("code_name", "")).strip(),
                "industry": industry,
                "classification": classification,
                "source": "baostock.query_stock_industry",
            }
        )
    return sorted(normalized, key=lambda item: item["symbol"])


def require_baostock():
    try:
        import baostock as bs  # type: ignore
    except ModuleNotFoundError as exc:
        raise RuntimeError("baostock is required; install project dependencies first") from exc
    return bs


def _deadline_expired(_signum, _frame) -> None:
    raise TimeoutError("BaoStock industry request exceeded its hard deadline")


def _close_baostock_socket() -> None:
    try:
        import baostock.common.context as context  # type: ignore
    except ModuleNotFoundError:
        return
    connection = getattr(context, "default_socket", None)
    if connection is not None:
        connection.close()
        setattr(context, "default_socket", None)


def main() -> int:
    args = parse_args()
    if args.timeout_seconds <= 0:
        raise ValueError("--timeout-seconds must be positive")
    as_of_date = normalize_date(args.as_of_date)
    output = Path(args.output) if args.output else data_path(
        "reference", "ashare", "industry", "as_of", as_of_date[:4],
        as_of_date[4:6], f"{as_of_date}.csv",
    )
    bs = require_baostock()
    previous_timeout = socket.getdefaulttimeout()
    previous_handler = signal.getsignal(signal.SIGALRM)
    socket.setdefaulttimeout(args.timeout_seconds)
    signal.signal(signal.SIGALRM, _deadline_expired)
    signal.alarm(max(1, math.ceil(args.timeout_seconds)))
    try:
        login = bs.login()
        if login.error_code != "0":
            raise RuntimeError(f"baostock login failed: {login.error_code} {login.error_msg}")
        result = bs.query_stock_industry(date=f"{as_of_date[:4]}-{as_of_date[4:6]}-{as_of_date[6:]}")
        if result.error_code != "0":
            raise RuntimeError(
                f"query_stock_industry failed: {result.error_code} {result.error_msg}"
            )
        rows = normalize_rows(rows_from_result(result), as_of_date, utc_now())
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)
        _close_baostock_socket()
        socket.setdefaulttimeout(previous_timeout)
    if not rows:
        raise RuntimeError(f"query_stock_industry returned no usable rows for {as_of_date}")
    write_csv_atomic(output, INDUSTRY_CLASSIFICATION_COLUMNS, rows)
    print(f"rows={len(rows)} output={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
