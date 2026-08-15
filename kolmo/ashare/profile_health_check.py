#!/usr/bin/env python3
"""Check completeness and schema health for Kolmo A-share daily profile files."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path
from statistics import median

from kolmo.paths import data_path


REQUIRED_COLUMNS = {
    "date",
    "symbol",
    "exchange",
    "board",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "trade_status",
    "is_st",
    "source",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check A-share daily profile completeness and schema health.")
    parser.add_argument("--profile-root", default="", help="Default: $KOLMO_DATA_ROOT/profile/daily.")
    parser.add_argument("--days", type=int, default=20, help="Number of latest profile dates to check.")
    parser.add_argument("--min-row-ratio", type=float, default=0.85, help="Fail if rows are below this ratio of recent median.")
    parser.add_argument("--min-absolute-rows", type=int, default=1000, help="Fail if rows are below this absolute threshold.")
    parser.add_argument(
        "--failures-root",
        default="",
        help="Deprecated compatibility option; health checks now follow partition provenance.",
    )
    parser.add_argument("--json-output", default="", help="Optional JSON report path.")
    parser.add_argument("--csv-output", default="", help="Optional per-date CSV report path.")
    return parser.parse_args()


def discover_dates(profile_root: Path, exchanges: list[str]) -> list[str]:
    dates: set[str] = set()
    for exchange in exchanges:
        for path in (profile_root / exchange).glob("*/*/*.csv"):
            if path.stem.isdigit() and len(path.stem) == 8:
                dates.add(path.stem)
    return sorted(dates)


def profile_path(profile_root: Path, exchange: str, trade_date: str) -> Path:
    return profile_root / exchange / trade_date[:4] / trade_date[4:6] / f"{trade_date}.csv"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch_evidence_for_profile(path: Path) -> dict[str, object]:
    provenance_path = path.with_name(f"{path.name}.provenance.json")
    if not provenance_path.is_file():
        return {"status": "missing", "failure_rows": 0, "path": ""}
    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        if provenance["profile_path"] != str(path.resolve()) or provenance["profile_sha256"] != sha256_file(path):
            raise ValueError("profile path or hash mismatch")
        evidence_path = Path(str(provenance["fetch_evidence_path"]))
        if not evidence_path.is_file() or provenance["fetch_evidence_sha256"] != sha256_file(evidence_path):
            raise ValueError("fetch evidence path or hash mismatch")
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        failed = evidence["symbols"]["failed"]
        if not isinstance(failed, int):
            raise ValueError("fetch evidence failed count is not an integer")
        return {
            "status": str(evidence.get("status", "invalid")),
            "failure_rows": failed,
            "path": str(evidence_path),
        }
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return {"status": "invalid", "failure_rows": 0, "path": str(provenance_path)}


def check_file(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {
            "exists": False,
            "rows": 0,
            "missing_columns": sorted(REQUIRED_COLUMNS),
            "duplicate_symbols": 0,
            "non_positive_ohlc": 0,
        }
    symbols: list[str] = []
    non_positive_ohlc = 0
    with path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        fieldnames = set(reader.fieldnames or [])
        missing = sorted(REQUIRED_COLUMNS - fieldnames)
        for row in reader:
            symbols.append(row.get("symbol", ""))
            try:
                values = [float(row.get(field, "0") or 0.0) for field in ["open", "high", "low", "close"]]
            except ValueError:
                values = [0.0]
            if any(value <= 0.0 for value in values):
                non_positive_ohlc += 1
    counts = Counter(symbols)
    duplicate_symbols = sum(count - 1 for count in counts.values() if count > 1)
    return {
        "exists": True,
        "rows": len(symbols),
        "missing_columns": missing,
        "duplicate_symbols": duplicate_symbols,
        "non_positive_ohlc": non_positive_ohlc,
    }


def rows_by_exchange(records: list[dict[str, object]], exchange: str) -> list[int]:
    return [int(record["rows"]) for record in records if record["exchange"] == exchange and bool(record["exists"])]


def enrich_status(records: list[dict[str, object]], min_row_ratio: float, min_absolute_rows: int) -> None:
    medians = {
        exchange: median(rows) if rows else 0
        for exchange, rows in ((exchange, rows_by_exchange(records, exchange)) for exchange in ["sz", "sh"])
    }
    for record in records:
        exchange = str(record["exchange"])
        row_floor = max(min_absolute_rows, int(medians[exchange] * min_row_ratio))
        issues: list[str] = []
        if not record["exists"]:
            issues.append("missing_file")
        if record["missing_columns"]:
            issues.append("missing_columns")
        if int(record["rows"]) < row_floor:
            issues.append("low_rows")
        if int(record["duplicate_symbols"]) > 0:
            issues.append("duplicate_symbols")
        if int(record["non_positive_ohlc"]) > 0:
            issues.append("non_positive_ohlc")
        if int(record["failure_rows"]) > 0:
            issues.append("fetch_failures")
        if record.get("fetch_evidence_status") in {"running", "login_failed", "invalid"}:
            issues.append("fetch_evidence_incomplete")
        record["row_floor"] = row_floor
        record["ok"] = not issues
        record["issues"] = issues


def write_csv(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "date",
        "exchange",
        "exists",
        "rows",
        "row_floor",
        "failure_rows",
        "duplicate_symbols",
        "non_positive_ohlc",
        "missing_columns",
        "ok",
        "issues",
        "path",
        "failure_file",
        "fetch_evidence_status",
        "fetch_evidence_file",
    ]
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for record in records:
            output = dict(record)
            output["missing_columns"] = ",".join(record["missing_columns"])
            output["issues"] = ",".join(record["issues"])
            writer.writerow(output)


def main() -> int:
    args = parse_args()
    exchanges = ["sz", "sh"]
    profile_root = Path(args.profile_root) if args.profile_root else data_path("profile", "daily")
    dates = discover_dates(profile_root, exchanges)[-args.days :]
    records: list[dict[str, object]] = []
    for trade_date in dates:
        for exchange in exchanges:
            path = profile_path(profile_root, exchange, trade_date)
            evidence = fetch_evidence_for_profile(path)
            record = {
                "date": trade_date,
                "exchange": exchange,
                "path": str(path),
                "failure_file": "",
                "failure_rows": evidence["failure_rows"],
                "fetch_evidence_status": evidence["status"],
                "fetch_evidence_file": evidence["path"],
                **check_file(path),
            }
            records.append(record)
    enrich_status(records, args.min_row_ratio, args.min_absolute_rows)
    failed = [record for record in records if not record["ok"]]
    latest_date = dates[-1] if dates else ""
    report = {
        "profile_root": str(profile_root),
        "latest_date": latest_date,
        "dates_checked": len(dates),
        "records_checked": len(records),
        "ok": not failed,
        "failed_records": len(failed),
        "records": records,
    }
    if args.json_output:
        output = Path(args.json_output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.csv_output:
        write_csv(Path(args.csv_output), records)
    print(
        json.dumps(
            {
                "latest_date": latest_date,
                "dates_checked": len(dates),
                "ok": report["ok"],
                "failed_records": len(failed),
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
