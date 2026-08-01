#!/usr/bin/env python3
"""Compatibility entrypoint for the approved local MOJ capture program."""

from __future__ import annotations

import os
import runpy
from pathlib import Path

DEFAULT_IMPLEMENTATION = Path(r"D:\網頁識別認證\capture_auction_results.py")


def main() -> None:
    implementation = Path(
        os.environ.get("AUCTION_CAPTURE_IMPLEMENTATION", str(DEFAULT_IMPLEMENTATION))
    )
    if not implementation.is_file():
        raise SystemExit(f"auction capture implementation not found: {implementation}")
    runpy.run_path(str(implementation), run_name="__main__")


if __name__ == "__main__":
    main()
