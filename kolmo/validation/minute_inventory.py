#!/usr/bin/env python3
"""Inventory vendor minute deliveries before expensive row-level checks."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from kolmo.ashare.vendor_minute import DEFAULT_ROOT, FREQUENCIES, find_archives
from kolmo.ashare.vendor_minute_delisted import find_delisted_archive


PARTIAL_SUFFIXES = (".qkdownloading", ".part", ".download", ".crdownload")


@dataclass(frozen=True)
class Finding:
    severity: str
    check: str
    scope: str
    detail: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def period_for(path: Path, year: int) -> str:
    stem = path.stem
    suffix = stem.rsplit("_", 1)[-1]
    return suffix if suffix.startswith(str(year)) else ""


def inspect_zip(path: Path, deep: bool) -> tuple[dict[str, object], list[Finding]]:
    findings: list[Finding] = []
    record: dict[str, object] = {
        "path": str(path), "bytes": path.stat().st_size, "parquet_members": 0,
        "member_count": 0, "crc": "not_run", "sha256": None,
    }
    try:
        with ZipFile(path) as archive:
            infos = archive.infolist()
            record["member_count"] = len(infos)
            parquet = [item for item in infos if item.filename.lower().endswith(".parquet")]
            record["parquet_members"] = len(parquet)
            if not parquet:
                findings.append(Finding("error", "no_parquet_members", str(path), "ZIP contains no Parquet files"))
            names = [Path(item.filename).name.upper() for item in parquet]
            duplicates = len(names) - len(set(names))
            if duplicates:
                findings.append(Finding("error", "duplicate_symbol_member", str(path), str(duplicates)))
            if deep:
                bad = archive.testzip()
                record["crc"] = "ok" if bad is None else "failed"
                if bad is not None:
                    findings.append(Finding("error", "zip_crc_failed", str(path), bad))
    except BadZipFile as exc:
        record["crc"] = "failed"
        findings.append(Finding("error", "bad_zip", str(path), str(exc)))
    if deep:
        record["sha256"] = sha256_file(path)
    return record, findings


def inspect_nested_zip(path: Path, deep: bool) -> tuple[dict[str, object], list[Finding]]:
    """Inspect the outer and, in deep mode, inner delisted-stock ZIPs."""
    findings: list[Finding] = []
    record: dict[str, object] = {
        "path": str(path), "bytes": path.stat().st_size, "nested_zip_members": 0,
        "member_count": 0, "crc": "not_run", "sha256": None,
    }
    try:
        with ZipFile(path) as outer:
            infos = outer.infolist()
            nested = [item for item in infos if item.filename.lower().endswith(".zip")]
            record["member_count"] = len(infos)
            record["nested_zip_members"] = len(nested)
            if not nested:
                findings.append(Finding("error", "no_nested_symbol_archives", str(path), "no symbol ZIPs"))
            names = [Path(item.filename).name.upper() for item in nested]
            record["symbols"] = sorted(name.removesuffix(".ZIP") for name in names)
            if len(names) != len(set(names)):
                findings.append(Finding("error", "duplicate_nested_symbol_archive", str(path), "duplicate names"))
            if deep:
                bad = outer.testzip()
                if bad is None:
                    for item in nested:
                        with ZipFile(io.BytesIO(outer.read(item))) as inner:
                            bad_inner = inner.testzip()
                            if bad_inner is not None:
                                bad = f"{item.filename}:{bad_inner}"
                                break
                record["crc"] = "ok" if bad is None else "failed"
                if bad is not None:
                    findings.append(Finding("error", "zip_crc_failed", str(path), bad))
    except BadZipFile as exc:
        record["crc"] = "failed"
        findings.append(Finding("error", "bad_zip", str(path), str(exc)))
    if deep:
        record["sha256"] = sha256_file(path)
    return record, findings


def build_inventory(
    root: Path, start_year: int, end_year: int, through_month: int, deep: bool,
    include_delisted: bool = True,
) -> dict[str, object]:
    findings: list[Finding] = []
    archives: list[dict[str, object]] = []
    discovered_paths: set[Path] = set()
    layouts: dict[str, dict[str, object]] = {}
    for year in range(start_year, end_year + 1):
        periods_by_frequency: dict[int, set[str]] = {}
        for frequency in FREQUENCIES:
            scope = f"{year}/{frequency}m"
            try:
                paths = find_archives(root, year, frequency)
            except FileNotFoundError as exc:
                findings.append(Finding("error", "archive_missing", scope, str(exc)))
                continue
            discovered_paths.update(paths)
            periods = {period_for(path, year) for path in paths}
            periods_by_frequency[frequency] = periods
            layout = "annual" if periods == {str(year)} else "monthly"
            layouts[scope] = {"layout": layout, "periods": sorted(periods)}
            if layout == "monthly":
                final_month = through_month if year == end_year else 12
                expected = {f"{year}-{month:02d}" for month in range(1, final_month + 1)}
                missing = sorted(expected - periods)
                if missing:
                    findings.append(Finding("error", "month_missing", scope, ",".join(missing)))
            for path in paths:
                record, issues = inspect_zip(path, deep)
                record.update({"year": year, "frequency": frequency, "period": period_for(path, year)})
                archives.append(record)
                findings.extend(issues)
        monthly_sets = [periods for periods in periods_by_frequency.values() if str(year) not in periods]
        if monthly_sets and any(periods != monthly_sets[0] for periods in monthly_sets[1:]):
            findings.append(Finding(
                "error", "frequency_period_mismatch", str(year),
                json.dumps({str(freq): sorted(periods) for freq, periods in periods_by_frequency.items()}, ensure_ascii=False),
            ))

    if include_delisted:
        periods: list[tuple[str, int]] = []
        if start_year <= 2025 and end_year >= 2010:
            periods.append(("2010-2025", max(start_year, 2010)))
        if start_year <= 2026 <= end_year:
            periods.append(("2026", 2026))
        for period, representative_year in periods:
            symbols_by_frequency: dict[int, set[str]] = {}
            for frequency in FREQUENCIES:
                scope = f"delisted/{period}/{frequency}m"
                try:
                    path = find_delisted_archive(root, representative_year, frequency)
                except FileNotFoundError as exc:
                    findings.append(Finding("error", "delisted_archive_missing", scope, str(exc)))
                    continue
                discovered_paths.add(path)
                record, issues = inspect_nested_zip(path, deep)
                record.update({"period": period, "frequency": frequency, "scope": "delisted"})
                archives.append(record)
                findings.extend(issues)
                symbols_by_frequency[frequency] = set(record.get("symbols", []))
            if symbols_by_frequency:
                union = set().union(*symbols_by_frequency.values())
                missing = {
                    str(frequency): sorted(union.difference(symbols))
                    for frequency, symbols in symbols_by_frequency.items()
                    if symbols != union
                }
                if missing:
                    findings.append(Finding(
                        "warning", "delisted_symbol_set_mismatch", f"delisted/{period}",
                        json.dumps(missing, ensure_ascii=False, sort_keys=True),
                    ))

    for path in root.rglob("*") if root.is_dir() else ():
        if path.is_file() and (path.name.endswith(PARTIAL_SUFFIXES) or path.stat().st_size == 0):
            findings.append(Finding("error", "incomplete_or_empty_file", str(path), "download is incomplete or empty"))

    auxiliary = sorted(
        str(path) for path in root.rglob("*.zip")
        if path not in discovered_paths
    )
    if auxiliary:
        findings.append(Finding(
            "warning", "auxiliary_archives_not_in_active_inventory", str(root),
            f"{len(auxiliary)} archives (for example delisted-stock bundles) require a separate merge gate",
        ))
    return {
        "schema_version": "1.0.0",
        "product_id": "vendor_minute_delivery_inventory",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "root": str(root),
        "range": {"start_year": start_year, "end_year": end_year, "through_month": through_month},
        "deep": deep,
        "layouts": layouts,
        "archives": archives,
        "auxiliary_archives": auxiliary,
        "findings": [asdict(finding) for finding in findings],
        "summary": {
            "archives": len(archives),
            "errors": sum(finding.severity == "error" for finding in findings),
            "warnings": sum(finding.severity == "warning" for finding in findings),
        },
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inventory A-share minute ZIP deliveries.")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--start-year", type=int, default=2010)
    parser.add_argument("--end-year", type=int, default=2026)
    parser.add_argument("--through-month", type=int, default=9, help="Expected last month in --end-year.")
    parser.add_argument("--deep", action="store_true", help="Read every byte for ZIP CRC and SHA-256.")
    parser.add_argument("--include-delisted", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.start_year > args.end_year or not 1 <= args.through_month <= 12:
        raise ValueError("invalid year range or --through-month")
    report = build_inventory(
        args.root, args.start_year, args.end_year, args.through_month, args.deep, args.include_delisted,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), **report["summary"]}, ensure_ascii=False))
    return 1 if report["summary"]["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
