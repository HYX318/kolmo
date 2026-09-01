#!/usr/bin/env python3
"""Build point-in-time standardized US financial metrics from local SEC data."""

from __future__ import annotations

import argparse
import csv
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from kolmo.data_products import SEC_COMPANY_FACT_COLUMNS, SEC_FILING_COLUMNS
from kolmo.fundamental.sec_edgar import (
    SecUniverseEntry,
    facts_path,
    filings_path,
    load_sec_universe,
    read_gzip_csv,
    select_sec_universe,
)
from kolmo.fundamental.us_concepts import classify_business_model
from kolmo.fundamental.us_financials import standardize_symbol, write_standardized_atomic
from kolmo.paths import data_path
from kolmo.us_market.daily import default_universe_path, write_json_atomic


@dataclass(frozen=True)
class BuildResult:
    symbol: str
    status: str
    business_model: str
    input_facts_rows: int
    quarterly_rows: int
    annual_rows: int
    ttm_rows: int
    excluded_future_period_facts: int
    unmapped_tags: int
    conflicting_concepts: int
    ambiguous_period_facts: int
    warnings: list[str]
    outputs: dict[str, str]
    error: str = ""


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build offline standardized US financial metrics.")
    parser.add_argument("--universe", default="", help="Default: configs/us_value_universe.csv.")
    parser.add_argument("--symbol", action="append", default=[], help="Enabled stock symbol; repeatable.")
    parser.add_argument("--input-root", default="", help="Default: $KOLMO_DATA_ROOT/fundamental/us/sec.")
    parser.add_argument("--output-root", default="", help="Default: $KOLMO_DATA_ROOT/fundamental/us/standardized.")
    parser.add_argument("--refresh", action="store_true", help="Rebuild deterministically from all canonical facts.")
    return parser.parse_args(argv)


def load_business_models(path: Path) -> dict[str, str]:
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        required = {"symbol", "sector", "industry"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"universe is missing business-model columns: {', '.join(sorted(missing))}")
        return {
            row["symbol"].strip().upper(): classify_business_model(row["sector"], row["industry"])
            for row in reader
        }


def build_one(
    entry: SecUniverseEntry, business_model: str, input_root: Path, output_root: Path
) -> BuildResult:
    try:
        facts = read_gzip_csv(facts_path(input_root, entry.symbol), SEC_COMPANY_FACT_COLUMNS)
        filings = read_gzip_csv(filings_path(input_root, entry.symbol), SEC_FILING_COLUMNS)
        if not facts or not filings:
            raise ValueError("SEC canonical facts or filings are missing/empty")
        products, provenance, stats = standardize_symbol(
            entry.symbol, entry.cik, facts, filings, business_model
        )
        outputs = write_standardized_atomic(output_root, entry.symbol, products, provenance)
        warnings = []
        if stats.excluded_future_period_facts:
            warnings.append("future_period_fact_excluded")
        if stats.ambiguous_period_facts:
            warnings.append("period_classification_ambiguous")
        if stats.unsupported_custom_taxonomy:
            warnings.append("unsupported_custom_taxonomy")
        return BuildResult(
            entry.symbol, "built", business_model, stats.input_facts_rows,
            len(products["quarterly"]), len(products["annual"]), len(products["ttm"]),
            stats.excluded_future_period_facts, stats.unmapped_tags,
            stats.conflicting_concepts, stats.ambiguous_period_facts, warnings,
            {name: str(path) for name, path in outputs.items()},
        )
    except Exception as exc:
        return BuildResult(
            entry.symbol, "failed", business_model, 0, 0, 0, 0, 0, 0, 0, 0,
            [], {}, str(exc),
        )


def run_build(
    entries: Sequence[SecUniverseEntry], business_models: dict[str, str],
    input_root: Path, output_root: Path,
) -> list[BuildResult]:
    results = []
    for entry in entries:
        result = build_one(
            entry, business_models.get(entry.symbol, "non_financial"), input_root, output_root
        )
        results.append(result)
        detail = (
            f" quarterly={result.quarterly_rows} annual={result.annual_rows} ttm={result.ttm_rows}"
            if result.status == "built" else f" error={result.error}"
        )
        print(f"{entry.symbol} status={result.status}{detail}", flush=True)
    return results


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    universe = Path(args.universe) if args.universe else default_universe_path()
    input_root = Path(args.input_root) if args.input_root else data_path("fundamental", "us", "sec")
    output_root = Path(args.output_root) if args.output_root else data_path("fundamental", "us", "standardized")
    try:
        entries, _ = load_sec_universe(universe)
        selected = select_sec_universe(entries, args.symbol, [])
        if not selected:
            raise ValueError("no enabled US stocks with CIKs selected")
        business_models = load_business_models(universe)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    started_at = datetime.now(timezone.utc).isoformat()
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    results = run_build(selected, business_models, input_root, output_root)
    failed = sum(result.status == "failed" for result in results)
    manifest = {
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "input_root": str(input_root),
        "output_root": str(output_root),
        "universe_path": str(universe),
        "symbols_requested": [entry.symbol for entry in selected],
        "refresh": bool(args.refresh),
        "status": "completed" if failed == 0 else "completed_with_failures",
        "results": [asdict(result) for result in results],
    }
    manifest_path = output_root / "_meta" / "build_runs" / f"{run_id}.json"
    write_json_atomic(manifest_path, manifest)
    print(f"manifest={manifest_path}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
