import io
import tempfile
import unittest
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pandas as pd

from kolmo.ashare.vendor_minute import VendorMinuteArchive, VendorMinuteDataset, find_archives
from kolmo.validation.minute_mdcheck import aggregate_1m, validate_frame
from kolmo.validation.minute_inventory import build_inventory


def bars(frequency: int = 1) -> pd.DataFrame:
    times = ["09:30", "09:31", "09:32", "09:33", "09:34", "09:35"]
    frame = pd.DataFrame({
        "ts_code": ["600519.SH"] * len(times),
        "freq": [f"{frequency}min"] * len(times),
        "trade_time": pd.to_datetime([f"2010-01-04 {time}" for time in times]).tz_localize("Asia/Shanghai"),
        "open": [10, 10, 11, 12, 11, 13], "close": [10, 11, 12, 11, 13, 14],
        "high": [10, 11, 12, 12, 13, 14], "low": [10, 10, 11, 11, 11, 13],
        "vol": [1, 2, 3, 4, 5, 6], "amount": [10, 21, 35, 46, 60, 80],
    })
    return frame


class VendorMinuteReaderTest(unittest.TestCase):
    def test_reads_one_symbol_from_zip_without_extracting_archive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.zip"
            payload = io.BytesIO()
            bars().to_parquet(payload, index=False)
            with ZipFile(path, "w", ZIP_DEFLATED) as archive:
                archive.writestr("2010/1分钟/600519.SH.parquet", payload.getvalue())
            reader = VendorMinuteArchive(path, 1)
            result = reader.read_symbol("600519.sh")
            self.assertEqual(reader.symbols, ("600519.SH",))
            self.assertEqual(len(result), 6)
            self.assertEqual(str(result.trade_time.dt.tz), "Asia/Shanghai")

    def test_discovers_and_combines_monthly_archives(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "2026" / "1分钟"
            folder.mkdir(parents=True)
            for month, row in (("01", 0), ("02", 1)):
                frame = bars().iloc[[row]].copy()
                time = frame["trade_time"].dt.strftime("%H:%M").iloc[0]
                frame["trade_time"] = pd.to_datetime(
                    [f"2026-{month}-04 {time}"]
                ).tz_localize("Asia/Shanghai")
                payload = io.BytesIO()
                frame.to_parquet(payload, index=False)
                label = "分钟" if month == "01" else "min"
                path = folder / f"A股1{label}历史行情_2026-{month}.zip"
                with ZipFile(path, "w", ZIP_DEFLATED) as archive:
                    archive.writestr(f"2026/1分钟/2026-{month}/600519.SH.parquet", payload.getvalue())
            paths = find_archives(root, 2026, 1)
            self.assertEqual([path.name for path in paths], [
                "A股1分钟历史行情_2026-01.zip", "A股1min历史行情_2026-02.zip",
            ])
            with VendorMinuteDataset(paths, 1) as dataset:
                result = dataset.read_symbol("600519.SH")
                february = dataset.read_symbol(
                    "600519.SH", start_date="2026-02-01", end_date="2026-02-28",
                )
            self.assertEqual(len(result), 2)
            self.assertTrue(result.trade_time.is_monotonic_increasing)
            self.assertEqual(len(february), 1)
            self.assertEqual(february.trade_time.iloc[0].month, 2)


class MinuteMdcheckTest(unittest.TestCase):
    def test_session_aware_aggregation_keeps_auction_bar_separate(self) -> None:
        result = aggregate_1m(bars(), 5)
        self.assertEqual(list(result.index.strftime("%H:%M")), ["09:30", "09:35"])
        self.assertEqual(result.iloc[1]["open"], 10)
        self.assertEqual(result.iloc[1]["close"], 14)
        self.assertEqual(result.iloc[1]["vol"], 20)

    def test_validation_finds_bad_ohlc_and_negative_volume(self) -> None:
        frame = bars()
        frame.loc[0, "low"] = 11
        frame.loc[1, "vol"] = -1
        _, findings = validate_frame("600519.SH", 1, frame)
        names = {finding.check for finding in findings}
        self.assertIn("invalid_ohlc", names)
        self.assertIn("negative_volume", names)

class MinuteInventoryTest(unittest.TestCase):
    def test_inventory_accepts_annual_layout_and_flags_partial_download(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "2025"
            folder.mkdir()
            for frequency in (1, 5, 15, 30, 60):
                path = folder / f"A股{frequency}分钟历史行情_2025.zip"
                with ZipFile(path, "w", ZIP_DEFLATED) as archive:
                    archive.writestr(f"2025/{frequency}分钟/600519.SH.parquet", b"placeholder")
            (root / "pending.zip.qkdownloading").write_bytes(b"pending")
            report = build_inventory(root, 2025, 2025, 9, False, include_delisted=False)
            self.assertEqual(report["summary"]["archives"], 5)
            self.assertEqual(report["summary"]["errors"], 1)
            self.assertEqual(report["findings"][0]["check"], "incomplete_or_empty_file")


if __name__ == "__main__":
    unittest.main()
