# OGC CLI (WMS + EDR)

Command-line tools for OGC web services with fuzzy search and natural dimension syntax:

- **`wms`** — Web Map Service: layers, maps, tiles, legends
- **`edr`** — Environmental Data Retrieval: collections, position/area/cube/trajectory/corridor/radius queries

## Features

- **Fuzzy search** - Find layers/collections with natural queries like `gfs temp f` or `hrrr surface temp`
- **Natural dimensions** - Use `6h`, `12z`, `850mb` instead of ISO8601
- **Smart defaults** - Automatically uses latest RUN, default FORECAST
- **Shell completion** - Tab completion for commands, options, and layer/collection names
- **Cache management** - 10-minute sliding TTL cache for capabilities/collections
- **Batch downloads** - Download multiple layers matching a pattern
- **Performance testing** - Hammer WMS/EDR servers with concurrent requests

## Installation

Requires the [Rust toolchain](https://rustup.rs).

```bash
# Quick setup (builds from source, installs to ~/.local/bin,
# sets up PATH + shell completion)
./install.sh

# Or build manually
cd rust
cargo build --release
# Binaries: rust/target/release/{wms,edr,ogcli-mcp}
```

## Shell Completion

Enable tab completion for commands, options, and layer names:

**Bash** (add to `~/.bashrc`):
```bash
eval "$(_WMS_COMPLETE=bash_source wms)"
eval "$(_EDR_COMPLETE=bash_source edr)"
```

**Zsh** (add to `~/.zshrc`):
```bash
eval "$(_WMS_COMPLETE=zsh_source wms)"
eval "$(_EDR_COMPLETE=zsh_source edr)"
```

**Fish** (run once):
```bash
_WMS_COMPLETE=fish_source wms > ~/.config/fish/completions/wms.fish
_EDR_COMPLETE=fish_source edr > ~/.config/fish/completions/edr.fish
```

After adding, restart your shell or run `source ~/.bashrc` (or `~/.zshrc`).

### What Completes

- `wms <tab>` / `edr <tab>` - Shows all commands
- `wms map --<tab>` / `edr position --<tab>` - Shows all options
- `wms map gfs<tab>` - Shows matching layer names
- `edr position hrrr<tab>` - Shows matching collection ids

## Quick Start

```bash
# Initialize with a WMS server
wms init https://ogc.shyftwx.com/ogc/WMS

# Search for layers
wms layers temp

# Fetch a map with natural syntax
wms map gfs temp f 6h

# Get layer details
wms describe gfs surface temp

# Show current configuration
wms status
```

## Commands

| Command | Description |
|---------|-------------|
| `wms init <source>` | Initialize from server URL or XML file |
| `wms map <query>` | Fetch a map image (GetMap) |
| `wms tile <query>` | Fetch a tile (GetGTile) |
| `wms legend <query>` | Fetch legend graphic |
| `wms layers [query]` | List/search layers |
| `wms describe <query>` | Show layer details |
| `wms groups` | List layer groups |
| `wms batch <query>` | Download all matching layers |
| `wms hammer <query>` | Performance testing |
| `wms url <type> <query>` | Build URL without fetching |
| `wms cache status` | Show cache status |
| `wms cache clear` | Clear capabilities cache |
| `wms cache refresh` | Force refresh from server |
| `wms status` | Show configuration |

## Natural Dimension Syntax

Instead of ISO8601 strings, use shortcuts:

| Input | Dimension | Meaning |
|-------|-----------|---------|
| `6h`, `12h`, `24h` | FORECAST | Forecast time |
| `00z`, `12z`, `latest` | RUN | Model run time |
| `850`, `500mb` | ELEVATION | Pressure level |
| `f`, `c`, `k` | Unit | Temperature unit preference |

## Configuration

Stored in `~/.wms/`:
- `config.json` - WMS server URL, settings
- `edr_config.json` - EDR server URL, settings
- `capabilities_cache.xml` - Cached WMS capabilities (10-min sliding TTL)
- `edr_collections_cache.json` - Cached EDR collections (10-min sliding TTL)

WMS and EDR state are independent: `edr init` does not affect `wms init`.

## Dependencies

- [Rust toolchain](https://rustup.rs) (to build from source)

---

# EDR CLI

Query OGC API - Environmental Data Retrieval servers: browse **collections** (instead of WMS layers), run query patterns, and load-test with `edr hammer`.

## Quick Start

```bash
# Initialize with an EDR server (first one becomes the default)
edr init https://folkweather.com/edr

# Search collections
edr collections hrrr

# Fetch data with natural syntax
edr position hrrr surface temp -c -105,40
```

## Server Profiles

Save multiple servers and switch between them. The active profile persists until you change it.

```bash
# Save named profiles
edr init https://folkweather.com/edr --save folkweather
edr init http://localhost:8006/ogc/edr --save local

wms init https://ogc.shyftwx.com/ogc/WMS --save prod

# List (* = active)
edr profiles
wms profiles

# Switch the active profile (persists)
edr set profile local
wms set profile prod

# Interactive picker: lists profiles, pick by number or name
edr --profile
wms --profile

# One-off against another profile without switching
edr --profile folkweather collections hrrr
wms -p prod map gfs temp

# Refresh the active profile's collections/capabilities
edr init
wms init
```

Notes: profile names allow letters/numbers/`-`/`_`. Each profile has its own cache, so switching servers never serves stale data. Existing single-server configs migrate automatically to a `default` profile.

## Commands

| Command | Description |
|---------|-------------|
| `edr init [url]` | Initialize from an EDR server (no URL = refresh active profile) |
| `edr profiles` | List saved server profiles |
| `edr set profile <name>` | Switch the active profile |
| `edr status` | Show configuration |
| `edr collections [query]` | List/search collections |
| `edr describe <query>` | Show collection details |
| `edr position <query>` | Point query (`-c lon,lat` or WKT) |
| `edr radius <query>` | Radius query (`--within 25km`) |
| `edr area <query>` | Polygon query (`--bbox` or `-c POLYGON(...)`) |
| `edr cube <query>` | BBox + vertical + time (`--bbox ... -z 500/850`) |
| `edr trajectory <query>` | Path query (`-c "lon1,lat1;lon2,lat2"`) |
| `edr corridor <query>` | Path + width (`--width 10km`) |
| `edr locations <query>` | List named locations (`--id` to fetch one) |
| `edr instances <query>` | List model-run instances |
| `edr hammer <query>` | Full dimension sweep load test |
| `edr url <type> <query>` | Build URL without fetching |
| `edr cache status/clear/refresh` | Manage collections cache |

## Natural Dimension Syntax

| Input | Meaning |
|-------|---------|
| `latest` | Last time in the collection's extent |
| `6h`, `+6h`, `PT6H`, `2d` | Offset from the extent start (forecast-hour semantics) |
| `850`, `850mb`, `500hpa` | Vertical level (snapped to nearest advertised) |
| `500/850` | Vertical interval |
| `-c -105,40` | Point (lon,lat) |
| `-c "lon1,lat1;lon2,lat2"` | Line (trajectory/corridor) |
| `-c POINT(-105 40)` | Raw WKT passthrough |

Leftover query words map to parameter names: `edr position nbm conus temp max` → collection `nbm-conus-2m-agl`, parameter `TMAX`.

## Query Examples

```bash
# Temperature at 850 hPa, 6h forecast
edr position hrrr isobaric temp -c -105,40 -z 850 -t 6h

# METAR observations within 25 km
edr radius metar -c -105,40 --within 25km

# Lightning in a box
edr area glm lightning --bbox -106,39,-104,42

# 3D slice
edr cube hrrr isobaric --bbox -106,39,-104,42 -z 500/850 -t latest

# Flight path corridor
edr corridor hrrr surface -c "-105,40;-104,41" --width 10km

# Just print the URL
edr url position hrrr surface temp -c -105,40 -t 6h
```

## Hammer

Full dimension sweep: matching collections × parameters × timesteps × z-levels × random points, fired in parallel.

```bash
edr hammer hrrr                                    # everything matching "hrrr"
edr hammer guam --dry-run                          # preview request URLs
edr hammer hrrr isobaric -w 10 \
    --max-timesteps 8 --max-levels 3 --limit 200   # capped sweep
edr hammer hrrr --query-type cube                  # sweep with cube queries

# Paced + duration-bounded runs
edr hammer guam --rps 50 --duration 30s            # 50 req/s for 30s (cycling)
edr hammer guam --warmup 5 --report run.json       # stats JSON for diffing runs
```

Key options:
- `--rps N` — pace to a target request rate (default: as fast as possible)
- `--duration T` — run for a wall-clock window (`30s`, `5m`), cycling the request list
- `--warmup N` — exclude the first N requests from latency stats
- `--report PATH` — write machine-readable results JSON
- `--limit N` — hard cap on total requests (always applies)

Output: ok/fail counts, status-code breakdown, throughput, latency percentiles + histogram. Responses saved to `edr_hammer_output/` unless `--no-save`.

While running, a live status panel shows the progress bar, ok/fail counts, req/s, latency, data received, and the most recent requests (status code, payload size, duration). Non-TTY sessions get plain progress lines instead.

---

# MCP Server

An MCP (Model Context Protocol) server exposes the same WMS + EDR tooling to LLM agents (Claude Code, Claude Desktop, etc.). It runs on the same core as the CLIs and shares their config and caches in `~/.wms/`.

## Setup

`ogcli-mcp` is built alongside the CLIs (`cargo build --release` or
`./install.sh`) and speaks stdio.

### Claude Code

```bash
claude mcp add ogcli -- ~/.local/bin/ogcli-mcp
```

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "ogcli": {
      "command": "/Users/you/.local/bin/ogcli-mcp"
    }
  }
}
```

## Tools

| Tool | Description |
|------|-------------|
| `edr_status` / `wms_status` | Show current server config |
| `edr_init` / `wms_init` | Point at a server (shared with CLI config) |
| `edr_collections(query)` | Fuzzy-search EDR collections |
| `edr_describe(query)` | Collection detail: extent, params, query types |
| `edr_query(query, type, ...)` | Run any EDR query; returns a compact summary + saved payload path |
| `edr_instances(query)` | List model-run instances |
| `edr_locations(query, id?)` | List named locations or fetch one |
| `edr_hammer(query, ...)` | Load test — **dry-run preview by default** |
| `wms_layers(query)` | Fuzzy-search WMS layers |
| `wms_describe(query)` | Layer detail: dimensions, styles, bbox |
| `wms_map(query)` | GetMap — returns an inline PNG the model can view |
| `wms_url(type, query)` | Build a WMS URL without fetching |
| `wms_hammer(query, ...)` | Load test — **dry-run preview by default** |

Agents get natural syntax for free: `edr_query("hrrr surface temp", coords="-105,40", z="850")` resolves the collection *and* parameter. All tools accept an optional `profile` parameter to target a saved server profile (default: active).

Notes for agent use:
- EDR responses are summarized (axes + value ranges); full payloads are saved to `~/.wms/mcp_output/` and the path is returned
- The hammer tools default to `dry_run=true` and are hard-capped at 500 requests / 64 workers per call; the server instructions tell agents to preview first and confirm large runs with the user
