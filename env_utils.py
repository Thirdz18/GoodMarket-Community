"""Environment variable parsing helpers.

Vercel crashes during module import if numeric env vars contain accidental
quotes, whitespace, or dashboard placeholders. These helpers keep import-time
configuration safe by falling back to defaults instead of raising ValueError.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def _clean_env_value(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'", '`'}:
        value = value[1:-1].strip()
    return value or None


def get_env_int(name: str, default: int) -> int:
    value = _clean_env_value(name)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning("Invalid integer env var %s=%r; using default %s", name, value, default)
        return default


def get_env_float(name: str, default: float) -> float:
    value = _clean_env_value(name)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.warning("Invalid float env var %s=%r; using default %s", name, value, default)
        return default
