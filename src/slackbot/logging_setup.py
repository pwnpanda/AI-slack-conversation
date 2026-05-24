"""Configure root logger from LOG_LEVEL."""

from __future__ import annotations

import logging
import sys


def configure(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        stream=sys.stderr,
    )
