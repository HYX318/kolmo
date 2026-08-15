#!/usr/bin/env python3
"""Partition normalized daily bars into one file per trade date."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import tempfile
from collections import OrderedDict
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Split a normalized A-share daily bar CSV into one file per date."
    )
    parser.add_argument("--input", required=True, help="Normalized combined daily bar CSV.")
    parser.add_argument("--output-dir", required=True, help="Directory for YYYYMMDD.csv files.")
    parser.add_argument(
        "--date-column",
        default="date",
        help="Date column name in YYYYMMDD or YYYY-MM-DD format.",
    )
    parser.add_argument(
        "--extension",
        default="csv",
        choices=["csv", "txt"],
        help="Output file extension.",
    )
    parser.add_argument(
        "--layout",
        choices=["year-month", "flat"],
        default="year-month",
        help="Output layout. year-month writes YYYY/MM/YYYYMMDD.ext.",
    )
    parser.add_argument(
        "--delimiter",
        default=",",
        help="Output delimiter. Use '\\t' for tab-separated txt.",
    )
    parser.add_argument(
        "--overwrite",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Overwrite existing date files.",
    )
    parser.add_argument(
        "--fetch-evidence",
        default="",
        help="Completed BaoStock fetch-evidence JSON to bind to every written partition.",
    )
    return parser.parse_args()


def normalize_date(value: str) -> str:
    text = value.strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10].replace("-", "")
    if len(text) >= 8 and text[:8].isdigit():
        return text[:8]
    raise ValueError(f"invalid date: {value}")


def parse_delimiter(value: str) -> str:
    if value == r"\t":
        return "\t"
    return value


def output_path_for_date(output_dir: Path, trade_date: str, extension: str, layout: str) -> Path:
    if layout == "flat":
        return output_dir / f"{trade_date}.{extension}"
    return output_dir / trade_date[:4] / trade_date[4:6] / f"{trade_date}.{extension}"


def provenance_path_for_profile(profile_path: Path) -> Path:
    return profile_path.with_name(f"{profile_path.name}.provenance.json")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_fetch_evidence(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as source:
            evidence = json.load(source)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read fetch evidence: {path}: {exc}") from exc
    required = {
        "schema_version", "product_id", "run_id", "exchange", "start_date", "end_date",
        "status", "failure_manifest_path", "failure_manifest_sha256", "symbols",
    }
    missing = sorted(required - set(evidence)) if isinstance(evidence, dict) else sorted(required)
    if missing:
        raise ValueError(f"invalid fetch evidence {path}; missing fields: {missing}")
    return evidence


def write_atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            json.dump(payload, output, ensure_ascii=True, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> int:
    args = parse_args()
    input_path = Path(args.input)
    output_dir = Path(args.output_dir)
    delimiter = parse_delimiter(args.delimiter)
    fetch_evidence_path = Path(args.fetch_evidence).resolve() if args.fetch_evidence else None
    fetch_evidence = load_fetch_evidence(fetch_evidence_path) if fetch_evidence_path else None

    if not input_path.is_file():
        print(f"input does not exist: {input_path}", file=sys.stderr)
        return 1

    writers: OrderedDict[str, tuple[object, csv.DictWriter, Path, Path]] = OrderedDict()
    rows = 0

    try:
        with input_path.open("r", encoding="utf-8", newline="") as input_file:
            reader = csv.DictReader(input_file)
            if not reader.fieldnames:
                print(f"input has no header: {input_path}", file=sys.stderr)
                return 1
            if args.date_column not in reader.fieldnames:
                print(f"missing date column: {args.date_column}", file=sys.stderr)
                return 1

            for row in reader:
                trade_date = normalize_date(row[args.date_column])
                writer_entry = writers.get(trade_date)
                if writer_entry is None:
                    output_path = output_path_for_date(
                        output_dir, trade_date, args.extension, args.layout
                    )
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    if not args.overwrite:
                        raise ValueError(
                            "non-atomic append mode is not supported for date partitions; "
                            "rerun with --overwrite"
                        )
                    descriptor, temporary_name = tempfile.mkstemp(
                        prefix=f".{output_path.name}.",
                        suffix=".tmp",
                        dir=output_path.parent,
                    )
                    os.close(descriptor)
                    temporary_path = Path(temporary_name)
                    file = temporary_path.open("w", encoding="utf-8", newline="")
                    writer = csv.DictWriter(
                        file,
                        fieldnames=reader.fieldnames,
                        delimiter=delimiter,
                        lineterminator="\n",
                    )
                    writer.writeheader()
                    writer_entry = (file, writer, temporary_path, output_path)
                    writers[trade_date] = writer_entry

                writer_entry[1].writerow(row)
                rows += 1
        for file, _, temporary_path, output_path in writers.values():
            file.flush()
            os.fsync(file.fileno())
            file.close()
            os.replace(temporary_path, output_path)
        if fetch_evidence_path and fetch_evidence:
            evidence_hash = sha256_file(fetch_evidence_path)
            for trade_date, (_, _, _, output_path) in writers.items():
                provenance = {
                    "schema_version": "1.0.0",
                    "product_id": "profile_daily_partition_provenance",
                    "date": trade_date,
                    "exchange": fetch_evidence["exchange"],
                    "profile_path": str(output_path.resolve()),
                    "profile_sha256": sha256_file(output_path),
                    "fetch_evidence_path": str(fetch_evidence_path),
                    "fetch_evidence_sha256": evidence_hash,
                }
                write_atomic_json(provenance_path_for_profile(output_path), provenance)
    finally:
        for file, _, temporary_path, _ in writers.values():
            if not file.closed:
                file.close()
            temporary_path.unlink(missing_ok=True)

    print(f"rows={rows} dates={len(writers)} output_dir={output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
