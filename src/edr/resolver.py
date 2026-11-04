#!/usr/bin/env python3
"""
EDR smart query resolver.

Two-phase resolution like the WMS SmartQueryResolver:
Phase 1: Extract datetime/z tokens → fuzzy match collection + parameter
Phase 2: Validate datetime/z against the collection's extent

Natural syntax:
    datetime: 'latest', '+6h'/'6h' (offset from end of temporal extent),
              ISO duration (PT6H, P2D), ISO instant, or 'a/b' interval
    z:        '850', '850mb', '500hpa' (snapped to nearest advertised level),
              or 'low/high' interval
    coords:   'lon,lat' → POINT(lon lat); 'lon1,lat1;lon2,lat2' → LINESTRING;
              raw WKT passed through
"""

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from rapidfuzz import fuzz

from .parser import EDRCapabilities, EDRCollection


class AmbiguousCollectionError(Exception):
    """Raised when query doesn't match a collection with sufficient confidence."""
    def __init__(self, message: str, alternatives: List[str]):
        super().__init__(message)
        self.alternatives = alternatives


@dataclass
class ResolvedEDR:
    """Result of EDR query resolution"""
    collection: EDRCollection
    parameter: Optional[str]         # parameter-name filter (None = all)
    confidence: float                # Match confidence (0-100)
    warnings: List[str] = field(default_factory=list)
    alternatives: List[str] = field(default_factory=list)
    extracted: dict = field(default_factory=dict)   # raw datetime/z tokens from query


def _tokenize(text: str) -> List[str]:
    """Tokenize for searching: split on -_/, camelCase, and letters/digits"""
    text = text.replace('_', ' ').replace('-', ' ').replace('/', ' ')
    text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)
    text = re.sub(r'([a-zA-Z])(\d)', r'\1 \2', text)
    text = re.sub(r'(\d)([a-zA-Z])', r'\1 \2', text)
    return [t.lower() for t in text.split() if t.strip()]


# Tokens that are dimension syntax, not searchable text
_DATETIME_RE = re.compile(r'\b(latest|\+?\d+[hd]|PT\d+[HMD]|P\d+[DWM])(?::?\d+Z?)?\b', re.I)
_Z_RE = re.compile(r'\b(\d{2,6}(?:mb|hpa|m)?(?:/\d{2,6}(?:mb|hpa|m)?)?)\b', re.I)


class EDRResolver:
    """Fuzzy resolver for EDR collections and parameters."""

    def __init__(self, capabilities: EDRCapabilities, confidence_threshold: float = 50.0):
        self.capabilities = capabilities
        self.confidence_threshold = confidence_threshold
        # {collection_id: tokens}
        self._index = {
            c.id: _tokenize(f"{c.id} {c.title}")
            for c in capabilities.collections
        }
        # {collection_id: parameter tokens} - query words like 'temp' should
        # rank collections that actually offer that parameter
        self._param_index = {
            c.id: _tokenize(' '.join(
                f"{pname} {p.label} {p.observed_property}"
                for pname, p in c.parameter_names.items()
            ))
            for c in capabilities.collections
        }

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _score(self, query_tokens: List[str], query: str, tokens: List[str],
               param_tokens: Optional[List[str]] = None) -> float:
        """Score query tokens against an item's tokens (token overlap + partial ratio).

        A token that matches one of the collection's parameter names
        contributes at 85% weight, so 'nbm conus temp' ranks collections
        that actually offer a temperature parameter higher.
        """
        if not query_tokens:
            return 0.0

        matches = 0
        for qt in query_tokens:
            best = _token_match(qt, tokens)
            if param_tokens:
                best = max(best, _token_match(qt, param_tokens) * 0.85)
            matches += best

        token_score = (matches / len(query_tokens)) * 100
        if matches == len(query_tokens):
            token_score = min(100, token_score + 5)

        # Partial ratio is only a tiebreaker - scaled down so short ids
        # (e.g. 'guam' vs 'dart') don't score high without token support
        partial = fuzz.partial_ratio(' '.join(query_tokens), ' '.join(tokens)) * 0.45
        return max(token_score, partial)

    def list_collections(self, query: Optional[str] = None, limit: int = 20) -> List[tuple]:
        """
        List collections, optionally fuzzy-filtered.

        Returns:
            List of (EDRCollection, score) tuples, best first.
            Score is 0.0 when no query is given.
        """
        if not query or not query.strip():
            return [(c, 0.0) for c in self.capabilities.collections[:limit]]

        dims, clean = self._extract_tokens(query)
        query_tokens = _tokenize(clean)
        scored = []
        for c in self.capabilities.collections:
            score = self._score(query_tokens, clean, self._index[c.id],
                                self._param_index.get(c.id))
            if score > 0:
                scored.append((c, score))
        scored.sort(key=lambda t: t[1], reverse=True)
        return scored[:limit]

    def resolve(self, query: str) -> ResolvedEDR:
        """
        Resolve a user query to a collection (+ optional parameter filter).

        Raises:
            AmbiguousCollectionError: if no confident collection match
        """
        dims, clean = self._extract_tokens(query)
        query_tokens = _tokenize(clean)

        results = self.list_collections(clean, limit=10) if query_tokens else []
        if not results:
            raise AmbiguousCollectionError(
                f"No collections found matching '{clean or query}'",
                alternatives=[c.id for c, _ in self.list_collections(limit=5)],
            )

        collection, score = results[0]
        if score < self.confidence_threshold:
            raise AmbiguousCollectionError(
                f"No high-confidence match for '{query}' (best: {collection.id} at {score:.0f}%)",
                alternatives=[c.id for c, _ in results[:5]],
            )

        # Try to map leftover tokens to a parameter of this collection
        parameter, param_warning = self._match_parameter(collection, query_tokens)

        resolved = ResolvedEDR(
            collection=collection,
            parameter=parameter,
            confidence=score,
            alternatives=[c.id for c, _ in results[1:6]],
            extracted=dims,
        )
        if param_warning:
            resolved.warnings.append(param_warning)
        return resolved

    def _match_parameter(self, collection: EDRCollection,
                         query_tokens: List[str]) -> Tuple[Optional[str], Optional[str]]:
        """Match leftover query tokens against the collection's parameter names."""
        if not collection.parameter_names or not query_tokens:
            return None, None

        # Only consider tokens that did NOT match the collection itself
        collection_tokens = self._index[collection.id]
        leftovers = [qt for qt in query_tokens if _token_match(qt, collection_tokens) == 0]
        if not leftovers:
            return None, None

        best_param = None
        best_score = 0.0
        for pname, param in collection.parameter_names.items():
            p_tokens = _tokenize(f"{pname} {param.label} {param.observed_property}")
            # Max-aggregate: param names are often short abbreviations
            # (TMAX), so any single strong word match ('max') counts
            per_token = [_token_match(qt, p_tokens) for qt in leftovers]
            agg = max(per_token) if per_token else 0.0
            score = max(
                agg * 100,
                fuzz.partial_ratio(' '.join(leftovers), ' '.join(p_tokens)) * 0.45,
            )
            if score > best_score:
                best_score = score
                best_param = pname

        # Require a solid match - otherwise return all parameters.
        # 55 (not 70): common param names are contractions (temp vs TMP
        # fuzzy-matches at ~60), so exact word matches are rare.
        if best_param and best_score >= 55:
            return best_param, None
        return None, None

    def _extract_tokens(self, query: str) -> Tuple[dict, str]:
        """Extract datetime/z tokens from the query, returning (dims, clean_query)."""
        dims = {}
        clean = query

        # Datetime tokens: latest, 6h, PT6H, P2D
        m = _DATETIME_RE.search(clean)
        if m:
            dims['datetime'] = m.group(1)
            clean = clean[:m.start()] + ' ' + clean[m.end():]

        # Z tokens: 850, 850mb, 500/850
        m = _Z_RE.search(clean)
        if m:
            dims['z'] = m.group(1)
            clean = clean[:m.start()] + ' ' + clean[m.end():]

        return dims, ' '.join(clean.split())

    # ------------------------------------------------------------------
    # Natural dimension syntax
    # ------------------------------------------------------------------

    def resolve_datetime(self, expr: str, collection: EDRCollection) -> Tuple[Optional[str], List[str]]:
        """
        Resolve a datetime expression against the collection's temporal extent.

        Returns:
            (datetime param value or None, warnings)
        """
        warnings = []
        expr = expr.strip()
        if not expr or expr.lower() == 'none':
            return None, warnings

        if expr.lower() == 'latest':
            end = collection.temporal_end
            if end:
                return end, warnings
            return None, ["Collection has no temporal extent; ignoring datetime"]

        # Offset from the start of the extent (forecast-hour semantics,
        # matching WMS RUN + FORECAST): +6h / 6h / PT6H / P2D / 2d
        offset = self._parse_duration(expr)
        if offset is not None:
            base = collection.temporal_start
            if not base:
                return None, ["Collection has no temporal extent; ignoring datetime offset"]
            try:
                target = _parse_iso(base) + offset
            except ValueError:
                return None, [f"Cannot parse temporal extent value '{base}'"]
            # Clamp to the extent end with a warning
            end = collection.temporal_end
            if end:
                try:
                    end_dt = _parse_iso(end)
                    if target > end_dt:
                        target = end_dt
                        warnings.append(f"Clamped datetime to extent end {end}")
                except ValueError:
                    pass
            value = _format_iso(target)
            warnings.append(f"datetime: {value} ({expr} from {base})")
            return value, warnings

        # Passthrough: ISO instant or a/b interval
        return expr, warnings

    def resolve_z(self, expr: str, collection: EDRCollection) -> Tuple[Optional[str], List[str]]:
        """
        Resolve a vertical level expression against the collection's vertical extent.

        Returns:
            (z param value or None, warnings)
        """
        warnings = []
        expr = expr.strip()
        if not expr or expr.lower() == 'none':
            return None, warnings

        # Interval passthrough (e.g. 500/850)
        if '/' in expr:
            low, high = expr.split('/', 1)
            low = self._strip_unit(low)
            high = self._strip_unit(high)
            return f"{low}/{high}", warnings

        value = self._strip_unit(expr)
        try:
            num = float(value)
        except ValueError:
            return expr, [f"z value '{expr}' not numeric; passing through as-is"]

        if collection.vertical_values:
            try:
                levels = [float(v) for v in collection.vertical_values]
                nearest = collection.vertical_values[min(
                    range(len(levels)), key=lambda i: abs(levels[i] - num)
                )]
                if float(nearest) != num:
                    warnings.append(f"z: snapped {expr} → {nearest}")
                return str(nearest), warnings
            except ValueError:
                pass

        return str(int(num)) if num == int(num) else str(num), warnings

    @staticmethod
    def _strip_unit(expr: str) -> str:
        return re.sub(r'(mb|hpa|m)$', '', expr.strip().lower())

    @staticmethod
    def _parse_duration(expr: str) -> Optional[timedelta]:
        """Parse a natural duration expression to timedelta (None if not a duration)"""
        expr = expr.strip().lstrip('+').lower()
        m = re.match(r'^pt?(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s)?$', expr)
        if m and any(m.groups()):
            h, mi, s = (float(g) if g else 0 for g in m.groups())
            return timedelta(hours=h, minutes=mi, seconds=s)
        m = re.match(r'^p(?:(\d+(?:\.\d+)?)d)?(?:(\d+(?:\.\d+)?)w)?$', expr)
        if m and any(m.groups()):
            d, w = (float(g) if g else 0 for g in m.groups())
            return timedelta(days=d + 7 * w)
        m = re.match(r'^(\d+(?:\.\d+)?)([hd])$', expr)
        if m:
            num = float(m.group(1))
            return timedelta(hours=num) if m.group(2) == 'h' else timedelta(days=num)
        return None


def _token_match(qt: str, tokens: List[str]) -> float:
    """Best match score between a query token and item tokens (0-1).

    Exact > prefix > substring > fuzzy.
    """
    best = 0.0
    for it in tokens:
        if qt == it:
            return 1.0
        if it.startswith(qt) and len(qt) >= 2:
            best = max(best, 0.9)
        elif qt.startswith(it) and len(it) >= 2:
            best = max(best, 0.8)
        elif len(qt) >= 3 and qt in it:
            # Substring (e.g. 'max' inside 'tmax')
            best = max(best, 0.85)
        elif len(qt) >= 3 and len(it) >= 3:
            ratio = fuzz.ratio(qt, it)
            if ratio > 80:
                best = max(best, ratio / 100 * 0.7)
    return best


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def _format_iso(dt: datetime) -> str:
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_coords(coords: str) -> Tuple[Optional[str], List[str]]:
    """
    Parse a coords expression into WKT (pass raw WKT through).

    Supported:
        POINT(-105 40) / POLYGON(...) / LINESTRING(...)   → passthrough
        "-105,40" or "-105 40"                            → POINT(-105 40)
        "-105,40;-104,41" or "-105 40 -104 41"            → LINESTRING(-105 40,-104 41)

    Returns:
        (WKT string or None, warnings)
    """
    warnings = []
    s = coords.strip()

    upper = s.upper()
    for wkt_type in ('POINT', 'POLYGON', 'LINESTRING', 'MULTIPOINT', 'MULTIPOLYGON'):
        if upper.startswith(wkt_type + '(') or upper.startswith(wkt_type + ' ('):
            return s, warnings

    # Normalize separators: ';' between points, ',' or ' ' between lon/lat
    points = []
    for chunk in s.split(';'):
        chunk = chunk.strip()
        if not chunk:
            continue
        for sep in (',', ' '):
            if sep in chunk:
                lon, lat = chunk.split(sep, 1)
                try:
                    points.append((float(lon), float(lat)))
                except ValueError:
                    return None, [f"Cannot parse coords '{coords}'"]
                break
        else:
            return None, [f"Cannot parse coords '{coords}' - use 'lon,lat'"]

    if not points:
        return None, [f"Cannot parse coords '{coords}'"]

    if len(points) == 1:
        lon, lat = points[0]
        return f"POINT({lon} {lat})", warnings

    wkt_points = ','.join(f"{lon} {lat}" for lon, lat in points)
    return f"LINESTRING({wkt_points})", warnings


def bbox_to_wkt(bbox: str) -> Optional[str]:
    """
    Convert 'minx,miny,maxx,maxy' to a rectangular WKT POLYGON
    (used by area and cube queries).
    """
    try:
        minx, miny, maxx, maxy = (float(v) for v in bbox.split(','))
    except ValueError:
        return None
    if minx >= maxx or miny >= maxy:
        return None
    return (f"POLYGON(({minx} {miny},{maxx} {miny},"
            f"{maxx} {maxy},{minx} {maxy},{minx} {miny}))")
