//! Shared helpers: config directory and cache TTL logic.

use std::path::PathBuf;

/// Shared config directory (`~/.wms` by default) used by both the WMS
/// and EDR tools - the same location the Python implementation uses.
///
/// The `OGCLI_HOME` environment variable overrides it (used by tests).
pub fn config_dir() -> PathBuf {
    if let Ok(dir) = std::env::var("OGCLI_HOME") {
        if !dir.is_empty() {
            return PathBuf::from(dir);
        }
    }
    let home = dirs::home_dir().unwrap_or_else(|| PathBuf::from("."));
    home.join(".wms")
}
