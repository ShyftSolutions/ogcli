#!/usr/bin/env python3
"""
EDR (OGC API - Environmental Data Retrieval) parser.

Parses the /collections JSON document (the EDR equivalent of WMS
GetCapabilities) into structured dataclasses, and summarizes
CoverageJSON query responses.

Reference: OGC API - EDR Part 1: Core
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any, Tuple

logger = logging.getLogger(__name__)


def _label_of(value) -> str:
    """
    Extract a human-readable label from nested JSON, e.g.:
        {"label": {"en": "RH"}}    -> "RH"
        {"en": "RH"}               -> "RH"
        {"value": "km2", ...}      -> "km2"
        "RH"                       -> "RH"
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("label", "en", "name", "value", "id"):
            if key in value:
                got = _label_of(value[key])
                if got:
                    return got
    return ""


@dataclass
class Parameter:
    """A parameter (observable) offered by a collection"""
    id: str
    label: str = ""
    unit: str = ""
    observed_property: str = ""

    @classmethod
    def from_json(cls, data: dict) -> "Parameter":
        unit = data.get("unit", "")
        if isinstance(unit, dict):
            symbol = unit.get("symbol")
            if isinstance(symbol, dict):
                # e.g. {"value": "km2", "type": "<qudt uri>"}
                symbol = _label_of(symbol)
            unit = symbol or _label_of(unit) or ""
        obs = _label_of(data.get("observedProperty", ""))
        return cls(
            id=data.get("id", ""),
            label=data.get("label", "") or "",
            unit=unit or "",
            observed_property=obs or "",
        )

    def display(self) -> str:
        parts = [self.id]
        if self.label and self.label != self.id:
            parts.append(self.label)
        if self.unit:
            parts.append(f"[{self.unit}]")
        return " ".join(parts)


@dataclass
class EDRCollection:
    """An EDR collection (analogous to a WMS layer)"""
    id: str
    title: str = ""
    description: str = ""

    # Spatial extent
    bbox: Optional[List[float]] = None       # [minx, miny, maxx, maxy]
    crs: str = "CRS:84"

    # Temporal extent
    temporal_intervals: List[Tuple[Optional[str], Optional[str]]] = field(default_factory=list)
    temporal_values: List[str] = field(default_factory=list)   # discrete instants if advertised

    # Vertical extent
    vertical_values: List[str] = field(default_factory=list)   # as strings (URL-ready)
    vertical_intervals: List[Tuple[Optional[float], Optional[float]]] = field(default_factory=list)
    vertical_unit: str = ""

    # Query support
    data_queries: List[str] = field(default_factory=list)      # position, area, cube, ...
    parameter_names: Dict[str, Parameter] = field(default_factory=dict)

    # Raw links
    links: List[dict] = field(default_factory=list)

    def supports(self, query_type: str) -> bool:
        return query_type in self.data_queries

    def supported_queries(self) -> List[str]:
        return list(self.data_queries)

    @property
    def temporal_start(self) -> Optional[str]:
        if self.temporal_values:
            return self.temporal_values[0]
        if self.temporal_intervals:
            return self.temporal_intervals[0][0]
        return None

    @property
    def temporal_end(self) -> Optional[str]:
        if self.temporal_values:
            return self.temporal_values[-1]
        if self.temporal_intervals:
            return self.temporal_intervals[0][1]
        return None

    @property
    def n_timesteps(self) -> int:
        return len(self.temporal_values)

    @property
    def n_levels(self) -> int:
        return len(self.vertical_values)

    def center(self) -> Optional[Tuple[float, float]]:
        """Center point (lon, lat) of the spatial extent"""
        if not self.bbox or len(self.bbox) < 4:
            return None
        return ((self.bbox[0] + self.bbox[2]) / 2.0, (self.bbox[1] + self.bbox[3]) / 2.0)

    def summary_line(self) -> str:
        """One-line summary for listings"""
        bits = []
        if self.n_timesteps:
            bits.append(f"{self.n_timesteps}t")
        if self.n_levels:
            bits.append(f"{self.n_levels}z")
        if self.parameter_names:
            bits.append(f"{len(self.parameter_names)}p")
        extras = f" ({', '.join(bits)})" if bits else ""
        return f"{self.id}{extras}"


@dataclass
class EDRCapabilities:
    """Parsed EDR /collections document"""
    title: str = ""
    description: str = ""
    server_url: str = ""
    collections: List[EDRCollection] = field(default_factory=list)

    def get_collection(self, collection_id: str) -> Optional[EDRCollection]:
        for c in self.collections:
            if c.id == collection_id:
                return c
        return None

    @property
    def query_types(self) -> List[str]:
        """Union of query types across all collections"""
        types: List[str] = []
        for c in self.collections:
            for q in c.data_queries:
                if q not in types:
                    types.append(q)
        return sorted(types)


def _pretty_crs(crs: str) -> str:
    """
    Normalize a CRS identifier. Some servers (e.g. Met Office Labs)
    provide full WKT instead of a URI - extract the EPSG authority
    code from it for a readable display.
    """
    if crs and re.match(r'^(GEOGCS|PROJCS|GEODCRS|ENGCS)\[', crs):
        codes = re.findall(r'AUTHORITY\["EPSG","(\d+)"\]', crs)
        if codes:
            return f"EPSG:{codes[-1]}"
    return crs


def _parse_bbox(extent: dict) -> Tuple[Optional[List[float]], str]:
    spatial = extent.get("spatial", {}) or {}
    bboxes = spatial.get("bbox") or []
    crs = _pretty_crs(spatial.get("crs", "CRS:84") or "CRS:84")
    if bboxes and isinstance(bboxes[0], list):
        return list(bboxes[0]), crs
    return None, crs


def _parse_temporal(extent: dict) -> Tuple[List[Tuple[Optional[str], Optional[str]]], List[str]]:
    temporal = extent.get("temporal", {}) or {}
    intervals = []
    for iv in temporal.get("interval") or []:
        if isinstance(iv, list) and len(iv) >= 2:
            intervals.append((iv[0], iv[1]))
    values = [str(v) for v in (temporal.get("values") or [])]
    return intervals, values


def _parse_vertical(extent: dict) -> Tuple[List[str], List[Tuple[Optional[float], Optional[float]]], str]:
    vertical = extent.get("vertical", {}) or {}
    values = []
    for v in vertical.get("values") or []:
        values.append(str(v))
    intervals = []
    for iv in vertical.get("interval") or []:
        if isinstance(iv, list) and len(iv) >= 2:
            try:
                intervals.append((float(iv[0]) if iv[0] is not None else None,
                                  float(iv[1]) if iv[1] is not None else None))
            except (TypeError, ValueError):
                pass
    vrs = vertical.get("vrs", "")
    if isinstance(vrs, list):
        vrs = vrs[0] if vrs else ""
    return values, intervals, str(vrs)


def parse_collection(data: dict) -> EDRCollection:
    """Parse a single collection JSON object"""
    extent = data.get("extent", {}) or {}

    bbox, crs = _parse_bbox(extent)
    temporal_intervals, temporal_values = _parse_temporal(extent)
    vertical_values, vertical_intervals, vertical_unit = _parse_vertical(extent)

    data_queries = list((data.get("data_queries", {}) or {}).keys())

    parameter_names = {}
    for pname, pdata in (data.get("parameter_names", {}) or {}).items():
        if isinstance(pdata, dict):
            param = Parameter.from_json(pdata)
            param.id = param.id or pname
            parameter_names[pname] = param
        else:
            parameter_names[pname] = Parameter(id=pname)

    return EDRCollection(
        id=data.get("id", ""),
        title=data.get("title", ""),
        description=data.get("description", ""),
        bbox=bbox,
        crs=crs,
        temporal_intervals=temporal_intervals,
        temporal_values=temporal_values,
        vertical_values=vertical_values,
        vertical_intervals=vertical_intervals,
        vertical_unit=vertical_unit,
        data_queries=data_queries,
        parameter_names=parameter_names,
        links=data.get("links", []) or [],
    )


def parse_collections(data, server_url: str = "") -> EDRCapabilities:
    """
    Parse an EDR /collections JSON document.

    Args:
        data: Raw JSON string, bytes, or already-parsed dict
        server_url: Base URL of the EDR service

    Returns:
        EDRCapabilities object
    """
    if isinstance(data, (str, bytes)):
        data = json.loads(data)

    collections = []
    for cdata in data.get("collections", []) or []:
        try:
            collections.append(parse_collection(cdata))
        except Exception as e:
            cid = cdata.get("id", "?")
            logger.warning(f"Failed to parse collection '{cid}': {e}")

    # Landing-page style metadata may be embedded
    title = data.get("title", "")
    description = data.get("description", "")

    return EDRCapabilities(
        title=title,
        description=description,
        server_url=server_url,
        collections=collections,
    )


def parse_landing(data) -> Tuple[str, str]:
    """
    Parse an EDR landing page document.

    Returns:
        (title, description)
    """
    if isinstance(data, (str, bytes)):
        data = json.loads(data)
    return data.get("title", ""), data.get("description", "")


@dataclass
class CoverageSummary:
    """Summary of a CoverageJSON response"""
    coverage_type: str = ""
    axes: Dict[str, int] = field(default_factory=dict)       # axis name -> count
    parameters: List[dict] = field(default_factory=list)     # {name, unit, count, min, max, nils}


def summarize_coverage(data) -> Optional[CoverageSummary]:
    """
    Summarize a CoverageJSON (or GeoJSON) response.

    Returns None if the payload isn't recognizable JSON.
    """
    try:
        if isinstance(data, (str, bytes)):
            data = json.loads(data)
        if not isinstance(data, dict):
            return None
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None

    summary = CoverageSummary(coverage_type=data.get("type", ""))

    # GeoJSON feature collections
    if summary.coverage_type == "FeatureCollection":
        features = data.get("features", [])
        summary.axes["features"] = len(features)

    # CoverageCollection: aggregate nested coverages (first few only -
    # payloads can be huge)
    nested: List[dict] = []
    if summary.coverage_type == "CoverageCollection":
        all_nested = data.get("coverages") or data.get("collections") or []
        nested = [c for c in all_nested if isinstance(c, dict)][:3]
        summary.axes["coverages"] = len(all_nested)

    # Domain axes (top-level + nested). Grid axes may use start/stop/num
    # instead of explicit values.
    for source, prefix in [(data, "")] + [(c, f"{i + 1}:") for i, c in enumerate(nested)]:
        domain = source.get("domain")
        if isinstance(domain, dict) and isinstance(domain.get("axes"), dict):
            for axis_name, axis in domain["axes"].items():
                values = axis.get("values") if isinstance(axis, dict) else None
                if isinstance(values, list):
                    n = len(values)
                elif isinstance(axis, dict) and axis.get("num") is not None:
                    n = axis["num"]
                else:
                    n = 1
                summary.axes[f"{prefix}{axis_name}"] = n

    # Parameter metadata (name -> unit)
    units: Dict[str, str] = {}
    for source in [data] + nested:
        for pname, pdata in (source.get("parameters", {}) or {}).items():
            if isinstance(pdata, dict) and pname not in units:
                unit = pdata.get("unit", "")
                if isinstance(unit, dict):
                    unit = unit.get("symbol") or unit.get("label") or ""
                units[pname] = unit or ""

    # Ranges (actual data values)
    totals: Dict[str, dict] = {}
    for source in [data] + nested:
        ranges = source.get("ranges")
        if not isinstance(ranges, dict):
            continue
        for pname, rng in ranges.items():
            values = rng.get("values") if isinstance(rng, dict) else None
            if not isinstance(values, list):
                continue
            numeric = [v for v in values
                       if isinstance(v, (int, float)) and not isinstance(v, bool)]
            agg = totals.setdefault(
                pname, {"count": 0, "nils": 0, "mins": [], "maxs": []}
            )
            agg["count"] += len(values)
            agg["nils"] += len(values) - len(numeric)
            if numeric:
                agg["mins"].append(min(numeric))
                agg["maxs"].append(max(numeric))

    for pname, agg in totals.items():
        entry = {
            "name": pname,
            "unit": units.get(pname, ""),
            "count": agg["count"],
            "nils": agg["nils"],
        }
        if agg["mins"]:
            entry["min"] = min(agg["mins"])
            entry["max"] = max(agg["maxs"])
        summary.parameters.append(entry)

    if not totals and units:
        for pname, unit in units.items():
            summary.parameters.append({"name": pname, "unit": unit, "count": 0})

    return summary
