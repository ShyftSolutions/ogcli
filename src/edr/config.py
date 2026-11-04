#!/usr/bin/env python3
"""
EDR CLI configuration and collections caching.

Mirror of the WMS config module, but for OGC API - EDR services:
- Config stored in ~/.wms/edr_config.json
- Collections JSON cached in ~/.wms/edr_collections_cache.json
- Same sliding-window TTL strategy as the WMS capabilities cache

WMS and EDR state coexist: `edr init` does not touch `wms init` state.
"""

import json
import logging
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import requests

from ..common.cache import (
    CONFIG_DIR,
    DEFAULT_CACHE_TTL,
    ensure_config_dir,
    is_cache_valid,
    touch_cache,
)

logger = logging.getLogger(__name__)

EDR_CONFIG_FILE = CONFIG_DIR / "edr_config.json"
EDR_CACHE_FILE = CONFIG_DIR / "edr_collections_cache.json"


@dataclass
class EDRConfig:
    """EDR CLI configuration"""
    server_url: str = ""     # Base URL, e.g. https://folkweather.com/edr
    api_key: str = ""        # Optional API key (Met Office Data Hub style)
    cache_ttl: int = DEFAULT_CACHE_TTL

    def is_initialized(self) -> bool:
        return bool(self.server_url)


def load_edr_config() -> EDRConfig:
    """Load EDR configuration from disk (empty if not initialized)"""
    if not EDR_CONFIG_FILE.exists():
        return EDRConfig()

    try:
        with open(EDR_CONFIG_FILE, 'r') as f:
            data = json.load(f)
        return EDRConfig(**data)
    except Exception as e:
        logger.warning(f"Failed to load EDR config: {e}")
        return EDRConfig()


def save_edr_config(config: EDRConfig):
    """Save EDR configuration to disk"""
    ensure_config_dir()
    with open(EDR_CONFIG_FILE, 'w') as f:
        json.dump(asdict(config), f, indent=2)
    logger.info(f"EDR config saved to {EDR_CONFIG_FILE}")


def normalize_base_url(url: str) -> str:
    """Normalize a user-supplied EDR base URL"""
    url = url.strip().rstrip('/')
    # Common mistake: pointing at /collections directly
    if url.endswith('/collections'):
        url = url[:-len('/collections')]
    return url


def clear_edr_cache() -> bool:
    """Clear the collections cache. Returns True if a cache existed."""
    if EDR_CACHE_FILE.exists():
        EDR_CACHE_FILE.unlink()
        logger.info("EDR collections cache cleared")
        return True
    return False


def fetch_landing(base_url: str, api_key: str = "", timeout: int = 30) -> dict:
    """Fetch the EDR landing page JSON"""
    params = {'apikey': api_key} if api_key else {}
    response = requests.get(base_url, params=params or None, timeout=timeout,
                            headers={'Accept': 'application/json'})
    response.raise_for_status()
    return response.json()


def fetch_collections(base_url: str, api_key: str = "", timeout: int = 30) -> str:
    """Fetch raw /collections JSON from an EDR server"""
    params = {'apikey': api_key} if api_key else {}
    url = f"{base_url.rstrip('/')}/collections"
    logger.info(f"Fetching EDR collections from {url}")
    response = requests.get(url, params=params or None, timeout=timeout,
                            headers={'Accept': 'application/json'})
    response.raise_for_status()
    return response.text


def get_collections_json(config: EDRConfig) -> str:
    """
    Get collections JSON content based on config.

    Uses the cached version with sliding-window TTL; fetches fresh
    content when the cache is missing or expired.
    """
    if not config.is_initialized():
        raise ValueError("EDR not initialized. Run 'edr init <url>' first.")

    ttl = config.cache_ttl if config.cache_ttl > 0 else DEFAULT_CACHE_TTL

    if is_cache_valid(EDR_CACHE_FILE, ttl):
        logger.debug(f"Using cached EDR collections (TTL: {ttl}s)")
        with open(EDR_CACHE_FILE, 'r', encoding='utf-8') as f:
            content = f.read()
        touch_cache(EDR_CACHE_FILE)
        return content

    logger.info("EDR cache expired or missing, fetching fresh collections...")
    content = fetch_collections(config.server_url, config.api_key)

    ensure_config_dir()
    with open(EDR_CACHE_FILE, 'w', encoding='utf-8') as f:
        f.write(content)
    logger.debug(f"Cached EDR collections to {EDR_CACHE_FILE}")

    return content


def get_edr_capabilities(config: Optional[EDRConfig] = None):
    """Convenience: load config (if needed) and return parsed EDRCapabilities"""
    from .parser import parse_collections
    if config is None:
        config = load_edr_config()
    return parse_collections(get_collections_json(config), config.server_url)


def init_from_server(server_url: str, api_key: str = "") -> EDRConfig:
    """
    Initialize EDR config from a server.

    Fetches the landing page (best-effort, for the title) and the
    /collections document, validates it parses, and caches it.

    Raises:
        requests.RequestException: On network error
        ValueError: If collections document is not valid EDR JSON
    """
    from .parser import parse_collections, parse_landing

    base_url = normalize_base_url(server_url)
    if not api_key:
        api_key = os.environ.get('EDR_API_KEY', '')

    title = ""
    try:
        landing = fetch_landing(base_url, api_key)
        title, _ = parse_landing(landing)
    except Exception as e:
        # Landing page is nice-to-have; collections is the real requirement
        logger.debug(f"Landing page fetch failed (continuing): {e}")

    collections_json = fetch_collections(base_url, api_key)
    caps = parse_collections(collections_json, base_url)
    if not caps.collections:
        raise ValueError(
            f"No collections found at {base_url}/collections - is this an EDR server?"
        )
    logger.info(
        f"Validated EDR collections: {title or base_url} "
        f"({len(caps.collections)} collections)"
    )

    # Cache
    ensure_config_dir()
    with open(EDR_CACHE_FILE, 'w', encoding='utf-8') as f:
        f.write(collections_json)

    config = EDRConfig(server_url=base_url, api_key=api_key)
    save_edr_config(config)

    return config
