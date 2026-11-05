#!/usr/bin/env python3
"""
Tests for the ogcli MCP server tools.

Tool functions are plain callables (the MCPServer decorator registers
them without wrapping), so tests call them directly against monkey-
patched contexts and fake HTTP clients.
"""

import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import src.mcp_server as mcp_mod
from src.edr.config import EDRConfig
from src.edr.client import EDRClient
from src.edr.parser import parse_collections
from src.edr.resolver import EDRResolver
from src.wms.config import WMSConfig
from src.wms.parser import parse_capabilities
from src.wms.resolver import SmartQueryResolver

EDR_FIXTURE = """
{
  "collections": [
    {
      "id": "hrrr-surface",
      "title": "HRRR - Surface",
      "extent": {
        "spatial": {"bbox": [[-122.0, 21.0, -72.0, 53.0]]},
        "temporal": {
          "interval": [["2026-11-04T12:00:00Z", "2026-11-05T12:00:00Z"]],
          "values": ["2026-11-04T12:00:00Z", "2026-11-05T12:00:00Z"]
        }
      },
      "data_queries": {"position": {}, "area": {}},
      "parameter_names": {"TMP": {"id": "TMP", "label": "Temperature"}}
    },
    {
      "id": "no-position",
      "data_queries": {"area": {}}
    }
  ]
}
"""

COVERAGE_BODY = json.dumps({
    "type": "Coverage",
    "domain": {"axes": {"x": {"values": [-105.0]}, "y": {"values": [40.0]}}},
    "parameters": {"TMP": {"unit": {"symbol": "K"}}},
    "ranges": {"TMP": {"values": [285.2]}},
}).encode()


def make_edr_context(profile: str = ""):
    config = EDRConfig(server_url="https://example.com/edr")
    caps = parse_collections(EDR_FIXTURE, config.server_url)
    return config, EDRResolver(caps)


@pytest.fixture
def edr_ctx(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_mod, "_edr_context", make_edr_context)
    monkeypatch.setattr(mcp_mod, "MCP_OUTPUT_DIR", tmp_path / "out")
    return tmp_path


@pytest.fixture
def wms_ctx(monkeypatch, tmp_path):
    fixture = Path(__file__).parent / "fixtures" / "capabilities.xml"
    caps = parse_capabilities(fixture.read_text())
    config = WMSConfig(source_type="file", source_path=str(fixture),
                       server_url="https://example.com/ogc/WMS")
    monkeypatch.setattr(mcp_mod, "_wms_context",
                        lambda profile="": (config, SmartQueryResolver(caps)))
    monkeypatch.setattr(mcp_mod, "MCP_OUTPUT_DIR", tmp_path / "out")
    return tmp_path


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def test_clamp_hammer():
    limit, workers, notes = mcp_mod._clamp_hammer(1000, 100)
    assert limit == mcp_mod.HAMMER_MAX_REQUESTS
    assert workers == mcp_mod.HAMMER_MAX_WORKERS
    assert len(notes) == 2
    limit, workers, notes = mcp_mod._clamp_hammer(10, 5)
    assert (limit, workers, notes) == (10, 5, [])


def test_dump_truncates():
    out = mcp_mod._dump({"x": "a" * 100_000}, max_chars=500)
    assert len(out) < 1000
    assert "truncated" in out


# ----------------------------------------------------------------------
# EDR tools
# ----------------------------------------------------------------------

def test_edr_status_not_initialized(monkeypatch):
    monkeypatch.setattr(mcp_mod, "load_edr_config", lambda: EDRConfig())
    result = json.loads(mcp_mod.edr_status())
    assert result["initialized"] is False


def test_edr_collections(edr_ctx):
    result = json.loads(mcp_mod.edr_collections("hrrr"))
    assert result["count"] >= 1
    assert result["collections"][0]["id"] == "hrrr-surface"
    assert result["collections"][0]["query_types"] == ["position", "area"]


def test_edr_describe(edr_ctx):
    result = json.loads(mcp_mod.edr_describe("hrrr surface"))
    assert result["id"] == "hrrr-surface"
    assert "TMP" in result["parameters"]
    assert result["temporal"]["n_timesteps"] == 2


def test_edr_query_position(edr_ctx, monkeypatch):
    calls = {}

    def fake_query(self, collection_id, query_type, **kw):
        calls["args"] = (collection_id, query_type, kw)
        return COVERAGE_BODY, "application/vnd.cov+json"

    monkeypatch.setattr(EDRClient, "query", fake_query)

    result = json.loads(mcp_mod.edr_query(
        "hrrr surface temp", coords="-105,40", datetime="latest"
    ))

    assert result["collection"] == "hrrr-surface"
    assert calls["args"][0] == "hrrr-surface"
    assert calls["args"][1] == "position"
    kw = calls["args"][2]
    assert kw["coords"] == "POINT(-105.0 40.0)"
    assert kw["parameter-name"] == "TMP"
    assert kw["datetime"] == "2026-11-05T12:00:00Z"
    # Summary extracted
    assert result["summary"]["parameters"][0]["name"] == "TMP"
    assert result["summary"]["parameters"][0]["min"] == 285.2
    # Payload saved
    saved = Path(result["saved_to"])
    assert saved.exists()
    assert saved.read_bytes() == COVERAGE_BODY


def test_edr_query_defaults_to_center(edr_ctx, monkeypatch):
    def fake_query(self, collection_id, query_type, **kw):
        return COVERAGE_BODY, "application/vnd.cov+json"

    monkeypatch.setattr(EDRClient, "query", fake_query)
    result = json.loads(mcp_mod.edr_query("hrrr surface"))
    assert result["collection"] == "hrrr-surface"
    assert any("center" in w for w in result["warnings"])


def test_edr_query_rejects_unsupported_type(edr_ctx):
    from mcp.server.mcpserver.exceptions import ToolError
    with pytest.raises(ToolError, match="does not support position"):
        mcp_mod.edr_query("no-position", query_type="position",
                          coords="-105,40")


def test_edr_hammer_dry_run_preview(edr_ctx):
    """Dry run (default) generates a preview without any network calls."""
    result = json.loads(mcp_mod.edr_hammer("hrrr"))
    assert result["dry_run"] is True
    assert "hrrr-surface" in result["collections"]
    assert result["total_requests"] > 0
    assert result["sample_urls"]


def test_edr_hammer_clamps_limits(edr_ctx):
    result = json.loads(mcp_mod.edr_hammer("hrrr", limit=99999))
    assert any("clamped" in n for n in result["notes"])


# ----------------------------------------------------------------------
# WMS tools
# ----------------------------------------------------------------------

def test_wms_layers(wms_ctx):
    result = json.loads(mcp_mod.wms_layers("gfs temp"))
    assert result["count"] >= 1
    assert result["layers"][0]["name"].startswith("GFS_Temperature")


def test_wms_describe(wms_ctx):
    result = json.loads(mcp_mod.wms_describe("gfs temp f"))
    assert result["name"] == "GFS_Temperature_F"
    assert "RUN" in result["dimensions"]


def test_wms_url(wms_ctx):
    url = mcp_mod.wms_url("gfs temp")
    assert "GetMap" in url
    assert "GFS_Temperature" in url


def test_wms_map_returns_image_and_text(wms_ctx, monkeypatch):
    from src.wms.client import WMSClient
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"0" * 100

    def fake_get_map(self, **kw):
        return png_bytes

    monkeypatch.setattr(WMSClient, "get_map", fake_get_map)

    result = mcp_mod.wms_map("gfs temp f")

    assert isinstance(result, list) and len(result) == 2
    text_block, image_block = result
    assert text_block.type == "text"
    info = json.loads(text_block.text)
    assert info["layer"] == "GFS_Temperature_F"
    assert Path(info["saved_to"]).exists()
    assert image_block.type == "image"
    assert base64.b64decode(image_block.data) == png_bytes


def test_wms_hammer_dry_run(wms_ctx):
    result = json.loads(mcp_mod.wms_hammer("gfs temp"))
    assert result["dry_run"] is True
    assert result["total_requests"] > 0
    assert result["sample_requests"][0]["layer"].startswith("GFS_Temperature")


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
