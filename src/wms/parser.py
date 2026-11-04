#!/usr/bin/env python3
"""
WMS GetCapabilities Parser
Parses WMS 1.1.1 and 1.3.0 GetCapabilities XML documents into structured Python dataclasses.
"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional
import xml.etree.ElementTree as ET
from lxml import etree
import logging

logger = logging.getLogger(__name__)


@dataclass
class BoundingBox:
    """Spatial extent in a specific CRS"""
    crs: str              # e.g., "EPSG:3857", "CRS:84"
    minx: float
    miny: float
    maxx: float
    maxy: float


@dataclass
class Dimension:
    """Temporal or other dimension (TIME, ELEVATION, RUN, FORECAST, etc.)"""
    name: str                          # e.g., "TIME", "ELEVATION", "RUN", "FORECAST"
    units: str                         # e.g., "ISO8601", "meters", "hPa"
    default: Optional[str] = None      # Default value
    values: List[str] = field(default_factory=list)  # List of valid values
    nearest_value: bool = False        # WMS 1.3.0: nearestValue attribute
    multiple_values: bool = False      # WMS 1.3.0: multipleValues attribute


@dataclass
class Style:
    """Visual style for a layer"""
    name: str                          # e.g., "default", "temperature_rainbow"
    title: str                         # Human-readable name
    abstract: Optional[str] = None     # Description
    legend_url: Optional[str] = None   # URL to legend graphic


@dataclass
class Layer:
    """WMS Layer (can be container or queryable)"""
    name: Optional[str] = None         # Layer identifier (None for container layers)
    title: str = ""                    # Human-readable name
    abstract: Optional[str] = None     # Description
    queryable: bool = False            # Supports GetFeatureInfo

    # Spatial reference
    crs_list: List[str] = field(default_factory=list)  # ["EPSG:3857", "EPSG:4326", ...]
    bounding_boxes: Dict[str, BoundingBox] = field(default_factory=dict)  # {crs: bbox}
    geographic_bbox: Optional[BoundingBox] = None  # WGS84 extent (lat/lon)

    # Visualization
    styles: List[Style] = field(default_factory=list)

    # Dimensions
    dimensions: Dict[str, Dimension] = field(default_factory=dict)  # {name: dimension}

    # Metadata
    min_scale: Optional[float] = None
    max_scale: Optional[float] = None
    attribution: Optional[str] = None
    keywords: List[str] = field(default_factory=list)      # Keywords (may contain JSON)
    identifiers: List[str] = field(default_factory=list)   # WMS 1.3.0: Identifier elements

    # Hierarchy
    parent: Optional['Layer'] = None
    children: List['Layer'] = field(default_factory=list)

    def is_queryable(self) -> bool:
        """Check if this layer is queryable (has a name)"""
        return self.name is not None and self.name != ""


@dataclass
class WMSCapabilities:
    """Complete parsed GetCapabilities response"""
    version: str                       # "1.1.1" or "1.3.0"

    # Service info
    service_name: str = ""             # "WMS" or "OGC:WMS"
    service_title: str = ""
    service_abstract: Optional[str] = None
    service_url: str = ""

    # Supported operations
    get_map_formats: List[str] = field(default_factory=list)  # ["image/png", "image/jpeg"]
    get_feature_info_formats: List[str] = field(default_factory=list)
    exception_formats: List[str] = field(default_factory=list)

    # Layer hierarchy
    root_layer: Optional[Layer] = None

    def get_queryable_layers(self) -> List[Layer]:
        """Get all queryable layers as a flat list"""
        def collect_layers(layer: Layer) -> List[Layer]:
            result = []
            if layer.is_queryable():
                result.append(layer)
            for child in layer.children:
                result.extend(collect_layers(child))
            return result

        return collect_layers(self.root_layer) if self.root_layer else []


class WMSParser:
    """Parser for WMS GetCapabilities XML documents"""

    # Namespace mappings for different WMS versions
    # Note: In WMS XML, the default namespace is usually http://www.opengis.net/wms
    # Elements without prefix use this namespace
    NS = {
        'wms': 'http://www.opengis.net/wms',
        'xlink': 'http://www.w3.org/1999/xlink'
    }

    def __init__(self):
        self.ns = {}
        self.version = None

    def _find_elem(self, parent: etree.Element, *tags) -> Optional[etree.Element]:
        """
        Find an element, trying multiple tag options.
        Works around lxml's FutureWarning for truth-testing elements.
        """
        for tag in tags:
            elem = parent.find(tag, self.ns) if ':' in tag else parent.find(tag)
            if elem is not None:
                return elem
        return None

    def parse(self, xml_content: str) -> WMSCapabilities:
        """
        Parse a GetCapabilities XML document.

        Args:
            xml_content: Raw XML string

        Returns:
            WMSCapabilities object
        """
        try:
            root = etree.fromstring(xml_content.encode('utf-8'))
        except Exception as e:
            logger.error(f"Failed to parse XML: {e}")
            raise

        # Detect version
        self.version = root.get('version')
        if not self.version:
            raise ValueError("Could not determine WMS version from XML")

        # Use the standard namespace mapping
        self.ns = self.NS

        # Parse main sections
        caps = WMSCapabilities(version=self.version)

        self._parse_service(root, caps)
        self._parse_capability(root, caps)

        return caps

    def _parse_service(self, root: etree.Element, caps: WMSCapabilities):
        """Parse the Service section"""
        service = self._find_elem(root, 'wms:Service', 'Service')

        if service is not None:
            name_elem = self._find_elem(service, 'wms:Name', 'Name')
            title_elem = self._find_elem(service, 'wms:Title', 'Title')
            abstract_elem = self._find_elem(service, 'wms:Abstract', 'Abstract')
            online_elem = self._find_elem(service, 'wms:OnlineResource', 'OnlineResource')

            caps.service_name = name_elem.text if name_elem is not None else ""
            caps.service_title = title_elem.text if title_elem is not None else ""
            caps.service_abstract = abstract_elem.text if abstract_elem is not None else None

            if online_elem is not None:
                # Try different xlink attribute formats
                caps.service_url = (
                    online_elem.get('{http://www.w3.org/1999/xlink}href') or
                    online_elem.get('href') or
                    ""
                )

    def _parse_capability(self, root: etree.Element, caps: WMSCapabilities):
        """Parse the Capability section"""
        capability = self._find_elem(root, 'wms:Capability', 'Capability')

        if capability is None:
            logger.warning("No Capability section found")
            return

        # Parse Request section
        request = self._find_elem(capability, 'wms:Request', 'Request')
        if request is not None:
            # GetMap formats
            get_map = self._find_elem(request, 'wms:GetMap', 'GetMap')
            if get_map is not None:
                for fmt in get_map.findall('wms:Format', self.ns):
                    if fmt.text:
                        caps.get_map_formats.append(fmt.text)
                if not caps.get_map_formats:
                    for fmt in get_map.findall('Format'):
                        if fmt.text:
                            caps.get_map_formats.append(fmt.text)

            # GetFeatureInfo formats
            get_feature_info = self._find_elem(request, 'wms:GetFeatureInfo', 'GetFeatureInfo')
            if get_feature_info is not None:
                for fmt in get_feature_info.findall('wms:Format', self.ns):
                    if fmt.text:
                        caps.get_feature_info_formats.append(fmt.text)
                if not caps.get_feature_info_formats:
                    for fmt in get_feature_info.findall('Format'):
                        if fmt.text:
                            caps.get_feature_info_formats.append(fmt.text)

        # Parse Exception section
        exception = self._find_elem(capability, 'wms:Exception', 'Exception')
        if exception is not None:
            for fmt in exception.findall('wms:Format', self.ns):
                if fmt.text:
                    caps.exception_formats.append(fmt.text)
            if not caps.exception_formats:
                for fmt in exception.findall('Format'):
                    if fmt.text:
                        caps.exception_formats.append(fmt.text)

        # Parse root Layer
        root_layer_elem = self._find_elem(capability, 'wms:Layer', 'Layer')

        if root_layer_elem is not None:
            caps.root_layer = self._parse_layer(root_layer_elem, None)

    def _parse_layer(self, elem: etree.Element, parent: Optional[Layer]) -> Layer:
        """Parse a Layer element recursively"""
        layer = Layer()

        # Basic attributes
        layer.queryable = elem.get('queryable') == '1'

        # Name and Title
        name_elem = self._find_elem(elem, 'wms:Name', 'Name')
        title_elem = self._find_elem(elem, 'wms:Title', 'Title')
        abstract_elem = self._find_elem(elem, 'wms:Abstract', 'Abstract')

        layer.name = name_elem.text if name_elem is not None else None
        layer.title = title_elem.text if title_elem is not None else ""
        layer.abstract = abstract_elem.text if abstract_elem is not None else None

        # CRS/SRS list
        crs_tag = 'wms:CRS' if self.version.startswith('1.3') else 'wms:SRS'
        for crs_elem in elem.findall(crs_tag, self.ns):
            if crs_elem.text:
                layer.crs_list.append(crs_elem.text)
        # Try without namespace
        if not layer.crs_list:
            crs_tag_simple = 'CRS' if self.version.startswith('1.3') else 'SRS'
            for crs_elem in elem.findall(crs_tag_simple):
                if crs_elem.text:
                    layer.crs_list.append(crs_elem.text)

        # Bounding boxes
        for bbox_elem in elem.findall('wms:BoundingBox', self.ns):
            bbox = self._parse_bounding_box(bbox_elem)
            if bbox:
                layer.bounding_boxes[bbox.crs] = bbox
        if not layer.bounding_boxes:
            for bbox_elem in elem.findall('BoundingBox'):
                bbox = self._parse_bounding_box(bbox_elem)
                if bbox:
                    layer.bounding_boxes[bbox.crs] = bbox

        # Geographic bounding box
        layer.geographic_bbox = self._parse_geographic_bbox(elem)

        # Styles
        for style_elem in elem.findall('wms:Style', self.ns):
            style = self._parse_style(style_elem)
            if style:
                layer.styles.append(style)
        if not layer.styles:
            for style_elem in elem.findall('Style'):
                style = self._parse_style(style_elem)
                if style:
                    layer.styles.append(style)

        # Dimensions
        for dim_elem in elem.findall('wms:Dimension', self.ns):
            dimension = self._parse_dimension(dim_elem)
            if dimension:
                layer.dimensions[dimension.name] = dimension
        if not layer.dimensions:
            for dim_elem in elem.findall('Dimension'):
                dimension = self._parse_dimension(dim_elem)
                if dimension:
                    layer.dimensions[dimension.name] = dimension

        # Keywords
        keyword_list = self._find_elem(elem, 'wms:KeywordList', 'KeywordList')
        if keyword_list is not None:
            for kw_elem in keyword_list.findall('wms:Keyword', self.ns):
                if kw_elem.text:
                    layer.keywords.append(kw_elem.text)
            if not layer.keywords:
                for kw_elem in keyword_list.findall('Keyword'):
                    if kw_elem.text:
                        layer.keywords.append(kw_elem.text)

        # Identifiers (WMS 1.3.0)
        for id_elem in elem.findall('wms:Identifier', self.ns):
            if id_elem.text:
                layer.identifiers.append(id_elem.text)
        if not layer.identifiers:
            for id_elem in elem.findall('Identifier'):
                if id_elem.text:
                    layer.identifiers.append(id_elem.text)

        # Min/Max scale
        min_scale_elem = self._find_elem(elem, 'wms:MinScaleDenominator', 'MinScaleDenominator')
        max_scale_elem = self._find_elem(elem, 'wms:MaxScaleDenominator', 'MaxScaleDenominator')

        if min_scale_elem is not None and min_scale_elem.text:
            try:
                layer.min_scale = float(min_scale_elem.text)
            except ValueError:
                pass

        if max_scale_elem is not None and max_scale_elem.text:
            try:
                layer.max_scale = float(max_scale_elem.text)
            except ValueError:
                pass

        # Parent
        layer.parent = parent

        # Child layers (recursive)
        for child_elem in elem.findall('wms:Layer', self.ns):
            child_layer = self._parse_layer(child_elem, layer)
            layer.children.append(child_layer)
        if not layer.children:
            for child_elem in elem.findall('Layer'):
                child_layer = self._parse_layer(child_elem, layer)
                layer.children.append(child_layer)

        return layer

    def _parse_bounding_box(self, elem: etree.Element) -> Optional[BoundingBox]:
        """Parse a BoundingBox element"""
        try:
            crs = elem.get('CRS') or elem.get('SRS')
            if not crs:
                return None

            return BoundingBox(
                crs=crs,
                minx=float(elem.get('minx')),
                miny=float(elem.get('miny')),
                maxx=float(elem.get('maxx')),
                maxy=float(elem.get('maxy'))
            )
        except (ValueError, TypeError) as e:
            logger.warning(f"Failed to parse BoundingBox: {e}")
            return None

    def _parse_geographic_bbox(self, elem: etree.Element) -> Optional[BoundingBox]:
        """Parse EX_GeographicBoundingBox (1.3.0) or LatLonBoundingBox (1.1.1)"""
        # Try WMS 1.3.0 format
        geo_bbox = self._find_elem(elem, 'wms:EX_GeographicBoundingBox', 'EX_GeographicBoundingBox')
        if geo_bbox is not None:
            try:
                west = self._find_elem(geo_bbox, 'wms:westBoundLongitude', 'westBoundLongitude')
                east = self._find_elem(geo_bbox, 'wms:eastBoundLongitude', 'eastBoundLongitude')
                south = self._find_elem(geo_bbox, 'wms:southBoundLatitude', 'southBoundLatitude')
                north = self._find_elem(geo_bbox, 'wms:northBoundLatitude', 'northBoundLatitude')

                if all([west is not None, east is not None, south is not None, north is not None]):
                    return BoundingBox(
                        crs='EPSG:4326',
                        minx=float(west.text),
                        miny=float(south.text),
                        maxx=float(east.text),
                        maxy=float(north.text)
                    )
            except (ValueError, TypeError) as e:
                logger.warning(f"Failed to parse EX_GeographicBoundingBox: {e}")

        # Try WMS 1.1.1 format
        latlon_bbox = self._find_elem(elem, 'wms:LatLonBoundingBox', 'LatLonBoundingBox')
        if latlon_bbox is not None:
            try:
                return BoundingBox(
                    crs='EPSG:4326',
                    minx=float(latlon_bbox.get('minx')),
                    miny=float(latlon_bbox.get('miny')),
                    maxx=float(latlon_bbox.get('maxx')),
                    maxy=float(latlon_bbox.get('maxy'))
                )
            except (ValueError, TypeError) as e:
                logger.warning(f"Failed to parse LatLonBoundingBox: {e}")

        return None

    def _parse_style(self, elem: etree.Element) -> Optional[Style]:
        """Parse a Style element"""
        name_elem = self._find_elem(elem, 'wms:Name', 'Name')
        title_elem = self._find_elem(elem, 'wms:Title', 'Title')
        abstract_elem = self._find_elem(elem, 'wms:Abstract', 'Abstract')

        if name_elem is None or not name_elem.text:
            return None

        style = Style(
            name=name_elem.text,
            title=title_elem.text if title_elem is not None else name_elem.text,
            abstract=abstract_elem.text if abstract_elem is not None else None
        )

        # Legend URL
        legend_elem = self._find_elem(elem, 'wms:LegendURL', 'LegendURL')
        if legend_elem is not None:
            online_elem = self._find_elem(legend_elem, 'wms:OnlineResource', 'OnlineResource')
            if online_elem is not None:
                style.legend_url = (
                    online_elem.get('{http://www.w3.org/1999/xlink}href') or
                    online_elem.get('href')
                )

        return style

    def _parse_dimension(self, elem: etree.Element) -> Optional[Dimension]:
        """Parse a Dimension element"""
        name = elem.get('name')
        if not name:
            return None

        units = elem.get('units', '')
        default = elem.get('default')

        # Parse comma-separated values
        values = []
        if elem.text and elem.text.strip():
            values = [v.strip() for v in elem.text.split(',') if v.strip()]

        # WMS 1.3.0 attributes
        nearest_value = elem.get('nearestValue') == '1'
        multiple_values = elem.get('multipleValues') == '1'

        return Dimension(
            name=name,
            units=units,
            default=default,
            values=values,
            nearest_value=nearest_value,
            multiple_values=multiple_values
        )


# Convenience function
def parse_capabilities(xml_content: str) -> WMSCapabilities:
    """
    Parse a WMS GetCapabilities XML document.

    Args:
        xml_content: Raw XML string or bytes

    Returns:
        WMSCapabilities object
    """
    parser = WMSParser()
    if isinstance(xml_content, bytes):
        xml_content = xml_content.decode('utf-8')
    return parser.parse(xml_content)
