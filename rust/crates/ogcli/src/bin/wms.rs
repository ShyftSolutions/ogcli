//! wms - WMS query tool with fuzzy search. Rust port of the Python CLI.

use std::process::ExitCode;

use anyhow::{bail, Context, Result};
use clap::{Parser, Subcommand};

use ogcli_core::wms::config::{self as wms_config, WmsConfig};
use ogcli_core::wms::resolver::SmartQueryResolver;

#[derive(Parser)]
#[command(
    name = "wms",
    about = "WMS query tool with fuzzy search.",
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
    /// Build a WMS URL without fetching
    Url {
        request_type: String,
        query: Vec<String>,
        #[arg(short = 'w', long, default_value_t = 800)]
        width: i64,
        #[arg(short = 'h', long, default_value_t = 600)]
        height: i64,
    },
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
    let mut config = WmsConfig::new();
    if cli.command.is_some() {
        config = wms_config::load_config();
        if let Some(name) = &cli.profile {
            if !config.has_profile(name) {
                let names = config.profile_names().join(", ");
                bail!("no profile named '{name}'. Saved profiles: {names}");
            }
            config.active = name.clone();
        }
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
        Command::Url { request_type, query, width, height } => {
            cmd_url(request_type, query, width, height, config)
        }
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
