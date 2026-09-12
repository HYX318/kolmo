import argparse
import csv
import gzip
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kolmo.ashare.fetch_baostock_5min import fetch_symbol
from kolmo.ashare.fetch_baostock_daily import StockInfo
from kolmo.ashare import partition_baostock_5min as partition


STOCK = StockInfo(
    code="600519",
    bs_code="sh.600519",
    symbol="600519.SH",
    name="Kweichow Moutai",
    listing_date="2001-08-27",
    delisting_date="",
    status="active",
)


class FakeResult:
    error_code = "0"
    error_msg = ""
    fields = [
        "date", "time", "code", "open", "high", "low", "close",
        "volume", "amount", "adjustflag",
    ]

    def __init__(self, rows: list[list[str]]) -> None:
        self.rows = iter(rows)
        self.current: list[str] = []

    def next(self) -> bool:
        try:
            self.current = next(self.rows)
            return True
        except StopIteration:
            return False

    def get_row_data(self) -> list[str]:
        return self.current


class FakeBaoStock:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def query_history_k_data_plus(self, code: str, fields: str, **kwargs):
        self.calls.append({"code": code, "fields": fields, **kwargs})
        date = kwargs["start_date"]
        compact = date.replace("-", "")
        return FakeResult([[
            date, f"{compact}093500000", code, "10", "11", "9", "10.5",
            "1000", "10500", kwargs["adjustflag"],
        ]])


class EmptyBaoStock(FakeBaoStock):
    def query_history_k_data_plus(self, code: str, fields: str, **kwargs):
        self.calls.append({"code": code, "fields": fields, **kwargs})
        return FakeResult([])


def args(**overrides) -> argparse.Namespace:
    values = {
        "adjust": "raw", "resume": True, "start_date": "2026-09-01",
        "end_date": "2026-09-01", "overlap_days": 5, "chunk_days": 366,
        "max_rows_per_chunk": 30_000, "timeout_seconds": 1.0, "retries": 0,
        "retry_backoff_seconds": 0.0, "sleep": 0.0,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class BaoStockFiveMinuteFetchTest(unittest.TestCase):
    def test_fetch_uses_five_minute_unadjusted_query_and_writes_symbol_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider = FakeBaoStock()
            result = fetch_symbol(provider, STOCK, args(), root)

            self.assertEqual(result.state, "fetched")
            self.assertEqual(provider.calls[0]["frequency"], "5")
            self.assertEqual(provider.calls[0]["adjustflag"], "3")
            self.assertEqual(result.path, root / "raw" / "600519.SH.csv.gz")
            with gzip.open(result.path, "rt", encoding="utf-8", newline="") as source:
                rows = list(csv.DictReader(source))
            self.assertEqual(rows[0]["time"], "20260901093500000")

    def test_complete_cache_is_reused_without_provider_query(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider = FakeBaoStock()
            fetch_symbol(provider, STOCK, args(), root)
            provider.calls.clear()
            result = fetch_symbol(provider, STOCK, args(), root)
            self.assertEqual(result.state, "cached")
            self.assertEqual(provider.calls, [])

    def test_successful_empty_result_records_coverage_and_is_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider = EmptyBaoStock()
            result = fetch_symbol(provider, STOCK, args(), root)
            self.assertEqual(result.state, "empty")
            self.assertEqual(result.rows, 0)
            self.assertTrue(result.path.with_name(f"{result.path.name}.coverage.json").is_file())
            provider.calls.clear()
            result = fetch_symbol(provider, STOCK, args(), root)
            self.assertEqual(result.state, "cached")
            self.assertEqual(provider.calls, [])


class BaoStockFiveMinutePartitionTest(unittest.TestCase):
    def test_partitions_multiple_symbols_by_day_as_gzip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / "input"
            output_dir = root / "profile"
            input_dir.mkdir()
            fields = FakeResult.fields
            for symbol, code in [("600519.SH", "sh.600519"), ("600000.SH", "sh.600000")]:
                with gzip.open(input_dir / f"{symbol}.csv.gz", "wt", encoding="utf-8", newline="") as output:
                    writer = csv.writer(output, lineterminator="\n")
                    writer.writerow(fields)
                    writer.writerow([
                        "2026-09-01", "20260901093500000", code,
                        "10", "11", "9", "10.5", "1000", "10500", "3",
                    ])

            with patch.object(partition, "data_path", side_effect=lambda *parts: root.joinpath(*parts)):
                code = partition.main([
                    "--exchange", "sh", "--adjust", "raw",
                    "--input-dir", str(input_dir), "--output-dir", str(output_dir),
                ])
            self.assertEqual(code, 0)
            result = output_dir / "sh" / "2026" / "09" / "20260901.csv.gz"
            with gzip.open(result, "rt", encoding="utf-8", newline="") as source:
                rows = list(csv.DictReader(source))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["datetime"], "2026-09-01 09:35:00")
            self.assertEqual({row["symbol"] for row in rows}, {"600000.SH", "600519.SH"})
            self.assertEqual({row["adjust"] for row in rows}, {"raw"})


if __name__ == "__main__":
    unittest.main()
