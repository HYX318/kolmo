"""Canonical US daily-bar contract and filesystem helpers."""

from __future__ import annotations

import csv
import gzip
import io
import json
import math
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import date
from itertools import chain
from pathlib import Path
from typing import Iterable, Mapping


ASSET_TYPES = {"STK", "ETF"}
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,19}$")
DAILY_BAR_COLUMNS = [
    "date",
    "symbol",
    "asset_type",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "adj_open",
    "adj_high",
    "adj_low",
    "adj_close",
    "adj_volume",
    "div_cash",
    "split_factor",
    "source",
]


@dataclass(frozen=True)
class UniverseEntry:
    symbol: str
    asset_type: str
    provider_symbol: str


def normalize_symbol(value: str) -> str:
    symbol = value.strip().upper()
    if not SYMBOL_PATTERN.fullmatch(symbol):
        raise ValueError(f"invalid US symbol: {value!r}")
    return symbol


def normalize_asset_type(value: str) -> str:
    asset_type = value.strip().upper()
    if asset_type not in ASSET_TYPES:
        raise ValueError(f"asset_type must be one of {sorted(ASSET_TYPES)}: {value!r}")
    return asset_type


def normalize_iso_date(value: str) -> str:
    text = value.strip()[:10]
    date.fromisoformat(text)
    return text


def load_universe(path: Path) -> list[UniverseEntry]:
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        required = {"symbol", "asset_type"}
        missing = sorted(required - set(reader.fieldnames or []))
        if missing:
            raise ValueError(f"universe is missing columns: {', '.join(missing)}")
        entries: list[UniverseEntry] = []
        seen: set[tuple[str, str]] = set()
        for line_number, row in enumerate(reader, start=2):
            if str(row.get("enabled", "1")).strip().lower() in {"0", "false", "no", "off"}:
                continue
            try:
                symbol = normalize_symbol(row.get("symbol", ""))
                asset_type = normalize_asset_type(row.get("asset_type", ""))
                provider_symbol = normalize_symbol(row.get("provider_symbol", "") or symbol)
            except ValueError as exc:
                raise ValueError(f"{path}:{line_number}: {exc}") from exc
            key = (asset_type, symbol)
            if key in seen:
                raise ValueError(f"{path}:{line_number}: duplicate asset {asset_type}/{symbol}")
            seen.add(key)
            entries.append(UniverseEntry(symbol, asset_type, provider_symbol))
    if not entries:
        raise ValueError(f"universe contains no enabled assets: {path}")
    return entries


def daily_path(us_root: Path, entry: UniverseEntry) -> Path:
    return us_root / entry.asset_type / "1d" / f"{entry.symbol}.csv.gz"


def default_universe_path() -> Path:
    return Path(__file__).parents[2] / "configs" / "us_value_universe.csv"


def read_daily_rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with gzip.open(path, "rt", encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames != DAILY_BAR_COLUMNS:
            raise ValueError(f"unexpected daily-bar schema: {path}")
        return list(reader)


def merge_daily_rows(
    existing: Iterable[Mapping[str, str]], incoming: Iterable[Mapping[str, str]]
) -> list[dict[str, str]]:
    by_date: dict[str, dict[str, str]] = {}
    for row in chain(existing, incoming):
        by_date[str(row["date"])] = {column: str(row.get(column, "")) for column in DAILY_BAR_COLUMNS}
    return [by_date[key] for key in sorted(by_date)]


def validate_daily_rows(
    rows: Iterable[Mapping[str, str]], symbol: str, asset_type: str
) -> list[str]:
    errors: list[str] = []
    previous_date = ""
    for row_number, row in enumerate(rows, start=2):
        prefix = f"row {row_number}"
        try:
            trade_date = normalize_iso_date(str(row.get("date", "")))
        except ValueError:
            errors.append(f"{prefix}: invalid date")
            continue
        if trade_date <= previous_date:
            errors.append(f"{prefix}: dates are duplicate or not increasing")
        previous_date = trade_date
        if row.get("symbol") != symbol or row.get("asset_type") != asset_type:
            errors.append(f"{prefix}: asset identity mismatch")
        if not str(row.get("source", "")).strip():
            errors.append(f"{prefix}: missing source")
        values: dict[str, float] = {}
        for field in (
            "open", "high", "low", "close", "volume", "adj_open", "adj_high",
            "adj_low", "adj_close", "adj_volume", "div_cash", "split_factor",
        ):
            try:
                values[field] = float(str(row.get(field, "")))
                if not math.isfinite(values[field]):
                    raise ValueError
            except ValueError:
                errors.append(f"{prefix}: invalid {field}")
        if not all(field in values for field in ("open", "high", "low", "close")):
            continue
        if min(values[field] for field in ("open", "high", "low", "close")) <= 0:
            errors.append(f"{prefix}: raw OHLC must be positive")
        if values["high"] < max(values["open"], values["low"], values["close"]):
            errors.append(f"{prefix}: raw high is inconsistent")
        if values["low"] > min(values["open"], values["high"], values["close"]):
            errors.append(f"{prefix}: raw low is inconsistent")
        if all(field in values for field in ("adj_open", "adj_high", "adj_low", "adj_close")):
            if min(values[field] for field in ("adj_open", "adj_high", "adj_low", "adj_close")) <= 0:
                errors.append(f"{prefix}: adjusted OHLC must be positive")
            if values["adj_high"] < max(
                values["adj_open"], values["adj_low"], values["adj_close"]
            ):
                errors.append(f"{prefix}: adjusted high is inconsistent")
            if values["adj_low"] > min(
                values["adj_open"], values["adj_high"], values["adj_close"]
            ):
                errors.append(f"{prefix}: adjusted low is inconsistent")
        if values.get("volume", -1) < 0 or values.get("adj_volume", -1) < 0:
            errors.append(f"{prefix}: volume must be non-negative")
        if values.get("div_cash", -1) < 0 or values.get("split_factor", 0) <= 0:
            errors.append(f"{prefix}: invalid corporate action")
    if not previous_date:
        errors.append("no daily rows")
    return errors


def write_daily_rows_atomic(path: Path, rows: Iterable[Mapping[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as raw:
            # 固定 gzip 时间戳，使同一份规范化数据具有可复现的文件内容。
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
                with io.TextIOWrapper(compressed, encoding="utf-8", newline="") as output:
                    writer = csv.DictWriter(output, fieldnames=DAILY_BAR_COLUMNS, lineterminator="\n")
                    writer.writeheader()
                    writer.writerows(rows)
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def universe_entry_dict(entry: UniverseEntry) -> dict[str, str]:
    return asdict(entry)
