#!/usr/bin/env python3
"""
Configuration management for WMS CLI.
Handles init, loading/saving config, and capabilities caching.

Cache Strategy:
- Capabilities are cached in ~/.wms/capabilities_cache.xml
- Uses sliding window TTL: cache resets on each access
- Default TTL is 10 minutes (600 seconds)
- If you're actively using the tool, cache stays fresh
- If you return after being away, cache expires and refreshes
"""

import json
import os
import re
import time
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional
import logging

from ..common.cache import (
    CONFIG_DIR,
    DEFAULT_CACHE_TTL,
    ensure_config_dir,
    is_cache_valid,
    touch_cache,
)

logger = logging.getLogger(__name__)

CONFIG_FILE = CONFIG_DIR / "config.json"
CAPS_CACHE_FILE = CONFIG_DIR / "capabilities_cache.xml"

PROFILE_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]*$')


def validate_profile_name(name: str):
    """Raise ValueError for profile names unusable as cache filenames."""
    if not name or not PROFILE_NAME_RE.match(name):
        raise ValueError(
            f"Invalid profile name {name!r}: use letters, numbers, '-' or '_'"
        )


@dataclass
class WMSProfile:
    """One saved WMS server"""
    source_type: str = ""       # "file" or "server"
    source_path: str = ""       # Local file path or server URL
    server_url: str = ""        # Server URL for actual requests
    default_format: str = "image/png"
    cache_ttl: int = 600        # Cache TTL in seconds (10 min)


class WMSConfig:
    """Multi-profile WMS configuration.

    output_dir is global; everything server-specific lives in profiles.
    Property views on the active profile keep the pre-profiles API
    (config.server_url etc.) working everywhere.
    """

    def __init__(self, active: str = "default", profiles=None,
                 output_dir: str = ".", **legacy):
        self.active = active
        self.profiles: dict = profiles if profiles is not None else {}
        self.output_dir = output_dir
        # Pre-profiles compatibility: WMSConfig(server_url=...) etc.
        if legacy:
            p = self.ensure_profile(active)
            for key, value in legacy.items():
                if key in WMSProfile.__dataclass_fields__:
                    setattr(p, key, value)
                elif key == "output_dir":
                    self.output_dir = value

    # -- profile access -------------------------------------------------

    def has_profile(self, name: str) -> bool:
        return name in self.profiles

    def profile(self, name=None) -> Optional[WMSProfile]:
        return self.profiles.get(name or self.active)

    def ensure_profile(self, name=None) -> WMSProfile:
        name = name or self.active
        if name not in self.profiles:
            self.profiles[name] = WMSProfile()
        return self.profiles[name]

    def set_active(self, name: str):
        if name not in self.profiles:
            raise KeyError(name)
        self.active = name

    def profile_names(self) -> list:
        return list(self.profiles.keys())

    # -- active-profile property views -----------------------------------

    @property
    def source_type(self) -> str:
        p = self.profile()
        return p.source_type if p else ""

    @source_type.setter
    def source_type(self, value: str):
        self.ensure_profile().source_type = value

    @property
    def source_path(self) -> str:
        p = self.profile()
        return p.source_path if p else ""

    @source_path.setter
    def source_path(self, value: str):
        self.ensure_profile().source_path = value

    @property
    def server_url(self) -> str:
        p = self.profile()
        return p.server_url if p else ""

    @server_url.setter
    def server_url(self, value: str):
        self.ensure_profile().server_url = value

    @property
    def default_format(self) -> str:
        p = self.profile()
        return p.default_format if p else "image/png"

    @default_format.setter
    def default_format(self, value: str):
        self.ensure_profile().default_format = value

    @property
    def cache_ttl(self) -> int:
        p = self.profile()
        return p.cache_ttl if p else 600

    @cache_ttl.setter
    def cache_ttl(self, value: int):
        self.ensure_profile().cache_ttl = value

    def is_initialized(self) -> bool:
        """Check if the active profile has been initialized"""
        return bool(self.source_path)


def caps_cache_file(config: WMSConfig, profile: Optional[str] = None) -> Path:
    """Per-profile capabilities cache path ('default' keeps the legacy path)."""
    name = profile or config.active
    if name == "default":
        return CAPS_CACHE_FILE
    return CAPS_CACHE_FILE.parent / f"capabilities_cache_{name}.xml"


def clear_cache() -> bool:
    """
    Clear ALL capabilities caches (all profiles).

    Returns:
        True if any cache was cleared
    """
    import glob
    removed = False
    pattern = str(CONFIG_DIR / "capabilities_cache*.xml")
    for path in glob.glob(pattern):
        Path(path).unlink()
        removed = True
    if removed:
        logger.info("Capabilities caches cleared")
    return removed


def fetch_capabilities_xml(server_url: str, timeout: int = 30) -> str:
    """
    Fetch raw GetCapabilities XML from a WMS server.

    Args:
        server_url: WMS server base URL
        timeout: Request timeout in seconds

    Returns:
        Raw XML content as string

    Raises:
        requests.RequestException: On network error
    """
    import requests

    params = {
        'SERVICE': 'WMS',
        'VERSION': '1.3.0',
        'REQUEST': 'GetCapabilities'
    }

    logger.info(f"Fetching capabilities from {server_url}")
    response = requests.get(server_url, params=params, timeout=timeout)
    response.raise_for_status()

    return response.text


def load_config() -> WMSConfig:
    """
    Load configuration from disk.

    Legacy flat configs migrate transparently to a 'default' profile.

    Returns:
        WMSConfig object (empty if not initialized)
    """
    if not CONFIG_FILE.exists():
        return WMSConfig()

    try:
        with open(CONFIG_FILE, 'r') as f:
            data = json.load(f)
        if isinstance(data.get("profiles"), dict) and data["profiles"]:
            profiles = {}
            for name, pdata in data["profiles"].items():
                if isinstance(pdata, dict):
                    profiles[name] = WMSProfile(**{
                        k: v for k, v in pdata.items()
                        if k in WMSProfile.__dataclass_fields__
                    })
            config = WMSConfig(active=data.get("active") or "default",
                               profiles=profiles,
                               output_dir=data.get("output_dir", "."))
            if not config.has_profile(config.active) and profiles:
                config.active = next(iter(profiles))
            return config
        # Legacy flat config
        return WMSConfig(active="default",
                         profiles={"default": WMSProfile(**{
                             k: v for k, v in data.items()
                             if k in WMSProfile.__dataclass_fields__
                         })},
                         output_dir=data.get("output_dir", "."))
    except Exception as e:
        logger.warning(f"Failed to load config: {e}")
        return WMSConfig()


def save_config(config: WMSConfig):
    """
    Save configuration to disk.

    Writes the profiles map plus a flat mirror of the active profile so
    pre-profiles versions of the tool keep working.
    """
    ensure_config_dir()

    data = {
        "active": config.active,
        "profiles": {name: asdict(p) for name, p in config.profiles.items()},
        "output_dir": config.output_dir,
    }
    p = config.profile()
    if p:
        data.update(asdict(p))

    with open(CONFIG_FILE, 'w') as f:
        json.dump(data, f, indent=2)

    logger.info(f"Config saved to {CONFIG_FILE}")


def get_capabilities_xml(config: WMSConfig) -> str:
    """
    Get capabilities XML content based on config.

    For local files, reads directly.
    For servers, uses cached version with sliding window TTL.

    Cache behavior:
    - Cache is valid for config.cache_ttl seconds (default 10 min)
    - Each access "touches" the cache, resetting the TTL
    - If cache expires, fresh data is fetched from server

    Args:
        config: WMSConfig with source information

    Returns:
        XML content as string

    Raises:
        FileNotFoundError: If local file doesn't exist
        ValueError: If config not initialized
    """
    if not config.is_initialized():
        raise ValueError("WMS not initialized. Run 'wms init <source>' first.")

    if config.source_type == "file":
        # Read from local file
        path = Path(config.source_path)
        if not path.exists():
            raise FileNotFoundError(f"Capabilities file not found: {path}")

        with open(path, 'r', encoding='utf-8') as f:
            return f.read()

    elif config.source_type == "server":
        ttl = config.cache_ttl if config.cache_ttl > 0 else DEFAULT_CACHE_TTL
        cache_path = caps_cache_file(config)

        # Check if cache is valid (exists and not expired)
        if is_cache_valid(cache_path, ttl):
            logger.debug(f"Using cached capabilities (TTL: {ttl}s)")
            with open(cache_path, 'r', encoding='utf-8') as f:
                content = f.read()
            # Touch cache to reset sliding window TTL
            touch_cache(cache_path)
            return content

        # Cache expired or doesn't exist - fetch fresh
        logger.info("Cache expired or missing, fetching fresh capabilities...")
        xml_content = fetch_capabilities_xml(config.source_path)

        # Save to cache
        ensure_config_dir()
        with open(cache_path, 'w', encoding='utf-8') as f:
            f.write(xml_content)
        logger.debug(f"Cached capabilities to {cache_path}")

        return xml_content

    else:
        raise ValueError(f"Unknown source type: {config.source_type}")


def init_from_file(file_path: str, server_url: Optional[str] = None,
                   config: Optional[WMSConfig] = None,
                   save_as: Optional[str] = None) -> WMSConfig:
    """
    Initialize a profile from a local capabilities file.

    Args:
        file_path: Path to capabilities XML file
        server_url: Optional server URL for requests
        config: Existing config to update (loaded if omitted)
        save_as: Save as a named profile (and make it active)

    Returns:
        Initialized WMSConfig
    """
    path = Path(file_path).resolve()

    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    # Parse to validate and extract service URL
    from .parser import parse_capabilities

    with open(path, 'r', encoding='utf-8') as f:
        xml_content = f.read()

    caps = parse_capabilities(xml_content)

    # Use provided server URL, or extract from capabilities
    if not server_url:
        server_url = caps.service_url or ""

    if config is None:
        config = load_config()
    name = save_as or (config.active if config.profiles else "default")
    validate_profile_name(name)
    profile = config.ensure_profile(name)
    profile.source_type = "file"
    profile.source_path = str(path)
    profile.server_url = server_url
    config.active = name

    save_config(config)
    return config


def init_from_server(server_url: str, config: Optional[WMSConfig] = None,
                     save_as: Optional[str] = None) -> WMSConfig:
    """
    Initialize a profile from a remote WMS server.

    Args:
        server_url: WMS server URL
        config: Existing config to update (loaded if omitted)
        save_as: Save as a named profile (and make it active)

    Returns:
        Initialized WMSConfig

    Raises:
        requests.RequestException: On network error
        ValueError: If capabilities cannot be parsed
    """
    from .parser import parse_capabilities

    # Fetch raw XML (single request)
    xml_content = fetch_capabilities_xml(server_url)

    # Parse to validate it's valid capabilities XML
    caps = parse_capabilities(xml_content)
    logger.info(f"Validated capabilities: {caps.service_title} ({len(caps.get_queryable_layers())} layers)")

    if config is None:
        config = load_config()
    name = save_as or (config.active if config.profiles else "default")
    validate_profile_name(name)
    profile = config.ensure_profile(name)
    profile.source_type = "server"
    profile.source_path = server_url
    profile.server_url = server_url
    config.active = name

    # Cache the XML
    ensure_config_dir()
    with open(caps_cache_file(config, name), 'w', encoding='utf-8') as f:
        f.write(xml_content)

    save_config(config)

    return config


def refresh_active_profile(config: Optional[WMSConfig] = None) -> WMSConfig:
    """Re-fetch capabilities for the active profile (`wms init` with no source)."""
    if config is None:
        config = load_config()
    if not config.is_initialized():
        raise ValueError(
            f"Profile '{config.active}' has no source. "
            "Run 'wms init <source>' first."
        )
    if config.source_type == "file":
        # Local file: nothing to refresh, just re-read
        return config
    xml_content = fetch_capabilities_xml(config.source_path)
    ensure_config_dir()
    with open(caps_cache_file(config), 'w', encoding='utf-8') as f:
        f.write(xml_content)
    return config
