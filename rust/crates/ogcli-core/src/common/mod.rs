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

/// Interactive profile picker: prints a numbered list (with the active
/// profile marked) and reads a selection (number or exact name).
/// Returns the chosen name. Non-TTY stdin returns an error listing the
/// profiles so scripts fail loudly instead of hanging.
pub fn pick_profile_interactive(
    profiles: &[(String, String)], // (name, detail)
    active: &str,
    tool: &str,
) -> anyhow::Result<String> {
    use std::io::{BufRead, Write};

    if !std::io::IsTerminal::is_terminal(&std::io::stdin()) {
        let list = profiles
            .iter()
            .map(|(name, detail)| format!("  {name:<20} {detail}"))
            .collect::<Vec<_>>()
            .join("\n");
        anyhow::bail!(
            "interactive picker requires a terminal. Profiles:\n{list}\n\
             Use '{tool} set profile NAME' or '{tool} --profile NAME <command>'"
        );
    }

    println!("Saved profiles:");
    for (i, (name, detail)) in profiles.iter().enumerate() {
        let marker = if name == active { "*" } else { " " };
        println!(" {marker} {:2}. {name:<20} {detail}", i + 1);
    }
    print!("Select profile [1-{}] (blank = keep '{}'): ",
           profiles.len(), active);
    std::io::stdout().flush()?;

    let mut line = String::new();
    std::io::stdin().lock().read_line(&mut line)?;
    let line = line.trim();

    if line.is_empty() {
        return Ok(active.to_string());
    }
    // Number selection
    if let Ok(n) = line.parse::<usize>() {
        if n >= 1 && n <= profiles.len() {
            return Ok(profiles[n - 1].0.clone());
        }
        anyhow::bail!("invalid selection {n} (expected 1-{})", profiles.len());
    }
    // Exact name selection
    if let Some((name, _)) = profiles.iter().find(|(name, _)| name == line) {
        return Ok(name.clone());
    }
    anyhow::bail!("no profile named '{line}'")
}
