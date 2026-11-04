#!/usr/bin/env python3
"""
Tests for the EDR parser (collections JSON + CoverageJSON summarizer).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.edr.parser import (
    parse_collections, parse_collection, summarize_coverage, Parameter,
)

# Trimmed folkweather-style /collections document
COLLECTIONS_JSON = """
{
  "links": [{"href": "https://example.com/edr/collections", "rel": "self"}],
  "collections": [
    {
      "id": "hrrr-surface",
      "title": "HRRR - Surface",
      "description": "Surface parameters",
      "extent": {
        "spatial": {"bbox": [[-122.0, 21.0, -72.0, 53.0]], "crs": "CRS:84"},
        "temporal": {
          "interval": [["2026-11-04T12:00:00Z", "2026-11-05T12:00:00Z"]],
          "values": ["2026-11-04T12:00:00Z", "2026-11-04T13:00:00Z",
                     "2026-11-04T14:00:00Z", "2026-11-05T12:00:00Z"]
        }
      },
      "data_queries": {
        "position": {"link": {"href": "https://example.com/edr/collections/hrrr-surface/position"}},
        "area": {"link": {"href": "https://example.com/edr/collections/hrrr-surface/area"}}
      },
      "parameter_names": {
        "TMP": {"type": "Parameter", "id": "TMP", "label": "Temperature",
                "unit": {"label": "kelvin", "symbol": "K"},
                "observedProperty": {"en": "Air Temperature"}},
        "RH": {"type": "Parameter", "id": "RH", "label": "Relative Humidity",
                "unit": "%", "observedProperty": "RH"}
      }
    },
    {
      "id": "hrrr-isobaric",
      "title": "HRRR - Isobaric Levels",
      "extent": {
        "spatial": {"bbox": [[-122.0, 21.0, -72.0, 53.0]]},
        "temporal": {"interval": [["2026-11-04T12:00:00Z", "2026-11-05T12:00:00Z"]]},
        "vertical": {"interval": [[100, 1000]], "values": [100, 250, 500, 700, 850], "vrs": "hPa"}
      },
      "data_queries": {
        "position": {"link": {}},
        "cube": {"link": {}}
      },
      "parameter_names": {
        "TMP": {"id": "TMP", "label": "Temperature"}
      }
    },
    {
      "id": "locations-only",
      "title": "Locations Registry",
      "data_queries": {"locations": {"link": {}}}
    }
  ]
}
"""

COVERAGE_JSON = """
{
  "type": "Coverage",
  "domain": {
    "type": "Domain",
    "axes": {
      "x": {"values": [-105.0]},
      "y": {"values": [40.0]},
      "t": {"values": ["2026-11-04T12:00:00Z", "2026-11-04T13:00:00Z", "2026-11-04T14:00:00Z"]}
    }
  },
  "parameters": {
    "TMP": {"type": "Parameter", "unit": {"symbol": "K"}}
  },
  "ranges": {
    "TMP": {"type": "NdArray", "dataType": "float", "values": [280.1, null, 285.3]}
  }
}
"""


def load_caps():
    return parse_collections(COLLECTIONS_JSON, "https://example.com/edr")


def test_parse_collections_basic():
    caps = load_caps()
    assert len(caps.collections) == 3
    assert caps.get_collection("hrrr-surface").title == "HRRR - Surface"
    assert caps.get_collection("nope") is None


def test_spatial_extent():
    c = load_caps().get_collection("hrrr-surface")
    assert c.bbox == [-122.0, 21.0, -72.0, 53.0]
    assert c.crs == "CRS:84"
    center = c.center()
    assert abs(center[0] - (-97.0)) < 0.01
    assert abs(center[1] - 37.0) < 0.01


def test_temporal_extent():
    c = load_caps().get_collection("hrrr-surface")
    assert c.temporal_start == "2026-11-04T12:00:00Z"
    assert c.temporal_end == "2026-11-05T12:00:00Z"
    assert c.n_timesteps == 4


def test_vertical_extent():
    c = load_caps().get_collection("hrrr-isobaric")
    assert c.n_levels == 5
    assert c.vertical_values == ["100", "250", "500", "700", "850"]
    assert c.vertical_unit == "hPa"


def test_data_queries():
    caps = load_caps()
    c = caps.get_collection("hrrr-surface")
    assert c.supports("position") and c.supports("area")
    assert not c.supports("cube")
    assert not c.supports("locations")
    assert set(caps.query_types) >= {"position", "area", "cube", "locations"}


def test_parameter_names():
    c = load_caps().get_collection("hrrr-surface")
    assert set(c.parameter_names.keys()) == {"TMP", "RH"}
    tmp = c.parameter_names["TMP"]
    assert isinstance(tmp, Parameter)
    assert tmp.unit == "K"
    assert tmp.label == "Temperature"
    rh = c.parameter_names["RH"]
    assert rh.unit == "%"


def test_collection_without_extent():
    c = load_caps().get_collection("locations-only")
    assert c.bbox is None
    assert c.center() is None
    assert c.temporal_start is None
    assert c.n_timesteps == 0


def test_landing_parse():
    from src.edr.parser import parse_landing
    title, desc = parse_landing('{"title": "Weather EDR", "description": "d"}')
    assert title == "Weather EDR"


def test_summarize_coverage():
    summary = summarize_coverage(COVERAGE_JSON)
    assert summary is not None
    assert summary.coverage_type == "Coverage"
    assert summary.axes == {"x": 1, "y": 1, "t": 3}
    assert len(summary.parameters) == 1
    p = summary.parameters[0]
    assert p["name"] == "TMP"
    assert p["unit"] == "K"
    assert p["count"] == 3
    assert p["nils"] == 1
    assert p["min"] == 280.1
    assert p["max"] == 285.3


def test_summarize_non_json():
    assert summarize_coverage(b"\x89PNG not json") is None


def test_summarize_geojson():
    summary = summarize_coverage('{"type": "FeatureCollection", "features": [{"id": 1}]}')
    assert summary is not None
    assert summary.axes.get("features") == 1


def test_summarize_coverage_collection():
    """CoverageCollection: nested 'coverages', grid axes with num, range aggregation"""
    cov = """
    {
      "type": "CoverageCollection",
      "parameters": {"TMP": {"unit": {"symbol": "K"}}},
      "coverages": [
        {
          "type": "Coverage",
          "domain": {"axes": {
            "t": {"values": ["2026-11-04T12:00:00Z"]},
            "x": {"start": -106, "stop": -104, "num": 1799},
            "z": {"values": [500]}
          }},
          "ranges": {"TMP": {"values": [250.0, 260.0]}}
        }
      ]
    }
    """
    summary = summarize_coverage(cov)
    assert summary.coverage_type == "CoverageCollection"
    assert summary.axes["coverages"] == 1
    assert summary.axes["1:x"] == 1799
    assert summary.axes["1:z"] == 1
    assert len(summary.parameters) == 1
    p = summary.parameters[0]
    assert p["name"] == "TMP"
    assert p["count"] == 2
    assert p["min"] == 250.0
    assert p["max"] == 260.0


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"✓ {name}")
            except Exception as e:
                failed += 1
                print(f"✗ {name}: {e}")
    sys.exit(1 if failed else 0)
