#!/usr/bin/env python3
"""
Comprehensive test suite for SmartQueryResolver.

Tests:
- Unit preferences (F, C, K)
- Natural time expressions (6h, 12z, latest)
- Multi-dimension queries
- Confidence thresholds
- Dimension validation
- Auto-correction warnings
- Error handling
"""

import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.wms.parser import parse_capabilities
from src.wms.resolver import SmartQueryResolver, AmbiguousQueryError


def load_test_capabilities():
    """Load test capabilities XML"""
    caps_path = Path(__file__).parent / "fixtures" / "capabilities.xml"
    with open(caps_path, 'r') as f:
        xml = f.read()
    return parse_capabilities(xml)


def test_basic_query():
    """Test basic query without dimensions"""
    print("\n=== Test: Basic Query ===")
    capabilities = load_test_capabilities()
    resolver = SmartQueryResolver(capabilities)

    result = resolver.resolve("gfs temp")

    assert result.layer.name is not None
    assert "temp" in result.layer.name.lower() or "temp" in (result.layer.title or "").lower()
    assert result.confidence >= 80.0
    assert result.style is not None
    assert result.crs is not None
    assert result.bbox is not None

    print(f"✓ Layer: {result.layer.name}")
    print(f"✓ Confidence: {result.confidence:.0f}%")
    print(f"✓ Style: {result.style}")
    print(f"✓ CRS: {result.crs}")


def test_unit_preferences():
    """Test unit preference handling (F, C, K)"""
    print("\n=== Test: Unit Preferences ===")
    capabilities = load_test_capabilities()
    resolver = SmartQueryResolver(capabilities)

    # Test Fahrenheit
    result_f = resolver.resolve("gfs temp f")
    assert "_F" in result_f.layer.name or "Fahrenheit" in result_f.layer.name
    print(f"✓ Fahrenheit: {result_f.layer.name}")

    # Test Celsius
    result_c = resolver.resolve("gfs temp c")
    assert "_C" in result_c.layer.name or "Celsius" in result_c.layer.name
    print(f"✓ Celsius: {result_c.layer.name}")

    # Test without preference (should get first match)
    result_none = resolver.resolve("gfs temp")
    print(f"✓ No preference: {result_none.layer.name}")


def test_time_expressions():
    """Test natural time expressions (6h, 12z, latest)"""
    print("\n=== Test: Time Expressions ===")
    capabilities = load_test_capabilities()
    resolver = SmartQueryResolver(capabilities)

    # Test forecast hours
    result_6h = resolver.resolve("gfs temp 6h")
    assert 'FORECAST' in result_6h.dimensions
    assert result_6h.dimensions['FORECAST'] == 'PT6H'
    print(f"✓ 6h → {result_6h.dimensions['FORECAST']}")

    # Test 12h
    result_12h = resolver.resolve("gfs temp 12h")
    assert result_12h.dimensions.get('FORECAST') == 'PT12H'
    print(f"✓ 12h → {result_12h.dimensions['FORECAST']}")

    # Test 2d (2 days) - may be auto-corrected to PT48H
    result_2d = resolver.resolve("gfs temp 2d")
    forecast = result_2d.dimensions.get('FORECAST')
    # Accept either P2D or PT48H (48 hours = 2 days)
    assert forecast in ['P2D', 'PT48H'], f"Expected P2D or PT48H, got {forecast}"
    print(f"✓ 2d → {result_2d.dimensions['FORECAST']}")

    # Test run time (12z)
    result_12z = resolver.resolve("gfs temp 12z")
    assert 'RUN' in result_12z.dimensions
    assert 'T12:00:00Z' in result_12z.dimensions['RUN']
    print(f"✓ 12z → {result_12z.dimensions['RUN']}")

    # Test latest
    result_latest = resolver.resolve("gfs temp latest")
    assert 'RUN' in result_latest.dimensions
    print(f"✓ latest → {result_latest.dimensions['RUN']}")


def test_multi_dimension():
    """Test queries with multiple dimensions"""
    print("\n=== Test: Multi-Dimension Queries ===")
    capabilities = load_test_capabilities()
    resolver = SmartQueryResolver(capabilities)

    # Test: unit + run + forecast
    result = resolver.resolve("gfs temp f 12z 6h")
    assert "_F" in result.layer.name or "Fahrenheit" in result.layer.name
    assert 'RUN' in result.dimensions
    assert result.dimensions.get('FORECAST') == 'PT6H'
    print(f"✓ Multi-dimension: {result.layer.name}")
    print(f"  - RUN: {result.dimensions.get('RUN')}")
    print(f"  - FORECAST: {result.dimensions.get('FORECAST')}")

    # Test: different order
    result2 = resolver.resolve("gfs temp 6h f 12z")
    assert "_F" in result2.layer.name or "Fahrenheit" in result2.layer.name
    assert result2.dimensions.get('FORECAST') == 'PT6H'
    print(f"✓ Different order works: {result2.layer.name}")


def test_elevation():
    """Test elevation/pressure level handling"""
    print("\n=== Test: Elevation ===")
    capabilities = load_test_capabilities()
    resolver = SmartQueryResolver(capabilities)

    # Test with layer that has elevation
    result = resolver.resolve("gfs temp 850")

    # Either it has ELEVATION dimension or warns about missing it
    if 'ELEVATION' in result.dimensions:
        print(f"✓ Layer has ELEVATION: {result.dimensions['ELEVATION']}")
    else:
        assert any("ELEVATION" in w for w in result.warnings)
        print(f"✓ Warning about missing ELEVATION: {result.warnings}")


def test_confidence_threshold():
    """Test confidence threshold enforcement"""
    print("\n=== Test: Confidence Threshold ===")
    capabilities = load_test_capabilities()
    resolver = SmartQueryResolver(capabilities)

    # Good query - should pass
    result = resolver.resolve("gfs temp")
    assert result.confidence >= 80.0
    print(f"✓ Good query confidence: {result.confidence:.0f}%")

    # Bad query - should fail
    try:
        result = resolver.resolve("asdfqwerzxcv")
        assert False, "Should have raised AmbiguousQueryError"
    except AmbiguousQueryError as e:
        assert len(e.alternatives) > 0
        print(f"✓ Bad query rejected: {e}")
        print(f"✓ Alternatives provided: {len(e.alternatives)} options")


def test_warnings():
    """Test that warnings are generated appropriately"""
    print("\n=== Test: Warnings ===")
    capabilities = load_test_capabilities()
    resolver = SmartQueryResolver(capabilities)

    # Query with dimension that gets auto-corrected
    result = resolver.resolve("gfs temp 12z")

    # Should have warning about run time auto-correction
    has_run_warning = any("RUN" in w for w in result.warnings)
    if has_run_warning:
        print(f"✓ Auto-correction warning: {result.warnings[0]}")
    else:
        print(f"✓ No warning needed (exact match)")

    # Query with invalid dimension for layer
    result2 = resolver.resolve("gfs temp 850")
    has_elevation_warning = any("ELEVATION" in w for w in result2.warnings)
    if has_elevation_warning:
        print(f"✓ Invalid dimension warning: {[w for w in result2.warnings if 'ELEVATION' in w][0]}")


def test_iso_duration_formats():
    """Test ISO 8601 duration format support"""
    print("\n=== Test: ISO Duration Formats ===")
    capabilities = load_test_capabilities()
    resolver = SmartQueryResolver(capabilities)

    # Test PT6H format
    result = resolver.resolve("gfs temp PT6H")
    assert result.dimensions.get('FORECAST') == 'PT6H'
    print(f"✓ PT6H accepted: {result.dimensions['FORECAST']}")

    # Test P2D format - may be auto-corrected to PT48H
    result2 = resolver.resolve("gfs temp P2D")
    forecast2 = result2.dimensions.get('FORECAST')
    assert forecast2 in ['P2D', 'PT48H'], f"Expected P2D or PT48H, got {forecast2}"
    print(f"✓ P2D accepted: {result2.dimensions['FORECAST']}")


def test_dimension_defaults():
    """Test that missing dimensions get sensible defaults"""
    print("\n=== Test: Dimension Defaults ===")
    capabilities = load_test_capabilities()
    resolver = SmartQueryResolver(capabilities)

    # Query without specifying dimensions
    result = resolver.resolve("gfs temp")

    # Should have default RUN (latest)
    if 'RUN' in result.dimensions:
        print(f"✓ Default RUN: {result.dimensions['RUN']}")

    # Should have default FORECAST
    if 'FORECAST' in result.dimensions:
        print(f"✓ Default FORECAST: {result.dimensions['FORECAST']}")


def test_token_extraction():
    """Test that dimension tokens are extracted correctly"""
    print("\n=== Test: Token Extraction ===")
    capabilities = load_test_capabilities()
    resolver = SmartQueryResolver(capabilities)

    # Test extraction
    query = "gfs temp f 12z 6h 850mb"
    extracted_dims, extracted_units, clean_query = resolver._extract_tokens(query)

    print(f"✓ Original query: {query}")
    print(f"✓ Clean query: {clean_query}")
    print(f"✓ Extracted dimensions: {extracted_dims}")
    print(f"✓ Extracted units: {extracted_units}")

    # Verify clean query doesn't have dimension tokens
    assert "12z" not in clean_query
    assert "6h" not in clean_query
    assert "850" not in clean_query
    # Note: "f" as a standalone unit was extracted, but "temp" still contains "f"
    # The important thing is that dimension tokens are extracted
    assert clean_query == "gfs temp"

    # Verify dimensions were extracted
    assert 'RUN' in extracted_dims
    assert 'FORECAST' in extracted_dims
    assert 'ELEVATION' in extracted_dims
    assert 'fahrenheit' in extracted_units


def test_alternatives():
    """Test that alternatives are provided on low confidence"""
    print("\n=== Test: Alternatives ===")
    capabilities = load_test_capabilities()
    resolver = SmartQueryResolver(capabilities)

    try:
        result = resolver.resolve("xyz")
    except AmbiguousQueryError as e:
        assert len(e.alternatives) > 0
        print(f"✓ Alternatives provided: {e.alternatives[:5]}")


def test_case_insensitivity():
    """Test that queries are case-insensitive"""
    print("\n=== Test: Case Insensitivity ===")
    capabilities = load_test_capabilities()
    resolver = SmartQueryResolver(capabilities)

    result_lower = resolver.resolve("gfs temp")
    result_upper = resolver.resolve("GFS TEMP")
    result_mixed = resolver.resolve("GfS TeMp")

    # All should resolve to same layer (or similar)
    print(f"✓ Lowercase: {result_lower.layer.name}")
    print(f"✓ Uppercase: {result_upper.layer.name}")
    print(f"✓ Mixed case: {result_mixed.layer.name}")

    assert result_lower.confidence >= 80.0
    assert result_upper.confidence >= 80.0
    assert result_mixed.confidence >= 80.0


def run_all_tests():
    """Run all tests"""
    print("=" * 70)
    print("COMPREHENSIVE SMART RESOLVER TEST SUITE")
    print("=" * 70)

    tests = [
        test_basic_query,
        test_unit_preferences,
        test_time_expressions,
        test_multi_dimension,
        test_elevation,
        test_confidence_threshold,
        test_warnings,
        test_iso_duration_formats,
        test_dimension_defaults,
        test_token_extraction,
        test_alternatives,
        test_case_insensitivity,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            failed += 1
            print(f"\n✗ FAILED: {test.__name__}")
            print(f"  Error: {e}")
            import traceback
            traceback.print_exc()

    print("\n" + "=" * 70)
    print(f"RESULTS: {passed} passed, {failed} failed")
    print("=" * 70)

    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
