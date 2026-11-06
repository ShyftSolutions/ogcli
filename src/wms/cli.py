#!/usr/bin/env python3
"""
WMS CLI - Command-line interface for WMS queries with fuzzy search.
"""

import sys
import json
import random
import time
from pathlib import Path

import click

from .config import (
    load_config, save_config, get_capabilities_xml,
    init_from_file, init_from_server, refresh_active_profile,
    validate_profile_name, caps_cache_file, WMSConfig,
    clear_cache, is_cache_valid, CAPS_CACHE_FILE, DEFAULT_CACHE_TTL,
    fetch_capabilities_xml, ensure_config_dir
)
from .parser import parse_capabilities, Layer
from .client import WMSClient
from .resolver import SmartQueryResolver, AmbiguousQueryError
from .fuzzy import FuzzySearchEngine
from .completions import complete_layer_names, complete_group_names
from ..common.display import display_image


def complete_profile_names(ctx, param, incomplete):
    """Complete saved WMS profile names."""
    try:
        from .config import load_config as _load
        config = _load()
        return [
            click.shell_completion.CompletionItem(p, help="active" if p == config.active else "")
            for p in sorted(config.profile_names())
            if p.startswith(incomplete)
        ]
    except Exception:
        return []


def get_resolver(ctx=None):
    """Get resolver from current config (honoring a --profile override)"""
    config = getattr(ctx, "obj", None) if ctx else None
    if config is None:
        config = load_config()
    if not config.is_initialized():
        click.echo("Error: WMS not initialized. Run 'wms init <source>' first.", err=True)
        click.echo("\nExamples:", err=True)
        click.echo("  wms init ./capabilities.xml", err=True)
        click.echo("  wms init http://my-server/wms", err=True)
        sys.exit(1)

    xml_content = get_capabilities_xml(config)
    capabilities = parse_capabilities(xml_content)
    return SmartQueryResolver(capabilities), config


def sanitize_filename(name: str) -> str:
    """Sanitize string for use in filename"""
    # Remove/replace problematic characters
    name = name.replace(':', '-').replace('/', '-').replace('\\', '-')
    name = name.replace(' ', '_').replace('?', '').replace('&', '_')
    return name


@click.group(invoke_without_command=True)
@click.version_option(package_name='wms-cli', version='0.1.0 (python)')
@click.option('--profile', '-p', 'profile', is_flag=False, flag_value='', default=None,
              metavar='[NAME]',
              help='Use a saved server profile for this invocation. With no value, '
                   'interactively pick a profile and make it active.',
              shell_complete=complete_profile_names)
@click.pass_context
def cli(ctx, profile):
    """WMS query tool with fuzzy search.

    Run without arguments to open interactive TUI.
    """
    if profile == '':
        # Bare --profile: interactive picker, sets the active profile
        config = load_config()
        names = sorted(config.profile_names())
        profiles = [(n, config.profiles[n].source_path) for n in names]
        if not profiles:
            click.echo("No saved profiles. Save one with: wms init <source> --save NAME")
            sys.exit(1)
        try:
            from ..common.profiles import pick_profile_interactive
            picked = pick_profile_interactive(profiles, config.active, 'wms')
        except (RuntimeError, EOFError) as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        config.set_active(picked)
        save_config(config)
        click.echo(f"\u2713 Active profile: {picked}")
        ctx.obj = config
        if ctx.invoked_subcommand is None:
            return
    elif profile:
        config = load_config()
        if not config.has_profile(profile):
            click.echo(f"Error: no profile named '{profile}'.", err=True)
            click.echo(f"Saved profiles: {', '.join(config.profile_names()) or '(none)'}",
                       err=True)
            sys.exit(1)
        config.active = profile
        ctx.obj = config
    if ctx.invoked_subcommand is None:
        # Launch TUI
        try:
            from .tui import run_tui
            run_tui()
        except Exception as e:
            click.echo(f"Error launching TUI: {e}", err=True)
            click.echo("\nUse subcommands instead:")
            click.echo("  wms init <source>")
            click.echo("  wms map <query>")
            click.echo("  wms tile <query>")
            click.echo("  wms legend <query>")
            click.echo("  wms layers [query]")
            click.echo("  wms hammer <query>")


@cli.command()
@click.argument('source', required=False)
@click.option('--server', '-s', help='Server URL for requests (when source is local file)')
@click.option('--save', 'save_as', metavar='NAME', default=None,
              help='Save as a named profile (and make it active)')
@click.option('--force', '-f', is_flag=True, help='Force re-initialization (clears cache)')
@click.pass_context
def init(ctx, source, server, save_as, force):
    """Initialize WMS context from file or server.

    SOURCE can be a local file path or HTTP URL. With no SOURCE,
    refreshes the active profile's capabilities.

    Examples:
        wms init http://localhost:8008/ogc/AFW_WMS
        wms init http://server/wms --save prod
        wms init ./capabilities.xml --server http://prod-server/wms
        wms init                   # refresh active profile
    """
    try:
        # Clear cache if force flag is set
        if force:
            if clear_cache():
                click.echo("✓ Cache cleared")

        existing = getattr(ctx, 'obj', None) if ctx and ctx.obj else None

        if not source:
            config = refresh_active_profile(existing)
            click.echo(f"✓ Refreshed profile '{config.active}'")
        elif source.startswith('http://') or source.startswith('https://'):
            # Initialize from server
            click.echo(f"Fetching capabilities from {source}...")
            config = init_from_server(source, config=existing, save_as=save_as)
            click.echo(f"✓ Initialized profile '{config.active}' from server")
        else:
            # Initialize from local file
            click.echo(f"Loading capabilities from {source}...")
            config = init_from_file(source, server, config=existing, save_as=save_as)
            click.echo(f"✓ Initialized profile '{config.active}' from local file")

        # Show summary
        xml_content = get_capabilities_xml(config)
        caps = parse_capabilities(xml_content)

        click.echo(f"  Service: {caps.service_title}")
        click.echo(f"  Layers: {len(caps.get_queryable_layers())} queryable")

        if config.server_url:
            click.echo(f"  Server: {config.server_url}")

    except FileNotFoundError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.pass_context
def status(ctx):
    """Show current WMS configuration."""
    config = getattr(ctx, 'obj', None) if ctx and ctx.obj else load_config()

    if not config.is_initialized():
        click.echo("Not initialized. Run 'wms init <source>' first.")
        return

    click.echo(f"Profile: {config.active}" +
               (f" ({len(config.profiles)} saved)" if len(config.profiles) > 1 else ""))
    click.echo(f"Source: {config.source_path}")
    click.echo(f"Type: {config.source_type}")

    if config.server_url:
        click.echo(f"Server: {config.server_url}")

    # Load and show layer count
    try:
        xml_content = get_capabilities_xml(config)
        caps = parse_capabilities(xml_content)
        click.echo(f"Layers: {len(caps.get_queryable_layers())} queryable")
        click.echo(f"Service: {caps.service_title}")
    except Exception as e:
        click.echo(f"Error loading capabilities: {e}", err=True)


# =============================================================================
# Profiles
# =============================================================================

@cli.command('profiles')
def profiles_cmd():
    """List saved server profiles (* = active).

    Examples:
        wms profiles
        wms init http://my-server/wms --save prod
        wms set profile prod
    """
    config = load_config()
    if not config.profiles:
        click.echo("No saved profiles. Save one with: wms init <source> --save NAME")
        return

    for name in sorted(config.profiles):
        p = config.profiles[name]
        marker = "*" if name == config.active else " "
        click.echo(f"{marker} {name:20} {p.source_path}")


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
    config = load_config()

    if not name:
        for pname in sorted(config.profiles):
            p = config.profiles[pname]
            marker = "*" if pname == config.active else " "
            click.echo(f"{marker} {pname:20} {p.source_path}")
        return

    if not config.has_profile(name):
        click.echo(f"Error: no profile named '{name}'.", err=True)
        click.echo(f"Saved profiles: {', '.join(sorted(config.profiles)) or '(none)'}",
                   err=True)
        sys.exit(1)

    config.set_active(name)
    save_config(config)
    click.echo(f"✓ Active profile: {name} ({config.profile(name).source_path})")


def build_url(base_url: str, params: dict) -> str:
    """Build a URL with query parameters"""
    from urllib.parse import urlencode
    return f"{base_url}?{urlencode(params)}"


@cli.command()
@click.argument('query', nargs=-1, required=True, shell_complete=complete_layer_names)
@click.option('--output', '-o', help='Output filename')
@click.option('--display', '-D', is_flag=True, help='Display image in terminal using Unicode')
@click.option('--width', '-w', default=800, help='Image width')
@click.option('--height', '-h', default=600, help='Image height')
@click.option('--bbox', help='Bounding box: minx,miny,maxx,maxy')
@click.option('--crs', help='Coordinate reference system (default: from layer)')
@click.option('--format', 'img_format', default='image/png', help='Image format')
@click.option('--style', help='Style name (default: layer default)')
@click.option('--debug', '-d', is_flag=True, help='Show resolved URL without fetching')
@click.pass_context
def map(ctx, query, output, display, width, height, bbox, crs, img_format, style, debug):
    """Fetch a map image (GetMap).

    QUERY is fuzzy-matched to find the best layer.

    Examples:
        wms map gfs temp
        wms map galwem cloud 850
        wms map gfs temp f 12z 6h
        wms map gfs temp --bbox -125,24,-66,50
        wms map gfs temp --debug
    """
    resolver, config = get_resolver(ctx)
    query_str = ' '.join(query)

    try:
        # Resolve the query
        resolved = resolver.resolve(query_str)

        # Override with explicit options
        use_style = style if style else resolved.style
        use_crs = crs if crs else resolved.crs
        use_bbox = tuple(map(float, bbox.split(','))) if bbox else resolved.bbox

        # Show what we resolved to
        click.echo(f"→ Resolved: {resolved.layer.name}")
        click.echo(f"  Style: {use_style}")

        for dim_name, value in resolved.dimensions.items():
            click.echo(f"  {dim_name}: {value}")

        # Show confidence (on a 0-100 scale)
        if resolved.confidence < 100:
            click.echo(f"  (confidence: {resolved.confidence:.0f}%)")

        # Show warnings if any
        for warning in resolved.warnings:
            click.echo(f"  ⚠ {warning}")

        # Build dimension kwargs
        dim_kwargs = {}
        for dim_name, value in resolved.dimensions.items():
            param_name = resolved.get_dimension_param_name(dim_name)
            dim_kwargs[param_name] = value

        # Build the URL params
        bbox_str = ','.join(str(x) for x in use_bbox)
        params = {
            'SERVICE': 'WMS',
            'VERSION': '1.3.0',
            'REQUEST': 'GetMap',
            'LAYERS': resolved.layer.name,
            'BBOX': bbox_str,
            'WIDTH': width,
            'HEIGHT': height,
            'CRS': use_crs,
            'FORMAT': img_format,
            'STYLES': use_style,
            **{k.upper(): v for k, v in dim_kwargs.items()}
        }

        if debug:
            # Just show the URL
            url = build_url(config.server_url, params)
            click.echo(f"\nURL:\n{url}")
            return

        # Fetch the map
        click.echo("Fetching...")

        client = WMSClient(config.server_url)
        image_data = client.get_map(
            layer=resolved.layer.name,
            bbox=use_bbox,
            width=width,
            height=height,
            crs=use_crs,
            styles=use_style,
            format=img_format,
            **dim_kwargs
        )

        # Display in terminal if --display flag is used
        if display:
            click.echo("\nDisplaying image:")
            try:
                import tempfile
                import os
                with tempfile.NamedTemporaryFile(mode='wb', suffix='.png', delete=False) as tmp:
                    tmp.write(image_data)
                    tmp_path = tmp.name
                display_image(tmp_path)
                os.unlink(tmp_path)
            except Exception as e:
                click.echo(f"Warning: Could not display image: {e}", err=True)

        # Save to file only if -o is specified, or if not displaying
        if output or not display:
            if not output:
                parts = [resolved.layer.name, use_style]
                for dim_name, value in resolved.dimensions.items():
                    parts.append(sanitize_filename(value))
                output = '_'.join(parts[:4]) + '.png'
            with open(output, 'wb') as f:
                f.write(image_data)
            size_kb = len(image_data) / 1024
            click.echo(f"\n✓ Saved: {output} ({size_kb:.1f} KB)")

    except AmbiguousQueryError as e:
        click.echo(f"Error: {e}", err=True)
        if e.alternatives:
            click.echo("\nDid you mean one of these?", err=True)
            for alt in e.alternatives[:5]:
                click.echo(f"  - {alt}", err=True)
        sys.exit(1)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.argument('query', nargs=-1, required=True, shell_complete=complete_layer_names)
@click.option('--output', '-o', help='Output filename')
@click.option('--display', '-D', is_flag=True, help='Display image in terminal using Unicode')
@click.option('--zoom', '-z', default=3, help='Tile zoom level')
@click.option('--debug', '-d', is_flag=True, help='Show resolved URL without fetching')
@click.pass_context
def tile(ctx, query, output, display, zoom, debug):
    """Fetch a random tile (GetGTile).

    QUERY is fuzzy-matched to find the best layer.

    Examples:
        wms tile galwem cloud
        wms tile gfs temp
        wms tile hrrr --zoom 5
        wms tile gfs temp --debug
    """
    resolver, config = get_resolver(ctx)
    query_str = ' '.join(query)

    try:
        # Resolve the query
        resolved = resolver.resolve(query_str)

        # Generate random tile coordinates for the zoom level
        max_tiles = 2 ** zoom
        tilerow = random.randint(0, max_tiles - 1)
        tilecol = random.randint(0, max_tiles - 1)

        # Show what we resolved to
        click.echo(f"→ Resolved: {resolved.layer.name}")
        click.echo(f"  Tile: z={zoom}, row={tilerow}, col={tilecol}")

        for dim_name, value in resolved.dimensions.items():
            click.echo(f"  {dim_name}: {value}")

        # Show confidence and warnings
        if resolved.confidence < 100:
            click.echo(f"  (confidence: {resolved.confidence:.0f}%)")
        for warning in resolved.warnings:
            click.echo(f"  ⚠ {warning}")

        # Build dimension kwargs
        dim_kwargs = {}
        for dim_name, value in resolved.dimensions.items():
            param_name = resolved.get_dimension_param_name(dim_name)
            dim_kwargs[param_name] = value

        # Build URL params
        params = {
            'SERVICE': 'WMS',
            'VERSION': '1.3.0',
            'REQUEST': 'GetGTile',
            'LAYER': resolved.layer.name,
            'TILEZOOM': zoom,
            'TILEROW': tilerow,
            'TILECOL': tilecol,
            'CRS': 'EPSG:900913',
            'FORMAT': 'image/png; mode=8bit',
            'STYLE': resolved.style,
            'TRANSPARENT': 'TRUE',
            **{k.upper(): v for k, v in dim_kwargs.items()}
        }

        if debug:
            url = build_url(config.server_url, params)
            click.echo(f"\nURL:\n{url}")
            return

        # Fetch the tile
        click.echo("Fetching...")

        client = WMSClient(config.server_url)
        image_data = client.get_gtile(
            layer=resolved.layer.name,
            tilezoom=zoom,
            tilerow=tilerow,
            tilecol=tilecol,
            style=resolved.style,
            **dim_kwargs
        )

        # Display in terminal if --display flag is used
        if display:
            click.echo("\nDisplaying image:")
            try:
                import tempfile
                import os
                with tempfile.NamedTemporaryFile(mode='wb', suffix='.png', delete=False) as tmp:
                    tmp.write(image_data)
                    tmp_path = tmp.name
                display_image(tmp_path)
                os.unlink(tmp_path)
            except Exception as e:
                click.echo(f"Warning: Could not display image: {e}", err=True)

        # Save to file only if -o is specified, or if not displaying
        if output or not display:
            if not output:
                output = f"tile_{resolved.layer.name}_z{zoom}.png"
            with open(output, 'wb') as f:
                f.write(image_data)
            size_kb = len(image_data) / 1024
            click.echo(f"\n✓ Saved: {output} ({size_kb:.1f} KB)")

    except AmbiguousQueryError as e:
        click.echo(f"Error: {e}", err=True)
        if e.alternatives:
            click.echo("\nDid you mean one of these?", err=True)
            for alt in e.alternatives[:5]:
                click.echo(f"  - {alt}", err=True)
        sys.exit(1)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.argument('query', nargs=-1, required=True, shell_complete=complete_layer_names)
@click.option('--output', '-o', help='Output filename')
@click.option('--display', '-D', is_flag=True, help='Display image in terminal using Unicode')
@click.option('--debug', '-d', is_flag=True, help='Show resolved URL without fetching')
@click.pass_context
def legend(ctx, query, output, display, debug):
    """Fetch legend graphic for a layer.

    Examples:
        wms legend gfs temp
        wms legend galwem cloud
        wms legend hrrr --debug
    """
    resolver, config = get_resolver(ctx)
    query_str = ' '.join(query)

    try:
        # Resolve the query
        resolved = resolver.resolve(query_str)

        click.echo(f"→ Resolved: {resolved.layer.name}")
        click.echo(f"  Style: {resolved.style}")

        # Show confidence and warnings
        if resolved.confidence < 100:
            click.echo(f"  (confidence: {resolved.confidence:.0f}%)")
        for warning in resolved.warnings:
            click.echo(f"  ⚠ {warning}")

        # Build URL params
        params = {
            'SERVICE': 'WMS',
            'VERSION': '1.3.0',
            'REQUEST': 'GetLegendGraphic',
            'LAYER': resolved.layer.name,
            'STYLE': resolved.style,
            'FORMAT': 'image/png'
        }

        if debug:
            url = build_url(config.server_url, params)
            click.echo(f"\nURL:\n{url}")
            return

        # Fetch the legend
        click.echo("Fetching...")

        client = WMSClient(config.server_url)
        image_data = client.get_legend_graphic(
            layer=resolved.layer.name,
            style=resolved.style
        )

        # Display in terminal if --display flag is used
        if display:
            click.echo("\nDisplaying image:")
            try:
                import tempfile
                import os
                with tempfile.NamedTemporaryFile(mode='wb', suffix='.png', delete=False) as tmp:
                    tmp.write(image_data)
                    tmp_path = tmp.name
                display_image(tmp_path)
                os.unlink(tmp_path)
            except Exception as e:
                click.echo(f"Warning: Could not display image: {e}", err=True)

        # Save to file only if -o is specified, or if not displaying
        if output or not display:
            if not output:
                output = f"legend_{resolved.layer.name}.png"
            with open(output, 'wb') as f:
                f.write(image_data)
            size_kb = len(image_data) / 1024
            click.echo(f"\n✓ Saved: {output} ({size_kb:.1f} KB)")

    except AmbiguousQueryError as e:
        click.echo(f"Error: {e}", err=True)
        if e.alternatives:
            click.echo("\nDid you mean one of these?", err=True)
            for alt in e.alternatives[:5]:
                click.echo(f"  - {alt}", err=True)
        sys.exit(1)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.argument('query', nargs=-1, required=False, shell_complete=complete_layer_names)
@click.option('--limit', '-n', default=20, help='Maximum results to show')
@click.option('--all', '-a', 'show_all', is_flag=True, help='Show all layers (no limit)')
@click.option('--verbose', '-v', is_flag=True, help='Show dimensions and styles')
@click.option('--json', 'as_json', is_flag=True, help='Output as JSON')
@click.option('--count', is_flag=True, help='Just show count of matching layers')
@click.pass_context
def layers(ctx, query, limit, show_all, verbose, as_json, count):
    """List available layers, optionally filtered.

    Examples:
        wms layers              # List all layers
        wms layers gfs          # Search for GFS layers
        wms layers cloud -v     # Verbose with dimensions
        wms layers --json       # JSON output
        wms layers gfs --count  # Just count
    """
    resolver, config = get_resolver(ctx)
    query_str = ' '.join(query) if query else None

    try:
        actual_limit = None if show_all else limit
        results = resolver.list_layers(query_str, limit=actual_limit or 1000)

        if not results:
            if as_json:
                click.echo(json.dumps({"layers": [], "count": 0}))
            elif query_str:
                click.echo(f"No layers found matching: {query_str}")
            else:
                click.echo("No queryable layers found")
            return

        # Apply limit if not showing all
        if not show_all and len(results) > limit:
            results = results[:limit]

        # Count only mode
        if count:
            if as_json:
                click.echo(json.dumps({"count": len(results)}))
            else:
                click.echo(f"{len(results)} layers")
            return

        # JSON output mode
        if as_json:
            layer_data = []
            for result in results:
                layer = result.layer
                entry = {
                    "name": layer.name,
                    "title": layer.title,
                    "score": result.score if query_str else None,
                    "dimensions": list(layer.dimensions.keys()),
                    "styles": [s.name for s in layer.styles] if layer.styles else []
                }
                layer_data.append(entry)
            click.echo(json.dumps({"layers": layer_data, "count": len(layer_data)}, indent=2))
            return

        # Regular output
        if query_str:
            click.echo(f"Layers matching '{query_str}':\n")
        else:
            click.echo(f"Available layers ({len(results)} shown):\n")

        for result in results:
            layer = result.layer
            dims = list(layer.dimensions.keys())
            dim_str = f" [{', '.join(dims)}]" if dims else ""

            if query_str:
                click.echo(f"  {result.score:5.1f}  {layer.name}{dim_str}")
            else:
                click.echo(f"  {layer.name}{dim_str}")

            # Verbose mode: show dimensions and styles
            if verbose:
                if layer.dimensions:
                    for dim_name, dim in layer.dimensions.items():
                        values = dim.values[:5] if len(dim.values) > 5 else dim.values
                        more = f" (+{len(dim.values) - 5} more)" if len(dim.values) > 5 else ""
                        click.echo(f"        {dim_name}: {', '.join(values)}{more}")
                if layer.styles:
                    style_names = [s.name for s in layer.styles[:3]]
                    more = f" (+{len(layer.styles) - 3} more)" if len(layer.styles) > 3 else ""
                    click.echo(f"        Styles: {', '.join(style_names)}{more}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.argument('query', nargs=-1, required=True, shell_complete=complete_layer_names)
@click.option('--workers', '-w', default=20, help='Number of parallel workers')
@click.option('--dry-run', is_flag=True, help='Show requests without executing')
@click.option('--output', '-o', default='wms_output', help='Output directory')
@click.pass_context
def hammer(ctx, query, workers, dry_run, output):
    """Hammer all layers matching query.

    Generates all dimension combinations and fetches them in parallel.

    Examples:
        wms hammer gfs
        wms hammer galwem cloud
        wms hammer hrrr --workers 10
    """
    resolver, config = get_resolver(ctx)
    query_str = ' '.join(query)

    try:
        # Find all matching layers
        results = resolver.list_layers(query_str, limit=100)

        if not results:
            click.echo(f"No layers found matching: {query_str}")
            sys.exit(1)

        # Filter to good matches (score > 50)
        matching_layers = [r.layer for r in results if r.score > 50]

        if not matching_layers:
            click.echo(f"No confident matches for: {query_str}")
            click.echo("Try a more specific query.")
            sys.exit(1)

        click.echo(f"→ Found {len(matching_layers)} layers matching '{query_str}':")
        for layer in matching_layers[:10]:
            click.echo(f"  - {layer.name}")
        if len(matching_layers) > 10:
            click.echo(f"  ... and {len(matching_layers) - 10} more")

        # Import and run hammer
        from .hammer import WMSHammer
        import argparse

        # Build args for hammer
        args = argparse.Namespace(
            url=config.server_url,
            glob=None,  # We'll pass layer names directly
            layer=None,
            list_layers=False,
            workers=workers,
            output=output,
            max_timesteps=0,
            latest_run=False,
            tile_mode=False,
            dry_run=dry_run,
            verbose=False,
            random_tiles=False,
            no_save=False,
            single_forecast=False,
            num_random_tiles=100,
            timeout=30
        )

        # Get capabilities
        xml_content = get_capabilities_xml(config)
        caps = parse_capabilities(xml_content)

        # Create hammer and run for each layer
        hammer_instance = WMSHammer(args, caps)

        # Override layer filtering to use our matches
        hammer_instance.layers = matching_layers

        click.echo(f"\nHammering with {workers} workers...")
        if dry_run:
            click.echo("(dry run - no requests will be made)")

        hammer_instance.run()

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# =============================================================================
# Cache Management Commands
# =============================================================================

@cli.group()
def cache():
    """Manage capabilities cache."""
    pass


@cache.command('status')
def cache_status():
    """Show cache status."""
    config = load_config()

    if not config.is_initialized():
        click.echo("Not initialized. Run 'wms init <source>' first.")
        return

    if config.source_type == 'file':
        click.echo("Source type: file (no cache used)")
        click.echo(f"File: {config.source_path}")
        return

    click.echo(f"Cache file: {CAPS_CACHE_FILE}")

    if CAPS_CACHE_FILE.exists():
        mtime = CAPS_CACHE_FILE.stat().st_mtime
        age_seconds = time.time() - mtime
        age_minutes = int(age_seconds / 60)
        ttl = config.cache_ttl if config.cache_ttl > 0 else DEFAULT_CACHE_TTL
        remaining = max(0, ttl - age_seconds)

        click.echo(f"Cache age: {age_minutes} minutes")

        if is_cache_valid(CAPS_CACHE_FILE, ttl):
            click.echo(f"Status: valid ({int(remaining / 60)} min remaining)")
        else:
            click.echo("Status: expired")

        size_kb = CAPS_CACHE_FILE.stat().st_size / 1024
        click.echo(f"Size: {size_kb:.1f} KB")
    else:
        click.echo("Status: no cache file")


@cache.command('clear')
def cache_clear():
    """Clear the capabilities cache."""
    if clear_cache():
        click.echo("✓ Cache cleared")
    else:
        click.echo("No cache to clear")


@cache.command('refresh')
def cache_refresh():
    """Force refresh capabilities from server."""
    config = load_config()

    if not config.is_initialized():
        click.echo("Not initialized. Run 'wms init <source>' first.", err=True)
        sys.exit(1)

    if config.source_type == 'file':
        click.echo("Source is a file - no cache to refresh")
        return

    click.echo(f"Fetching capabilities from {config.source_path}...")
    try:
        xml_content = fetch_capabilities_xml(config.source_path)
        ensure_config_dir()
        with open(CAPS_CACHE_FILE, 'w', encoding='utf-8') as f:
            f.write(xml_content)

        caps = parse_capabilities(xml_content)
        click.echo(f"✓ Cache refreshed ({len(caps.get_queryable_layers())} layers)")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# =============================================================================
# Describe Command
# =============================================================================

@cli.command()
@click.argument('query', nargs=-1, required=True, shell_complete=complete_layer_names)
@click.pass_context
def describe(ctx, query):
    """Show detailed information about a layer.

    Examples:
        wms describe galwem cloud base
        wms describe gfs temp f
        wms describe GALWEM_CloudBase
    """
    resolver, config = get_resolver(ctx)
    query_str = ' '.join(query)

    try:
        resolved = resolver.resolve(query_str)
        layer = resolved.layer

        click.echo(f"Layer: {layer.name}")
        if layer.title:
            click.echo(f"Title: {layer.title}")

        # Try to extract model from layer name
        if '_' in layer.name:
            model = layer.name.split('_')[0]
            click.echo(f"Model: {model}")

        click.echo(f"\nConfidence: {resolved.confidence:.0f}%")

        # Dimensions
        if layer.dimensions:
            click.echo(f"\nDimensions ({len(layer.dimensions)}):")
            for dim_name, dim in layer.dimensions.items():
                click.echo(f"  {dim_name}:")
                click.echo(f"    Default: {dim.default}")
                click.echo(f"    Values: {len(dim.values)} available")
                if dim.values:
                    preview = dim.values[:5]
                    if len(dim.values) > 5:
                        click.echo(f"    Range: {dim.values[0]} ... {dim.values[-1]}")
                    else:
                        click.echo(f"    Options: {', '.join(preview)}")

        # Styles
        if layer.styles:
            click.echo(f"\nStyles ({len(layer.styles)}):")
            for style in layer.styles:
                default = " (default)" if style.name == resolved.style else ""
                click.echo(f"  - {style.name}{default}")
                if style.title and style.title != style.name:
                    click.echo(f"      {style.title}")

        # Bounding box
        if layer.geographic_bbox:
            bb = layer.geographic_bbox
            click.echo(f"\nGeographic Extent:")
            click.echo(f"  {bb.minx}, {bb.miny} → {bb.maxx}, {bb.maxy}")
        elif layer.bounding_boxes:
            # Get first bounding box from dict
            crs, bb = next(iter(layer.bounding_boxes.items()))
            click.echo(f"\nBounding Box ({crs}):")
            click.echo(f"  {bb.minx}, {bb.miny} → {bb.maxx}, {bb.maxy}")

        # CRS
        if layer.crs_list:
            click.echo(f"\nCRS Options: {', '.join(layer.crs_list[:5])}")
            if len(layer.crs_list) > 5:
                click.echo(f"  (+{len(layer.crs_list) - 5} more)")

    except AmbiguousQueryError as e:
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
# URL Builder Command
# =============================================================================

@cli.command()
@click.argument('request_type', type=click.Choice(['map', 'tile', 'legend', 'capabilities']))
@click.argument('query', nargs=-1, required=False, shell_complete=complete_layer_names)
@click.option('--width', '-w', default=800, help='Image width (for map)')
@click.option('--height', '-h', default=600, help='Image height (for map)')
@click.option('--zoom', '-z', default=3, help='Tile zoom level (for tile)')
@click.pass_context
def url(ctx, request_type, query, width, height, zoom):
    """Build a WMS URL without fetching.

    Examples:
        wms url capabilities
        wms url map gfs temp f 6h
        wms url tile galwem cloud -z 5
        wms url legend hrrr precip
    """
    config = getattr(ctx, 'obj', None) if ctx and ctx.obj else load_config()

    if not config.is_initialized():
        click.echo("Error: WMS not initialized. Run 'wms init <source>' first.", err=True)
        sys.exit(1)

    if request_type == 'capabilities':
        params = {
            'SERVICE': 'WMS',
            'VERSION': '1.3.0',
            'REQUEST': 'GetCapabilities'
        }
        click.echo(build_url(config.server_url, params))
        return

    # Other request types need a query
    if not query:
        click.echo(f"Error: {request_type} requires a query", err=True)
        sys.exit(1)

    resolver, _ = get_resolver(ctx)
    query_str = ' '.join(query)

    try:
        resolved = resolver.resolve(query_str)

        # Build dimension kwargs
        dim_kwargs = {}
        for dim_name, value in resolved.dimensions.items():
            param_name = resolved.get_dimension_param_name(dim_name)
            dim_kwargs[param_name.upper()] = value

        if request_type == 'map':
            bbox_str = ','.join(str(x) for x in resolved.bbox)
            params = {
                'SERVICE': 'WMS',
                'VERSION': '1.3.0',
                'REQUEST': 'GetMap',
                'LAYERS': resolved.layer.name,
                'BBOX': bbox_str,
                'WIDTH': width,
                'HEIGHT': height,
                'CRS': resolved.crs,
                'FORMAT': 'image/png',
                'STYLES': resolved.style,
                **dim_kwargs
            }

        elif request_type == 'tile':
            max_tiles = 2 ** zoom
            tilerow = random.randint(0, max_tiles - 1)
            tilecol = random.randint(0, max_tiles - 1)
            params = {
                'SERVICE': 'WMS',
                'VERSION': '1.3.0',
                'REQUEST': 'GetGTile',
                'LAYER': resolved.layer.name,
                'TILEZOOM': zoom,
                'TILEROW': tilerow,
                'TILECOL': tilecol,
                'CRS': 'EPSG:900913',
                'FORMAT': 'image/png',
                'STYLE': resolved.style,
                **dim_kwargs
            }

        elif request_type == 'legend':
            params = {
                'SERVICE': 'WMS',
                'VERSION': '1.3.0',
                'REQUEST': 'GetLegendGraphic',
                'LAYER': resolved.layer.name,
                'STYLE': resolved.style,
                'FORMAT': 'image/png'
            }

        click.echo(build_url(config.server_url, params))

    except AmbiguousQueryError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# =============================================================================
# Groups Command
# =============================================================================

def build_layer_tree(capabilities):
    """Build a tree structure of layer groups."""
    tree = {}

    def add_to_tree(layer, path=[]):
        """Recursively add layers to tree."""
        if layer.name:
            # This is a queryable layer, add it under current path
            current = tree
            for part in path:
                if part not in current:
                    current[part] = {'_layers': [], '_children': {}}
                current = current[part]['_children']
            if '_layers' not in tree:
                tree['_layers'] = []
            # Add to parent's layer list
            parent = tree
            for part in path:
                parent = parent.get(part, {}).get('_children', {})

        # Process children (sublayers)
        if hasattr(layer, 'layers') and layer.layers:
            for child in layer.layers:
                child_path = path + [layer.title or 'Unnamed']
                add_to_tree(child, child_path)

    # Start from root layer
    if capabilities.root_layer:
        add_to_tree(capabilities.root_layer)

    return tree


@cli.command()
@click.argument('pattern', required=False, shell_complete=complete_group_names)
@click.option('--tree', 'as_tree', is_flag=True, help='Show as hierarchical tree')
@click.option('--counts', is_flag=True, help='Show layer counts per group')
@click.pass_context
def groups(ctx, pattern, as_tree, counts):
    """List layer groups/hierarchy.

    Examples:
        wms groups
        wms groups galwem
        wms groups --tree
        wms groups --counts
    """
    config = load_config()

    if not config.is_initialized():
        click.echo("Not initialized. Run 'wms init <source>' first.", err=True)
        sys.exit(1)

    xml_content = get_capabilities_xml(config)
    caps = parse_capabilities(xml_content)

    # Get all queryable layers and extract their groups from names
    queryable = caps.get_queryable_layers()

    # Extract group prefixes from layer names (e.g., GALWEM_CloudBase -> GALWEM)
    groups_dict = {}
    for layer in queryable:
        if '_' in layer.name:
            parts = layer.name.split('_')
            group = parts[0]
            if pattern and pattern.lower() not in group.lower():
                continue
            if group not in groups_dict:
                groups_dict[group] = []
            groups_dict[group].append(layer.name)
        else:
            group = 'Other'
            if group not in groups_dict:
                groups_dict[group] = []
            groups_dict[group].append(layer.name)

    if not groups_dict:
        click.echo("No groups found" + (f" matching '{pattern}'" if pattern else ""))
        return

    # Sort groups
    sorted_groups = sorted(groups_dict.keys())

    if as_tree:
        for group in sorted_groups:
            layer_count = len(groups_dict[group])
            if counts:
                click.echo(f"{group} ({layer_count} layers)")
            else:
                click.echo(group)

            # Show some sample layers
            for layer_name in groups_dict[group][:5]:
                click.echo(f"  └── {layer_name}")
            if len(groups_dict[group]) > 5:
                click.echo(f"  └── ... (+{len(groups_dict[group]) - 5} more)")
    else:
        for group in sorted_groups:
            if counts:
                click.echo(f"{group}: {len(groups_dict[group])} layers")
            else:
                click.echo(group)


# =============================================================================
# Batch Command
# =============================================================================

@cli.command()
@click.argument('query', nargs=-1, required=True, shell_complete=complete_layer_names)
@click.option('--output', '-o', default='./batch_output', help='Output directory')
@click.option('--workers', '-w', default=10, help='Parallel download workers')
@click.option('--dry-run', is_flag=True, help='Show what would be downloaded')
@click.option('--list', 'list_only', is_flag=True, help='Just list matching layers')
@click.option('--limit', '-n', type=int, help='Max layers to process')
@click.pass_context
def batch(ctx, query, output, workers, dry_run, list_only, limit):
    """Fetch all layers matching a group pattern.

    Unlike hammer (performance testing), batch is for organized downloading.

    Examples:
        wms batch galwem cloud 6h
        wms batch gfs temp --dry-run
        wms batch "GALWEM*" --list
        wms batch hrrr -o ./hrrr_data/
    """
    resolver, config = get_resolver(ctx)
    query_str = ' '.join(query)

    try:
        # Find matching layers
        results = resolver.list_layers(query_str, limit=limit or 100)

        if not results:
            click.echo(f"No layers found matching: {query_str}")
            sys.exit(1)

        # Filter to reasonable confidence
        matching = [r for r in results if r.score > 50]

        if not matching:
            click.echo(f"No confident matches for: {query_str}")
            sys.exit(1)

        if limit:
            matching = matching[:limit]

        # List only mode
        if list_only:
            click.echo(f"Layers matching '{query_str}' ({len(matching)}):\n")
            for result in matching:
                click.echo(f"  {result.score:5.1f}  {result.layer.name}")
            return

        click.echo(f"→ Found {len(matching)} layers matching '{query_str}'")

        # Dry run mode
        if dry_run:
            click.echo(f"\nWould download to: {output}/")
            for result in matching[:10]:
                click.echo(f"  - {result.layer.name}")
            if len(matching) > 10:
                click.echo(f"  ... and {len(matching) - 10} more")
            return

        # Create output directory
        output_path = Path(output)
        output_path.mkdir(parents=True, exist_ok=True)

        click.echo(f"Downloading to: {output}/")
        click.echo(f"Workers: {workers}")

        # Download each layer
        from concurrent.futures import ThreadPoolExecutor, as_completed

        client = WMSClient(config.server_url)
        success = 0
        failed = 0

        def download_layer(result):
            try:
                resolved = resolver.resolve(result.layer.name)

                # Build dimension kwargs
                dim_kwargs = {}
                for dim_name, value in resolved.dimensions.items():
                    param_name = resolved.get_dimension_param_name(dim_name)
                    dim_kwargs[param_name] = value

                image_data = client.get_map(
                    layer=resolved.layer.name,
                    bbox=resolved.bbox,
                    width=800,
                    height=600,
                    crs=resolved.crs,
                    styles=resolved.style,
                    **dim_kwargs
                )

                # Save file
                filename = f"{resolved.layer.name}.png"
                filepath = output_path / filename
                with open(filepath, 'wb') as f:
                    f.write(image_data)

                return True, result.layer.name
            except Exception as e:
                return False, f"{result.layer.name}: {e}"

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(download_layer, r): r for r in matching}

            with click.progressbar(length=len(matching), label='Downloading') as bar:
                for future in as_completed(futures):
                    ok, msg = future.result()
                    if ok:
                        success += 1
                    else:
                        failed += 1
                        click.echo(f"\n  ✗ {msg}", err=True)
                    bar.update(1)

        click.echo(f"\n✓ Downloaded {success} layers")
        if failed:
            click.echo(f"✗ Failed: {failed}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


def main():
    """Main entry point"""
    cli()


if __name__ == '__main__':
    main()
