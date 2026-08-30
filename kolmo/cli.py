"""Top-level command-line interface for a standalone Kolmo installation."""

from __future__ import annotations

import argparse
import os
import shlex
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOCAL_ENV = PROJECT_ROOT / ".env"
ALLOWED_ENV_KEYS = {"KOLMO_DATA_ROOT", "TIINGO_API_TOKEN"}


def load_local_env(path: Path = LOCAL_ENV) -> None:
    """Load Kolmo's private config without executing it as shell code."""
    if not path.is_file():
        return
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        if not separator or key not in ALLOWED_ENV_KEYS:
            continue
        try:
            parts = shlex.split(raw_value, comments=True, posix=True)
        except ValueError as exc:
            raise ValueError(f"invalid {path.name} line {line_number}: {exc}") from exc
        value = " ".join(parts) if parts else ""
        value = os.path.expandvars(os.path.expanduser(value))
        os.environ.setdefault(key, value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kolmo",
        description="Kolmo market-data tools.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    web = subparsers.add_parser("web", help="Start the local market terminal.")
    web.add_argument("--host", default="127.0.0.1", help="Default binds to localhost only.")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--data-root", default="", help="Override KOLMO_DATA_ROOT.")
    web.add_argument("--frontend-root", default="", help="Override the built frontend path.")
    web.add_argument("--no-browser", action="store_true", help="Do not open the default browser.")
    return parser


def main(argv: list[str] | None = None) -> int:
    load_local_env()
    args = build_parser().parse_args(argv)
    if args.command == "web":
        from kolmo.web.server import main as web_main

        web_args = ["--host", args.host, "--port", str(args.port)]
        if args.data_root:
            web_args.extend(("--data-root", args.data_root))
        if args.frontend_root:
            web_args.extend(("--frontend-root", args.frontend_root))
        if not args.no_browser:
            web_args.append("--open-browser")
        return web_main(web_args)
    raise AssertionError(f"unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
