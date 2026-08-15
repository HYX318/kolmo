#!/usr/bin/env python3
"""Fetch normalized A-share quarterly financial data from BaoStock."""

from __future__ import annotations

import argparse
import csv
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

from kolmo.data_products import FINANCIAL_PRIMARY_KEY_COLUMNS, FINANCIAL_STATEMENT_COLUMNS, is_a_share_symbol
from kolmo.paths import data_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch BaoStock quarterly A-share financial statements.")
    parser.add_argument("--start-year", type=int, required=True)
    parser.add_argument("--end-year", type=int, required=True)
    parser.add_argument("--exchange", choices=["sz", "sh", "all"], default="all")
    parser.add_argument("--symbol", action="append", help="Optional symbol, e.g. 000001.SZ. Can be repeated.")
    parser.add_argument("--symbols-file", default="", help="Optional text/CSV file containing one symbol per row.")
    parser.add_argument("--output-root", default="", help="Default: $KOLMO_DATA_ROOT/fundamental/ashare/financial_statement/quarterly.")
    parser.add_argument("--failures-output", default="", help="Optional failure manifest CSV.")
    parser.add_argument("--sleep", type=float, default=0.05)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--quarter", type=int, choices=[1, 2, 3, 4], help="Fetch only one quarter for each selected year.")
    parser.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Skip symbols whose output file already contains the requested periods.",
    )
    parser.add_argument(
        "--skip-operation",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Skip BaoStock query_operation_data. Current normalized schema does not require it.",
    )
    return parser.parse_args()


def require_baostock():
    try:
        import baostock as bs  # type: ignore
    except ModuleNotFoundError as exc:
        print("missing Python dependency: baostock", file=sys.stderr)
        raise SystemExit(2) from exc
    return bs


def to_bs_code(symbol: str) -> str:
    text = symbol.strip().upper()
    if "." not in text:
        raise ValueError(f"symbol must be like 000001.SZ: {symbol}")
    code, exchange = text.split(".", 1)
    return f"{exchange.lower()}.{code}"


def to_symbol(bs_code: str) -> str:
    exchange, code = bs_code.lower().split(".", 1)
    return f"{code}.{exchange.upper()}"


def is_a_share_bs_code(bs_code: str) -> bool:
    return is_a_share_symbol(to_symbol(bs_code))


def rows_from_result(result) -> list[dict[str, str]]:
    fields = list(getattr(result, "fields", []) or [])
    rows: list[dict[str, str]] = []
    while result.next():
        values = result.get_row_data()
        rows.append({field: values[index] if index < len(values) else "" for index, field in enumerate(fields)})
    return rows


def query_rows(bs, name: str, **kwargs) -> list[dict[str, str]]:
    fn = getattr(bs, name, None)
    if fn is None:
        return []
    result = fn(**kwargs)
    if result.error_code != "0":
        raise RuntimeError(f"{name}: {result.error_code} {result.error_msg}")
    return rows_from_result(result)


def value(row: dict[str, str], *names: str) -> str:
    for name in names:
        text = row.get(name, "").strip()
        if text:
            return text
    return ""


def percent_to_decimal(row: dict[str, str], name: str) -> str:
    """BaoStock 财务比率接口返回百分数，统一显式换算为小数。"""
    text = value(row, name)
    if not text:
        return ""
    try:
        parsed = float(text)
    except ValueError as error:
        raise ValueError(f"BaoStock field {name} is not numeric: {text}") from error
    return f"{parsed / 100.0:.8f}"


def statement_type_for(quarter: int) -> str:
    return {1: "q1", 2: "half_year", 3: "q3", 4: "annual"}[quarter]


def _aligned_payload(
    bs_code: str,
    year: int,
    quarter: int,
    payload: dict[str, list[dict[str, str]]],
) -> dict[str, dict[str, str]] | None:
    required = ("profit", "balance", "cash_flow")
    if not any(payload.get(name) for name in required):
        return None
    missing = [name for name in required if not payload.get(name)]
    if missing:
        raise ValueError(f"incomplete BaoStock reports: missing {', '.join(missing)}")

    aligned: dict[str, dict[str, str]] = {}
    expected_stat_date = f"{year}-{quarter * 3:02d}-{31 if quarter in (1, 4) else 30:02d}"
    expected_key: tuple[str, str, str] | None = None
    for name in required + (("operation",) if payload.get("operation") else ()):
        rows = payload[name]
        if len(rows) != 1:
            raise ValueError(f"{name}: expected one row, got {len(rows)}")
        row = rows[0]
        key = (value(row, "code").lower(), value(row, "statDate"), value(row, "pubDate"))
        if not all(key):
            raise ValueError(f"{name}: missing code/statDate/pubDate")
        if key[0] != bs_code.lower():
            raise ValueError(f"{name}: code mismatch: {key[0]} != {bs_code.lower()}")
        if key[1] != expected_stat_date:
            raise ValueError(f"{name}: statDate mismatch for {year}Q{quarter}: {key[1]}")
        if expected_key is None:
            expected_key = key
        elif key != expected_key:
            raise ValueError(f"{name}: report version mismatch: {key} != {expected_key}")
        aligned[name] = row
    return aligned


def normalize_statement(
    bs_code: str,
    year: int,
    quarter: int,
    payload: dict[str, list[dict[str, str]]],
) -> dict[str, str] | None:
    aligned = _aligned_payload(bs_code, year, quarter, payload)
    if aligned is None:
        return None
    profit = aligned["profit"]
    balance = aligned["balance"]
    cash_flow = aligned["cash_flow"]
    announce_date = value(profit, "pubDate")
    report_period = value(profit, "statDate")
    debt_raw = value(balance, "liabilityToAsset")
    cash_flow_ratio_raw = value(cash_flow, "CFOToOR")
    roe_raw = value(profit, "roeAvg")
    gross_margin_raw = value(profit, "gpMargin")
    return {
        "symbol": to_symbol(bs_code),
        "report_period": report_period.replace("-", ""),
        "announce_date": announce_date.replace("-", ""),
        "statement_type": statement_type_for(quarter),
        "source_code": bs_code.lower(),
        "source_year": str(year),
        "source_quarter": str(quarter),
        "source_stat_date": report_period,
        "source_pub_date": announce_date,
        "revenue": value(profit, "MBRevenue"),
        "net_profit": value(profit, "netProfit"),
        # BaoStock 的这组季频接口不提供可确认的金额字段，禁止用比率反推。
        "operating_cash_flow": "",
        "operating_cash_flow_ratio": percent_to_decimal(cash_flow, "CFOToOR"),
        "operating_cash_flow_ratio_raw_percent": cash_flow_ratio_raw,
        "total_assets": "",
        "total_liabilities": "",
        "equity": "",
        "roe": percent_to_decimal(profit, "roeAvg"),
        "roe_raw_percent": roe_raw,
        "gross_margin": percent_to_decimal(profit, "gpMargin"),
        "gross_margin_raw_percent": gross_margin_raw,
        "debt_to_assets": percent_to_decimal(balance, "liabilityToAsset"),
        "debt_to_assets_raw_percent": debt_raw,
    }


def symbols_from_file(path: Path) -> list[str]:
    output: list[str] = []
    with path.open("r", encoding="utf-8", newline="") as file:
        for row in csv.reader(file):
            if not row:
                continue
            value = row[0].strip()
            if not value or value.startswith("#") or value.lower() in {"symbol", "code"}:
                continue
            output.append(value)
    return output


def load_universe(bs, exchange: str, symbols: list[str] | None, symbols_file: str, limit: int) -> list[str]:
    requested = list(symbols or [])
    if symbols_file:
        requested.extend(symbols_from_file(Path(symbols_file)))
    if requested:
        codes = [to_bs_code(symbol) for symbol in requested]
        if exchange != "all":
            codes = [code for code in codes if code.startswith(f"{exchange}.")]
        codes = [code for code in codes if is_a_share_bs_code(code)]
        return sorted(dict.fromkeys(codes))
    result = bs.query_stock_basic()
    if result.error_code != "0":
        raise RuntimeError(f"query_stock_basic: {result.error_code} {result.error_msg}")
    codes = []
    for row in rows_from_result(result):
        code = row.get("code", "")
        if not code.startswith(("sz.", "sh.")):
            continue
        if exchange != "all" and not code.startswith(f"{exchange}."):
            continue
        if row.get("type", "") != "1":
            continue
        if not is_a_share_bs_code(code):
            continue
        codes.append(code)
        if limit and len(codes) >= limit:
            break
    return codes


def requested_periods(start_year: int, end_year: int, quarter: int | None) -> list[tuple[int, int]]:
    quarters = [quarter] if quarter else [1, 2, 3, 4]
    return [(year, item) for year in range(start_year, end_year + 1) for item in quarters]


def output_path(output_root: Path, bs_code: str) -> Path:
    return output_root / f"{to_symbol(bs_code)}.csv"


def output_has_periods(path: Path, periods: list[tuple[int, int]]) -> bool:
    if not path.is_file() or path.stat().st_size <= 0:
        return False
    from kolmo.fundamental.validate_financials import validate_file

    if not validate_file(path)["ok"]:
        return False
    expected = {f"{year}Q{quarter}" for year, quarter in periods}
    seen: set[str] = set()
    with path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        if not reader.fieldnames or not set(FINANCIAL_STATEMENT_COLUMNS).issubset(reader.fieldnames):
            return False
        for row in reader:
            if row.get("symbol", "") != path.stem:
                return False
            report_period = row.get("report_period", "")
            if len(report_period) != 8 or not report_period.isdigit():
                continue
            month = int(report_period[4:6])
            quarter = {3: 1, 6: 2, 9: 3, 12: 4}.get(month)
            if quarter:
                seen.add(f"{report_period[:4]}Q{quarter}")
    return expected.issubset(seen)


def fetch_symbol(
    bs,
    bs_code: str,
    periods: list[tuple[int, int]],
    sleep_seconds: float,
    skip_operation: bool,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for year, quarter in periods:
        payload = {
            "profit": query_rows(bs, "query_profit_data", code=bs_code, year=year, quarter=quarter),
            "balance": query_rows(bs, "query_balance_data", code=bs_code, year=year, quarter=quarter),
            "cash_flow": query_rows(bs, "query_cash_flow_data", code=bs_code, year=year, quarter=quarter),
            "operation": [] if skip_operation else query_rows(bs, "query_operation_data", code=bs_code, year=year, quarter=quarter),
        }
        row = normalize_statement(bs_code, year, quarter, payload)
        if row is not None:
            rows.append(row)
        if sleep_seconds > 0.0:
            time.sleep(sleep_seconds)
    rows.sort(key=lambda item: (item["report_period"], item["announce_date"]))
    return rows


def _primary_key(row: dict[str, str]) -> tuple[str, ...]:
    return tuple(row.get(column, "") for column in FINANCIAL_PRIMARY_KEY_COLUMNS)


def _load_existing(path: Path) -> list[dict[str, str]]:
    if not path.is_file() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        rows = list(reader)
        # Interrupted legacy fetches left header-only files behind. They contain
        # no history to preserve, so a successful v1 fetch may replace them.
        if not rows:
            return []
        if not reader.fieldnames or not set(FINANCIAL_STATEMENT_COLUMNS).issubset(reader.fieldnames):
            raise ValueError(f"existing financial file has incompatible columns: {path}")
        return [{column: row.get(column, "") for column in FINANCIAL_STATEMENT_COLUMNS} for row in rows]


def write_symbol(output_root: Path, symbol: str, rows: list[dict[str, str]]) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    path = output_root / f"{symbol}.csv"
    merged: dict[tuple[str, ...], dict[str, str]] = {}
    for row in _load_existing(path):
        key = _primary_key(row)
        if not all(key):
            raise ValueError(f"financial row has incomplete primary key: {key}")
        if key in merged:
            raise ValueError(f"duplicate primary key in existing file: {key}")
        merged[key] = {column: row.get(column, "") for column in FINANCIAL_STATEMENT_COLUMNS}
    for row in rows:
        key = _primary_key(row)
        if not all(key):
            raise ValueError(f"financial row has incomplete primary key: {key}")
        merged[key] = {column: row.get(column, "") for column in FINANCIAL_STATEMENT_COLUMNS}
    ordered = sorted(merged.values(), key=lambda item: (_primary_key(item), item["symbol"]))
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="", dir=output_root, prefix=f".{symbol}.", suffix=".tmp", delete=False
        ) as file:
            temp_path = Path(file.name)
            writer = csv.DictWriter(file, fieldnames=FINANCIAL_STATEMENT_COLUMNS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(ordered)
        temp_path.replace(path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def write_failures(path: Path, failures: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=["bs_code", "symbol", "error"], lineterminator="\n")
        writer.writeheader()
        writer.writerows(failures)


def main() -> int:
    args = parse_args()
    if args.start_year > args.end_year:
        raise SystemExit("--start-year must be <= --end-year")
    bs = require_baostock()
    print("connecting to BaoStock...", file=sys.stderr, flush=True)
    login = bs.login()
    if login.error_code != "0":
        raise SystemExit(f"baostock login failed: {login.error_code} {login.error_msg}")
    print("BaoStock login ok", file=sys.stderr, flush=True)
    output_root = Path(args.output_root) if args.output_root else data_path("fundamental", "ashare", "financial_statement", "quarterly")
    counts: defaultdict[str, int] = defaultdict(int)
    failures: list[dict[str, str]] = []
    periods = requested_periods(args.start_year, args.end_year, args.quarter)
    try:
        print("loading A-share universe...", file=sys.stderr, flush=True)
        codes = load_universe(bs, args.exchange, args.symbol, args.symbols_file, args.limit)
        print(f"selected symbols={len(codes)} periods={len(periods)} output_root={output_root}", file=sys.stderr, flush=True)
        for index, bs_code in enumerate(codes, start=1):
            path = output_path(output_root, bs_code)
            if args.resume and output_has_periods(path, periods):
                counts["skipped"] += 1
                print(f"{index}/{len(codes)} {bs_code} skipped existing={path}", file=sys.stderr, flush=True)
                continue
            try:
                rows = fetch_symbol(bs, bs_code, periods, args.sleep, args.skip_operation)
                write_symbol(output_root, to_symbol(bs_code), rows)
                counts["symbols"] += 1
                counts["rows"] += len(rows)
            except Exception as error:  # noqa: BLE001
                counts["failures"] += 1
                failures.append({"bs_code": bs_code, "symbol": to_symbol(bs_code), "error": str(error)})
                print(f"failed {bs_code}: {error}", file=sys.stderr, flush=True)
            print(f"{index}/{len(codes)} {bs_code} rows={counts['rows']}", file=sys.stderr, flush=True)
    finally:
        bs.logout()
    failures_output = Path(args.failures_output) if args.failures_output else output_root / "failures.csv"
    if failures:
        write_failures(failures_output, failures)
    print(
        f"symbols={counts['symbols']} skipped={counts['skipped']} rows={counts['rows']} "
        f"failures={counts['failures']} output_root={output_root} failures_output={failures_output}"
    )
    return 0 if counts["failures"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
