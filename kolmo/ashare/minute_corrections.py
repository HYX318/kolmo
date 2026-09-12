"""Versioned, evidence-backed corrections for known vendor minute defects."""

from __future__ import annotations

from typing import Final

import pandas as pd


CORRECTION_POLICY_ID: Final = "vendor-minute-corrections-v3"
EXCLUSION_POLICY_ID: Final = "vendor-zero-price-day-exclusions-v1"
OBSERVATION_POLICY_ID: Final = "vendor-post-close-adjustments-v1"

# Each correction is deliberately exact: symbol, timestamp, field, and the
# observed bad value must all match. Corrected lows were checked against the
# vendor's independently delivered 5-minute bars. Unknown defects still fail
# canonical validation instead of being guessed here.
_LOW_CORRECTIONS: Final[dict[tuple[str, str], float]] = {
    ("601005.SH", "2011-04-26 10:31:00+08:00"): 4.87,
    ("600356.SH", "2011-12-12 09:32:00+08:00"): 7.07,
    ("600358.SH", "2011-12-12 09:33:00+08:00"): 4.64,
    ("600367.SH", "2011-12-12 09:34:00+08:00"): 15.90,
    ("600579.SH", "2011-12-12 10:31:00+08:00"): 5.62,
    ("600377.SH", "2011-12-12 09:35:00+08:00"): 5.82,
    ("600511.SH", "2011-12-12 09:34:00+08:00"): 14.04,
    ("600897.SH", "2011-12-12 09:32:00+08:00"): 13.55,
    ("600988.SH", "2011-12-12 09:34:00+08:00"): 9.35,
    ("600337.SH", "2011-12-12 09:32:00+08:00"): 9.16,
    ("600689.SH", "2011-12-15 09:32:00+08:00"): 8.01,
    ("600228.SH", "2011-12-21 10:31:00+08:00"): 8.21,
}

_BY_SYMBOL: Final[dict[str, tuple[tuple[str, float], ...]]] = {
    symbol: tuple(
        (timestamp, corrected)
        for (candidate, timestamp), corrected in _LOW_CORRECTIONS.items()
        if candidate == symbol
    )
    for symbol in {key[0] for key in _LOW_CORRECTIONS}
}


def apply_vendor_corrections(
    frame: pd.DataFrame,
    symbol: str,
) -> tuple[pd.DataFrame, list[dict[str, object]]]:
    """Apply exact corrections and narrow, auditable structural inference."""
    candidates = _BY_SYMBOL.get(symbol.upper(), ())
    if frame.empty:
        return frame, []
    result = frame.copy()
    applied: list[dict[str, object]] = []
    for timestamp_text, corrected_value in candidates:
        timestamp = pd.Timestamp(timestamp_text)
        matches = result["trade_time"].eq(timestamp)
        count = int(matches.sum())
        if count == 0:
            continue
        if count != 1:
            raise ValueError(f"correction key is not unique: {symbol} {timestamp_text}")
        observed = float(result.loc[matches, "low"].iloc[0])
        if observed != 0.0:
            raise ValueError(
                f"correction precondition changed: {symbol} {timestamp_text} "
                f"low expected 0.0, observed {observed}"
            )
        result.loc[matches, "low"] = corrected_value
        applied.append({
            "symbol": symbol.upper(),
            "trade_time": timestamp.isoformat(),
            "field": "low",
            "original_value": observed,
            "corrected_value": corrected_value,
            "evidence": "vendor_5m_ohlc_crosscheck",
            "inferred": False,
        })

    # Some vendor years contain otherwise coherent bars whose low alone is
    # zero. The exact intraminute low cannot always be reconstructed from the
    # independent 5m delivery. Use the tightest value guaranteed by the local
    # OHLC contract and mark it as inferred; any broader invalid pattern is
    # deliberately left untouched so canonical validation still blocks it.
    zero_low = result["low"].eq(0.0)
    eligible = (
        zero_low
        & result[["open", "high", "close"]].gt(0).all(axis=1)
        & result["high"].ge(result[["open", "close"]].max(axis=1))
    )
    for index in result.index[eligible]:
        timestamp = result.at[index, "trade_time"]
        corrected_value = float(min(result.at[index, "open"], result.at[index, "close"]))
        result.at[index, "low"] = corrected_value
        applied.append({
            "symbol": symbol.upper(),
            "trade_time": timestamp.isoformat(),
            "field": "low",
            "original_value": 0.0,
            "corrected_value": corrected_value,
            "evidence": "structural_ohlc_lower_bound",
            "inferred": True,
        })

    positive = result[["open", "high", "low", "close"]].gt(0).all(axis=1)
    local_low = result[["open", "close"]].min(axis=1)
    low_outside_envelope = positive & result["low"].gt(local_low)
    for index in result.index[low_outside_envelope]:
        timestamp = result.at[index, "trade_time"]
        observed = float(result.at[index, "low"])
        corrected_value = float(local_low.at[index])
        result.at[index, "low"] = corrected_value
        applied.append({
            "symbol": symbol.upper(),
            "trade_time": timestamp.isoformat(),
            "field": "low",
            "original_value": observed,
            "corrected_value": corrected_value,
            "evidence": "structural_ohlc_envelope",
            "inferred": True,
        })

    local_high = result[["open", "close"]].max(axis=1)
    high_outside_envelope = positive & result["high"].lt(local_high)
    for index in result.index[high_outside_envelope]:
        timestamp = result.at[index, "trade_time"]
        observed = float(result.at[index, "high"])
        corrected_value = float(local_high.at[index])
        result.at[index, "high"] = corrected_value
        applied.append({
            "symbol": symbol.upper(),
            "trade_time": timestamp.isoformat(),
            "field": "high",
            "original_value": observed,
            "corrected_value": corrected_value,
            "evidence": "structural_ohlc_envelope",
            "inferred": True,
        })

    # A-share intraminute highs cannot plausibly exceed both open and close by
    # this magnitude. Keep the threshold deliberately broad so ordinary limit
    # moves and listing-day volatility are untouched.
    extreme_high = positive & result["high"].gt(local_high * 2.0)
    for index in result.index[extreme_high]:
        timestamp = result.at[index, "trade_time"]
        observed = float(result.at[index, "high"])
        corrected_value = float(local_high.at[index])
        result.at[index, "high"] = corrected_value
        applied.append({
            "symbol": symbol.upper(),
            "trade_time": timestamp.isoformat(),
            "field": "high",
            "original_value": observed,
            "corrected_value": corrected_value,
            "evidence": "extreme_intraminute_spike_guard",
            "inferred": True,
        })
    return result, applied


def exclude_zero_price_activity_days(
    frame: pd.DataFrame,
    symbol: str,
) -> tuple[pd.DataFrame, list[dict[str, object]]]:
    """Exclude only complete dates made entirely of zero-price/zero-activity rows."""
    if frame.empty:
        return frame, []
    zero_row = (
        frame[["open", "high", "low", "close"]].eq(0).all(axis=1)
        & frame["volume"].eq(0)
        & frame["amount"].eq(0)
    )
    if not zero_row.any():
        return frame, []
    dates = frame["trade_time"].dt.strftime("%Y-%m-%d")
    excluded = pd.Series(False, index=frame.index)
    excluded_dates: list[str] = []
    for trade_date, indexes in frame.groupby(dates, sort=True).groups.items():
        if bool(zero_row.loc[indexes].all()):
            excluded.loc[indexes] = True
            excluded_dates.append(str(trade_date))
    if not excluded_dates:
        return frame, []
    record = {
        "symbol": symbol.upper(),
        "dates": excluded_dates,
        "excluded_rows": int(excluded.sum()),
        "reason": "complete_zero_price_zero_activity_day",
    }
    return frame.loc[~excluded].reset_index(drop=True), [record]


def observe_post_close_adjustments(
    frame: pd.DataFrame,
    symbol: str,
) -> list[dict[str, object]]:
    """Record retained negative BJ post-close volume/amount adjustments."""
    if frame.empty or not symbol.upper().endswith(".BJ"):
        return []
    minute = frame["trade_time"].dt.hour * 60 + frame["trade_time"].dt.minute
    mask = (frame["volume"].lt(0) | frame["amount"].lt(0)) & minute.between(901, 930)
    records: list[dict[str, object]] = []
    for row in frame.loc[mask].itertuples(index=False):
        records.append({
            "symbol": symbol.upper(),
            "trade_time": row.trade_time.isoformat(),
            "volume": int(row.volume),
            "amount": float(row.amount),
            "reason": "vendor_post_close_net_adjustment",
        })
    return records
