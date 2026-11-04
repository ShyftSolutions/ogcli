#!/usr/bin/env python3
"""
Shared cache utilities for ogcli (WMS + EDR).

Both CLIs cache their discovery documents (GetCapabilities XML /
EDR collections JSON) in ~/.wms/ with a sliding-window TTL:
the TTL resets on each access, so active use keeps the cache fresh.
"""

import time
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Shared config directory (both WMS and EDR state live here)
CONFIG_DIR = Path.home() / ".wms"

# Default cache TTL in seconds (10 minutes)
DEFAULT_CACHE_TTL = 600


def ensure_config_dir():
    """Create config directory if it doesn't exist"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def is_cache_valid(cache_path: Path, ttl_seconds: int) -> bool:
    """
    Check if cache file exists and is within TTL.

    Args:
        cache_path: Path to cache file
        ttl_seconds: Maximum age in seconds

    Returns:
        True if cache exists and is not expired
    """
    if not cache_path.exists():
        return False

    try:
        mtime = cache_path.stat().st_mtime
        age = time.time() - mtime
        return age < ttl_seconds
    except OSError:
        return False


def touch_cache(cache_path: Path) -> None:
    """
    Touch cache file to reset its modification time (sliding window).

    Args:
        cache_path: Path to cache file
    """
    try:
        cache_path.touch()
        logger.debug(f"Cache touched: {cache_path}")
    except OSError as e:
        logger.warning(f"Failed to touch cache: {e}")
