#!/usr/bin/env python3
"""Validate a staged canonical minute-profile build before publication."""

from __future__ import annotations

import argparse
import csv
import json
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from kolmo.ashare.minute_corrections import (
    CORRECTION_POLICY_ID,
    EXCLUSION_POLICY_ID,
    OBSERVATION_POLICY_ID,
)
from kolmo.ashare.minute_product import (
    FREQUENCIES,
    MINUTE_BAR_COLUMNS,
    MINUTE_BAR_SCHEMA,
    PRODUCT_ID,
    SCHEMA_VERSION,
    aggregate_bars,
    expected_bars_per_day,
    validate_canonical_bars,
)


@dataclass(frozen=True)
class Finding:
    severity: str
    check: str
    scope: str
    detail: str


def add(findings: list[Finding], severity: str, check: str, scope: str, detail: str) -> None:
    findings.append(Finding(severity, check, scope, detail))


def parse_partition_path(product_root: Path, path: Path) -> tuple[int, str, str]:
    relative = path.relative_to(product_root)
    parts = relative.parts
    if len(parts) != 6 or parts[1] != "raw" or not parts[0].endswith("m"):
        raise ValueError(f"invalid minute partition path: {relative}")
    frequency = int(parts[0][:-1])
    exchange = parts[2]
    compact = Path(parts[5]).stem
    if frequency not in FREQUENCIES or exchange not in {"sz", "sh", "bj"}:
        raise ValueError(f"invalid frequency or exchange in path: {relative}")
    if parts[3] != compact[:4] or parts[4] != compact[4:6] or len(compact) != 8:
        raise ValueError(f"date directories do not match filename: {relative}")
    trade_date = f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"
    return frequency, exchange, trade_date


def read_and_validate_partition_frame(
    product_root: Path, path: Path, build_id: str, findings: list[Finding],
    *, check_symbol_sessions: bool = False,
) -> tuple[tuple[int, str, str], pd.DataFrame] | None:
    scope = str(path.relative_to(product_root))
    try:
        frequency, exchange, trade_date = parse_partition_path(product_root, path)
        parquet = pq.ParquetFile(path)
        schema = parquet.schema_arrow
        if not schema.remove_metadata().equals(MINUTE_BAR_SCHEMA):
            add(findings, "error", "schema_mismatch", scope, str(schema))
            return None
        metadata = schema.metadata or {}
        expected_metadata = {
            b"kolmo.schema_version": SCHEMA_VERSION.encode(),
            b"kolmo.product_id": PRODUCT_ID.encode(),
            b"kolmo.frequency_minutes": str(frequency).encode(),
            b"kolmo.trade_date": trade_date.encode(),
            b"kolmo.exchange": exchange.upper().encode(),
            b"kolmo.adjustment": b"raw",
            b"kolmo.timezone": b"Asia/Shanghai",
            b"kolmo.bar_label": b"end",
            b"kolmo.volume_unit": b"share",
            b"kolmo.amount_unit": b"CNY",
            b"kolmo.build_id": build_id.encode(),
        }
        mismatched = {
            key.decode(): {"expected": value.decode(), "actual": metadata.get(key, b"").decode()}
            for key, value in expected_metadata.items() if metadata.get(key) != value
        }
        if mismatched:
            add(findings, "error", "metadata_mismatch", scope, json.dumps(mismatched, sort_keys=True))
        frame = parquet.read().to_pandas()
        if frame.empty:
            add(findings, "error", "empty_partition", scope, "partition contains no rows")
            return (frequency, exchange, trade_date), frame
        validate_canonical_bars(frame, frequency)
        order = pd.MultiIndex.from_frame(frame[["symbol", "trade_time"]])
        if not order.is_monotonic_increasing:
            add(findings, "error", "row_order_mismatch", scope, "expected symbol,trade_time ascending")
        expected_date = pd.Timestamp(trade_date, tz="Asia/Shanghai")
        if not frame["trade_time"].dt.normalize().eq(expected_date).all():
            add(findings, "error", "row_date_mismatch", scope, trade_date)
        suffix = f".{exchange.upper()}"
        if not frame["symbol"].str.endswith(suffix).all():
            add(findings, "error", "row_exchange_mismatch", scope, suffix)
        # Higher frequencies are checked by exact re-aggregation below, so the
        # 1-minute grid is the only independent completeness signal.
        if check_symbol_sessions and frequency == 1:
            counts = frame.groupby("symbol", sort=False, observed=True).size()
            expected_counts = {expected_bars_per_day(frequency, exchange)}
            # BJ post-close 15:01-15:30 is a provider adjustment grid. A normal
            # 241-bar session and a session with all 30 adjustment bars are
            # both complete; a partially present adjustment grid is suspicious.
            if exchange == "bj":
                expected_counts.add(expected_bars_per_day(frequency))
            incomplete = counts.loc[~counts.isin(expected_counts)]
            if not incomplete.empty:
                expected = "|".join(str(value) for value in sorted(expected_counts))
                sample = ",".join(
                    f"{symbol}:{int(count)}" for symbol, count in incomplete.iloc[:10].items()
                )
                add(
                    findings, "warning", "incomplete_symbol_session", scope,
                    f"expected={expected} symbols={len(incomplete)} sample={sample}",
                )
        return (frequency, exchange, trade_date), frame
    except Exception as exc:
        add(findings, "error", "partition_validation_failed", scope, repr(exc))
        return None


def read_and_validate_partition(
    product_root: Path, path: Path, build_id: str, findings: list[Finding],
    *, check_symbol_sessions: bool = False,
) -> tuple[tuple[int, str, str], int] | None:
    """Validate one file while keeping the legacy row-count result contract."""
    result = read_and_validate_partition_frame(
        product_root, path, build_id, findings,
        check_symbol_sessions=check_symbol_sessions,
    )
    if result is None:
        return None
    key, frame = result
    return key, len(frame)


def compare_derived(
    expected: pd.DataFrame, actual: pd.DataFrame, frequency: int, scope: str,
    findings: list[Finding],
) -> None:
    keys = ["symbol", "trade_time"]
    same_keys = (
        len(expected) == len(actual)
        and np.array_equal(expected["symbol"].astype(str).to_numpy(), actual["symbol"].astype(str).to_numpy())
        and bool((expected["trade_time"].array == actual["trade_time"].array).all())
    )
    if not same_keys:
        joined = expected[keys].merge(actual[keys], how="outer", indicator=True)
        counts = joined["_merge"].value_counts().to_dict()
        add(findings, "error", "derived_primary_key_mismatch", scope, json.dumps(counts, sort_keys=True))
        return
    for field in ("open", "high", "low", "close", "volume", "amount"):
        left = expected[field].to_numpy()
        right = actual[field].to_numpy()
        if field == "volume":
            mismatched = left != right
        else:
            mismatched = ~np.isclose(left, right, rtol=1e-12, atol=1e-9)
        count = int(mismatched.sum())
        if count:
            maximum = float(np.max(np.abs(left[mismatched] - right[mismatched])))
            add(
                findings, "error", f"derived_{field}_mismatch", scope,
                f"frequency={frequency} count={count} max_abs_diff={maximum}",
            )


def validate_partition_group(
    product_root: Path, paths: tuple[Path, ...], build_id: str,
    check_symbol_sessions: bool,
) -> tuple[list[tuple[tuple[int, str, str], int, int]], list[Finding]]:
    """Read one date/exchange group once and validate all derived frequencies."""
    findings: list[Finding] = []
    frames: dict[int, pd.DataFrame] = {}
    entries: list[tuple[tuple[int, str, str], int, int]] = []
    exchange = ""
    trade_date = ""
    for path in paths:
        result = read_and_validate_partition_frame(
            product_root, path, build_id, findings,
            check_symbol_sessions=check_symbol_sessions,
        )
        if result is None:
            continue
        key, frame = result
        frequency, exchange, trade_date = key
        frames[frequency] = frame
        entries.append((key, len(frame), path.stat().st_size))
    base = frames.get(1)
    if base is not None:
        for frequency in FREQUENCIES[1:]:
            target = frames.get(frequency)
            if target is not None:
                compare_derived(
                    aggregate_bars(base, frequency), target, frequency,
                    f"{trade_date}/{exchange}", findings,
                )
    return entries, findings


def validate_build(
    build_root: Path, *, check_symbol_sessions: bool = False, progress: bool = True,
    workers: int = 1,
) -> dict[str, Any]:
    manifest_path = build_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    findings: list[Finding] = []
    build_id = str(manifest.get("build_id", ""))
    if manifest.get("product_id") != PRODUCT_ID or manifest.get("schema_version") != SCHEMA_VERSION:
        add(findings, "error", "manifest_contract_mismatch", "manifest.json", "product or schema version")
    if manifest.get("status") != "built_not_validated":
        add(findings, "error", "manifest_status_mismatch", "manifest.json", str(manifest.get("status")))

    correction_summary = manifest.get("corrections")
    if correction_summary is not None:
        correction_path = build_root / str(correction_summary.get("report", ""))
        try:
            correction_report = json.loads(correction_path.read_text(encoding="utf-8"))
            expected_corrections = {
                "policy_id": CORRECTION_POLICY_ID,
                "applied_rows": correction_summary.get("applied_rows"),
            }
            actual_corrections = {
                "policy_id": correction_report.get("policy_id"),
                "applied_rows": correction_report.get("applied_rows"),
            }
            records = correction_report.get("records")
            if (
                actual_corrections != expected_corrections
                or not isinstance(records, list)
                or len(records) != correction_report.get("applied_rows")
            ):
                add(
                    findings, "error", "correction_report_mismatch", str(correction_path),
                    json.dumps({"expected": expected_corrections, "actual": actual_corrections}),
                )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            add(findings, "error", "correction_report_unreadable", str(correction_path), repr(exc))

    unresolved_summary = manifest.get("unresolved")
    if unresolved_summary is not None:
        unresolved_path = build_root / str(unresolved_summary.get("report", ""))
        try:
            unresolved_report = json.loads(unresolved_path.read_text(encoding="utf-8"))
            records = unresolved_report.get("records")
            if (
                unresolved_summary.get("issues") != 0
                or unresolved_report.get("issues") != 0
                or not isinstance(records, list)
                or records
            ):
                add(
                    findings, "error", "unresolved_quality_issues", str(unresolved_path),
                    json.dumps(unresolved_summary, sort_keys=True),
                )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            add(findings, "error", "unresolved_report_unreadable", str(unresolved_path), repr(exc))

    exclusion_summary = manifest.get("exclusions")
    if exclusion_summary is not None:
        exclusion_path = build_root / str(exclusion_summary.get("report", ""))
        try:
            exclusion_report = json.loads(exclusion_path.read_text(encoding="utf-8"))
            records = exclusion_report.get("records")
            recorded_rows = (
                sum(int(record.get("excluded_rows", 0)) for record in records)
                if isinstance(records, list) else -1
            )
            expected_rows = exclusion_summary.get("excluded_rows")
            if (
                exclusion_summary.get("policy_id") != EXCLUSION_POLICY_ID
                or exclusion_report.get("policy_id") != EXCLUSION_POLICY_ID
                or exclusion_report.get("excluded_rows") != expected_rows
                or recorded_rows != expected_rows
            ):
                add(findings, "error", "exclusion_report_mismatch", str(exclusion_path), "summary mismatch")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            add(findings, "error", "exclusion_report_unreadable", str(exclusion_path), repr(exc))

    observation_summary = manifest.get("observations")
    if observation_summary is not None:
        observation_path = build_root / str(observation_summary.get("report", ""))
        try:
            observation_report = json.loads(observation_path.read_text(encoding="utf-8"))
            records = observation_report.get("records")
            expected_rows = observation_summary.get("observed_rows")
            if (
                observation_summary.get("policy_id") != OBSERVATION_POLICY_ID
                or observation_report.get("policy_id") != OBSERVATION_POLICY_ID
                or observation_report.get("observed_rows") != expected_rows
                or not isinstance(records, list)
                or len(records) != expected_rows
            ):
                add(findings, "error", "observation_report_mismatch", str(observation_path), "summary mismatch")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            add(findings, "error", "observation_report_unreadable", str(observation_path), repr(exc))

    product_root = build_root / "profile" / "minute" / "v1"
    paths = sorted(product_root.rglob("*.parquet"))
    partition_paths: dict[tuple[int, str, str], Path] = {}
    groups: dict[tuple[str, str], list[Path]] = {}
    for path in paths:
        try:
            key = parse_partition_path(product_root, path)
        except Exception as exc:
            add(
                findings, "error", "partition_validation_failed",
                str(path.relative_to(product_root)), repr(exc),
            )
            continue
        if key in partition_paths:
            add(findings, "error", "duplicate_partition", str(path), str(key))
            continue
        partition_paths[key] = path
        _, exchange, trade_date = key
        groups.setdefault((exchange, trade_date), []).append(path)

    actual_stats = {
        str(frequency): {
            "partitions": 0, "rows": 0, "bytes": 0,
            "first_trade_date": None, "last_trade_date": None,
        }
        for frequency in FREQUENCIES
    }

    def record_group(
        result: tuple[list[tuple[tuple[int, str, str], int, int]], list[Finding]],
    ) -> None:
        entries, group_findings = result
        findings.extend(group_findings)
        for key, rows, size in entries:
            frequency, _, trade_date = key
            stats = actual_stats[str(frequency)]
            stats["partitions"] += 1
            stats["rows"] += rows
            stats["bytes"] += size
            if stats["first_trade_date"] is None or trade_date < stats["first_trade_date"]:
                stats["first_trade_date"] = trade_date
            if stats["last_trade_date"] is None or trade_date > stats["last_trade_date"]:
                stats["last_trade_date"] = trade_date

    group_items = sorted(groups.items())
    worker_count = min(workers, max(1, len(group_items)))
    if worker_count == 1:
        for index, (_, group_paths) in enumerate(group_items, start=1):
            record_group(validate_partition_group(
                product_root, tuple(group_paths), build_id, check_symbol_sessions,
            ))
            if progress and (index % 10 == 0 or index == len(group_items)):
                print(
                    f"[{index}/{len(group_items)}] checked_daily_partitions "
                    f"findings={len(findings)}",
                    flush=True,
                )
    else:
        with ProcessPoolExecutor(max_workers=worker_count) as executor:
            futures = [
                executor.submit(
                    validate_partition_group, product_root, tuple(group_paths), build_id,
                    check_symbol_sessions,
                )
                for _, group_paths in group_items
            ]
            for index, future in enumerate(as_completed(futures), start=1):
                record_group(future.result())
                if progress and (index % 10 == 0 or index == len(futures)):
                    print(
                        f"[{index}/{len(futures)}] checked_daily_partitions "
                        f"findings={len(findings)} workers={worker_count}",
                        flush=True,
                    )

    expected_stats = manifest.get("outputs", {})
    if actual_stats != expected_stats:
        add(
            findings, "error", "manifest_output_stats_mismatch", "manifest.json",
            json.dumps({"expected": expected_stats, "actual": actual_stats}, ensure_ascii=False, sort_keys=True),
        )

    base_partitions = {
        (exchange, date) for frequency, exchange, date in partition_paths if frequency == 1
    }
    for frequency in FREQUENCIES[1:]:
        target = {
            (exchange, date) for freq, exchange, date in partition_paths if freq == frequency
        }
        if target != base_partitions:
            add(
                findings, "error", "frequency_partition_set_mismatch", f"{frequency}m",
                f"missing={len(base_partitions - target)} extra={len(target - base_partitions)}",
            )
    return {
        "schema_version": SCHEMA_VERSION,
        "product_id": "ashare_minute_profile_validation",
        "build_id": build_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "build_root": str(build_root),
        "files_checked": len(paths),
        "partitions_checked": len(base_partitions),
        "actual_outputs": actual_stats,
        "findings": [asdict(finding) for finding in findings],
        "summary": {
            "errors": sum(finding.severity == "error" for finding in findings),
            "warnings": sum(finding.severity == "warning" for finding in findings),
        },
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate a staged canonical minute-profile build.")
    parser.add_argument("--build-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--details", type=Path, default=None)
    return parser.parse_args(argv)


def write_validation_report(
    report: dict[str, Any], output: Path, details: Path | None = None,
) -> tuple[Path, Path]:
    details = details or output.with_name("findings.csv")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    details.parent.mkdir(parents=True, exist_ok=True)
    with details.open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=list(Finding.__dataclass_fields__), lineterminator="\n")
        writer.writeheader()
        writer.writerows(report["findings"])
    return output, details


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = validate_build(args.build_root)
    output = args.output or args.build_root / "validation" / "report.json"
    output, _ = write_validation_report(report, output, args.details)
    print(json.dumps({"output": str(output), **report["summary"]}, ensure_ascii=False))
    return 1 if report["summary"]["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
