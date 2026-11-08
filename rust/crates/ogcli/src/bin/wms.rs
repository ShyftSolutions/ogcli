//! wms - WMS query tool with fuzzy search. Rust port of the Python CLI.

use std::process::ExitCode;

use anyhow::{bail, Context, Result};
use clap::{Parser, Subcommand};

use ogcli_core::wms::config::{self as wms_config, WmsConfig};
use ogcli_core::wms::hammer::WmsHammer;
use ogcli_core::wms::parser::layer_groups;
use ogcli_core::wms::resolver::SmartQueryResolver;

#[derive(Parser)]
#[command(
    name = "wms",
    version = concat!(env!("CARGO_PKG_VERSION"), " (rust)"),
    about = "WMS query tool with fuzzy search.",
)]
struct Cli {
    /// Use a saved server profile for this invocation. With no value,
    /// interactively pick a profile and make it the active one.
    #[arg(short, long, global = true, value_name = "NAME", num_args = 0..=1)]
    profile: Option<Option<String>>,

    #[command(subcommand)]
    command: Option<Command>,
}

#[derive(Subcommand)]
enum Command {
    /// Initialize WMS context from a server URL or capabilities file
    /// (no source = refresh active profile)
    Init {
        source: Option<String>,
        /// Server URL for requests (when source is a local file)
        #[arg(short = 's', long)]
        server: Option<String>,
        /// Save as a named profile (and make it active)
        #[arg(long = "save", value_name = "NAME")]
        save: Option<String>,
        /// Force re-initialization (clears caches)
        #[arg(short, long)]
        force: bool,
    },
    /// Show current WMS configuration
    Status,
    /// List saved server profiles (* = active)
    Profiles,
    /// Set configuration values
    Set {
        #[command(subcommand)]
        what: SetWhat,
    },
    /// List available layers, optionally fuzzy-filtered
    Layers {
        query: Vec<String>,
        #[arg(short = 'n', long, default_value_t = 20)]
        limit: usize,
        /// Show all layers
        #[arg(short = 'a', long)]
        all: bool,
        /// Show dimensions and styles
        #[arg(short = 'v', long)]
        verbose: bool,
    },
    /// Show detailed information about a layer
    Describe { query: Vec<String> },
    /// Fetch a map image (GetMap)
    Map(MapArgs),
    /// Fetch a random tile (GetGTile)
    Tile(TileArgs),
    /// Fetch legend graphic for a layer
    Legend {
        query: Vec<String>,
        /// Output filename
        #[arg(short = 'o', long)]
        output: Option<String>,
        /// Show resolved URL without fetching
        #[arg(short = 'd', long)]
        debug: bool,
    },
    /// Hammer all layers matching query with a full dimension sweep
    Hammer(HammerArgs),
    /// Build a WMS URL without fetching
    Url {
        request_type: String,
        query: Vec<String>,
        #[arg(short = 'w', long, default_value_t = 800)]
        width: i64,
        #[arg(short = 'h', long, default_value_t = 600)]
        height: i64,
    },
    /// List layer groups/hierarchy
    Groups {
        /// Group name filter
        pattern: Option<String>,
        /// Show as hierarchical tree
        #[arg(long = "tree", name = "as_tree")]
        as_tree: bool,
        /// Show layer counts per group
        #[arg(long)]
        counts: bool,
    },
    /// Fetch all layers matching a group pattern (organized download)
    Batch(BatchArgs),
    /// Manage capabilities caches
    Cache {
        #[command(subcommand)]
        what: CacheWhat,
    },
}

#[derive(Subcommand)]
enum SetWhat {
    /// Set the active server profile (persists until changed)
    Profile { name: Option<String> },
}

#[derive(Subcommand)]
enum CacheWhat {
    /// Show cache status (all profiles)
    Status,
    /// Clear ALL capabilities caches
    Clear,
    /// Force refresh for the active profile
    Refresh,
}

#[derive(clap::Args, Debug)]
struct MapArgs {
    query: Vec<String>,
    /// Output filename
    #[arg(short = 'o', long)]
    output: Option<String>,
    /// Image width
    #[arg(short = 'w', long, default_value_t = 800)]
    width: i64,
    /// Image height
    #[arg(short = 'h', long, default_value_t = 600)]
    height: i64,
    /// Bounding box: minx,miny,maxx,maxy
    #[arg(short = 'b', long)]
    bbox: Option<String>,
    /// Coordinate reference system (default: from layer)
    #[arg(long)]
    crs: Option<String>,
    /// Image format
    #[arg(long = "format", default_value = "image/png")]
    img_format: String,
    /// Style name (default: layer default)
    #[arg(long)]
    style: Option<String>,
    /// Show resolved URL without fetching
    #[arg(short = 'd', long)]
    debug: bool,
}

#[derive(clap::Args, Debug)]
struct TileArgs {
    query: Vec<String>,
    /// Output filename
    #[arg(short = 'o', long)]
    output: Option<String>,
    /// Tile zoom level
    #[arg(short = 'z', long, default_value_t = 3)]
    zoom: u32,
    /// Show resolved URL without fetching
    #[arg(short = 'd', long)]
    debug: bool,
}

#[derive(clap::Args, Debug)]
struct HammerArgs {
    query: Vec<String>,
    /// Number of parallel workers
    #[arg(short = 'w', long, default_value_t = 20)]
    workers: usize,
    /// Show requests without executing
    #[arg(long = "dry-run")]
    dry_run: bool,
    /// Output directory
    #[arg(short = 'o', long, default_value = "wms_output")]
    output: String,
    /// Cap total requests (0 = unlimited)
    #[arg(short = 'n', long)]
    limit: Option<usize>,
}

#[derive(clap::Args, Debug)]
struct BatchArgs {
    query: Vec<String>,
    /// Output directory
    #[arg(short = 'o', long, default_value = "./batch_output")]
    output: String,
    /// Parallel download workers
    #[arg(short = 'w', long, default_value_t = 10)]
    workers: usize,
    /// Show what would be downloaded
    #[arg(long = "dry-run")]
    dry_run: bool,
    /// Just list matching layers
    #[arg(long = "list", name = "list_only")]
    list_only: bool,
    /// Max layers to process
    #[arg(short = 'n', long)]
    limit: Option<usize>,
}

fn main() -> ExitCode {
    // Die quietly on SIGPIPE instead of panicking when output is piped
    #[cfg(unix)]
    unsafe {
        libc::signal(libc::SIGPIPE, libc::SIG_DFL);
    }
    // Click-compatible shell completion (same protocol as the Python CLIs)
    if let Some(code) = ogcli::completion::handle(
        ogcli::completion::Tool::Wms,
        "wms",
    ) {
        return ExitCode::from(code as u8);
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
    let mut config;
    {
        config = wms_config::load_config();
        match cli.profile {
            // Bare --profile: interactive picker, sets the active profile
            Some(None) => {
                let mut names = config.profile_names();
                names.sort();
                let profiles: Vec<(String, String)> = names
                    .iter()
                    .map(|n| {
                        let p = &config.profiles[n];
                        (n.clone(), p.source_path.clone())
                    })
                    .collect();
                let picked = ogcli_core::common::pick_profile_interactive(
                    &profiles, &config.active, "wms",
                )?;
                config.set_active(&picked)?;
                wms_config::save_config(&config)?;
                println!("✓ Active profile: {picked}");
            }
            // --profile NAME: one-off override
            Some(Some(ref name)) => {
                if !config.has_profile(name) {
                    let names = config.profile_names().join(", ");
                    bail!("no profile named '{name}'. Saved profiles: {names}");
                }
                config.active = name.clone();
            }
            None => {}
        }
    }

    // Bare --profile with no subcommand: the picker already set the
    // active profile; nothing else to do.
    if cli.profile == Some(None) && cli.command.is_none() {
        return Ok(());
    }

    match cli.command.unwrap_or(Command::Status) {
        Command::Init { source, server, save, force } => {
            cmd_init(source, server, save, force, config)
        }
        Command::Status => cmd_status(config),
        Command::Profiles => cmd_profiles(config),
        Command::Set { what } => match what {
            SetWhat::Profile { name } => cmd_set_profile(name),
        },
        Command::Layers { query, limit, all, verbose } => {
            cmd_layers(query, limit, all, verbose, config)
        }
        Command::Describe { query } => cmd_describe(query, config),
        Command::Map(args) => cmd_map(args, config),
        Command::Tile(args) => cmd_tile(args, config),
        Command::Legend { query, output, debug } => cmd_legend(query, output, debug, config),
        Command::Hammer(args) => cmd_hammer(args, config),
        Command::Url { request_type, query, width, height } => {
            cmd_url(request_type, query, width, height, config)
        }
        Command::Groups { pattern, as_tree, counts } => {
            cmd_groups(pattern, as_tree, counts, config)
        }
        Command::Batch(args) => cmd_batch(args, config),
        Command::Cache { what } => match what {
            CacheWhat::Status => cmd_cache_status(config),
            CacheWhat::Clear => {
                if wms_config::clear_all_caches() {
                    println!("✓ WMS caches cleared");
                } else {
                    println!("No WMS cache to clear");
                }
                Ok(())
            }
            CacheWhat::Refresh => {
                let mut config = config;
                wms_config::refresh_active_profile(&mut config)
                    .context("refresh active profile")?;
                let caps = wms_config::get_capabilities(&config)?;
                println!(
                    "✓ Cache refreshed ({} layers)",
                    caps.queryable_layers().len()
                );
                Ok(())
            }
        },
    }
}

fn cmd_init(
    source: Option<String>,
    server: Option<String>,
    save: Option<String>,
    force: bool,
    mut config: WmsConfig,
) -> Result<()> {
    if force && wms_config::clear_all_caches() {
        println!("✓ Cache cleared");
    }
    match source {
        Some(source) => {
            if source.starts_with("http://") || source.starts_with("https://") {
                println!("Fetching capabilities from {source}...");
            } else {
                println!("Loading capabilities from {source}...");
            }
            wms_config::init(&source, server.as_deref(), &mut config, save.as_deref())?;
            println!("✓ Initialized WMS profile '{}'", config.active);
        }
        None => {
            wms_config::refresh_active_profile(&mut config)
                .context("refresh active profile (run 'wms init <source>' first)")?;
            println!("✓ Refreshed profile '{}'", config.active);
        }
    }
    let caps = wms_config::get_capabilities(&config)?;
    println!("  Service: {}", caps.service_title);
    println!("  Layers: {} queryable", caps.queryable_layers().len());
    if !config.server_url().is_empty() {
        println!("  Server: {}", config.server_url());
    }
    Ok(())
}

fn cmd_status(config: WmsConfig) -> Result<()> {
    if !config.is_initialized() {
        println!("Not initialized. Run 'wms init <source>' first.");
        return Ok(());
    }
    let extra = if config.profiles.len() > 1 {
        format!(" ({} saved)", config.profiles.len())
    } else {
        String::new()
    };
    println!("Profile: {}{extra}", config.active);
    println!("Source: {}", config.source_path());
    println!("Type: {}", config.source_type());
    if !config.server_url().is_empty() {
        println!("Server: {}", config.server_url());
    }
    let caps = wms_config::get_capabilities(&config)?;
    println!("Layers: {} queryable", caps.queryable_layers().len());
    println!("Service: {}", caps.service_title);
    Ok(())
}

fn cmd_profiles(config: WmsConfig) -> Result<()> {
    if config.profiles.is_empty() {
        println!("No saved profiles. Save one with: wms init <source> --save NAME");
        return Ok(());
    }
    let mut names = config.profile_names();
    names.sort();
    for name in names {
        let p = &config.profiles[&name];
        let marker = if name == config.active { "*" } else { " " };
        println!("{marker} {name:<20} {}", p.source_path);
    }
    Ok(())
}

fn cmd_set_profile(name: Option<String>) -> Result<()> {
    let mut config = wms_config::load_config();
    let Some(name) = name else {
        let mut names = config.profile_names();
        names.sort();
        for pname in names {
            let p = &config.profiles[&pname];
            let marker = if pname == config.active { "*" } else { " " };
            println!("{marker} {pname:<20} {}", p.source_path);
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
    wms_config::save_config(&config)?;
    let path = config.profile().unwrap().source_path.clone();
    println!("✓ Active profile: {name} ({path})");
    Ok(())
}

fn resolver_for(config: &WmsConfig) -> Result<SmartQueryResolver> {
    let caps = wms_config::get_capabilities(config)?;
    Ok(SmartQueryResolver::new(caps))
}

fn cmd_layers(
    query: Vec<String>,
    limit: usize,
    all: bool,
    verbose: bool,
    config: WmsConfig,
) -> Result<()> {
    if !config.is_initialized() {
        bail!("WMS not initialized. Run 'wms init <source>' first.");
    }
    let resolver = resolver_for(&config)?;
    let query_str = if query.is_empty() {
        None
    } else {
        Some(query.join(" "))
    };

    let results = match &query_str {
        Some(q) => {
            let (_, _, clean) = SmartQueryResolver::extract_tokens(q);
            resolver.search(&clean, 1000)
        }
        None => resolver
            .layers
            .iter()
            .enumerate()
            .take(if all { usize::MAX } else { limit })
            .map(|(i, _)| (0.0, i))
            .collect::<Vec<_>>(),
    };
    let mut results = results;
    if !all && results.len() > limit {
        results.truncate(limit);
    }

    match &query_str {
        Some(q) => println!("Layers matching '{q}':\n"),
        None => println!("Available layers ({} shown):\n", results.len()),
    }
    for (score, i) in &results {
        let layer = &resolver.layers[*i];
        let dims: Vec<&str> =
            layer.dimensions.keys().map(|k| k.as_str()).collect();
        let dim_str = if dims.is_empty() {
            String::new()
        } else {
            format!(" [{}]", dims.join(", "))
        };
        match &query_str {
            Some(_) => println!("  {score:5.1}  {}{dim_str}", layer.name.clone().unwrap_or_default()),
            None => println!("  {}{dim_str}", layer.name.clone().unwrap_or_default()),
        }
        if verbose {
            for (name, dim) in &layer.dimensions {
                let preview: Vec<String> = dim.values.iter().take(5).cloned().collect();
                let more = if dim.values.len() > 5 {
                    format!(" (+{} more)", dim.values.len() - 5)
                } else {
                    String::new()
                };
                println!("        {name}: {}{more}", preview.join(", "));
            }
            if !layer.styles.is_empty() {
                let styles: Vec<&str> =
                    layer.styles.iter().take(3).map(|s| s.name.as_str()).collect();
                let more = if layer.styles.len() > 3 {
                    format!(" (+{} more)", layer.styles.len() - 3)
                } else {
                    String::new()
                };
                println!("        Styles: {}{more}", styles.join(", "));
            }
        }
    }
    Ok(())
}

fn cmd_describe(query: Vec<String>, config: WmsConfig) -> Result<()> {
    if !config.is_initialized() {
        bail!("WMS not initialized. Run 'wms init <source>' first.");
    }
    if query.is_empty() {
        bail!("describe requires a layer query");
    }
    let resolver = resolver_for(&config)?;
    let resolved = resolver
        .resolve(&query.join(" "))
        .map_err(|e| anyhow::anyhow!("{e}"))?;
    let layer = &resolver.layers[resolved.layer];

    println!("Layer: {}", layer.name.clone().unwrap_or_default());
    if !layer.title.is_empty() {
        println!("Title: {}", layer.title);
    }
    if let Some(name) = &layer.name {
        if let Some(model) = name.split('_').next() {
            println!("Model: {model}");
        }
    }
    println!();
    println!("Confidence: {:.0}%", resolved.confidence);

    if !layer.dimensions.is_empty() {
        println!();
        println!("Dimensions ({}):", layer.dimensions.len());
        for (name, dim) in &layer.dimensions {
            println!("  {name}:");
            println!("    Default: {}", dim.default.clone().unwrap_or_default());
            println!("    Values: {} available", dim.values.len());
            if !dim.values.is_empty() {
                if dim.values.len() > 5 {
                    println!("    Range: {} ... {}", dim.values[0], dim.values[dim.values.len() - 1]);
                } else {
                    println!("    Options: {}", dim.values.join(", "));
                }
            }
        }
    }
    if !layer.styles.is_empty() {
        println!();
        println!("Styles ({}):", layer.styles.len());
        for style in &layer.styles {
            let default = if style.name == resolved.style { " (default)" } else { "" };
            println!("  - {}{default}", style.name);
            if !style.title.is_empty() && style.title != style.name {
                println!("      {}", style.title);
            }
        }
    }
    if let Some(bb) = &layer.geographic_bbox {
        println!();
        println!("Geographic Extent:");
        println!("  {}, {} -> {}, {}", bb.minx, bb.miny, bb.maxx, bb.maxy);
    }
    if !layer.crs_list.is_empty() {
        let shown: Vec<&str> = layer.crs_list.iter().take(5).map(|s| s.as_str()).collect();
        println!();
        println!("CRS Options: {}", shown.join(", "));
        if layer.crs_list.len() > 5 {
            println!("  (+{} more)", layer.crs_list.len() - 5);
        }
    }
    Ok(())
}

/// WMS HTTP request helper (GetMap/GetGTile/GetLegendGraphic).
fn wms_get(
    server_url: &str,
    params: &[(String, String)],
) -> Result<Vec<u8>> {
    let client = reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_secs(30))
        .user_agent("wms-cli/0.1-rust")
        .build()?;
    let filtered: Vec<(&str, &str)> = params
        .iter()
        .map(|(k, v)| (k.as_str(), v.as_str()))
        .collect();
    let resp = client.get(server_url).query(&filtered).send()?;
    let status = resp.status();
    if !status.is_success() {
        bail!("HTTP {status} for WMS request");
    }
    Ok(resp.bytes()?.to_vec())
}

fn build_url(server_url: &str, params: &[(String, String)]) -> String {
    let qs: Vec<String> = params
        .iter()
        .map(|(k, v)| format!("{}={}", k, ogcli_core::edr::query::urlencode(v)))
        .collect();
    format!("{server_url}?{}", qs.join("&"))
}

fn cmd_map(args: MapArgs, config: WmsConfig) -> Result<()> {
    if !config.is_initialized() {
        bail!("WMS not initialized. Run 'wms init <source>' first.");
    }
    if args.query.is_empty() {
        bail!("map requires a layer query");
    }
    let resolver = resolver_for(&config)?;
    let resolved = resolver
        .resolve(&args.query.join(" "))
        .map_err(|e| anyhow::anyhow!("{e}"))?;
    let layer = &resolver.layers[resolved.layer];
    let layer_name = layer.name.clone().unwrap_or_default();

    let use_style = args.style.clone().unwrap_or_else(|| resolved.style.clone());
    let use_crs = args.crs.clone().unwrap_or_else(|| resolved.crs.clone());
    let use_bbox: Vec<f64> = match &args.bbox {
        Some(bbox) => {
            let vals: Vec<f64> =
                bbox.split(',').filter_map(|v| v.trim().parse().ok()).collect();
            if vals.len() != 4 {
                bail!("invalid bbox '{bbox}' (use minx,miny,maxx,maxy)");
            }
            vals
        }
        None => vec![resolved.bbox.0, resolved.bbox.1, resolved.bbox.2, resolved.bbox.3],
    };

    println!("-> Resolved: {layer_name}");
    println!("  Style: {use_style}");
    for (dim, value) in &resolved.dimensions {
        println!("  {dim}: {value}");
    }
    if resolved.confidence < 100.0 {
        println!("  (confidence: {:.0}%)", resolved.confidence);
    }
    for w in &resolved.warnings {
        println!("  ! {w}");
    }

    let mut params: Vec<(String, String)> = vec![
        ("SERVICE".into(), "WMS".into()),
        ("VERSION".into(), "1.3.0".into()),
        ("REQUEST".into(), "GetMap".into()),
        ("LAYERS".into(), layer_name.clone()),
        (
            "BBOX".into(),
            use_bbox
                .iter()
                .map(|v| {
                    if *v == v.trunc() && v.abs() < 1e15 {
                        format!("{}", *v as i64)
                    } else {
                        format!("{v}")
                    }
                })
                .collect::<Vec<_>>()
                .join(","),
        ),
        ("WIDTH".into(), args.width.to_string()),
        ("HEIGHT".into(), args.height.to_string()),
        ("CRS".into(), use_crs),
        ("FORMAT".into(), args.img_format.clone()),
        ("STYLES".into(), use_style.clone()),
    ];
    for (dim, value) in &resolved.dimensions {
        params.push((ResolvedQueryHelper::dimension_param_name(dim).to_uppercase(), value.clone()));
    }

    if args.debug {
        println!();
        println!("URL:");
        println!("{}", build_url(config.server_url(), &params));
        return Ok(());
    }

    println!("Fetching...");
    let data = wms_get(config.server_url(), &params)?;
    let output = args.output.clone().unwrap_or_else(|| {
        let mut parts = vec![layer_name.clone(), use_style.clone()];
        for (_, value) in resolved.dimensions.iter().take(3) {
            parts.push(sanitize(value));
        }
        format!("{}.png", parts.join("_"))
    });
    std::fs::write(&output, &data)?;
    println!();
    println!("✓ Saved: {output} ({:.1} KB)", data.len() as f64 / 1024.0);
    Ok(())
}

struct ResolvedQueryHelper;
impl ResolvedQueryHelper {
    fn dimension_param_name(dim: &str) -> String {
        ogcli_core::wms::resolver::ResolvedQuery::dimension_param_name(dim)
    }
}

fn sanitize(name: &str) -> String {
    name.replace(':', "-")
        .replace('/', "-")
        .replace(' ', "_")
        .replace('?', "")
        .replace('&', "_")
}

fn cmd_tile(args: TileArgs, config: WmsConfig) -> Result<()> {
    if !config.is_initialized() {
        bail!("WMS not initialized. Run 'wms init <source>' first.");
    }
    if args.query.is_empty() {
        bail!("tile requires a layer query");
    }
    let resolver = resolver_for(&config)?;
    let resolved = resolver
        .resolve(&args.query.join(" "))
        .map_err(|e| anyhow::anyhow!("{e}"))?;
    let layer = &resolver.layers[resolved.layer];
    let layer_name = layer.name.clone().unwrap_or_default();

    // Random tile coordinates for the zoom level (Python parity)
    let max_tiles = 2u64.pow(args.zoom);
    let seed = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.subsec_nanos() as u64)
        .unwrap_or(4);
    let tilerow = seed % max_tiles;
    let tilecol = (seed / 7) % max_tiles;

    println!("-> Resolved: {layer_name}");
    println!("  Tile: z={}, row={tilerow}, col={tilecol}", args.zoom);
    for (dim, value) in &resolved.dimensions {
        println!("  {dim}: {value}");
    }

    let mut params: Vec<(String, String)> = vec![
        ("SERVICE".into(), "WMS".into()),
        ("VERSION".into(), "1.3.0".into()),
        ("REQUEST".into(), "GetGTile".into()),
        ("LAYER".into(), layer_name.clone()),
        ("TILEZOOM".into(), args.zoom.to_string()),
        ("TILEROW".into(), tilerow.to_string()),
        ("TILECOL".into(), tilecol.to_string()),
        ("CRS".into(), "EPSG:900913".into()),
        ("FORMAT".into(), "image/png; mode=8bit".into()),
        ("STYLE".into(), resolved.style.clone()),
        ("TRANSPARENT".into(), "TRUE".into()),
    ];
    for (dim, value) in &resolved.dimensions {
        params.push((ResolvedQueryHelper::dimension_param_name(dim).to_uppercase(), value.clone()));
    }

    if args.debug {
        println!();
        println!("URL:");
        println!("{}", build_url(config.server_url(), &params));
        return Ok(());
    }

    println!("Fetching...");
    let data = wms_get(config.server_url(), &params)?;
    let output = args.output.clone().unwrap_or_else(|| {
        format!("tile_{layer_name}_z{}.png", args.zoom)
    });
    std::fs::write(&output, &data)?;
    println!();
    println!("✓ Saved: {output} ({:.1} KB)", data.len() as f64 / 1024.0);
    Ok(())
}

fn cmd_legend(
    query: Vec<String>,
    output: Option<String>,
    debug: bool,
    config: WmsConfig,
) -> Result<()> {
    if !config.is_initialized() {
        bail!("WMS not initialized. Run 'wms init <source>' first.");
    }
    if query.is_empty() {
        bail!("legend requires a layer query");
    }
    let resolver = resolver_for(&config)?;
    let resolved = resolver
        .resolve(&query.join(" "))
        .map_err(|e| anyhow::anyhow!("{e}"))?;
    let layer = &resolver.layers[resolved.layer];
    let layer_name = layer.name.clone().unwrap_or_default();

    println!("-> Resolved: {layer_name}");
    println!("  Style: {}", resolved.style);

    let params: Vec<(String, String)> = vec![
        ("SERVICE".into(), "WMS".into()),
        ("VERSION".into(), "1.3.0".into()),
        ("REQUEST".into(), "GetLegendGraphic".into()),
        ("LAYER".into(), layer_name.clone()),
        ("STYLE".into(), resolved.style.clone()),
        ("FORMAT".into(), "image/png".into()),
    ];

    if debug {
        println!();
        println!("URL:");
        println!("{}", build_url(config.server_url(), &params));
        return Ok(());
    }

    println!("Fetching...");
    let data = wms_get(config.server_url(), &params)?;
    let output = output.unwrap_or_else(|| format!("legend_{layer_name}.png"));
    std::fs::write(&output, &data)?;
    println!();
    println!("✓ Saved: {output} ({:.1} KB)", data.len() as f64 / 1024.0);
    Ok(())
}

fn cmd_url(
    request_type: String,
    query: Vec<String>,
    width: i64,
    height: i64,
    config: WmsConfig,
) -> Result<()> {
    if !config.is_initialized() {
        bail!("WMS not initialized. Run 'wms init <source>' first.");
    }
    if !["map", "tile", "legend", "capabilities"].contains(&request_type.as_str()) {
        bail!("unknown request type '{request_type}' (map | tile | legend | capabilities)");
    }
    if request_type == "capabilities" {
        println!(
            "{}",
            build_url(
                config.server_url(),
                &[
                    ("SERVICE".into(), "WMS".into()),
                    ("VERSION".into(), "1.3.0".into()),
                    ("REQUEST".into(), "GetCapabilities".into()),
                ]
            )
        );
        return Ok(());
    }
    if query.is_empty() {
        bail!("{request_type} requires a layer query");
    }

    match request_type.as_str() {
        "map" => cmd_map(
            MapArgs {
                query,
                output: None,
                width,
                height,
                bbox: None,
                crs: None,
                img_format: "image/png".into(),
                style: None,
                debug: true,
            },
            config,
        ),
        "tile" => cmd_tile(
            TileArgs { query, output: None, zoom: 3, debug: true },
            config,
        ),
        _ => cmd_legend(query, None, true, config),
    }
}

fn cmd_cache_status(config: WmsConfig) -> Result<()> {
    let mut names = config.profile_names();
    names.push("__default_check__".into());
    for name in config.profile_names() {
        let path = wms_config::caps_cache_file_for(&name);
        let marker = if name == config.active { "*" } else { " " };
        match std::fs::metadata(&path) {
            Ok(meta) => {
                let age_min = meta
                    .modified()
                    .ok()
                    .and_then(|t| t.elapsed().ok())
                    .map(|d| d.as_secs() / 60)
                    .unwrap_or(0);
                println!(
                    "{marker} {name:<20} {} KB  {age_min} min old",
                    meta.len() / 1024
                );
            }
            Err(_) => println!("{marker} {name:<20} no cache"),
        }
    }
    let _ = names;
    Ok(())
}

fn cmd_groups(
    pattern: Option<String>,
    as_tree: bool,
    counts: bool,
    config: WmsConfig,
) -> Result<()> {
    if !config.is_initialized() {
        bail!("WMS not initialized. Run 'wms init <source>' first.");
    }
    let resolver = resolver_for(&config)?;
    let queryable = resolver.capabilities.queryable_layers();
    let groups = layer_groups(&queryable, pattern.as_deref());
    if groups.is_empty() {
        let suffix = pattern
            .as_ref()
            .map(|p| format!(" matching '{p}'"))
            .unwrap_or_default();
        println!("No groups found{suffix}");
        return Ok(());
    }
    if as_tree {
        for (group, layers) in &groups {
            if counts {
                println!("{group} ({} layers)", layers.len());
            } else {
                println!("{group}");
            }
            for name in layers.iter().take(5) {
                println!("  └── {name}");
            }
            if layers.len() > 5 {
                println!("  └── ... (+{} more)", layers.len() - 5);
            }
        }
    } else {
        for (group, layers) in &groups {
            if counts {
                println!("{group}: {} layers", layers.len());
            } else {
                println!("{group}");
            }
        }
    }
    Ok(())
}

fn cmd_hammer(args: HammerArgs, config: WmsConfig) -> Result<()> {
    if !config.is_initialized() {
        bail!("WMS not initialized. Run 'wms init <source>' first.");
    }
    if args.query.is_empty() {
        bail!("hammer requires a layer query");
    }
    let resolver = resolver_for(&config)?;
    let query_str = args.query.join(" ");
    let (_, _, clean) = SmartQueryResolver::extract_tokens(&query_str);
    let results = resolver.search(&clean, 100);
    if results.is_empty() {
        bail!("No layers found matching: {query_str}");
    }
    let matching: Vec<usize> = results
        .into_iter()
        .filter(|(score, _)| *score > 50.0)
        .map(|(_, i)| i)
        .collect();
    if matching.is_empty() {
        bail!("No confident matches for: {query_str}\nTry a more specific query.");
    }

    println!("-> Found {} layers matching '{query_str}':", matching.len());
    for &i in matching.iter().take(10) {
        println!("  - {}", resolver.layers[i].name.clone().unwrap_or_default());
    }
    if matching.len() > 10 {
        println!("  ... and {} more", matching.len() - 10);
    }

    let layers: Vec<&ogcli_core::wms::parser::Layer> =
        matching.iter().map(|&i| &resolver.layers[i]).collect();
    let mut hammer = WmsHammer::new(config.server_url(), layers);
    hammer.workers = args.workers;
    hammer.output_dir = std::path::PathBuf::from(&args.output);
    hammer.limit = args.limit.unwrap_or(0);
    hammer.generate();

    println!();
    println!("Total requests: {}", hammer.requests.len());
    if hammer.requests.is_empty() {
        return Ok(());
    }

    if args.dry_run {
        println!("(dry run - no requests will be made)");
        println!();
        println!("Sample requests (first 5):");
        for (i, req) in hammer.requests.iter().enumerate().take(5) {
            println!(" {}. {}", i + 1, req.layer);
            println!("    Style: {}", req.style);
            let dims = req
                .dimensions
                .iter()
                .map(|(k, v)| format!("{k}={v}"))
                .collect::<Vec<_>>()
                .join(", ");
            println!("    Dimensions: {}", if dims.is_empty() { "none" } else { &dims });
        }
        if hammer.requests.len() > 5 {
            println!("    ... and {} more requests", hammer.requests.len() - 5);
        }
        return Ok(());
    }

    std::fs::create_dir_all(&hammer.output_dir)?;
    println!("Saving images to: {}", hammer.output_dir.display());
    println!();
    println!("Hammering with {} workers...", args.workers);
    let stats = hammer.run()?;

    let pct = |n: usize| {
        if stats.total > 0 {
            n as f64 / stats.total as f64 * 100.0
        } else {
            0.0
        }
    };
    let dur = stats.duration_s();
    println!("{}", "=".repeat(72));
    println!("SUMMARY");
    println!("{}", "=".repeat(72));
    println!("Total requests:     {}", stats.total);
    println!("Successful:         {} ({:.1}%)", stats.success, pct(stats.success));
    println!("Failed:             {} ({:.1}%)", stats.failed, pct(stats.failed));
    println!("Time elapsed:       {dur:.2}s");
    if dur > 0.0 {
        println!("Requests/sec:       {:.2}", stats.total as f64 / dur);
    }
    println!("Total size:         {:.2} MB", stats.bytes as f64 / 1024.0 / 1024.0);
    println!("Output directory:   {}", hammer.output_dir.display());
    Ok(())
}

fn cmd_batch(args: BatchArgs, config: WmsConfig) -> Result<()> {
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Mutex;

    if !config.is_initialized() {
        bail!("WMS not initialized. Run 'wms init <source>' first.");
    }
    if args.query.is_empty() {
        bail!("batch requires a layer query");
    }
    let resolver = resolver_for(&config)?;
    let query_str = args.query.join(" ");
    let (_, _, clean) = SmartQueryResolver::extract_tokens(&query_str);
    let results = resolver.search(&clean, args.limit.unwrap_or(100));
    if results.is_empty() {
        bail!("No layers found matching: {query_str}");
    }
    let matching: Vec<(f64, usize)> =
        results.into_iter().filter(|(score, _)| *score > 50.0).collect();
    if matching.is_empty() {
        bail!("No confident matches for: {query_str}\nTry a more specific query.");
    }

    if args.list_only {
        println!("Layers matching '{query_str}' ({}):\n", matching.len());
        for (score, i) in &matching {
            println!("  {score:5.1}  {}", resolver.layers[*i].name.clone().unwrap_or_default());
        }
        return Ok(());
    }

    println!("-> Found {} layers matching '{query_str}'", matching.len());
    if args.dry_run {
        println!();
        println!("Would download to: {}/", args.output);
        for (_, i) in matching.iter().take(10) {
            println!("  - {}", resolver.layers[*i].name.clone().unwrap_or_default());
        }
        if matching.len() > 10 {
            println!("  ... and {} more", matching.len() - 10);
        }
        return Ok(());
    }

    let output_path = std::path::Path::new(&args.output);
    std::fs::create_dir_all(output_path)?;
    println!("Downloading to: {}/", args.output);
    println!("Workers: {}", args.workers);

    // Resolve everything up front (smart defaults: latest run, ...).
    struct Job {
        layer: String,
        params: Vec<(String, String)>,
    }
    let mut jobs: Vec<Job> = Vec::new();
    let mut failed = 0usize;
    for (_, i) in &matching {
        let name = resolver.layers[*i].name.clone().unwrap_or_default();
        match resolver.resolve(&name) {
            Ok(resolved) => {
                let mut params: Vec<(String, String)> = vec![
                    ("SERVICE".into(), "WMS".into()),
                    ("VERSION".into(), "1.3.0".into()),
                    ("REQUEST".into(), "GetMap".into()),
                    ("LAYERS".into(), name.clone()),
                    (
                        "BBOX".into(),
                        format!(
                            "{},{},{},{}",
                            resolved.bbox.0, resolved.bbox.1, resolved.bbox.2, resolved.bbox.3
                        ),
                    ),
                    ("WIDTH".into(), "800".into()),
                    ("HEIGHT".into(), "600".into()),
                    ("CRS".into(), resolved.crs.clone()),
                    ("FORMAT".into(), "image/png".into()),
                    ("STYLES".into(), resolved.style.clone()),
                ];
                for (dim, value) in &resolved.dimensions {
                    params.push((
                        ResolvedQueryHelper::dimension_param_name(dim).to_uppercase(),
                        value.clone(),
                    ));
                }
                jobs.push(Job { layer: name, params });
            }
            Err(e) => {
                eprintln!("  ✗ {name}: {e}");
                failed += 1;
            }
        }
    }

    let client = reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_secs(30))
        .user_agent("wms-cli/0.1-rust")
        .build()?;

    use indicatif::{ProgressBar, ProgressStyle};
    let bar = ProgressBar::new(jobs.len() as u64);
    bar.set_style(
        ProgressStyle::with_template(
            "{spinner:.cyan} Downloading {wide_bar:.cyan} {pos}/{len} [{elapsed_precise}]",
        )
        .unwrap()
        .progress_chars("█░"),
    );

    let next = AtomicUsize::new(0);
    let counts = Mutex::new((0usize, 0usize)); // (ok, fail)
    std::thread::scope(|scope| {
        for _ in 0..args.workers.max(1) {
            scope.spawn(|| loop {
                let i = next.fetch_add(1, Ordering::Relaxed);
                if i >= jobs.len() {
                    break;
                }
                let job = &jobs[i];
                let outcome = (|| -> Result<Vec<u8>> {
                    let filtered: Vec<(&str, &str)> = job
                        .params
                        .iter()
                        .map(|(k, v)| (k.as_str(), v.as_str()))
                        .collect();
                    let resp = client.get(config.server_url()).query(&filtered).send()?;
                    let status = resp.status();
                    if !status.is_success() {
                        bail!("{}: HTTP {status}", job.layer);
                    }
                    Ok(resp.bytes()?.to_vec())
                })();
                let ok = match outcome {
                    Ok(data) => {
                        let path = output_path.join(format!("{}.png", job.layer));
                        std::fs::write(&path, &data)
                            .map(|_| true)
                            .map_err(|e| anyhow::anyhow!("{}: {e}", job.layer))
                            .is_ok()
                    }
                    Err(e) => {
                        eprintln!("  ✗ {e:#}");
                        false
                    }
                };
                let mut c = counts.lock().unwrap();
                if ok {
                    c.0 += 1;
                } else {
                    c.1 += 1;
                }
                drop(c);
                bar.inc(1);
            });
        }
    });
    bar.finish_and_clear();

    let (success, fail) = counts.into_inner().unwrap();
    let failed = failed + fail;
    println!();
    println!("✓ Downloaded {success} layers");
    if failed > 0 {
        println!("✗ Failed: {failed}");
    }
    Ok(())
}
