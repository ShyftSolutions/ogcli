#!/usr/bin/env python3
"""
WMS Hammer - Automated WMS Request Tool

A modern, clean automation script for batch WMS requests.
Uses the parser and client modules for robust operation.

Usage:
    As module: from src.hammer import WMSHammer
    Standalone: python -m src.hammer <url>
"""

import argparse
import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional
import fnmatch
from itertools import product
import time

from .client import WMSClient
from .parser import WMSCapabilities, Layer

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


class WMSHammer:
    """Automated WMS request tool"""

    def __init__(self, args, capabilities: Optional[WMSCapabilities] = None):
        """
        Initialize WMS Hammer.

        Args:
            args: Parsed command-line arguments
            capabilities: Optional pre-loaded capabilities (avoids duplicate fetch)
        """
        self.base_url = args.url
        self.output_dir = Path(args.output)
        self.glob_pattern = getattr(args, 'glob', '*')
        self.max_workers = args.workers
        self.dry_run = args.dry_run
        self.tile_mode = getattr(args, 'tile_mode', False)
        self.width = getattr(args, 'width', 800)
        self.height = getattr(args, 'height', 600)
        self.max_timesteps = getattr(args, 'max_timesteps', 10)
        self.latest_run_only = getattr(args, 'latest_run_only', False)
        self.save_images = not getattr(args, 'no_save', False)
        self.verbose = getattr(args, 'verbose', False)

        # Tile hammering options
        self.random_tiles = getattr(args, 'random_tiles', None)
        self.tile_zoom = getattr(args, 'tile_zoom', 0)
        self.tile_count = getattr(args, 'tile_count', 100)

        # Layer-specific targeting
        self.specific_layer = getattr(args, 'layer', None)
        self.list_layers = getattr(args, 'list_layers', False)

        # Pre-loaded capabilities (optional)
        self._capabilities = capabilities
        self.layers: List[Layer] = []

        # Statistics
        self.total_requests = 0
        self.successful_requests = 0
        self.failed_requests = 0
        self.start_time = None

        if self.verbose:
            logging.getLogger().setLevel(logging.DEBUG)

    def run(self):
        """Main execution"""
        self.start_time = time.time()

        logger.info(f"🔨 WMS Hammer starting...")
        logger.info(f"   URL: {self.base_url}")

        # Use pre-loaded layers if available (set by caller)
        if self.layers:
            filtered_layers = self.layers
            logger.info(f"✓ Using {len(filtered_layers)} pre-loaded layers")
        else:
            # Fetch and parse capabilities
            if self._capabilities:
                caps = self._capabilities
                logger.info(f"✓ Using pre-loaded capabilities")
            else:
                logger.info(f"\n📡 Fetching GetCapabilities...")
                try:
                    with WMSClient(self.base_url) as client:
                        caps = client.get_capabilities()
                except Exception as e:
                    logger.error(f"❌ Failed to fetch capabilities: {e}")
                    return 1

            logger.info(f"✓ Service: {caps.service_title}")
            logger.info(f"✓ Version: {caps.version}")

            queryable = caps.get_queryable_layers()
            logger.info(f"✓ Total queryable layers: {len(queryable)}")

            # Handle --list-layers mode
            if self.list_layers:
                self._list_layers(queryable)
                return 0

            # Handle specific layer targeting
            if self.specific_layer:
                layer = next((l for l in queryable if l.name == self.specific_layer), None)
                if not layer:
                    logger.error(f"❌ Layer '{self.specific_layer}' not found")
                    logger.info(f"Use --list-layers to see available layers")
                    return 1
                filtered_layers = [layer]
                logger.info(f"✓ Targeting specific layer: {layer.name}")
            else:
                # Filter layers by glob pattern
                filtered_layers = self._filter_layers(queryable)
                logger.info(f"✓ Layers matching '{self.glob_pattern}': {len(filtered_layers)}")

        if not filtered_layers:
            logger.warning("No layers match the pattern!")
            return 0

        # Determine mode
        if self.random_tiles:
            logger.info(f"   Mode: Random Tile Hammering")
            logger.info(f"   Zoom: {self.tile_zoom}")
            logger.info(f"   Tile Count: {self.tile_count}")
        else:
            logger.info(f"   Mode: {'GetGTile' if self.tile_mode else 'GetMap'}")
        logger.info(f"   Workers: {self.max_workers}")

        # Generate requests for all filtered layers
        all_requests = []
        if self.random_tiles:
            # Generate random tile requests
            for layer in filtered_layers:
                requests = self._generate_random_tile_requests(layer)
                all_requests.extend(requests)
        else:
            # Standard request generation
            for layer in filtered_layers:
                requests = self._generate_requests_for_layer(layer)
                all_requests.extend(requests)

        self.total_requests = len(all_requests)
        logger.info(f"\n📊 Total requests to make: {self.total_requests}")

        if self.dry_run:
            logger.info("\n🔍 DRY RUN - Not executing requests")
            self._show_sample_requests(all_requests)
            return 0

        # Execute requests
        if self.save_images:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"💾 Saving images to: {self.output_dir}")

        logger.info(f"\n🚀 Executing requests with {self.max_workers} workers...")
        self._execute_requests(all_requests)

        # Show summary
        self._show_summary()

        return 0 if self.failed_requests == 0 else 1

    def _list_layers(self, layers: List[Layer]) -> None:
        """List all queryable layers with details."""
        logger.info(f"\n📋 Available Layers ({len(layers)} total):")
        logger.info("=" * 80)

        for i, layer in enumerate(layers, 1):
            dims = ", ".join(layer.dimensions.keys()) if layer.dimensions else "none"
            logger.info(f"{i}. {layer.name}")
            logger.info(f"   Title: {layer.title}")
            logger.info(f"   Dimensions: {dims}")
            if i % 10 == 0 and i < len(layers):
                logger.info("")  # Blank line every 10 layers

        logger.info("=" * 80)

    def _generate_random_tile_requests(self, layer: Layer) -> List[Dict[str, Any]]:
        """Generate random tile requests for a layer."""
        import random

        requests = []

        # Get styles
        styles = ['default']
        if layer.styles:
            styles = [s.name for s in layer.styles]

        # Calculate max tile coordinates for zoom level
        max_tile = 2 ** self.tile_zoom - 1

        # Generate random tiles
        tiles_per_style = max(1, self.tile_count // len(styles))

        for style in styles:
            for _ in range(tiles_per_style):
                tilerow = random.randint(0, max_tile)
                tilecol = random.randint(0, max_tile)

                req = {
                    'layer': layer,
                    'style': style,
                    'tilezoom': self.tile_zoom,
                    'tilerow': tilerow,
                    'tilecol': tilecol,
                    'crs': 'EPSG:900913',  # Web Mercator for tiles
                    'dimensions': {}
                }
                requests.append(req)

        return requests

    def _filter_layers(self, layers: List[Layer]) -> List[Layer]:
        """Filter layers by glob pattern"""
        if self.glob_pattern == "*":
            return layers

        filtered = []
        for layer in layers:
            if fnmatch.fnmatch(layer.name.upper(), self.glob_pattern.upper()):
                filtered.append(layer)

        return filtered

    def _generate_requests_for_layer(self, layer: Layer) -> List[Dict[str, Any]]:
        """Generate all request combinations for a layer"""

        # Collect dimension values
        dim_values = {}

        # RUN dimension (forecast model run time)
        if 'RUN' in layer.dimensions:
            run_dim = layer.dimensions['RUN']
            if self.latest_run_only and run_dim.values:
                dim_values['DIM_RUN'] = [run_dim.values[-1]]  # Latest run only
            elif run_dim.values:
                dim_values['DIM_RUN'] = run_dim.values
            elif run_dim.default:
                dim_values['DIM_RUN'] = [run_dim.default]

        # FORECAST dimension (forecast period)
        if 'FORECAST' in layer.dimensions:
            forecast_dim = layer.dimensions['FORECAST']
            if forecast_dim.values:
                values = forecast_dim.values[:self.max_timesteps] if self.max_timesteps > 0 else forecast_dim.values
                dim_values['DIM_FORECAST'] = values
            elif forecast_dim.default:
                dim_values['DIM_FORECAST'] = [forecast_dim.default]

        # TIME dimension
        if 'TIME' in layer.dimensions:
            time_dim = layer.dimensions['TIME']
            if time_dim.values:
                values = time_dim.values[:self.max_timesteps] if self.max_timesteps > 0 else time_dim.values
                dim_values['TIME'] = values
            elif time_dim.default:
                dim_values['TIME'] = [time_dim.default]

        # ELEVATION dimension
        if 'ELEVATION' in layer.dimensions:
            elev_dim = layer.dimensions['ELEVATION']
            if elev_dim.values:
                dim_values['ELEVATION'] = elev_dim.values
            elif elev_dim.default:
                dim_values['ELEVATION'] = [elev_dim.default]

        # STATION dimension (for meteograms)
        if 'STATION' in layer.dimensions:
            # Generate a sample station location
            dim_values['DIM_STATION'] = ['CRS:84[-107.4229;43.6178]']

        # PLACE dimension (for cross-sections)
        if 'PLACE' in layer.dimensions:
            # Generate a sample linestring
            place = self._generate_sample_linestring()
            dim_values['DIM_PLACE'] = [place]

        # ISOTHERM dimension
        if 'ISOTHERM' in layer.dimensions:
            iso_dim = layer.dimensions['ISOTHERM']
            if iso_dim.values:
                dim_values['ISOTHERM'] = iso_dim.values
            elif iso_dim.default:
                dim_values['ISOTHERM'] = [iso_dim.default]

        # THRESHOLD dimension
        if 'THRESHOLD' in layer.dimensions:
            thresh_dim = layer.dimensions['THRESHOLD']
            if thresh_dim.values:
                dim_values['THRESHOLD'] = thresh_dim.values
            elif thresh_dim.default:
                dim_values['THRESHOLD'] = [thresh_dim.default]

        # Get styles
        styles = ['default']
        if layer.styles:
            styles = [s.name for s in layer.styles]

        # Get CRS
        crs = layer.crs_list[0] if layer.crs_list else 'CRS:84'
        if self.tile_mode:
            crs = 'EPSG:900913'  # Web Mercator for tiles

        # Get bounding box
        bbox = self._get_bbox(layer)

        # Generate all combinations
        requests = []

        if not dim_values:
            # No dimensions, just one request per style
            for style in styles:
                req = self._build_request(layer, style, crs, bbox, {})
                requests.append(req)
        else:
            # Generate combinations of all dimensions
            keys = list(dim_values.keys())
            values = list(dim_values.values())

            for style in styles:
                for combo in product(*values):
                    dims = dict(zip(keys, combo))
                    req = self._build_request(layer, style, crs, bbox, dims)
                    requests.append(req)

        return requests

    def _build_request(self, layer: Layer, style: str, crs: str, bbox: tuple, dimensions: Dict[str, str]) -> Dict[str, Any]:
        """Build a single request"""
        return {
            'layer': layer,
            'style': style,
            'crs': crs,
            'bbox': bbox,
            'dimensions': dimensions,
        }

    def _get_bbox(self, layer: Layer) -> tuple:
        """Get bounding box for layer"""
        # Try geographic bbox first
        if layer.geographic_bbox:
            bbox = layer.geographic_bbox
            return (bbox.minx, bbox.miny, bbox.maxx, bbox.maxy)

        # Try first available bounding box
        if layer.bounding_boxes:
            bbox = list(layer.bounding_boxes.values())[0]
            return (bbox.minx, bbox.miny, bbox.maxx, bbox.maxy)

        # Default global bbox
        return (-180, -90, 180, 90)

    def _generate_sample_linestring(self) -> str:
        """Generate a sample linestring for cross-sections"""
        import random
        random.seed(12345)

        coords = []
        for _ in range(3):
            lat = random.uniform(-90, 90)
            lon = random.uniform(-180, 180)
            coords.append(f"{lon:.3f};{lat:.3f}")

        # URL encode: CRS:84[lon;lat] CRS:84[lon;lat]...
        linestring = "%20".join([f"CRS:84%5B{coord}%5D" for coord in coords])
        return linestring

    def _execute_requests(self, requests: List[Dict[str, Any]]):
        """Execute requests with threading"""

        with WMSClient(self.base_url) as client:
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                futures = {}

                for req in requests:
                    future = executor.submit(self._execute_single_request, client, req)
                    futures[future] = req

                # Process completed requests
                for future in as_completed(futures):
                    req = futures[future]
                    try:
                        future.result()
                    except Exception as e:
                        logger.error(f"Request failed for {req['layer'].name}: {e}")
                        self.failed_requests += 1

    def _execute_single_request(self, client: WMSClient, req: Dict[str, Any]):
        """Execute a single request"""
        layer = req['layer']
        style = req['style']
        crs = req['crs']
        dims = req['dimensions']

        try:
            # Check if this is a tile request
            if 'tilezoom' in req:
                # GetGTile request (random tile or standard tile mode)
                image_data = client.get_gtile(
                    layer=layer.name,
                    tilezoom=req['tilezoom'],
                    tilerow=req['tilerow'],
                    tilecol=req['tilecol'],
                    crs=crs,
                    style=style,
                    **dims
                )
                tile_info = f"z{req['tilezoom']}_r{req['tilerow']}_c{req['tilecol']}"
                filename = self._generate_filename(layer.name, style, dims, tile_info)
            elif self.tile_mode:
                # Standard GetGTile request
                bbox = req['bbox']
                image_data = client.get_gtile(
                    layer=layer.name,
                    tilezoom=0,
                    tilerow=0,
                    tilecol=0,
                    crs=crs,
                    style=style,
                    **dims
                )
                filename = self._generate_filename(layer.name, style, dims)
            else:
                # GetMap request
                bbox = req['bbox']
                image_data = client.get_map(
                    layer=layer.name,
                    bbox=bbox,
                    width=self.width,
                    height=self.height,
                    crs=crs,
                    styles=style,
                    **dims
                )
                filename = self._generate_filename(layer.name, style, dims)

            self.successful_requests += 1

            if self.save_images:
                filepath = self.output_dir / filename
                filepath.parent.mkdir(parents=True, exist_ok=True)

                with open(filepath, 'wb') as f:
                    f.write(image_data)

                logger.debug(f"✓ Saved: {filename}")
            else:
                logger.debug(f"✓ Fetched: {layer.name} ({len(image_data)} bytes)")

        except Exception as e:
            logger.error(f"✗ Failed: {layer.name} - {e}")
            raise

    def _generate_filename(self, layer_name: str, style: str, dimensions: Dict[str, str], tile_info: str = None) -> str:
        """Generate filename for saved image"""
        parts = [layer_name]

        if style != 'default':
            parts.append(style)

        if tile_info:
            parts.append(tile_info)

        for key, value in dimensions.items():
            # Clean up dimension value for filename
            clean_value = value.replace(':', '-').replace('/', '-').replace(' ', '_')
            parts.append(f"{key}_{clean_value}")

        filename = "_".join(parts) + ".png"
        return filename

    def _show_sample_requests(self, requests: List[Dict[str, Any]]):
        """Show sample requests for dry run"""
        logger.info("\n📋 Sample requests (first 5):")

        for i, req in enumerate(requests[:5], 1):
            layer = req['layer']
            dims_str = ', '.join(f"{k}={v}" for k, v in req['dimensions'].items())
            logger.info(f"\n{i}. {layer.name}")
            logger.info(f"   Style: {req['style']}")
            logger.info(f"   CRS: {req['crs']}")
            logger.info(f"   Dimensions: {dims_str or 'none'}")

        if len(requests) > 5:
            logger.info(f"\n   ... and {len(requests) - 5} more requests")

    def _show_summary(self):
        """Show execution summary"""
        elapsed = time.time() - self.start_time

        logger.info("\n" + "=" * 80)
        logger.info("📊 SUMMARY")
        logger.info("=" * 80)
        logger.info(f"Total requests:     {self.total_requests}")
        logger.info(f"Successful:         {self.successful_requests} ({self.successful_requests/self.total_requests*100:.1f}%)")
        logger.info(f"Failed:             {self.failed_requests} ({self.failed_requests/self.total_requests*100:.1f}%)")
        logger.info(f"Time elapsed:       {elapsed:.2f}s")
        logger.info(f"Requests/sec:       {self.total_requests/elapsed:.2f}")

        if self.save_images:
            total_size = sum(f.stat().st_size for f in self.output_dir.rglob('*.png'))
            logger.info(f"Total size:         {total_size / 1024 / 1024:.2f} MB")
            logger.info(f"Output directory:   {self.output_dir}")

        logger.info("=" * 80)


def main():
    parser = argparse.ArgumentParser(
        description='WMS Hammer - Automated WMS request tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # List all available layers
  python hammer.py http://your-wms-server.com/wms --list-layers

  # Target a specific layer
  python hammer.py http://your-wms-server.com/wms -l "Temperature_Layer"

  # Fetch layers matching a pattern
  python hammer.py http://your-wms-server.com/wms -g "Temperature*"

  # Random tile hammering (100 random tiles at zoom 4)
  python hammer.py http://your-wms-server.com/wms -l "Temperature_Layer" --random-tiles --tile-zoom 4 --tile-count 100

  # Dry run to preview requests
  python hammer.py http://your-wms-server.com/wms -g "Cloud*" --dry-run

  # Full automation: limit timesteps, latest run, high concurrency
  python hammer.py http://your-wms-server.com/wms -g "Temperature*" -t 5 --latest-run -w 50
        """
    )

    parser.add_argument('url', help='WMS service base URL')

    parser.add_argument('-g', '--glob', default='*',
                        help='Glob pattern for layer names (default: *)')

    parser.add_argument('-o', '--output', default='wms_output',
                        help='Output directory for images (default: wms_output)')

    parser.add_argument('-w', '--workers', type=int, default=20,
                        help='Number of concurrent workers (default: 20)')

    parser.add_argument('-t', '--max-timesteps', type=int, default=10,
                        help='Max timesteps per layer (0 = all, default: 10)')

    parser.add_argument('--width', type=int, default=800,
                        help='Image width for GetMap (default: 800)')

    parser.add_argument('--height', type=int, default=600,
                        help='Image height for GetMap (default: 600)')

    parser.add_argument('--tile-mode', action='store_true',
                        help='Use GetGTile instead of GetMap')

    parser.add_argument('--latest-run', dest='latest_run_only', action='store_true',
                        help='Only use latest model run (for RUN dimension)')

    parser.add_argument('--dry-run', action='store_true',
                        help='Show what would be requested without executing')

    parser.add_argument('--no-save', action='store_true',
                        help='Do not save images (just fetch and discard)')

    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Verbose output (debug logging)')

    # Layer targeting options
    parser.add_argument('-l', '--layer', type=str,
                        help='Target a specific layer by name')

    parser.add_argument('--list-layers', action='store_true',
                        help='List all available layers and exit')

    # Random tile hammering options
    parser.add_argument('--random-tiles', action='store_true',
                        help='Generate random tile requests instead of standard requests')

    parser.add_argument('--tile-zoom', type=int, default=0,
                        help='Zoom level for random tiles (default: 0)')

    parser.add_argument('--tile-count', type=int, default=100,
                        help='Number of random tiles to request per layer (default: 100)')

    args = parser.parse_args()

    # Run the hammer
    hammer = WMSHammer(args)
    return hammer.run()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logger.info("\n\n⚠️  Interrupted by user")
        sys.exit(130)
    except Exception as e:
        logger.error(f"\n❌ Fatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
