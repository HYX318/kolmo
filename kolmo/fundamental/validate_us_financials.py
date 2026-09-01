#!/usr/bin/env python3
"""Offline validator for standardized US financial metrics and provenance."""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping, Sequence

from kolmo.data_products import (
    SEC_COMPANY_FACT_COLUMNS,
    US_STANDARDIZED_BASE_METRICS,
    US_STANDARDIZED_DERIVED_METRICS,
    US_STANDARDIZED_FINANCIAL_COLUMNS,
    US_STANDARDIZED_PROVENANCE_COLUMNS,
)
from kolmo.fundamental.sec_edgar import SecUniverseEntry, facts_path, load_sec_universe, read_gzip_csv, select_sec_universe
from kolmo.fundamental.us_concepts import QUALITY_FLAGS, TTM_ADDITIVE_METRICS
from kolmo.fundamental.us_financials import (
    PERIOD_TYPES,
    STANDARDIZED_SOURCE,
    provenance_path,
    standardized_path,
)
from kolmo.paths import data_path
from kolmo.us_market.daily import default_universe_path, write_json_atomic


def _date(value: str, context: str, allow_empty: bool = False) -> str:
    if not value and allow_empty:
        return ""
    try:
        date.fromisoformat(value)
        return ""
    except ValueError:
        return f"{context}: invalid date {value!r}"


def _time(value: str, context: str, allow_empty: bool = False) -> str:
    if not value and allow_empty:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return f"{context}: invalid timestamp {value!r}"
    return "" if parsed.tzinfo else f"{context}: timestamp lacks timezone"


def _sort_key(row: Mapping[str, str]) -> tuple[str, str, str, str, str]:
    return (
        row["symbol"], row["report_period"], row["period_type"],
        row["available_date"], row["accession_number"],
    )


def _quarters_are_consecutive(rows: Sequence[Mapping[str, str]]) -> bool:
    if len(rows) != 4:
        return False
    ordered = sorted(rows, key=lambda row: row["report_period"])
    ends = [date.fromisoformat(row["report_period"]) for row in ordered]
    if any(not 45 <= (right - left).days <= 150 for left, right in zip(ends, ends[1:])):
        return False
    labels = [row["fiscal_period"] for row in ordered]
    order = {"Q1": 0, "Q2": 1, "Q3": 2, "Q4": 3}
    return all(label in order for label in labels) and all(
        order[right] == (order[left] + 1) % 4 for left, right in zip(labels, labels[1:])
    )


def validate_symbol(
    input_root: Path,
    output_root: Path,
    entry: SecUniverseEntry,
    as_of_date: str | None = None,
    max_ttm_age_days: int = 180,
) -> dict[str, object]:
    errors: list[str] = []
    warnings: list[str] = []
    facts: list[dict[str, str]] = []
    provenance: list[dict[str, str]] = []
    products: dict[str, list[dict[str, str]]] = {}
    try:
        facts = read_gzip_csv(facts_path(input_root, entry.symbol), SEC_COMPANY_FACT_COLUMNS)
    except (OSError, EOFError, ValueError) as exc:
        errors.append(f"cannot read source facts: {exc}")
    try:
        provenance = read_gzip_csv(
            provenance_path(output_root, entry.symbol), US_STANDARDIZED_PROVENANCE_COLUMNS
        )
    except (OSError, EOFError, ValueError) as exc:
        errors.append(f"cannot read provenance: {exc}")
    for period_type in PERIOD_TYPES:
        try:
            products[period_type] = read_gzip_csv(
                standardized_path(output_root, period_type, entry.symbol),
                US_STANDARDIZED_FINANCIAL_COLUMNS,
            )
        except (OSError, EOFError, ValueError) as exc:
            products[period_type] = []
            errors.append(f"cannot read {period_type}: {exc}")

    fact_by_id = {row["row_id"]: row for row in facts}
    provenance_by_key: dict[tuple[str, str, str, str, str], dict[str, str]] = {}
    for index, item in enumerate(provenance, start=2):
        key = (
            item["report_period"], item["period_type"], item["available_date"],
            item["accession_number"], item["metric"],
        )
        if key in provenance_by_key:
            errors.append(f"{entry.symbol} provenance row {index}: duplicate metric provenance")
        provenance_by_key[key] = item
        if item["symbol"] != entry.symbol:
            errors.append(f"{entry.symbol} provenance row {index}: symbol mismatch")
        for row_id in filter(None, item["source_row_ids"].split(";")):
            source = fact_by_id.get(row_id)
            if source is None:
                errors.append(f"{entry.symbol} provenance row {index}: unknown source row_id {row_id}")
                continue
            if source["available_date"] > item["available_date"]:
                errors.append(f"{entry.symbol} provenance row {index}: future source availability")
            if source["end_date"] > item["available_date"]:
                errors.append(f"{entry.symbol} provenance row {index}: future-period source fact")

    metrics = (*US_STANDARDIZED_BASE_METRICS, *US_STANDARDIZED_DERIVED_METRICS)
    keys: set[tuple[str, str, str, str, str]] = set()
    for period_type in PERIOD_TYPES:
        rows = products[period_type]
        if rows != sorted(rows, key=_sort_key):
            errors.append(f"{entry.symbol} {period_type}: unstable row order")
        for index, row in enumerate(rows, start=2):
            context = f"{entry.symbol} {period_type} row {index}"
            if row["symbol"] != entry.symbol or row["cik"] != entry.cik:
                errors.append(f"{context}: symbol/CIK mismatch")
            if row["period_type"] != period_type:
                errors.append(f"{context}: period_type mismatch")
            if row["report_period"] != row["period_end"]:
                errors.append(f"{context}: report_period must equal period_end")
            for field, allow_empty in (("period_start", True), ("period_end", False), ("available_date", False)):
                error = _date(row[field], f"{context} {field}", allow_empty)
                if error:
                    errors.append(error)
            for field, allow_empty in (("accepted_at", True), ("built_at", False)):
                error = _time(row[field], f"{context} {field}", allow_empty)
                if error:
                    errors.append(error)
            if not row["accession_number"]:
                errors.append(f"{context}: accession_number is empty")
            if row["source"] != STANDARDIZED_SOURCE:
                errors.append(f"{context}: invalid source")
            key = _sort_key(row)
            if key in keys:
                errors.append(f"{context}: duplicate primary key")
            keys.add(key)
            flags = set(filter(None, row["quality_flags"].split(";")))
            invalid_flags = flags - QUALITY_FLAGS
            if invalid_flags:
                errors.append(f"{context}: invalid quality flags {sorted(invalid_flags)}")
            try:
                score = Decimal(row["completeness_score"])
                if not score.is_finite() or score < 0 or score > 1:
                    raise InvalidOperation
            except (InvalidOperation, ValueError):
                errors.append(f"{context}: invalid completeness_score")
            for metric in metrics:
                if not row[metric]:
                    continue
                try:
                    value = Decimal(row[metric])
                    if not value.is_finite():
                        raise InvalidOperation
                except (InvalidOperation, ValueError):
                    errors.append(f"{context}: non-finite {metric}")
                    continue
                provenance_key = (
                    row["report_period"], period_type, row["available_date"],
                    row["accession_number"], metric,
                )
                source = provenance_by_key.get(provenance_key)
                if source is None:
                    errors.append(f"{context}: {metric} has no provenance")
                elif period_type == "ttm" and metric in TTM_ADDITIVE_METRICS:
                    periods = list(filter(None, source["component_periods"].split(";")))
                    accessions = list(filter(None, source["component_accessions"].split(";")))
                    if source["calculation"] != "sum_four_quarters" or len(periods) != 4 or len(accessions) != 4:
                        errors.append(f"{context}: {metric} TTM does not have four quarter components")
                    else:
                        components = []
                        for component_period, component_accession in zip(periods, accessions):
                            candidates = [
                                item for item in products["quarterly"]
                                if item["report_period"] == component_period
                                and item["accession_number"] == component_accession
                                and item["available_date"] <= row["available_date"]
                                and item[metric]
                            ]
                            if not candidates:
                                errors.append(
                                    f"{context}: {metric} component {component_period}/{component_accession} is missing"
                                )
                                break
                            components.append(max(candidates, key=lambda item: (
                                item["available_date"], item["accepted_at"], item["accession_number"]
                            )))
                        if len(components) == 4:
                            if not _quarters_are_consecutive(components):
                                errors.append(f"{context}: {metric} components are not consecutive quarters")
                            recalculated = sum((Decimal(item[metric]) for item in components), Decimal(0))
                            if recalculated != Decimal(row[metric]):
                                errors.append(
                                    f"{context}: {metric} does not equal its four quarter components"
                                )
                            for component in components:
                                latest_candidates = [
                                    item for item in products["quarterly"]
                                    if item["report_period"] == component["report_period"]
                                    and item["available_date"] <= row["available_date"] and item[metric]
                                ]
                                latest = max(latest_candidates, key=lambda item: (
                                    item["available_date"], item["accepted_at"], item["accession_number"]
                                ))
                                if latest["accession_number"] != component["accession_number"]:
                                    errors.append(
                                        f"{context}: {metric} does not use latest non-empty version for "
                                        f"{component['report_period']}"
                                    )
            if period_type == "quarterly" and row["period_start"]:
                duration = (date.fromisoformat(row["period_end"]) - date.fromisoformat(row["period_start"])).days + 1
                if duration < 45 or duration > 150:
                    warnings.append(f"{context}: unusual quarterly duration {duration} days")

    identities: dict[str, set[tuple[str, str]]] = {}
    for row in products["quarterly"]:
        identities.setdefault(row["report_period"], set()).add((row["fiscal_year"], row["fiscal_period"]))
    for period, values in identities.items():
        if len(values) > 1:
            errors.append(f"{entry.symbol} quarterly {period}: unstable fiscal identity {sorted(values)}")
    latest_quarters = []
    for period in sorted(identities)[-4:]:
        candidates = [row for row in products["quarterly"] if row["report_period"] == period]
        latest_quarters.append(max(candidates, key=lambda row: (
            row["available_date"], row["accepted_at"], row["accession_number"]
        )))
    if len(latest_quarters) == 4 and not _quarters_are_consecutive(latest_quarters):
        warnings.append(f"{entry.symbol}: latest four normalized fiscal quarters are not consecutive")

    ttm_rows = products["ttm"]
    if not ttm_rows:
        warnings.append(f"{entry.symbol}: no TTM output")
    else:
        latest_ttm = max(ttm_rows, key=lambda row: (
            row["report_period"], row["available_date"], row["accepted_at"], row["accession_number"]
        ))
        as_of = date.fromisoformat(as_of_date) if as_of_date else date.today()
        age_days = (as_of - date.fromisoformat(latest_ttm["report_period"])).days
        if age_days > max_ttm_age_days:
            warnings.append(
                f"{entry.symbol}: latest TTM report period is stale ({age_days} days; "
                f"limit {max_ttm_age_days})"
            )
        not_applicable = {
            item.removeprefix("not_applicable:")
            for item in latest_ttm["applicability_flags"].split(";") if item
        }
        for metric in ("revenue", "net_income", "operating_cash_flow", "capital_expenditure"):
            if metric not in not_applicable and not latest_ttm[metric]:
                warnings.append(f"{entry.symbol}: latest TTM is missing core metric {metric}")
    return {
        "symbol": entry.symbol,
        "ok": not errors,
        "rows": {period_type: len(products[period_type]) for period_type in PERIOD_TYPES},
        "provenance_rows": len(provenance),
        "errors": errors,
        "warnings": warnings,
    }


def validate_files(
    entries: Sequence[SecUniverseEntry], input_root: Path, output_root: Path,
    as_of_date: str | None = None, max_ttm_age_days: int = 180,
) -> dict[str, object]:
    records = [
        validate_symbol(input_root, output_root, entry, as_of_date, max_ttm_age_days)
        for entry in entries
    ]
    errors = sum(len(record["errors"]) for record in records)  # type: ignore[arg-type]
    warnings = sum(len(record["warnings"]) for record in records)  # type: ignore[arg-type]
    return {
        "input_root": str(input_root), "output_root": str(output_root),
        "ok": errors == 0, "symbols_checked": len(records),
        "symbols_failed": sum(not record["ok"] for record in records),
        "errors": errors, "warnings": warnings, "records": records,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate standardized US financial metrics offline.")
    parser.add_argument("--universe", default="")
    parser.add_argument("--symbol", action="append", default=[])
    parser.add_argument("--input-root", default="", help="Canonical SEC root.")
    parser.add_argument("--output-root", default="", help="Standardized financial root.")
    parser.add_argument("--json-output", default="")
    parser.add_argument("--as-of-date", default="", help="Freshness date; default: today.")
    parser.add_argument("--max-ttm-age-days", type=int, default=180)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    universe = Path(args.universe) if args.universe else default_universe_path()
    input_root = Path(args.input_root) if args.input_root else data_path("fundamental", "us", "sec")
    output_root = Path(args.output_root) if args.output_root else data_path("fundamental", "us", "standardized")
    try:
        entries, _ = load_sec_universe(universe)
        selected = select_sec_universe(entries, args.symbol, [])
        if args.as_of_date:
            date.fromisoformat(args.as_of_date)
        if args.max_ttm_age_days < 0:
            raise ValueError("--max-ttm-age-days must be non-negative")
        report = validate_files(
            selected, input_root, output_root, args.as_of_date or None, args.max_ttm_age_days
        )
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
