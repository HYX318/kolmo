import unittest
from unittest.mock import patch

from kolmo.scheduler import update_cn_profile_if_trading_day as scheduler


class ScheduledUpdateTest(unittest.TestCase):
    def test_non_trading_day_uses_distinct_skip_exit_code(self) -> None:
        argv = [
            "scheduler",
            "--date",
            "20260829",
            "--skip-exit-code",
            "20",
        ]
        with patch("sys.argv", argv), patch.object(
            scheduler, "baostock_trading_day", return_value=False
        ):
            self.assertEqual(scheduler.main(), 20)

    def test_trading_day_runs_update_with_total_deadline(self) -> None:
        argv = [
            "scheduler",
            "--date",
            "20260831",
            "--update-timeout-seconds",
            "123",
            "--",
            "--target",
            "all",
        ]
        with patch("sys.argv", argv), patch.object(
            scheduler, "baostock_trading_day", return_value=True
        ), patch.object(scheduler, "run_with_timeout", return_value=0) as runner:
            self.assertEqual(scheduler.main(), 0)
        self.assertEqual(runner.call_args.kwargs["timeout_seconds"], 123)
        self.assertIn("--target", runner.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
