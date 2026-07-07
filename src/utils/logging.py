"""Shared logging configuration for entry points."""

from __future__ import annotations

import logging

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(level: int = logging.INFO) -> None:
    """Configure root logging handlers once per process entry point."""
    logging.basicConfig(level=level, format=LOG_FORMAT)
