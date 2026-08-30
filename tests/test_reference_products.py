import csv
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from kolmo.data_products import (
    INDUSTRY_CLASSIFICATION_COLUMNS, SECURITY_MASTER_COLUMNS,
    TRADING_CALENDAR_COLUMNS,
)
from kolmo.reference.industry_classification import normalize_rows as normalize_industry_rows
from kolmo.reference.common import write_csv_atomic
from kolmo.reference.security_master import normalize_rows as normalize_security_rows
from kolmo.reference.security_master import price_limit_rule
from kolmo.reference.trading_calendar import normalize_rows as normalize_calendar_rows
from kolmo.reference.trading_calendar import output_groups
from kolmo.reference.security_master_store import SecurityMasterUnavailableError, load_active_symbols, select_snapshot


class ReferenceProductTest(unittest.TestCase):
    def test_industry_rows_are_date_effective_and_exclude_non_a_shares(self) -> None:
        rows = normalize_industry_rows(
            [
                {"updateDate": "2026-05-01", "code": "sh.600000", "code_name": "PF Bank", "industry": "银行", "industryClassification": "证监会行业分类"},
                {"updateDate": "2026-05-01", "code": "sh.000001", "code_name": "Index", "industry": "指数", "industryClassification": "test"},
            ],
            "20260513", "2026-08-28T00:00:00+00:00",
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "600000.SH")
        self.assertEqual(rows[0]["classification_date"], "20260513")
        self.assertEqual(rows[0]["provider_update_date"], "20260501")
        self.assertEqual(list(rows[0]), INDUSTRY_CLASSIFICATION_COLUMNS)

    def test_calendar_rows_are_normalized_and_sorted(self) -> None:
        rows = normalize_calendar_rows(
            [
                {"calendar_date": "2026-01-02", "is_trading_day": "1"},
                {"calendar_date": "2026-01-01", "is_trading_day": "0"},
            ],
            "2026-01-03T00:00:00+00:00",
        )
        self.assertEqual([item["date"] for item in rows], ["20260101", "20260102"])
        self.assertEqual(rows[0]["is_trading_day"], "0")
        self.assertEqual(rows[1]["source"], "baostock.query_trade_dates")

    def test_calendar_rejects_unknown_open_status(self) -> None:
        with self.assertRaisesRegex(ValueError, "is_trading_day"):
            normalize_calendar_rows([{"calendar_date": "2026-01-02", "is_trading_day": ""}], "now")

    def test_calendar_default_output_is_partitioned_by_year(self) -> None:
        rows = [
            {"date": "20251231", "market": "ashare", "is_trading_day": "1", "source": "test", "observed_at": "now"},
            {"date": "20260101", "market": "ashare", "is_trading_day": "0", "source": "test", "observed_at": "now"},
        ]
        groups = output_groups(rows, "")
        self.assertEqual([path.name for path, _ in groups], ["2025.csv", "2026.csv"])
        self.assertEqual([len(year_rows) for _, year_rows in groups], [1, 1])

    def test_security_master_is_date_effective_for_listing_and_delisting(self) -> None:
        rows = normalize_security_rows(
            [
                {"code": "sz.000001", "code_name": "Ping An", "ipoDate": "1991-04-03", "outDate": ""},
                {"code": "sh.688981", "code_name": "SMIC", "ipoDate": "2020-07-16", "outDate": ""},
                {"code": "sh.600001", "code_name": "Delisted", "ipoDate": "1990-01-01", "outDate": "2025-12-31"},
                {"code": "sz.301999", "code_name": "Future", "ipoDate": "2026-01-03", "outDate": ""},
                {"code": "sh.000001", "code_name": "Index", "ipoDate": "1991-01-01", "outDate": ""},
            ],
            "20260102",
            "2026-01-03T00:00:00+00:00",
        )
        self.assertEqual([item["symbol"] for item in rows], ["000001.SZ", "301999.SZ", "600001.SH", "688981.SH"])
        self.assertEqual(rows[0]["status_as_of"], "active")
        self.assertEqual(rows[1]["status_as_of"], "not_listed")
        self.assertEqual(rows[2]["status_as_of"], "delisted")
        self.assertEqual(rows[3]["base_price_limit_pct"], "0.20")
        self.assertEqual(rows[3]["st_status"], "unknown")

    def test_price_limit_rules_do_not_claim_exception_coverage(self) -> None:
        self.assertEqual(price_limit_rule("main")[0], "0.10")
        self.assertEqual(price_limit_rule("star")[0], "0.20")
        self.assertIn("requires_st", price_limit_rule("chi_next")[2])
        self.assertEqual(price_limit_rule("other"), ("", "unknown", "unavailable"))

    def test_reference_csv_write_is_atomic_and_contract_shaped(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "calendar.csv"
            write_csv_atomic(
                path,
                TRADING_CALENDAR_COLUMNS,
                [{"date": "20260102", "market": "ashare", "is_trading_day": "1", "source": "test", "observed_at": "now"}],
            )
            with path.open(encoding="utf-8", newline="") as source:
                reader = csv.DictReader(source)
                self.assertEqual(reader.fieldnames, TRADING_CALENDAR_COLUMNS)
                self.assertEqual(next(reader)["date"], "20260102")
            self.assertEqual(SECURITY_MASTER_COLUMNS[0], "observed_at")

    def test_security_master_store_never_uses_future_observation(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "2026" / "08" / "20260814.csv"
            second = root / "2026" / "08" / "20260816.csv"
            for path, symbol in ((first, "000001.SZ"), (second, "000002.SZ")):
                write_csv_atomic(
                    path,
                    SECURITY_MASTER_COLUMNS,
                    [{"observed_at": "now", "symbol": symbol, "exchange": "SZ", "name": "test", "board": "main", "listing_date": "", "delisting_date": "", "status_as_of": "active", "st_status": "unknown", "base_price_limit_pct": "0.10", "price_limit_rule": "main_10pct_base", "price_limit_status": "unknown", "source": "test"}],
                )
            self.assertEqual(select_snapshot(root, "20260815"), first)
            self.assertEqual(load_active_symbols(root, "20260816"), {"000002.SZ"})
            with self.assertRaises(SecurityMasterUnavailableError):
                select_snapshot(root, "20260813")


if __name__ == "__main__":
    unittest.main()
