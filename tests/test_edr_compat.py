#!/usr/bin/env python3
"""
Tests for cross-server compatibility hardening in the EDR client/parser.

Covers quirks found verifying against Met Office Labs and USGS waterdata:
- unit.symbol as a nested dict (Labs)
- CRS as full WKT instead of a URI (Labs)
- distance params requiring bare meters instead of '100km' (Labs)
- error detail under 'description'/'message' keys (Labs, USGS)
- collection ids containing slashes (USGS 'edr/daily')
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.edr.client import EDRClient, _distance_to_meters
from src.edr.parser import Parameter, _pretty_crs, parse_collections


# ----------------------------------------------------------------------
# Distance handling
# ----------------------------------------------------------------------

def test_distance_to_meters():
    assert _distance_to_meters('100km') == 100000.0
    assert _distance_to_meters('10 km') == 10000.0
    assert _distance_to_meters('25m') == 25.0
    assert _distance_to_meters('10mi') == 16093.44
    # No suffix / unparseable -> None (leave as-is)
    assert _distance_to_meters('100000') is None
    assert _distance_to_meters('100foos') is None
    assert _distance_to_meters('km') is None


def test_convert_distances():
    convert = EDRClient._convert_distances
    out = convert({'within': '100km'})
    assert out == {'within': '100000'}
    out = convert({'within': '25km', 'corridor-width': '10km'})
    assert out == {'within': '25000', 'corridor-width': '10000'}
    # Leaves other params and non-suffixed values alone
    out = convert({'within': '5000', 'coords': 'POINT(1 2)'})
    assert out is None


# ----------------------------------------------------------------------
# Parameter unit quirks
# ----------------------------------------------------------------------

def test_parameter_nested_symbol_dict():
    """Met Office Labs: unit.symbol is {'value': 'km2', 'type': '<uri>'}"""
    p = Parameter.from_json({
        'type': 'Parameter',
        'label': None,
        'unit': {
            'label': 'Number per square kilometre',
            'symbol': {'value': 'km2', 'type': 'https://qudt.org/vocab/unit/X'},
        },
        'observedProperty': {
            'id': 'https://en.wikipedia.org/wiki/Population_density',
            'label': 'Pop_Density',
        },
    })
    assert p.unit == 'km2'
    assert p.label == ''          # null label -> empty, not 'None'
    assert p.observed_property == 'Pop_Density'


def test_parameter_unit_string_still_works():
    p = Parameter.from_json({'id': 'TMP', 'label': 'T', 'unit': 'K'})
    assert p.unit == 'K'


# ----------------------------------------------------------------------
# CRS quirks
# ----------------------------------------------------------------------

def test_pretty_crs_wkt_to_epsg():
    wkt = ('GEOGCS["WGS 84",DATUM["WGS_1984"],'
           'AUTHORITY["EPSG","6326"]],AUTHORITY["EPSG","4326"]]')
    assert _pretty_crs(wkt) == 'EPSG:4326'
    # URIs pass through untouched
    assert _pretty_crs('CRS:84') == 'CRS:84'
    assert _pretty_crs('http://www.opengis.net/def/crs/OGC/1.3/CRS84') == \
        'http://www.opengis.net/def/crs/OGC/1.3/CRS84'


def test_collections_parse_wkt_crs():
    caps = parse_collections("""
    {
      "collections": [{
        "id": "c",
        "extent": {"spatial": {"bbox": [[0, 0, 1, 1]],
          "crs": "GEOGCS[\\"WGS 84\\",AUTHORITY[\\"EPSG\\",\\"4326\\"]]"}}
      }]
    }
    """)
    assert caps.collections[0].crs == 'EPSG:4326'


# ----------------------------------------------------------------------
# Error detail extraction
# ----------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body

    def raise_for_status(self):
        import requests
        raise requests.HTTPError(f"{self.status_code} error", response=None)


def test_error_detail_description_key():
    """Labs/USGS style: {'code': '400', 'description': '...'}"""
    import requests
    client = EDRClient('https://example.com/edr')
    resp = FakeResponse(400, {'code': '400', 'description': 'Invalid request'})
    try:
        client._check(resp)
        assert False, 'should raise'
    except requests.HTTPError as e:
        assert 'Invalid request' in str(e)


def test_error_detail_detail_key():
    import requests
    client = EDRClient('https://example.com/edr')
    resp = FakeResponse(400, {'detail': 'Missing bbox'})
    try:
        client._check(resp)
        assert False, 'should raise'
    except requests.HTTPError as e:
        assert 'Missing bbox' in str(e)


# ----------------------------------------------------------------------
# Slash-in-id collections (USGS 'edr/daily')
# ----------------------------------------------------------------------

def test_slash_collection_id():
    caps = parse_collections("""
    {
      "collections": [{
        "id": "edr/daily",
        "title": "Daily values EDR",
        "data_queries": {"locations": {}},
        "parameter_names": {"00060": {"id": "00060", "label": "Streamflow"}}
      }]
    }
    """)
    c = caps.get_collection('edr/daily')
    assert c is not None
    assert c.supports('locations')

    # Resolver handles the slash (tokenized as 'edr' + 'daily')
    from src.edr.resolver import EDRResolver
    r = EDRResolver(caps)
    res = r.resolve('edr daily')
    assert res.collection.id == 'edr/daily'

    # Client builds the right URL path
    url = EDRClient._collections_url('https://example.com/ogcapi/v0',
                                     'edr/daily', 'locations')
    assert url == 'https://example.com/ogcapi/v0/collections/edr/daily/locations'


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
