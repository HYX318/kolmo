"""Canonical A-share minute-bar contract and deterministic aggregation."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pyarrow as pa


SCHEMA_VERSION = "1.0.0"
PRODUCT_ID = "ashare_minute_bars"
FREQUENCIES = (1, 5, 15, 30, 60)
EXPECTED_BARS = {1: 241, 5: 49, 15: 17, 30: 9, 60: 5}
MINUTE_BAR_COLUMNS = (
    "symbol", "trade_time", "open", "high", "low", "close", "volume", "amount",
)
MINUTE_BAR_SCHEMA = pa.schema([
    pa.field("symbol", pa.string(), nullable=False),
    pa.field("trade_time", pa.timestamp("ms", tz="Asia/Shanghai"), nullable=False),
    pa.field("open", pa.float64(), nullable=False),
    pa.field("high", pa.float64(), nullable=False),
    pa.field("low", pa.float64(), nullable=False),
    pa.field("close", pa.float64(), nullable=False),
    pa.field("volume", pa.int64(), nullable=False),
    pa.field("amount", pa.float64(), nullable=False),
])


def expected_minute_of_day(frequency: int, exchange: str = "") -> set[int]:
    if frequency not in FREQUENCIES:
        raise ValueError(f"unsupported frequency: {frequency}")
    times = {9 * 60 + 30}
    times.update(range(9 * 60 + 30 + frequency, 11 * 60 + 30 + 1, frequency))
    times.update(range(13 * 60 + frequency, 15 * 60 + 1, frequency))
    if exchange.lower() == "bj":
        if frequency == 1:
            times.update(range(15 * 60 + 1, 15 * 60 + 30 + 1))
        elif frequency < 60:
            times.update(range(15 * 60 + frequency, 15 * 60 + 30 + 1, frequency))
        else:
            times.add(15 * 60 + 30)
    return times


def expected_bars_per_day(frequency: int, exchange: str = "") -> int:
    return len(expected_minute_of_day(frequency, exchange))


def bucket_1m_trade_time(values: pd.Series, frequency: int) -> pd.Series:
    """Map 1-minute close labels to session-aware N-minute close labels."""
    if frequency not in FREQUENCIES[1:]:
        raise ValueError(f"unsupported derived frequency: {frequency}")
    minute = (values.dt.hour * 60 + values.dt.minute).astype("int64")
    bucket = minute.copy()
    morning = (minute > 570) & (minute <= 690)
    afternoon = (minute > 780) & (minute <= 900)
    beijing_post_close = (minute > 900) & (minute <= 930)
    bucket.loc[morning] = 570 + ((minute.loc[morning] - 570 + frequency - 1) // frequency) * frequency
    bucket.loc[afternoon] = 780 + ((minute.loc[afternoon] - 780 + frequency - 1) // frequency) * frequency
    bucket.loc[beijing_post_close] = (
        900 + ((minute.loc[beijing_post_close] - 900 + frequency - 1) // frequency) * frequency
    ).clip(upper=930)
    return values.dt.normalize() + pd.to_timedelta(bucket, unit="m")


def aggregate_bars(frame: pd.DataFrame, frequency: int) -> pd.DataFrame:
    """Derive one higher frequency from canonical 1-minute bars."""
    # Canonical partitions are already sorted by (symbol, trade_time). Keeping
    # that order makes the bucket key monotonic and avoids four full re-sorts.
    work = frame.copy()
    work["trade_time"] = bucket_1m_trade_time(work["trade_time"], frequency)
    result = work.groupby(["symbol", "trade_time"], as_index=False, sort=False).agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"),
        close=("close", "last"), volume=("volume", "sum"), amount=("amount", "sum"),
    )
    return result.loc[:, MINUTE_BAR_COLUMNS]


def normalize_vendor_bars(frame: pd.DataFrame, symbol: str, frequency: int = 1) -> pd.DataFrame:
    """Map the vendor schema to the canonical contract without repairing values."""
    required = {"ts_code", "freq", "trade_time", "open", "high", "low", "close", "vol", "amount"}
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(f"vendor minute frame is missing columns: {missing}")
    symbol = symbol.upper()
    if frame["ts_code"].ne(symbol).any():
        raise ValueError(f"vendor symbol does not match member name: {symbol}")
    if frame["freq"].ne(f"{frequency}min").any():
        raise ValueError(f"vendor frequency does not match requested {frequency}min: {symbol}")
    result = frame.rename(columns={"ts_code": "symbol", "vol": "volume"})
    result = result.loc[:, MINUTE_BAR_COLUMNS].copy()
    result["symbol"] = result["symbol"].astype("string")
    result["volume"] = pd.to_numeric(result["volume"], errors="raise").astype("int64")
    for column in ("open", "high", "low", "close", "amount"):
        result[column] = pd.to_numeric(result[column], errors="raise").astype("float64")
    if str(result["trade_time"].dt.tz) != "Asia/Shanghai":
        raise ValueError(f"vendor timezone is not Asia/Shanghai: {symbol}")
    return result.sort_values("trade_time", kind="stable").reset_index(drop=True)


def validate_canonical_bars(frame: pd.DataFrame, frequency: int) -> None:
    missing = sorted(set(MINUTE_BAR_COLUMNS).difference(frame.columns))
    if missing:
        raise ValueError(f"canonical minute frame is missing columns: {missing}")
    if frame.loc[:, MINUTE_BAR_COLUMNS].isna().any(axis=None):
        raise ValueError("canonical minute frame contains null values")
    if frame.duplicated(["symbol", "trade_time"]).any():
        raise ValueError("canonical minute frame contains duplicate primary keys")
    invalid_ohlc = (
        frame["low"].gt(frame[["open", "close"]].min(axis=1))
        | frame["high"].lt(frame[["open", "close"]].max(axis=1))
        | frame["low"].gt(frame["high"])
        | frame[["open", "high", "low", "close"]].le(0).any(axis=1)
    )
    if invalid_ohlc.any():
        columns = ["symbol", "trade_time", "open", "high", "low", "close"]
        sample = frame.loc[invalid_ohlc, columns].head(5).to_dict("records")
        raise ValueError(
            f"canonical minute frame contains {int(invalid_ohlc.sum())} invalid OHLC rows; "
            f"sample={sample}"
        )
    minute = frame["trade_time"].dt.hour * 60 + frame["trade_time"].dt.minute
    is_beijing = frame["symbol"].str.endswith(".BJ")
    post_close_adjustment = is_beijing & minute.between(901, 930)
    invalid_negative = (frame["volume"].lt(0) | frame["amount"].lt(0)) & ~post_close_adjustment
    if invalid_negative.any():
        raise ValueError("canonical minute frame contains negative volume or amount")
    valid_time = (
        (~is_beijing & minute.isin(expected_minute_of_day(frequency)))
        | (is_beijing & minute.isin(expected_minute_of_day(frequency, "bj")))
    )
    invalid_time = ~valid_time | frame["trade_time"].dt.second.ne(0)
    if invalid_time.any():
        raise ValueError(f"canonical minute frame contains {int(invalid_time.sum())} invalid timestamps")


def partition_path(root: Path, frequency: int, exchange: str, trade_date: str) -> Path:
    compact = trade_date.replace("-", "")
    return root / f"{frequency}m" / "raw" / exchange.lower() / compact[:4] / compact[4:6] / f"{compact}.parquet"


def parquet_metadata(*, frequency: int, exchange: str, trade_date: str, source: str, build_id: str) -> dict[bytes, bytes]:
    values = {
        "kolmo.schema_version": SCHEMA_VERSION,
        "kolmo.product_id": PRODUCT_ID,
        "kolmo.frequency_minutes": str(frequency),
        "kolmo.trade_date": trade_date,
        "kolmo.exchange": exchange.upper(),
        "kolmo.adjustment": "raw",
        "kolmo.timezone": "Asia/Shanghai",
        "kolmo.bar_label": "end",
        "kolmo.volume_unit": "share",
        "kolmo.amount_unit": "CNY",
        "kolmo.source": source,
        "kolmo.build_id": build_id,
    }
    return {key.encode(): value.encode() for key, value in values.items()}
