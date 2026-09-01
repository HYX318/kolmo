import csv
import gzip
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from kolmo.ashare import update_cn_profile_daily
from kolmo.ashare.fetch_baostock_daily import (
    BaoStockDeadlineError,
    StockInfo,
    hard_deadline,
    login_baostock_with_retry,
    load_universe,
    raw_cache_covers_requested_end,
    read_universe,
    rows_from_result,
    run_with_reconnect,
    write_universe,
)
from kolmo.ashare.update_cn_profile_daily import merge_raw_cache
from kolmo.scheduler.process_timeout import TIMEOUT_EXIT_CODE, run_with_timeout


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
                partition_on_fetch_failure=False,
                exchange_timeout_seconds=30.0,
            )
            captured: dict[str, list[str]] = {}
            with patch.object(
                update_cn_profile_daily,
                "run",
                side_effect=lambda command, cwd, timeout: captured.setdefault("command", command) and 0,
            ), patch.object(update_cn_profile_daily, "sync_incremental_raw_cache", return_value=2):
                self.assertEqual(update_cn_profile_daily.update_exchange(args, root, "sz"), 0)

            command = captured["command"]
            self.assertIn("kolmo.ashare.fetch_baostock_daily", command)
            self.assertIn("--no-combine", command)
            self.assertNotIn("kolmo.ashare.build_cn_profile_daily", command)

    def test_profile_update_does_not_enable_partial_partition_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "profile"
            latest = output_dir / "2026" / "07" / "20260707.csv"
            latest.parent.mkdir(parents=True)
            latest.touch()
            args = update_cn_profile_daily.argparse.Namespace(
                target="profile", output_dir=str(output_dir), end_date="20260718",
                lookback_days=10, adjust="qfq", sleep=0.0, workers=4,
                refresh_raw=False, raw_window="", limit=0, no_include_delisted=False,
                full_start_date="20170101", partition_on_fetch_failure=False,
                exchange_timeout_seconds=30.0,
            )
            captured = {}
            with patch.object(
                update_cn_profile_daily,
                "run",
                side_effect=lambda command, cwd, timeout: captured.setdefault("command", command) and 0,
            ):
                self.assertEqual(update_cn_profile_daily.update_exchange(args, root, "sz"), 0)
            self.assertNotIn("--partition-on-fetch-failure", captured["command"])


class RowsFromResultTest(unittest.TestCase):
    def test_initial_login_uses_finite_retry(self) -> None:
        class LoginResult:
            error_code = "0"
            error_msg = ""

        class FlakyBaoStock:
            attempts = 0

            @classmethod
            def login(cls):
                cls.attempts += 1
                if cls.attempts == 1:
                    raise OSError("temporary network failure")
                return LoginResult()

        login_baostock_with_retry(FlakyBaoStock(), 0.1, retries=1, retry_backoff_seconds=0)
        self.assertEqual(FlakyBaoStock.attempts, 2)

    def test_resume_cache_must_cover_requested_end_date(self) -> None:
        stock = StockInfo(
            code="000001", bs_code="sz.000001", symbol="000001.SZ", name="Ping An",
            listing_date="1991-04-03", delisting_date="", status="active",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "000001.SZ.csv.gz"
            write_rows(path, [{"date": "2026-08-28", "code": "sz.000001", "close": "10"}])
            self.assertFalse(raw_cache_covers_requested_end(path, stock, "2026-08-31"))
            write_rows(path, [{"date": "2026-08-31", "code": "sz.000001", "close": "10"}])
            self.assertTrue(raw_cache_covers_requested_end(path, stock, "2026-08-31"))

    def test_resume_cache_uses_delisting_date_as_expected_end(self) -> None:
        stock = StockInfo(
            code="000001", bs_code="sz.000001", symbol="000001.SZ", name="Old",
            listing_date="1991-04-03", delisting_date="2026-08-28", status="delisted",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "000001.SZ.csv.gz"
            write_rows(path, [{"date": "2026-08-28", "code": "sz.000001", "close": "10"}])
            self.assertTrue(raw_cache_covers_requested_end(path, stock, "2026-08-31"))

    def test_cached_universe_round_trip_validates_exchange(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "universe.csv"
            stock = StockInfo(
                code="000001", bs_code="sz.000001", symbol="000001.SZ", name="Ping An",
                listing_date="1991-04-03", delisting_date="", status="active",
            )
            write_universe(path, [stock])
            self.assertEqual(read_universe(path, "sz"), [stock])
            with self.assertRaisesRegex(ValueError, "empty or has duplicate"):
                read_universe(path, "sh")

    def test_empty_universe_is_not_treated_as_success(self) -> None:
        class EmptyResult:
            error_code = "0"
            error_msg = ""
            fields = ["code"]

            def next(self) -> bool:
                return False

        class FakeBaoStock:
            @staticmethod
            def query_stock_basic():
                return EmptyResult()

        with self.assertRaisesRegex(RuntimeError, "empty sz stock universe"):
            load_universe(FakeBaoStock(), "2026-08-01", "2026-08-31", True, "sz")

    def test_row_limit_stops_malformed_unbounded_result(self) -> None:
        class EndlessResult:
            def next(self) -> bool:
                return True

            def get_row_data(self) -> list[str]:
                return ["repeat"]

        with self.assertRaisesRegex(RuntimeError, "exceeded 3 rows"):
            rows_from_result(EndlessResult(), max_rows=3)

    def test_hard_deadline_interrupts_busy_loop(self) -> None:
        with self.assertRaises(BaoStockDeadlineError):
            with hard_deadline(0.01):
                while True:
                    pass

    def test_hard_deadline_interrupts_baostock_empty_recv_loop(self) -> None:
        import baostock.common.context as context
        import baostock.util.socketutil as socketutil

        class ClosedPeerSocket:
            def send(self, _payload) -> None:
                return None

            def recv(self, _size) -> bytes:
                return b""

        previous_socket = getattr(context, "default_socket", None)
        context.default_socket = ClosedPeerSocket()
        started = time.monotonic()
        try:
            with self.assertRaises(BaoStockDeadlineError):
                with hard_deadline(0.01):
                    self.assertIsNone(socketutil.send_msg("test"))
        finally:
            context.default_socket = previous_socket
        self.assertLess(time.monotonic() - started, 0.5)

    def test_failed_operation_reconnects_and_retries(self) -> None:
        class LoginResult:
            error_code = "0"
            error_msg = ""

        class FakeBaoStock:
            def __init__(self) -> None:
                self.logins = 0

            def login(self):
                self.logins += 1
                return LoginResult()

        attempts = 0

        def operation() -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("connection lost")
            return "ok"

        bs = FakeBaoStock()
        result = run_with_reconnect(
            bs,
            operation,
            context="test operation",
            timeout_seconds=1,
            retries=1,
            retry_backoff_seconds=0,
        )
        self.assertEqual(result, "ok")
        self.assertEqual(attempts, 2)
        self.assertEqual(bs.logins, 1)


class ProcessTimeoutTest(unittest.TestCase):
    def test_timeout_terminates_subprocess_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            code = run_with_timeout(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                cwd=Path(directory),
                timeout_seconds=0.05,
                terminate_grace_seconds=0.1,
                label="test process",
            )
        self.assertEqual(code, TIMEOUT_EXIT_CODE)
