"""Run subprocess trees with a bounded wall-clock deadline."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Sequence


TIMEOUT_EXIT_CODE = 124


def run_with_timeout(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    label: str,
    terminate_grace_seconds: float = 10.0,
) -> int:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    process = subprocess.Popen(list(command), cwd=str(cwd), start_new_session=True)
    try:
        return process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        print(
            f"{label} exceeded {timeout_seconds:g}s; terminating process group {process.pid}",
            file=sys.stderr,
            flush=True,
        )
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=terminate_grace_seconds)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        return TIMEOUT_EXIT_CODE


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout-seconds", type=float, required=True)
    parser.add_argument("--label", default="command")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("a command is required after --")
    return run_with_timeout(
        command,
        cwd=Path.cwd(),
        timeout_seconds=args.timeout_seconds,
        label=args.label,
    )


if __name__ == "__main__":
    raise SystemExit(main())
