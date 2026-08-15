#!/usr/bin/env python3
"""Backfill board classification into existing daily profile CSV files."""

from __future__ import annotations

import argparse
import csv
import os
import tempfile
from pathlib import Path

from kolmo.ashare.board_rules import board_for_symbol
from kolmo.paths import data_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Add or refresh the board column in profile/daily files.")
    parser.add_argument("--profile-root", default="", help="Default: $KOLMO_DATA_ROOT/profile/daily.")
    parser.add_argument("--exchange", choices=["sz", "sh", "all"], default="all")
    parser.add_argument("--start-date", default="", help="Inclusive YYYYMMDD.")
    parser.add_argument("--end-date", default="", help="Inclusive YYYYMMDD.")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def iter_profile_files(root: Path, exchange: str, start_date: str, end_date: str):
    exchanges = ["sz", "sh"] if exchange == "all" else [exchange]
    for item in exchanges:
        exchange_root = root / item
        if not exchange_root.is_dir():
            continue
        for path in sorted(exchange_root.glob("*/*/*.csv")):
            trade_date = path.stem
            if len(trade_date) != 8 or not trade_date.isdigit():
                continue
            if start_date and trade_date < start_date:
                continue
            if end_date and trade_date > end_date:
                continue
            yield path


def rewrite_file(path: Path, dry_run: bool) -> tuple[int, bool]:
    with path.open("r", encoding="utf-8", newline="") as input_file:
        reader = csv.DictReader(input_file)
        if not reader.fieldnames:
            return 0, False
        fieldnames = list(reader.fieldnames)
        had_board = "board" in fieldnames
        if not had_board:
            insert_at = fieldnames.index("exchange") + 1 if "exchange" in fieldnames else len(fieldnames)
            fieldnames.insert(insert_at, "board")
        rows = []
        changed = not had_board
        for row in reader:
            board = board_for_symbol(row.get("symbol", ""))
            if row.get("board", "") != board:
                row["board"] = board
                changed = True
            rows.append(row)

    if dry_run or not changed:
        return len(rows), changed

    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with temporary.open("w", encoding="utf-8", newline="") as output_file:
            writer = csv.DictWriter(output_file, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return len(rows), True


def main() -> int:
    args = parse_args()
    root = Path(args.profile_root or data_path("profile", "daily"))
    files = 0
    changed_files = 0
    rows = 0
    for path in iter_profile_files(root, args.exchange, args.start_date, args.end_date):
        count, changed = rewrite_file(path, args.dry_run)
        files += 1
        rows += count
        changed_files += 1 if changed else 0
    action = "would_update" if args.dry_run else "updated"
    print(f"files={files} {action}={changed_files} rows={rows} root={root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
