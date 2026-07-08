"""Shared filesystem locations for Kolmo data."""

from __future__ import annotations

import os
from pathlib import Path


DEFAULT_DATA_ROOT = Path("/Users/galoishuang/dat/all")


def data_root() -> Path:
    """Return the canonical data root.

    `KOLMO_DATA_ROOT` lets production and research jobs read/write the same
    external data location without changing command arguments.
    """
    return Path(os.environ.get("KOLMO_DATA_ROOT", str(DEFAULT_DATA_ROOT)))


def data_path(*parts: str) -> Path:
    return data_root().joinpath(*parts)
