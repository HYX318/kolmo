"""Read the vendor's nested ZIP bundles for delisted A-share minute bars."""

from __future__ import annotations

import io
import re
from pathlib import Path
from zipfile import ZipFile

import pandas as pd

from kolmo.ashare.vendor_minute import DEFAULT_ROOT, normalize_symbol


INNER_SYMBOL = re.compile(r"(?P<symbol>\d{6}\.(?:SH|SZ|BJ))\.zip$", re.IGNORECASE)


def find_delisted_archive(root: Path, year: int, frequency: int = 1) -> Path:
    period = "2010-2025" if year <= 2025 else str(year)
    folder = Path(root) / "退市股票" / period / f"{frequency}min"
    matches = sorted(folder.glob(f"A股退市股票{frequency}分钟历史行情_*.zip"))
    if len(matches) != 1:
        raise FileNotFoundError(f"expected one delisted {frequency}m archive under {folder}, found {len(matches)}")
    return matches[0]


class VendorDelistedMinuteDataset:
    def __init__(self, path: Path, year: int, frequency: int = 1) -> None:
        self.path = Path(path)
        self.year = year
        self.frequency = frequency
        self._outer = ZipFile(self.path)
        self._members: dict[str, str] = {}
        for name in self._outer.namelist():
            match = INNER_SYMBOL.search(name)
            if match:
                self._members[match.group("symbol").upper()] = name

    @classmethod
    def discover(cls, root: Path = DEFAULT_ROOT, year: int = 2026, frequency: int = 1) -> "VendorDelistedMinuteDataset":
        return cls(find_delisted_archive(root, year, frequency), year, frequency)

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self._members))

    def read_symbol(
        self, symbol: str, *, start_date: str = "", end_date: str = "",
    ) -> pd.DataFrame:
        symbol = normalize_symbol(symbol)
        member = self._members.get(symbol)
        if member is None:
            raise KeyError(f"{symbol} is not present in {self.path}")
        with ZipFile(io.BytesIO(self._outer.read(member))) as inner:
            target = next(
                (name for name in inner.namelist() if name.endswith(f"/{symbol}.parquet") and Path(name).parts[0] == str(self.year)),
                None,
            )
            if target is None:
                raise KeyError(f"{symbol} has no {self.year} data in {self.path}")
            frame = pd.read_parquet(io.BytesIO(inner.read(target)))
            if start_date:
                frame = frame.loc[
                    frame["trade_time"] >= pd.Timestamp(start_date, tz="Asia/Shanghai")
                ]
            if end_date:
                end = pd.Timestamp(end_date, tz="Asia/Shanghai") + pd.Timedelta(days=1)
                frame = frame.loc[frame["trade_time"] < end]
            return frame.reset_index(drop=True)

    def close(self) -> None:
        self._outer.close()

    def __enter__(self) -> "VendorDelistedMinuteDataset":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
