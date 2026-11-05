#!/usr/bin/env python3
"""
ogcli MCP server - exposes the WMS + EDR tooling to LLM agents via the
Model Context Protocol (stdio).

Runs on the same core as the wms/edr CLIs (src/wms, src/edr), sharing
their config and caches in ~/.wms/.

Run directly:
    python -m src.mcp_server
or via the console script (after `pip install -e ".[mcp]"`):
    ogcli-mcp

Register with Claude Code:
    claude mcp add ogcli -- /path/to/venv/bin/ogcli-mcp
"""

import base64
import json
import logging
from pathlib import Path
from typing import Optional

from mcp.server.mcpserver import MCPServer, Image
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import TextContent

from .common.cache import CONFIG_DIR
from .edr.config import (
    load_edr_config, get_edr_capabilities,
    init_from_server as edr_init_server,
)
from .edr.parser import summarize_coverage
from .edr.client import EDRClient
from .edr.resolver import (
    EDRResolver, AmbiguousCollectionError, parse_coords, bbox_to_wkt,
)
from .edr import hammer as edr_hammer_mod
from .wms.config import (
    load_config, get_capabilities_xml,
    init_from_server as wms_init_server,
)
from .wms.parser import parse_capabilities
from .wms.client import WMSClient
from .wms.resolver import SmartQueryResolver, AmbiguousQueryError

logger = logging.getLogger(__name__)

# Where tool outputs (raw payloads, maps, hammer responses) are saved
MCP_OUTPUT_DIR = CONFIG_DIR / "mcp_output"

# Safety rails for load generation via agents
HAMMER_MAX_REQUESTS = 500
HAMMER_MAX_WORKERS = 64

# Cap on JSON returned to the model (full payloads go to disk instead)
MAX_JSON_CHARS = 50_000

INSTRUCTIONS = """ogcli provides two OGC weather-geospatial services:

WMS (Web Map Service) - map images rendered per layer:
- Layers are named like GFS_Temperature_F or GALWEM_CloudBase
- Natural dimension syntax: 'f'/'c' (units), '6h' (forecast hours),
  '12z' (model run cycle), '850' (pressure level)

EDR (Environmental Data Retrieval) - point/area data as JSON:
- Collections are named like hrrr-surface or nbm-conus-2m-agl
- Fuzzy queries work for both: 'hrrr surface temp' resolves the
  collection AND the parameter (TMP)

Tips:
- Start with edr_collections / wms_layers to see what exists
- edr_query returns a SUMMARY of the coverage; the full payload is
  saved to disk and the path is included in the response
- WMS queries with tight bounding boxes are faster and cheaper

Hammer etiquette (load testing):
- The hammer tools generate real traffic against real servers.
- ALWAYS call with dry_run=true first to preview URLs and count.
- Confirm with the user before sending more than ~100 requests,
  and never exceed what they asked for.
- Responses are not saved by default; enable save only when asked.
"""


mcp = MCPServer("ogcli", instructions=INSTRUCTIONS)


# =============================================================================
# Shared helpers
# =============================================================================

def _edr_context(profile: str = ""):
    """Load EDR config + resolver, or raise a friendly error."""
    config = load_edr_config()
    if profile:
        if not config.has_profile(profile):
            raise ToolError(
                f"No EDR profile named {profile!r}; saved: "
                f"{', '.join(config.profile_names()) or '(none)'}"
            )
        config.active = profile
    if not config.is_initialized():
        raise ToolError(
            "EDR not initialized. Call the edr_init tool first with the "
            "server URL (e.g. https://folkweather.com/edr)."
        )
    caps = get_edr_capabilities(config)
    return config, EDRResolver(caps)


def _wms_context(profile: str = ""):
    """Load WMS config + resolver, or raise a friendly error."""
    config = load_config()
    if profile:
        if not config.has_profile(profile):
            raise ToolError(
                f"No WMS profile named {profile!r}; saved: "
                f"{', '.join(config.profile_names()) or '(none)'}"
            )
        config.active = profile
    if not config.is_initialized():
        raise ToolError(
            "WMS not initialized. Call the wms_init tool first with the "
            "server URL (e.g. https://ogc.shyftwx.com/ogc/WMS)."
        )
    caps = parse_capabilities(get_capabilities_xml(config))
    return config, SmartQueryResolver(caps)


def _resolve_edr(resolver, query: str):
    """resolver.resolve() with agent-friendly errors."""
    try:
        return resolver.resolve(query)
    except AmbiguousCollectionError as e:
        alts = ", ".join(e.alternatives[:5]) or "(none)"
        raise ToolError(f"{e} Alternatives: {alts}") from None


def _resolve_wms(resolver, query: str):
    """resolver.resolve() with agent-friendly errors."""
    try:
        return resolver.resolve(query)
    except AmbiguousQueryError as e:
        alts = ", ".join(e.alternatives[:5]) or "(none)"
        raise ToolError(f"{e} Alternatives: {alts}") from None


def _save_payload(name: str, data: bytes, ext: str = "json") -> str:
    """Save raw bytes to the MCP output dir; returns absolute path."""
    MCP_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = MCP_OUTPUT_DIR / f"{name}.{ext}"
    path.write_bytes(data)
    return str(path)


def _dump(data, max_chars: int = MAX_JSON_CHARS) -> str:
    """Serialize to JSON, truncating huge payloads with a notice."""
    text = json.dumps(data, indent=2, default=str)
    if len(text) > max_chars:
        text = text[:max_chars] + f'\n...[truncated at {max_chars} chars - refine the query or read the saved file]'
    return text


def _coverage_summary_dict(body: bytes) -> dict:
    """Compact CoverageJSON/GeoJSON summary for the model."""
    summary = summarize_coverage(body)
    if summary is None:
        return {"note": "non-JSON response"}
    return {
        "type": summary.coverage_type,
        "axes": summary.axes,
        "parameters": summary.parameters,
    }


def _clamp_hammer(limit: int, workers: int) -> tuple:
    """Clamp hammer params to safety rails; returns (limit, workers, notes)."""
    notes = []
    if limit > HAMMER_MAX_REQUESTS:
        notes.append(f"limit clamped to {HAMMER_MAX_REQUESTS} (safety cap)")
        limit = HAMMER_MAX_REQUESTS
    if workers > HAMMER_MAX_WORKERS:
        notes.append(f"workers clamped to {HAMMER_MAX_WORKERS} (safety cap)")
        workers = HAMMER_MAX_WORKERS
    return limit, workers, notes


# =============================================================================
# EDR tools
# =============================================================================

@mcp.tool()
def edr_status(profile: str = "") -> str:
    """Show the current EDR server configuration and collection summary."""
    config = load_edr_config()
    if profile:
        if not config.has_profile(profile):
            raise ToolError(
                f"No EDR profile named {profile!r}; saved: "
                f"{', '.join(config.profile_names()) or '(none)'}"
            )
        config.active = profile
    if not config.is_initialized():
        return _dump({"initialized": False,
                      "hint": "call edr_init with a server URL first"})
    caps = get_edr_capabilities(config)
    return _dump({
        "initialized": True,
        "profile": config.active,
        "server": config.server_url,
        "n_collections": len(caps.collections),
        "query_types": caps.query_types,
    })


@mcp.tool()
def edr_init(url: str, api_key: str = "") -> str:
    """Initialize the EDR context from a server URL (fetches /collections).

    Changes the shared ogcli EDR config (~/.wms/edr_config.json) used by
    both this server and the edr CLI.
    """
    config = edr_init_server(url, api_key or "")
    caps = get_edr_capabilities(config)
    return _dump({
        "initialized": True,
        "server": config.server_url,
        "n_collections": len(caps.collections),
        "query_types": caps.query_types,
    })


@mcp.tool()
def edr_collections(query: str = "", limit: int = 20, profile: str = "") -> str:
    """List EDR collections, optionally fuzzy-filtered.

    The query matches collection ids, titles, and parameter names, e.g.
    'hrrr surface', 'guam', 'temp'. Each result shows the supported
    query types, parameter names, timestep/level counts, and bbox.
    """
    _, resolver = _edr_context(profile)
    results = resolver.list_collections(query or None, limit=limit)
    return _dump({
        "query": query or None,
        "count": len(results),
        "collections": [
            {
                "id": c.id,
                "title": c.title,
                "score": round(score, 1),
                "query_types": c.data_queries,
                "parameters": list(c.parameter_names.keys()),
                "timesteps": c.n_timesteps,
                "levels": c.n_levels,
                "vertical_unit": c.vertical_unit,
                "temporal": [c.temporal_start, c.temporal_end],
                "bbox": c.bbox,
            }
            for c, score in results
        ],
    })


@mcp.tool()
def edr_describe(query: str, profile: str = "") -> str:
    """Show full details of one EDR collection (fuzzy-matched):
    extent, temporal/vertical values, parameters with units, and the
    query types it supports.
    """
    _, resolver = _edr_context(profile)
    resolved = _resolve_edr(resolver, query)
    c = resolved.collection
    return _dump({
        "id": c.id,
        "title": c.title,
        "description": c.description,
        "confidence": round(resolved.confidence),
        "query_types": c.data_queries,
        "bbox": c.bbox,
        "crs": c.crs,
        "temporal": {
            "start": c.temporal_start,
            "end": c.temporal_end,
            "n_timesteps": c.n_timesteps,
            "sample_values": c.temporal_values[:10],
        },
        "vertical": {
            "values": c.vertical_values[:30],
            "unit": c.vertical_unit,
        },
        "parameters": {
            name: {"label": p.label, "unit": p.unit}
            for name, p in c.parameter_names.items()
        },
    })


@mcp.tool()
def edr_query(
    query: str,
    query_type: str = "position",
    coords: Optional[str] = None,
    bbox: Optional[str] = None,
    datetime: Optional[str] = None,
    z: Optional[str] = None,
    param: Optional[str] = None,
    within: Optional[str] = None,
    width: Optional[str] = None,
    instance: Optional[str] = None,
    save: bool = True,
    profile: str = "",
) -> str:
    """Run an EDR query and return a compact summary of the data.

    query_type: position | radius | area | cube | trajectory | corridor
    profile: saved server profile (default: active profile)
    query: fuzzy collection (and parameter) e.g. 'hrrr surface temp'
    coords: 'lon,lat' (point) or 'lon1,lat1;lon2,lat2' (line) or WKT
    bbox: 'minx,miny,maxx,maxy' (for area/cube; defaults to extent for cube)
    datetime: 'latest', '+6h' (from extent start), ISO instant, or 'a/b'
    z: vertical level e.g. '850', '850mb', or 'low/high' interval
    param: parameter name(s), comma-separated (default: auto-resolved)
    within: radius distance e.g. '25km' (radius queries)
    width: corridor width e.g. '10km' (corridor queries)

    Returns axes + per-parameter value ranges. The full payload is saved
    to disk when save=true and the path is included. Point queries
    without coords use the collection center.
    """
    config, resolver = _edr_context(profile)
    resolved = _resolve_edr(resolver, query)
    c = resolved.collection

    if c.data_queries and not c.supports(query_type):
        raise ToolError(
            f"Collection '{c.id}' does not support {query_type}; "
            f"supported: {', '.join(c.data_queries)}"
        )

    warnings = []

    # Geometry
    params = {}
    if query_type == "cube":
        if bbox:
            params["bbox"] = bbox
        elif c.bbox:
            params["bbox"] = ",".join(str(v) for v in c.bbox)
            warnings.append("bbox defaults to full collection extent")
        else:
            raise ToolError("cube requires bbox (collection has no extent)")
    elif bbox:
        wkt = bbox_to_wkt(bbox)
        if not wkt:
            raise ToolError(f"invalid bbox {bbox!r} (use minx,miny,maxx,maxy)")
        params["coords"] = wkt
    elif coords:
        wkt, pwarn = parse_coords(coords)
        if not wkt:
            raise ToolError(f"invalid coords {coords!r} (use 'lon,lat')")
        warnings.extend(pwarn)
        params["coords"] = wkt
    elif query_type in ("position", "radius"):
        center = c.center()
        if not center:
            raise ToolError(f"{query_type} requires coords")
        params["coords"] = f"POINT({center[0]} {center[1]})"
        warnings.append("coords default to collection center")
    else:
        raise ToolError(f"{query_type} requires coords (or bbox for area)")

    # Dimensions
    dt_expr = datetime or resolved.extracted.get("datetime")
    if dt_expr:
        dt_value, dwarn = resolver.resolve_datetime(dt_expr, c)
        warnings.extend(dwarn)
        if dt_value:
            params["datetime"] = dt_value

    z_expr = z or resolved.extracted.get("z")
    if z_expr:
        z_value, zwarn = resolver.resolve_z(z_expr, c)
        warnings.extend(zwarn)
        if z_value:
            params["z"] = z_value

    param_value = param or resolved.parameter
    if param_value:
        params["parameter-name"] = param_value
    if within:
        params["within"] = within
    if width:
        from .edr.client import corridor_params
        params.update(corridor_params(width))

    client = EDRClient(config.server_url, api_key=config.api_key)
    body, content_type = client.query(
        c.id, query_type, instance_id=instance, **params
    )

    result = {
        "collection": c.id,
        "query_type": query_type,
        "params": params,
        "instance": instance,
        "content_type": content_type,
        "bytes": len(body),
        "warnings": warnings,
        "summary": _coverage_summary_dict(body),
    }
    if save:
        ext = "json" if "json" in (content_type or "").lower() else "bin"
        result["saved_to"] = _save_payload(
            f"{c.id}_{query_type}".replace("/", "-"), body, ext
        )
    return _dump(result)


@mcp.tool()
def edr_instances(query: str, limit: int = 20, profile: str = "") -> str:
    """List model-run instances (e.g. individual HRRR runs) for a
    collection. Use an instance id in edr_query's instance parameter."""
    config, resolver = _edr_context(profile)
    resolved = _resolve_edr(resolver, query)
    client = EDRClient(config.server_url, api_key=config.api_key)
    data = client.get_instances(resolved.collection.id)
    instances = data.get("instances", []) if isinstance(data, dict) else data
    return _dump({
        "collection": resolved.collection.id,
        "count": len(instances),
        "instances": [
            {"id": i.get("id"), "title": i.get("title", "")}
            for i in instances[:limit]
        ],
    })


@mcp.tool()
def edr_locations(query: str, location_id: str = "", limit: int = 20, profile: str = "") -> str:
    """List named locations for a collection (stations, cities, ...).

    With location_id, fetches the data at that location instead."""
    config, resolver = _edr_context(profile)
    resolved = _resolve_edr(resolver, query)
    c = resolved.collection
    client = EDRClient(config.server_url, api_key=config.api_key)

    if location_id:
        body, ctype = client.get_location(c.id, location_id)
        return _dump({
            "collection": c.id,
            "location": location_id,
            "content_type": ctype,
            "bytes": len(body),
            "summary": _coverage_summary_dict(body),
            "saved_to": _save_payload(
                f"{c.id}_{location_id}".replace("/", "-"), body,
                "json" if "json" in (ctype or "").lower() else "bin",
            ),
        })

    data = client.get_locations(c.id)
    features = data.get("features", []) if isinstance(data, dict) else []
    return _dump({
        "collection": c.id,
        "count": len(features),
        "locations": [
            {
                "id": f.get("id"),
                "name": (f.get("properties", {}) or {}).get("name", ""),
                "coords": (f.get("geometry", {}) or {}).get("coordinates"),
            }
            for f in features[:limit]
        ],
    })


@mcp.tool()
def edr_hammer(
    query: str,
    dry_run: bool = True,
    limit: int = 100,
    workers: int = 20,
    query_type: str = "position",
    max_timesteps: int = 0,
    max_params: int = 0,
    max_levels: int = 0,
    points: int = 1,
    save: bool = False,
    profile: str = "",
) -> str:
    """Load-test EDR collections with a full dimension sweep
    (collections x parameters x timesteps x z-levels x random points).

    SAFETY: dry_run=true (the default) previews URLs without sending
    anything. Confirm the count looks reasonable, then re-call with
    dry_run=false. Ask the user before runs larger than ~100 requests.
    Limits are capped at 500 requests / 64 workers.

    Returns per-run stats: ok/failed counts, throughput, latency
    percentiles, HTTP status breakdown.
    """
    config, resolver = _edr_context(profile)
    limit, workers, notes = _clamp_hammer(limit, workers)

    results = resolver.list_collections(query, limit=200)
    matching = [c for c, score in results if score > 50]
    if not matching:
        raise ToolError(f"No collections match {query!r}")

    hammer = edr_hammer_mod.EDRHammer(
        base_url=config.server_url,
        collections=matching,
        api_key=config.api_key,
        workers=workers,
        query_type=query_type,
        output=str(MCP_OUTPUT_DIR / "hammer"),
        save=save,
        dry_run=dry_run,
        max_timesteps=max_timesteps,
        max_params=max_params,
        max_levels=max_levels,
        points=points,
        limit=limit,
    )
    hammer.generate()
    if dry_run:
        sample = [
            f"{r.url}?{'&'.join(f'{k}={v}' for k, v in r.params.items() if v)}"
            for r in hammer.requests[:5]
        ]
        return _dump({
            "dry_run": True,
            "note": "preview only - re-call with dry_run=false to send",
            "collections": [c.id for c in matching],
            "total_requests": len(hammer.requests),
            "sample_urls": sample,
            "notes": notes,
        })

    stats = hammer.run()
    return _dump({
        "collections": [c.id for c in matching],
        "total_requests": stats.total,
        "ok": stats.success,
        "failed": stats.failed,
        "status_breakdown": stats.status_counts,
        "duration_s": round(stats.end - stats.start, 2),
        "req_per_s": round(
            stats.total / (stats.end - stats.start), 1
        ) if stats.end > stats.start else None,
        "data_kb": round(stats.bytes / 1024, 1),
        "notes": notes,
    })


# =============================================================================
# WMS tools
# =============================================================================

@mcp.tool()
def wms_status(profile: str = "") -> str:
    """Show the current WMS server configuration and layer summary."""
    config = load_config()
    if profile:
        if not config.has_profile(profile):
            raise ToolError(
                f"No WMS profile named {profile!r}; saved: "
                f"{', '.join(config.profile_names()) or '(none)'}"
            )
        config.active = profile
    if not config.is_initialized():
        return _dump({"initialized": False,
                      "hint": "call wms_init with a server URL first"})
    caps = parse_capabilities(get_capabilities_xml(config))
    return _dump({
        "initialized": True,
        "profile": config.active,
        "server": config.server_url,
        "service": caps.service_title,
        "n_layers": len(caps.get_queryable_layers()),
    })


@mcp.tool()
def wms_init(url: str) -> str:
    """Initialize the WMS context from a server URL (fetches
    GetCapabilities). Changes the shared ogcli WMS config (~/.wms/)."""
    config = wms_init_server(url)
    caps = parse_capabilities(get_capabilities_xml(config))
    return _dump({
        "initialized": True,
        "server": config.server_url,
        "service": caps.service_title,
        "n_layers": len(caps.get_queryable_layers()),
    })


@mcp.tool()
def wms_layers(query: str = "", limit: int = 20, profile: str = "") -> str:
    """List WMS layers, optionally fuzzy-filtered ('gfs temp', 'cloud').

    Layer names encode model + parameter (e.g. GFS_Temperature_F);
    common dimensions are RUN/FORECAST/ELEVATION."""
    _, resolver = _wms_context(profile)
    results = resolver.list_layers(query or None, limit=limit)
    return _dump({
        "query": query or None,
        "count": len(results),
        "layers": [
            {
                "name": r.layer.name,
                "title": r.layer.title,
                "score": round(r.score, 1),
                "dimensions": list(r.layer.dimensions.keys()),
                "styles": [s.name for s in r.layer.styles],
            }
            for r in results
        ],
    })


@mcp.tool()
def wms_describe(query: str, profile: str = "") -> str:
    """Show full details of one WMS layer (fuzzy-matched): dimensions
    with available values, styles, bbox, CRS options."""
    _, resolver = _wms_context(profile)
    resolved = _resolve_wms(resolver, query)
    layer = resolved.layer
    return _dump({
        "name": layer.name,
        "title": layer.title,
        "abstract": layer.abstract,
        "confidence": round(resolved.confidence),
        "dimensions": {
            name: {
                "default": dim.default,
                "n_values": len(dim.values),
                "sample_values": dim.values[:10],
            }
            for name, dim in layer.dimensions.items()
        },
        "styles": [
            {"name": s.name, "title": s.title} for s in layer.styles
        ],
        "bbox": (layer.geographic_bbox and
                 [layer.geographic_bbox.minx, layer.geographic_bbox.miny,
                  layer.geographic_bbox.maxx, layer.geographic_bbox.maxy]),
        "crs": layer.crs_list[:10],
    })


@mcp.tool()
def wms_map(
    query: str,
    width: int = 800,
    height: int = 600,
    bbox: Optional[str] = None,
    image: bool = True,
    profile: str = "",
) -> list:
    """Fetch a WMS map image (GetMap) and return it inline as a PNG
    the model can view, plus resolution details.

    query: fuzzy layer match, e.g. 'gfs temp f 6h' (natural dimension
    syntax supported: f/c units, 6h forecast, 12z run, 850 level)
    bbox: 'minx,miny,maxx,maxy' (default: layer extent)

    The image is also saved to ~/.wms/mcp_output/ and the path is
    included in the text part of the response."""
    config, resolver = _wms_context(profile)
    resolved = resolver.resolve(query)
    layer = resolved.layer

    use_bbox = tuple(float(v) for v in bbox.split(",")) if bbox else resolved.bbox
    dim_kwargs = {}
    for dim_name, value in resolved.dimensions.items():
        dim_kwargs[resolved.get_dimension_param_name(dim_name)] = value

    client = WMSClient(config.server_url)
    png = client.get_map(
        layer=layer.name,
        bbox=use_bbox,
        width=width,
        height=height,
        crs=resolved.crs,
        styles=resolved.style,
        format="image/png",
        **dim_kwargs,
    )
    saved = _save_payload(f"map_{layer.name}", png, "png")

    info = _dump({
        "layer": layer.name,
        "style": resolved.style,
        "dimensions": resolved.dimensions,
        "bbox": list(use_bbox),
        "size": [width, height],
        "bytes": len(png),
        "saved_to": saved,
        "confidence": round(resolved.confidence),
        "warnings": resolved.warnings,
    })
    return [TextContent(type="text", text=info),
            Image(data=png, format="png").to_image_content()]


@mcp.tool()
def wms_url(query: str, request_type: str = "map",
            width: int = 800, height: int = 600, profile: str = "") -> str:
    """Build a WMS request URL without fetching (map | tile | legend)."""
    config, resolver = _wms_context(profile)
    resolved = resolver.resolve(query)
    dim_kwargs = {
        resolved.get_dimension_param_name(n).upper(): v
        for n, v in resolved.dimensions.items()
    }
    from urllib.parse import urlencode
    if request_type == "map":
        params = {
            "SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetMap",
            "LAYERS": resolved.layer.name,
            "BBOX": ",".join(str(x) for x in resolved.bbox),
            "WIDTH": width, "HEIGHT": height,
            "CRS": resolved.crs, "FORMAT": "image/png",
            "STYLES": resolved.style, **dim_kwargs,
        }
    elif request_type == "tile":
        params = {
            "SERVICE": "WMS", "VERSION": "1.3.0", "REQUEST": "GetGTile",
            "LAYER": resolved.layer.name, "TILEZOOM": 3,
            "TILEROW": 4, "TILECOL": 4, "CRS": "EPSG:900913",
            "FORMAT": "image/png", "STYLE": resolved.style, **dim_kwargs,
        }
    elif request_type == "legend":
        params = {
            "SERVICE": "WMS", "VERSION": "1.3.0",
            "REQUEST": "GetLegendGraphic",
            "LAYER": resolved.layer.name,
            "STYLE": resolved.style, "FORMAT": "image/png",
        }
    else:
        raise ToolError(f"unknown request_type {request_type!r}")
    return f"{config.server_url}?{urlencode(params)}"


@mcp.tool()
def wms_hammer(
    query: str,
    dry_run: bool = True,
    limit: int = 100,
    workers: int = 20,
    profile: str = "",
) -> str:
    """Load-test WMS layers matching a query (GetMap sweep across
    dimension combinations).

    SAFETY: dry_run=true (the default) previews without sending
    anything. Confirm the count, then re-call with dry_run=false. Ask
    the user before runs larger than ~100 requests. Capped at 500
    requests / 64 workers."""
    import argparse
    import time as time_mod

    config, resolver = _wms_context(profile)
    limit, workers, notes = _clamp_hammer(limit, workers)

    results = resolver.list_layers(query, limit=100)
    matching = [r.layer for r in results if r.score > 50]
    if not matching:
        raise ToolError(f"No layers match {query!r}")

    from .wms.hammer import WMSHammer
    caps = parse_capabilities(get_capabilities_xml(config))
    args = argparse.Namespace(
        url=config.server_url, glob="*", layer=None, list_layers=False,
        workers=workers, output=str(MCP_OUTPUT_DIR / "wms_hammer"),
        max_timesteps=0, latest_run=False, tile_mode=False,
        dry_run=False, verbose=False, random_tiles=False, no_save=True,
        single_forecast=False, num_random_tiles=100, timeout=30,
    )
    hammer = WMSHammer(args, caps)

    # Generate the request list ourselves so we can cap it (run()
    # regenerates from self.layers with no cap support)
    all_requests = []
    for layer in matching:
        all_requests.extend(hammer._generate_requests_for_layer(layer))
    capped = len(all_requests) > limit
    all_requests = all_requests[:limit]

    layer_names = [l.name for l in matching[:20]]
    sample = [
        {
            "layer": r["layer"].name,
            "dimensions": r["dimensions"],
        }
        for r in all_requests[:5]
    ]

    if dry_run:
        return _dump({
            "dry_run": True,
            "note": "preview only - re-call with dry_run=false to send",
            "layers": layer_names,
            "total_requests": len(all_requests),
            "truncated_to_limit": capped,
            "sample_requests": sample,
            "notes": notes,
        })

    hammer.start_time = time_mod.time()
    hammer._execute_requests(all_requests)
    duration = time_mod.time() - hammer.start_time
    return _dump({
        "layers": layer_names,
        "total_requests": len(all_requests),
        "ok": hammer.successful_requests,
        "failed": hammer.failed_requests,
        "duration_s": round(duration, 2),
        "req_per_s": round(len(all_requests) / duration, 1) if duration else None,
        "truncated_to_limit": capped,
        "notes": notes,
    })


# =============================================================================
# Entry point
# =============================================================================

def main():
    """Run the ogcli MCP server over stdio."""
    logging.basicConfig(level=logging.WARNING)
    # Keep our internal request logging out of the client's stderr
    logging.getLogger("src").setLevel(logging.WARNING)
    mcp.run()


if __name__ == "__main__":
    main()
