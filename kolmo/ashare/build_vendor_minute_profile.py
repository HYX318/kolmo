#!/usr/bin/env python3
"""Build staged, date-partitioned minute profiles from vendor 1-minute ZIPs."""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import json
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from kolmo.ashare.minute_product import (
    FREQUENCIES,
    MINUTE_BAR_COLUMNS,
    MINUTE_BAR_SCHEMA,
    PRODUCT_ID,
    SCHEMA_VERSION,
    aggregate_bars,
    normalize_vendor_bars,
    parquet_metadata,
    partition_path,
    validate_canonical_bars,
)
from kolmo.ashare.minute_corrections import (
    CORRECTION_POLICY_ID,
    EXCLUSION_POLICY_ID,
    OBSERVATION_POLICY_ID,
    apply_vendor_corrections,
    exclude_zero_price_activity_days,
    observe_post_close_adjustments,
)
from kolmo.ashare.vendor_minute import DEFAULT_ROOT, VendorMinuteDataset
from kolmo.ashare.vendor_minute_delisted import VendorDelistedMinuteDataset
from kolmo.paths import data_path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_parquet(
    frame: pd.DataFrame,
    path: Path,
    *,
    frequency: int,
    exchange: str,
    trade_date: str,
    source: str,
    build_id: str,
    compression_level: int,
    row_group_size: int,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(frame.loc[:, MINUTE_BAR_COLUMNS], schema=MINUTE_BAR_SCHEMA, preserve_index=False)
    table = table.replace_schema_metadata(parquet_metadata(
        frequency=frequency, exchange=exchange, trade_date=trade_date,
        source=source, build_id=build_id,
    ))
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        pq.write_table(
            table, temporary, compression="zstd", compression_level=compression_level,
            row_group_size=row_group_size, write_statistics=True,
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class FragmentWriter:
    """Bound memory by writing one fragment per symbol batch and day/exchange."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.partitions: set[tuple[str, str]] = set()
        self.rows = 0

    def write_batch(self, frames: list[pd.DataFrame], batch_number: int) -> None:
        if not frames:
            return
        batch = pd.concat(frames, ignore_index=True)
        dates = batch["trade_time"].dt.normalize()
        exchanges = batch["symbol"].str[-2:].str.lower()
        for (date_stamp, exchange), indexes in batch.groupby([dates, exchanges], sort=False).groups.items():
            trade_date = date_stamp.strftime("%Y-%m-%d")
            partition = batch.loc[indexes, MINUTE_BAR_COLUMNS]
            path = self.root / trade_date / exchange / f"batch-{batch_number:05d}.parquet"
            path.parent.mkdir(parents=True, exist_ok=True)
            table = pa.Table.from_pandas(partition, schema=MINUTE_BAR_SCHEMA, preserve_index=False)
            # Fragments are short-lived and read once. Snappy avoids spending
            # CPU on ZSTD data that will immediately be decompressed again.
            pq.write_table(table, path, compression="snappy")
            self.partitions.add((trade_date, exchange))
            self.rows += len(partition)


def stage_symbol_chunk(
    source_root: Path,
    year: int,
    start_date: str,
    end_date: str,
    entries: list[tuple[str, bool]],
    fragment_root: Path,
    batch_symbols: int,
    worker_number: int,
) -> dict[str, object]:
    """Decode one independent symbol shard and write uniquely named fragments."""
    fragments = FragmentWriter(fragment_root)
    pending: list[pd.DataFrame] = []
    missing = 0
    batches = 0
    corrections: list[dict[str, object]] = []
    exclusions: list[dict[str, object]] = []
    observations: list[dict[str, object]] = []
    unresolved: list[dict[str, str]] = []
    with VendorMinuteDataset.discover(source_root, year, 1) as active:
        needs_delisted = any(is_delisted for _, is_delisted in entries)
        delisted = VendorDelistedMinuteDataset.discover(source_root, year, 1) if needs_delisted else None
        try:
            for index, (symbol, is_delisted) in enumerate(entries, start=1):
                source = delisted if is_delisted else active
                assert source is not None
                try:
                    vendor = source.read_symbol(
                        symbol, start_date=start_date, end_date=end_date,
                    )
                except KeyError:
                    missing += 1
                    continue
                frame = normalize_vendor_bars(vendor, symbol, 1)
                frame, applied = apply_vendor_corrections(frame, symbol)
                frame, excluded = exclude_zero_price_activity_days(frame, symbol)
                try:
                    validate_canonical_bars(frame, 1)
                except ValueError as exc:
                    unresolved.append({"symbol": symbol, "error": str(exc)})
                    continue
                corrections.extend(applied)
                exclusions.extend(excluded)
                observations.extend(observe_post_close_adjustments(frame, symbol))
                if not frame.empty:
                    pending.append(frame)
                if len(pending) >= batch_symbols or index == len(entries):
                    batches += 1
                    fragments.write_batch(pending, worker_number * 1_000_000 + batches)
                    pending.clear()
        finally:
            if delisted:
                delisted.close()
    return {
        "partitions": sorted(fragments.partitions),
        "rows": fragments.rows,
        "missing": missing,
        "symbols": len(entries),
        "corrections": corrections,
        "exclusions": exclusions,
        "observations": observations,
        "unresolved": unresolved,
    }


def build_daily_partition(
    fragment_root: Path,
    product_root: Path,
    trade_date: str,
    exchange: str,
    build_id: str,
    compression_level: int,
    row_group_size: int,
) -> dict[str, dict[str, int | str]]:
    """Merge and derive one independent day/exchange partition."""
    paths = sorted((fragment_root / trade_date / exchange).glob("*.parquet"))
    tables = [pq.read_table(path, use_threads=False) for path in paths]
    frame = pa.concat_tables(tables).to_pandas()
    frame = frame.sort_values(["symbol", "trade_time"], kind="stable").reset_index(drop=True)
    validate_canonical_bars(frame, 1)
    result: dict[str, dict[str, int | str]] = {}
    for frequency in FREQUENCIES:
        output = frame if frequency == 1 else aggregate_bars(frame, frequency)
        validate_canonical_bars(output, frequency)
        destination = partition_path(product_root, frequency, exchange, trade_date)
        write_parquet(
            output, destination, frequency=frequency, exchange=exchange,
            trade_date=trade_date, source="vendor_1m_derived" if frequency != 1 else "vendor_1m",
            build_id=build_id, compression_level=compression_level,
            row_group_size=row_group_size,
        )
        result[str(frequency)] = {
            "rows": len(output), "bytes": destination.stat().st_size,
            "trade_date": trade_date,
        }
    return result


def build_profile(args: argparse.Namespace) -> dict[str, object]:
    total_started = perf_counter()
    build_id = args.build_id.strip() or f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    output_dir = Path(args.output_dir or data_path("staging", "minute", build_id))
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"staging output is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    product_root = output_dir / "profile" / "minute" / "v1"
    fragment_root = output_dir / "_fragments"
    start_date = args.start_date or f"{args.year}-01-01"
    end_date = args.end_date or f"{args.year}-12-31"
    if start_date[:4] != str(args.year) or end_date[:4] != str(args.year) or start_date > end_date:
        raise ValueError("date bounds must be ordered and belong to --year")

    manifest: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "product_id": PRODUCT_ID,
        "build_id": build_id,
        "year": args.year,
        "date_range": {"start": start_date, "end": end_date},
        "status": "building",
        "started_at": utc_now(),
        "completed_at": None,
        "source_archives": [],
        "symbols": {"active": 0, "delisted": 0, "without_year_data": 0},
        "outputs": {},
        "settings": {
            "batch_symbols": args.batch_symbols,
            "row_group_size": args.row_group_size,
            "compression": "zstd",
            "compression_level": args.compression_level,
            "fragment_compression": "snappy",
            "workers": args.workers,
        },
        "timings_seconds": {},
        "corrections": {
            "policy_id": CORRECTION_POLICY_ID,
            "applied_rows": 0,
            "report": "quality/corrections.json",
        },
        "unresolved": {
            "issues": 0,
            "report": "quality/unresolved.json",
        },
        "exclusions": {
            "policy_id": EXCLUSION_POLICY_ID,
            "excluded_rows": 0,
            "report": "quality/exclusions.json",
        },
        "observations": {
            "policy_id": OBSERVATION_POLICY_ID,
            "observed_rows": 0,
            "report": "quality/observations.json",
        },
    }
    manifest_path = output_dir / "manifest.json"
    write_json_atomic(manifest_path, manifest)

    fragments = FragmentWriter(fragment_root)
    try:
        with VendorMinuteDataset.discover(args.source_root, args.year, 1) as active:
            delisted_context = (
                VendorDelistedMinuteDataset.discover(args.source_root, args.year, 1)
                if args.include_delisted else None
            )
            try:
                active_symbols = list(active.symbols)
                delisted_symbols = list(delisted_context.symbols) if delisted_context else []
                overlap = set(active_symbols).intersection(delisted_symbols)
                if overlap:
                    raise ValueError(f"active and delisted sources overlap: {sorted(overlap)[:10]}")
                selected = [(symbol, False) for symbol in active_symbols]
                selected.extend((symbol, True) for symbol in delisted_symbols)
                if args.limit:
                    selected = selected[:args.limit]
                manifest["source_archives"] = [
                    {"path": str(path), "bytes": path.stat().st_size} for path in active.paths
                ]
                if delisted_context:
                    manifest["source_archives"].append({
                        "path": str(delisted_context.path), "bytes": delisted_context.path.stat().st_size,
                    })
                manifest["symbols"] = {
                    "active": min(len(active_symbols), len(selected)),
                    "delisted": max(0, len(selected) - len(active_symbols)),
                    "without_year_data": 0,
                }
                write_json_atomic(manifest_path, manifest)
            finally:
                if delisted_context:
                    delisted_context.close()

        worker_count = min(args.workers, max(1, len(selected)))
        chunks = [selected[index::worker_count] for index in range(worker_count)]

        def record_staging(result: dict[str, object]) -> None:
            fragments.partitions.update(result["partitions"])
            fragments.rows += int(result["rows"])
            counts = manifest["symbols"]
            assert isinstance(counts, dict)
            counts["without_year_data"] = int(counts["without_year_data"]) + int(result["missing"])
            applied_corrections.extend(result["corrections"])
            applied_exclusions.extend(result["exclusions"])
            observations.extend(result["observations"])
            unresolved_issues.extend(result["unresolved"])

        applied_corrections: list[dict[str, object]] = []
        applied_exclusions: list[dict[str, object]] = []
        observations: list[dict[str, object]] = []
        unresolved_issues: list[dict[str, str]] = []

        if worker_count == 1:
            result = stage_symbol_chunk(
                args.source_root, args.year, start_date, end_date, chunks[0],
                fragment_root, args.batch_symbols, 0,
            )
            record_staging(result)
            print(f"[{len(selected)}/{len(selected)}] staged_rows={fragments.rows} workers=1", flush=True)
        else:
            with ProcessPoolExecutor(max_workers=worker_count) as executor:
                futures = [
                    executor.submit(
                        stage_symbol_chunk, args.source_root, args.year, start_date, end_date,
                        chunk, fragment_root, args.batch_symbols, worker_number,
                    )
                    for worker_number, chunk in enumerate(chunks)
                ]
                completed_symbols = 0
                for future in as_completed(futures):
                    result = future.result()
                    record_staging(result)
                    completed_symbols += int(result["symbols"])
                    print(
                        f"[{completed_symbols}/{len(selected)}] staged_rows={fragments.rows} workers={worker_count}",
                        flush=True,
                    )

        manifest["timings_seconds"]["read_and_fragment"] = round(perf_counter() - total_started, 3)
        applied_corrections.sort(key=lambda item: (str(item["trade_time"]), str(item["symbol"])))
        correction_summary = manifest["corrections"]
        assert isinstance(correction_summary, dict)
        correction_summary["applied_rows"] = len(applied_corrections)
        write_json_atomic(output_dir / "quality" / "corrections.json", {
            "schema_version": "1.0.0",
            "policy_id": CORRECTION_POLICY_ID,
            "applied_rows": len(applied_corrections),
            "records": applied_corrections,
        })
        applied_exclusions.sort(key=lambda item: str(item["symbol"]))
        excluded_rows = sum(int(item["excluded_rows"]) for item in applied_exclusions)
        exclusion_summary = manifest["exclusions"]
        assert isinstance(exclusion_summary, dict)
        exclusion_summary["excluded_rows"] = excluded_rows
        write_json_atomic(output_dir / "quality" / "exclusions.json", {
            "schema_version": "1.0.0",
            "policy_id": EXCLUSION_POLICY_ID,
            "excluded_rows": excluded_rows,
            "records": applied_exclusions,
        })
        observations.sort(key=lambda item: (str(item["trade_time"]), str(item["symbol"])))
        observation_summary = manifest["observations"]
        assert isinstance(observation_summary, dict)
        observation_summary["observed_rows"] = len(observations)
        write_json_atomic(output_dir / "quality" / "observations.json", {
            "schema_version": "1.0.0",
            "policy_id": OBSERVATION_POLICY_ID,
            "observed_rows": len(observations),
            "records": observations,
        })
        unresolved_issues.sort(key=lambda item: item["symbol"])
        unresolved_summary = manifest["unresolved"]
        assert isinstance(unresolved_summary, dict)
        unresolved_summary["issues"] = len(unresolved_issues)
        write_json_atomic(output_dir / "quality" / "unresolved.json", {
            "schema_version": "1.0.0",
            "issues": len(unresolved_issues),
            "records": unresolved_issues,
        })
        write_json_atomic(manifest_path, manifest)
        if unresolved_issues:
            raise ValueError(
                f"staging found {len(unresolved_issues)} unresolved symbol-level issues; "
                f"report={output_dir / 'quality' / 'unresolved.json'}; "
                f"sample={unresolved_issues[:5]}"
            )

        partition_started = perf_counter()
        output_stats = {
            str(frequency): {
                "partitions": 0, "rows": 0, "bytes": 0,
                "first_trade_date": None, "last_trade_date": None,
            }
            for frequency in FREQUENCIES
        }
        partitions = sorted(fragments.partitions)

        def record_partition(result: dict[str, dict[str, int | str]]) -> None:
            for frequency, values in result.items():
                stats = output_stats[frequency]
                trade_date = str(values["trade_date"])
                stats["partitions"] += 1
                stats["rows"] += int(values["rows"])
                stats["bytes"] += int(values["bytes"])
                if stats["first_trade_date"] is None or trade_date < stats["first_trade_date"]:
                    stats["first_trade_date"] = trade_date
                if stats["last_trade_date"] is None or trade_date > stats["last_trade_date"]:
                    stats["last_trade_date"] = trade_date

        if args.workers == 1:
            for number, (trade_date, exchange) in enumerate(partitions, start=1):
                record_partition(build_daily_partition(
                    fragment_root, product_root, trade_date, exchange, build_id,
                    args.compression_level, args.row_group_size,
                ))
                if number % 25 == 0 or number == len(partitions):
                    print(f"[{number}/{len(partitions)}] built_partition={trade_date}/{exchange}", flush=True)
        else:
            with ProcessPoolExecutor(max_workers=args.workers) as executor:
                futures = {
                    executor.submit(
                        build_daily_partition, fragment_root, product_root, trade_date, exchange,
                        build_id, args.compression_level, args.row_group_size,
                    ): (trade_date, exchange)
                    for trade_date, exchange in partitions
                }
                for number, future in enumerate(as_completed(futures), start=1):
                    record_partition(future.result())
                    if number % 25 == 0 or number == len(partitions):
                        trade_date, exchange = futures[future]
                        print(
                            f"[{number}/{len(partitions)}] built_partition={trade_date}/{exchange} workers={args.workers}",
                            flush=True,
                        )

        manifest["outputs"] = output_stats
        manifest["timings_seconds"]["merge_derive_write"] = round(perf_counter() - partition_started, 3)
        manifest["timings_seconds"]["total"] = round(perf_counter() - total_started, 3)
        manifest["status"] = "built_not_validated"
        manifest["completed_at"] = utc_now()
        write_json_atomic(manifest_path, manifest)
        shutil.rmtree(fragment_root)
        return manifest
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["completed_at"] = utc_now()
        manifest["error"] = repr(exc)
        write_json_atomic(manifest_path, manifest)
        raise


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build staged daily Parquet profiles from vendor 1-minute bars.")
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--source-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--build-id", default="")
    parser.add_argument("--include-delisted", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--batch-symbols", type=int, default=50)
    parser.add_argument("--row-group-size", type=int, default=65_536)
    parser.add_argument("--compression-level", type=int, default=3)
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--limit", type=int, default=0, help="Smoke-test symbol limit; 0 means all.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if (
        args.batch_symbols < 1 or args.row_group_size < 1 or args.limit < 0
        or not 1 <= args.compression_level <= 22 or args.workers < 1
    ):
        raise ValueError("batch, row-group, and limit arguments are invalid")
    report = build_profile(args)
    print(json.dumps({"build_id": report["build_id"], "status": report["status"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
