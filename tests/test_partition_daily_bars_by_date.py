import csv
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from kolmo.ashare import partition_daily_bars_by_date as partition


class PartitionDailyBarsTest(unittest.TestCase):
    def test_overwrite_replaces_inode_and_preserves_existing_hardlink(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "combined.csv"
            output_root = root / "profile"
            input_path.write_text("date,symbol,close\n20240102,000001.SZ,10\n", encoding="utf-8")
            argv = [
                "partition", "--input", str(input_path), "--output-dir", str(output_root)
            ]
            with patch("sys.argv", argv):
                self.assertEqual(partition.main(), 0)
            output = output_root / "2024" / "01" / "20240102.csv"
            snapshot_link = root / "snapshot.csv"
            os.link(output, snapshot_link)
            old_inode = output.stat().st_ino

            input_path.write_text("date,symbol,close\n20240102,000001.SZ,11\n", encoding="utf-8")
            with patch("sys.argv", argv):
                self.assertEqual(partition.main(), 0)

            self.assertNotEqual(output.stat().st_ino, old_inode)
            self.assertIn(",11\n", output.read_text(encoding="utf-8"))
            self.assertIn(",10\n", snapshot_link.read_text(encoding="utf-8"))

    def test_append_mode_is_rejected(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "combined.csv"
            input_path.write_text("date,symbol\n20240102,000001.SZ\n", encoding="utf-8")
            with patch(
                "sys.argv",
                [
                    "partition", "--input", str(input_path), "--output-dir", str(root / "out"),
                    "--no-overwrite",
                ],
            ), self.assertRaisesRegex(ValueError, "append mode"):
                partition.main()

    def test_fetch_evidence_is_bound_to_written_partition(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_path = root / "combined.csv"
            output_root = root / "profile"
            evidence_path = root / "fetch.json"
            input_path.write_text(
                "date,symbol,close\n20240102,000001.SZ,10\n", encoding="utf-8"
            )
            evidence_path.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0.0",
                        "product_id": "baostock_daily_fetch",
                        "run_id": "test",
                        "exchange": "sz",
                        "start_date": "20240102",
                        "end_date": "20240102",
                        "status": "completed",
                        "failure_manifest_path": str(root / "failures.csv"),
                        "failure_manifest_sha256": "0" * 64,
                        "symbols": {"total": 1, "fetched": 1, "cached": 0, "failed": 0},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with patch(
                "sys.argv",
                [
                    "partition", "--input", str(input_path), "--output-dir", str(output_root),
                    "--fetch-evidence", str(evidence_path),
                ],
            ):
                self.assertEqual(partition.main(), 0)

            output = output_root / "2024" / "01" / "20240102.csv"
            provenance = output.with_name("20240102.csv.provenance.json")
            payload = json.loads(provenance.read_text(encoding="utf-8"))
            self.assertEqual(payload["date"], "20240102")
            self.assertEqual(payload["exchange"], "sz")
            self.assertEqual(payload["profile_path"], str(output.resolve()))
            self.assertEqual(payload["fetch_evidence_path"], str(evidence_path.resolve()))


if __name__ == "__main__":
    unittest.main()
