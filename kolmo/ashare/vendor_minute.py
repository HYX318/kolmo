#!/usr/bin/env python3
"""Read per-symbol Parquet bars from annual or monthly vendor ZIP files."""

from __future__ import annotations

import argparse
import io
import re
from collections.abc import Iterable
from pathlib import Path
from zipfile import ZipFile

import pandas as pd

from kolmo.paths import data_path


DEFAULT_ROOT = data_path("raw", "vendor_candidate", "minute_201001_202609", "分钟线数据")
FREQUENCIES = (1, 5, 15, 30, 60)
REQUIRED_COLUMNS = (
    "ts_code", "freq", "trade_time", "open", "close", "high", "low", "vol", "amount",
)
SYMBOL_PATTERN = re.compile(r"^\d{6}\.(?:SH|SZ|BJ)$")
ARCHIVE_PATTERN = re.compile(
    r"^A股(?P<frequency>1|5|15|30|60)(?:分钟|min)历史行情_"
    r"(?P<period>\d{4}(?:-(?:0[1-9]|1[0-2]))?)\.zip$"
)


def normalize_symbol(value: str) -> str:
    symbol = value.strip().upper()
    if not SYMBOL_PATTERN.fullmatch(symbol):
        raise ValueError(f"invalid A-share symbol: {value!r}")
    return symbol


def find_archive(root: Path, year: int, frequency: int) -> Path:
    """Find a single annual archive (legacy API)."""
    paths = find_archives(root, year, frequency)
    if len(paths) != 1:
        raise FileNotFoundError(
            f"expected one annual {frequency}m archive under {Path(root) / str(year)}, "
            f"found {len(paths)} monthly shards"
        )
    return paths[0]


def find_archives(root: Path, year: int, frequency: int) -> tuple[Path, ...]:
    """Return one annual archive or an ordered set of monthly shards.

    Annual input wins when both layouts are present, preventing accidental
    double counting while a normalized annual artifact is staged nearby.
    """
    if frequency not in FREQUENCIES:
        raise ValueError(f"unsupported frequency: {frequency}")
    year_root = Path(root) / str(year)
    candidates: list[tuple[str, Path]] = []
    for path in year_root.rglob("*.zip") if year_root.is_dir() else ():
        match = ARCHIVE_PATTERN.fullmatch(path.name)
        if match and int(match.group("frequency")) == frequency:
            period = match.group("period")
            if period == str(year) or period.startswith(f"{year}-"):
                candidates.append((period, path))
    annual = [path for period, path in candidates if period == str(year)]
    if len(annual) > 1:
        raise FileNotFoundError(f"multiple annual {frequency}m archives under {year_root}: {annual}")
    if annual:
        return (annual[0],)
    monthly: dict[str, Path] = {}
    for period, path in candidates:
        if period in monthly:
            raise FileNotFoundError(
                f"duplicate {frequency}m archive period {period}: {monthly[period]} and {path}"
            )
        monthly[period] = path
    if not monthly:
        raise FileNotFoundError(f"no {frequency}m archives under {year_root}")
    return tuple(monthly[period] for period in sorted(monthly))


class VendorMinuteArchive:
    """An indexed annual archive. Only the requested symbol is decompressed."""

    def __init__(self, path: Path, frequency: int) -> None:
        self.path = Path(path)
        self.frequency = frequency
        self._archive = ZipFile(self.path)
        self._members = {
            Path(name).name.removesuffix(".parquet").upper(): name
            for name in self._archive.namelist()
            if name.lower().endswith(".parquet")
        }
        self._symbols = tuple(sorted(self._members))

    @classmethod
    def discover(cls, root: Path, year: int, frequency: int) -> "VendorMinuteArchive":
        return cls(find_archive(Path(root), year, frequency), frequency)

    @property
    def symbols(self) -> tuple[str, ...]:
        return self._symbols

    def has_symbol(self, symbol: str) -> bool:
        return symbol in self._members

    def read_symbol(
        self,
        symbol: str,
        *,
        columns: list[str] | None = None,
        start_date: str = "",
        end_date: str = "",
        validate_schema: bool = True,
    ) -> pd.DataFrame:
        symbol = normalize_symbol(symbol)
        member = self._members.get(symbol)
        if member is None:
            raise KeyError(f"{symbol} is not present in {self.path.name}")
        payload = io.BytesIO(self._archive.read(member))
        frame = pd.read_parquet(payload, columns=columns)
        if validate_schema and columns is None:
            missing = set(REQUIRED_COLUMNS).difference(frame.columns)
            if missing:
                raise ValueError(f"missing columns {sorted(missing)} in {self.path.name}:{member}")
        if "trade_time" in frame:
            if start_date:
                start = pd.Timestamp(start_date, tz="Asia/Shanghai")
                frame = frame.loc[frame["trade_time"] >= start]
            if end_date:
                end = pd.Timestamp(end_date, tz="Asia/Shanghai") + pd.Timedelta(days=1)
                frame = frame.loc[frame["trade_time"] < end]
        return frame.reset_index(drop=True)

    def close(self) -> None:
        self._archive.close()

    def __enter__(self) -> "VendorMinuteArchive":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __del__(self) -> None:
        archive = getattr(self, "_archive", None)
        if archive is not None:
            archive.close()


class VendorMinuteDataset:
    """Logical year/frequency dataset backed by one annual or many monthly ZIPs."""

    def __init__(self, paths: Iterable[Path], frequency: int) -> None:
        self.frequency = frequency
        self.archives = tuple(VendorMinuteArchive(path, frequency) for path in paths)
        if not self.archives:
            raise ValueError("at least one archive is required")

    @classmethod
    def discover(cls, root: Path, year: int, frequency: int) -> "VendorMinuteDataset":
        return cls(find_archives(Path(root), year, frequency), frequency)

    @property
    def paths(self) -> tuple[Path, ...]:
        return tuple(archive.path for archive in self.archives)

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(set().union(*(archive.symbols for archive in self.archives))))

    def read_symbol(
        self,
        symbol: str,
        *,
        columns: list[str] | None = None,
        start_date: str = "",
        end_date: str = "",
        validate_schema: bool = True,
    ) -> pd.DataFrame:
        symbol = normalize_symbol(symbol)
        start_period = start_date[:7] if start_date else ""
        end_period = end_date[:7] if end_date else ""
        archives = []
        for archive in self.archives:
            match = ARCHIVE_PATTERN.fullmatch(archive.path.name)
            period = match.group("period") if match else ""
            if "-" in period and (
                (start_period and period < start_period) or (end_period and period > end_period)
            ):
                continue
            archives.append(archive)
        frames = [
            archive.read_symbol(
                symbol,
                columns=columns,
                start_date=start_date,
                end_date=end_date,
                validate_schema=validate_schema,
            )
            for archive in archives
            if archive.has_symbol(symbol)
        ]
        if not frames:
            raise KeyError(f"{symbol} is not present in {[str(path) for path in self.paths]}")
        if len(frames) == 1:
            return frames[0]
        frame = pd.concat(frames, ignore_index=True)
        if "trade_time" in frame:
            frame = frame.sort_values("trade_time", kind="stable").reset_index(drop=True)
        return frame

    def close(self) -> None:
        for archive in self.archives:
            archive.close()

    def __enter__(self) -> "VendorMinuteDataset":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read one symbol from an annual vendor minute ZIP.")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--frequency", type=int, choices=FREQUENCIES, required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--output", type=Path, help="Optional .csv, .csv.gz, or .parquet output.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    archive = VendorMinuteDataset.discover(args.root, args.year, args.frequency)
    frame = archive.read_symbol(
        args.symbol, start_date=args.start_date, end_date=args.end_date,
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if args.output.suffix == ".parquet":
            frame.to_parquet(args.output, index=False)
        elif args.output.name.endswith((".csv", ".csv.gz")):
            frame.to_csv(args.output, index=False)
        else:
            raise ValueError("--output must end in .csv, .csv.gz, or .parquet")
        print(f"rows={len(frame)} archives={list(map(str, archive.paths))} output={args.output}")
        return 0
    print(frame.head(5).to_string(index=False))
    print("...")
    print(frame.tail(5).to_string(index=False))
    print(f"rows={len(frame)} columns={list(frame.columns)} archives={list(map(str, archive.paths))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
