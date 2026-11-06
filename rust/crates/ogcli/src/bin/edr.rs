//! edr - EDR query tool with fuzzy search (OGC API - Environmental
//! Data Retrieval). Rust port of the Python CLI.

use std::process::ExitCode;

use anyhow::{bail, Context, Result};
use clap::{Parser, Subcommand};

use ogcli_core::edr::config::{
    self as edr_config, EdrConfig,
};
use ogcli_core::edr::query::{build_query, QueryOptions};
use ogcli_core::edr::resolver::EdrResolver;

#[derive(Parser)]
#[command(
    name = "edr",
    version = concat!(env!("CARGO_PKG_VERSION"), " (rust)"),
    about = "EDR query tool with fuzzy search (OGC API - Environmental Data Retrieval).",
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
    /// Point query (coords=POINT)
    Position(QueryArgs),
    /// Radius query (coords=POINT + within)
    Radius(QueryArgs),
    /// Area query (coords=POLYGON or --bbox)
    Area(QueryArgs),
    /// Cube query (bbox + vertical + time)
    Cube(QueryArgs),
    /// Trajectory query (coords=LINESTRING)
    Trajectory(QueryArgs),
    /// Corridor query (LINESTRING + width)
    Corridor(QueryArgs),
    /// List named locations for a collection (or fetch one with --id)
    Locations {
        query: Vec<String>,
        /// Fetch data at a specific location id
        #[arg(long = "id", value_name = "ID")]
        location_id: Option<String>,
        /// Time filter
        #[arg(short = 't', long = "datetime", value_name = "TIME")]
        datetime: Option<String>,
        /// Vertical level filter
        #[arg(short = 'z', long)]
        z: Option<String>,
        /// Parameter name(s)
        #[arg(short = 'p', long)]
        param: Option<String>,
        /// Max locations to list
        #[arg(short = 'n', long, default_value_t = 20)]
        limit: usize,
    },
    /// List model-run instances for a collection
    Instances { query: Vec<String> },
    /// Build an EDR URL without fetching
    Url {
        request_type: String,
        query: Vec<String>,
        #[command(flatten)]
        opts: QueryOptionsArgs,
    },
    /// Hammer all collections matching query with a full dimension sweep
    Hammer {
        query: Vec<String>,
        /// Number of parallel workers
        #[arg(short = 'w', long, default_value_t = 20)]
        workers: usize,
        /// Query pattern to hammer with
        #[arg(long = "query-type", default_value = "position")]
        query_type: String,
        /// Sample N timesteps per collection (0=all)
        #[arg(long)]
        max_timesteps: Option<usize>,
        /// Sample N parameters per collection (0=all)
        #[arg(long)]
        max_params: Option<usize>,
        /// Sample N vertical levels (0=all)
        #[arg(long)]
        max_levels: Option<usize>,
        /// Random points per collection
        #[arg(long, default_value_t = 1)]
        points: usize,
        /// Cap total requests (0=unlimited)
        #[arg(short = 'n', long)]
        limit: Option<usize>,
        /// Run for a wall-clock window (e.g. 30s, 5m), cycling requests
        #[arg(short = 'D', long)]
        duration: Option<String>,
        /// Pace to N requests/second (default: as fast as possible)
        #[arg(long)]
        rps: Option<f64>,
        /// Exclude the first N requests from latency stats
        #[arg(long)]
        warmup: Option<usize>,
        /// Write machine-readable results JSON to PATH
        #[arg(long = "report", value_name = "PATH")]
        report_path: Option<String>,
        /// Output directory
        #[arg(short = 'o', long, default_value = "edr_hammer_output")]
        output: String,
        /// Do not save response bodies
        #[arg(long = "no-save")]
        no_save: bool,
        /// Show requests without executing
        #[arg(long = "dry-run")]
        dry_run: bool,
    },
}

/// Shared arguments for the geometry query commands.
#[derive(clap::Args, Debug)]
pub struct QueryArgs {
    query: Vec<String>,
    /// Coordinates: "lon,lat" or WKT (POINT(-105 40))
    #[arg(short = 'c', long, allow_hyphen_values = true)]
    coords: Option<String>,
    /// Bounding box minx,miny,maxx,maxy (area/cube)
    #[arg(short = 'b', long, allow_hyphen_values = true)]
    bbox: Option<String>,
    /// Time: latest, +6h, PT6H, ISO instant, or a/b interval
    #[arg(short = 't', long = "datetime", value_name = "TIME")]
    datetime: Option<String>,
    /// Vertical level: 850, 850mb, or low/high interval
    #[arg(short = 'z', long)]
    z: Option<String>,
    /// Parameter name(s), comma-separated
    #[arg(short = 'p', long)]
    param: Option<String>,
    /// Output CRS
    #[arg(long)]
    crs: Option<String>,
    /// Output format (f= parameter)
    #[arg(short = 'f', long)]
    format: Option<String>,
    /// Search distance (radius), e.g. 25km
    #[arg(long)]
    within: Option<String>,
    /// Corridor width, e.g. 10km
    #[arg(long)]
    width: Option<String>,
    /// Instance (model run) id
    #[arg(long)]
    instance: Option<String>,
    /// Output filename
    #[arg(short = 'o', long)]
    output: Option<String>,
    /// Show resolved URL without fetching
    #[arg(short = 'd', long)]
    debug: bool,
}

/// Options-only variant for `edr url`.
#[derive(clap::Args, Debug)]
pub struct QueryOptionsArgs {
    #[arg(short = 'c', long, allow_hyphen_values = true)]
    coords: Option<String>,
    #[arg(short = 'b', long, allow_hyphen_values = true)]
    bbox: Option<String>,
    #[arg(short = 't', long = "datetime", value_name = "TIME")]
    datetime: Option<String>,
    #[arg(short = 'z', long)]
    z: Option<String>,
    #[arg(short = 'p', long)]
    param: Option<String>,
    #[arg(long)]
    within: Option<String>,
    #[arg(long)]
    width: Option<String>,
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
    // Die quietly on SIGPIPE instead of panicking when output is piped
    #[cfg(unix)]
    unsafe {
        libc::signal(libc::SIGPIPE, libc::SIG_DFL);
    }
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
        Command::Position(args) => cmd_query("position", &args, config),
        Command::Radius(args) => {
            if args.within.is_none() {
                bail!("radius query requires --within (e.g. --within 25km)");
            }
            cmd_query("radius", &args, config)
        }
        Command::Area(args) => cmd_query("area", &args, config),
        Command::Cube(args) => cmd_query("cube", &args, config),
        Command::Trajectory(args) => {
            if args.coords.is_none() && args.bbox.is_none() {
                bail!("trajectory requires --coords (two or more points)");
            }
            cmd_query("trajectory", &args, config)
        }
        Command::Corridor(args) => {
            if args.width.is_none() {
                bail!("corridor requires --width (e.g. --width 10km)");
            }
            if args.coords.is_none() && args.bbox.is_none() {
                bail!("corridor requires --coords (two or more points)");
            }
            cmd_query("corridor", &args, config)
        }
        Command::Locations { query, location_id, datetime, z, param, limit } => {
            cmd_locations(query, location_id, datetime, z, param, limit, config)
        }
        Command::Instances { query } => cmd_instances(query, config),
        Command::Url { request_type, query, opts } => {
            cmd_url(request_type, query, opts, config)
        }
        Command::Hammer {
            query, workers, query_type, max_timesteps, max_params, max_levels,
            points, limit, duration, rps, warmup, report_path, output,
            no_save, dry_run,
        } => cmd_hammer(
            HammerOpts {
                query, workers, query_type, max_timesteps, max_params, max_levels,
                points, limit, duration, rps, warmup, report_path, output,
                no_save, dry_run,
            },
            config,
        ),
    }
}

/// Collected hammer arguments (passed as a struct for readability).
struct HammerOpts {
    query: Vec<String>,
    workers: usize,
    query_type: String,
    max_timesteps: Option<usize>,
    max_params: Option<usize>,
    max_levels: Option<usize>,
    points: usize,
    limit: Option<usize>,
    duration: Option<String>,
    rps: Option<f64>,
    warmup: Option<usize>,
    report_path: Option<String>,
    output: String,
    no_save: bool,
    dry_run: bool,
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

// ---------------------------------------------------------------------------
// Query commands
// ---------------------------------------------------------------------------

fn cmd_query(query_type: &str, args: &QueryArgs, config: EdrConfig) -> Result<()> {
    if !config.is_initialized() {
        bail!("EDR not initialized. Run 'edr init <url>' first.");
    }
    if args.query.is_empty() {
        bail!("{query_type} requires a collection query");
    }
    let resolver = resolver_for(&config)?;
    let query_str = args.query.join(" ");
    let resolved = resolver
        .resolve(&query_str)
        .map_err(|e| anyhow::anyhow!("{e}"))?;
    let collection = resolver.collection(resolved.collection).clone();

    let opts = QueryOptions {
        coords: args.coords.clone(),
        bbox: args.bbox.clone(),
        datetime: args.datetime.clone(),
        z: args.z.clone(),
        param: args.param.clone(),
        within: args.within.clone(),
        width: args.width.clone(),
        instance: args.instance.clone(),
        crs: args.crs.clone(),
        format: args.format.clone(),
    };
    let built = build_query(&collection, &resolved, query_type, &opts, &resolver)?;

    // Show what we resolved to
    println!("-> Collection: {}", collection.id);
    let geom: Vec<String> = built
        .params
        .iter()
        .filter(|(k, _)| k == "coords" || k == "bbox")
        .map(|(k, v)| format!("{k}={v}"))
        .collect();
    if !geom.is_empty() {
        println!("  Query: {query_type} {}", geom.join(" "));
    } else {
        println!("  Query: {query_type}");
    }
    if let Some(dt) = &built.datetime {
        println!("  datetime: {dt}");
    }
    if let Some(z) = &built.z {
        println!("  z: {z}");
    }
    if let Some(p) = &built.param {
        println!("  parameter-name: {p}");
    }
    if let Some(instance) = &built.instance {
        println!("  instance: {instance}");
    }
    if resolved.confidence < 100.0 {
        println!("  (confidence: {:.0}%)", resolved.confidence);
    }
    for w in &built.warnings {
        println!("  ! {w}");
    }

    let url = ogcli_core::edr::query::build_url(config.server_url(), &built, config.api_key());
    if args.debug {
        println!();
        println!("URL:");
        println!("{url}");
        return Ok(());
    }

    println!("Fetching...");
    let client = ogcli_core::edr::client::EdrClient::new(config.server_url(), config.api_key());
    let (body, content_type) = client.query(
        &built.collection_id,
        &built.query_type,
        built.instance.as_deref(),
        &built.params,
    )?;

    report_response(&body, &content_type, &built.collection_id, &built.query_type, args.output.as_deref());
    Ok(())
}

/// Save the raw payload and print a coverage summary (Python parity).
fn report_response(
    body: &[u8],
    content_type: &str,
    collection_id: &str,
    query_type: &str,
    output: Option<&str>,
) {
    let is_json = content_type.to_lowercase().contains("json")
        || body.first().is_some_and(|b| *b == b'{' || *b == b'[');
    let output = output.map(String::from).unwrap_or_else(|| {
        let ext = if is_json { "json" } else { "bin" };
        format!("{collection_id}_{query_type}.{ext}")
    });
    let sanitized = output
        .replace(':', "-")
        .replace("/", "-")
        .replace(' ', "_");
    if let Err(e) = std::fs::write(&sanitized, body) {
        eprintln!("Error saving {sanitized}: {e}");
        return;
    }
    let size_kb = body.len() as f64 / 1024.0;
    println!();
    println!("✓ Saved: {sanitized} ({size_kb:.1} KB)  [{}]",
             if content_type.is_empty() { "unknown type" } else { content_type });

    if is_json {
        if let Some(summary) = ogcli_core::edr::parser::summarize_coverage(body) {
            println!();
            println!("Coverage summary:");
            let ctype = if summary.coverage_type.is_empty() { "-" } else { &summary.coverage_type };
            println!("  type: {ctype}");
            if !summary.axes.is_empty() {
                let mut axes: Vec<(&String, &usize)> = summary.axes.iter().collect();
                axes.sort_by(|a, b| a.0.cmp(b.0));
                let parts: Vec<String> = axes
                    .iter()
                    .map(|(k, v)| format!("{k}={v}"))
                    .collect();
                println!("  axes: {}", parts.join(", "));
            }
            for p in &summary.parameters {
                let mut bits = vec![p.name.clone()];
                if !p.unit.is_empty() {
                    bits.push(format!("[{}]", p.unit));
                }
                if p.count > 0 {
                    bits.push(format!("{} values", p.count));
                }
                if let Some(min) = p.min {
                    bits.push(format!("min {:.3e} max {:.3e}", min, p.max.unwrap_or(min)));
                }
                if p.nils > 0 {
                    bits.push(format!("({} nulls)", p.nils));
                }
                println!("    {}", bits.join(" "));
            }
        }
    }
}

fn cmd_locations(
    query: Vec<String>,
    location_id: Option<String>,
    datetime: Option<String>,
    z: Option<String>,
    param: Option<String>,
    limit: usize,
    config: EdrConfig,
) -> Result<()> {
    if !config.is_initialized() {
        bail!("EDR not initialized. Run 'edr init <url>' first.");
    }
    if query.is_empty() {
        bail!("locations requires a collection query");
    }
    let resolver = resolver_for(&config)?;
    let resolved = resolver
        .resolve(&query.join(" "))
        .map_err(|e| anyhow::anyhow!("{e}"))?;
    let c = resolver.collection(resolved.collection);
    let client = ogcli_core::edr::client::EdrClient::new(config.server_url(), config.api_key());

    if let Some(location_id) = location_id {
        let mut params: Vec<(String, String)> = Vec::new();
        if let Some(dt) = &datetime {
            params.push(("datetime".into(), dt.clone()));
        }
        if let Some(z) = &z {
            params.push(("z".into(), z.clone()));
        }
        if let Some(p) = &param {
            params.push(("parameter-name".into(), p.clone()));
        }
        println!("Fetching location '{location_id}'...");
        let (body, ctype) = client.get_location(&c.id, &location_id, &params)?;
        report_response(&body, &ctype, &format!("{}_{location_id}", c.id), "location", None);
        return Ok(());
    }

    println!("-> Collection: {}", c.id);
    println!("Fetching locations...");
    let data = client.get_locations(&c.id)?;
    let empty = Vec::new();
    let features = data
        .get("features")
        .and_then(|v| v.as_array())
        .unwrap_or(&empty);
    println!();
    println!(
        "Locations ({} total, showing {}):",
        features.len(),
        limit.min(features.len())
    );
    for feat in features.iter().take(limit) {
        let fid = feat.get("id").and_then(|v| v.as_str()).unwrap_or("?");
        let props = feat.get("properties").cloned().unwrap_or_default();
        let name = props
            .get("name")
            .or_else(|| props.get("title"))
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let coords = feat
            .get("geometry")
            .and_then(|g| g.get("coordinates"))
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default();
        let loc = if coords.len() >= 2 {
            format!(
                "  ({}, {})",
                coords[0], coords[1]
            )
        } else {
            String::new()
        };
        println!("  {fid}  {name}{loc}");
    }
    Ok(())
}

fn cmd_instances(query: Vec<String>, config: EdrConfig) -> Result<()> {
    if !config.is_initialized() {
        bail!("EDR not initialized. Run 'edr init <url>' first.");
    }
    if query.is_empty() {
        bail!("instances requires a collection query");
    }
    let resolver = resolver_for(&config)?;
    let resolved = resolver
        .resolve(&query.join(" "))
        .map_err(|e| anyhow::anyhow!("{e}"))?;
    let c = resolver.collection(resolved.collection);
    println!("-> Collection: {}", c.id);
    println!("Fetching instances...");
    let client = ogcli_core::edr::client::EdrClient::new(config.server_url(), config.api_key());
    let data = client.get_instances(&c.id)?;
    let empty = Vec::new();
    let list = data
        .get("instances")
        .and_then(|v| v.as_array())
        .unwrap_or(&empty);
    println!();
    println!("Instances ({}):", list.len());
    for inst in list {
        let iid = inst.get("id").and_then(|v| v.as_str()).unwrap_or("?");
        let title = inst.get("title").and_then(|v| v.as_str()).unwrap_or("");
        println!("  {iid}  {title}");
    }
    Ok(())
}

fn cmd_url(
    request_type: String,
    query: Vec<String>,
    opts: QueryOptionsArgs,
    config: EdrConfig,
) -> Result<()> {
    if !config.is_initialized() {
        bail!("EDR not initialized. Run 'edr init <url>' first.");
    }
    let valid = [
        "collections", "position", "radius", "area", "cube",
        "trajectory", "corridor", "locations", "instances",
    ];
    if !valid.contains(&request_type.as_str()) {
        bail!("unknown request type '{request_type}' (one of: {})", valid.join(", "));
    }
    if request_type == "collections" {
        println!(
            "{}/collections",
            config.server_url().trim_end_matches('/')
        );
        return Ok(());
    }
    if request_type == "locations" || request_type == "instances" {
        if query.is_empty() {
            bail!("{request_type} requires a collection query");
        }
        let resolver = resolver_for(&config)?;
        let resolved = resolver
            .resolve(&query.join(" "))
            .map_err(|e| anyhow::anyhow!("{e}"))?;
        let c = resolver.collection(resolved.collection);
        println!(
            "{}/collections/{}/{request_type}",
            config.server_url().trim_end_matches('/'),
            c.id
        );
        return Ok(());
    }
    if query.is_empty() {
        bail!("{request_type} requires a collection query");
    }

    // Reuse the query path in debug (URL-only) mode
    let args = QueryArgs {
        query,
        coords: opts.coords,
        bbox: opts.bbox,
        datetime: opts.datetime,
        z: opts.z,
        param: opts.param,
        crs: None,
        format: None,
        within: opts.within,
        width: opts.width,
        instance: None,
        output: None,
        debug: true,
    };
    cmd_query(&request_type, &args, config)
}

// ---------------------------------------------------------------------------
// Hammer
// ---------------------------------------------------------------------------

fn cmd_hammer(args: HammerOpts, config: EdrConfig) -> Result<()> {
    use ogcli_core::edr::hammer::EdrHammer;
    use ogcli_core::edr::resolver::parse_duration;

    if !config.is_initialized() {
        bail!("EDR not initialized. Run 'edr init <url>' first.");
    }
    if args.query.is_empty() {
        bail!("hammer requires a collection query");
    }
    let query_str = args.query.join(" ");

    let duration = match &args.duration {
        Some(d) => parse_duration(d)
            .and_then(|d| d.to_std().ok())
            .ok_or_else(|| anyhow::anyhow!("invalid duration '{d}' (use e.g. 30s, 5m, 2h)"))?,
        None => std::time::Duration::ZERO,
    };

    let resolver = resolver_for(&config)?;
    let matching: Vec<usize> = resolver
        .list_collections(Some(&query_str), 200)
        .into_iter()
        .filter(|(_, score)| *score > 50.0)
        .map(|(i, _)| i)
        .collect();
    if matching.is_empty() {
        bail!("no confident matches for '{query_str}'");
    }

    println!("-> Found {} collections matching '{query_str}':", matching.len());
    for &i in matching.iter().take(10) {
        println!("  - {}", resolver.collection(i).summary_line());
    }
    if matching.len() > 10 {
        println!("  ... and {} more", matching.len() - 10);
    }

    // The hammer borrows the collection slice
    let mut collections = resolver.capabilities.collections.clone();
    let matching_ids: Vec<String> = matching
        .iter()
        .map(|&i| collections[i].id.clone())
        .collect();
    collections.retain(|c| matching_ids.contains(&c.id));

    let mut hammer = EdrHammer::new(config.server_url(), config.api_key(), &collections);
    hammer.workers = args.workers;
    hammer.query_type = args.query_type.clone();
    hammer.output_dir = std::path::PathBuf::from(&args.output);
    hammer.save = !args.no_save;
    hammer.dry_run = args.dry_run;
    hammer.max_timesteps = args.max_timesteps.unwrap_or(0);
    hammer.max_params = args.max_params.unwrap_or(0);
    hammer.max_levels = args.max_levels.unwrap_or(0);
    hammer.points = args.points;
    hammer.limit = args.limit.unwrap_or(0);
    hammer.duration = duration;
    hammer.rps = args.rps.unwrap_or(0.0);
    hammer.warmup = args.warmup.unwrap_or(0);

    hammer.generate();
    if args.dry_run || duration.is_zero() {
        println!();
        println!(
            "Generated {} requests (query: {}, workers: {})",
            hammer.requests.len(),
            args.query_type,
            args.workers
        );
    } else {
        println!();
        println!(
            "Running for {:.0}s over {} generated requests (cycling, query: {})",
            duration.as_secs_f64(),
            hammer.requests.len(),
            args.query_type
        );
    }
    if let Some(rps) = args.rps {
        println!("Paced at {rps} req/s");
    }
    if args.dry_run {
        println!("(dry run - no requests will be made)");
    }

    let stats = hammer.run()?;
    if !args.dry_run {
        println!("{}", "=".repeat(50));
        println!("{}", stats.report());
        if let Some(path) = &args.report_path {
            hammer.write_report(&stats, std::path::Path::new(path))?;
            println!();
            println!("✓ Report saved: {path}");
        }
    }
    Ok(())
}
