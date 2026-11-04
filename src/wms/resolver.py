#!/usr/bin/env python3
"""
Smart query resolver with two-phase resolution.

Phase 1: Extract dimensions and units → fuzzy match layer
Phase 2: Validate dimensions against layer capabilities
"""

import re
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple

from .parser import WMSCapabilities, Layer
from .fuzzy import FuzzySearchEngine, SearchResult
from .dimensions import DimensionParser


@dataclass
class ResolvedQuery:
    """Result of smart query resolution."""
    layer: Layer
    style: str                        # Validated style name
    dimensions: Dict[str, str]        # Validated dimension values
    crs: str                          # Coordinate reference system
    bbox: Tuple[float, float, float, float]  # (minx, miny, maxx, maxy)
    confidence: float                 # Match confidence (0-100)
    warnings: List[str]               # Warnings about auto-corrections
    alternatives: List[str]           # Alternative layer names if confidence was low

    def get_dimension_param_name(self, dim_name: str) -> str:
        """Get the WMS parameter name for a dimension"""
        # TIME and ELEVATION don't get DIM_ prefix
        if dim_name in ('TIME', 'ELEVATION'):
            return dim_name
        return f'DIM_{dim_name}'


class AmbiguousQueryError(Exception):
    """Raised when query doesn't match with sufficient confidence."""
    def __init__(self, message: str, alternatives: List[str]):
        super().__init__(message)
        self.alternatives = alternatives


class SmartQueryResolver:
    """
    Two-phase query resolver with capabilities validation.

    Extracts dimension tokens before fuzzy matching, then validates
    all dimension values against the selected layer's capabilities.
    """

    # Dimension patterns that should be extracted BEFORE fuzzy search
    DIMENSION_PATTERNS = {
        'RUN': [
            (r'\b(\d{1,2}z)\b', 'cycle'),           # 6z, 12z
            (r'\b(latest)\b', 'keyword'),           # latest
        ],
        'FORECAST': [
            (r'\+?(\d+h)\b', 'hours'),              # 6h, +12h
            (r'\+?(PT\d+H)\b', 'iso_hours'),        # PT6H
            (r'\+?(\d+d)\b', 'days'),               # 2d
            (r'\+?(P\d+D)\b', 'iso_days'),          # P2D
        ],
        'ELEVATION': [
            (r'\b(\d{3,5})(?:mb|hpa)?\b', 'pressure'),  # 850, 850mb, 10000
        ],
    }

    # Unit preference patterns
    UNIT_PATTERNS = {
        'fahrenheit': r'\b(f|fahrenheit)\b',
        'celsius': r'\b(c|celsius)\b',
        'kelvin': r'\b(k|kelvin)\b',
    }

    def __init__(self, capabilities: WMSCapabilities, confidence_threshold: float = 80.0):
        """
        Initialize smart resolver.

        Args:
            capabilities: Parsed WMS capabilities
            confidence_threshold: Minimum confidence score for layer match (default: 80%)
        """
        self.capabilities = capabilities
        self.search_engine = FuzzySearchEngine(capabilities)
        self.dim_parser = DimensionParser()
        self.confidence_threshold = confidence_threshold

    def list_layers(self, query: str = None, limit: int = 20) -> List:
        """
        List layers, optionally filtered by query.

        Args:
            query: Optional search query (fuzzy matched)
            limit: Maximum results to return

        Returns:
            List of SearchResult objects with layer and score
        """
        from .fuzzy import SearchResult

        if query:
            # Extract dimension tokens to clean the query
            _, _, clean_query = self._extract_tokens(query)
            return self.search_engine.search(clean_query, limit=limit)
        else:
            # Return all layers wrapped in SearchResult with score 0
            layers = self.search_engine.get_all_layers()[:limit]
            return [SearchResult(layer=l, score=0.0, matched_on='all') for l in layers]

    def resolve(self, query: str) -> ResolvedQuery:
        """
        Resolve user query to valid WMS request parameters.

        Args:
            query: User input (e.g., "gfs temp f 12z 6h 850")

        Returns:
            ResolvedQuery with validated layer and dimensions

        Raises:
            AmbiguousQueryError: If no high-confidence layer match found
        """
        # Phase 1: Extract tokens
        extracted_dims, extracted_units, clean_query = self._extract_tokens(query)

        # Phase 2: Fuzzy match clean query to layers
        results = self.search_engine.search(clean_query, limit=10)

        if not results:
            raise AmbiguousQueryError(
                f"No layers found matching '{clean_query}'",
                alternatives=[]
            )

        # Phase 3: Apply unit preferences
        results = self._apply_unit_preference(results, extracted_units)

        # Phase 4: Validate confidence
        best = results[0]
        if best.score < self.confidence_threshold:
            raise AmbiguousQueryError(
                f"No high-confidence match for '{query}' (best: {best.score:.0f}%)",
                alternatives=[r.layer.name for r in results[:5]]
            )

        # Phase 5: Validate and resolve dimensions for this layer
        dimensions, warnings = self._resolve_dimensions(extracted_dims, best.layer)

        # Phase 6: Resolve style, CRS, and bbox
        style = self._resolve_style(best.layer)
        crs = self._get_preferred_crs(best.layer)
        bbox = self._get_bbox(best.layer, crs)

        return ResolvedQuery(
            layer=best.layer,
            style=style,
            dimensions=dimensions,
            crs=crs,
            bbox=bbox,
            confidence=best.score,
            warnings=warnings,
            alternatives=[r.layer.name for r in results[1:6]]
        )

    def _extract_tokens(self, query: str) -> Tuple[Dict[str, List[str]], List[str], str]:
        """
        Extract dimension tokens and unit preferences from query.

        Returns:
            Tuple of (extracted_dims, extracted_units, clean_query)

        Examples:
            "gfs temp f 12z 6h 850" ->
                ({RUN: ['12z'], FORECAST: ['6h'], ELEVATION: ['850']},
                 ['f'],
                 "gfs temp")
        """
        extracted_dims = {}
        extracted_units = []
        clean_query = query.lower()

        # Extract dimension patterns
        for dim_name, patterns in self.DIMENSION_PATTERNS.items():
            for pattern, pattern_type in patterns:
                matches = re.findall(pattern, clean_query, re.IGNORECASE)
                if matches:
                    if dim_name not in extracted_dims:
                        extracted_dims[dim_name] = []
                    extracted_dims[dim_name].extend(matches)
                    # Remove from clean query
                    clean_query = re.sub(pattern, ' ', clean_query, flags=re.IGNORECASE)

        # Extract unit preferences
        for unit_type, pattern in self.UNIT_PATTERNS.items():
            matches = re.findall(pattern, clean_query, re.IGNORECASE)
            if matches:
                extracted_units.append(unit_type)
                # Remove from clean query
                clean_query = re.sub(pattern, ' ', clean_query, flags=re.IGNORECASE)

        # Clean up query (remove extra spaces)
        clean_query = ' '.join(clean_query.split())

        return extracted_dims, extracted_units, clean_query

    def _apply_unit_preference(self, results: List[SearchResult], units: List[str]) -> List[SearchResult]:
        """
        Apply unit preferences to boost matching layers.

        Args:
            results: Fuzzy search results
            units: Extracted unit preferences (e.g., ['fahrenheit'])

        Returns:
            Results with adjusted scores
        """
        if not units:
            return results

        # Unit suffixes to look for
        unit_suffixes = {
            'fahrenheit': ['_F', '_Fahrenheit', 'Fahrenheit'],
            'celsius': ['_C', '_Celsius', 'Celsius'],
            'kelvin': ['_K', '_Kelvin', 'Kelvin'],
        }

        # Separate layers into matching and non-matching
        matching = []
        non_matching = []

        for result in results:
            layer_name = result.layer.name
            matches_unit = False

            for unit in units:
                if unit in unit_suffixes:
                    for suffix in unit_suffixes[unit]:
                        if suffix in layer_name:
                            matches_unit = True
                            break
                    if matches_unit:
                        break

            if matches_unit:
                matching.append(result)
            else:
                non_matching.append(result)

        # Return matching layers first, then non-matching
        # Within each group, maintain original score order
        return matching + non_matching

    def _resolve_dimensions(self, extracted: Dict[str, List[str]], layer: Layer) -> Tuple[Dict[str, str], List[str]]:
        """
        Resolve extracted dimension expressions to valid values for this layer.

        Args:
            extracted: Extracted dimension expressions from query
            layer: Target layer from capabilities

        Returns:
            Tuple of (resolved_dimensions, warnings)
        """
        resolved = {}
        warnings = []

        # Process each extracted dimension
        for dim_name, expressions in extracted.items():
            # Skip if layer doesn't have this dimension
            if dim_name not in layer.dimensions:
                warnings.append(f"Layer '{layer.name}' doesn't have {dim_name} dimension, skipping")
                continue

            # Use first expression (in case multiple were found)
            expr = expressions[0] if expressions else None
            if not expr:
                continue

            # Parse and validate
            parsed = None
            if dim_name == 'RUN':
                parsed = self.dim_parser.parse_run_time(expr, layer)
                if parsed != expr and parsed:
                    # Auto-corrected
                    warnings.append(f"Using {parsed} for RUN (requested: {expr})")
            elif dim_name == 'FORECAST':
                parsed = self.dim_parser.parse_forecast(expr, layer)
                # Convert shortcut to ISO for comparison
                original_iso = self.dim_parser.FORECAST_SHORTCUTS.get(expr.lower().lstrip('+'))
                if not original_iso:
                    # Try parsing without layer to get ISO form
                    original_iso = self.dim_parser.parse_forecast(expr, layer=None)
                if parsed and original_iso and parsed != original_iso:
                    warnings.append(f"Using {parsed} for FORECAST (requested: {original_iso})")
            elif dim_name == 'ELEVATION':
                parsed = self.dim_parser.parse_elevation(expr, layer)
                if parsed != expr.replace('mb', '').replace('hpa', '').strip() and parsed:
                    warnings.append(f"Using {parsed} for ELEVATION (requested: {expr})")

            if parsed:
                resolved[dim_name] = parsed

        # Fill in defaults for unspecified dimensions
        for dim_name, dim in layer.dimensions.items():
            if dim_name not in resolved:
                # Use smart defaults
                if dim_name == 'RUN':
                    # Latest run
                    latest = self.dim_parser.get_latest_run(layer)
                    if latest:
                        resolved[dim_name] = latest
                elif dim_name == 'FORECAST':
                    # PT0S (analysis) or default
                    if hasattr(dim, 'default') and dim.default:
                        resolved[dim_name] = dim.default
                    else:
                        resolved[dim_name] = 'PT0S'
                elif dim_name == 'TIME':
                    # Latest time or default
                    if hasattr(dim, 'default') and dim.default:
                        resolved[dim_name] = dim.default
                    else:
                        latest = self.dim_parser.get_latest_time(layer)
                        if latest:
                            resolved[dim_name] = latest
                else:
                    # Use dimension default
                    if hasattr(dim, 'default') and dim.default:
                        resolved[dim_name] = dim.default

        return resolved, warnings

    def get_dimension_hints(self, layer: Layer) -> Dict[str, List[str]]:
        """
        Get available dimension values for a layer.

        Useful for showing hints to the user.

        Args:
            layer: Target layer

        Returns:
            Dict of {dimension_name: [available_shortcuts]}
        """
        hints = {}

        # Get forecast shortcuts
        if 'FORECAST' in layer.dimensions:
            shortcuts = self.dim_parser.get_available_shortcuts(layer, 'FORECAST')
            if shortcuts:
                hints['FORECAST'] = list(shortcuts.keys())

        # Get run time shortcuts
        if 'RUN' in layer.dimensions:
            shortcuts = self.dim_parser.get_available_shortcuts(layer, 'RUN')
            if shortcuts:
                hints['RUN'] = list(shortcuts.keys())

        # Get elevation values (just show first few)
        if 'ELEVATION' in layer.dimensions:
            values = self.dim_parser._get_dimension_values(layer, 'ELEVATION')
            if values:
                hints['ELEVATION'] = values[:10]  # First 10

        return hints

    def _resolve_style(self, layer: Layer) -> str:
        """
        Resolve style name for layer.

        Args:
            layer: Target layer

        Returns:
            Valid style name (defaults to 'default')
        """
        if not layer.styles:
            return 'default'

        # Get available style names
        style_names = [s.name for s in layer.styles]

        # Use default or first style
        if 'default' in style_names:
            return 'default'
        return style_names[0]

    def _get_preferred_crs(self, layer: Layer) -> str:
        """Get preferred CRS for layer"""
        # Preference order
        preferred = ['CRS:84', 'EPSG:4326', 'EPSG:3857', 'EPSG:900913']

        for crs in preferred:
            if crs in layer.crs_list:
                return crs

        # Fallback to first available
        if layer.crs_list:
            return layer.crs_list[0]

        return 'CRS:84'

    def _get_bbox(self, layer: Layer, crs: str) -> Tuple[float, float, float, float]:
        """Get bounding box for layer and CRS"""
        # Try to get bbox for specific CRS
        if crs in layer.bounding_boxes:
            bb = layer.bounding_boxes[crs]
            return (bb.minx, bb.miny, bb.maxx, bb.maxy)

        # Use geographic bbox
        if layer.geographic_bbox:
            bb = layer.geographic_bbox
            return (bb.minx, bb.miny, bb.maxx, bb.maxy)

        # Default global extent
        return (-180, -90, 180, 90)
