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

    def _get(self, url: str, params: Optional[dict] = None) -> requests.Response:
        params = dict(params or {})
        if self.api_key:
            params.setdefault('apikey', self.api_key)
        params = {k: v for k, v in params.items() if v is not None and v != ''}
        response = self.session.get(url, params=params, timeout=self.timeout)
        if response.status_code >= 400:
            # Surface the server's error detail (OGC API exceptions) if present
            detail = ''
            try:
                err = response.json()
                detail = err.get('detail') or err.get('title') or ''
            except Exception:
                pass
            if detail:
                raise requests.HTTPError(
                    f"HTTP {response.status_code}: {detail}", response=response
                )
        response.raise_for_status()
        return response

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
        response = self._get(url, params)
        return response.content, response.headers.get('Content-Type', '')

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
