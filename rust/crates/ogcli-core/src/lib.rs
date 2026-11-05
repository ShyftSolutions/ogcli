//! ogcli-core: shared library for the ogcli WMS/EDR tools.
//!
//! Wire-compatible with the Python implementation - it reads and
//! writes the same `~/.wms` configuration and cache files.

pub mod common;
pub mod edr;
pub mod fuzz;
pub mod wms;
