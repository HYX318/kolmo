import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kolmo.validation.daily_mdcheck import (
    discover_years,
    run_daily_mdcheck,
    write_reports,
)


def validation_report(year: int) -> dict[str, object]:
    return {
        "build_id": f"build-{year}",
        "files_checked": 5,
        "partitions_checked": 1,
        "actual_outputs": {"1": {"rows": 241}},
        "findings": [],
        "summary": {"errors": 0, "warnings": 0},
    }


class DailyMdcheckTest(unittest.TestCase):
    def test_discovers_contiguous_range_and_reports_missing_year(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for year in (2010, 2012):
                year_root = root / f"year={year}"
                year_root.mkdir()
                (year_root / "manifest.json").write_text(json.dumps({
                    "year": year,
                    "date_range": {"start": f"{year}-01-01", "end": f"{year}-12-31"},
                }), encoding="utf-8")
            (root / "history-manifest.json").write_text(json.dumps({
                "status": "validated",
                "years": {
                    "2010": {"status": "validated"},
                    "2011": {"status": "validated"},
                    "2012": {"status": "validated"},
                },
            }), encoding="utf-8")
            self.assertEqual(discover_years(root, None, None), [2010, 2011, 2012])
            args = argparse.Namespace(
                root=root, start_year=None, end_year=None, workers=1,
                check_symbol_sessions=True,
            )
            with patch(
                "kolmo.validation.daily_mdcheck.validate_build",
                side_effect=lambda path, **_: validation_report(int(path.name[-4:])),
            ):
                report = run_daily_mdcheck(args)
            self.assertEqual(report["summary"]["years_requested"], 3)
            self.assertEqual(report["summary"]["years_checked"], 2)
            self.assertEqual(report["summary"]["files_checked"], 10)
            self.assertEqual(report["summary"]["errors"], 1)
            self.assertEqual(report["findings"][0]["check"], "missing_year_directory")

    def test_writes_json_and_csv_reports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = {
                "findings": [{
                    "year": 2023, "severity": "warning", "check": "sample",
                    "scope": "1m/raw/bj", "detail": "example",
                }],
                "summary": {"errors": 0, "warnings": 1},
            }
            output, details = write_reports(report, root / "report.json")
            self.assertEqual(json.loads(output.read_text())["summary"]["warnings"], 1)
            self.assertIn("2023,warning,sample", details.read_text())


if __name__ == "__main__":
    unittest.main()
