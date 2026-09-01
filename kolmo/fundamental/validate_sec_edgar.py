#!/usr/bin/env python3
"""Offline validation for canonical SEC EDGAR filings and Company Facts."""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping, Sequence

from kolmo.data_products import SEC_COMPANY_FACT_COLUMNS, SEC_FILING_COLUMNS
from kolmo.fundamental.sec_edgar import (
    SEC_FACT_SOURCE,
    SEC_FILING_SOURCE,
    SecUniverseEntry,
    fact_row_id,
    fact_sort_key,
    facts_path,
    filing_sort_key,
    filings_path,
    load_sec_universe,
    read_gzip_csv,
    select_sec_universe,
)
from kolmo.paths import data_path
from kolmo.us_market.daily import default_universe_path, write_json_atomic


def _date_error(value: str, field: str, context: str, allow_empty: bool = False) -> str:
    if not value and allow_empty:
        return ""
    try:
        date.fromisoformat(value)
    except ValueError:
        return f"{context}: invalid {field}: {value!r}"
    return ""


def _timestamp_error(value: str, field: str, context: str, allow_empty: bool = False) -> str:
    if not value and allow_empty:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return f"{context}: invalid {field}: {value!r}"
    if parsed.tzinfo is None:
        return f"{context}: {field} must include a timezone"
    return ""


def validate_filing_rows(
    rows: Sequence[Mapping[str, str]], symbol: str, cik: str
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    accessions: set[str] = set()
    for index, row in enumerate(rows, start=2):
        accession = str(row.get("accession_number", ""))
        context = f"{symbol} filings row {index} accession {accession or '<missing>'}"
        if row.get("symbol") != symbol or row.get("cik") != cik:
            errors.append(f"{context}: symbol or CIK mismatch")
        if not accession:
            errors.append(f"{context}: accession number is empty")
        elif accession in accessions:
            errors.append(f"{context}: duplicate accession number")
        accessions.add(accession)
        for field, allow_empty in (("filing_date", False), ("report_date", True)):
            error = _date_error(str(row.get(field, "")), field, context, allow_empty)
            if error:
                errors.append(error)
        for field, allow_empty in (("accepted_at", True), ("retrieved_at", False)):
            error = _timestamp_error(str(row.get(field, "")), field, context, allow_empty)
            if error:
                errors.append(error)
        if row.get("source") != SEC_FILING_SOURCE:
            errors.append(f"{context}: unexpected source")
        if row.get("is_xbrl") not in {"0", "1"} or row.get("is_inline_xbrl") not in {"0", "1"}:
            errors.append(f"{context}: invalid XBRL flags")
    if list(rows) != sorted(rows, key=filing_sort_key):
        errors.append(f"{symbol}: filing rows are not in stable order")
    return errors, warnings


def validate_fact_rows(
    rows: Sequence[Mapping[str, str]],
    symbol: str,
    cik: str,
    filing_accessions: set[str],
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    row_ids: set[str] = set()
    for index, row in enumerate(rows, start=2):
        accession = str(row.get("accession_number", ""))
        tag = str(row.get("tag", ""))
        context = f"{symbol} facts row {index} tag {tag or '<missing>'} accession {accession or '<missing>'}"
        if row.get("symbol") != symbol or row.get("cik") != cik:
            errors.append(f"{context}: symbol or CIK mismatch")
        for field in ("taxonomy", "tag", "unit", "accession_number"):
            if not str(row.get(field, "")).strip():
                errors.append(f"{context}: {field} is empty")
        for field, allow_empty in (
            ("start_date", True), ("end_date", False), ("filed_date", False), ("available_date", False)
        ):
            error = _date_error(str(row.get(field, "")), field, context, allow_empty)
            if error:
                errors.append(error)
        for field, allow_empty in (("accepted_at", True), ("retrieved_at", False)):
            error = _timestamp_error(str(row.get(field, "")), field, context, allow_empty)
            if error:
                errors.append(error)
        try:
            numeric = Decimal(str(row.get("value", "")))
            if not numeric.is_finite():
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            errors.append(f"{context}: invalid numeric value")
        accepted_at = str(row.get("accepted_at", ""))
        filed_date = str(row.get("filed_date", ""))
        expected_available = accepted_at[:10] if accepted_at else filed_date
        if row.get("available_date") != expected_available:
            errors.append(f"{context}: available_date does not match accepted_at/filed_date")
        if row.get("source") != SEC_FACT_SOURCE:
            errors.append(f"{context}: unexpected source")
        expected_id = fact_row_id(row)
        observed_id = str(row.get("row_id", ""))
        if observed_id != expected_id:
            errors.append(f"{context}: row_id does not match the stable fact key")
        if observed_id in row_ids:
            errors.append(f"{context}: duplicate fact row_id")
        row_ids.add(observed_id)
        if accession and accession not in filing_accessions:
            warnings.append(f"{context}: accession is not present in canonical filings")
        available = str(row.get("available_date", ""))
        end = str(row.get("end_date", ""))
        if available and end and available < end:
            warnings.append(f"{context}: available_date precedes end_date")
    if list(rows) != sorted(rows, key=fact_sort_key):
        errors.append(f"{symbol}: fact rows are not in stable order")
    return errors, warnings


def validate_symbol(output_root: Path, entry: SecUniverseEntry) -> dict[str, object]:
    filing_file = filings_path(output_root, entry.symbol)
    fact_file = facts_path(output_root, entry.symbol)
    errors: list[str] = []
    warnings: list[str] = []
    filing_rows: list[dict[str, str]] = []
    fact_rows: list[dict[str, str]] = []
    if not filing_file.is_file():
        errors.append(f"missing filings file: {filing_file}")
    else:
        try:
            filing_rows = read_gzip_csv(filing_file, SEC_FILING_COLUMNS)
            row_errors, row_warnings = validate_filing_rows(filing_rows, entry.symbol, entry.cik)
            errors.extend(row_errors)
            warnings.extend(row_warnings)
        except (OSError, EOFError, ValueError) as exc:
            errors.append(f"cannot read filings file {filing_file}: {exc}")
    if not fact_file.is_file():
        errors.append(f"missing company facts file: {fact_file}")
    else:
        try:
            fact_rows = read_gzip_csv(fact_file, SEC_COMPANY_FACT_COLUMNS)
            row_errors, row_warnings = validate_fact_rows(
                fact_rows, entry.symbol, entry.cik,
                {row["accession_number"] for row in filing_rows},
            )
            errors.extend(row_errors)
            warnings.extend(row_warnings)
        except (OSError, EOFError, ValueError) as exc:
            errors.append(f"cannot read company facts file {fact_file}: {exc}")
    return {
        "symbol": entry.symbol,
        "cik": entry.cik,
        "ok": not errors,
        "filings_rows": len(filing_rows),
        "facts_rows": len(fact_rows),
        "filings_path": str(filing_file),
        "facts_path": str(fact_file),
        "errors": errors,
        "warnings": warnings,
    }


def validate_sec_files(entries: Sequence[SecUniverseEntry], output_root: Path) -> dict[str, object]:
    records = [validate_symbol(output_root, entry) for entry in entries]
    errors = sum(len(record["errors"]) for record in records)  # type: ignore[arg-type]
    warnings = sum(len(record["warnings"]) for record in records)  # type: ignore[arg-type]
    return {
        "output_root": str(output_root),
        "ok": errors == 0,
        "symbols_checked": len(records),
        "symbols_failed": sum(not bool(record["ok"]) for record in records),
        "errors": errors,
        "warnings": warnings,
        "records": records,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate canonical SEC EDGAR files without network access.")
    parser.add_argument("--universe", default="", help="Default: configs/us_value_universe.csv.")
    parser.add_argument("--symbol", action="append", default=[], help="Enabled stock symbol; repeatable.")
    parser.add_argument("--cik", action="append", default=[], help="CIK present in the universe; repeatable.")
    parser.add_argument("--output-root", default="", help="Default: $KOLMO_DATA_ROOT/fundamental/us/sec.")
    parser.add_argument("--json-output", default="", help="Optional validation report JSON.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    universe = Path(args.universe) if args.universe else default_universe_path()
    output_root = Path(args.output_root) if args.output_root else data_path("fundamental", "us", "sec")
    try:
        entries, _ = load_sec_universe(universe)
        selected = select_sec_universe(entries, args.symbol, args.cik)
        if not selected:
            raise ValueError("no enabled US stocks with CIKs selected")
        report = validate_sec_files(selected, output_root)
        if args.json_output:
            write_json_atomic(Path(args.json_output), report)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(
        f"symbols={report['symbols_checked']} failed={report['symbols_failed']} "
        f"errors={report['errors']} warnings={report['warnings']} output_root={output_root}",
        flush=True,
    )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
