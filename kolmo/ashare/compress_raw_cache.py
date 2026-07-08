#!/usr/bin/env python3
"""Compress per-symbol raw cache CSV files to gzip."""

from __future__ import annotations

import argparse
import gzip
import shutil
from pathlib import Path

from kolmo.paths import data_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compress raw cache CSV files to .csv.gz.")
    parser.add_argument("--source", default="baostock", choices=["baostock"])
    parser.add_argument("--exchange", default="sz", choices=["sz", "sh"])
    parser.add_argument("--adjust", default="qfq")
    parser.add_argument(
        "--input-dir",
        default="",
        help="Raw cache directory. Defaults to KOLMO_DATA_ROOT/raw/ashare/{source}/{exchange}/daily/{adjust}.",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Keep original .csv files after writing .csv.gz.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Optional file limit for testing.")
    return parser.parse_args()


def compress_file(path: Path, keep: bool) -> bool:
    if path.suffix != ".csv":
        return False
    output = path.with_suffix(path.suffix + ".gz")
    if output.exists() and output.stat().st_size > 0:
        if not keep:
            path.unlink()
        return False

    with path.open("rb") as source:
        with gzip.open(output, "wb", compresslevel=6) as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)

    if not keep:
        path.unlink()
    return True


def main() -> int:
    args = parse_args()
    input_dir = Path(
        args.input_dir
        or data_path("raw", "ashare", args.source, args.exchange, "daily", args.adjust)
    )
    files = sorted(input_dir.glob("*.csv"))
    if args.limit > 0:
        files = files[: args.limit]

    converted = 0
    for path in files:
        if compress_file(path, args.keep):
            converted += 1

    print(f"input_dir={input_dir} files={len(files)} converted={converted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
