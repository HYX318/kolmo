#!/usr/bin/env python3
"""Fetch SEC EDGAR submissions and Company Facts into canonical long tables."""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from kolmo.data_products import SEC_COMPANY_FACT_COLUMNS, SEC_FILING_COLUMNS
from kolmo.fundamental.sec_edgar import (
    SEC_COMPANY_FACTS_URL,
    SEC_SUBMISSIONS_BASE_URL,
    SEC_SUBMISSIONS_URL,
    SecEdgarError,
    SecHttpClient,
    SecHttpError,
    SecUniverseEntry,
    UniverseSkip,
    facts_path,
    filings_path,
    historical_submission_files,
    load_sec_universe,
    merge_facts,
    merge_filings,
    normalize_cik,
    normalize_company_facts,
    normalize_filings,
    parse_json_bytes,
    read_gzip_csv,
    select_sec_universe,
    write_raw_snapshot_if_changed,
    write_symbol_atomic,
)
from kolmo.fundamental.validate_sec_edgar import validate_fact_rows, validate_filing_rows
from kolmo.paths import data_path
from kolmo.us_market.daily import default_universe_path, write_json_atomic


@dataclass(frozen=True)
class SecFetchResult:
    symbol: str
    cik: str
    status: str
    companyfacts_http_status: int
    submissions_http_status: int
    raw_snapshot_paths: list[str]
    facts_output: str
    filings_output: str
    facts_rows: int
    filings_rows: int
    error: str = ""


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch SEC EDGAR filings and Company Facts.")
    parser.add_argument("--universe", default="", help="Default: configs/us_value_universe.csv.")
    parser.add_argument("--symbol", action="append", default=[], help="Enabled stock symbol; repeatable.")
    parser.add_argument("--cik", action="append", default=[], help="CIK present in the universe; repeatable.")
    parser.add_argument("--output-root", default="", help="Default: $KOLMO_DATA_ROOT/fundamental/us/sec.")
    parser.add_argument("--user-agent", default="", help="SEC identity; prefer SEC_USER_AGENT.")
    parser.add_argument("--workers", type=int, default=2, help="Concurrent companies, between 1 and 8.")
    parser.add_argument("--requests-per-second", type=float, default=5.0, help="Global SEC request rate; maximum 10.")
    parser.add_argument("--timeout", type=float, default=30.0, help="Per-request timeout in seconds.")
    parser.add_argument("--retries", type=int, default=3, help="Bounded retries for 429, 5xx, and network errors.")
    parser.add_argument("--refresh", action="store_true", help="Rebuild canonical files instead of merging existing rows.")
    return parser.parse_args(argv)


def _payload_mapping(body: bytes, context: str) -> dict[str, object]:
    payload = parse_json_bytes(body)
    if not isinstance(payload, dict):
        raise SecEdgarError(f"{context}: SEC response must be a JSON object")
    return payload


def _assert_payload_cik(payload: dict[str, object], expected: str, context: str) -> None:
    observed = payload.get("cik")
    if observed in (None, ""):
        return
    try:
        actual = normalize_cik(observed)
    except ValueError as exc:
        raise SecEdgarError(f"{context}: invalid response CIK: {observed!r}") from exc
    if actual != expected:
        raise SecEdgarError(f"{context}: response CIK mismatch: expected {expected}, got {actual}")


def fetch_one(
    entry: SecUniverseEntry,
    output_root: Path,
    raw_root: Path,
    client: SecHttpClient,
    retrieved_at: str,
    refresh: bool,
) -> SecFetchResult:
    filing_output = filings_path(output_root, entry.symbol)
    fact_output = facts_path(output_root, entry.symbol)
    raw_paths: list[str] = []
    submissions_status = 0
    companyfacts_status = 0
    phase = "submissions"
    try:
        submissions_response = client.get(SEC_SUBMISSIONS_URL.format(cik=entry.cik))
        submissions_status = submissions_response.status
        submissions = _payload_mapping(submissions_response.body, f"{entry.symbol} submissions")
        _assert_payload_cik(submissions, entry.cik, f"{entry.symbol} submissions")
        snapshot, _ = write_raw_snapshot_if_changed(
            raw_root, "submissions", entry.cik, retrieved_at, submissions_response.body, "main"
        )
        raw_paths.append(str(snapshot))

        historical: list[dict[str, object]] = []
        for name in historical_submission_files(submissions):
            response = client.get(f"{SEC_SUBMISSIONS_BASE_URL}{name}")
            submissions_status = response.status
            payload = _payload_mapping(response.body, f"{entry.symbol} historical submissions {name}")
            historical.append(payload)
            snapshot, _ = write_raw_snapshot_if_changed(
                raw_root, "submissions", entry.cik, retrieved_at, response.body, name.removesuffix(".json")
            )
            raw_paths.append(str(snapshot))

        phase = "companyfacts"
        facts_response = client.get(SEC_COMPANY_FACTS_URL.format(cik=entry.cik))
        companyfacts_status = facts_response.status
        facts_payload = _payload_mapping(facts_response.body, f"{entry.symbol} companyfacts")
        _assert_payload_cik(facts_payload, entry.cik, f"{entry.symbol} companyfacts")
        snapshot, _ = write_raw_snapshot_if_changed(
            raw_root, "companyfacts", entry.cik, retrieved_at, facts_response.body, "main"
        )
        raw_paths.append(str(snapshot))

        incoming_filings = normalize_filings(entry.symbol, entry.cik, submissions, historical, retrieved_at)
        accepted = {row["accession_number"]: row["accepted_at"] for row in incoming_filings if row["accepted_at"]}
        incoming_facts = normalize_company_facts(entry.symbol, entry.cik, facts_payload, accepted, retrieved_at)
        existing_filings = [] if refresh else read_gzip_csv(filing_output, SEC_FILING_COLUMNS)
        existing_facts = [] if refresh else read_gzip_csv(fact_output, SEC_COMPANY_FACT_COLUMNS)
        merged_filings = merge_filings(existing_filings, incoming_filings)
        merged_facts = merge_facts(existing_facts, incoming_facts)
        filing_errors, _ = validate_filing_rows(merged_filings, entry.symbol, entry.cik)
        fact_errors, _ = validate_fact_rows(merged_facts, entry.symbol, entry.cik, {row["accession_number"] for row in merged_filings})
        errors = filing_errors + fact_errors
        if errors:
            raise SecEdgarError(f"validation failed for {entry.symbol}: {'; '.join(errors[:8])}")
        write_symbol_atomic(output_root, entry.symbol, merged_filings, merged_facts)
        return SecFetchResult(
            entry.symbol, entry.cik, "updated", companyfacts_status, submissions_status,
            raw_paths, str(fact_output), str(filing_output), len(merged_facts), len(merged_filings),
        )
    except Exception as exc:
        if isinstance(exc, SecHttpError):
            if phase == "companyfacts":
                companyfacts_status = exc.status
            else:
                submissions_status = exc.status
        return SecFetchResult(
            entry.symbol, entry.cik, "failed", companyfacts_status, submissions_status,
            raw_paths, str(fact_output), str(filing_output), 0, 0, str(exc),
        )


def skipped_result(item: UniverseSkip) -> SecFetchResult:
    return SecFetchResult(item.symbol, item.cik, "skipped", 0, 0, [], "", "", 0, 0, item.reason)


def run_fetch(
    entries: Sequence[SecUniverseEntry],
    output_root: Path,
    raw_root: Path,
    client: SecHttpClient,
    retrieved_at: str,
    refresh: bool,
    workers: int,
) -> list[SecFetchResult]:
    results: list[SecFetchResult] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(fetch_one, entry, output_root, raw_root, client, retrieved_at, refresh): entry
            for entry in entries
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            detail = f" filings={result.filings_rows} facts={result.facts_rows}" if result.status == "updated" else f" error={result.error}"
            print(f"{result.symbol} CIK{result.cik} status={result.status}{detail}", flush=True)
    return sorted(results, key=lambda item: item.symbol)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    universe = Path(args.universe) if args.universe else default_universe_path()
    output_root = Path(args.output_root) if args.output_root else data_path("fundamental", "us", "sec")
    raw_root = data_path("raw", "US", "sec")
    try:
        if args.workers < 1 or args.workers > 8:
            raise ValueError("workers must be between 1 and 8")
        if args.requests_per_second <= 0 or args.requests_per_second > 10:
            raise ValueError("requests-per-second must be greater than 0 and at most 10")
        if args.timeout <= 0:
            raise ValueError("timeout must be greater than 0")
        entries, skipped = load_sec_universe(universe)
        selected = select_sec_universe(entries, args.symbol, args.cik)
        if not selected:
            raise ValueError("no enabled US stocks with CIKs selected")
        if args.symbol or args.cik:
            skipped = []
        user_agent = args.user_agent or os.environ.get("SEC_USER_AGENT", "")
        client = SecHttpClient(
            user_agent, args.requests_per_second, args.timeout, args.retries
        )
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    started_at = datetime.now(timezone.utc).isoformat()
    retrieved_at = started_at
    results = run_fetch(selected, output_root, raw_root, client, retrieved_at, bool(args.refresh), args.workers)
    results.extend(skipped_result(item) for item in skipped)
    results.sort(key=lambda item: item.symbol)
    failed = sum(result.status == "failed" for result in results)
    manifest = {
        "run_id": run_id,
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "provider": "sec.edgar",
        "status": "completed" if failed == 0 else "completed_with_failures",
        "user_agent_present": True,
        "universe_path": str(universe),
        "requested_symbols": [entry.symbol for entry in selected],
        "request": {
            "workers": args.workers,
            "requests_per_second": args.requests_per_second,
            "timeout": args.timeout,
            "retries": args.retries,
            "refresh": bool(args.refresh),
        },
        "results": [asdict(result) for result in results],
    }
    manifest_path = output_root / "_meta" / "fetch_runs" / f"{run_id}.json"
    write_json_atomic(manifest_path, manifest)
    print(f"manifest={manifest_path}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
