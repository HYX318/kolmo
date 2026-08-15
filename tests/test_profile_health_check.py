import csv
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from kolmo.ashare.profile_health_check import check_file, discover_dates, enrich_status, profile_path


HEADER = [
    "date",
    "symbol",
    "exchange",
    "board",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "trade_status",
    "is_st",
    "source",
]


def write_profile(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=HEADER, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


class ProfileHealthCheckTest(unittest.TestCase):
    def test_check_file_counts_rows_and_duplicates(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "20260806.csv"
            write_profile(
                path,
                [
                    {"date": "20260806", "symbol": "000001.SZ", "exchange": "SZ", "board": "main", "open": "1", "high": "1", "low": "1", "close": "1", "volume": "1", "amount": "1", "trade_status": "1", "is_st": "0", "source": "test"},
                    {"date": "20260806", "symbol": "000001.SZ", "exchange": "SZ", "board": "main", "open": "1", "high": "1", "low": "1", "close": "1", "volume": "1", "amount": "1", "trade_status": "1", "is_st": "0", "source": "test"},
                ],
            )
            result = check_file(path)
            self.assertTrue(result["exists"])
            self.assertEqual(result["rows"], 2)
            self.assertEqual(result["duplicate_symbols"], 1)

    def test_discover_dates_and_profile_path(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_profile(profile_path(root, "sz", "20260806"), [])
            self.assertEqual(discover_dates(root, ["sz", "sh"]), ["20260806"])

    def test_enrich_status_flags_low_rows_and_failures(self) -> None:
        records = [
            {"date": "20260805", "exchange": "sz", "exists": True, "rows": 100, "missing_columns": [], "duplicate_symbols": 0, "non_positive_ohlc": 0, "failure_rows": 0},
            {"date": "20260806", "exchange": "sz", "exists": True, "rows": 50, "missing_columns": [], "duplicate_symbols": 0, "non_positive_ohlc": 0, "failure_rows": 2},
        ]
        enrich_status(records, min_row_ratio=0.85, min_absolute_rows=80)
        self.assertTrue(records[0]["ok"])
        self.assertFalse(records[1]["ok"])
        self.assertIn("low_rows", records[1]["issues"])
        self.assertIn("fetch_failures", records[1]["issues"])


if __name__ == "__main__":
    unittest.main()
