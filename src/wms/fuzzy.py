#!/usr/bin/env python3
"""
Fuzzy search engine for WMS layers.
Builds an index from capabilities and provides fast fuzzy matching.
"""

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from rapidfuzz import fuzz, process

from .parser import WMSCapabilities, Layer


@dataclass
class LayerIndex:
    """Indexed layer data for searching"""
    layer: Layer
    name: str                    # Original layer name
    tokens: List[str]            # Tokenized for searching
    search_text: str             # Combined searchable text
    model: Optional[str] = None  # Extracted model name (GFS, GALWEM, etc.)


@dataclass
class SearchResult:
    """Result from fuzzy search"""
    layer: Layer
    score: float                 # Match score (0-100)
    matched_on: str              # What matched (name, title, tokens)


class FuzzySearchEngine:
    """
    Fuzzy search engine for WMS layers.

    Builds a search index from capabilities and provides
    fast fuzzy matching against layer names, titles, and tokens.
    """

    # Common weather model prefixes
    MODEL_PATTERNS = [
        'GFS', 'GALWEM', 'HRRR', 'NAM', 'RAP', 'GEFS', 'NAEFS',
        'ECMWF', 'UKMET', 'NAVGEM', 'COAMPS', 'WRF', 'RTMA',
        'URMA', 'NDFD', 'NBM', 'HREF', 'SREF', 'GEPS', 'CMC',
        'FNMOC', 'NOGAPS', 'ENS', 'MESO', 'UPPER_AIR'
    ]

    def __init__(self, capabilities: WMSCapabilities):
        """
        Initialize search engine from capabilities.

        Args:
            capabilities: Parsed WMSCapabilities object
        """
        self.capabilities = capabilities
        self.layers = capabilities.get_queryable_layers()
        self.index = self._build_index()

    def _build_index(self) -> List[LayerIndex]:
        """Build searchable index from layers"""
        index = []

        for layer in self.layers:
            if not layer.name:
                continue

            # Tokenize the layer name
            tokens = self._tokenize(layer.name)

            # Also tokenize title if different
            if layer.title and layer.title != layer.name:
                tokens.extend(self._tokenize(layer.title))

            # Extract model name
            model = self._extract_model(layer.name)

            # Build combined search text
            search_text = ' '.join(tokens).lower()

            index.append(LayerIndex(
                layer=layer,
                name=layer.name,
                tokens=tokens,
                search_text=search_text,
                model=model
            ))

        return index

    def _tokenize(self, text: str) -> List[str]:
        """
        Tokenize text for searching.

        Splits on underscores, camelCase boundaries, and numbers.

        Examples:
            "GALWEM_CloudBase" → ["galwem", "cloud", "base"]
            "GFS_Temperature_2m" → ["gfs", "temperature", "2m"]
            "HRRR_Geopotential_Height" → ["hrrr", "geopotential", "height"]
            "GFS_Temperature_in_F" → ["gfs", "temperature", "in", "f"]
        """
        # Replace underscores with spaces
        text = text.replace('_', ' ')

        # Split camelCase
        text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)

        # Split numbers from letters
        text = re.sub(r'([a-zA-Z])(\d)', r'\1 \2', text)
        text = re.sub(r'(\d)([a-zA-Z])', r'\1 \2', text)

        # Lowercase and split
        tokens = text.lower().split()

        # Remove empty tokens
        return [t.strip() for t in tokens if t.strip()]

    def _tokenize_query(self, text: str) -> List[str]:
        """
        Tokenize user query more aggressively.

        Handles cases like "tempf" → ["temp", "f"] and "tempc" → ["temp", "c"]

        IMPORTANT: This method preserves dimension tokens (12z, 6h, 850mb, etc.)
        by extracting them BEFORE tokenization. This prevents pollution of fuzzy matching.
        """
        # Extract dimension patterns BEFORE tokenization
        dimension_tokens = []
        clean_text = text

        # Patterns that should NOT be tokenized
        # RUN patterns: 6z, 12z, 00z, 18z, latest
        run_pattern = r'\b(\d{1,2}z|latest)\b'
        for match in re.finditer(run_pattern, clean_text, re.IGNORECASE):
            dimension_tokens.append(match.group(1).lower())
            clean_text = clean_text[:match.start()] + ' ' + clean_text[match.end():]

        # FORECAST patterns: 6h, +12h, PT6H, 2d, P2D
        forecast_pattern = r'\b(\+?\d+[hd]|PT\d+H|P\d+D)\b'
        for match in re.finditer(forecast_pattern, clean_text, re.IGNORECASE):
            dimension_tokens.append(match.group(1).lower())
            clean_text = clean_text[:match.start()] + ' ' + clean_text[match.end():]

        # ELEVATION patterns: 850, 500mb, 10000
        elevation_pattern = r'\b(\d{3,5}(?:mb|hpa)?)\b'
        for match in re.finditer(elevation_pattern, clean_text, re.IGNORECASE):
            dimension_tokens.append(match.group(1).lower())
            clean_text = clean_text[:match.start()] + ' ' + clean_text[match.end():]

        # Now do standard tokenization on the clean text
        tokens = self._tokenize(clean_text)

        # Additional splitting for common patterns (unit indicators)
        expanded = []
        for token in tokens:
            # Split trailing single letters that might be unit indicators
            # e.g., "tempf" → "temp" + "f", "tempc" → "temp" + "c"
            match = re.match(r'^(.+?)([cfkm])$', token)
            if match and len(match.group(1)) >= 3:
                expanded.append(match.group(1))
                expanded.append(match.group(2))
            else:
                expanded.append(token)

        # NOTE: We return only the clean tokens here, not dimension tokens
        # Dimension tokens are extracted separately by SmartQueryResolver
        return expanded

    def _extract_model(self, name: str) -> Optional[str]:
        """Extract weather model name from layer name"""
        name_upper = name.upper()
        for model in self.MODEL_PATTERNS:
            if model in name_upper:
                return model
        return None

    def search(self, query: str, limit: int = 10) -> List[SearchResult]:
        """
        Search for layers matching query.

        Args:
            query: User search query (e.g., "gfs temp")
            limit: Maximum results to return

        Returns:
            List of SearchResult sorted by score (best first)
        """
        if not query.strip():
            return []

        # Tokenize query with aggressive splitting for user input
        query_tokens = self._tokenize_query(query)
        query_lower = ' '.join(query_tokens)

        results = []

        for item in self.index:
            score = self._calculate_score(query_lower, query_tokens, item)
            if score > 0:
                results.append(SearchResult(
                    layer=item.layer,
                    score=score,
                    matched_on=item.name
                ))

        # Sort by score descending
        results.sort(key=lambda r: r.score, reverse=True)

        return results[:limit]

    def _calculate_score(self, query: str, query_tokens: List[str],
                         item: LayerIndex) -> float:
        """
        Calculate match score between query and indexed item.

        Uses multiple scoring strategies:
        1. Token overlap (prioritize matching ALL tokens)
        2. Fuzzy ratio
        3. Bonuses for exact matches
        """
        score = 0.0

        # Strategy 1: Token overlap scoring - prioritize matching ALL tokens
        if query_tokens:
            matches = 0
            matched_tokens = []
            for qt in query_tokens:
                best_match = 0
                for it in item.tokens:
                    if qt == it:
                        # Exact token match
                        best_match = 1.0
                        break
                    elif it.startswith(qt) and len(qt) >= 2:
                        # Query token is prefix of item token (e.g., "temp" matches "temperature")
                        # Require at least 2 chars to avoid "f" matching "forecast"
                        best_match = max(best_match, 0.9)
                    elif qt.startswith(it) and len(it) >= 2:
                        # Item token is prefix of query token
                        best_match = max(best_match, 0.8)
                    elif len(qt) >= 3 and len(it) >= 3:
                        # Only do fuzzy matching for longer tokens
                        ratio = fuzz.ratio(qt, it)
                        if ratio > 80:
                            best_match = max(best_match, ratio / 100 * 0.7)

                matches += best_match
                if best_match > 0:
                    matched_tokens.append(qt)

            # Score based on proportion of tokens matched
            token_score = (matches / len(query_tokens)) * 100

            # Bonus if ALL tokens matched
            if len(matched_tokens) == len(query_tokens):
                token_score = min(100, token_score + 5)

            score = max(score, token_score)

        # Strategy 2: Direct fuzzy match on full search text
        ratio = fuzz.partial_ratio(query, item.search_text)
        # Weight this less than token matching
        score = max(score, ratio * 0.9)

        # Strategy 3: Exact name match bonus
        if query.replace(' ', '_').upper() in item.name.upper():
            score = min(100, score + 10)

        # Bonus for model match at start
        if query_tokens and item.model:
            if item.model.lower().startswith(query_tokens[0]):
                score = min(100, score + 5)

        return score

    def search_by_model(self, model: str) -> List[Layer]:
        """
        Get all layers for a specific model.

        Args:
            model: Model name (e.g., "GFS", "GALWEM")

        Returns:
            List of matching layers
        """
        model_upper = model.upper()
        return [
            item.layer for item in self.index
            if item.model and item.model.upper() == model_upper
        ]

    def get_models(self) -> List[str]:
        """Get list of all unique model names"""
        models = set()
        for item in self.index:
            if item.model:
                models.add(item.model)
        return sorted(models)

    def get_all_layers(self) -> List[Layer]:
        """Get all queryable layers"""
        return self.layers
