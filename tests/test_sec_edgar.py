import gzip
import json
import os
import tempfile
import unittest
import urllib.error
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from kolmo.data_products import SEC_COMPANY_FACT_COLUMNS, SEC_FILING_COLUMNS
from kolmo.fundamental.fetch_sec_edgar import fetch_one, main as fetch_main, run_fetch
from kolmo.fundamental.sec_edgar import (
    HttpResponse,
    GlobalRateLimiter,
    SecHttpClient,
    SecHttpError,
    SecUniverseEntry,
    fact_sort_key,
    facts_path,
    filings_path,
    load_sec_universe,
    merge_facts,
    normalize_cik,
    normalize_company_facts,
    normalize_filings,
    parse_json_bytes,
    read_gzip_csv,
    select_sec_universe,
    write_raw_snapshot_if_changed,
    write_symbol_atomic,
)
from kolmo.fundamental.validate_sec_edgar import validate_sec_files, validate_symbol


FIXTURES = Path(__file__).parent / "fixtures" / "sec"
ENTRY = SecUniverseEntry("AAPL", "0000320193")
RETRIEVED = "2026-08-30T08:00:00+00:00"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def fixture_payload(name: str) -> dict[str, object]:
    payload = parse_json_bytes(fixture_bytes(name))
    assert isinstance(payload, dict)
    return payload


def normalized_fixture() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    submissions = fixture_payload("aapl_submissions.json")
    historical = fixture_payload("aapl_submissions_001.json")
    filings = normalize_filings(ENTRY.symbol, ENTRY.cik, submissions, [historical], RETRIEVED)
    accepted = {row["accession_number"]: row["accepted_at"] for row in filings}
    facts = normalize_company_facts(
        ENTRY.symbol, ENTRY.cik, fixture_payload("aapl_companyfacts.json"), accepted, RETRIEVED
    )
    return filings, facts


class FakeClient:
    def __init__(self, fail_cik: str = ""):
        self.fail_cik = fail_cik
        self.calls: list[str] = []

    def get(self, url: str) -> HttpResponse:
        self.calls.append(url)
        is_msft = "0000789019" in url
        cik = "0000789019" if is_msft else ENTRY.cik
        if self.fail_cik == cik and "companyfacts" in url:
            raise SecHttpError("temporary failure", 503)
        if "companyfacts" in url:
            if is_msft:
                payload = json.loads(fixture_bytes("aapl_companyfacts.json"))
                payload["cik"] = int(cik)
                body = json.dumps(payload, separators=(",", ":")).encode()
            else:
                body = fixture_bytes("aapl_companyfacts.json")
        elif "submissions-001" in url:
            body = fixture_bytes("aapl_submissions_001.json")
        else:
            payload = json.loads(fixture_bytes("aapl_submissions.json"))
            payload["cik"] = int(cik)
            if is_msft:
                payload["filings"]["files"] = []
            body = json.dumps(payload, separators=(",", ":")).encode()
        return HttpResponse(url, 200, body)


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200):
        self.body = body
        self.status = status
        self.headers: dict[str, str] = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return self.body


class SecEdgarContractTest(unittest.TestCase):
    def test_cik_zero_padding_and_validation(self) -> None:
        self.assertEqual(normalize_cik("320193"), "0000320193")
        self.assertEqual(normalize_cik(320193), "0000320193")
        with self.assertRaisesRegex(ValueError, "invalid CIK"):
            normalize_cik("32A193")

    def test_universe_filters_disabled_etf_and_missing_cik(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "universe.csv"
            path.write_text(
                "symbol,asset_type,cik,enabled\n"
                "AAPL,STK,320193,1\nSPY,ETF,,1\nMSFT,STK,789019,0\nNVDA,STK,,1\n",
                encoding="utf-8",
            )
            entries, skipped = load_sec_universe(path)
        self.assertEqual(entries, [ENTRY])
        self.assertEqual({item.reason for item in skipped}, {"asset_type_not_stock", "disabled", "missing_cik"})

    def test_cik_selector_can_resolve_multiple_share_classes(self) -> None:
        entries = [SecUniverseEntry("GOOG", "0001652044"), SecUniverseEntry("GOOGL", "0001652044")]
        self.assertEqual(select_sec_universe(entries, [], ["1652044"]), entries)

    def test_company_facts_expand_multiple_taxonomies_and_units(self) -> None:
        _, facts = normalized_fixture()
        self.assertEqual({row["taxonomy"] for row in facts}, {"dei", "us-gaap"})
        revenue = [row for row in facts if row["tag"] == "Revenues"]
        self.assertEqual({row["unit"] for row in revenue}, {"USD", "USD/shares"})

    def test_instant_fact_allows_empty_start_date(self) -> None:
        _, facts = normalized_fixture()
        instant = next(row for row in facts if row["tag"] == "EntityPublicFloat")
        self.assertEqual(instant["start_date"], "")

    def test_negative_and_high_precision_values_are_preserved(self) -> None:
        _, facts = normalized_fixture()
        loss = next(row for row in facts if row["tag"] == "NetIncomeLoss")
        revenue = next(row for row in facts if row["tag"] == "Revenues" and row["form"] == "10-K" and row["unit"] == "USD")
        self.assertEqual(loss["value"], "-1000000.000000001")
        self.assertEqual(revenue["value"], "383285000000.123456789")

    def test_accession_links_accepted_at_and_available_date(self) -> None:
        _, facts = normalized_fixture()
        row = next(item for item in facts if item["accession_number"] == "0000320193-23-000106")
        self.assertTrue(row["accepted_at"].startswith("2023-11-03T18:30:01"))
        self.assertEqual(row["available_date"], "2023-11-03")

    def test_amendment_and_restatement_are_both_retained(self) -> None:
        filings, facts = normalized_fixture()
        self.assertEqual({row["form"] for row in filings}, {"10-K", "10-K/A", "10-Q"})
        values = {
            row["value"] for row in facts
            if row["tag"] == "Revenues" and row["unit"] == "USD"
        }
        self.assertEqual(values, {"383285000000.123456789", "383284999999.123456789"})

    def test_main_and_historical_submissions_merge_and_deduplicate(self) -> None:
        filings, _ = normalized_fixture()
        self.assertEqual(len(filings), 3)
        original = next(row for row in filings if row["accession_number"] == "0000320193-23-000106")
        self.assertEqual(original["primary_document"], "aapl-20230930.htm")

    def test_fact_rows_have_unique_ids_and_stable_sort(self) -> None:
        _, facts = normalized_fixture()
        self.assertEqual(facts, sorted(facts, key=fact_sort_key))
        self.assertEqual(len({row["row_id"] for row in facts}), len(facts))

    def test_incremental_repeat_is_byte_stable_and_has_no_duplicates(self) -> None:
        filings, facts = normalized_fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_symbol_atomic(root, ENTRY.symbol, filings, facts)
            before_filings = filings_path(root, ENTRY.symbol).read_bytes()
            before_facts = facts_path(root, ENTRY.symbol).read_bytes()
            newer_filings = [dict(row, retrieved_at="2026-09-01T00:00:00+00:00") for row in filings]
            newer_facts = [dict(row, retrieved_at="2026-09-01T00:00:00+00:00") for row in facts]
            from kolmo.fundamental.sec_edgar import merge_filings
            write_symbol_atomic(root, ENTRY.symbol, merge_filings(filings, newer_filings), merge_facts(facts, newer_facts))
            self.assertEqual(filings_path(root, ENTRY.symbol).read_bytes(), before_filings)
            self.assertEqual(facts_path(root, ENTRY.symbol).read_bytes(), before_facts)

    def test_failed_symbol_does_not_overwrite_old_canonical_files(self) -> None:
        filings, facts = normalized_fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "canonical"
            write_symbol_atomic(output, ENTRY.symbol, filings, facts)
            before = (filings_path(output, ENTRY.symbol).read_bytes(), facts_path(output, ENTRY.symbol).read_bytes())
            result = fetch_one(ENTRY, output, root / "raw", FakeClient(ENTRY.cik), RETRIEVED, False)
            self.assertEqual(result.status, "failed")
            self.assertEqual(before[0], filings_path(output, ENTRY.symbol).read_bytes())
            self.assertEqual(before[1], facts_path(output, ENTRY.symbol).read_bytes())

    def test_one_symbol_failure_does_not_stop_other_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entries = [ENTRY, SecUniverseEntry("MSFT", "0000789019")]
            results = run_fetch(entries, root / "canonical", root / "raw", FakeClient("0000789019"), RETRIEVED, False, 2)
            self.assertEqual({item.symbol: item.status for item in results}, {"AAPL": "updated", "MSFT": "failed"})
            self.assertTrue(facts_path(root / "canonical", "AAPL").is_file())

    def test_http_429_and_5xx_are_retried_with_a_bound(self) -> None:
        attempts = []
        responses = [
            urllib.error.HTTPError("https://example", 429, "rate", {}, None),
            urllib.error.HTTPError("https://example", 503, "down", {}, None),
            FakeResponse(b"{}"),
        ]

        def opener(request, timeout):
            attempts.append((request, timeout))
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        client = SecHttpClient(
            "Kolmo test test@example.com", 5, 3, 2, opener=opener,
            sleeper=lambda _: None, limiter=GlobalRateLimiter(10, sleeper=lambda _: None),
        )
        self.assertEqual(client.get("https://example").status, 200)
        self.assertEqual(len(attempts), 3)

    def test_global_limiter_never_exceeds_configured_rate(self) -> None:
        now = [0.0]
        waits: list[float] = []

        def sleep(seconds: float) -> None:
            waits.append(seconds)
            now[0] += seconds

        limiter = GlobalRateLimiter(5, clock=lambda: now[0], sleeper=sleep)
        limiter.wait()
        limiter.wait()
        limiter.wait()
        self.assertEqual(waits, [0.2, 0.2])
        with self.assertRaises(ValueError):
            GlobalRateLimiter(10.01)

    def test_unchanged_raw_response_does_not_create_another_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, created_first = write_raw_snapshot_if_changed(root, "companyfacts", ENTRY.cik, RETRIEVED, b"same")
            second, created_second = write_raw_snapshot_if_changed(root, "companyfacts", ENTRY.cik, "2026-09-01T00:00:00+00:00", b"same")
            self.assertTrue(created_first)
            self.assertFalse(created_second)
            self.assertEqual(first, second)

    def test_atomic_deterministic_gzip_has_no_temporary_files(self) -> None:
        filings, facts = normalized_fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_symbol_atomic(root, ENTRY.symbol, filings, facts)
            first = facts_path(root, ENTRY.symbol).read_bytes()
            write_symbol_atomic(root, ENTRY.symbol, filings, facts)
            self.assertEqual(first, facts_path(root, ENTRY.symbol).read_bytes())
            self.assertFalse(list(root.rglob("*.tmp")))

    def test_manifest_records_partial_failure_and_nonzero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            universe = root / "universe.csv"
            universe.write_text(
                "symbol,asset_type,cik,enabled\nAAPL,STK,320193,1\nMSFT,STK,789019,1\nSPY,ETF,,1\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"KOLMO_DATA_ROOT": str(root), "SEC_USER_AGENT": "Kolmo test test@example.com"}, clear=False), patch(
                "kolmo.fundamental.fetch_sec_edgar.SecHttpClient", return_value=FakeClient("0000789019")
            ):
                code = fetch_main(["--universe", str(universe), "--output-root", str(root / "canonical")])
            self.assertEqual(code, 1)
            manifests = list((root / "canonical" / "_meta" / "fetch_runs").glob("*.json"))
            payload = json.loads(manifests[0].read_text(encoding="utf-8"))
            self.assertTrue(payload["user_agent_present"])
            self.assertEqual(payload["status"], "completed_with_failures")
            self.assertEqual({item["status"] for item in payload["results"]}, {"updated", "failed", "skipped"})
            self.assertNotIn("test@example.com", json.dumps(payload))

    def test_validator_returns_failure_for_corrupt_gzip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            filings_path(root, ENTRY.symbol).parent.mkdir(parents=True)
            filings_path(root, ENTRY.symbol).write_bytes(b"not gzip")
            facts_path(root, ENTRY.symbol).parent.mkdir(parents=True)
            facts_path(root, ENTRY.symbol).write_bytes(b"not gzip")
            report = validate_sec_files([ENTRY], root)
            self.assertFalse(report["ok"])
            self.assertEqual(report["symbols_failed"], 1)

    def test_validator_warns_for_unlinked_fact_accession(self) -> None:
        filings, facts = normalized_fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_symbol_atomic(root, ENTRY.symbol, filings[:1], facts)
            result = validate_symbol(root, ENTRY)
            self.assertTrue(result["warnings"])

    def test_missing_user_agent_fails_clearly_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            universe = Path(directory) / "universe.csv"
            universe.write_text("symbol,asset_type,cik,enabled\nAAPL,STK,320193,1\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(fetch_main(["--universe", str(universe), "--output-root", str(Path(directory) / "out")]), 2)


if __name__ == "__main__":
    unittest.main()
