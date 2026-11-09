# WMS CLI Usage Guide

A comprehensive guide to using the WMS CLI tool for fetching weather map data.

## Table of Contents

- [Getting Started](#getting-started)
- [Shell Completion](#shell-completion)
- [Basic Workflow](#basic-workflow)
- [Searching for Layers](#searching-for-layers)
- [Fetching Maps](#fetching-maps)
- [Natural Dimension Syntax](#natural-dimension-syntax)
- [Batch Operations](#batch-operations)
- [Cache Management](#cache-management)
- [URL Building](#url-building)
- [Performance Testing](#performance-testing)
- [Troubleshooting](#troubleshooting)

---

## Getting Started

### Installation

Requires the [Rust toolchain](https://rustup.rs).

```bash
# Clone the repository
git clone <repo-url>
cd ogcli

# Quick setup (builds from source, installs to ~/.local/bin,
# enables shell completion)
./install.sh

# Or build manually
cd rust
cargo build --release
```

### Initialize the CLI

Before using most commands, you need to initialize with a WMS server:

```bash
# From a WMS server URL
wms init https://ogc.shyftwx.com/ogc/WMS

# From a local capabilities XML file
wms init ./capabilities.xml

# Force re-initialization (clears cache)
wms init https://ogc.shyftwx.com/ogc/WMS --force
```

The CLI will:
1. Fetch the GetCapabilities XML from the server
2. Parse available layers and their dimensions
3. Cache the capabilities locally (`~/.wms/capabilities_cache.xml`)
4. Save configuration to `~/.wms/config.json`

### Verify Setup

```bash
# Check current configuration
wms status

# Output:
# Source: https://ogc.shyftwx.com/ogc/WMS
# Type: server
# Server: https://ogc.shyftwx.com/ogc/WMS
# Layers: 46 queryable
# Service: Shyft Web Mapping Services
```

---

## Shell Completion

The CLI supports tab completion for commands, options, and layer names.

### Setup

**Bash** (add to `~/.bashrc`):
```bash
eval "$(_WMS_COMPLETE=bash_source wms)"
```

**Zsh** (add to `~/.zshrc`):
```bash
eval "$(_WMS_COMPLETE=zsh_source wms)"
```

**Fish** (run once):
```bash
_WMS_COMPLETE=fish_source wms > ~/.config/fish/completions/wms.fish
```

After adding, restart your shell or source the config file.

### For Better Performance

Generate a static completion script instead of evaluating on every shell start:

```bash
# Bash
_WMS_COMPLETE=bash_source wms > ~/.wms-complete.bash
echo 'source ~/.wms-complete.bash' >> ~/.bashrc

# Zsh
_WMS_COMPLETE=zsh_source wms > ~/.wms-complete.zsh
echo 'source ~/.wms-complete.zsh' >> ~/.zshrc
```

### What Completes

| Context | Completion |
|---------|------------|
| `wms <tab>` | All commands (map, tile, layers, etc.) |
| `wms cache <tab>` | Subcommands (status, clear, refresh) |
| `wms map --<tab>` | Options (--output, --debug, --width, etc.) |
| `wms map gfs<tab>` | Layer names starting with "GFS" |
| `wms groups G<tab>` | Group names starting with "G" |

### Requirements

- **Bash**: Version 4.4+ (macOS ships with 3.2, install newer via `brew install bash`)
- **Zsh**: Works with default macOS zsh
- **Fish**: Works with any recent version

---

## Basic Workflow

A typical workflow looks like this:

```bash
# 1. Initialize with server
wms init https://ogc.shyftwx.com/ogc/WMS

# 2. Explore available layers
wms layers
wms groups --counts

# 3. Search for specific layers
wms layers temp
wms layers gfs cloud

# 4. Get details about a layer
wms describe gfs temp

# 5. Fetch a map
wms map gfs surface temp f 6h

# 6. Build URL for integration
wms url map gfs temp 6h
```

---

## Searching for Layers

### List All Layers

```bash
# Show first 20 layers
wms layers

# Show all layers (no limit)
wms layers --all

# Just count total layers
wms layers --count
```

### Search with Fuzzy Matching

The CLI uses fuzzy matching so you don't need exact layer names:

```bash
# Search for temperature layers
wms layers temp

# Search for GFS layers
wms layers gfs

# Search for cloud layers
wms layers cloud

# Combine terms
wms layers gfs temp
```

### Verbose Layer Output

```bash
# Show dimensions and styles
wms layers temp -v

# Output:
#   100.0  GFS_Temperature_in_C [RUN, FORECAST, ELEVATION]
#         RUN: 2025-12-03T00:00:00Z, 2025-12-04T00:00:00Z, 2025-12-05T00:00:00Z
#         FORECAST: PT0S, PT1H, PT2H, PT3H, PT4H (+5 more)
#         ELEVATION: 1000, 975, 950, 925, 900 (+36 more)
#         Styles: default, 4_deg_contours, 3_deg_contours (+3 more)
```

### JSON Output

```bash
# Get machine-readable output
wms layers temp --json -n 5
```

### View Layer Groups

```bash
# List all groups with counts
wms groups --counts

# Output:
# GFS: 36 layers
# GIS: 8 layers
# GOES-East: 1 layers
# GOES-West: 1 layers

# Show group tree with sample layers
wms groups --tree
```

### Layer Details

```bash
# Get full details about a specific layer
wms describe gfs temp

# Output:
# Layer: GFS_Temperature_in_C
# Title: GFS Temperature °C
# Model: GFS
#
# Confidence: 100%
#
# Dimensions (3):
#   RUN:
#     Default: 2025-12-05T00:00:00Z
#     Values: 3 available
#   FORECAST:
#     Default: PT0S
#     Values: 10 available
#     Range: PT0S ... PT9H
#   ELEVATION:
#     Default: 1000
#     Values: 41 available
#
# Styles (6):
#   - default (default)
#   - 4_deg_contours
#   ...
```

---

## Fetching Maps

### Basic Map Fetch

```bash
# Fuzzy search finds the best matching layer
wms map gfs temp

# Output:
# → Resolved: GFS_Temperature_in_C
#   Style: default
#   FORECAST: PT0S
#   RUN: 2025-12-05T00:00:00Z
#   ELEVATION: 1000
# Fetching...
# ✓ Saved: GFS_Temperature_in_C_default_PT0S_2025-12-05T00-00-00Z.png (336.6 KB)
```

### With Dimensions

```bash
# Specify forecast time
wms map gfs temp 6h

# Specify model run
wms map gfs temp 12z

# Combine dimensions (any order)
wms map gfs temp 6h 12z

# Temperature at specific pressure level
wms map gfs temp 850
```

### Unit Preferences

For temperature layers, specify your preferred unit:

```bash
# Fahrenheit
wms map gfs temp f

# Celsius
wms map gfs temp c

# Kelvin
wms map gfs temp k
```

### Custom Output

```bash
# Specify output filename
wms map gfs temp -o my_map.png

# Custom dimensions
wms map gfs temp --width 1200 --height 900

# Custom bounding box (minx,miny,maxx,maxy)
wms map gfs temp --bbox -125,24,-66,50
```

### Debug Mode

Preview the URL without actually fetching:

```bash
wms map gfs temp 6h --debug

# Output:
# → Resolved: GFS_Temperature_in_C
#   Style: default
#   FORECAST: PT6H
#   RUN: 2025-12-05T00:00:00Z
#   ELEVATION: 1000
#
# URL:
# https://ogc.shyftwx.com/ogc/WMS?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&...
```

### Fetch Legend

```bash
# Get legend graphic for a layer
wms legend gfs temp

# Save to specific file
wms legend gfs temp -o legend.png
```

### Fetch Tile

```bash
# Random tile at default zoom
wms tile gfs temp

# Specific zoom level
wms tile gfs temp -z 5
```

---

## Natural Dimension Syntax

The CLI accepts natural shortcuts instead of ISO8601 strings:

### Forecast Time (FORECAST)

| Input | Resolves To | Description |
|-------|-------------|-------------|
| `0h` | PT0S | Analysis time |
| `6h` | PT6H | 6-hour forecast |
| `12h` | PT12H | 12-hour forecast |
| `24h` | PT24H | 24-hour forecast |
| `2d` | P2D | 2-day forecast |
| `3d` | P3D | 3-day forecast |

### Model Run Time (RUN)

| Input | Resolves To | Description |
|-------|-------------|-------------|
| `00z` | Latest 00Z run | Most recent 00:00 UTC run |
| `06z` | Latest 06Z run | Most recent 06:00 UTC run |
| `12z` | Latest 12Z run | Most recent 12:00 UTC run |
| `18z` | Latest 18Z run | Most recent 18:00 UTC run |
| `latest` | Most recent run | Any cycle |

### Elevation (ELEVATION)

| Input | Resolves To | Description |
|-------|-------------|-------------|
| `850` | 850 | 850 hPa level |
| `850mb` | 850 | 850 hPa level |
| `500` | 500 | 500 hPa level |
| `300` | 300 | 300 hPa level |

### Examples

```bash
# GFS Temperature, Fahrenheit, 12Z run, 6-hour forecast, 850mb
wms map gfs temp f 12z 6h 850

# Surface temperature
wms map gfs surface temp f

# Latest run with 24-hour forecast
wms map gfs cloud latest 24h
```

---

## Batch Operations

### List Matching Layers

```bash
# List all layers matching a pattern
wms batch gfs temp --list

# Limit results
wms batch gfs --list -n 10
```

### Dry Run

Preview what would be downloaded:

```bash
wms batch gfs temp --dry-run

# Output:
# → Found 5 layers matching 'gfs temp'
#
# Would download to: ./batch_output/
#   - GFS_Temperature_in_C
#   - GFS_Temperature_in_F
#   - GFS_Surface_Temperature_in_C
#   - GFS_Surface_Temperature_in_F
#   - GFS_Temp_Ten
```

### Download All Matching

```bash
# Download all matching layers
wms batch gfs temp -o ./gfs_temps/

# With parallel workers
wms batch gfs temp --workers 20

# Limit number of layers
wms batch gfs --limit 10
```

---

## Cache Management

The CLI caches GetCapabilities responses to avoid repeated server requests.

### Check Cache Status

```bash
wms cache status

# Output:
# Cache file: /Users/you/.wms/capabilities_cache.xml
# Cache age: 2 minutes
# Status: valid (8 min remaining)
# Size: 126.1 KB
```

### Clear Cache

```bash
wms cache clear
# ✓ Cache cleared
```

### Force Refresh

```bash
wms cache refresh
# Fetching capabilities from https://ogc.shyftwx.com/ogc/WMS...
# ✓ Cache refreshed (46 layers)
```

### Cache Behavior

- **TTL**: 10 minutes (sliding window)
- **Sliding window**: Each access resets the timer
- **Location**: `~/.wms/capabilities_cache.xml`
- **Force refresh**: Use `--force` with `init` or `cache refresh`

---

## URL Building

Build WMS URLs without fetching, useful for integration with other tools:

### GetCapabilities URL

```bash
wms url capabilities
# https://ogc.shyftwx.com/ogc/WMS?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetCapabilities
```

### GetMap URL

```bash
wms url map gfs temp f 6h
# https://ogc.shyftwx.com/ogc/WMS?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&LAYERS=GFS_Temperature_in_F&...
```

### GetGTile URL

```bash
wms url tile gfs temp -z 5
# https://ogc.shyftwx.com/ogc/WMS?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetGTile&LAYER=GFS_Temperature_in_C&...
```

### GetLegendGraphic URL

```bash
wms url legend gfs temp
# https://ogc.shyftwx.com/ogc/WMS?SERVICE=WMS&VERSION=1.3.0&REQUEST=GetLegendGraphic&LAYER=GFS_Temperature_in_C&...
```

---

## Performance Testing

The `hammer` command is for stress testing WMS servers:

```bash
# Hammer all matching layers
wms hammer gfs temp

# High concurrency
wms hammer gfs --workers 50

# Dry run to preview
wms hammer gfs temp --dry-run
```

---

## Troubleshooting

### "WMS not initialized"

```bash
# Error: WMS not initialized. Run 'wms init <source>' first.

# Solution:
wms init https://ogc.shyftwx.com/ogc/WMS
```

### "No layers found matching"

```bash
# Error: No layers found matching 'xyz'

# Solution: Try broader search terms
wms layers          # List all
wms layers temp     # Search for temperature
wms groups          # See group structure
```

### "Query too ambiguous"

```bash
# Error: No high-confidence match for 'temp' (best: 45%)

# Solution: Be more specific
wms map gfs temp    # Add model name
wms map gfs surface temp f  # Add more context
```

### Cache Issues

```bash
# If layers seem outdated
wms cache refresh

# If having persistent issues
wms init https://server/wms --force
```

### Network Errors

```bash
# If requests timeout, try debug mode first
wms map gfs temp --debug

# Copy the URL and test in browser or curl
curl -o test.png "URL_FROM_DEBUG_OUTPUT"
```

---

## Examples Cheatsheet

```bash
# Setup
wms init https://ogc.shyftwx.com/ogc/WMS

# Explore
wms layers                          # List all
wms layers temp -v                  # Search with details
wms groups --counts                 # See model groups
wms describe gfs temp               # Layer details

# Fetch maps
wms map gfs temp                    # Basic
wms map gfs temp f 6h               # With dimensions
wms map gfs temp --debug            # Preview URL
wms map gfs temp -o output.png      # Custom filename

# Batch
wms batch gfs temp --list           # List matches
wms batch gfs temp --dry-run        # Preview download
wms batch gfs temp -o ./output/     # Download all

# Cache
wms cache status                    # Check cache
wms cache clear                     # Clear cache
wms cache refresh                   # Force refresh

# URLs
wms url capabilities                # GetCapabilities
wms url map gfs temp 6h             # GetMap
wms url tile gfs temp -z 5          # GetGTile
wms url legend gfs temp             # GetLegendGraphic
```
