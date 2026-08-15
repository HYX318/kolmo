#!/usr/bin/env python3
"""Build an observed A-share security-master snapshot from BaoStock basics."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from kolmo.ashare.board_rules import board_for_symbol
from kolmo.data_products import SECURITY_MASTER_COLUMNS, is_a_share_symbol, normalize_date
from kolmo.paths import data_path
from kolmo.reference.common import write_csv_atomic


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a BaoStock observed A-share security-master snapshot.")
    parser.add_argument("--as-of-date", default=date.today().strftime("%Y%m%d"), help="Observation date, YYYYMMDD or YYYY-MM-DD.")
    parser.add_argument("--output", default="", help="Default: $KOLMO_DATA_ROOT/reference/ashare/security_master/observed/YYYY/MM/YYYYMMDD.csv.")
    return parser.parse_args()


def price_limit_rule(board: str) -> tuple[str, str, str]:
    """Return the base board rule, never claiming ST/IPO exception coverage."""
    if board in {"chi_next", "star"}:
        return "0.20", f"{board}_20pct_base", "requires_st_ipo_and_corporate_action_overrides"
    if board in {"main", "sme"}:
        return "0.10", f"{board}_10pct_base", "requires_st_ipo_and_corporate_action_overrides"
    if board == "beijing":
        return "0.30", "beijing_30pct_base", "requires_st_ipo_and_corporate_action_overrides"
    return "", "unknown", "unavailable"


def rows_from_result(result) -> Iterable[Mapping[str, str]]:
    fields = list(getattr(result, "fields", []))
    while result.next():
        yield dict(zip(fields, result.get_row_data()))


def normalize_rows(
    rows: Iterable[Mapping[str, str]], as_of_date: str, observed_at: str
) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
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
        listing_date = normalize_date(str(row.get("ipoDate", ""))) if row.get("ipoDate") else ""
        delisting_date = normalize_date(str(row.get("outDate", ""))) if row.get("outDate") else ""
        board = board_for_symbol(symbol)
        base_limit, rule, limit_status = price_limit_rule(board)
        if listing_date and listing_date > as_of_date:
            status = "not_listed"
        elif delisting_date and delisting_date <= as_of_date:
            status = "delisted"
        else:
            status = "active"
        normalized.append(
            {
                "observed_at": observed_at,
                "symbol": symbol,
                "exchange": suffix,
                "name": str(row.get("code_name", "")).strip(),
                "board": board,
                "listing_date": listing_date,
                "delisting_date": delisting_date,
                "status_as_of": status,
                "st_status": "unknown",
                "base_price_limit_pct": base_limit,
                "price_limit_rule": rule,
                "price_limit_status": limit_status,
                "source": "baostock.query_stock_basic",
            }
        )
    return sorted(normalized, key=lambda item: item["symbol"])


def require_baostock():
    try:
        import baostock as bs  # type: ignore
    except ModuleNotFoundError as exc:
        raise RuntimeError("baostock is required; install project dependencies first") from exc
    return bs


def main() -> int:
    args = parse_args()
    as_of_date = normalize_date(args.as_of_date)
    output = Path(args.output) if args.output else data_path(
        "reference", "ashare", "security_master", "observed", as_of_date[:4], as_of_date[4:6], f"{as_of_date}.csv"
    )
    bs = require_baostock()
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"baostock login failed: {login.error_code} {login.error_msg}")
    try:
        result = bs.query_stock_basic()
        if result.error_code != "0":
            raise RuntimeError(f"query_stock_basic failed: {result.error_code} {result.error_msg}")
        rows = normalize_rows(rows_from_result(result), as_of_date, utc_now())
    finally:
        bs.logout()
    write_csv_atomic(output, SECURITY_MASTER_COLUMNS, rows)
    print(f"rows={len(rows)} output={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
