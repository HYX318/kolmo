import tempfile
import unittest
from pathlib import Path

from kolmo.us_market.daily import (
    UniverseEntry,
    daily_path,
    load_universe,
    merge_daily_rows,
    read_daily_rows,
    validate_daily_rows,
    write_daily_rows_atomic,
)
from kolmo.us_market.drawdown import latest_drawdown_row
from kolmo.us_market.fetch_daily import request_start_date, update_one
from kolmo.us_market.tiingo import normalize_tiingo_rows


ENTRY = UniverseEntry("AAPL", "STK", "AAPL")


def provider_row(trade_date: str, close: float, *, split_factor: float = 1.0) -> dict[str, object]:
    return {
        "date": f"{trade_date}T00:00:00.000Z",
        "open": close,
        "high": close + 1,
        "low": close - 1,
        "close": close,
        "volume": 100,
        "adjOpen": close,
        "adjHigh": close + 1,
        "adjLow": close - 1,
        "adjClose": close,
        "adjVolume": 100,
        "divCash": 0,
        "splitFactor": split_factor,
    }


def normalized_rows(*payload: dict[str, object]) -> list[dict[str, str]]:
    return normalize_tiingo_rows(list(payload), ENTRY)


class UsDailyContractTest(unittest.TestCase):
    def test_canonical_path_uses_us_asset_class_and_gzip(self) -> None:
        self.assertEqual(
            daily_path(Path("/data/US"), ENTRY),
            Path("/data/US/STK/1d/AAPL.csv.gz"),
        )

    def test_universe_supports_provider_alias_and_disabled_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "universe.csv"
            path.write_text(
                "symbol,asset_type,provider_symbol,enabled\n"
                "BRK.B,STK,BRK-B,1\nSPY,ETF,SPY,0\n",
                encoding="utf-8",
            )
            self.assertEqual(load_universe(path), [UniverseEntry("BRK.B", "STK", "BRK-B")])

    def test_atomic_gzip_round_trip_and_incremental_replacement(self) -> None:
        old = normalized_rows(provider_row("2024-01-02", 100), provider_row("2024-01-03", 101))
        new = normalized_rows(provider_row("2024-01-03", 105), provider_row("2024-01-04", 106))
        merged = merge_daily_rows(old, new)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "AAPL.csv.gz"
            write_daily_rows_atomic(path, merged)
            actual = read_daily_rows(path)
        self.assertEqual([row["date"] for row in actual], ["2024-01-02", "2024-01-03", "2024-01-04"])
        self.assertEqual(actual[1]["close"], "105")

    def test_validator_rejects_bad_ohlc(self) -> None:
        rows = normalized_rows(provider_row("2024-01-02", 100))
        rows[0]["high"] = "90"
        self.assertTrue(any("raw high is inconsistent" in error for error in validate_daily_rows(rows, "AAPL", "STK")))

    def test_incremental_start_uses_overlap(self) -> None:
        rows = normalized_rows(provider_row("2024-01-20", 100))
        self.assertEqual(request_start_date(rows, "2020-01-01", 10), "2024-01-10")

    def test_failed_validation_does_not_replace_existing_file(self) -> None:
        existing = normalized_rows(provider_row("2024-01-02", 100))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "US"
            path = daily_path(root, ENTRY)
            write_daily_rows_atomic(path, existing)
            before = path.read_bytes()

            def bad_fetch(entry: UniverseEntry, token: str, start: str, end: str) -> list[dict[str, str]]:
                rows = normalized_rows(provider_row("2024-01-03", 101))
                rows[0]["low"] = "200"
                return rows

            with self.assertRaisesRegex(ValueError, "validation failed"):
                update_one(ENTRY, root, "secret", "2020-01-01", "2024-01-03", 10, False, bad_fetch)
            self.assertEqual(path.read_bytes(), before)

    def test_new_corporate_action_triggers_full_adjusted_history_refresh(self) -> None:
        existing = normalized_rows(provider_row("2024-01-02", 100))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "US"
            path = daily_path(root, ENTRY)
            write_daily_rows_atomic(path, existing)
            starts: list[str] = []

            def fetch(entry: UniverseEntry, token: str, start: str, end: str) -> list[dict[str, str]]:
                starts.append(start)
                dividend = provider_row("2024-01-10", 90)
                dividend["divCash"] = 1
                if start == "2020-01-01":
                    history = provider_row("2024-01-02", 100)
                    history["adjClose"] = 99
                    return normalized_rows(history, dividend)
                return normalized_rows(dividend)

            update_one(ENTRY, root, "secret", "2020-01-01", "2024-01-10", 10, False, fetch)
            actual = read_daily_rows(path)
            self.assertEqual(starts, ["2023-12-23", "2020-01-01"])
            self.assertEqual(actual[0]["adj_close"], "99")

    def test_split_adjusted_drawdown_ignores_mechanical_split(self) -> None:
        rows = normalized_rows(
            provider_row("2020-01-02", 400),
            provider_row("2020-08-31", 100, split_factor=4),
            provider_row("2020-09-01", 80),
        )
        result = latest_drawdown_row(ENTRY, rows)
        self.assertAlmostEqual(float(result["ath"]), 100.0)
        self.assertAlmostEqual(float(result["drawdown_from_ath"]), -0.2)
        self.assertEqual(result["sessions_since_ath"], "1")


if __name__ == "__main__":
    unittest.main()
