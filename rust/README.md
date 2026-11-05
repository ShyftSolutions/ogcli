# ogcli (Rust)

Rust rewrite of the ogcli WMS/EDR tools. Wire-compatible with the
Python implementation: reads and writes the same `~/.wms` config,
profiles, and cache files, so both can be used interchangeably.

## Layout

- `crates/ogcli-core` — library: config/profiles, parsers, fuzzy
  resolver, HTTP clients (shared by binaries and, later, the MCP server)
- `crates/ogcli` — binaries (`edr`; `wms` comes with the WMS port)

## Status

Phase 1 (EDR core + discovery commands) is complete:
- config/profiles with legacy migration, per-profile caches
- /collections parser with the cross-server quirk handling (WKT CRS,
  nested unit symbols, slash-in-ids)
- fuzzy resolver at behavioral parity with the Python implementation
  (fixture-locked scoring, including exact rapidfuzz-compatible
  ratio/partial_ratio ported to LCS-based implementations)
- `edr init/status/profiles/set profile/collections/describe` with
  `-p/--profile` override
- CoverageJSON/CoverageCollection summarizer

Remaining phases: EDR queries -> hammer -> WMS -> MCP -> distribution.

## Build

```sh
cargo build --release
target/release/edr --help
```

The Python implementation in `../src` remains the reference until the
port reaches feature parity.
