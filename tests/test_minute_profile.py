import argparse
import io
import tempfile
import unittest
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pandas as pd

from kolmo.ashare.build_vendor_minute_profile import build_profile
from kolmo.ashare.build_vendor_minute_history import run_history
from kolmo.ashare.minute_corrections import (
    apply_vendor_corrections,
    exclude_zero_price_activity_days,
    observe_post_close_adjustments,
)
from kolmo.ashare.minute_product import (
    MINUTE_BAR_COLUMNS,
    aggregate_bars,
    expected_bars_per_day,
    normalize_vendor_bars,
    validate_canonical_bars,
)
from kolmo.ashare.vendor_minute_delisted import VendorDelistedMinuteDataset
from kolmo.validation.minute_profile import validate_build


def vendor_bars(year: int = 2010) -> pd.DataFrame:
    times = ["09:30", "09:31", "09:32", "09:33", "09:34", "09:35"]
    return pd.DataFrame({
        "ts_code": ["600519.SH"] * len(times),
        "freq": ["1min"] * len(times),
        "trade_time": pd.to_datetime(
            [f"{year}-01-04 {time}" for time in times]
        ).tz_localize("Asia/Shanghai"),
        "open": [10, 10, 11, 12, 11, 13], "close": [10, 11, 12, 11, 13, 14],
        "high": [10, 11, 12, 12, 13, 14], "low": [10, 10, 11, 11, 11, 13],
        "vol": [1, 2, 3, 4, 5, 6], "amount": [10, 21, 35, 46, 60, 80],
    })


class MinuteProductTest(unittest.TestCase):
    def test_applies_only_registered_vendor_correction(self) -> None:
        frame = normalize_vendor_bars(vendor_bars(), "600519.SH")
        frame["symbol"] = "601005.SH"
        frame["trade_time"] = frame["trade_time"].map(
            lambda value: value.replace(year=2011, month=4, day=26, hour=10, minute=31)
        )
        frame.loc[:, "low"] = 0.0
        corrected, records = apply_vendor_corrections(frame.iloc[:1], "601005.SH")
        self.assertEqual(corrected.iloc[0]["low"], 4.87)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["original_value"], 0.0)
        self.assertFalse(records[0]["inferred"])

    def test_marks_unregistered_zero_low_as_inferred(self) -> None:
        frame = normalize_vendor_bars(vendor_bars(), "600519.SH").iloc[:1].copy()
        frame.loc[:, "low"] = 0.0
        corrected, records = apply_vendor_corrections(frame, "600519.SH")
        self.assertEqual(corrected.iloc[0]["low"], 10.0)
        self.assertEqual(records[0]["evidence"], "structural_ohlc_lower_bound")
        self.assertTrue(records[0]["inferred"])

    def test_repairs_ohlc_envelope_and_extreme_high(self) -> None:
        frame = normalize_vendor_bars(vendor_bars(), "600519.SH").iloc[:2].copy()
        frame.iloc[0, frame.columns.get_loc("low")] = 11.0
        frame.iloc[0, frame.columns.get_loc("close")] = 9.0
        frame.iloc[1, frame.columns.get_loc("high")] = 18459.89
        corrected, records = apply_vendor_corrections(frame, "600519.SH")
        self.assertEqual(corrected.iloc[0]["low"], 9.0)
        self.assertEqual(corrected.iloc[1]["high"], 11.0)
        self.assertEqual({record["field"] for record in records}, {"low", "high"})
        self.assertTrue(all(record["inferred"] for record in records))

    def test_reads_delisted_symbol_from_nested_zip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "退市股票" / "2010-2025" / "1min"
            folder.mkdir(parents=True)
            parquet = io.BytesIO()
            vendor_bars().to_parquet(parquet, index=False)
            inner_payload = io.BytesIO()
            with ZipFile(inner_payload, "w", ZIP_DEFLATED) as inner:
                inner.writestr("2010/600519.SH.parquet", parquet.getvalue())
            path = folder / "A股退市股票1分钟历史行情_2010-2025.zip"
            with ZipFile(path, "w", ZIP_DEFLATED) as outer:
                outer.writestr("退市股票数据/600519.SH.zip", inner_payload.getvalue())
            with VendorDelistedMinuteDataset.discover(root, 2010, 1) as dataset:
                result = dataset.read_symbol("600519.SH")
            self.assertEqual(len(result), 6)
            self.assertEqual(str(result.trade_time.dt.tz), "Asia/Shanghai")

    def test_canonical_normalization_and_derivation(self) -> None:
        canonical = normalize_vendor_bars(vendor_bars(), "600519.SH")
        self.assertEqual(tuple(canonical.columns), MINUTE_BAR_COLUMNS)
        result = aggregate_bars(canonical, 5)
        self.assertEqual(list(result.trade_time.dt.strftime("%H:%M")), ["09:30", "09:35"])
        self.assertEqual(result.iloc[1].volume, 20)

    def test_beijing_post_close_session_is_separate(self) -> None:
        times = ["15:00", *[f"15:{minute:02d}" for minute in range(1, 31)]]
        vendor = pd.DataFrame({
            "ts_code": ["920000.BJ"] * len(times),
            "freq": ["1min"] * len(times),
            "trade_time": pd.to_datetime(
                [f"2022-07-15 {time}" for time in times]
            ).tz_localize("Asia/Shanghai"),
            "open": [5.3] * len(times), "high": [5.3] * len(times),
            "low": [5.3] * len(times), "close": [5.3] * len(times),
            "vol": [1, *([0] * 29), 100], "amount": [5.3, *([0.0] * 29), 530.0],
        })
        canonical = normalize_vendor_bars(vendor, "920000.BJ")
        validate_canonical_bars(canonical, 1)
        result = aggregate_bars(canonical, 5)
        validate_canonical_bars(result, 5)
        self.assertEqual(
            result.trade_time.dt.strftime("%H:%M").tolist(),
            ["15:00", "15:05", "15:10", "15:15", "15:20", "15:25", "15:30"],
        )
        self.assertEqual(result.iloc[0].volume, 1)
        self.assertEqual(result.iloc[-1].volume, 100)
        self.assertEqual(expected_bars_per_day(1, "bj"), 271)
        self.assertEqual(expected_bars_per_day(60, "bj"), 6)

    def test_excludes_complete_zero_price_activity_day(self) -> None:
        frame = normalize_vendor_bars(vendor_bars(), "600519.SH")
        frame.loc[:, ["open", "high", "low", "close", "volume", "amount"]] = 0
        filtered, records = exclude_zero_price_activity_days(frame, "600519.SH")
        self.assertTrue(filtered.empty)
        self.assertEqual(records[0]["excluded_rows"], 6)
        self.assertEqual(records[0]["dates"], ["2010-01-04"])

    def test_allows_and_observes_only_beijing_post_close_adjustment(self) -> None:
        frame = normalize_vendor_bars(vendor_bars(), "600519.SH").iloc[:1].copy()
        frame["symbol"] = "920000.BJ"
        frame["trade_time"] = pd.to_datetime(["2023-07-11 15:30"]).tz_localize("Asia/Shanghai")
        frame["volume"] = -100
        frame["amount"] = -500.0
        validate_canonical_bars(frame, 1)
        observations = observe_post_close_adjustments(frame, "920000.BJ")
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["volume"], -100)
        regular = frame.copy()
        regular["trade_time"] = pd.to_datetime(["2023-07-11 15:00"]).tz_localize("Asia/Shanghai")
        with self.assertRaisesRegex(ValueError, "negative volume or amount"):
            validate_canonical_bars(regular, 1)

    def test_staged_builder_writes_daily_frequency_partitions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source" / "2010"
            source.mkdir(parents=True)
            payload = io.BytesIO()
            vendor_bars().to_parquet(payload, index=False)
            with ZipFile(source / "A股1分钟历史行情_2010.zip", "w", ZIP_DEFLATED) as archive:
                archive.writestr("2010/1分钟/600519.SH.parquet", payload.getvalue())
                beijing = vendor_bars().copy()
                beijing["ts_code"] = "920000.BJ"
                beijing["trade_time"] = beijing["trade_time"].map(
                    lambda value: value.replace(month=7, day=15)
                )
                beijing_payload = io.BytesIO()
                beijing.to_parquet(beijing_payload, index=False)
                archive.writestr("2010/1分钟/920000.BJ.parquet", beijing_payload.getvalue())
            output = root / "output"
            args = argparse.Namespace(
                year=2010, start_date="2010-01-04", end_date="2010-07-15",
                source_root=root / "source", output_dir=output, build_id="test-build",
                include_delisted=False, batch_symbols=1, row_group_size=64,
                compression_level=3, workers=2, limit=0,
            )
            report = build_profile(args)
            self.assertEqual(report["status"], "built_not_validated")
            for frequency in (1, 5, 15, 30, 60):
                path = (
                    output / "profile" / "minute" / "v1" / f"{frequency}m" / "raw"
                    / "sh" / "2010" / "01" / "20100104.parquet"
                )
                self.assertTrue(path.is_file())
                self.assertEqual(pd.read_parquet(path).columns.tolist(), list(MINUTE_BAR_COLUMNS))
            self.assertFalse((output / "_fragments").exists())
            self.assertTrue((output / "quality" / "corrections.json").is_file())
            self.assertTrue((output / "quality" / "unresolved.json").is_file())
            self.assertTrue((output / "quality" / "exclusions.json").is_file())
            self.assertTrue((output / "quality" / "observations.json").is_file())
            self.assertEqual(report["corrections"]["applied_rows"], 0)
            self.assertEqual(report["unresolved"]["issues"], 0)
            validation = validate_build(output)
            self.assertEqual(validation["summary"], {"errors": 0, "warnings": 0})
            self.assertEqual(validation["files_checked"], 10)
            self.assertEqual(validation["actual_outputs"]["1"]["first_trade_date"], "2010-01-04")
            self.assertEqual(validation["actual_outputs"]["1"]["last_trade_date"], "2010-07-15")
            session_validation = validate_build(
                output, check_symbol_sessions=True, progress=False,
            )
            self.assertEqual(session_validation["summary"]["errors"], 0)
            self.assertGreater(session_validation["summary"]["warnings"], 0)
            self.assertEqual(
                {item["check"] for item in session_validation["findings"]},
                {"incomplete_symbol_session"},
            )

    def test_history_builder_validates_and_resumes_completed_year(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source" / "2010"
            source.mkdir(parents=True)
            payload = io.BytesIO()
            vendor_bars().to_parquet(payload, index=False)
            with ZipFile(source / "A股1分钟历史行情_2010.zip", "w", ZIP_DEFLATED) as archive:
                archive.writestr("2010/1分钟/600519.SH.parquet", payload.getvalue())
            args = argparse.Namespace(
                start_year=2010, end_year=2010,
                start_date="2010-01-04", end_date="2010-01-04",
                source_root=root / "source", output_root=root / "history", run_id="test-history",
                include_delisted=False, batch_symbols=1, row_group_size=64, compression_level=3,
                workers=1,
            )
            first = run_history(args)
            second = run_history(args)
            self.assertEqual(first["status"], "validated")
            self.assertEqual(second["status"], "validated")
            self.assertEqual(second["years"]["2010"]["status"], "validated")

    def test_history_builder_extends_existing_year_range(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for year in (2010, 2011):
                source = root / "source" / str(year)
                source.mkdir(parents=True)
                payload = io.BytesIO()
                vendor_bars(year).to_parquet(payload, index=False)
                with ZipFile(source / f"A股1分钟历史行情_{year}.zip", "w", ZIP_DEFLATED) as archive:
                    archive.writestr(f"{year}/1分钟/600519.SH.parquet", payload.getvalue())

            common = {
                "source_root": root / "source", "output_root": root / "history",
                "include_delisted": False, "batch_symbols": 1, "row_group_size": 64,
                "compression_level": 3, "workers": 1,
            }
            first = run_history(argparse.Namespace(
                start_year=2010, end_year=2010,
                start_date="2010-01-04", end_date="2010-01-04",
                run_id="test-history-2010", **common,
            ))
            second = run_history(argparse.Namespace(
                start_year=2011, end_year=2011,
                start_date="2011-01-04", end_date="2011-01-04",
                run_id="test-history-2011", **common,
            ))

            self.assertEqual(first["status"], "validated")
            self.assertEqual(second["status"], "validated")
            self.assertEqual((second["start_year"], second["end_year"]), (2010, 2011))
            self.assertEqual(set(second["years"]), {"2010", "2011"})
            self.assertEqual(second["years"]["2010"]["status"], "validated")
            self.assertEqual(second["years"]["2011"]["status"], "validated")


if __name__ == "__main__":
    unittest.main()
