#!/usr/bin/env python3
"""
EDR CLI - Command-line interface for OGC API - EDR queries with fuzzy search.

Usage mirrors the WMS CLI: init against a server, fuzzy-search collections,
query with natural dimension syntax, and hammer for load testing.
"""

import sys
import json
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import click

from .config import (
    load_edr_config, save_edr_config, init_from_server,
    get_collections_json, clear_edr_cache, fetch_collections,
    refresh_active_profile, edr_cache_file, EDR_CACHE_DIR,
    validate_profile_name,
)
from .parser import parse_collections, summarize_coverage
from .client import EDRClient, corridor_params
from .resolver import (
    EDRResolver, AmbiguousCollectionError, parse_coords, bbox_to_wkt,
)
from .completions import complete_collection_names
from .hammer import parse_duration
from ..common.cache import ensure_config_dir, is_cache_valid


QUERY_TYPES = ['position', 'radius', 'area', 'cube', 'trajectory', 'corridor',
               'locations', 'instances']


def complete_profile_names(ctx, param, incomplete):
    """Complete saved EDR profile names."""
    try:
        from .config import load_edr_config
        config = load_edr_config()
        return [
            click.shell_completion.CompletionItem(p, help="active" if p == config.active else "")
            for p in sorted(config.profile_names())
            if p.startswith(incomplete)
        ]
    except Exception:
        return []


def get_resolver(ctx: Optional[click.Context] = None):
    """Get EDR resolver from current config (honoring a --profile override)"""
    config = getattr(ctx, "obj", None) if ctx else None
    if config is None:
        config = load_edr_config()
    if not config.is_initialized():
        active = config.active if config.profiles else None
        click.echo("Error: EDR not initialized. Run 'edr init <url>' first.", err=True)
        click.echo("\nExamples:", err=True)
        click.echo("  edr init https://folkweather.com/edr", err=True)
        click.echo("  edr init http://localhost:8006/ogc/edr --save local", err=True)
        sys.exit(1)

    caps = parse_collections(get_collections_json(config), config.server_url)
    return EDRResolver(caps), config


def sanitize_filename(name: str) -> str:
    """Sanitize string for use in filename"""
    return (name.replace(':', '-')
                .replace('/', '-')
                .replace(' ', '_')
                .replace('?', '')
                .replace('&', '_'))


def build_url(base_url: str, path: str, params: dict) -> str:
    """Build a URL with query parameters, skipping empties"""
    clean = {k: v for k, v in params.items() if v is not None and v != ''}
    url = f"{base_url.rstrip('/')}{path}"
    if clean:
        url += f"?{urlencode(clean)}"
    return url


@click.group(invoke_without_command=True)
@click.version_option(package_name='wms-cli', version='0.1.0 (python)')
@click.option('--profile', '-p', 'profile', is_flag=False, flag_value='', default=None,
              metavar='[NAME]',
              help='Use a saved server profile for this invocation. With no value, '
                   'interactively pick a profile and make it active.',
              shell_complete=complete_profile_names)
@click.pass_context
def cli(ctx, profile):
    """EDR query tool with fuzzy search (OGC API - Environmental Data Retrieval).

    Run 'edr' without arguments to see this help. (Interactive TUI coming later.)
    """
    if profile == '':
        # Bare --profile: interactive picker, sets the active profile
        config = load_edr_config()
        names = sorted(config.profile_names())
        profiles = [(n, config.profiles[n].server_url) for n in names]
        if not profiles:
            click.echo("No saved profiles. Save one with: edr init <url> --save NAME")
            sys.exit(1)
        try:
            from ..common.profiles import pick_profile_interactive
            picked = pick_profile_interactive(profiles, config.active, 'edr')
        except (RuntimeError, EOFError) as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        config.set_active(picked)
        save_edr_config(config)
        click.echo(f"\u2713 Active profile: {picked}")
        ctx.obj = config
        if ctx.invoked_subcommand is None:
            return
    elif profile:
        config = load_edr_config()
        if not config.has_profile(profile):
            click.echo(f"Error: no profile named '{profile}'.", err=True)
            click.echo(f"Saved profiles: {', '.join(config.profile_names()) or '(none)'}",
                       err=True)
            sys.exit(1)
        config.active = profile
        ctx.obj = config
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@cli.command()
@click.argument('url', required=False)
@click.option('--api-key', help='API key (or set EDR_API_KEY env var)')
@click.option('--save', 'save_as', metavar='NAME', default=None,
              help='Save as a named profile (and make it active)',
              shell_complete=complete_profile_names)
@click.option('--force', '-f', is_flag=True, help='Force re-initialization (clears cache)')
@click.pass_context
def init(ctx, url, api_key, save_as, force):
    """Initialize EDR context from a server URL.

    With no URL, refreshes the active profile's collections.

    Examples:
        edr init https://folkweather.com/edr
        edr init http://localhost:8006/ogc/edr/ --save local
        edr init https://data.hub.../edr --api-key XXX --save metoffice
        edr init                  # refresh active profile
    """
    try:
        if force and clear_edr_cache():
            click.echo("✓ Cache cleared")

        if url:
            click.echo(f"Fetching collections from {url}...")
            config = init_from_server(
                url, api_key or '',
                config=getattr(ctx, 'obj', None) if ctx and ctx.obj else None,
                save_as=save_as,
            )
            click.echo(f"✓ Initialized EDR profile '{config.active}'")
        else:
            config = refresh_active_profile(
                getattr(ctx, 'obj', None) if ctx and ctx.obj else None
            )
            click.echo(f"✓ Refreshed profile '{config.active}'")

        click.echo(f"  Server: {config.server_url}")

        caps = parse_collections(get_collections_json(config), config.server_url)
        click.echo(f"  Collections: {len(caps.collections)}")
        click.echo(f"  Query types: {', '.join(caps.query_types)}")

    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.pass_context
def status(ctx):
    """Show current EDR configuration."""
    config = getattr(ctx, 'obj', None) if ctx and ctx.obj else load_edr_config()

    if not config.is_initialized():
        click.echo("Not initialized. Run 'edr init <url>' first.")
        return

    click.echo(f"Profile: {config.active}" +
               (f" ({len(config.profiles)} saved)" if len(config.profiles) > 1 else ""))
    click.echo(f"Server: {config.server_url}")
    if config.api_key:
        click.echo("API key: set")

    try:
        caps = parse_collections(get_collections_json(config), config.server_url)
        click.echo(f"Collections: {len(caps.collections)}")
        click.echo(f"Query types: {', '.join(caps.query_types)}")

        cache = edr_cache_file(config)
        if cache.exists():
            age_min = int((time.time() - cache.stat().st_mtime) / 60)
            ttl = config.cache_ttl if config.cache_ttl > 0 else 600
            valid = is_cache_valid(cache, ttl)
            click.echo(f"Cache: {'valid' if valid else 'expired'} ({age_min} min old)")
    except Exception as e:
        click.echo(f"Error loading collections: {e}", err=True)


# =============================================================================
# Profiles
# =============================================================================

@cli.command('profiles')
def profiles_cmd():
    """List saved server profiles (* = active).

    Examples:
        edr profiles
        edr init https://folkweather.com/edr --save folkweather
        edr set profile local
    """
    config = load_edr_config()
    if not config.profiles:
        click.echo("No saved profiles. Save one with: edr init <url> --save NAME")
        return

    for name in sorted(config.profiles):
        p = config.profiles[name]
        marker = "*" if name == config.active else " "
        key = "  (api key set)" if p.api_key else ""
        click.echo(f"{marker} {name:20} {p.server_url}{key}")


@cli.group('set')
def set_cmd():
    """Set configuration values."""
    pass


@set_cmd.command('profile')
@click.argument('name', required=False, shell_complete=complete_profile_names)
def set_profile(name):
    """Set the active server profile (persists until changed).

    With no name, shows available profiles.
    """
    config = load_edr_config()

    if not name:
        for pname in sorted(config.profiles):
            p = config.profiles[pname]
            marker = "*" if pname == config.active else " "
            click.echo(f"{marker} {pname:20} {p.server_url}")
        return

    if not config.has_profile(name):
        click.echo(f"Error: no profile named '{name}'.", err=True)
        click.echo(f"Saved profiles: {', '.join(sorted(config.profiles)) or '(none)'}",
                   err=True)
        sys.exit(1)

    config.set_active(name)
    save_edr_config(config)
    click.echo(f"✓ Active profile: {name} ({config.profile(name).server_url})")


# =============================================================================
# Collections / Describe
# =============================================================================

@cli.command()
@click.argument('query', nargs=-1, required=False, shell_complete=complete_collection_names)
@click.option('--limit', '-n', default=20, help='Maximum results to show')
@click.option('--all', '-a', 'show_all', is_flag=True, help='Show all collections')
@click.option('--verbose', '-v', is_flag=True, help='Show query types, extent and parameters')
@click.option('--json', 'as_json', is_flag=True, help='Output as JSON')
@click.option('--count', is_flag=True, help='Just show count of matching collections')
@click.pass_context
def collections(ctx, query, limit, show_all, verbose, as_json, count):
    """List available collections, optionally fuzzy-filtered.

    Examples:
        edr collections
        edr collections hrrr
        edr collections guam -v
        edr collections --json
    """
    resolver, config = get_resolver(ctx)
    query_str = ' '.join(query) if query else None

    try:
        results = resolver.list_collections(query_str, limit=None if show_all else 1000)

        if not results:
            if as_json:
                click.echo(json.dumps({"collections": [], "count": 0}))
            else:
                click.echo(f"No collections found matching: {query_str}" if query_str
                           else "No collections found")
            return

        if not show_all and len(results) > limit:
            results = results[:limit]

        if count:
            out = json.dumps({"count": len(results)}) if as_json else f"{len(results)} collections"
            click.echo(out)
            return

        if as_json:
            data = [
                {
                    "id": c.id,
                    "title": c.title,
                    "score": round(score, 1) if query_str else None,
                    "query_types": c.data_queries,
                    "parameters": list(c.parameter_names.keys()),
                    "timesteps": c.n_timesteps,
                    "levels": c.n_levels,
                    "bbox": c.bbox,
                }
                for c, score in results
            ]
            click.echo(json.dumps({"collections": data, "count": len(data)}, indent=2))
            return

        header = f"Collections matching '{query_str}':" if query_str else \
                 f"Available collections ({len(results)} shown):"
        click.echo(f"{header}\n")

        for c, score in results:
            score_str = f"{score:5.1f}  " if query_str else "       "
            click.echo(f"  {score_str}{c.summary_line()}")

            if verbose:
                if c.title:
                    click.echo(f"         {c.title}")
                if c.data_queries:
                    click.echo(f"         queries: {', '.join(c.data_queries)}")
                if c.temporal_start:
                    click.echo(f"         time: {c.temporal_start} → {c.temporal_end}"
                               + (f" ({c.n_timesteps} steps)" if c.n_timesteps else ""))
                if c.vertical_values:
                    vals = c.vertical_values[:8]
                    more = f" (+{len(c.vertical_values) - 8} more)" if len(c.vertical_values) > 8 else ""
                    unit = f" {c.vertical_unit}" if c.vertical_unit else ""
                    click.echo(f"         z: {', '.join(vals)}{more}{unit}")
                if c.parameter_names:
                    names = list(c.parameter_names.keys())[:10]
                    more = f" (+{len(c.parameter_names) - 10} more)" if len(c.parameter_names) > 10 else ""
                    click.echo(f"         params: {', '.join(names)}{more}")
                click.echo("")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.argument('query', nargs=-1, required=True, shell_complete=complete_collection_names)
@click.pass_context
def describe(ctx, query):
    """Show detailed information about a collection.

    Examples:
        edr describe hrrr surface
        edr describe hrrr-isobaric
    """
    resolver, config = get_resolver(ctx)
    query_str = ' '.join(query)

    try:
        resolved = resolver.resolve(query_str)
        c = resolved.collection

        click.echo(f"Collection: {c.id}")
        if c.title:
            click.echo(f"Title: {c.title}")
        if c.description:
            desc = c.description
            if len(desc) > 300:
                desc = desc[:300] + "..."
            click.echo(f"Description: {desc}")

        click.echo(f"\nConfidence: {resolved.confidence:.0f}%")
        click.echo(f"Query types: {', '.join(c.data_queries)}")

        if c.bbox:
            click.echo(f"\nSpatial extent ({c.crs}):")
            click.echo(f"  {c.bbox[0]}, {c.bbox[1]} → {c.bbox[2]}, {c.bbox[3]}")

        if c.temporal_start:
            click.echo(f"\nTemporal extent:")
            click.echo(f"  {c.temporal_start} → {c.temporal_end}")
            if c.n_timesteps:
                click.echo(f"  {c.n_timesteps} steps")

        if c.vertical_values:
            unit = f" ({c.vertical_unit})" if c.vertical_unit else ""
            click.echo(f"\nVertical levels{unit}:")
            for i in range(0, len(c.vertical_values), 10):
                click.echo(f"  {', '.join(c.vertical_values[i:i+10])}")

        if c.parameter_names:
            click.echo(f"\nParameters ({len(c.parameter_names)}):")
            for pname, p in c.parameter_names.items():
                bits = [pname]
                if p.label and p.label != pname:
                    bits.append(f"({p.label})")
                if p.unit:
                    bits.append(f"[{p.unit}]")
                click.echo(f"  - {' '.join(bits)}")

    except AmbiguousCollectionError as e:
        click.echo(f"Error: {e}", err=True)
        if e.alternatives:
            click.echo("\nDid you mean one of these?", err=True)
            for alt in e.alternatives[:5]:
                click.echo(f"  - {alt}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# =============================================================================
# Query commands
# =============================================================================

def common_query_options(require_geometry=True):
    """Decorator adding the shared options for EDR query commands."""
    def decorator(f):
        f = click.argument('query', nargs=-1, required=True,
                           shell_complete=complete_collection_names)(f)
        f = click.option('--coords', '-c', default=None,
                         help='Coordinates: "lon,lat" or WKT (POINT(-105 40))')(f)
        f = click.option('--bbox', '-b', default=None,
                         help='Bounding box minx,miny,maxx,maxy (area/cube)')(f)
        f = click.option('--datetime', '-t', 'datetime_', default=None,
                         help='Time: latest, +6h, PT6H, ISO instant, or a/b interval')(f)
        f = click.option('--z', '-z', 'z_', default=None,
                         help='Vertical level: 850, 850mb, or low/high interval')(f)
        f = click.option('--param', '-p', default=None,
                         help='Parameter name(s), comma-separated')(f)
        f = click.option('--crs', default=None, help='Output CRS')(f)
        f = click.option('--format', '-f', 'format_', default=None,
                         help='Output format (f= parameter, e.g. CovJSON, GeoJSON)')(f)
        f = click.option('--within', default=None, help='Search distance (radius), e.g. 25km')(f)
        f = click.option('--width', default=None, help='Corridor width, e.g. 10km')(f)
        f = click.option('--instance', default=None, help='Instance (model run) id')(f)
        f = click.option('--output', '-o', default=None, help='Output filename')(f)
        f = click.option('--debug', '-d', is_flag=True, help='Show resolved URL without fetching')(f)
        return f
    return decorator


def run_edr_query(query_type, query, ctx=None, coords=None, bbox=None, datetime_=None, z_=None,
                  param=None, crs=None, format_=None, within=None, width=None,
                  instance=None, output=None, debug=False):
    """Shared implementation for all EDR query commands."""
    resolver, config = get_resolver(ctx)
    query_str = ' '.join(query)

    try:
        resolved = resolver.resolve(query_str)
        collection = resolved.collection

        # Validate the collection supports this query type
        if collection.data_queries and not collection.supports(query_type):
            click.echo(
                f"Error: collection '{collection.id}' does not support {query_type}. "
                f"Supported: {', '.join(collection.data_queries)}", err=True
            )
            sys.exit(1)

        # Resolve geometry
        warnings = list(resolved.warnings)
        geometry_params = {}
        if query_type == 'cube':
            # Cube queries use bbox=minx,miny,maxx,maxy (not a WKT polygon)
            if bbox:
                try:
                    floats = [float(v) for v in bbox.split(',')]
                    if len(floats) != 4 or floats[0] >= floats[2] or floats[1] >= floats[3]:
                        raise ValueError
                    geometry_params['bbox'] = bbox
                except ValueError:
                    click.echo(f"Error: invalid bbox '{bbox}' (use minx,miny,maxx,maxy)", err=True)
                    sys.exit(1)
            elif coords:
                wkt, cwarn = parse_coords(coords)
                warnings.extend(cwarn)
                if not wkt:
                    click.echo(f"Error: {cwarn[0] if cwarn else 'invalid coords'}", err=True)
                    sys.exit(1)
                geometry_params['coords'] = wkt
            else:
                # Default: whole collection bbox
                if collection.bbox:
                    geometry_params['bbox'] = ','.join(str(v) for v in collection.bbox)
                    warnings.append("bbox: using full collection extent")
                else:
                    click.echo("Error: cube requires --bbox (collection has no extent)", err=True)
                    sys.exit(1)
        elif bbox:
            wkt = bbox_to_wkt(bbox)
            if not wkt:
                click.echo(f"Error: invalid bbox '{bbox}' (use minx,miny,maxx,maxy)", err=True)
                sys.exit(1)
            geometry_params['coords'] = wkt
        elif coords:
            wkt, cwarn = parse_coords(coords)
            warnings.extend(cwarn)
            if not wkt:
                click.echo(f"Error: {cwarn[0] if cwarn else 'invalid coords'}", err=True)
                sys.exit(1)
            geometry_params['coords'] = wkt
        elif query_type in ('position', 'radius'):
            # Default: center of the collection extent
            center = collection.center()
            if center:
                geometry_params['coords'] = f"POINT({center[0]} {center[1]})"
                warnings.append(f"coords: using collection center {center[0]:.3f},{center[1]:.3f}")
        else:
            click.echo(f"Error: {query_type} requires --coords or --bbox", err=True)
            sys.exit(1)

        # Resolve datetime (option wins over query token)
        dt_expr = datetime_ or resolved.extracted.get('datetime')
        dt_value = None
        if dt_expr:
            dt_value, dwarn = resolver.resolve_datetime(dt_expr, collection)
            warnings.extend(dwarn)

        # Resolve z (option wins over query token)
        z_expr = z_ or resolved.extracted.get('z')
        z_value = None
        if z_expr:
            z_value, zwarn = resolver.resolve_z(z_expr, collection)
            warnings.extend(zwarn)

        # Parameter filter (explicit option wins over token match)
        param_value = param or resolved.parameter

        # Show what we resolved to
        click.echo(f"→ Collection: {collection.id}")
        if geometry_params:
            geom_str = ' '.join(f"{k}={v}" for k, v in geometry_params.items())
            click.echo(f"  Query: {query_type} {geom_str}")
        else:
            click.echo(f"  Query: {query_type}")
        if dt_value:
            click.echo(f"  datetime: {dt_value}")
        if z_value:
            click.echo(f"  z: {z_value}")
        if param_value:
            click.echo(f"  parameter-name: {param_value}")
        if instance:
            click.echo(f"  instance: {instance}")
        if resolved.confidence < 100:
            click.echo(f"  (confidence: {resolved.confidence:.0f}%)")
        for w in warnings:
            click.echo(f"  ⚠ {w}")

        # Build request params
        params = {
            **geometry_params,
            'datetime': dt_value,
            'z': z_value,
            'parameter-name': param_value,
            'crs': crs,
            'f': format_,
            'within': within,
            **(corridor_params(width) if width else {}),
        }
        params = {k: v for k, v in params.items() if v is not None and v != ''}

        path = f"/collections/{collection.id}"
        if instance:
            path += f"/instances/{instance}"
        path += f"/{query_type}"
        url = build_url(config.server_url, path, params)

        if debug:
            click.echo(f"\nURL:\n{url}")
            return

        # Fetch
        click.echo("Fetching...")
        client = EDRClient(config.server_url, api_key=config.api_key)
        body, content_type = client.query(
            collection.id, query_type, instance_id=instance, **params
        )

        # Summarize + save
        _report_response(body, content_type, collection.id, query_type, output)

    except AmbiguousCollectionError as e:
        click.echo(f"Error: {e}", err=True)
        if e.alternatives:
            click.echo("\nDid you mean one of these?", err=True)
            for alt in e.alternatives[:5]:
                click.echo(f"  - {alt}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


def _report_response(body: bytes, content_type: str, collection_id: str,
                     query_type: str, output: Optional[str]):
    """Summarize a response and save the raw payload."""
    is_json = 'json' in (content_type or '').lower() or body[:1] in (b'{', b'[')
    if not output:
        ext = 'json' if is_json else 'bin'
        output = f"{collection_id}_{query_type}.{ext}"

    Path(output).write_bytes(body)
    size_kb = len(body) / 1024
    click.echo(f"\n✓ Saved: {output} ({size_kb:.1f} KB)  [{content_type or 'unknown type'}]")

    if is_json:
        summary = summarize_coverage(body)
        if summary:
            click.echo("\nCoverage summary:")
            click.echo(f"  type: {summary.coverage_type or '-'}")
            if summary.axes:
                click.echo(f"  axes: {', '.join(f'{k}={v}' for k, v in summary.axes.items())}")
            for p in summary.parameters:
                bits = [p['name']]
                if p['unit']:
                    bits.append(f"[{p['unit']}]")
                if p.get('count'):
                    bits.append(f"{p['count']} values")
                if 'min' in p:
                    bits.append(f"min {p['min']:.3g} max {p['max']:.3g}")
                if p.get('nils'):
                    bits.append(f"({p['nils']} nulls)")
                click.echo(f"    {' '.join(bits)}")


@cli.command()
@common_query_options()
@click.pass_context
def position(ctx, query, coords, bbox, datetime_, z_, param, crs, format_, within, width,
             instance, output, debug):
    """Point query (coords=POINT).

    Examples:
        edr position hrrr surface -c -105,40
        edr position hrrr surface temp -c -105,40 -t 6h
        edr position hrrr isobaric -c POINT(-105 40) -z 850
    """
    run_edr_query('position', query, ctx=ctx, coords=coords, bbox=bbox, datetime_=datetime_,
                  z_=z_, param=param, crs=crs, format_=format_, within=within,
                  width=width, instance=instance, output=output, debug=debug)


@cli.command()
@common_query_options()
@click.pass_context
def radius(ctx, query, coords, bbox, datetime_, z_, param, crs, format_, within, width,
           instance, output, debug):
    """Radius query (coords=POINT + within).

    Examples:
        edr radius metar -c -105,40 --within 50km
    """
    if not within:
        click.echo("Error: radius query requires --within (e.g. --within 25km)", err=True)
        sys.exit(1)
    run_edr_query('radius', query, ctx=ctx, coords=coords, bbox=bbox, datetime_=datetime_,
                  z_=z_, param=param, crs=crs, format_=format_, within=within,
                  width=width, instance=instance, output=output, debug=debug)


@cli.command()
@common_query_options()
@click.pass_context
def area(ctx, query, coords, bbox, datetime_, z_, param, crs, format_, within, width,
         instance, output, debug):
    """Area query (coords=POLYGON or --bbox).

    Examples:
        edr area glm lightning --bbox -105,39,-104,41
        edr area hrrr surface -c "POLYGON((-105 39, -104 39, -104 41, -105 41, -105 39))"
    """
    run_edr_query('area', query, ctx=ctx, coords=coords, bbox=bbox, datetime_=datetime_,
                  z_=z_, param=param, crs=crs, format_=format_, within=within,
                  width=width, instance=instance, output=output, debug=debug)


@cli.command()
@common_query_options()
@click.pass_context
def cube(ctx, query, coords, bbox, datetime_, z_, param, crs, format_, within, width,
         instance, output, debug):
    """Cube query (bbox + vertical + time).

    Examples:
        edr cube hrrr isobaric --bbox -110,38,-100,44 -z 500/850 -t latest
    """
    run_edr_query('cube', query, ctx=ctx, coords=coords, bbox=bbox, datetime_=datetime_,
                  z_=z_, param=param, crs=crs, format_=format_, within=within,
                  width=width, instance=instance, output=output, debug=debug)


@cli.command()
@common_query_options()
@click.pass_context
def trajectory(ctx, query, coords, bbox, datetime_, z_, param, crs, format_, within, width,
               instance, output, debug):
    """Trajectory query (coords=LINESTRING).

    Examples:
        edr trajectory hrrr surface -c "-105,40;-104,41;-103,42"
        edr trajectory hrrr surface -c "LINESTRING(-105 40, -103 42)"
    """
    if not (coords or bbox):
        click.echo("Error: trajectory requires --coords (two or more points)", err=True)
        sys.exit(1)
    run_edr_query('trajectory', query, ctx=ctx, coords=coords, bbox=bbox, datetime_=datetime_,
                  z_=z_, param=param, crs=crs, format_=format_, within=within,
                  width=width, instance=instance, output=output, debug=debug)


@cli.command()
@common_query_options()
@click.pass_context
def corridor(ctx, query, coords, bbox, datetime_, z_, param, crs, format_, within, width,
             instance, output, debug):
    """Corridor query (LINESTRING + width).

    Examples:
        edr corridor hrrr surface -c "-105,40;-103,42" --width 25km
    """
    if not width:
        click.echo("Error: corridor requires --width (e.g. --width 10km)", err=True)
        sys.exit(1)
    if not (coords or bbox):
        click.echo("Error: corridor requires --coords (two or more points)", err=True)
        sys.exit(1)
    run_edr_query('corridor', query, ctx=ctx, coords=coords, bbox=bbox, datetime_=datetime_,
                  z_=z_, param=param, crs=crs, format_=format_, within=within,
                  width=width, instance=instance, output=output, debug=debug)


# =============================================================================
# Locations / Instances
# =============================================================================

@cli.command()
@click.argument('query', nargs=-1, required=True, shell_complete=complete_collection_names)
@click.option('--id', 'location_id', default=None, help='Fetch data at a specific location id')
@click.option('--datetime', '-t', 'datetime_', default=None, help='Time filter')
@click.option('--z', '-z', 'z_', default=None, help='Vertical level filter')
@click.option('--param', '-p', default=None, help='Parameter name(s)')
@click.option('--limit', '-n', default=20, help='Max locations to list')
@click.option('--output', '-o', default=None, help='Output filename (with --id)')
@click.option('--debug', '-d', is_flag=True, help='Show URL without fetching')
@click.pass_context
def locations(ctx, query, location_id, datetime_, z_, param, limit, output, debug):
    """List named locations for a collection (or fetch one with --id).

    Examples:
        edr locations populated
        edr locations populated --id denver_co
        edr locations metar -n 5
    """
    resolver, config = get_resolver(ctx)
    query_str = ' '.join(query)

    try:
        resolved = resolver.resolve(query_str)
        c = resolved.collection

        if location_id:
            path = f"/collections/{c.id}/locations/{location_id}"
            params = {'datetime': datetime_, 'z': z_, 'parameter-name': param}
            url = build_url(config.server_url, path, params)
            if debug:
                click.echo(url)
                return
            click.echo(f"Fetching location '{location_id}'...")
            client = EDRClient(config.server_url, api_key=config.api_key)
            body, ctype = client.get_location(
                c.id, location_id, datetime_=datetime_, z=z_, parameter_name=param
            )
            _report_response(body, ctype, f"{c.id}_{location_id}", 'location', output)
            return

        click.echo(f"→ Collection: {c.id}")
        click.echo("Fetching locations...")
        client = EDRClient(config.server_url, api_key=config.api_key)
        data = client.get_locations(c.id)

        features = data.get('features', []) if isinstance(data, dict) else []
        click.echo(f"\nLocations ({len(features)} total, showing {min(limit, len(features))}):")
        for feat in features[:limit]:
            props = feat.get('properties', {}) or {}
            fid = feat.get('id', '?')
            name = props.get('name') or props.get('title') or ''
            coords = (feat.get('geometry', {}) or {}).get('coordinates', [])
            loc = f"  ({coords[0]}, {coords[1]})" if isinstance(coords, list) and len(coords) >= 2 else ""
            click.echo(f"  {fid}  {name}{loc}")

    except AmbiguousCollectionError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.argument('query', nargs=-1, required=True, shell_complete=complete_collection_names)
@click.pass_context
def instances(ctx, query):
    """List model-run instances for a collection.

    Examples:
        edr instances hrrr surface
        edr instances gfswave
    """
    resolver, config = get_resolver(ctx)
    query_str = ' '.join(query)

    try:
        resolved = resolver.resolve(query_str)
        c = resolved.collection

        click.echo(f"→ Collection: {c.id}")
        click.echo("Fetching instances...")
        client = EDRClient(config.server_url, api_key=config.api_key)
        data = client.get_instances(c.id)

        instance_list = data.get('instances', []) if isinstance(data, dict) else data
        click.echo(f"\nInstances ({len(instance_list)}):")
        for inst in instance_list:
            iid = inst.get('id', '?')
            title = inst.get('title', '')
            click.echo(f"  {iid}  {title}")

    except AmbiguousCollectionError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# =============================================================================
# URL Builder
# =============================================================================

def _get_query_commands():
    """Map query types to their click commands (for `edr url`)."""
    return {
        'position': position,
        'radius': radius,
        'area': area,
        'cube': cube,
        'trajectory': trajectory,
        'corridor': corridor,
    }


@cli.command()
@click.argument('request_type', type=click.Choice(
    ['collections', 'position', 'radius', 'area', 'cube', 'trajectory', 'corridor',
     'locations', 'instances']))
@click.argument('query', nargs=-1, required=False, shell_complete=complete_collection_names)
@click.option('--coords', '-c', default=None, help='Coordinates (lon,lat or WKT)')
@click.option('--bbox', '-b', default=None, help='Bounding box minx,miny,maxx,maxy')
@click.option('--datetime', '-t', 'datetime_', default=None, help='Time filter')
@click.option('--z', '-z', 'z_', default=None, help='Vertical level')
@click.option('--param', '-p', default=None, help='Parameter name(s)')
@click.option('--within', default=None, help='Radius distance')
@click.option('--width', default=None, help='Corridor width')
@click.pass_context
def url(ctx, request_type, query, coords, bbox, datetime_, z_, param, within, width):
    """Build an EDR URL without fetching.

    Examples:
        edr url collections
        edr url position hrrr surface -c -105,40 -t 6h
        edr url radius metar -c -105,40 --within 25km
        edr url instances hrrr surface
    """
    config = getattr(ctx, 'obj', None) if ctx and ctx.obj else load_edr_config()

    if not config.is_initialized():
        click.echo("Error: EDR not initialized. Run 'edr init <url>' first.", err=True)
        sys.exit(1)

    if request_type == 'collections':
        click.echo(build_url(config.server_url, '/collections', {}))
        return

    if request_type in ('locations', 'instances'):
        if not query:
            click.echo(f"Error: {request_type} requires a collection query", err=True)
            sys.exit(1)
        resolver, _ = get_resolver(ctx)
        try:
            resolved = resolver.resolve(' '.join(query))
            click.echo(build_url(config.server_url,
                                 f"/collections/{resolved.collection.id}/{request_type}", {}))
        except AmbiguousCollectionError as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        return

    if not query:
        click.echo(f"Error: {request_type} requires a collection query", err=True)
        sys.exit(1)

    # Reuse the shared query path, stopping at the debug (URL print) step
    command = _get_query_commands()[request_type]
    with click.Context(command) as ctx:
        ctx.invoke(
            command,
            query=query, coords=coords, bbox=bbox, datetime_=datetime_, z_=z_,
            param=param, crs=None, format_=None, within=within, width=width,
            instance=None, output=None, debug=True,
        )


# =============================================================================
# Hammer
# =============================================================================

@cli.command()
@click.argument('query', nargs=-1, required=True, shell_complete=complete_collection_names)
@click.option('--workers', '-w', default=20, help='Number of parallel workers')
@click.option('--query-type', default='position',
              type=click.Choice(['position', 'area', 'cube', 'radius']),
              help='Query pattern to hammer with')
@click.option('--max-timesteps', default=0, help='Sample N timesteps per collection (0=all)')
@click.option('--max-params', default=0, help='Sample N parameters per collection (0=all)')
@click.option('--max-levels', default=0, help='Sample N vertical levels (0=all)')
@click.option('--points', default=1, help='Random points per collection')
@click.option('--limit', '-n', default=0, help='Cap total requests (0=unlimited)')
@click.option('--duration', '-D', default=None, metavar='T',
              help='Run for a wall-clock window (e.g. 30s, 5m), cycling requests')
@click.option('--rps', default=0.0, type=float, metavar='N',
              help='Pace to N requests/second (default: as fast as possible)')
@click.option('--warmup', default=0, type=int, metavar='N',
              help='Exclude the first N requests from latency stats')
@click.option('--report', 'report_path', default=None, metavar='PATH',
              help='Write machine-readable results JSON to PATH')
@click.option('--output', '-o', default='edr_hammer_output', help='Output directory')
@click.option('--no-save', is_flag=True, help='Do not save response bodies')
@click.option('--dry-run', is_flag=True, help='Show requests without executing')
@click.pass_context
def hammer(ctx, query, workers, query_type, max_timesteps, max_params, max_levels,
           points, limit, duration, rps, warmup, report_path, output, no_save, dry_run):
    """Hammer all collections matching query with a full dimension sweep.

    Enumerates matching collections × parameters × timesteps × z-levels
    × random points, fired in parallel.

    Examples:
        edr hammer hrrr
        edr hammer guam --dry-run
        edr hammer hrrr isobaric -w 10 --max-timesteps 8 --limit 100
        edr hammer hrrr --rps 50 --duration 30s
        edr hammer guam --warmup 5 --report run.json
    """
    resolver, config = get_resolver(ctx)
    query_str = ' '.join(query)

    try:
        duration_s = parse_duration(duration) if duration else 0.0
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    try:
        results = resolver.list_collections(query_str, limit=200)

        if not results:
            click.echo(f"No collections found matching: {query_str}")
            sys.exit(1)

        matching = [c for c, score in results if score > 50]

        if not matching:
            click.echo(f"No confident matches for: {query_str}")
            click.echo("Try a more specific query.")
            sys.exit(1)

        click.echo(f"→ Found {len(matching)} collections matching '{query_str}':")
        for c in matching[:10]:
            click.echo(f"  - {c.summary_line()}")
        if len(matching) > 10:
            click.echo(f"  ... and {len(matching) - 10} more")

        from .hammer import EDRHammer

        hammer_instance = EDRHammer(
            base_url=config.server_url,
            collections=matching,
            api_key=config.api_key,
            workers=workers,
            query_type=query_type,
            output=output,
            save=not no_save,
            dry_run=dry_run,
            max_timesteps=max_timesteps,
            max_params=max_params,
            max_levels=max_levels,
            points=points,
            limit=limit,
            duration=duration_s,
            rps=rps,
            warmup=warmup,
        )

        hammer_instance.generate()
        if dry_run or not duration_s:
            click.echo(f"\nGenerated {len(hammer_instance.requests)} requests "
                       f"(query: {query_type}, workers: {workers})")
        else:
            click.echo(f"\nRunning for {duration_s:.0f}s over "
                       f"{len(hammer_instance.requests)} generated requests "
                       f"(cycling, query: {query_type})")
        if rps:
            click.echo(f"Paced at {rps:g} req/s")
        if dry_run:
            click.echo("(dry run - no requests will be made)")

        stats = hammer_instance.run()

        if not dry_run:
            click.echo(f"\n{'=' * 50}")
            click.echo(stats.report())
            if report_path:
                Path(report_path).write_text(
                    json.dumps(stats.to_report_dict(), indent=2) + "\n"
                )
                click.echo(f"\n✓ Report saved: {report_path}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# =============================================================================
# Cache Management
# =============================================================================

@cli.group()
def cache():
    """Manage EDR collections cache."""
    pass


@cache.command('status')
def cache_status():
    """Show EDR cache status (all profiles)."""
    config = load_edr_config()

    if not config.profiles:
        click.echo("Not initialized. Run 'edr init <url>' first.")
        return

    for name in sorted(config.profiles):
        cache_path = edr_cache_file(config, name)
        marker = "*" if name == config.active else " "
        if cache_path.exists():
            ttl = config.profiles[name].cache_ttl or DEFAULT_CACHE_TTL
            age_seconds = time.time() - cache_path.stat().st_mtime
            valid = is_cache_valid(cache_path, ttl)
            click.echo(
                f"{marker} {name:20} {'valid' if valid else 'expired'}  "
                f"{int(age_seconds / 60)} min old  "
                f"{cache_path.stat().st_size / 1024:.1f} KB"
            )
        else:
            click.echo(f"{marker} {name:20} no cache")


@cache.command('clear')
def cache_clear():
    """Clear ALL EDR collections caches (all profiles)."""
    if clear_edr_cache():
        click.echo("✓ EDR caches cleared")
    else:
        click.echo("No EDR cache to clear")


@cache.command('refresh')
def cache_refresh():
    """Force refresh collections for the active profile from the server."""
    config = load_edr_config()

    if not config.is_initialized():
        click.echo("Error: EDR not initialized. Run 'edr init <url>' first.", err=True)
        sys.exit(1)

    click.echo(f"Fetching collections for profile '{config.active}' "
               f"from {config.server_url}...")
    try:
        refresh_active_profile(config)
        caps = parse_collections(get_collections_json(config), config.server_url)
        click.echo(f"✓ Cache refreshed ({len(caps.collections)} collections)")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


def main():
    """Main entry point"""
    cli()


if __name__ == '__main__':
    main()
