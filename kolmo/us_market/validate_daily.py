#!/usr/bin/env python3
"""Validate canonical US daily files without contacting the provider."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from kolmo.paths import data_path
from kolmo.us_market.daily import (
    daily_path,
    default_universe_path,
    load_universe,
    read_daily_rows,
    validate_daily_rows,
    write_json_atomic,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate Kolmo US daily-bar files.")
    parser.add_argument("--universe", default="", help="Default: configs/us_value_universe.csv.")
    parser.add_argument("--us-root", default="", help="Default: $KOLMO_DATA_ROOT/US.")
    parser.add_argument("--json-output", default="", help="Optional machine-readable report.")
    return parser.parse_args()


def validate_universe_files(universe: Path, us_root: Path) -> dict[str, object]:
    records: list[dict[str, object]] = []
    for entry in load_universe(universe):
        path = daily_path(us_root, entry)
        if not path.is_file():
            records.append({
                "symbol": entry.symbol,
                "asset_type": entry.asset_type,
                "path": str(path),
                "rows": 0,
                "first_date": "",
                "last_date": "",
                "ok": False,
                "errors": ["missing file"],
            })
            continue
        try:
            rows = read_daily_rows(path)
            errors = validate_daily_rows(rows, entry.symbol, entry.asset_type)
        except (OSError, ValueError) as exc:
            rows = []
            errors = [str(exc)]
        records.append({
            "symbol": entry.symbol,
            "asset_type": entry.asset_type,
            "path": str(path),
            "rows": len(rows),
            "first_date": rows[0]["date"] if rows else "",
            "last_date": rows[-1]["date"] if rows else "",
            "ok": not errors,
            "errors": errors,
        })
    failed = sum(not bool(record["ok"]) for record in records)
    return {
        "us_root": str(us_root),
        "ok": failed == 0,
        "assets_checked": len(records),
        "assets_failed": failed,
        "records": records,
    }


def main() -> int:
    args = parse_args()
    universe = Path(args.universe) if args.universe else default_universe_path()
    us_root = Path(args.us_root) if args.us_root else data_path("US")
    try:
        report = validate_universe_files(universe, us_root)
        if args.json_output:
            write_json_atomic(Path(args.json_output), report)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(
        f"assets={report['assets_checked']} failed={report['assets_failed']} us_root={us_root}",
        flush=True,
    )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
