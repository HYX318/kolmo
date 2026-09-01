import csv
import gzip
import json
import tempfile
import unittest
from pathlib import Path

from kolmo.data_products import SEC_COMPANY_FACT_COLUMNS, SEC_FILING_COLUMNS, US_STANDARDIZED_PROVENANCE_COLUMNS
from kolmo.fundamental.build_us_financials import build_one, main as build_main
from kolmo.fundamental.sec_edgar import SecUniverseEntry, write_symbol_atomic
from kolmo.fundamental.us_financials import (
    classify_period,
    latest_metrics_as_of,
    provenance_path,
    standardize_symbol,
    standardized_path,
    write_standardized_atomic,
)
from kolmo.fundamental.validate_us_financials import validate_symbol


ENTRY = SecUniverseEntry("TEST", "0000000001")
BUILT_AT = "2026-08-30T00:00:00+00:00"


def filing(accession, form, available, report):
    return {
        "symbol": "TEST", "cik": ENTRY.cik, "accession_number": accession, "form": form,
        "filing_date": available, "report_date": report,
        "accepted_at": f"{available}T12:00:00+00:00", "primary_document": "report.htm",
        "is_xbrl": "1", "is_inline_xbrl": "1", "source": "sec.edgar.submissions",
        "retrieved_at": BUILT_AT,
    }


def fact(row_id, tag, value, start, end, available, accession, fp, fy="2024", form="10-Q", unit="USD", taxonomy="us-gaap"):
    return {
        "row_id": row_id, "symbol": "TEST", "cik": ENTRY.cik, "taxonomy": taxonomy,
        "tag": tag, "label": tag, "description": tag, "unit": unit,
        "start_date": start, "end_date": end, "value": str(value), "filed_date": available,
        "accepted_at": f"{available}T12:00:00+00:00", "available_date": available,
        "form": form, "fiscal_year": fy, "fiscal_period": fp, "frame": "",
        "accession_number": accession, "source": "sec.edgar.companyfacts", "retrieved_at": BUILT_AT,
    }


def standard_fixture(include_q3=True, amendment=False, custom=False):
    filings = [
        filing("0000000001-24-000001", "10-Q", "2024-05-01", "2024-03-31"),
        filing("0000000001-24-000002", "10-Q", "2024-08-01", "2024-06-30"),
        filing("0000000001-24-000003", "10-Q", "2024-11-01", "2024-09-30"),
        filing("0000000001-25-000004", "10-K", "2025-02-01", "2024-12-31"),
    ]
    facts = [
        fact("q1-rev", "RevenueFromContractWithCustomerExcludingAssessedTax", 40, "2024-01-01", "2024-03-31", "2024-05-01", filings[0]["accession_number"], "Q1"),
        fact("q1-rev-low", "Revenues", 41, "2024-01-01", "2024-03-31", "2024-05-01", filings[0]["accession_number"], "Q1"),
        fact("q1-oi", "OperatingIncomeLoss", 10, "2024-01-01", "2024-03-31", "2024-05-01", filings[0]["accession_number"], "Q1"),
        fact("q1-ni", "NetIncomeLoss", -2, "2024-01-01", "2024-03-31", "2024-05-01", filings[0]["accession_number"], "Q1"),
        fact("q1-ocf", "NetCashProvidedByUsedInOperatingActivities", -3, "2024-01-01", "2024-03-31", "2024-05-01", filings[0]["accession_number"], "Q1"),
        fact("q1-capex", "PaymentsToAcquirePropertyPlantAndEquipment", -5, "2024-01-01", "2024-03-31", "2024-05-01", filings[0]["accession_number"], "Q1"),
        fact("q2-rev", "RevenueFromContractWithCustomerExcludingAssessedTax", 60, "2024-04-01", "2024-06-30", "2024-08-01", filings[1]["accession_number"], "Q2"),
        fact("h1-rev", "RevenueFromContractWithCustomerExcludingAssessedTax", 100, "2024-01-01", "2024-06-30", "2024-08-01", filings[1]["accession_number"], "Q2"),
        fact("h1-oi", "OperatingIncomeLoss", 25, "2024-01-01", "2024-06-30", "2024-08-01", filings[1]["accession_number"], "Q2"),
        fact("q2-ni", "NetIncomeLoss", 3, "2024-04-01", "2024-06-30", "2024-08-01", filings[1]["accession_number"], "Q2"),
        fact("q2-assets", "Assets", 1000, "", "2024-06-30", "2024-08-01", filings[1]["accession_number"], "Q2"),
        fact("q2-equity", "StockholdersEquity", -10, "", "2024-06-30", "2024-08-01", filings[1]["accession_number"], "Q2"),
        fact("q2-debt", "LongTermDebtAndFinanceLeaseObligations", 200, "", "2024-06-30", "2024-08-01", filings[1]["accession_number"], "Q2"),
        fact("q2-rev-wrong-unit", "Revenues", 999, "2024-04-01", "2024-06-30", "2024-08-01", filings[1]["accession_number"], "Q2", unit="USD/shares"),
        fact("fy-rev", "RevenueFromContractWithCustomerExcludingAssessedTax", 250, "2024-01-01", "2024-12-31", "2025-02-01", filings[3]["accession_number"], "FY", form="10-K"),
        fact("fy-oi", "OperatingIncomeLoss", 70, "2024-01-01", "2024-12-31", "2025-02-01", filings[3]["accession_number"], "FY", form="10-K"),
        fact("fy-ni", "NetIncomeLoss", 20, "2024-01-01", "2024-12-31", "2025-02-01", filings[3]["accession_number"], "FY", form="10-K"),
        fact("fy-assets", "Assets", 1200, "", "2024-12-31", "2025-02-01", filings[3]["accession_number"], "FY", form="10-K"),
    ]
    if include_q3:
        facts += [
            fact("q3-rev", "RevenueFromContractWithCustomerExcludingAssessedTax", 70, "2024-07-01", "2024-09-30", "2024-11-01", filings[2]["accession_number"], "Q3"),
            fact("9m-rev", "RevenueFromContractWithCustomerExcludingAssessedTax", 170, "2024-01-01", "2024-09-30", "2024-11-01", filings[2]["accession_number"], "Q3"),
            fact("9m-oi", "OperatingIncomeLoss", 45, "2024-01-01", "2024-09-30", "2024-11-01", filings[2]["accession_number"], "Q3"),
            fact("q3-ni", "NetIncomeLoss", 7, "2024-07-01", "2024-09-30", "2024-11-01", filings[2]["accession_number"], "Q3"),
        ]
    if amendment:
        amendment_filing = filing("0000000001-24-000005", "10-Q/A", "2024-06-01", "2024-03-31")
        filings.append(amendment_filing)
        facts.append(fact("q1-rev-amend", "RevenueFromContractWithCustomerExcludingAssessedTax", 42, "2024-01-01", "2024-03-31", "2024-06-01", amendment_filing["accession_number"], "Q1", form="10-Q/A"))
    if custom:
        facts.append(fact("custom", "IssuerRevenue", 999, "2024-01-01", "2024-03-31", "2024-05-01", filings[0]["accession_number"], "Q1", taxonomy="testco"))
    return filings, facts


def build_fixture(include_q3=True, amendment=False, custom=False, business_model="non_financial"):
    filings, facts = standard_fixture(include_q3, amendment, custom)
    return (*standardize_symbol("TEST", ENTRY.cik, facts, filings, business_model), filings, facts)


class UsFinancialStandardizationTest(unittest.TestCase):
    def test_direct_quarter_ytd_derivation_q4_and_ttm(self):
        products, provenance, stats, _, _ = build_fixture()
        by_fp = {row["fiscal_period"]: row for row in products["quarterly"] if row["fiscal_year"] == "2024"}
        self.assertEqual(by_fp["Q1"]["revenue"], "40")
        self.assertEqual(by_fp["Q2"]["revenue"], "60")
        self.assertEqual(by_fp["Q2"]["operating_income"], "15")
        self.assertIn("quarter_derived_from_ytd", by_fp["Q2"]["quality_flags"])
        self.assertEqual(by_fp["Q3"]["operating_income"], "20")
        self.assertEqual(by_fp["Q4"]["revenue"], "80")
        self.assertEqual(by_fp["Q4"]["operating_income"], "25")
        self.assertEqual(by_fp["Q4"]["available_date"], "2025-02-01")
        self.assertIn("q4_derived_from_fy", by_fp["Q4"]["quality_flags"])
        self.assertEqual(len(products["ttm"]), 1)
        self.assertEqual(products["ttm"][0]["revenue"], "250")
        ttm_source = next(row for row in provenance if row["period_type"] == "ttm" and row["metric"] == "revenue")
        self.assertEqual(len(ttm_source["component_periods"].split(";")), 4)

    def test_priority_conflict_units_negative_values_and_capex_sign(self):
        products, _, _, _, _ = build_fixture()
        q1 = next(row for row in products["quarterly"] if row["fiscal_period"] == "Q1")
        self.assertEqual(q1["revenue"], "40")
        self.assertIn("conflicting_candidate_tags", q1["quality_flags"])
        self.assertEqual(q1["net_income"], "-2")
        self.assertEqual(q1["operating_cash_flow"], "-3")
        self.assertEqual(q1["capital_expenditure"], "5")
        self.assertEqual(q1["free_cash_flow"], "-8")
        q2 = next(row for row in products["quarterly"] if row["fiscal_period"] == "Q2")
        self.assertEqual(q2["revenue"], "60")

    def test_missing_quarter_prevents_ttm_and_balance_is_not_summed(self):
        products, _, _, _, _ = build_fixture(include_q3=False)
        self.assertEqual(products["ttm"], [])
        products, _, _, _, _ = build_fixture()
        self.assertEqual(products["ttm"][0]["total_assets"], "1200")

    def test_amendment_versions_and_as_of_visibility(self):
        products, provenance, _, _, _ = build_fixture(amendment=True)
        q1_versions = [row for row in products["quarterly"] if row["report_period"] == "2024-03-31"]
        self.assertEqual([(row["available_date"], row["revenue"]) for row in q1_versions], [("2024-05-01", "40"), ("2024-06-01", "42")])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_standardized_atomic(root, "TEST", products, provenance)
            self.assertEqual(latest_metrics_as_of("TEST", "2024-05-15", "quarterly", root)["revenue"], "40")
            self.assertEqual(latest_metrics_as_of("TEST", "2024-06-15", "quarterly", root)["revenue"], "42")
            self.assertIsNone(latest_metrics_as_of("TEST", "2024-04-30", "quarterly", root))

    def test_later_comparison_retains_original_version(self):
        filings, facts = standard_fixture()
        later = filing("0000000001-25-000006", "10-Q", "2025-05-01", "2025-03-31")
        filings.append(later)
        facts.append(fact("q1-compare", "RevenueFromContractWithCustomerExcludingAssessedTax", 43, "2024-01-01", "2024-03-31", "2025-05-01", later["accession_number"], "Q1", fy="2025"))
        products, _, _ = standardize_symbol("TEST", ENTRY.cik, facts, filings, "non_financial")
        versions = [row for row in products["quarterly"] if row["report_period"] == "2024-03-31"]
        self.assertEqual({row["revenue"] for row in versions}, {"40", "43"})

    def test_comparison_focus_does_not_relabel_quarter_or_erase_ttm_metric(self):
        filings, facts = standard_fixture()
        later = filing("0000000001-25-000006", "10-Q", "2025-08-01", "2025-06-30")
        filings.append(later)
        facts.append(fact(
            "q1-compare-ni", "NetIncomeLoss", 9, "2024-01-01", "2024-03-31",
            "2025-08-01", later["accession_number"], "Q2", fy="2025",
        ))
        products, provenance, _ = standardize_symbol("TEST", ENTRY.cik, facts, filings, "non_financial")
        q1_versions = [row for row in products["quarterly"] if row["report_period"] == "2024-03-31"]
        self.assertEqual({(row["fiscal_year"], row["fiscal_period"]) for row in q1_versions}, {("2024", "Q1")})
        latest_ttm = max(products["ttm"], key=lambda row: row["available_date"])
        self.assertEqual(latest_ttm["revenue"], "250")
        self.assertEqual(latest_ttm["net_income"], "31")
        revenue_source = next(
            row for row in provenance
            if row["period_type"] == "ttm" and row["available_date"] == "2025-08-01" and row["metric"] == "revenue"
        )
        self.assertEqual(len(revenue_source["component_accessions"].split(";")), 4)

    def test_future_fact_custom_taxonomy_and_ambiguous_period_are_flagged(self):
        filings, facts = standard_fixture(custom=True)
        facts.append(fact("future", "Assets", 9999, "", "2025-12-31", "2025-02-01", filings[3]["accession_number"], "FY", form="10-K"))
        facts.append(fact("ambiguous", "Revenues", 1, "2024-01-01", "2024-05-15", "2024-08-01", filings[1]["accession_number"], "Q2"))
        products, _, stats = standardize_symbol("TEST", ENTRY.cik, facts, filings, "non_financial")
        self.assertEqual(stats.excluded_future_period_facts, 1)
        self.assertEqual(stats.ambiguous_period_facts, 1)
        self.assertTrue(stats.unsupported_custom_taxonomy)
        flags = products["annual"][0]["quality_flags"]
        self.assertNotIn("future_period_fact_excluded", flags)
        self.assertNotIn("unsupported_custom_taxonomy", flags)
        self.assertNotIn("period_classification_ambiguous", flags)

    def test_as_of_freshness_guard(self):
        products, provenance, _, _, _ = build_fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_standardized_atomic(root, "TEST", products, provenance)
            self.assertIsNone(latest_metrics_as_of("TEST", "2026-08-30", "ttm", root))
            stale = latest_metrics_as_of(
                "TEST", "2026-08-30", "ttm", root, max_age_days=180, include_stale=True
            )
            self.assertEqual(stale["is_stale"], "1")
            self.assertGreater(int(stale["age_days"]), 180)

    def test_noncalendar_and_53_week_period_classification(self):
        row = fact("x", "Revenues", 1, "2023-10-01", "2024-09-28", "2024-11-01", "a", "FY", form="10-K")
        self.assertEqual(classify_period(row), "annual")
        row["start_date"], row["end_date"] = "2023-09-24", "2024-09-28"
        self.assertEqual(classify_period(row), "annual")

    def test_zero_denominator_ratio_is_empty_and_negative_equity_flagged(self):
        filings, facts = standard_fixture()
        facts.append(fact("current-assets", "AssetsCurrent", 100, "", "2024-06-30", "2024-08-01", filings[1]["accession_number"], "Q2"))
        facts.append(fact("zero-current-liabilities", "LiabilitiesCurrent", 0, "", "2024-06-30", "2024-08-01", filings[1]["accession_number"], "Q2"))
        products, _, _ = standardize_symbol("TEST", ENTRY.cik, facts, filings, "non_financial")
        q2 = next(row for row in products["quarterly"] if row["fiscal_period"] == "Q2")
        self.assertEqual(q2["current_ratio"], "")
        self.assertIn("negative_equity", q2["quality_flags"])

    def test_bank_and_reit_applicability(self):
        for model in ("bank", "reit"):
            products, _, _, _, _ = build_fixture(business_model=model)
            q1 = next(row for row in products["quarterly"] if row["fiscal_period"] == "Q1")
            self.assertEqual(q1["free_cash_flow"], "")
            self.assertIn("not_applicable_for_business_model", q1["quality_flags"])
            self.assertIn("not_applicable:free_cash_flow", q1["applicability_flags"])

    def test_deterministic_atomic_output_and_bad_provenance_validation(self):
        products, provenance, _, filings, facts = build_fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_root, output_root = root / "sec", root / "standardized"
            write_symbol_atomic(input_root, "TEST", filings, facts)
            write_standardized_atomic(output_root, "TEST", products, provenance)
            before = {path: path.read_bytes() for path in output_root.rglob("TEST.csv.gz")}
            write_standardized_atomic(output_root, "TEST", products, provenance)
            self.assertEqual(before, {path: path.read_bytes() for path in output_root.rglob("TEST.csv.gz")})
            self.assertTrue(validate_symbol(input_root, output_root, ENTRY)["ok"])
            broken = [dict(row) for row in provenance]
            broken[0]["source_row_ids"] = "missing-row"
            path = provenance_path(output_root, "TEST")
            with gzip.open(path, "wt", encoding="utf-8", newline="") as output:
                writer = csv.DictWriter(output, fieldnames=US_STANDARDIZED_PROVENANCE_COLUMNS)
                writer.writeheader(); writer.writerows(broken)
            self.assertFalse(validate_symbol(input_root, output_root, ENTRY)["ok"])

    def test_validator_recomputes_ttm_and_reports_staleness(self):
        products, provenance, _, filings, facts = build_fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_root, output_root = root / "sec", root / "standardized"
            write_symbol_atomic(input_root, "TEST", filings, facts)
            write_standardized_atomic(output_root, "TEST", products, provenance)
            report = validate_symbol(
                input_root, output_root, ENTRY, as_of_date="2026-08-30", max_ttm_age_days=180
            )
            self.assertTrue(any("stale" in warning for warning in report["warnings"]))
            broken = {name: [dict(row) for row in rows] for name, rows in products.items()}
            broken["ttm"][0]["revenue"] = "251"
            write_standardized_atomic(output_root, "TEST", broken, provenance)
            report = validate_symbol(input_root, output_root, ENTRY, as_of_date="2025-02-01")
            self.assertFalse(report["ok"])
            self.assertTrue(any("does not equal" in error for error in report["errors"]))

    def test_symbol_failure_preserves_old_outputs(self):
        products, provenance, _, _, _ = build_fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_standardized_atomic(root / "out", "TEST", products, provenance)
            target = standardized_path(root / "out", "quarterly", "TEST")
            before = target.read_bytes()
            result = build_one(ENTRY, "non_financial", root / "missing", root / "out")
            self.assertEqual(result.status, "failed")
            self.assertEqual(target.read_bytes(), before)

    def test_manifest_records_partial_failure(self):
        products, provenance, _, filings, facts = build_fixture()
        del products, provenance
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_root, output_root = root / "sec", root / "out"
            write_symbol_atomic(input_root, "TEST", filings, facts)
            universe = root / "universe.csv"
            universe.write_text(
                "symbol,asset_type,enabled,sector,industry,cik\n"
                "TEST,STK,1,Technology,Software,1\nFAIL,STK,1,Technology,Software,2\n",
                encoding="utf-8",
            )
            code = build_main(["--universe", str(universe), "--input-root", str(input_root), "--output-root", str(output_root)])
            self.assertEqual(code, 1)
            manifest = json.loads(next((output_root / "_meta" / "build_runs").glob("*.json")).read_text())
            self.assertEqual(manifest["status"], "completed_with_failures")
            self.assertEqual({row["status"] for row in manifest["results"]}, {"built", "failed"})


if __name__ == "__main__":
    unittest.main()
