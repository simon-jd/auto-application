#!/usr/bin/env python3
"""Run the read-only query CLI directly from a checkout, without installing the package."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auto_app.query.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
