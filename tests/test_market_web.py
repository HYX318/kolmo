import csv
import gzip
import tempfile
import unittest
from pathlib import Path

from kolmo.web.market_data import Bar, MarketStore, aggregate_bars


def bar(day: str, value: float, volume: float = 10.0) -> Bar:
    return Bar(day, value, value + 2, value - 1, value + 1, volume, volume * 100)


class BarAggregationTest(unittest.TestCase):
    def test_five_session_bar_preserves_ohlcv_semantics(self) -> None:
        source = [bar(f"2024-01-{day:02d}", 100 + day, day) for day in range(1, 7)]
        result = aggregate_bars(source, "5d")
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].date, "2024-01-05")
        self.assertEqual(result[0].open, 101)
        self.assertEqual(result[0].high, 107)
        self.assertEqual(result[0].low, 100)
        self.assertEqual(result[0].close, 106)
        self.assertEqual(result[0].volume, 15)

    def test_week_and_month_buckets_use_calendar_boundaries(self) -> None:
        source = [
            bar("2024-01-05", 100),
            bar("2024-01-08", 102),
            bar("2024-01-31", 104),
            bar("2024-02-01", 106),
        ]
        self.assertEqual(
            [item.date for item in aggregate_bars(source, "1w")],
            ["2024-01-05", "2024-01-08", "2024-02-01"],
        )
        self.assertEqual(
            [item.date for item in aggregate_bars(source, "1mo")],
            ["2024-01-31", "2024-02-01"],
        )


class MarketStoreTest(unittest.TestCase):
    def test_us_file_search_and_adjusted_bar_loading(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            data_root = base / "data"
            config_root = base / "project"
            config = config_root / "configs" / "us_value_universe.csv"
            config.parent.mkdir(parents=True)
            config.write_text(
                "symbol,asset_type,company,sector\nAAPL,STK,Apple,Technology\n",
                encoding="utf-8",
            )
            path = data_root / "US" / "STK" / "1d" / "AAPL.csv.gz"
            path.parent.mkdir(parents=True)
            fields = [
                "date", "open", "high", "low", "close", "volume",
                "adj_open", "adj_high", "adj_low", "adj_close", "adj_volume",
            ]
            with gzip.open(path, "wt", encoding="utf-8", newline="") as output:
                writer = csv.DictWriter(output, fieldnames=fields)
                writer.writeheader()
                writer.writerow({
                    "date": "2024-01-02", "open": "100", "high": "102", "low": "99",
                    "close": "101", "volume": "10", "adj_open": "50", "adj_high": "51",
                    "adj_low": "49.5", "adj_close": "50.5", "adj_volume": "20",
                })
                writer.writerow({
                    "date": "2024-01-03", "open": "102", "high": "104", "low": "101",
                    "close": "103", "volume": "12", "adj_open": "51", "adj_high": "52",
                    "adj_low": "50.5", "adj_close": "51.5", "adj_volume": "24",
                })

            store = MarketStore(data_root, config_root)
            self.assertEqual(store.search("app", "US")[0]["symbol"], "AAPL")
            payload = store.bars("AAPL", "1d", "adjusted")
            self.assertEqual(payload["bars"][0]["open"], 50.0)
            self.assertEqual(payload["quote"]["change_pct"], 51.5 / 50.5 - 1.0)


if __name__ == "__main__":
    unittest.main()
