#!/usr/bin/env python3
"""Compatibility entry point for the refactored Kolmo market terminal."""

from kolmo.web.server import main


if __name__ == "__main__":
    raise SystemExit(main())
