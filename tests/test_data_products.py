import csv
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from kolmo.data_products import FINANCIAL_STATEMENT_COLUMNS, is_a_share_symbol, normalize_date, validate_columns
from kolmo.fundamental.fetch_baostock_financials import (
    is_a_share_bs_code,
    normalize_statement,
    output_has_periods,
    requested_periods,
    to_bs_code,
    to_symbol,
    write_symbol,
)
from kolmo.fundamental.validate_financials import validate_file


def source_row(kind: str, **values: str) -> dict[str, str]:
    row = {
        "code": "sz.000001",
        "pubDate": "2024-04-30",
        "statDate": "2024-03-31",
    }
    fields = {
        "profit": {"MBRevenue": "100", "netProfit": "10", "roeAvg": "0.5", "gpMargin": "1"},
        "balance": {"liabilityToAsset": "5"},
        "cash_flow": {"CFOToOR": "0.5"},
    }
    row.update(fields[kind])
    row.update(values)
    return row


def normalized_row(**values: str) -> dict[str, str]:
    row = normalize_statement(
        "sz.000001",
        2024,
        1,
        {name: [source_row(name)] for name in ("profit", "balance", "cash_flow")},
    )
    assert row is not None
    row.update(values)
    return row


def write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=FINANCIAL_STATEMENT_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


class DataProductContractTest(unittest.TestCase):
    def test_normalize_date_accepts_common_formats(self) -> None:
        self.assertEqual(normalize_date("2024-01-02"), "20240102")
        self.assertEqual(normalize_date("20240102"), "20240102")

    def test_normalize_date_rejects_invalid_values(self) -> None:
        with self.assertRaises(ValueError):
            normalize_date("2024/01/02")

    def test_validate_columns_reports_missing_columns(self) -> None:
        result = validate_columns(["symbol", "report_period"], FINANCIAL_STATEMENT_COLUMNS)
        self.assertFalse(result.ok)
        self.assertIn("announce_date", result.missing)

    def test_financial_file_rejects_lookahead_announce_date(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "000001.SZ.csv"
            write_rows(path, [normalized_row(announce_date="20240301", source_pub_date="2024-03-01")])
            result = validate_file(path)
            self.assertFalse(result["ok"])
            self.assertIn("announce_date before report_period", result["errors"][0])

    def test_financial_file_rejects_empty_files(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "000001.SZ.csv"
            path.write_text(",".join(FINANCIAL_STATEMENT_COLUMNS) + "\n", encoding="utf-8")
            result = validate_file(path)
            self.assertFalse(result["ok"])
            self.assertIn("no financial rows", result["errors"][0])

    def test_baostock_symbol_conversion(self) -> None:
        self.assertEqual(to_bs_code("000001.SZ"), "sz.000001")
        self.assertEqual(to_symbol("sh.600000"), "600000.SH")

    def test_a_share_symbol_filter_excludes_indices(self) -> None:
        self.assertTrue(is_a_share_symbol("000001.SZ"))
        self.assertTrue(is_a_share_symbol("600000.SH"))
        self.assertFalse(is_a_share_symbol("000090.SH"))
        self.assertFalse(is_a_share_bs_code("sh.000090"))

    def test_normalize_baostock_statement(self) -> None:
        row = normalize_statement(
            "sz.000001",
            2024,
            1,
            {
                "profit": [{
                    "code": "sz.000001",
                    "pubDate": "2024-04-30",
                    "statDate": "2024-03-31",
                    "MBRevenue": "100",
                    "netProfit": "10",
                    "roeAvg": "0.5",
                    "gpMargin": "1",
                }],
                "balance": [{
                    "code": "sz.000001",
                    "pubDate": "2024-04-30",
                    "statDate": "2024-03-31",
                    "liabilityToAsset": "5",
                }],
                "cash_flow": [{
                    "code": "sz.000001",
                    "pubDate": "2024-04-30",
                    "statDate": "2024-03-31",
                    "CFOToOR": "0.5",
                }],
            },
        )
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["symbol"], "000001.SZ")
        self.assertEqual(row["announce_date"], "20240430")
        self.assertEqual(row["roe"], "0.00500000")
        self.assertEqual(row["gross_margin"], "0.01000000")
        self.assertEqual(row["debt_to_assets"], "0.05000000")
        self.assertEqual(row["operating_cash_flow_ratio"], "0.00500000")
        self.assertEqual(row["roe_raw_percent"], "0.5")
        self.assertEqual(row["gross_margin_raw_percent"], "1")
        self.assertEqual(row["debt_to_assets_raw_percent"], "5")
        self.assertEqual(row["total_liabilities"], "")

    def test_normalize_rejects_misaligned_report_versions(self) -> None:
        payload = {name: [source_row(name)] for name in ("profit", "balance", "cash_flow")}
        payload["cash_flow"][0]["pubDate"] = "2024-04-29"
        with self.assertRaisesRegex(ValueError, "report version mismatch"):
            normalize_statement("sz.000001", 2024, 1, payload)

    def test_write_symbol_merges_history_and_replaces_same_primary_key(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old = normalized_row()
            write_symbol(root, "000001.SZ", [old])
            new = normalized_row(
                report_period="20240630",
                announce_date="20240830",
                statement_type="half_year",
                source_year="2024",
                source_quarter="2",
                source_stat_date="2024-06-30",
                source_pub_date="2024-08-30",
            )
            replacement = dict(old, revenue="101")
            write_symbol(root, "000001.SZ", [new, replacement])
            with (root / "000001.SZ.csv").open("r", encoding="utf-8", newline="") as file:
                rows = list(csv.DictReader(file))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["revenue"], "101")
            self.assertEqual(rows[1]["report_period"], "20240630")

    def test_write_symbol_replaces_header_only_legacy_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "000001.SZ.csv"
            path.write_text(
                "symbol,report_period,announce_date,statement_type,roe\n",
                encoding="utf-8",
            )
            write_symbol(root, "000001.SZ", [normalized_row()])
            with path.open("r", encoding="utf-8", newline="") as file:
                rows = list(csv.DictReader(file))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["source_code"], "sz.000001")

    def test_validator_rejects_duplicate_primary_key_and_bad_unit_conversion(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "000001.SZ.csv"
            row = normalized_row(roe="0.5")
            write_rows(path, [row, row])
            result = validate_file(path)
            self.assertFalse(result["ok"])
            self.assertTrue(any("duplicate financial primary key" in error for error in result["errors"]))
            self.assertTrue(any("roe must equal roe_raw_percent / 100" in error for error in result["errors"]))

    def test_requested_periods_and_resume_period_check(self) -> None:
        self.assertEqual(requested_periods(2024, 2024, 1), [(2024, 1)])
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "000001.SZ.csv"
            write_rows(path, [normalized_row()])
            self.assertTrue(output_has_periods(path, [(2024, 1)]))
            self.assertFalse(output_has_periods(path, [(2024, 1), (2024, 2)]))

    def test_resume_rejects_corrupt_completed_period(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "000001.SZ.csv"
            write_rows(path, [normalized_row(revenue="not-a-number")])
            self.assertFalse(output_has_periods(path, [(2024, 1)]))


if __name__ == "__main__":
    unittest.main()
