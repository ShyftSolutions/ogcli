#!/usr/bin/env python3
"""
Tests for multi-profile configuration (EDR + WMS) and profile-aware
cache isolation.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import src.edr.config as edr_cfg
import src.wms.config as wms_cfg


@pytest.fixture
def edr_home(tmp_path, monkeypatch):
    """Redirect EDR config/cache paths to a temp home"""
    monkeypatch.setattr(edr_cfg, "EDR_CONFIG_FILE", tmp_path / "edr_config.json")
    monkeypatch.setattr(edr_cfg, "EDR_CACHE_DIR", tmp_path / "edr_cache")
    monkeypatch.setattr(edr_cfg, "LEGACY_CACHE_FILE",
                        tmp_path / "edr_collections_cache.json")
    return tmp_path


@pytest.fixture
def wms_home(tmp_path, monkeypatch):
    """Redirect WMS config/cache paths to a temp home"""
    monkeypatch.setattr(wms_cfg, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(wms_cfg, "CAPS_CACHE_FILE", tmp_path / "capabilities_cache.xml")
    return tmp_path


# ----------------------------------------------------------------------
# EDR profiles
# ----------------------------------------------------------------------

def test_edr_legacy_flat_config_migrates(edr_home):
    edr_home.joinpath("edr_config.json").write_text(json.dumps({
        "server_url": "https://example.com/edr",
        "api_key": "k",
        "cache_ttl": 300,
    }))
    config = edr_cfg.load_edr_config()
    assert config.active == "default"
    assert config.profile_names() == ["default"]
    assert config.server_url == "https://example.com/edr"
    assert config.api_key == "k"
    assert config.cache_ttl == 300


def test_edr_profile_roundtrip_and_switch(edr_home):
    config = edr_cfg.EDRConfig()
    config.ensure_profile("folkweather").server_url = "https://fw.com/edr"
    config.ensure_profile("local").server_url = "http://localhost:8006/ogc/edr"
    config.active = "folkweather"
    edr_cfg.save_edr_config(config)

    loaded = edr_cfg.load_edr_config()
    assert loaded.profile_names() == ["folkweather", "local"]
    assert loaded.server_url == "https://fw.com/edr"

    loaded.set_active("local")
    assert loaded.server_url == "http://localhost:8006/ogc/edr"
    assert loaded.is_initialized()


def test_edr_save_writes_flat_mirror(edr_home):
    config = edr_cfg.EDRConfig()
    config.ensure_profile("prod").server_url = "https://prod/edr"
    config.active = "prod"
    edr_cfg.save_edr_config(config)

    data = json.loads(edr_home.joinpath("edr_config.json").read_text())
    assert data["active"] == "prod"
    assert data["profiles"]["prod"]["server_url"] == "https://prod/edr"
    # Flat mirror keeps pre-profiles readers working
    assert data["server_url"] == "https://prod/edr"


def test_edr_cache_isolated_per_profile(edr_home):
    config = edr_cfg.EDRConfig()
    config.ensure_profile("a").server_url = "https://a/edr"
    config.ensure_profile("b").server_url = "https://b/edr"

    fa = edr_cfg.edr_cache_file(config, "a")
    fb = edr_cfg.edr_cache_file(config, "b")
    assert fa != fb
    assert fa.name == "a.json" and fb.name == "b.json"


def test_edr_legacy_cache_migrates_for_default(edr_home):
    legacy = edr_home / "edr_collections_cache.json"
    legacy.write_text('{"collections": []}')

    config = edr_cfg.EDRConfig()
    config.ensure_profile("default").server_url = "https://x/edr"
    path = edr_cfg.edr_cache_file(config)
    assert path.exists()
    assert path.read_text() == '{"collections": []}'


def test_profile_name_validation(edr_home):
    for bad in ["", "has space", "slash/name", "col:on", "."]:
        with pytest.raises(ValueError):
            edr_cfg.validate_profile_name(bad)
    edr_cfg.validate_profile_name("prod-2_xy")  # valid


def test_edr_flat_kwargs_shim(edr_home):
    """Pre-profiles construction style still works."""
    config = edr_cfg.EDRConfig(server_url="https://x/edr", api_key="k",
                               cache_ttl=42)
    assert config.server_url == "https://x/edr"
    assert config.api_key == "k"
    assert config.cache_ttl == 42
    assert config.is_initialized()


# ----------------------------------------------------------------------
# WMS profiles
# ----------------------------------------------------------------------

def test_wms_legacy_flat_config_migrates(wms_home):
    wms_home.joinpath("config.json").write_text(json.dumps({
        "source_type": "server",
        "source_path": "https://example.com/wms",
        "server_url": "https://example.com/wms",
        "output_dir": ".",
        "default_format": "image/png",
        "cache_ttl": 600,
    }))
    config = wms_cfg.load_config()
    assert config.active == "default"
    assert config.source_type == "server"
    assert config.source_path == "https://example.com/wms"
    assert config.is_initialized()


def test_wms_profile_roundtrip_and_switch(wms_home):
    config = wms_cfg.WMSConfig()
    config.ensure_profile("prod").source_path = "https://prod/wms"
    config.ensure_profile("staging").source_path = "https://staging/wms"
    config.ensure_profile("prod").source_type = "server"
    config.ensure_profile("staging").source_type = "server"
    config.active = "staging"
    wms_cfg.save_config(config)

    loaded = wms_cfg.load_config()
    assert loaded.source_path == "https://staging/wms"
    loaded.set_active("prod")
    assert loaded.source_path == "https://prod/wms"


def test_wms_cache_file_per_profile(wms_home):
    config = wms_cfg.WMSConfig()
    config.ensure_profile("default")
    config.ensure_profile("prod")
    # default keeps the legacy path
    assert wms_cfg.caps_cache_file(config, "default") == wms_home / "capabilities_cache.xml"
    assert wms_cfg.caps_cache_file(config, "prod") == \
        wms_home / "capabilities_cache_prod.xml"


def test_wms_flat_kwargs_shim(wms_home):
    config = wms_cfg.WMSConfig(source_type="server",
                               source_path="https://x/wms",
                               server_url="https://x/wms")
    assert config.source_path == "https://x/wms"
    assert config.is_initialized()


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(None, None) if fn.__code__.co_argcount else fn()
                print(f"✓ {name}")
            except TypeError:
                print(f"? {name} (needs fixtures - run via pytest)")
            except Exception as e:
                failed += 1
                print(f"✗ {name}: {e}")
    sys.exit(1 if failed else 0)
