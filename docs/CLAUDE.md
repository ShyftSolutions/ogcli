# CLAUDE.md

This file provides guidance to Claude Code when working with this repository.

## Project Overview

**ogcli** is a Rust command-line toolset for interacting with OGC web services: **wms** (Web Map Service) and **edr** (Environmental Data Retrieval). It features fuzzy search for layer/collection names and natural language dimension syntax, plus an MCP server (`ogcli-mcp`) exposing the same tooling to LLM agents.

### Key Features
- Fuzzy search to find layers/collections by name/description
- Natural dimension syntax: `6h`, `12z`, `850mb` instead of ISO8601
- Batch operations for downloading multiple layers
- Performance testing (hammer) for stress testing WMS/EDR servers
- MCP server with 15 tools (stdio, for Claude Code/Desktop etc.)

## Project Structure

```
ogcli/
├── rust/                        # Rust workspace (the implementation)
│   ├── crates/
│   │   ├── ogcli-core/          # Library: shared by CLIs + MCP
│   │   │   ├── src/common/      # Config dir, profile picker
│   │   │   ├── src/edr/         # EDR client, config, parser, resolver,
│   │   │   │                    #   query builder, hammer
│   │   │   ├── src/wms/         # WMS config, XML parser, resolver, hammer
│   │   │   ├── src/fuzz.rs      # Fuzzy scoring (rapidfuzz-compatible)
│   │   │   └── tests/           # Fixture-locked parity tests
│   │   ├── ogcli/               # The `edr` and `wms` binaries
│   │   │   └── src/completion.rs  # Click-compatible shell completion
│   │   └── ogcli-mcp/           # MCP server binary (rmcp, stdio)
│   └── README.md                # Rust-specific notes
├── docs/                        # Documentation
│   ├── USAGE.md                 # Comprehensive usage guide
│   ├── COMMAND_SYNTAX.md        # CLI command reference
│   └── CLAUDE.md                # This file - development guide
└── install.sh                   # Global install (builds + installs binaries)
```

## Development Setup

```bash
# Build (requires https://rustup.rs)
cd rust
cargo build --release

# Run CLI
./target/release/wms --help

# Run tests
cargo test

# Global install (~/.local/bin + shell completion)
../install.sh
```

## Key Commands

```bash
# Initialize with a WMS server
wms init https://ogc.shyftwx.com/ogc/WMS

# Search and explore layers
wms layers temp              # Search layers
wms layers temp -v           # With dimensions/styles
wms groups --counts          # List groups
wms describe gfs temp        # Layer details

# Fetch maps
wms map gfs temp f 6h        # Fetch map
wms map gfs temp --debug     # Preview URL
wms legend gfs temp          # Get legend
wms tile gfs temp -z 5       # Get tile

# Batch operations
wms batch gfs temp --list    # List matches
wms batch gfs temp -o ./out/ # Download all
wms hammer gfs --workers 20  # Performance test

# Cache management
wms cache status             # Check cache
wms cache refresh            # Force refresh

# URL building
wms url map gfs temp 6h      # Build GetMap URL
```

See `docs/USAGE.md` for comprehensive usage guide and `docs/COMMAND_SYNTAX.md` for full command reference.

## Architecture

### Query Resolution Flow

```
User Query: "gfs temp f 12z 6h"
     ↓
SmartQueryResolver (ogcli-core/src/wms/resolver.rs)
     ↓
1. Extract dimensions: {RUN: '12z', FORECAST: '6h'}
2. Extract unit preference: 'f' (Fahrenheit)
3. Clean query: "gfs temp"
     ↓
Fuzzy scoring (ogcli-core/src/fuzz.rs)
     ↓
4. Fuzzy match layer name
5. Apply unit filter
6. Return: GFS_Temperature_F
     ↓
Dimension parsing (same resolver module)
     ↓
7. Validate '12z' → find closest 12Z run
8. Validate '6h' → PT6H
     ↓
Result: ResolvedQuery with valid dimensions
```

### Key Modules

| Module | File | Purpose |
|--------|------|---------|
| `SmartQueryResolver` | `ogcli-core/src/wms/resolver.rs` | Query resolution, dimension parsing |
| `fuzz` | `ogcli-core/src/fuzz.rs` | rapidfuzz-compatible scoring |
| `WmsCapabilities` | `ogcli-core/src/wms/parser.rs` | GetCapabilities XML parsing (roxmltree) |
| `EdrResolver` / parser | `ogcli-core/src/edr/` | Collections JSON, CoverageJSON summaries |
| `hammer` | `ogcli-core/src/{wms,edr}/hammer.rs` | Dimension-sweep load generators |
| `completion` | `ogcli/src/completion.rs` | Click-compatible completion protocol |

### Configuration

Config stored in `~/.wms/` (shared by both CLIs and the MCP server):
- `config.json` - WMS profiles, source URL, settings
- `edr_config.json` - EDR profiles, server URL, settings
- `capabilities_cache.xml` / `edr_cache/` - Cached capabilities (10-min sliding TTL, per profile)

## WMS Concepts

### Dimensions

WMS layers can have multiple dimensions:

| Dimension | Purpose | Example Values |
|-----------|---------|----------------|
| RUN | Model run time | 2025-11-05T12:00:00Z |
| FORECAST | Forecast period | PT6H, PT12H, P2D |
| ELEVATION | Pressure level | 850, 500, 300 (hPa) |
| TIME | Valid time | ISO datetime |
| STATION | Point location | For meteograms |
| PLACE | Line location | For cross-sections |

### Natural Dimension Syntax

The CLI accepts shortcuts:
- `6h`, `12h`, `24h` → FORECAST (PT6H, PT12H, PT24H)
- `00z`, `12z`, `latest` → RUN (model run time)
- `850`, `500mb` → ELEVATION (pressure level)
- `f`, `c`, `k` → Unit preference (Fahrenheit/Celsius/Kelvin)

## Testing

```bash
cd rust
cargo test            # 45 tests: resolver parity (fixture-locked),
                      # parser, config, hammer generation
```

## Common Tasks

### Adding a New CLI Command

1. Add a variant to the `Command` enum in `rust/crates/ogcli/src/bin/wms.rs` (or `edr.rs`)
2. Implement `cmd_<name>` and wire it into the `match` dispatch
3. If it takes a fuzzy layer query, add it to `query_commands()` in `completion.rs`
4. Add the subcommand name to `subcommands()` in `completion.rs`

### Modifying Dimension Parsing

1. Token extraction and dimension parsing live in `SmartQueryResolver` (`ogcli-core/src/wms/resolver.rs`)
2. Resolver behavior is locked by the fixture tests in `ogcli-core/tests/wms_parity.rs` - run `cargo test` after changes

### Updating Cache Behavior

1. Cache logic is in `ogcli-core/src/wms/config.rs` (`WmsConfig.cache_ttl`, default 600s)
2. EDR caches are per-profile under `~/.wms/edr_cache/`

## Implementation Status

Rust implementation at parity with the former Python CLIs (Python code
was removed after the Rust rewrite):

| Command | Status |
|---------|--------|
| `wms init` | ✅ Complete |
| `wms map` | ✅ Complete |
| `wms tile` | ✅ Complete |
| `wms legend` | ✅ Complete |
| `wms layers` | ✅ Complete |
| `wms describe` | ✅ Complete |
| `wms groups` | ✅ Complete |
| `wms batch` | ✅ Complete |
| `wms hammer` | ✅ Complete |
| `wms url` | ✅ Complete |
| `wms cache` | ✅ Complete |
| `wms status` | ✅ Complete |

See `docs/COMMAND_SYNTAX.md` for detailed command syntax and options.
