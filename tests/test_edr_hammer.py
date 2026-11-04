#!/usr/bin/env python3
"""
Tests for the EDR hammer (stats tracking + request generation).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.edr.parser import parse_collections
from src.edr.hammer import EDRHammer, HammerStats

COLLECTIONS_JSON = """
{
  "collections": [
    {
      "id": "test-surface",
      "title": "Test Surface",
      "extent": {
        "spatial": {"bbox": [[-110.0, 35.0, -100.0, 42.0]]},
        "temporal": {
          "interval": [["2026-11-04T12:00:00Z", "2026-11-04T18:00:00Z"]],
          "values": ["2026-11-04T12:00:00Z", "2026-11-04T15:00:00Z",
                     "2026-11-04T18:00:00Z"]
        }
      },
      "data_queries": {"position": {}, "area": {}},
      "parameter_names": {
        "TMP": {"id": "TMP"},
        "RH": {"id": "RH"}
      }
    },
    {
      "id": "test-isobaric",
      "title": "Test Isobaric",
      "extent": {
        "spatial": {"bbox": [[-110.0, 35.0, -100.0, 42.0]]},
        "temporal": {"interval": [["2026-11-04T12:00:00Z", "2026-11-04T18:00:00Z"]]},
        "vertical": {"interval": [[500, 850]], "values": [500, 850]}
      },
      "data_queries": {"position": {}, "cube": {}},
      "parameter_names": {"HGT": {"id": "HGT"}}
    }
  ]
}
"""


def make_hammer(**kwargs):
    caps = parse_collections(COLLECTIONS_JSON, "https://example.com/edr")
    defaults = dict(base_url="https://example.com/edr",
                    collections=caps.collections)
    defaults.update(kwargs)
    return EDRHammer(**defaults)


def test_stats_record_and_report():
    stats = HammerStats(total=3)
    stats.start = 0.0
    stats.end = 1.0
    stats.record(True, 1000, 0.5, 200)
    stats.record(True, 2000, 0.3, 200)
    stats.record(False, 0, 0.2, 404)

    assert stats.success == 2
    assert stats.failed == 1
    assert stats.bytes == 3000
    assert stats.status_counts == {"200": 2, "404": 1}

    report = stats.report()
    assert "3 total, 2 ok, 1 failed" in report
    assert "200: 2" in report and "404: 1" in report
    assert "3.0 req/s" in report
    assert "Latency:" in report


def test_generate_full_sweep():
    hammer = make_hammer(save=False)
    requests = hammer.generate()

    # test-surface: 3 timesteps x 2 params = 6
    # test-isobaric: 1 timestep (none) x 1 param x 2 levels = 2
    assert len(requests) == 8
    surface = [r for r in requests if r.collection == "test-surface"]
    iso = [r for r in requests if r.collection == "test-isobaric"]
    assert len(surface) == 6
    assert len(iso) == 2

    # All surface requests are points within the bbox
    for r in surface:
        assert r.params["coords"].startswith("POINT(")
        assert r.query_type == "position"
        assert r.params["parameter-name"] in ("TMP", "RH")
        assert r.params["datetime"]


def test_generate_cube_uses_bbox():
    hammer = make_hammer(save=False, query_type="cube")
    requests = hammer.generate()
    # Only test-isobaric supports cube
    assert all(r.collection == "test-isobaric" for r in requests)
    assert all(r.query_type == "cube" for r in requests)
    assert all(r.params["bbox"] == "-110.0,35.0,-100.0,42.0" for r in requests)


def test_generate_sampling_and_limit():
    hammer = make_hammer(save=False, max_timesteps=2, max_params=1, limit=3)
    requests = hammer.generate()
    assert len(requests) == 3
    # With max_params=1, only one parameter per collection is used
    assert len({r.params["parameter-name"] for r in requests
                if r.collection == "test-surface"}) == 1


def test_generate_skips_unsupported_query_type():
    hammer = make_hammer(save=False, query_type="radius")
    requests = hammer.generate()
    assert requests == []   # no collection supports radius in the fixture


def test_render_status_smoke():
    """The rich panel builds without error."""
    from collections import deque
    import time
    state = {'total': 2, 'done': 2, 'ok': 1, 'failed': 1, 'bytes': 2048,
             'latencies': [0.1, 0.2], 'recent': deque([
                 ('14:00:01', 'test-surface:TMP', 200, 1024, 0.1),
                 ('14:00:02', 'test-isobaric:HGT', 500, 0, 0.2),
             ], maxlen=8), 'last_error': 'boom', 'start': time.time() - 1}
    panel = EDRHammer._render_status(state)
    assert panel is not None


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
