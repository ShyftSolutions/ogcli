//! edr - EDR query tool with fuzzy search (OGC API - Environmental
//! Data Retrieval). Rust port of the Python CLI.

use std::process::ExitCode;

use anyhow::{bail, Context, Result};
use clap::{Parser, Subcommand};

use ogcli_core::edr::config::{
    self as edr_config, EdrConfig,
};
use ogcli_core::edr::resolver::EdrResolver;

#[derive(Parser)]
#[command(
    name = "edr",
    about = "EDR query tool with fuzzy search (OGC API - Environmental Data Retrieval).",
    version
)]
struct Cli {
    /// Use a saved server profile for this invocation
    #[arg(short, long, global = true, value_name = "NAME")]
    profile: Option<String>,

    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Subcommand)]
enum Command {
    /// Initialize EDR context from a server URL (no URL = refresh active profile)
    Init {
        url: Option<String>,
        /// API key (or set EDR_API_KEY env var)
        #[arg(long = "api-key", value_name = "KEY")]
        api_key: Option<String>,
        /// Save as a named profile (and make it active)
        #[arg(long = "save", value_name = "NAME")]
        save: Option<String>,
        /// Force re-initialization (clears caches)
        #[arg(short, long)]
        force: bool,
    },
    /// Show current EDR configuration
    Status,
    /// List saved server profiles (* = active)
    Profiles,
    /// Set configuration values
    Set {
        #[command(subcommand)]
        what: SetWhat,
    },
    /// List available collections, optionally fuzzy-filtered
    Collections {
        query: Vec<String>,
        /// Maximum results to show
        #[arg(short = 'n', long, default_value_t = 20)]
        limit: usize,
        /// Show all collections
        #[arg(short = 'a', long)]
        all: bool,
        /// Show query types, extent and parameters
        #[arg(short = 'v', long)]
        verbose: bool,
        /// Output as JSON
        #[arg(long = "json", name = "as_json")]
        as_json: bool,
        /// Just show count of matching collections
        #[arg(long)]
        count: bool,
    },
    /// Show detailed information about a collection
    Describe { query: Vec<String> },
}

#[derive(Subcommand)]
enum SetWhat {
    /// Set the active server profile (persists until changed)
    Profile {
        /// Profile name (omit to list available)
        name: Option<String>,
    },
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    match run(cli) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("Error: {e:#}");
            ExitCode::FAILURE
        }
    }
}

fn run(cli: Cli) -> Result<()> {
    // Load config up-front, applying any --profile override
    let mut config = EdrConfig::new();
    if cli.command.is_some() {
        config = edr_config::load_config();
        if let Some(name) = &cli.profile {
            if !config.has_profile(name) {
                let names = config.profile_names().join(", ");
                bail!("no profile named '{name}'. Saved profiles: {names}");
            }
            config.active = name.clone();
        }
    }

    match cli.command.unwrap_or(Command::Status) {
        Command::Init { url, api_key, save, force } => {
            cmd_init(url, api_key, save, force, config)
        }
        Command::Status => cmd_status(config),
        Command::Profiles => cmd_profiles(config),
        Command::Set { what } => match what {
            SetWhat::Profile { name } => cmd_set_profile(name),
        },
        Command::Collections { query, limit, all, verbose, as_json, count } => {
            cmd_collections(query, limit, all, verbose, as_json, count, config)
        }
        Command::Describe { query } => cmd_describe(query, config),
    }
}

fn cmd_init(
    url: Option<String>,
    api_key: Option<String>,
    save: Option<String>,
    force: bool,
    mut config: EdrConfig,
) -> Result<()> {
    if force && edr_config::clear_all_caches() {
        println!("✓ Cache cleared");
    }

    if let Some(url) = url {
        println!("Fetching collections from {url}...");
        edr_config::init_from_server(&url, api_key.as_deref().unwrap_or(""), &mut config, save.as_deref())?;
        println!("✓ Initialized EDR profile '{}'", config.active);
    } else {
        edr_config::refresh_active_profile(&mut config)
            .context("refresh active profile (run 'edr init <url>' first)")?;
        println!("✓ Refreshed profile '{}'", config.active);
    }
    println!("  Server: {}", config.server_url());

    let caps = edr_config::get_capabilities(&config)?;
    println!("  Collections: {}", caps.collections.len());
    println!("  Query types: {}", caps.query_types().join(", "));
    Ok(())
}

fn cmd_status(config: EdrConfig) -> Result<()> {
    if !config.is_initialized() {
        println!("Not initialized. Run 'edr init <url>' first.");
        return Ok(());
    }
    let extra = if config.profiles.len() > 1 {
        format!(" ({} saved)", config.profiles.len())
    } else {
        String::new()
    };
    println!("Profile: {}{extra}", config.active);
    println!("Server: {}", config.server_url());
    if !config.api_key().is_empty() {
        println!("API key: set");
    }

    let caps = edr_config::get_capabilities(&config)?;
    println!("Collections: {}", caps.collections.len());
    println!("Query types: {}", caps.query_types().join(", "));

    let cache = edr_config::edr_cache_file_for(&config, None);
    if let Some(mtime) = edr_config::cache_mtime(&cache) {
        let age_min = mtime.elapsed().map(|d| d.as_secs() / 60).unwrap_or(0);
        let valid = cache
            .metadata()
            .ok()
            .and_then(|m| m.modified().ok())
            .and_then(|t| t.elapsed().ok())
            .is_some_and(|age| age < config.cache_ttl());
        println!("Cache: {} ({age_min} min old)", if valid { "valid" } else { "expired" });
    }
    Ok(())
}

fn cmd_profiles(config: EdrConfig) -> Result<()> {
    if config.profiles.is_empty() {
        println!("No saved profiles. Save one with: edr init <url> --save NAME");
        return Ok(());
    }
    let mut names = config.profile_names();
    names.sort();
    for name in names {
        let p = &config.profiles[&name];
        let marker = if name == config.active { "*" } else { " " };
        let key = if p.api_key.is_empty() { "" } else { "  (api key set)" };
        println!("{marker} {name:<20} {}{key}", p.server_url);
    }
    Ok(())
}

fn cmd_set_profile(name: Option<String>) -> Result<()> {
    let mut config = edr_config::load_config();
    let Some(name) = name else {
        let mut names = config.profile_names();
        names.sort();
        for pname in names {
            let p = &config.profiles[&pname];
            let marker = if pname == config.active { "*" } else { " " };
            println!("{marker} {pname:<20} {}", p.server_url);
        }
        return Ok(());
    };
    if !config.has_profile(&name) {
        let mut names = config.profile_names();
        names.sort();
        let names = if names.is_empty() { "(none)".to_string() } else { names.join(", ") };
        bail!("no profile named '{name}'. Saved profiles: {names}");
    }
    config.set_active(&name)?;
    edr_config::save_config(&config)?;
    let url = config.profile().unwrap().server_url.clone();
    println!("✓ Active profile: {name} ({url})");
    Ok(())
}

fn resolver_for(config: &EdrConfig) -> Result<EdrResolver> {
    let caps = edr_config::get_capabilities(config)?;
    Ok(EdrResolver::new(caps))
}

fn cmd_collections(
    query: Vec<String>,
    limit: usize,
    all: bool,
    verbose: bool,
    as_json: bool,
    count: bool,
    config: EdrConfig,
) -> Result<()> {
    if !config.is_initialized() {
        bail!("EDR not initialized. Run 'edr init <url>' first.");
    }
    let resolver = resolver_for(&config)?;
    let query_str = if query.is_empty() {
        None
    } else {
        Some(query.join(" "))
    };

    let mut results = resolver.list_collections(query_str.as_deref(), 1000);
    if results.is_empty() {
        match &query_str {
            Some(q) => println!("No collections found matching: {q}"),
            None => println!("No collections found"),
        }
        return Ok(());
    }
    if !all && results.len() > limit {
        results.truncate(limit);
    }

    if count {
        println!("{} collections", results.len());
        return Ok(());
    }

    if as_json {
        let data: Vec<serde_json::Value> = results
            .iter()
            .map(|(i, score)| {
                let c = resolver.collection(*i);
                serde_json::json!({
                    "id": c.id,
                    "title": c.title,
                    "score": if query_str.is_some() { serde_json::json!((score * 10.0).round() / 10.0) } else { serde_json::Value::Null },
                    "query_types": c.data_queries,
                    "parameters": c.parameter_names.keys().collect::<Vec<_>>(),
                    "timesteps": c.n_timesteps(),
                    "levels": c.n_levels(),
                    "bbox": c.bbox,
                })
            })
            .collect();
        println!(
            "{}",
            serde_json::to_string_pretty(&serde_json::json!({
                "collections": data, "count": data.len()
            }))?
        );
        return Ok(());
    }

    match &query_str {
        Some(q) => println!("Collections matching '{q}':\n"),
        None => println!("Available collections ({} shown):\n", results.len()),
    }

    for (i, score) in &results {
        let c = resolver.collection(*i);
        let score_str = match &query_str {
            Some(_) => format!("{score:5.1}  "),
            None => "       ".to_string(),
        };
        println!("  {score_str}{}", c.summary_line());
        if verbose {
            if !c.title.is_empty() {
                println!("         {}", c.title);
            }
            if !c.data_queries.is_empty() {
                println!("         queries: {}", c.data_queries.join(", "));
            }
            if let Some(start) = c.temporal_start() {
                let end = c.temporal_end().unwrap_or("");
                let steps = if c.n_timesteps() > 0 {
                    format!(" ({} steps)", c.n_timesteps())
                } else {
                    String::new()
                };
                println!("         time: {start} -> {end}{steps}");
            }
            if !c.vertical_values.is_empty() {
                let shown: Vec<String> =
                    c.vertical_values.iter().take(8).cloned().collect();
                let more = if c.vertical_values.len() > 8 {
                    format!(" (+{} more)", c.vertical_values.len() - 8)
                } else {
                    String::new()
                };
                let unit = if c.vertical_unit.is_empty() {
                    String::new()
                } else {
                    format!(" {}", c.vertical_unit)
                };
                println!("         z: {}{more}{unit}", shown.join(", "));
            }
            if !c.parameter_names.is_empty() {
                let names: Vec<String> = c.parameter_names.keys().take(10).cloned().collect();
                let more = if c.parameter_names.len() > 10 {
                    format!(" (+{} more)", c.parameter_names.len() - 10)
                } else {
                    String::new()
                };
                println!("         params: {}{more}", names.join(", "));
            }
            println!();
        }
    }
    Ok(())
}

fn cmd_describe(query: Vec<String>, config: EdrConfig) -> Result<()> {
    if !config.is_initialized() {
        bail!("EDR not initialized. Run 'edr init <url>' first.");
    }
    if query.is_empty() {
        bail!("describe requires a collection query");
    }
    let resolver = resolver_for(&config)?;
    let resolved = resolver
        .resolve(&query.join(" "))
        .map_err(|e| anyhow::anyhow!("{e}"))?;
    let c = resolver.collection(resolved.collection);

    println!("Collection: {}", c.id);
    if !c.title.is_empty() {
        println!("Title: {}", c.title);
    }
    if !c.description.is_empty() {
        let mut desc = c.description.clone();
        if desc.len() > 300 {
            desc.truncate(300);
            desc.push_str("...");
        }
        println!("Description: {desc}");
    }
    println!();
    println!("Confidence: {:.0}%", resolved.confidence);
    println!("Query types: {}", c.data_queries.join(", "));

    if let Some(bbox) = &c.bbox {
        println!();
        println!("Spatial extent ({}):", c.crs);
        println!("  {}, {} -> {}, {}", bbox[0], bbox[1], bbox[2], bbox[3]);
    }

    if let Some(start) = c.temporal_start() {
        println!();
        println!("Temporal extent:");
        println!("  {} -> {}", start, c.temporal_end().unwrap_or(""));
        if c.n_timesteps() > 0 {
            println!("  {} steps", c.n_timesteps());
        }
    }

    if !c.vertical_values.is_empty() {
        let unit = if c.vertical_unit.is_empty() {
            String::new()
        } else {
            format!(" ({})", c.vertical_unit)
        };
        println!();
        println!("Vertical levels{unit}:");
        for chunk in c.vertical_values.chunks(10) {
            let vals: Vec<String> = chunk.iter().cloned().collect();
            println!("  {}", vals.join(", "));
        }
    }

    if !c.parameter_names.is_empty() {
        println!();
        println!("Parameters ({}):", c.parameter_names.len());
        let mut names: Vec<&String> = c.parameter_names.keys().collect();
        names.sort();
        for name in names {
            let p = &c.parameter_names[name];
            let mut bits = vec![name.clone()];
            if !p.label.is_empty() && p.label != *name {
                bits.push(format!("({})", p.label));
            }
            if !p.unit.is_empty() {
                bits.push(format!("[{}]", p.unit));
            }
            println!("  - {}", bits.join(" "));
        }
    }
    Ok(())
}
