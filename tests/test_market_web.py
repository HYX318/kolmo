import csv
import gzip
import tempfile
import unittest
from pathlib import Path

from kolmo.web.market_data import Bar, MarketStore, aggregate_bars
from kolmo.web.fundamentals import FundamentalStore


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


class FundamentalStoreTest(unittest.TestCase):
    @staticmethod
    def _write(path: Path, rows: list[dict[str, str]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def test_summary_filtering_and_series_preserve_sec_versions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "fundamental" / "us" / "sec"
            facts = [
                {
                    "row_id": "one", "symbol": "AAPL", "cik": "0000320193",
                    "taxonomy": "us-gaap", "tag": "RevenueFromContractWithCustomerExcludingAssessedTax",
                    "label": "Revenue", "description": "Revenue from customers", "unit": "USD",
                    "start_date": "2023-01-01", "end_date": "2023-12-31",
                    "value": "383285000000.123456789", "filed_date": "2024-02-01",
                    "accepted_at": "2024-02-01T21:00:00+00:00", "available_date": "2024-02-01",
                    "form": "10-K", "fiscal_year": "2023", "fiscal_period": "FY", "frame": "CY2023",
                    "accession_number": "0000320193-24-000001", "source": "sec.edgar.companyfacts",
                    "retrieved_at": "2026-08-30T11:00:00+00:00",
                },
                {
                    "row_id": "two", "symbol": "AAPL", "cik": "0000320193",
                    "taxonomy": "us-gaap", "tag": "RevenueFromContractWithCustomerExcludingAssessedTax",
                    "label": "Revenue", "description": "Revenue from customers", "unit": "USD",
                    "start_date": "2023-01-01", "end_date": "2023-12-31",
                    "value": "383284999999.123456789", "filed_date": "2024-02-05",
                    "accepted_at": "2024-02-05T21:00:00+00:00", "available_date": "2024-02-05",
                    "form": "10-K/A", "fiscal_year": "2023", "fiscal_period": "FY", "frame": "CY2023",
                    "accession_number": "0000320193-24-000002", "source": "sec.edgar.companyfacts",
                    "retrieved_at": "2026-08-30T11:00:00+00:00",
                },
                {
                    "row_id": "three", "symbol": "AAPL", "cik": "0000320193",
                    "taxonomy": "dei", "tag": "EntityPublicFloat", "label": "Public Float",
                    "description": "Public float", "unit": "USD", "start_date": "", "end_date": "2024-03-30",
                    "value": "255000000000.00000001", "filed_date": "2024-05-01",
                    "accepted_at": "", "available_date": "2024-05-01", "form": "10-Q",
                    "fiscal_year": "2024", "fiscal_period": "Q2", "frame": "",
                    "accession_number": "0000320193-24-000003", "source": "sec.edgar.companyfacts",
                    "retrieved_at": "2026-08-30T11:00:00+00:00",
                },
            ]
            filings = [
                {"symbol": "AAPL", "cik": "0000320193", "accession_number": accession,
                 "form": form, "filing_date": filed, "report_date": "2023-12-31",
                 "accepted_at": accepted, "primary_document": "report.htm", "is_xbrl": "1",
                 "is_inline_xbrl": "1", "source": "sec.edgar.submissions",
                 "retrieved_at": "2026-08-30T11:00:00+00:00"}
                for accession, form, filed, accepted in (
                    ("0000320193-24-000001", "10-K", "2024-02-01", "2024-02-01T21:00:00+00:00"),
                    ("0000320193-24-000002", "10-K/A", "2024-02-05", "2024-02-05T21:00:00+00:00"),
                    ("0000320193-24-000003", "10-Q", "2024-05-01", "2024-05-01T21:00:00+00:00"),
                )
            ]
            self._write(base / "company_facts" / "AAPL.csv.gz", facts)
            self._write(base / "filings" / "AAPL.csv.gz", filings)

            store = FundamentalStore(root)
            summary = store.summary("aapl")
            self.assertEqual(summary["facts_rows"], 3)
            self.assertEqual(summary["concepts"], 2)
            self.assertEqual(summary["available_end"], "2024-05-01")
            filtered = store.facts("AAPL", query="revenue", form="10-K/A")
            self.assertEqual(filtered["total"], 1)
            self.assertEqual(filtered["facts"][0]["value"], "383284999999.123456789")
            series = store.series(
                "AAPL", "us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax", "USD"
            )
            self.assertEqual([row["form"] for row in series["rows"]], ["10-K", "10-K/A"])
            self.assertEqual(store.filings("AAPL")["filings"][0]["form"], "10-Q")

    def test_missing_canonical_files_are_clear(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = FundamentalStore(Path(directory))
            self.assertFalse(store.available("AAPL"))
            with self.assertRaisesRegex(FileNotFoundError, "company_facts data not found"):
                store.summary("AAPL")


if __name__ == "__main__":
    unittest.main()
