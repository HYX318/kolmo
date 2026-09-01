"""Fast, cached access to canonical A-share and US daily bar files."""

from __future__ import annotations

import csv
import gzip
import re
import time
from dataclasses import asdict, dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Sequence


CN_SYMBOL_RE = re.compile(r"^\d{6}\.(SZ|SH)$")
US_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,19}$")
INTERVALS = {"1d", "5d", "1w", "1mo"}
PRICE_MODES = {"adjusted", "raw"}


@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    market: str
    asset_type: str
    sector: str = ""


@dataclass(frozen=True)
class SourceBar:
    date: str
    raw_open: float
    raw_high: float
    raw_low: float
    raw_close: float
    raw_volume: float
    adjusted_open: float
    adjusted_high: float
    adjusted_low: float
    adjusted_close: float
    adjusted_volume: float
    amount: float | None


@dataclass(frozen=True)
class Bar:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float | None


def _float(value: object, default: float | None = None) -> float | None:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed == parsed else default


def _iso_date(value: str) -> str:
    text = value.strip()[:10]
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    date.fromisoformat(text)
    return text


def _valid_ohlc(values: Sequence[float | None]) -> bool:
    return all(value is not None and value > 0 for value in values)


@lru_cache(maxsize=128)
def _read_source_bars_cached(
    path_text: str, modified_ns: int, size: int, market: str
) -> tuple[SourceBar, ...]:
    del modified_ns, size
    path = Path(path_text)
    bars: list[SourceBar] = []
    with gzip.open(path, "rt", encoding="utf-8", newline="") as source:
        for row in csv.DictReader(source):
            if market == "CN" and row.get("tradestatus") != "1":
                continue
            raw = [_float(row.get(field)) for field in ("open", "high", "low", "close")]
            if not _valid_ohlc(raw):
                continue
            if market == "US":
                adjusted = [
                    _float(row.get(field))
                    for field in ("adj_open", "adj_high", "adj_low", "adj_close")
                ]
                if not _valid_ohlc(adjusted):
                    continue
                adjusted_volume = _float(row.get("adj_volume"), 0.0) or 0.0
            else:
                adjusted = raw
                adjusted_volume = _float(row.get("volume"), 0.0) or 0.0
            bars.append(
                SourceBar(
                    date=_iso_date(row.get("date", "")),
                    raw_open=float(raw[0]),
                    raw_high=float(raw[1]),
                    raw_low=float(raw[2]),
                    raw_close=float(raw[3]),
                    raw_volume=_float(row.get("volume"), 0.0) or 0.0,
                    adjusted_open=float(adjusted[0]),
                    adjusted_high=float(adjusted[1]),
                    adjusted_low=float(adjusted[2]),
                    adjusted_close=float(adjusted[3]),
                    adjusted_volume=adjusted_volume,
                    amount=_float(row.get("amount")),
                )
            )
    return tuple(bars)


def read_source_bars(path: Path, market: str) -> tuple[SourceBar, ...]:
    stat = path.stat()
    return _read_source_bars_cached(str(path), stat.st_mtime_ns, stat.st_size, market)


def select_price(bars: Iterable[SourceBar], price_mode: str) -> list[Bar]:
    adjusted = price_mode == "adjusted"
    return [
        Bar(
            date=bar.date,
            open=bar.adjusted_open if adjusted else bar.raw_open,
            high=bar.adjusted_high if adjusted else bar.raw_high,
            low=bar.adjusted_low if adjusted else bar.raw_low,
            close=bar.adjusted_close if adjusted else bar.raw_close,
            volume=bar.adjusted_volume if adjusted else bar.raw_volume,
            amount=bar.amount,
        )
        for bar in bars
    ]


def _bucket_key(bar: Bar, interval: str, index: int) -> object:
    current = date.fromisoformat(bar.date)
    if interval == "5d":
        return index // 5
    if interval == "1w":
        iso = current.isocalendar()
        return iso.year, iso.week
    if interval == "1mo":
        return current.year, current.month
    return index


def aggregate_bars(bars: Sequence[Bar], interval: str) -> list[Bar]:
    if interval == "1d":
        return list(bars)
    grouped: list[list[Bar]] = []
    previous_key: object = object()
    for index, bar in enumerate(bars):
        key = _bucket_key(bar, interval, index)
        if key != previous_key:
            grouped.append([])
            previous_key = key
        grouped[-1].append(bar)
    output: list[Bar] = []
    for group in grouped:
        amounts = [bar.amount for bar in group if bar.amount is not None]
        output.append(
            Bar(
                date=group[-1].date,
                open=group[0].open,
                high=max(bar.high for bar in group),
                low=min(bar.low for bar in group),
                close=group[-1].close,
                volume=sum(bar.volume for bar in group),
                amount=sum(amounts) if amounts else None,
            )
        )
    return output


def filter_bars(bars: Sequence[Bar], start: str, end: str, limit: int) -> list[Bar]:
    selected = [
        bar for bar in bars
        if (not start or bar.date >= start) and (not end or bar.date <= end)
    ]
    return selected[-limit:] if limit else selected


class MarketStore:
    def __init__(self, root: Path, config_root: Path):
        self.root = root
        self.config_root = config_root
        self._instruments: dict[str, Instrument] | None = None

    def _us_metadata(self) -> dict[str, Instrument]:
        output: dict[str, Instrument] = {}
        path = self.config_root / "configs" / "us_value_universe.csv"
        if not path.is_file():
            return output
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            for row in csv.DictReader(source):
                symbol = row.get("symbol", "").upper()
                if symbol:
                    output[symbol] = Instrument(
                        symbol=symbol,
                        name=row.get("company", ""),
                        market="US",
                        asset_type=row.get("asset_type", "STK"),
                        sector=row.get("sector", ""),
                    )
        return output

    def _cn_names(self) -> dict[str, str]:
        base = self.root / "reference" / "ashare" / "security_master" / "observed"
        paths = sorted(base.glob("*/*/*.csv"))
        if not paths:
            return {}
        with paths[-1].open("r", encoding="utf-8", newline="") as source:
            return {
                row.get("symbol", "").upper(): row.get("name", "")
                for row in csv.DictReader(source)
                if row.get("symbol")
            }

    def instruments(self) -> dict[str, Instrument]:
        if self._instruments is not None:
            return self._instruments
        output = self._us_metadata()
        for asset_type in ("STK", "ETF"):
            for path in (self.root / "US" / asset_type / "1d").glob("*.csv.gz"):
                symbol = path.name.removesuffix(".csv.gz").upper()
                output.setdefault(symbol, Instrument(symbol, symbol, "US", asset_type))
        names = self._cn_names()
        for exchange in ("sz", "sh"):
            base = self.root / "raw" / "ashare" / "baostock" / exchange / "daily" / "qfq"
            for path in base.glob("*.csv.gz"):
                symbol = path.name.removesuffix(".csv.gz").upper()
                output[symbol] = Instrument(symbol, names.get(symbol, symbol), "CN", "STK")
        self._instruments = output
        return output

    def search(self, query: str, market: str, limit: int = 30) -> list[dict[str, object]]:
        text = query.strip().upper()
        candidates = [
            instrument for instrument in self.instruments().values()
            if market in {"", "ALL"} or instrument.market == market
        ]
        if text:
            candidates = [
                item for item in candidates
                if text in item.symbol or text in item.name.upper()
            ]
            candidates.sort(
                key=lambda item: (
                    not item.symbol.startswith(text),
                    not item.name.upper().startswith(text),
                    item.symbol,
                )
            )
        else:
            candidates.sort(key=lambda item: (item.market != "US", item.symbol))
        output = []
        for item in candidates[:limit]:
            record = asdict(item)
            record["has_fundamentals"] = (
                item.market == "US"
                and item.asset_type == "STK"
                and (self.root / "fundamental" / "us" / "sec" / "company_facts" / f"{item.symbol}.csv.gz").is_file()
                and (self.root / "fundamental" / "us" / "sec" / "filings" / f"{item.symbol}.csv.gz").is_file()
            )
            output.append(record)
        return output

    def _path(self, instrument: Instrument) -> Path:
        if instrument.market == "US":
            return self.root / "US" / instrument.asset_type / "1d" / f"{instrument.symbol}.csv.gz"
        exchange = "sh" if instrument.symbol.endswith(".SH") else "sz"
        return (
            self.root / "raw" / "ashare" / "baostock" / exchange
            / "daily" / "qfq" / f"{instrument.symbol}.csv.gz"
        )

    def bars(
        self,
        symbol: str,
        interval: str,
        price_mode: str,
        start: str = "",
        end: str = "",
        limit: int = 0,
    ) -> dict[str, object]:
        started = time.perf_counter()
        normalized = symbol.strip().upper()
        if not (CN_SYMBOL_RE.fullmatch(normalized) or US_SYMBOL_RE.fullmatch(normalized)):
            raise ValueError("invalid symbol")
        if interval not in INTERVALS:
            raise ValueError(f"interval must be one of {sorted(INTERVALS)}")
        if price_mode not in PRICE_MODES:
            raise ValueError(f"price must be one of {sorted(PRICE_MODES)}")
        instrument = self.instruments().get(normalized)
        if instrument is None:
            raise FileNotFoundError(f"symbol is not available locally: {normalized}")
        path = self._path(instrument)
        if not path.is_file():
            raise FileNotFoundError(f"daily cache not found: {path}")
        source_bars = read_source_bars(path, instrument.market)
        selected_bars = select_price(source_bars, price_mode)
        aggregated = aggregate_bars(selected_bars, interval)
        visible = filter_bars(aggregated, start, end, limit)
        if not visible:
            raise FileNotFoundError(f"no bars for requested range: {normalized}")
        quote_bars = filter_bars(selected_bars, start, end, 0)
        latest = quote_bars[-1]
        previous = quote_bars[-2] if len(quote_bars) > 1 else latest
        trailing = quote_bars[-252:]
        peak = max(bar.high for bar in trailing)
        quote = {
            "date": latest.date,
            "open": latest.open,
            "high": latest.high,
            "low": latest.low,
            "close": latest.close,
            "volume": latest.volume,
            "change": latest.close - previous.close,
            "change_pct": latest.close / previous.close - 1.0 if previous.close else 0.0,
            "high_52w": peak,
            "drawdown_52w": latest.close / peak - 1.0 if peak else 0.0,
        }
        return {
            "instrument": asdict(instrument),
            "interval": interval,
            "price_mode": price_mode,
            "bars": [asdict(bar) for bar in visible],
            "quote": quote,
            "meta": {
                "source_rows": len(source_bars),
                "rows": len(visible),
                "first_date": visible[0].date,
                "last_date": visible[-1].date,
                "load_ms": round((time.perf_counter() - started) * 1000, 3),
            },
        }
