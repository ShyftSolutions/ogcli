#!/usr/bin/env python3
"""
Tests for the EDR resolver (fuzzy matching + natural dimension syntax).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.edr.parser import parse_collections
from src.edr.resolver import (
    EDRResolver, AmbiguousCollectionError, parse_coords, bbox_to_wkt,
)

COLLECTIONS_JSON = """
{
  "collections": [
    {
      "id": "hrrr-surface",
      "title": "HRRR - Surface",
      "extent": {
        "spatial": {"bbox": [[-122.0, 21.0, -72.0, 53.0]]},
        "temporal": {
          "interval": [["2026-11-04T12:00:00Z", "2026-11-05T12:00:00Z"]],
          "values": ["2026-11-04T12:00:00Z", "2026-11-04T18:00:00Z",
                     "2026-11-05T00:00:00Z", "2026-11-05T12:00:00Z"]
        }
      },
      "data_queries": {"position": {}, "area": {}},
      "parameter_names": {
        "TMP": {"id": "TMP", "label": "Temperature"},
        "DPT": {"id": "DPT", "label": "Dew Point"},
        "RH": {"id": "RH", "label": "Relative Humidity"}
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
      "data_queries": {"position": {}, "cube": {}},
      "parameter_names": {"TMP": {"id": "TMP", "label": "Temperature"}}
    },
    {
      "id": "nbm-conus-2m-agl",
      "title": "NBM CONUS - 2m AGL",
      "extent": {
        "spatial": {"bbox": [[-125.0, 24.0, -66.0, 50.0]]},
        "temporal": {"interval": [["2026-11-04T12:00:00Z", "2026-11-06T14:00:00Z"]]}
      },
      "data_queries": {"position": {}, "area": {}, "radius": {}},
      "parameter_names": {
        "TMP": {"id": "TMP", "label": "Temperature"},
        "TMAX": {"id": "TMAX", "label": "Max Temperature"},
        "TMIN": {"id": "TMIN", "label": "Min Temperature"}
      }
    }
  ]
}
"""


def make_resolver():
    caps = parse_collections(COLLECTIONS_JSON, "https://example.com/edr")
    return EDRResolver(caps)


def test_basic_resolve():
    r = make_resolver().resolve("hrrr surface")
    assert r.collection.id == "hrrr-surface"
    assert r.confidence >= 60


def test_resolve_with_parameter():
    r = make_resolver().resolve("hrrr surface temp")
    assert r.collection.id == "hrrr-surface"
    assert r.parameter == "TMP"


def test_resolve_isobaric():
    r = make_resolver().resolve("hrrr isobaric")
    assert r.collection.id == "hrrr-isobaric"


def test_fuzzy_partial_words():
    r = make_resolver().resolve("nbm conus temp max")
    assert r.collection.id == "nbm-conus-2m-agl"
    assert r.parameter == "TMAX"


def test_garbage_query_rejected():
    try:
        make_resolver().resolve("asdfqwerzxcv")
        assert False, "should have raised"
    except AmbiguousCollectionError as e:
        assert isinstance(e.alternatives, list)


def test_dimension_token_extraction():
    resolver = make_resolver()
    r = resolver.resolve("hrrr surface 6h")
    assert r.collection.id == "hrrr-surface"
    assert r.extracted.get("datetime") == "6h"
    r2 = resolver.resolve("hrrr isobaric 850")
    assert r2.extracted.get("z") == "850"


def test_datetime_latest():
    resolver = make_resolver()
    c = resolver.capabilities.get_collection("hrrr-surface")
    value, warns = resolver.resolve_datetime("latest", c)
    assert value == "2026-11-05T12:00:00Z"


def test_datetime_offset():
    resolver = make_resolver()
    c = resolver.capabilities.get_collection("hrrr-surface")
    # 6h from extent start
    value, warns = resolver.resolve_datetime("6h", c)
    assert value == "2026-11-04T18:00:00Z"
    # ISO duration form
    value2, _ = resolver.resolve_datetime("PT6H", c)
    assert value2 == value
    # Clamped to extent end
    value3, warns3 = resolver.resolve_datetime("P2D", c)
    assert value3 == "2026-11-05T12:00:00Z"
    assert any("Clamped" in w for w in warns3)


def test_datetime_passthrough():
    resolver = make_resolver()
    c = resolver.capabilities.get_collection("hrrr-surface")
    value, _ = resolver.resolve_datetime("2026-11-05T00:00:00Z", c)
    assert value == "2026-11-05T00:00:00Z"
    ivalue, _ = resolver.resolve_datetime("2026-11-04T12:00:00Z/2026-11-05T00:00:00Z", c)
    assert "/" in ivalue


def test_z_snapping():
    resolver = make_resolver()
    c = resolver.capabilities.get_collection("hrrr-isobaric")
    value, warns = resolver.resolve_z("850mb", c)
    assert value == "850"
    value2, warns2 = resolver.resolve_z("600", c)
    assert value2 in ("500", "700")   # nearest level to 600
    assert any("snapped" in w for w in warns2)
    # Interval passthrough
    value3, _ = resolver.resolve_z("500/850", c)
    assert value3 == "500/850"


def test_z_no_vertical_extent():
    resolver = make_resolver()
    c = resolver.capabilities.get_collection("hrrr-surface")
    value, _ = resolver.resolve_z("850", c)
    assert value == "850"


def test_parse_coords():
    wkt, warns = parse_coords("-105,40")
    assert wkt == "POINT(-105.0 40.0)"
    wkt2, _ = parse_coords("-105 40")
    assert wkt2 == "POINT(-105.0 40.0)"
    wkt3, _ = parse_coords("-105.5,40.1;-104.2,41.9")
    assert wkt3 == "LINESTRING(-105.5 40.1,-104.2 41.9)"
    # WKT passthrough
    wkt4, _ = parse_coords("point(-105 40)")
    assert wkt4 == "point(-105 40)"
    wkt5, _ = parse_coords("POLYGON((-105 39, -104 39, -104 41, -105 41, -105 39))")
    assert wkt5.startswith("POLYGON")
    # Bad input
    wkt6, warns6 = parse_coords("garbage")
    assert wkt6 is None
    assert warns6


def test_bbox_to_wkt():
    wkt = bbox_to_wkt("-105,39,-104,41")
    assert wkt == "POLYGON((-105.0 39.0,-104.0 39.0,-104.0 41.0,-105.0 41.0,-105.0 39.0))"
    assert bbox_to_wkt("bad,input") is None
    assert bbox_to_wkt("-104,39,-105,41") is None   # minx > maxx


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
