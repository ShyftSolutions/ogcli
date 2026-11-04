#!/usr/bin/env python3
"""
WMS HTTP Client
Handles HTTP requests to WMS servers for GetCapabilities, GetMap, GetGTile, etc.
"""

import requests
import logging
from typing import Optional, Dict, Any
from .parser import parse_capabilities, WMSCapabilities

logger = logging.getLogger(__name__)


class WMSClient:
    """HTTP client for WMS operations"""

    def __init__(self, base_url: str, timeout: int = 30):
        """
        Initialize WMS client.

        Args:
            base_url: Base URL for WMS service (e.g., "http://localhost:8008/ogc/AFW_WMS")
            timeout: Request timeout in seconds
        """
        self.base_url = base_url.rstrip('?')
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'wms-hammer/1.0'})

    def get_capabilities(self, version: str = "1.3.0") -> WMSCapabilities:
        """
        Fetch and parse GetCapabilities document.

        Args:
            version: WMS version ("1.3.0" or "1.1.1")

        Returns:
            Parsed WMSCapabilities object

        Raises:
            requests.RequestException: On network error
            ValueError: On parse error
        """
        params = {
            'SERVICE': 'WMS',
            'VERSION': version,
            'REQUEST': 'GetCapabilities'
        }

        logger.info(f"Fetching GetCapabilities from {self.base_url}")
        response = self.session.get(self.base_url, params=params, timeout=self.timeout)
        response.raise_for_status()

        # Check content type
        content_type = response.headers.get('Content-Type', '')
        if 'xml' not in content_type.lower():
            logger.warning(f"Unexpected content type: {content_type}")

        # Parse and return
        capabilities = parse_capabilities(response.text)
        logger.info(f"Successfully parsed {len(capabilities.get_queryable_layers())} queryable layers")

        return capabilities

    def get_map(self,
                layer: str,
                bbox: tuple,
                width: int,
                height: int,
                crs: str = "CRS:84",
                format: str = "image/png",
                styles: str = "default",
                version: str = "1.3.0",
                **kwargs) -> bytes:
        """
        Fetch a map image using GetMap request.

        Args:
            layer: Layer name
            bbox: Bounding box as (minx, miny, maxx, maxy)
            width: Image width in pixels
            height: Image height in pixels
            crs: Coordinate reference system (e.g., "CRS:84", "EPSG:4326")
            format: Image format (e.g., "image/png", "image/jpeg")
            styles: Style name(s)
            version: WMS version
            **kwargs: Additional WMS parameters (TIME, ELEVATION, DIM_RUN, DIM_FORECAST, etc.)

        Returns:
            Raw image bytes

        Raises:
            requests.RequestException: On network error
            ValueError: If response is not an image
        """
        params = {
            'SERVICE': 'WMS',
            'VERSION': version,
            'REQUEST': 'GetMap',
            'LAYERS': layer,
            'BBOX': ','.join(map(str, bbox)),
            'WIDTH': width,
            'HEIGHT': height,
            'CRS': crs if version.startswith('1.3') else crs,  # 1.1.1 uses SRS
            'FORMAT': format,
            'STYLES': styles,
        }

        # For WMS 1.1.1, use SRS instead of CRS
        if not version.startswith('1.3'):
            params['SRS'] = params.pop('CRS')

        # Add custom dimensions and other parameters
        for key, value in kwargs.items():
            params[key.upper()] = value

        logger.info(f"Requesting map for layer '{layer}' at {width}x{height}")
        logger.debug(f"GetMap URL: {self.base_url}?{self._format_params(params)}")

        response = self.session.get(self.base_url, params=params, timeout=self.timeout, stream=True)
        response.raise_for_status()

        # Check if response is an image
        content_type = response.headers.get('Content-Type', '')
        if 'image' not in content_type.lower() and 'xml' in content_type.lower():
            # Likely an error message
            error_text = response.text[:500]
            raise ValueError(f"WMS returned error instead of image: {error_text}")

        logger.info(f"Successfully fetched map ({len(response.content)} bytes)")
        return response.content

    def get_gtile(self,
                  layer: str,
                  tilezoom: int,
                  tilerow: int,
                  tilecol: int,
                  crs: str = "EPSG:900913",
                  format: str = "image/png; mode=8bit",
                  style: str = "default",
                  transparent: bool = True,
                  version: str = "1.3.0",
                  **kwargs) -> bytes:
        """
        Fetch a tile using GetGTile request (vendor-specific tile request).

        Args:
            layer: Layer name
            tilezoom: Tile zoom level
            tilerow: Tile row
            tilecol: Tile column
            crs: Coordinate reference system
            format: Image format
            style: Style name
            transparent: Whether to request transparent background
            version: WMS version
            **kwargs: Additional WMS parameters (TIME, ELEVATION, DIM_RUN, DIM_FORECAST, etc.)

        Returns:
            Raw image bytes

        Raises:
            requests.RequestException: On network error
            ValueError: If response is not an image
        """
        params = {
            'SERVICE': 'WMS',
            'VERSION': version,
            'REQUEST': 'GetGTile',
            'LAYER': layer,  # Note: LAYER not LAYERS for GetGTile
            'TILEZOOM': tilezoom,
            'TILEROW': tilerow,
            'TILECOL': tilecol,
            'CRS': crs,
            'FORMAT': format,
            'STYLE': style,
            'TRANSPARENT': 'TRUE' if transparent else 'FALSE',
        }

        # Add custom dimensions
        for key, value in kwargs.items():
            params[key.upper()] = value

        logger.info(f"Requesting tile for layer '{layer}' at zoom={tilezoom}, row={tilerow}, col={tilecol}")
        logger.debug(f"GetGTile URL: {self.base_url}?{self._format_params(params)}")

        response = self.session.get(self.base_url, params=params, timeout=self.timeout, stream=True)
        response.raise_for_status()

        # Check if response is an image
        content_type = response.headers.get('Content-Type', '')
        if 'image' not in content_type.lower() and 'xml' in content_type.lower():
            error_text = response.text[:500]
            raise ValueError(f"WMS returned error instead of tile: {error_text}")

        logger.info(f"Successfully fetched tile ({len(response.content)} bytes)")
        return response.content

    def get_feature_info(self,
                         layer: str,
                         bbox: tuple,
                         width: int,
                         height: int,
                         x: int,
                         y: int,
                         crs: str = "CRS:84",
                         info_format: str = "text/html",
                         version: str = "1.3.0",
                         **kwargs) -> str:
        """
        Get feature information at a specific pixel location.

        Args:
            layer: Layer name
            bbox: Bounding box as (minx, miny, maxx, maxy)
            width: Map width in pixels
            height: Map height in pixels
            x: X coordinate (pixel)
            y: Y coordinate (pixel) - note: called I/J in WMS 1.3.0
            crs: Coordinate reference system
            info_format: Response format (text/html, text/xml, etc.)
            version: WMS version
            **kwargs: Additional parameters (TIME, ELEVATION, etc.)

        Returns:
            Feature info as string (format depends on info_format)

        Raises:
            requests.RequestException: On network error
        """
        params = {
            'SERVICE': 'WMS',
            'VERSION': version,
            'REQUEST': 'GetFeatureInfo',
            'LAYERS': layer,
            'QUERY_LAYERS': layer,
            'BBOX': ','.join(map(str, bbox)),
            'WIDTH': width,
            'HEIGHT': height,
            'CRS': crs,
            'INFO_FORMAT': info_format,
            'STYLES': 'default',
        }

        # WMS 1.3.0 uses I/J, 1.1.1 uses X/Y
        if version.startswith('1.3'):
            params['I'] = x
            params['J'] = y
        else:
            params['X'] = x
            params['Y'] = y
            params['SRS'] = params.pop('CRS')

        # Add custom dimensions
        for key, value in kwargs.items():
            params[key.upper()] = value

        logger.info(f"Requesting feature info for layer '{layer}' at ({x},{y})")
        logger.debug(f"GetFeatureInfo URL: {self.base_url}?{self._format_params(params)}")

        response = self.session.get(self.base_url, params=params, timeout=self.timeout)
        response.raise_for_status()

        return response.text

    def get_legend_graphic(self,
                           layer: str,
                           style: str = "default",
                           format: str = "image/png",
                           version: str = "1.3.0",
                           **kwargs) -> bytes:
        """
        Fetch legend graphic for a layer/style.

        Args:
            layer: Layer name
            style: Style name
            format: Image format
            version: WMS version
            **kwargs: Additional parameters (WIDTH, HEIGHT, etc.)

        Returns:
            Raw image bytes

        Raises:
            requests.RequestException: On network error
        """
        params = {
            'SERVICE': 'WMS',
            'VERSION': version,
            'REQUEST': 'GetLegendGraphic',
            'LAYER': layer,
            'STYLE': style,
            'FORMAT': format,
        }

        # Add optional parameters
        for key, value in kwargs.items():
            params[key.upper()] = value

        logger.info(f"Requesting legend for layer '{layer}', style '{style}'")

        response = self.session.get(self.base_url, params=params, timeout=self.timeout, stream=True)
        response.raise_for_status()

        return response.content

    def _format_params(self, params: Dict[str, Any]) -> str:
        """Format parameters as query string for logging"""
        return '&'.join(f"{k}={v}" for k, v in params.items())

    def close(self):
        """Close the HTTP session"""
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


# Convenience function
def create_client(base_url: str, timeout: int = 30) -> WMSClient:
    """
    Create a WMS client.

    Args:
        base_url: Base URL for WMS service
        timeout: Request timeout in seconds

    Returns:
        WMSClient instance
    """
    return WMSClient(base_url, timeout)
