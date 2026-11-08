# ogcli (Rust)

Rust rewrite of the ogcli WMS/EDR tools. Wire-compatible with the
Python implementation: reads and writes the same `~/.wms` config,
profiles, and cache files, so both can be used interchangeably.

## Layout

- `crates/ogcli-core` — library: config/profiles, parsers, fuzzy
  resolver, HTTP clients (shared by binaries and, later, the MCP server)
- `crates/ogcli` — binaries (`edr`, `wms`)
- `crates/ogcli-mcp` — MCP server (`ogcli-mcp`, 15 tools, stdio)

## Status

Phases 1-4 (EDR complete + WMS) are complete:
- config/profiles with legacy migration, per-profile caches
- /collections parser with the cross-server quirk handling (WKT CRS,
  nested unit symbols, slash-in-ids)
- fuzzy resolver at behavioral parity with the Python implementation
  (fixture-locked scoring, including exact rapidfuzz-compatible
  ratio/partial_ratio ported to LCS-based implementations)
- `edr init/status/profiles/set profile/collections/describe` with
  `-p/--profile` override
- all query patterns (position/radius/area/cube/trajectory/corridor,
  locations, instances, url) with CoverageJSON summaries
- hammer: full dimension sweep, --rps pacing, --duration cycling,
  --warmup exclusion, --report JSON, latency histogram, indicatif
  progress

WMS side: config/profiles, capabilities XML parser (namespace-tolerant,
local-name matching), smart resolver (natural dimension syntax,
unit preference, model bonus, dimension defaults/snapping), and the
map/tile/legend/layers/describe/url/cache/hammer/groups/batch
commands. `wms hammer` sweeps the dimension space (RUN x FORECAST x
ELEVATION, styles) like the Python hammer, with `-n/--limit` as a
safety cap Python lacks; `wms batch` downloads one GetMap per matched
layer using the smart resolver's defaults; `wms groups` lists model
groups from layer-name prefixes.

The MCP server (crates/ogcli-mcp) ports all 15 Python tools via rmcp:
same ~/.wms config/profiles, same response shapes (JSON summaries,
capped at ~50k chars, payloads saved to ~/.wms/mcp_output), hammer
safety caps (dry_run default, 500 req / 64 workers), and model-visible
isError results with clean messages. wms_map returns an inline PNG.

Register (Rust): claude mcp add ogcli -- rust/target/release/ogcli-mcp

Remaining phases: distribution (install scripts still install the
Python package; nothing installs the Rust binaries yet).

## Build

```sh
cargo build --release
target/release/edr --help
```

The Python implementation in `../src` remains the reference until the
port reaches feature parity.
