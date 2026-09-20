#!/usr/bin/env python3
"""Parallel quality audit for the produced daily-partitioned minute history."""

from __future__ import annotations

import argparse
import csv
import json
import os
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

from kolmo.paths import data_path
from kolmo.validation.minute_profile import validate_build


PRODUCT_ID = "ashare_minute_daily_mdcheck"
REPORT_SCHEMA_VERSION = "1.0.0"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def add_finding(
    findings: list[dict[str, Any]], year: int | None, severity: str,
    check: str, scope: str, detail: str,
) -> None:
    findings.append({
        "year": year,
        "severity": severity,
        "check": check,
        "scope": scope,
        "detail": detail,
    })


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def discover_years(root: Path, start_year: int | None, end_year: int | None) -> list[int]:
    discovered = sorted(
        int(path.name.removeprefix("year="))
        for path in root.glob("year=*")
        if path.is_dir() and path.name.removeprefix("year=").isdigit()
    )
    if start_year is None and end_year is None:
        if not discovered:
            raise FileNotFoundError(f"no year=YYYY directories found: {root}")
        return list(range(discovered[0], discovered[-1] + 1))
    lower = start_year if start_year is not None else (discovered[0] if discovered else end_year)
    upper = end_year if end_year is not None else (discovered[-1] if discovered else start_year)
    assert lower is not None and upper is not None
    if lower > upper:
        raise ValueError("--start-year must not exceed --end-year")
    return list(range(lower, upper + 1))


def validate_year(
    year: int, year_root: Path, check_symbol_sessions: bool, workers: int = 1,
) -> dict[str, Any]:
    started = perf_counter()
    try:
        report = validate_build(
            year_root,
            check_symbol_sessions=check_symbol_sessions,
            progress=False,
            workers=workers,
        )
        findings = [
            {"year": year, **finding}
            for finding in report.get("findings", [])
        ]
        try:
            manifest = _read_json(year_root / "manifest.json")
            if manifest.get("year") != year:
                add_finding(
                    findings, year, "error", "manifest_year_mismatch", "manifest.json",
                    f"expected={year} actual={manifest.get('year')}",
                )
            date_range = manifest.get("date_range", {})
            for boundary in ("start", "end"):
                value = str(date_range.get(boundary, ""))
                if not value.startswith(f"{year}-"):
                    add_finding(
                        findings, year, "error", "manifest_date_range_mismatch",
                        "manifest.json", f"{boundary}={value}",
                    )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            add_finding(
                findings, year, "error", "year_manifest_unreadable", "manifest.json", repr(exc),
            )
        summary = {
            "errors": sum(item["severity"] == "error" for item in findings),
            "warnings": sum(item["severity"] == "warning" for item in findings),
        }
        return {
            "year": year,
            "build_id": report.get("build_id", ""),
            "files_checked": report.get("files_checked", 0),
            "partitions_checked": report.get("partitions_checked", 0),
            "actual_outputs": report.get("actual_outputs", {}),
            "findings": findings,
            "summary": summary,
            "elapsed_seconds": round(perf_counter() - started, 3),
        }
    except Exception as exc:
        findings: list[dict[str, Any]] = []
        add_finding(
            findings, year, "error", "year_validation_failed", str(year_root), repr(exc),
        )
        return {
            "year": year,
            "build_id": "",
            "files_checked": 0,
            "partitions_checked": 0,
            "actual_outputs": {},
            "findings": findings,
            "summary": {"errors": 1, "warnings": 0},
            "elapsed_seconds": round(perf_counter() - started, 3),
        }


def validate_history_manifest(
    root: Path, years: list[int], findings: list[dict[str, Any]],
) -> None:
    path = root / "history-manifest.json"
    try:
        manifest = _read_json(path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        add_finding(findings, None, "error", "history_manifest_unreadable", str(path), repr(exc))
        return
    if manifest.get("status") != "validated":
        add_finding(
            findings, None, "error", "history_status_mismatch", "history-manifest.json",
            f"expected=validated actual={manifest.get('status')}",
        )
    recorded = manifest.get("years", {})
    if not isinstance(recorded, dict):
        add_finding(
            findings, None, "error", "history_years_invalid", "history-manifest.json",
            "years must be an object",
        )
        return
    for year in years:
        entry = recorded.get(str(year))
        if not isinstance(entry, dict) or entry.get("status") != "validated":
            add_finding(
                findings, year, "error", "history_year_status_mismatch",
                "history-manifest.json", f"actual={entry}",
            )


def run_daily_mdcheck(args: argparse.Namespace) -> dict[str, Any]:
    started = perf_counter()
    root = Path(args.root)
    years = discover_years(root, args.start_year, args.end_year)
    findings: list[dict[str, Any]] = []
    validate_history_manifest(root, years, findings)

    present: list[int] = []
    for year in years:
        year_root = root / f"year={year}"
        if year_root.is_dir():
            present.append(year)
        else:
            add_finding(
                findings, year, "error", "missing_year_directory", str(year_root),
                "expected year=YYYY directory",
            )

    worker_count = min(args.workers, max(1, len(present)))
    year_reports: dict[int, dict[str, Any]] = {}
    if worker_count == 1:
        for index, year in enumerate(present, start=1):
            # A single requested year still uses all requested workers across
            # its date/exchange partitions.
            year_workers = args.workers if len(present) == 1 else 1
            result = validate_year(
                year, root / f"year={year}", args.check_symbol_sessions, year_workers,
            )
            year_reports[year] = result
            print(
                f"[{index}/{len(present)}] year={year} files={result['files_checked']} "
                f"errors={result['summary']['errors']} warnings={result['summary']['warnings']} "
                f"seconds={result['elapsed_seconds']}",
                flush=True,
            )
    elif present:
        with ProcessPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(
                    validate_year, year, root / f"year={year}", args.check_symbol_sessions,
                    1,
                ): year
                for year in present
            }
            for index, future in enumerate(as_completed(futures), start=1):
                result = future.result()
                year = int(result["year"])
                year_reports[year] = result
                print(
                    f"[{index}/{len(present)}] year={year} files={result['files_checked']} "
                    f"errors={result['summary']['errors']} warnings={result['summary']['warnings']} "
                    f"seconds={result['elapsed_seconds']} workers={worker_count}",
                    flush=True,
                )

    ordered_reports = [year_reports[year] for year in sorted(year_reports)]
    for report in ordered_reports:
        findings.extend(report["findings"])
    findings.sort(key=lambda item: (
        item["year"] is None,
        item["year"] if item["year"] is not None else 0,
        item["severity"], item["check"], item["scope"],
    ))
    errors = sum(item["severity"] == "error" for item in findings)
    warnings = sum(item["severity"] == "warning" for item in findings)
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "product_id": PRODUCT_ID,
        "generated_at": utc_now(),
        "root": str(root),
        "requested_range": {"start_year": years[0], "end_year": years[-1]},
        "settings": {
            "workers": worker_count,
            "check_symbol_sessions": args.check_symbol_sessions,
        },
        "years": ordered_reports,
        "findings": findings,
        "summary": {
            "years_requested": len(years),
            "years_checked": len(ordered_reports),
            "files_checked": sum(int(item["files_checked"]) for item in ordered_reports),
            "daily_partitions_checked": sum(
                int(item["partitions_checked"]) for item in ordered_reports
            ),
            "errors": errors,
            "warnings": warnings,
            "elapsed_seconds": round(perf_counter() - started, 3),
        },
    }


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_reports(
    report: dict[str, Any], output: Path, details: Path | None = None,
) -> tuple[Path, Path]:
    details = details or output.with_suffix(".findings.csv")
    write_json_atomic(output, report)
    details.parent.mkdir(parents=True, exist_ok=True)
    with details.open("w", encoding="utf-8", newline="") as target:
        fields = ["year", "severity", "check", "scope", "detail"]
        writer = csv.DictWriter(target, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(report["findings"])
    return output, details


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Parallel quality audit for produced daily minute Parquet history.",
    )
    parser.add_argument(
        "--root", type=Path,
        default=data_path("staging", "minute", "history-v1"),
        help="History root containing history-manifest.json and year=YYYY directories.",
    )
    parser.add_argument("--start-year", type=int, default=None)
    parser.add_argument("--end-year", type=int, default=None)
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument(
        "--check-symbol-sessions", action=argparse.BooleanOptionalAction, default=True,
        help="Report symbols whose daily bar grid is incomplete (default: enabled).",
    )
    parser.add_argument("--fail-on-warning", action="store_true")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--details", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.workers < 1:
        raise ValueError("--workers must be at least 1")
    report = run_daily_mdcheck(args)
    output = args.output or Path(args.root) / "validation" / "daily-mdcheck.json"
    output, details = write_reports(report, output, args.details)
    summary = report["summary"]
    print(json.dumps({
        "output": str(output),
        "details": str(details),
        **summary,
    }, ensure_ascii=False))
    failed = bool(summary["errors"] or (args.fail_on_warning and summary["warnings"]))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
