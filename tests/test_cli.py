import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kolmo.cli import load_local_env, main


class LocalEnvironmentTest(unittest.TestCase):
    def test_loads_only_supported_values_without_overriding_process_env(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                'KOLMO_DATA_ROOT="$HOME/dat/all"\n'
                'TIINGO_API_TOKEN="local-token"\n'
                'IGNORED_VALUE="no"\n',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"TIINGO_API_TOKEN": "process-token"}, clear=False):
                os.environ.pop("KOLMO_DATA_ROOT", None)
                load_local_env(path)
                self.assertEqual(os.environ["KOLMO_DATA_ROOT"], f"{Path.home()}/dat/all")
                self.assertEqual(os.environ["TIINGO_API_TOKEN"], "process-token")
                self.assertNotIn("IGNORED_VALUE", os.environ)

    def test_web_command_forwards_server_arguments(self) -> None:
        with patch("kolmo.web.server.main", return_value=0) as server_main:
            result = main(["web", "--port", "9001", "--no-browser"])
        self.assertEqual(result, 0)
        self.assertEqual(server_main.call_args.args[0], ["--host", "127.0.0.1", "--port", "9001"])


if __name__ == "__main__":
    unittest.main()
