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


def test_parse_duration():
    from src.edr.hammer import parse_duration
    assert parse_duration("30s") == 30.0
    assert parse_duration("5m") == 300.0
    assert parse_duration("2h") == 7200.0
    assert parse_duration("45") == 45.0
    import pytest as _pytest
    with _pytest.raises(ValueError):
        parse_duration("banana")
    with _pytest.raises(ValueError):
        parse_duration("")


def test_warmup_exclusion():
    stats = HammerStats()
    stats.record(True, 100, 0.9, 200, is_warmup=True)   # warmup
    stats.record(True, 100, 0.1, 200)
    stats.record(True, 100, 0.3, 200)
    assert stats.warmup == 1
    assert stats.success == 3
    assert len(stats.latencies) == 2          # warmup excluded
    assert stats.latencies == [0.9 * 0 + 0.1, 0.3][0:2] or sorted(stats.latencies) == [0.1, 0.3]
    ms = stats._latency_summary_ms()
    assert ms["min"] == 100.0 and ms["max"] == 300.0


def test_histogram_lines():
    stats = HammerStats()
    for lat in [0.1, 0.1, 0.1, 0.2, 0.3]:
        stats.record(True, 0, lat, 200)
    lines = stats.histogram(buckets=4)
    assert len(lines) == 4
    assert "│" in lines[0]
    # Counts sum to the number of recorded latencies
    total = sum(int(line.split()[-1]) for line in lines)
    assert total == 5


def test_report_dict():
    stats = HammerStats()
    stats.start, stats.end = 0.0, 2.0
    stats.total = 4
    stats.record(True, 1024, 0.1, 200)
    stats.record(True, 1024, 0.3, 200)
    stats.record(False, 0, 0.2, 500)
    stats.record(True, 1024, 0.1, 200, is_warmup=True)

    report = stats.to_report_dict()
    assert report["total"] == 4
    assert report["ok"] == 3 and report["failed"] == 1
    assert report["warmup_excluded"] == 1
    assert report["status_counts"] == {"200": 3, "500": 1}
    assert report["rps_actual"] == 2.0
    assert report["latency_ms"]["p50"] == 200.0


def test_duration_mode_with_pacing(monkeypatch):
    """Duration-bounded cycling run with fake requests (no network)."""
    import itertools
    counter = itertools.count()

    def fake_execute(self, request):
        n = next(counter)
        return (request.label, True, 0.001, 10, None, 200)

    monkeypatch.setattr(EDRHammer, "_execute", fake_execute)

    hammer = make_hammer(save=False, duration=0.4, rps=100,
                         warmup=2, limit=10_000)
    hammer.generate()
    assert len(hammer.requests) > 0

    stats = hammer.run()
    # ~40 requests at 100 rps over 0.4s; allow generous scheduling slack
    assert 15 <= stats.total <= 60
    assert stats.warmup == 2
    assert stats.failed == 0
    assert len(stats.latencies) == stats.total - 2


def test_limit_cap_applies_in_duration_mode(monkeypatch):
    import itertools
    counter = itertools.count()

    def fake_execute(self, request):
        next(counter)
        return (request.label, True, 0.001, 10, None, 200)

    monkeypatch.setattr(EDRHammer, "_execute", fake_execute)

    hammer = make_hammer(save=False, duration=5, limit=7)
    hammer.generate()
    stats = hammer.run()
    assert stats.total == 7


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
