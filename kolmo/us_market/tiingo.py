"""Small Tiingo EOD adapter with bounded retry behavior."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from kolmo.us_market.daily import DAILY_BAR_COLUMNS, UniverseEntry, normalize_iso_date


TIINGO_EOD_URL = "https://api.tiingo.com/tiingo/daily/{symbol}/prices"


class TiingoError(RuntimeError):
    """A provider response could not be fetched or normalized."""


def _number(value: object, field: str) -> str:
    if isinstance(value, bool) or value is None:
        raise TiingoError(f"missing numeric field: {field}")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return format(value, ".15g")
    text = str(value).strip()
    if not text:
        raise TiingoError(f"missing numeric field: {field}")
    float(text)
    return text


def normalize_tiingo_rows(payload: object, entry: UniverseEntry) -> list[dict[str, str]]:
    if not isinstance(payload, list):
        raise TiingoError("Tiingo response must be a JSON array")
    mapping = {
        "open": "open", "high": "high", "low": "low", "close": "close",
        "volume": "volume", "adjOpen": "adj_open", "adjHigh": "adj_high",
        "adjLow": "adj_low", "adjClose": "adj_close", "adjVolume": "adj_volume",
        "divCash": "div_cash", "splitFactor": "split_factor",
    }
    rows: list[dict[str, str]] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise TiingoError(f"Tiingo row {index} must be an object")
        try:
            trade_date = normalize_iso_date(str(item["date"]))
        except (KeyError, ValueError) as exc:
            raise TiingoError(f"Tiingo row {index} has an invalid date") from exc
        row = {
            "date": trade_date,
            "symbol": entry.symbol,
            "asset_type": entry.asset_type,
            "source": "tiingo.eod",
        }
        for source_field, output_field in mapping.items():
            row[output_field] = _number(item.get(source_field), source_field)
        rows.append({column: row[column] for column in DAILY_BAR_COLUMNS})
    rows.sort(key=lambda row: row["date"])
    return rows


def fetch_tiingo_daily(
    entry: UniverseEntry,
    token: str,
    start_date: str,
    end_date: str,
    *,
    timeout: float = 30.0,
    retries: int = 3,
    opener: Callable[..., object] = urllib.request.urlopen,
) -> list[dict[str, str]]:
    query = urllib.parse.urlencode({"startDate": start_date, "endDate": end_date})
    provider_symbol = urllib.parse.quote(entry.provider_symbol, safe="-")
    url = f"{TIINGO_EOD_URL.format(symbol=provider_symbol)}?{query}"
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Token {token}", "Accept": "application/json"},
    )
    for attempt in range(retries + 1):
        try:
            with opener(request, timeout=timeout) as response:  # type: ignore[attr-defined]
                payload = json.loads(response.read().decode("utf-8"))
            return normalize_tiingo_rows(payload, entry)
        except urllib.error.HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if not retryable or attempt == retries:
                raise TiingoError(f"Tiingo HTTP {exc.code} for {entry.symbol}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt == retries:
                raise TiingoError(f"Tiingo request failed for {entry.symbol}: {exc}") from exc
        time.sleep(min(2**attempt, 8))
    raise AssertionError("unreachable")

