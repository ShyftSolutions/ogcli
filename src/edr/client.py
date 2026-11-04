#!/usr/bin/env python3
"""
EDR HTTP client.
Handles requests to OGC API - EDR servers: collections, query patterns
(position, area, cube, trajectory, corridor, radius), locations, instances.
"""

import logging
import re
from typing import Optional, Dict, Tuple

import requests

from .parser import parse_collections, EDRCapabilities

logger = logging.getLogger(__name__)


# Unit -> meters conversion for distance parameters (within, corridor-width).
# Some servers accept '100km'; others (e.g. Met Office Labs) require a bare
# number in meters.
_DISTANCE_UNITS = {
    'km': 1000.0, 'kilometers': 1000.0, 'kilometres': 1000.0,
    'm': 1.0, 'meters': 1.0, 'metres': 1.0,
    'mi': 1609.344, 'miles': 1609.344,
    'nmi': 1852.0, 'ft': 0.3048, 'feet': 0.3048,
}


def _distance_to_meters(value: str):
    """
    Convert '100km' -> 100000.0. Returns None if the value has no
    unit suffix (already numeric) or can't be parsed.
    """
    m = re.match(r'^([0-9.]+)\s*([a-zA-Z]+)$', value.strip())
    if not m:
        return None
    num, unit = float(m.group(1)), m.group(2).lower()
    if unit not in _DISTANCE_UNITS:
        return None
    return num * _DISTANCE_UNITS[unit]


def corridor_params(width: str) -> Dict[str, str]:
    """
    Build corridor width parameters from a user expression.

    Splits '10km' into corridor-width=10 + width-units=km (the form most
    servers require); passes anything else through as corridor-width.
    """
    m = re.match(r'^([0-9.]+)\s*([a-zA-Z]+)$', width.strip())
    if m:
        return {'corridor-width': m.group(1), 'width-units': m.group(2).lower()}
    return {'corridor-width': width}


class EDRClient:
    """HTTP client for EDR operations"""

    def __init__(self, base_url: str, api_key: str = "", timeout: int = 30):
        """
        Initialize EDR client.

        Args:
            base_url: Base URL of the EDR service (e.g. "https://folkweather.com/edr")
            api_key: Optional API key sent as 'apikey' query parameter
            timeout: Request timeout in seconds
        """
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'edr-cli/1.0',
            'Accept': 'application/json, application/vnd.cov+json, application/geo+json',
        })

    def _check(self, response: requests.Response) -> requests.Response:
        """Raise on HTTP errors, surfacing the server's detail if present."""
        if response.status_code >= 400:
            # Surface the server's error detail (OGC API exceptions) if
            # present - servers use varying key names
            detail = ''
            try:
                err = response.json()
                if isinstance(err, dict):
                    detail = (err.get('detail') or err.get('description')
                              or err.get('message') or err.get('title') or '')
            except Exception:
                pass
            if detail:
                raise requests.HTTPError(
                    f"HTTP {response.status_code}: {detail}", response=response
                )
        response.raise_for_status()
        return response

    def _get(self, url: str, params: Optional[dict] = None,
             raise_soft: bool = False) -> requests.Response:
        """
        Fetch a URL.

        Args:
            url: Absolute URL
            params: Query parameters (empties removed; api key injected)
            raise_soft: If True, return the response even on HTTP errors
                (caller handles retries)
        """
        params = dict(params or {})
        if self.api_key:
            params.setdefault('apikey', self.api_key)
        params = {k: v for k, v in params.items() if v is not None and v != ''}
        response = self.session.get(url, params=params, timeout=self.timeout)
        if raise_soft:
            return response
        return self._check(response)

    @staticmethod
    def _collections_url(base_url: str, collection_id: str,
                         query_type: Optional[str] = None,
                         instance_id: Optional[str] = None) -> str:
        url = f"{base_url.rstrip('/')}/collections/{collection_id}"
        if instance_id:
            url += f"/instances/{instance_id}"
        if query_type:
            url += f"/{query_type}"
        return url

    def get_landing(self) -> dict:
        """Fetch the landing page JSON"""
        return self._get(self.base_url).json()

    def get_collections(self) -> EDRCapabilities:
        """Fetch and parse the /collections document"""
        response = self._get(f"{self.base_url}/collections")
        caps = parse_collections(response.text, self.base_url)
        logger.info(f"Successfully parsed {len(caps.collections)} EDR collections")
        return caps

    def query(self,
              collection_id: str,
              query_type: str,
              coords: Optional[str] = None,
              bbox: Optional[str] = None,
              datetime: Optional[str] = None,
              z: Optional[str] = None,
              parameter_name: Optional[str] = None,
              within: Optional[str] = None,
              width: Optional[str] = None,
              crs: Optional[str] = None,
              format_: Optional[str] = None,
              instance_id: Optional[str] = None,
              **extra_params) -> Tuple[bytes, str]:
        """
        Run an EDR query (position, area, cube, trajectory, corridor, radius).

        Args:
            collection_id: Collection id
            query_type: One of the collection's advertised query types
            coords: WKT geometry (POINT/POLYGON/LINESTRING)
            bbox: Bounding box "minx,miny,maxx,maxy" (cube queries)
            datetime: ISO8601 instant or interval (a/b)
            z: Vertical level or interval (low/high)
            parameter_name: Comma-separated parameter names to filter
            within: Radius search distance (e.g. "25km")
            width: Corridor width (e.g. "10km"; sent as corridor-width)
            crs: Output CRS
            format_: Output format (f= parameter)
            instance_id: Query a specific model-run instance
            extra_params: Any additional query parameters (e.g. hyphenated names)

        Returns:
            Tuple of (raw body bytes, content type)
        """
        url = self._collections_url(self.base_url, collection_id, query_type, instance_id)
        params = {
            'coords': coords,
            'bbox': bbox,
            'datetime': datetime,
            'z': z,
            'parameter-name': parameter_name,
            'within': within,
            **(corridor_params(width) if width else {}),
            'crs': crs,
            'f': format_,
            **extra_params,
        }
        params = {k: v for k, v in params.items() if v is not None and v != ''}

        logger.info(f"EDR {query_type} query on '{collection_id}'")
        logger.debug(f"EDR query URL: {url}")
        response = self._get(url, params, raise_soft=True)

        # Some servers reject unit-suffixed distances ('100km') with
        # 400/422 - retry once with bare meters
        if response.status_code in (400, 422):
            converted = self._convert_distances(params)
            if converted is not None:
                logger.debug("Retrying with bare-meter distances...")
                response = self._get(url, converted, raise_soft=True)

        self._check(response)
        return response.content, response.headers.get('Content-Type', '')

    @staticmethod
    def _convert_distances(params: dict) -> Optional[dict]:
        """Convert unit-suffixed distance params ('100km') to bare meters.

        Returns a new params dict, or None if nothing to convert.
        """
        converted = dict(params)
        changed = False
        for key in ('within', 'corridor-width'):
            val = converted.get(key)
            if isinstance(val, str):
                meters = _distance_to_meters(val)
                if meters is not None:
                    converted[key] = f"{meters:g}"
                    changed = True
        return converted if changed else None

    def get_instances(self, collection_id: str) -> dict:
        """List instances (model runs) for a collection"""
        url = f"{self.base_url.rstrip('/')}/collections/{collection_id}/instances"
        return self._get(url).json()

    def get_locations(self, collection_id: str, **filters) -> dict:
        """List named locations for a collection"""
        url = f"{self.base_url.rstrip('/')}/collections/{collection_id}/locations"
        return self._get(url, filters).json()

    def get_location(self, collection_id: str, location_id: str,
                     datetime_: Optional[str] = None,
                     z: Optional[str] = None,
                     parameter_name: Optional[str] = None) -> Tuple[bytes, str]:
        """Query data at a named location"""
        url = f"{self.base_url.rstrip('/')}/collections/{collection_id}/locations/{location_id}"
        params = {
            'datetime': datetime_,
            'z': z,
            'parameter-name': parameter_name,
        }
        params = {k: v for k, v in params.items() if v is not None and v != ''}
        response = self._get(url, params)
        return response.content, response.headers.get('Content-Type', '')
