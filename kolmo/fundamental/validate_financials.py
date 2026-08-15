#!/usr/bin/env python3
"""Validate normalized point-in-time financial statement files."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path

from kolmo.data_products import (
    FINANCIAL_PRIMARY_KEY_COLUMNS,
    FINANCIAL_STATEMENT_COLUMNS,
    is_a_share_symbol,
    normalize_date,
    validate_columns,
)
from kolmo.paths import data_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate normalized A-share financial statement files.")
    parser.add_argument(
        "--root",
        default="",
        help="Default: $KOLMO_DATA_ROOT/fundamental/ashare/financial_statement/quarterly.",
    )
    parser.add_argument("--output", default="", help="Optional JSON validation report path.")
    return parser.parse_args()


RATIO_FIELDS = {
    "operating_cash_flow_ratio": "operating_cash_flow_ratio_raw_percent",
    "roe": "roe_raw_percent",
    "gross_margin": "gross_margin_raw_percent",
    "debt_to_assets": "debt_to_assets_raw_percent",
}

AMOUNT_FIELDS = {
    "revenue",
    "net_profit",
    "operating_cash_flow",
    "total_assets",
    "total_liabilities",
    "equity",
}


def _number(text: str, field: str) -> float:
    try:
        result = float(text)
    except ValueError as error:
        raise ValueError(f"{field} is not numeric") from error
    if not math.isfinite(result):
        raise ValueError(f"{field} is not finite")
    return result


def validate_file(path: Path) -> dict[str, object]:
    errors: list[str] = []
    rows = 0
    seen_keys: set[tuple[str, ...]] = set()
    with path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        column_result = validate_columns(reader.fieldnames, FINANCIAL_STATEMENT_COLUMNS)
        if not column_result.ok:
            errors.append(f"missing columns: {', '.join(column_result.missing)}")
            return {"path": str(path), "rows": rows, "ok": False, "errors": errors}
        for row in reader:
            rows += 1
            if not is_a_share_symbol(row.get("symbol", "")):
                errors.append(f"row {rows}: invalid A-share symbol")
            key = tuple(row.get(column, "") for column in FINANCIAL_PRIMARY_KEY_COLUMNS)
            if not all(key):
                errors.append(f"row {rows}: incomplete financial primary key")
            elif key in seen_keys:
                errors.append(f"row {rows}: duplicate financial primary key")
            seen_keys.add(key)
            try:
                announce_date = normalize_date(row["announce_date"])
                report_period = normalize_date(row["report_period"])
                source_stat_date = normalize_date(row["source_stat_date"])
                source_pub_date = normalize_date(row["source_pub_date"])
            except ValueError as error:
                errors.append(f"row {rows}: {error}")
                continue
            if announce_date < report_period:
                errors.append(f"row {rows}: announce_date before report_period")
            if source_stat_date != report_period or source_pub_date != announce_date:
                errors.append(f"row {rows}: normalized dates do not match BaoStock source dates")
            try:
                year = int(row["source_year"])
                quarter = int(row["source_quarter"])
            except ValueError:
                errors.append(f"row {rows}: invalid source year/quarter")
                continue
            expected_month_day = {1: "0331", 2: "0630", 3: "0930", 4: "1231"}.get(quarter)
            if expected_month_day is None or report_period != f"{year}{expected_month_day}":
                errors.append(f"row {rows}: report_period does not match source year/quarter")
            expected_type = {1: "q1", 2: "half_year", 3: "q3", 4: "annual"}.get(quarter)
            if row["statement_type"] != expected_type:
                errors.append(f"row {rows}: statement_type does not match source quarter")
            try:
                if row["source_code"].lower() != row["symbol"].split(".")[1].lower() + "." + row["symbol"].split(".")[0]:
                    errors.append(f"row {rows}: source_code does not match symbol")
            except IndexError:
                pass
            for field, raw_field in RATIO_FIELDS.items():
                normalized_text = row[field].strip()
                raw_text = row[raw_field].strip()
                if bool(normalized_text) != bool(raw_text):
                    errors.append(f"row {rows}: {field} and {raw_field} must both be set or empty")
                    continue
                if not normalized_text:
                    continue
                try:
                    normalized = _number(normalized_text, field)
                    raw_percent = _number(raw_text, raw_field)
                except ValueError as error:
                    errors.append(f"row {rows}: {error}")
                    continue
                if not math.isclose(normalized, raw_percent / 100.0, rel_tol=1e-9, abs_tol=1e-10):
                    errors.append(f"row {rows}: {field} must equal {raw_field} / 100")
            for field in AMOUNT_FIELDS:
                text = row[field].strip()
                if not text:
                    continue
                try:
                    _number(text, field)
                except ValueError as error:
                    errors.append(f"row {rows}: {error}")
    if rows == 0:
        errors.append("no financial rows")
    return {"path": str(path), "rows": rows, "ok": not errors, "errors": errors[:20]}


def main() -> int:
    args = parse_args()
    root = Path(args.root) if args.root else data_path("fundamental", "ashare", "financial_statement", "quarterly")
    files = sorted(root.glob("*.csv")) if root.is_dir() else []
    results = [validate_file(path) for path in files]
    report = {
        "root": str(root),
        "files": len(files),
        "rows": sum(int(item["rows"]) for item in results),
        "failed_files": sum(1 for item in results if not item["ok"]),
        "results": results,
    }
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ["root", "files", "rows", "failed_files"]}, ensure_ascii=False))
    return 1 if report["files"] == 0 or report["failed_files"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
