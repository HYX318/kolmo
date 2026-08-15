#!/usr/bin/env python3
"""Compare Kolmo profile OHLC against an exchange/reference OHLC data product."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from kolmo.data_products import PROFILE_OHLC_CHECK_COLUMNS, normalize_date
from kolmo.paths import data_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate profile OHLC against reference daily OHLC files.")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--exchange", choices=["sz", "sh", "all"], default="all")
    parser.add_argument("--profile-root", default="", help="Default: $KOLMO_DATA_ROOT/profile/daily.")
    parser.add_argument(
        "--reference-root",
        default="",
        help="Default: $KOLMO_DATA_ROOT/raw/ashare/exchange_ohlc/daily. Files are {exchange}/YYYY/MM/YYYYMMDD.csv.",
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--tolerance", type=float, default=0.0001)
    return parser.parse_args()


def discover_dates(profile_root: Path, exchanges: list[str], start: str, end: str) -> list[str]:
    dates: set[str] = set()
    for exchange in exchanges:
        for path in (profile_root / exchange).glob("*/*/*.csv"):
            if path.stem.isdigit() and start <= path.stem <= end:
                dates.add(path.stem)
    return sorted(dates)


def read_reference(path: Path) -> dict[str, dict[str, str]]:
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        required = {"symbol", "open", "high", "low", "close"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise SystemExit(f"reference file must contain {sorted(required)}: {path}")
        return {row["symbol"].upper(): row for row in reader}


def as_float(row: dict[str, str], name: str) -> float:
    value = row.get(name, "").strip()
    return float(value) if value else 0.0


def status_for(diffs: list[float], tolerance: float) -> str:
    return "ok" if all(abs(value) <= tolerance for value in diffs) else "mismatch"


def main() -> int:
    args = parse_args()
    start = normalize_date(args.start_date)
    end = normalize_date(args.end_date)
    exchanges = ["sz", "sh"] if args.exchange == "all" else [args.exchange]
    profile_root = Path(args.profile_root) if args.profile_root else data_path("profile", "daily")
    reference_root = Path(args.reference_root) if args.reference_root else data_path("raw", "ashare", "exchange_ohlc", "daily")
    dates = discover_dates(profile_root, exchanges, start, end)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    summary = {"checked": 0, "missing_reference": 0, "mismatch": 0}
    with output.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=PROFILE_OHLC_CHECK_COLUMNS, lineterminator="\n")
        writer.writeheader()
        for trade_date in dates:
            for exchange in exchanges:
                profile_path = profile_root / exchange / trade_date[:4] / trade_date[4:6] / f"{trade_date}.csv"
                if not profile_path.is_file():
                    continue
                reference_path = reference_root / exchange / trade_date[:4] / trade_date[4:6] / f"{trade_date}.csv"
                reference = read_reference(reference_path)
                with profile_path.open("r", encoding="utf-8", newline="") as profile_file:
                    for row in csv.DictReader(profile_file):
                        symbol = row["symbol"].upper()
                        ref = reference.get(symbol)
                        if ref is None:
                            summary["missing_reference"] += 1
                            continue
                        diffs = [
                            as_float(row, "open") - as_float(ref, "open"),
                            as_float(row, "high") - as_float(ref, "high"),
                            as_float(row, "low") - as_float(ref, "low"),
                            as_float(row, "close") - as_float(ref, "close"),
                        ]
                        status = status_for(diffs, args.tolerance)
                        if status == "mismatch":
                            summary["mismatch"] += 1
                        summary["checked"] += 1
                        writer.writerow(
                            {
                                "date": trade_date,
                                "symbol": symbol,
                                "exchange": exchange,
                                "source": "exchange_ohlc",
                                "profile_open": row.get("open", ""),
                                "source_open": ref.get("open", ""),
                                "profile_high": row.get("high", ""),
                                "source_high": ref.get("high", ""),
                                "profile_low": row.get("low", ""),
                                "source_low": ref.get("low", ""),
                                "profile_close": row.get("close", ""),
                                "source_close": ref.get("close", ""),
                                "open_diff": f"{diffs[0]:.8f}",
                                "high_diff": f"{diffs[1]:.8f}",
                                "low_diff": f"{diffs[2]:.8f}",
                                "close_diff": f"{diffs[3]:.8f}",
                                "status": status,
                            }
                        )
    print(json.dumps({"output": str(output), **summary}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
