#!/usr/bin/env python3
"""Build provider-neutral latest drawdown statistics for US assets."""

from __future__ import annotations

import argparse
import csv
import os
import sys
import tempfile
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from kolmo.paths import data_path
from kolmo.us_market.daily import (
    UniverseEntry,
    daily_path,
    default_universe_path,
    load_universe,
    read_daily_rows,
)


DRAWDOWN_COLUMNS = [
    "as_of",
    "symbol",
    "asset_type",
    "close",
    "split_adjusted_close",
    "ath",
    "ath_date",
    "drawdown_from_ath",
    "sessions_since_ath",
    "high_252d",
    "drawdown_252d",
    "high_756d",
    "drawdown_756d",
    "total_return_drawdown_from_ath",
    "observations",
    "source",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build latest US stock and ETF drawdown statistics.")
    parser.add_argument("--universe", default="", help="Default: configs/us_value_universe.csv.")
    parser.add_argument("--us-root", default="", help="Default: $KOLMO_DATA_ROOT/US.")
    parser.add_argument(
        "--output",
        default="",
        help="Default: $KOLMO_DATA_ROOT/work/US/drawdown/latest.csv.",
    )
    return parser.parse_args()


def split_adjusted_closes(rows: Sequence[Mapping[str, str]]) -> list[float]:
    """Adjust raw closes for splits while deliberately excluding cash dividends."""
    adjusted_reversed: list[float] = []
    future_split_factor = 1.0
    for row in reversed(rows):
        adjusted_reversed.append(float(row["close"]) / future_split_factor)
        # Tiingo 的 splitFactor 在生效日记录；只调整该日期之前的历史价格。
        future_split_factor *= float(row["split_factor"])
    return list(reversed(adjusted_reversed))


def drawdown(current: float, peak: float) -> float:
    return current / peak - 1.0


def format_number(value: float) -> str:
    return format(value, ".10g")


def latest_drawdown_row(
    entry: UniverseEntry, rows: Sequence[Mapping[str, str]]
) -> dict[str, str]:
    if not rows:
        raise ValueError(f"no daily rows for {entry.asset_type}/{entry.symbol}")
    split_closes = split_adjusted_closes(rows)
    total_return_closes = [float(row["adj_close"]) for row in rows]
    current = split_closes[-1]
    ath = max(split_closes)
    # 同价创新高时采用最近一次，令峰值距离更符合观察语义。
    ath_index = max(index for index, value in enumerate(split_closes) if value == ath)
    high_252d = max(split_closes[-252:])
    high_756d = max(split_closes[-756:])
    total_return_ath = max(total_return_closes)
    latest = rows[-1]
    return {
        "as_of": str(latest["date"]),
        "symbol": entry.symbol,
        "asset_type": entry.asset_type,
        "close": str(latest["close"]),
        "split_adjusted_close": format_number(current),
        "ath": format_number(ath),
        "ath_date": str(rows[ath_index]["date"]),
        "drawdown_from_ath": format_number(drawdown(current, ath)),
        "sessions_since_ath": str(len(rows) - 1 - ath_index),
        "high_252d": format_number(high_252d),
        "drawdown_252d": format_number(drawdown(current, high_252d)),
        "high_756d": format_number(high_756d),
        "drawdown_756d": format_number(drawdown(current, high_756d)),
        "total_return_drawdown_from_ath": format_number(
            drawdown(total_return_closes[-1], total_return_ath)
        ),
        "observations": str(len(rows)),
        "source": str(latest["source"]),
    }


def build_drawdown_rows(entries: Iterable[UniverseEntry], us_root: Path) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    for entry in entries:
        path = daily_path(us_root, entry)
        if not path.is_file():
            raise FileNotFoundError(f"missing daily file: {path}")
        output.append(latest_drawdown_row(entry, read_daily_rows(path)))
    return sorted(output, key=lambda row: (row["asset_type"], row["symbol"]))


def write_csv_atomic(path: Path, rows: Iterable[Mapping[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=DRAWDOWN_COLUMNS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> int:
    args = parse_args()
    universe = Path(args.universe) if args.universe else default_universe_path()
    us_root = Path(args.us_root) if args.us_root else data_path("US")
    output = Path(args.output) if args.output else data_path("work", "US", "drawdown", "latest.csv")
    try:
        rows = build_drawdown_rows(load_universe(universe), us_root)
        write_csv_atomic(output, rows)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"assets={len(rows)} output={output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
