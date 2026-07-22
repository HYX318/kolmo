import csv
import gzip
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kolmo.ashare import update_cn_profile_daily
from kolmo.ashare.update_cn_profile_daily import merge_raw_cache


FIELDS = ["date", "code", "close"]


def write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def read_rows(path: Path) -> list[dict[str, str]]:
    with gzip.open(path, "rt", encoding="utf-8", newline="") as input_file:
        return list(csv.DictReader(input_file))


class MergeRawCacheTest(unittest.TestCase):
    def test_incremental_rows_replace_matching_dates_and_keep_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            canonical = root / "daily" / "qfq" / "000001.SZ.csv.gz"
            incremental = root / "daily_incremental" / "qfq" / "000001.SZ.csv.gz"
            write_rows(
                canonical,
                [
                    {"date": "2026-07-01", "code": "sz.000001", "close": "10"},
                    {"date": "2026-07-02", "code": "sz.000001", "close": "11"},
                ],
            )
            write_rows(
                incremental,
                [
                    {"date": "2026-07-02", "code": "sz.000001", "close": "12"},
                    {"date": "2026-07-03", "code": "sz.000001", "close": "13"},
                ],
            )

            merge_raw_cache(incremental, canonical)

            self.assertEqual(
                read_rows(canonical),
                [
                    {"date": "2026-07-01", "code": "sz.000001", "close": "10"},
                    {"date": "2026-07-02", "code": "sz.000001", "close": "12"},
                    {"date": "2026-07-03", "code": "sz.000001", "close": "13"},
                ],
            )

    def test_raw_target_fetches_without_rebuilding_profile_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "profile"
            latest = output_dir / "2026" / "07" / "20260707.csv"
            latest.parent.mkdir(parents=True)
            latest.touch()
            args = update_cn_profile_daily.argparse.Namespace(
                target="raw",
                output_dir=str(output_dir),
                end_date="20260718",
                lookback_days=10,
                adjust="qfq",
                sleep=0.0,
                workers=4,
                refresh_raw=True,
                raw_window="",
                limit=0,
                no_include_delisted=False,
                full_start_date="20170101",
            )
            captured: dict[str, list[str]] = {}
            with patch.object(
                update_cn_profile_daily,
                "run",
                side_effect=lambda command, cwd: captured.setdefault("command", command) and 0,
            ), patch.object(update_cn_profile_daily, "sync_incremental_raw_cache", return_value=2):
                self.assertEqual(update_cn_profile_daily.update_exchange(args, root, "sz"), 0)

            command = captured["command"]
            self.assertIn("kolmo.ashare.fetch_baostock_daily", command)
            self.assertIn("--no-combine", command)
            self.assertNotIn("kolmo.ashare.build_cn_profile_daily", command)
