#!/usr/bin/env python3
"""
EDR CLI configuration and collections caching.

Multi-profile configuration: any number of saved EDR servers with one
active at a time. The active profile persists until changed
(`edr set profile <name>`), and `edr init` with no URL refreshes it.

Storage (~/.wms/edr_config.json):
    {
      "active": "folkweather",
      "profiles": {
        "folkweather": {"server_url": "...", "api_key": "", "cache_ttl": 600},
        ...
      },
      // flat mirror of the active profile kept for backward compatibility
      "server_url": "...", "api_key": "", "cache_ttl": 600
    }

Legacy flat configs (pre-profiles) migrate transparently to a
"default" profile on first load. Collections caches are per-profile
(~/.wms/edr_cache/<profile>.json); the legacy cache file is migrated
for the default profile.
"""

import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Optional

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
EDR_CACHE_DIR = CONFIG_DIR / "edr_cache"
# Pre-profiles cache location (migrated for the "default" profile)
LEGACY_CACHE_FILE = CONFIG_DIR / "edr_collections_cache.json"

PROFILE_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]*$')


def validate_profile_name(name: str):
    """Raise ValueError for profile names unusable as cache filenames."""
    if not name or not PROFILE_NAME_RE.match(name):
        raise ValueError(
            f"Invalid profile name {name!r}: use letters, numbers, '-' or '_'"
        )


@dataclass
class EDRProfile:
    """One saved EDR server"""
    server_url: str = ""
    api_key: str = ""        # Optional API key (Met Office Data Hub style)
    cache_ttl: int = DEFAULT_CACHE_TTL


class EDRConfig:
    """Multi-profile EDR configuration."""

    def __init__(self, active: str = "default",
                 profiles: Optional[Dict[str, EDRProfile]] = None,
                 **legacy):
        self.active = active
        self.profiles: Dict[str, EDRProfile] = profiles if profiles is not None else {}
        # Pre-profiles compatibility: EDRConfig(server_url=...) etc.
        if legacy:
            p = self.ensure_profile(active)
            for key, value in legacy.items():
                if key in EDRProfile.__dataclass_fields__:
                    setattr(p, key, value)

    # -- profile access -------------------------------------------------

    def has_profile(self, name: str) -> bool:
        return name in self.profiles

    def profile(self, name: Optional[str] = None) -> Optional[EDRProfile]:
        return self.profiles.get(name or self.active)

    def ensure_profile(self, name: Optional[str] = None) -> EDRProfile:
        """Get the named (or active) profile, creating it if needed."""
        name = name or self.active
        if name not in self.profiles:
            self.profiles[name] = EDRProfile()
        return self.profiles[name]

    def set_active(self, name: str):
        if name not in self.profiles:
            raise KeyError(name)
        self.active = name

    def profile_names(self) -> list:
        return list(self.profiles.keys())

    # -- active-profile property views (backward compatible) -----------

    @property
    def server_url(self) -> str:
        p = self.profile()
        return p.server_url if p else ""

    @server_url.setter
    def server_url(self, value: str):
        self.ensure_profile().server_url = value

    @property
    def api_key(self) -> str:
        p = self.profile()
        return p.api_key if p else ""

    @api_key.setter
    def api_key(self, value: str):
        self.ensure_profile().api_key = value

    @property
    def cache_ttl(self) -> int:
        p = self.profile()
        return p.cache_ttl if p else DEFAULT_CACHE_TTL

    @cache_ttl.setter
    def cache_ttl(self, value: int):
        self.ensure_profile().cache_ttl = value

    def is_initialized(self) -> bool:
        p = self.profile()
        return bool(p and p.server_url)


def _profile_from_flat(data: dict) -> EDRProfile:
    """Build a profile from a flat (legacy) config dict"""
    return EDRProfile(
        server_url=data.get("server_url", ""),
        api_key=data.get("api_key", ""),
        cache_ttl=data.get("cache_ttl", DEFAULT_CACHE_TTL),
    )


def load_edr_config() -> EDRConfig:
    """Load EDR configuration from disk (empty if not initialized).

    Transparently migrates legacy flat configs to a 'default' profile.
    """
    if not EDR_CONFIG_FILE.exists():
        return EDRConfig()

    try:
        with open(EDR_CONFIG_FILE, 'r') as f:
            data = json.load(f)
        if isinstance(data.get("profiles"), dict) and data["profiles"]:
            profiles = {}
            for name, pdata in data["profiles"].items():
                if isinstance(pdata, dict):
                    profiles[name] = EDRProfile(**{
                        k: v for k, v in pdata.items()
                        if k in EDRProfile.__dataclass_fields__
                    })
            config = EDRConfig(active=data.get("active") or "default",
                               profiles=profiles)
            # Active pointing at a missing profile -> fall back to first
            if not config.has_profile(config.active) and profiles:
                config.active = next(iter(profiles))
            return config
        # Legacy flat config
        return EDRConfig(active="default",
                         profiles={"default": _profile_from_flat(data)})
    except Exception as e:
        logger.warning(f"Failed to load EDR config: {e}")
        return EDRConfig()


def save_edr_config(config: EDRConfig):
    """Save EDR configuration to disk.

    Writes the profiles map plus a flat mirror of the active profile so
    pre-profiles versions of the tool keep working.
    """
    ensure_config_dir()
    data = {
        "active": config.active,
        "profiles": {name: asdict(p) for name, p in config.profiles.items()},
    }
    p = config.profile()
    if p:
        data.update(asdict(p))
    with open(EDR_CONFIG_FILE, 'w') as f:
        json.dump(data, f, indent=2)
    logger.info(f"EDR config saved to {EDR_CONFIG_FILE}")


def normalize_base_url(url: str) -> str:
    """Normalize a user-supplied EDR base URL"""
    url = url.strip().rstrip('/')
    # Common mistake: pointing at /collections directly
    if url.endswith('/collections'):
        url = url[:-len('/collections')]
    return url


def edr_cache_file(config: EDRConfig, profile: Optional[str] = None) -> Path:
    """Per-profile collections cache path."""
    name = profile or config.active
    path = EDR_CACHE_DIR / f"{name}.json"
    # One-time migration of the legacy cache for the default profile
    if (name == "default" and not path.exists()
            and LEGACY_CACHE_FILE.exists()):
        EDR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(LEGACY_CACHE_FILE, path)
    return path


def clear_edr_cache() -> bool:
    """Clear all EDR collections caches. Returns True if any existed."""
    removed = False
    if EDR_CACHE_DIR.exists():
        shutil.rmtree(EDR_CACHE_DIR)
        removed = True
    if LEGACY_CACHE_FILE.exists():
        LEGACY_CACHE_FILE.unlink()
        removed = True
    return removed


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
    Get collections JSON for the config's active profile.

    Uses the per-profile cached version with sliding-window TTL;
    fetches fresh content when the cache is missing or expired.
    """
    if not config.is_initialized():
        raise ValueError("EDR not initialized. Run 'edr init <url>' first.")

    cache_path = edr_cache_file(config)
    ttl = config.cache_ttl if config.cache_ttl > 0 else DEFAULT_CACHE_TTL

    if is_cache_valid(cache_path, ttl):
        logger.debug(f"Using cached EDR collections (TTL: {ttl}s)")
        with open(cache_path, 'r', encoding='utf-8') as f:
            content = f.read()
        touch_cache(cache_path)
        return content

    logger.info("EDR cache expired or missing, fetching fresh collections...")
    content = fetch_collections(config.server_url, config.api_key)

    ensure_config_dir()
    with open(cache_path, 'w', encoding='utf-8') as f:
        f.write(content)
    logger.debug(f"Cached EDR collections to {cache_path}")

    return content


def get_edr_capabilities(config: Optional[EDRConfig] = None):
    """Convenience: load config (if needed) and return parsed EDRCapabilities"""
    from .parser import parse_collections
    if config is None:
        config = load_edr_config()
    return parse_collections(get_collections_json(config), config.server_url)


def refresh_active_profile(config: Optional[EDRConfig] = None) -> EDRConfig:
    """Re-fetch collections for the active profile (`edr init` with no URL)."""
    if config is None:
        config = load_edr_config()
    if not config.is_initialized():
        raise ValueError(
            f"Profile '{config.active}' has no server URL. "
            "Run 'edr init <url>' first."
        )
    content = fetch_collections(config.server_url, config.api_key)
    cache_path = edr_cache_file(config)
    ensure_config_dir()
    with open(cache_path, 'w', encoding='utf-8') as f:
        f.write(content)
    return config


def init_from_server(server_url: str, api_key: str = "",
                     config: Optional[EDRConfig] = None,
                     save_as: Optional[str] = None) -> EDRConfig:
    """
    Initialize an EDR profile from a server.

    Args:
        server_url: Base URL of the EDR service
        api_key: Optional API key (or EDR_API_KEY env var)
        config: Existing config to update (loaded if omitted)
        save_as: Save as a named profile and make it active. Without
            this, the active profile (or 'default') is updated.

    The landing page is fetched best-effort for a title; /collections
    must parse. Collections are cached at the per-profile path.
    """
    from .parser import parse_collections, parse_landing

    if save_as:
        validate_profile_name(save_as)

    if config is None:
        config = load_edr_config()

    base_url = normalize_base_url(server_url)
    if not api_key:
        api_key = os.environ.get('EDR_API_KEY', '')

    title = ""
    try:
        landing = fetch_landing(base_url, api_key)
        title, _ = parse_landing(landing)
    except Exception as e:
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

    # Update the target profile
    name = save_as or (config.active if config.profiles else "default")
    validate_profile_name(name)
    profile = config.ensure_profile(name)
    profile.server_url = base_url
    profile.api_key = api_key
    config.active = name

    # Cache at the per-profile path
    cache_path = edr_cache_file(config, name)
    EDR_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(cache_path, 'w', encoding='utf-8') as f:
        f.write(collections_json)

    save_edr_config(config)
    return config
