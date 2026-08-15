"""Point-in-time selection for observed security-master snapshots."""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path


class SecurityMasterUnavailableError(RuntimeError):
    """Raised when no observed master snapshot was available by a decision date."""


def normalize_date(value: str) -> str:
    text = value.replace("-", "")
    if len(text) != 8 or not text.isdigit():
        raise ValueError("decision date must be YYYYMMDD or YYYY-MM-DD")
    datetime.strptime(text, "%Y%m%d")
    return text


def observed_snapshot_paths(root: Path) -> list[Path]:
    paths: list[Path] = []
    for path in root.glob("*/*/*.csv"):
        if len(path.stem) == 8 and path.stem.isdigit():
            paths.append(path)
    return sorted(paths)


def observed_date(path: Path) -> str:
    return normalize_date(path.stem)


def select_snapshot(root: Path, decision_date: str) -> Path:
    cutoff = normalize_date(decision_date)
    candidates = [path for path in observed_snapshot_paths(root) if observed_date(path) <= cutoff]
    if not candidates:
        raise SecurityMasterUnavailableError(
            f"no security-master observation is available on or before {cutoff}: {root}"
        )
    return candidates[-1]


def load_active_symbols(root: Path, decision_date: str) -> set[str]:
    path = select_snapshot(root, decision_date)
    with path.open("r", encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source)
        required = {"symbol", "status_as_of"}
        if not required.issubset(reader.fieldnames or []):
            raise SecurityMasterUnavailableError(f"invalid security-master schema: {path}")
        return {
            row["symbol"]
            for row in reader
            if row.get("status_as_of") == "active" and row.get("symbol")
        }
