#!/usr/bin/env python3
"""Orchestrate resumable year-by-year minute-profile builds and validation."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from kolmo.ashare.build_vendor_minute_profile import build_profile, write_json_atomic
from kolmo.ashare.vendor_minute import DEFAULT_ROOT
from kolmo.paths import data_path
from kolmo.validation.minute_profile import validate_build, write_validation_report


def validated_year(year_root: Path) -> bool:
    manifest_path = year_root / "manifest.json"
    report_path = year_root / "validation" / "report.json"
    if not manifest_path.is_file() or not report_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return (
        manifest.get("status") == "built_not_validated"
        and report.get("build_id") == manifest.get("build_id")
        and report.get("summary") == {"errors": 0, "warnings": 0}
    )


def build_arguments(args: argparse.Namespace, year: int, year_root: Path, run_id: str) -> argparse.Namespace:
    start_date = args.start_date if year == args.start_year and args.start_date else f"{year}-01-01"
    end_date = args.end_date if year == args.end_year and args.end_date else f"{year}-12-31"
    return argparse.Namespace(
        year=year,
        start_date=start_date,
        end_date=end_date,
        source_root=args.source_root,
        output_dir=year_root,
        build_id=f"{run_id}-{year}",
        include_delisted=args.include_delisted,
        batch_symbols=args.batch_symbols,
        row_group_size=args.row_group_size,
        compression_level=args.compression_level,
        workers=args.workers,
        limit=0,
    )


def run_history(args: argparse.Namespace) -> dict[str, object]:
    run_id = args.run_id or f"history-v1-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    output_root = Path(args.output_root or data_path("staging", "minute", "history-v1"))
    output_root.mkdir(parents=True, exist_ok=True)
    history_path = output_root / "history-manifest.json"
    history: dict[str, object] = {
        "schema_version": "1.0.0",
        "product_id": "ashare_minute_history_build",
        "run_id": run_id,
        "start_year": args.start_year,
        "end_year": args.end_year,
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "years": {},
    }
    if history_path.is_file():
        previous = json.loads(history_path.read_text(encoding="utf-8"))
        if (
            previous.get("schema_version") != history["schema_version"]
            or previous.get("product_id") != history["product_id"]
        ):
            raise ValueError(f"existing history manifest is incompatible: {history_path}")
        previous_years = previous.get("years", {})
        if not isinstance(previous_years, dict):
            raise ValueError(f"existing history years are invalid: {history_path}")
        # The directory represents the cumulative history product, while each
        # invocation may request only the next year or another missing range.
        # Keep prior year results and widen the recorded coverage; the build
        # loop below still touches only the years requested by this invocation.
        history["start_year"] = min(int(previous["start_year"]), args.start_year)
        history["end_year"] = max(int(previous["end_year"]), args.end_year)
        history["years"] = previous_years
    write_json_atomic(history_path, history)

    years = history["years"]
    assert isinstance(years, dict)
    try:
        for year in range(args.start_year, args.end_year + 1):
            year_root = output_root / f"year={year}"
            if validated_year(year_root):
                years[str(year)] = {"status": "validated", "path": str(year_root)}
                write_json_atomic(history_path, history)
                print(f"year={year} already validated; skip", flush=True)
                continue
            manifest_path = year_root / "manifest.json"
            if manifest_path.is_file():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest.get("status") == "failed":
                    failed_root = output_root / "failed-attempts"
                    failed_root.mkdir(parents=True, exist_ok=True)
                    failed_name = f"year={year}-{manifest.get('build_id', 'unknown')}"
                    archived = failed_root / failed_name
                    if archived.exists():
                        raise FileExistsError(f"failed-attempt archive already exists: {archived}")
                    shutil.move(str(year_root), str(archived))
                    print(f"year={year} archived failed attempt at {archived}", flush=True)
                    manifest_path = year_root / "manifest.json"
                elif manifest.get("status") != "built_not_validated":
                    raise RuntimeError(f"year={year} has non-resumable status at {manifest_path}")
                else:
                    print(f"year={year} build exists; validating", flush=True)
            if not manifest_path.is_file():
                if year_root.exists() and any(year_root.iterdir()):
                    raise RuntimeError(f"year={year} output is non-empty without a manifest: {year_root}")
                years[str(year)] = {"status": "building", "path": str(year_root)}
                write_json_atomic(history_path, history)
                build_profile(build_arguments(args, year, year_root, run_id))

            years[str(year)] = {"status": "validating", "path": str(year_root)}
            write_json_atomic(history_path, history)
            report = validate_build(year_root)
            write_validation_report(report, year_root / "validation" / "report.json")
            if report["summary"]["errors"] or report["summary"]["warnings"]:
                years[str(year)] = {"status": "validation_failed", "path": str(year_root)}
                write_json_atomic(history_path, history)
                raise RuntimeError(f"year={year} validation failed: {report['summary']}")
            years[str(year)] = {"status": "validated", "path": str(year_root)}
            write_json_atomic(history_path, history)
            print(f"year={year} validated", flush=True)
        history["status"] = "validated"
        history["completed_at"] = datetime.now(timezone.utc).isoformat()
        write_json_atomic(history_path, history)
        return history
    except Exception as exc:
        history["status"] = "failed"
        history["completed_at"] = datetime.now(timezone.utc).isoformat()
        history["error"] = repr(exc)
        write_json_atomic(history_path, history)
        raise


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build and validate all annual A-share minute profiles.")
    parser.add_argument("--start-year", type=int, default=2010)
    parser.add_argument("--end-year", type=int, default=2026)
    parser.add_argument("--start-date", default="", help="Optional bound within --start-year.")
    parser.add_argument("--end-date", default="", help="Optional bound within --end-year.")
    parser.add_argument("--source-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--output-root", type=Path, default=None)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--include-delisted", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--batch-symbols", type=int, default=50)
    parser.add_argument("--row-group-size", type=int, default=65_536)
    parser.add_argument("--compression-level", type=int, default=3)
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.start_year > args.end_year:
        raise ValueError("--start-year must not exceed --end-year")
    if (
        args.batch_symbols < 1 or args.row_group_size < 1
        or not 1 <= args.compression_level <= 22 or args.workers < 1
    ):
        raise ValueError("batch, row-group, compression, and worker arguments are invalid")
    report = run_history(args)
    print(json.dumps({"run_id": report["run_id"], "status": report["status"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
