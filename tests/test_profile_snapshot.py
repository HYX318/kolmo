import csv
import io
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from kolmo.catalog.profile_snapshot import (
    PROFILE_COLUMNS,
    ProfileSnapshotCatalog,
    ProfileSnapshotError,
    main,
)


def profile_row(
    trade_date: str,
    exchange: str,
    symbol: str,
    close: str = "10",
    **updates: str,
) -> dict:
    row = {
        "date": trade_date,
        "symbol": symbol,
        "exchange": exchange.upper(),
        "board": "main",
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "preclose": close,
        "volume": "100",
        "volume_unit": "share",
        "amount": "1000",
        "turnover_rate": "0.1",
        "pct_change": "0",
        "pe_ttm": "10",
        "pb_mrq": "1",
        "ps_ttm": "2",
        "pcf_ncf_ttm": "3",
        "trade_status": "1",
        "is_st": "0",
        "adjust": "qfq",
        "source": "test",
    }
    row.update(updates)
    return row


def write_profile(
    catalog: ProfileSnapshotCatalog,
    trade_date: str,
    exchange: str,
    *,
    close: str = "10",
    duplicate: bool = False,
) -> Path:
    symbol = "000001.SZ" if exchange == "sz" else "600000.SH"
    path = catalog.profile_path(exchange, trade_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [profile_row(trade_date, exchange, symbol, close)]
    if duplicate:
        rows.append(dict(rows[0]))
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=PROFILE_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return path


def write_day(catalog: ProfileSnapshotCatalog, trade_date: str, close: str = "10") -> None:
    for exchange in ("sz", "sh"):
        write_profile(catalog, trade_date, exchange, close=close)


def replace_profile(
    catalog: ProfileSnapshotCatalog,
    trade_date: str,
    exchange: str,
    *,
    close: str,
) -> Path:
    source_path = catalog.profile_path(exchange, trade_date)
    replacement = source_path.with_name(f".{source_path.name}.replacement")
    symbol = "000001.SZ" if exchange == "sz" else "600000.SH"
    with replacement.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=PROFILE_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerow(profile_row(trade_date, exchange, symbol, close))
    os.replace(replacement, source_path)
    return source_path


def write_failure_manifest(
    catalog: ProfileSnapshotCatalog,
    trade_date: str,
    exchange: str,
    *,
    failed: bool,
) -> Path:
    path = (
        catalog.data_root
        / "raw"
        / "ashare"
        / "baostock"
        / exchange
        / f"failures_{exchange}_daily_{trade_date}.csv"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "symbol,status,error\n"
    if failed:
        content += "000001.SZ,active,upstream timeout\n"
    path.write_text(content, encoding="utf-8")
    return path


class ProfileSnapshotCatalogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.catalog = ProfileSnapshotCatalog(self.root)

    def test_publish_writes_manifest_objects_and_current(self) -> None:
        write_day(self.catalog, "20260810")
        manifest = self.catalog.publish(dates=["20260810"])

        snapshot_id = str(manifest["snapshot_id"])
        self.assertEqual(self.catalog.resolve_current(), snapshot_id)
        self.assertEqual(manifest["schema_version"], "1.0.0")
        self.assertEqual(manifest["as_of"], "20260810")
        self.assertEqual(manifest["adjustments"], ["qfq"])
        self.assertIs(manifest["research_only"], True)
        self.assertEqual(
            {
                result["code"]
                for result in manifest["quality_results"]
                if result["level"] == "RESEARCH_ONLY"
            },
            {
                "adjusted_price_research_only",
                "exchange_calendar_unverified",
                "fetch_failure_evidence_missing",
            },
        )
        self.assertEqual(len(manifest["partitions"]), 2)
        self.assertTrue(manifest["created_at"])
        self.assertTrue(manifest["quality_results"])
        self.assertIn(
            "fetch_failure_evidence_missing",
            {result["code"] for result in manifest["quality_results"]},
        )
        self.assertIn(
            "exchange_calendar_unverified",
            {result["code"] for result in manifest["quality_results"]},
        )

        verification = self.catalog.verify_snapshot(snapshot_id)
        self.assertEqual(verification["snapshot_id"], snapshot_id)
        for partition in manifest["partitions"]:
            object_path = self.root / str(partition["object_path"])
            source_path = self.root / str(partition["source_path"])
            self.assertTrue(object_path.is_file())
            self.assertEqual(object_path.stat().st_mode & 0o222, 0)
            self.assertEqual(os.stat(object_path).st_ino, os.stat(source_path).st_ino)

    def test_suspended_blank_activity_fields_are_allowed(self) -> None:
        for exchange in ("sz", "sh"):
            symbol = "000001.SZ" if exchange == "sz" else "600000.SH"
            path = self.catalog.profile_path(exchange, "20260810")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8", newline="") as output:
                writer = csv.DictWriter(output, fieldnames=PROFILE_COLUMNS, lineterminator="\n")
                writer.writeheader()
                writer.writerow(
                    profile_row(
                        "20260810", exchange, symbol,
                        trade_status="0", volume="", amount="",
                        turnover_rate="", pct_change="",
                    )
                )

        manifest = self.catalog.publish(dates=["20260810"])

        self.assertEqual(manifest["as_of"], "20260810")

    def test_tradable_blank_activity_field_is_blocked(self) -> None:
        write_day(self.catalog, "20260810")
        path = self.catalog.profile_path("sz", "20260810")
        with path.open(encoding="utf-8", newline="") as source:
            rows = list(csv.DictReader(source))
        rows[0]["volume"] = ""
        with path.open("w", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=PROFILE_COLUMNS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)

        with self.assertRaisesRegex(ProfileSnapshotError, "volume must be numeric"):
            self.catalog.publish(dates=["20260810"])

    def test_missing_sh_keeps_current_unchanged(self) -> None:
        write_day(self.catalog, "20260810")
        original_id = str(self.catalog.publish(dates=["20260810"])["snapshot_id"])
        write_profile(self.catalog, "20260811", "sz")

        with self.assertRaisesRegex(ProfileSnapshotError, "SH profile partition is missing"):
            self.catalog.publish(dates=["20260811"])
        self.assertEqual(self.catalog.resolve_current(), original_id)

    def test_failure_manifest_with_rows_blocks_and_keeps_current(self) -> None:
        write_day(self.catalog, "20260810")
        original_id = str(self.catalog.publish(dates=["20260810"])["snapshot_id"])
        write_day(self.catalog, "20260811")
        write_failure_manifest(self.catalog, "20260811", "sh", failed=True)

        with self.assertRaisesRegex(ProfileSnapshotError, "failure manifest has 1 data rows"):
            self.catalog.publish(latest_days=1)
        self.assertEqual(self.catalog.resolve_current(), original_id)

    def test_latest_days_uses_exchange_intersection(self) -> None:
        write_day(self.catalog, "20260810")
        write_profile(self.catalog, "20260811", "sz")

        manifest = self.catalog.publish(latest_days=1)
        self.assertEqual(manifest["as_of"], "20260810")
        self.assertEqual({item["date"] for item in manifest["partitions"]}, {"20260810"})

    def test_latest_days_requires_requested_count_and_explicit_dates_are_contiguous(self) -> None:
        write_day(self.catalog, "20260810")
        with self.assertRaisesRegex(ProfileSnapshotError, "only 1 complete"):
            self.catalog.publish(latest_days=2)
        write_day(self.catalog, "20260811")
        write_day(self.catalog, "20260812")
        with self.assertRaisesRegex(ProfileSnapshotError, "not contiguous"):
            self.catalog.publish(dates=["20260810", "20260812"])

    def test_existing_empty_failure_manifest_is_pass_evidence(self) -> None:
        write_day(self.catalog, "20260810")
        for exchange in ("sz", "sh"):
            write_failure_manifest(self.catalog, "20260810", exchange, failed=False)

        manifest = self.catalog.publish(dates=["20260810"])

        results = [
            item for item in manifest["quality_results"]
            if item["code"] == "no_fetch_failures"
        ]
        self.assertEqual(len(results), 2)
        self.assertTrue(all(item["level"] == "PASS" for item in results))

    def test_malformed_failure_manifest_is_blocked(self) -> None:
        write_day(self.catalog, "20260810")
        path = write_failure_manifest(self.catalog, "20260810", "sz", failed=False)
        path.write_text("symbol,error\n", encoding="utf-8")

        with self.assertRaisesRegex(ProfileSnapshotError, "failure manifest schema"):
            self.catalog.publish(dates=["20260810"])

    def test_zero_byte_failure_manifest_is_missing_evidence(self) -> None:
        write_day(self.catalog, "20260810")
        path = write_failure_manifest(self.catalog, "20260810", "sz", failed=False)
        path.write_bytes(b"")

        manifest = self.catalog.publish(dates=["20260810"])

        self.assertIn(
            "fetch_failure_evidence_missing",
            {item["code"] for item in manifest["quality_results"]},
        )

    def test_publish_without_promotion_preserves_current(self) -> None:
        write_day(self.catalog, "20260810")
        current = str(self.catalog.publish(dates=["20260810"])["snapshot_id"])
        write_day(self.catalog, "20260811", close="11")

        historical = self.catalog.publish(dates=["20260811"], promote=False)

        self.assertNotEqual(historical["snapshot_id"], current)
        self.assertEqual(self.catalog.resolve_current(), current)

    def test_object_tampering_is_detected(self) -> None:
        write_day(self.catalog, "20260810")
        manifest = self.catalog.publish(dates=["20260810"])
        object_path = self.root / str(manifest["partitions"][0]["object_path"])
        object_path.chmod(0o644)
        object_path.write_text("tampered\n", encoding="utf-8")

        with self.assertRaisesRegex(ProfileSnapshotError, "hash mismatch"):
            self.catalog.verify_snapshot(str(manifest["snapshot_id"]))

    def test_manifest_tampering_is_detected(self) -> None:
        write_day(self.catalog, "20260810")
        manifest = self.catalog.publish(dates=["20260810"])
        snapshot_id = str(manifest["snapshot_id"])
        manifest_path = self.catalog.snapshot_root / snapshot_id / "manifest.json"
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["as_of"] = "20260809"
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaises(ProfileSnapshotError):
            self.catalog.load_manifest(snapshot_id)

    def test_created_at_tampering_is_detected(self) -> None:
        write_day(self.catalog, "20260810")
        manifest = self.catalog.publish(dates=["20260810"])
        snapshot_id = str(manifest["snapshot_id"])
        manifest_path = self.catalog.snapshot_root / snapshot_id / "manifest.json"
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["created_at"] = "2099-01-01T00:00:00+00:00"
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaisesRegex(ProfileSnapshotError, "manifest hash mismatch"):
            self.catalog.load_manifest(snapshot_id)

    def test_same_content_is_idempotent(self) -> None:
        write_day(self.catalog, "20260810")
        first = self.catalog.publish(dates=["20260810"])
        manifest_path = (
            self.catalog.snapshot_root / str(first["snapshot_id"]) / "manifest.json"
        )
        before = manifest_path.read_bytes()

        second = self.catalog.publish(dates=["20260810"])

        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(first["created_at"], second["created_at"])
        self.assertEqual(manifest_path.read_bytes(), before)

    def test_old_snapshot_survives_source_replace_and_new_publish(self) -> None:
        source_path = write_profile(self.catalog, "20260810", "sz", close="10")
        write_profile(self.catalog, "20260810", "sh", close="10")
        first = self.catalog.publish(dates=["20260810"])
        first_id = str(first["snapshot_id"])
        old_object = next(
            self.root / str(item["object_path"])
            for item in first["partitions"]
            if item["exchange"] == "sz"
        )
        old_inode = os.stat(old_object).st_ino

        replace_profile(self.catalog, "20260810", "sz", close="11")
        replace_profile(self.catalog, "20260810", "sh", close="11")
        second = self.catalog.publish(dates=["20260810"])

        self.assertNotEqual(first_id, second["snapshot_id"])
        self.assertNotEqual(old_inode, os.stat(source_path).st_ino)
        self.catalog.verify_snapshot(first_id)
        self.catalog.verify_snapshot(str(second["snapshot_id"]))
        self.assertTrue((self.catalog.snapshot_root / first_id / "manifest.json").is_file())
        with old_object.open("r", encoding="utf-8", newline="") as source:
            self.assertEqual(next(csv.DictReader(source))["close"], "10")

    def test_invalid_profile_contract_fails_before_current_update(self) -> None:
        write_day(self.catalog, "20260810")
        current = str(self.catalog.publish(dates=["20260810"])["snapshot_id"])
        write_profile(self.catalog, "20260811", "sz", duplicate=True)
        write_profile(self.catalog, "20260811", "sh")

        with self.assertRaisesRegex(ProfileSnapshotError, "duplicate profile symbol"):
            self.catalog.publish(dates=["20260811"])
        self.assertEqual(self.catalog.resolve_current(), current)

    def test_non_qfq_and_mixed_adjustments_are_rejected(self) -> None:
        for adjustment in ("raw", "hfq"):
            with self.subTest(adjustment=adjustment):
                trade_date = "20260810"
                for exchange in ("sz", "sh"):
                    path = self.catalog.profile_path(exchange, trade_date)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    symbol = "000001.SZ" if exchange == "sz" else "600000.SH"
                    with path.open("w", encoding="utf-8", newline="") as output:
                        writer = csv.DictWriter(
                            output, fieldnames=PROFILE_COLUMNS, lineterminator="\n"
                        )
                        writer.writeheader()
                        writer.writerow(
                            profile_row(
                                trade_date, exchange, symbol, adjust=adjustment
                            )
                        )
                with self.assertRaisesRegex(
                    ProfileSnapshotError, "requires uniform qfq adjustment"
                ):
                    self.catalog.publish(dates=[trade_date])

        trade_date = "20260811"
        sz_path = self.catalog.profile_path("sz", trade_date)
        sz_path.parent.mkdir(parents=True, exist_ok=True)
        with sz_path.open("w", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=PROFILE_COLUMNS, lineterminator="\n")
            writer.writeheader()
            writer.writerow(profile_row(trade_date, "sz", "000001.SZ"))
            writer.writerow(
                profile_row(trade_date, "sz", "000002.SZ", adjust="raw")
            )
        write_profile(self.catalog, trade_date, "sh")
        with self.assertRaisesRegex(ProfileSnapshotError, "requires uniform qfq adjustment"):
            self.catalog.publish(dates=[trade_date])

    def test_price_validation_and_suspended_zero_ohlc(self) -> None:
        invalid_rows = (
            {"open": "nan"},
            {"high": "inf"},
            {"preclose": "0"},
            {"open": "0", "low": "0"},
        )
        for index, updates in enumerate(invalid_rows, start=10):
            with self.subTest(updates=updates):
                trade_date = f"202608{index:02d}"
                sz_path = self.catalog.profile_path("sz", trade_date)
                sz_path.parent.mkdir(parents=True, exist_ok=True)
                with sz_path.open("w", encoding="utf-8", newline="") as output:
                    writer = csv.DictWriter(
                        output, fieldnames=PROFILE_COLUMNS, lineterminator="\n"
                    )
                    writer.writeheader()
                    writer.writerow(
                        profile_row(trade_date, "sz", "000001.SZ", **updates)
                    )
                write_profile(self.catalog, trade_date, "sh")
                with self.assertRaises(ProfileSnapshotError):
                    self.catalog.publish(dates=[trade_date])

        suspended_date = "20260820"
        suspended_path = self.catalog.profile_path("sz", suspended_date)
        suspended_path.parent.mkdir(parents=True, exist_ok=True)
        with suspended_path.open("w", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=PROFILE_COLUMNS, lineterminator="\n")
            writer.writeheader()
            writer.writerow(
                profile_row(
                    suspended_date,
                    "sz",
                    "000001.SZ",
                    open="0",
                    high="0",
                    low="0",
                    close="0",
                    preclose="10",
                    trade_status="0",
                )
            )
        write_profile(self.catalog, suspended_date, "sh")
        manifest = self.catalog.publish(dates=[suspended_date])
        self.catalog.verify_snapshot(str(manifest["snapshot_id"]))

    def test_cli_publish_current_and_verify_are_scriptable(self) -> None:
        write_day(self.catalog, "20260810")
        publish_output = io.StringIO()
        self.assertEqual(
            main(
                ["--data-root", str(self.root), "publish", "--latest-days", "1"],
                stdout=publish_output,
            ),
            0,
        )
        snapshot_id = json.loads(publish_output.getvalue())["snapshot_id"]

        current_output = io.StringIO()
        self.assertEqual(
            main(["--data-root", str(self.root), "current"], stdout=current_output),
            0,
        )
        self.assertEqual(current_output.getvalue(), f"{snapshot_id}\n")

        verify_output = io.StringIO()
        self.assertEqual(
            main(["--data-root", str(self.root), "verify"], stdout=verify_output),
            0,
        )
        self.assertEqual(json.loads(verify_output.getvalue())["snapshot_id"], snapshot_id)

    def test_unknown_snapshot_fails_fast(self) -> None:
        with self.assertRaisesRegex(ProfileSnapshotError, "unknown profile snapshot"):
            self.catalog.load_manifest("0" * 64)


if __name__ == "__main__":
    unittest.main()
