//! ogcli MCP server - Rust port of src/mcp_server.py.
//!
//! Exposes the same 15 wms/edr tools over stdio using the same core
//! as the CLIs (ogcli-core) and the same ~/.wms config/profiles/caches.
//!
//! Register:
//!   claude mcp add ogcli -- /path/to/rust/target/release/ogcli-mcp

use std::path::PathBuf;

use anyhow::Result;
use base64::Engine;
use rmcp::model::{
    CallToolResult, Content, Implementation, ProtocolVersion, ServerCapabilities,
    ServerInfo,
};
use rmcp::service::serve_server;
use rmcp::handler::server::tool::ToolRouter;
use rmcp::handler::server::wrapper::Parameters;
use schemars::JsonSchema;
use serde::Deserialize;
use rmcp::{
    tool, tool_handler, tool_router,
    ErrorData as McpError, ServerHandler,
};
use rmcp::transport::io::stdio;

use ogcli_core::edr::config as ec;
use ogcli_core::edr::parser::summarize_coverage;
use ogcli_core::edr::query::{build_query, QueryOptions};
use ogcli_core::edr::resolver::EdrResolver;
use ogcli_core::wms::config as wc;
use ogcli_core::wms::resolver::{ResolvedQuery, SmartQueryResolver};

const HAMMER_MAX_REQUESTS: usize = 500;
const HAMMER_MAX_WORKERS: usize = 64;
const MAX_JSON_CHARS: usize = 50_000;

const INSTRUCTIONS: &str = "ogcli provides two OGC weather-geospatial services:

WMS (Web Map Service) - map images rendered per layer:
- Layers are named like GFS_Temperature_F or GALWEM_CloudBase
- Natural dimension syntax: 'f'/'c' (units), '6h' (forecast hours),
  '12z' (model run cycle), '850' (pressure level)

EDR (Environmental Data Retrieval) - point/area data as JSON:
- Collections are named like hrrr-surface or nbm-conus-2m-agl
- Fuzzy queries work for both: 'hrrr surface temp' resolves the
  collection AND the parameter (TMP)

Tips:
- Start with edr_collections / wms_layers to see what exists
- edr_query returns a SUMMARY of the coverage; the full payload is
  saved to disk and the path is included in the response
- WMS queries with tight bounding boxes are faster and cheaper

Hammer etiquette (load testing):
- The hammer tools generate real traffic against real servers.
- ALWAYS call with dry_run=true first to preview URLs and count.
- Confirm with the user before sending more than ~100 requests,
  and never exceed what they asked for.
- Responses are not saved by default; enable save only when asked.";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn mcp_err(e: anyhow::Error) -> McpError {
    McpError::internal_error(format!("{e:#}"), None)
}

fn invalid(msg: String) -> McpError {
    McpError::invalid_params(msg, None)
}

fn dump(value: &serde_json::Value) -> String {
    let text = serde_json::to_string_pretty(value).unwrap_or_default();
    if text.len() > MAX_JSON_CHARS {
        format!(
            "{text}\n...[truncated at {MAX_JSON_CHARS} chars - refine the query or read the saved file]"
        )
    } else {
        text
    }
}

fn text_result(value: serde_json::Value) -> CallToolResult {
    CallToolResult::success(vec![Content::text(dump(&value))])
}

/// Build "base?k=v&k=v" from raw params (percent-encoded).
fn url_with_params(base: &str, params: &[(String, String)]) -> String {
    use ogcli_core::edr::query::urlencode;
    let qs: Vec<String> = params
        .iter()
        .filter(|(_, v)| !v.is_empty())
        .map(|(k, v)| format!("{}={}", urlencode(k), urlencode(v)))
        .collect();
    if qs.is_empty() {
        base.to_string()
    } else {
        format!("{base}?{}", qs.join("&"))
    }
}

fn output_dir() -> PathBuf {
    ogcli_core::common::config_dir().join("mcp_output")
}

fn save_payload(name: &str, data: &[u8], ext: &str) -> Result<PathBuf> {
    let dir = output_dir();
    std::fs::create_dir_all(&dir)?;
    let name = name.replace(['/', ':'], "-");
    let path = dir.join(format!("{name}.{ext}"));
    std::fs::write(&path, data)?;
    Ok(path)
}

fn clamp_hammer(limit: usize, workers: usize) -> (usize, usize, Vec<String>) {
    let mut notes = Vec::new();
    let limit = if limit > HAMMER_MAX_REQUESTS {
        notes.push(format!("limit clamped to {HAMMER_MAX_REQUESTS} (safety cap)"));
        HAMMER_MAX_REQUESTS
    } else {
        limit
    };
    let workers = if workers > HAMMER_MAX_WORKERS {
        notes.push(format!("workers clamped to {HAMMER_MAX_WORKERS} (safety cap)"));
        HAMMER_MAX_WORKERS
    } else {
        workers
    };
    (limit, workers, notes)
}

fn edr_context(profile: Option<&str>) -> Result<(ec::EdrConfig, EdrResolver)> {
    let mut config = ec::load_config();
    if let Some(name) = profile.filter(|p| !p.is_empty()) {
        if !config.has_profile(name) {
            let names = config.profile_names().join(", ");
            return Err(anyhow::anyhow!(
                "No EDR profile named '{name}'; saved: {names}"
            ));
        }
        config.active = name.to_string();
    }
    if !config.is_initialized() {
        anyhow::bail!(
            "EDR not initialized. Call the edr_init tool first with the server \
             URL (e.g. https://folkweather.com/edr)."
        );
    }
    let caps = ec::get_capabilities(&config)?;
    Ok((config, EdrResolver::new(caps)))
}

fn wms_context(profile: Option<&str>) -> Result<(wc::WmsConfig, SmartQueryResolver)> {
    let mut config = wc::load_config();
    if let Some(name) = profile.filter(|p| !p.is_empty()) {
        if !config.has_profile(name) {
            let names = config.profile_names().join(", ");
            return Err(anyhow::anyhow!(
                "No WMS profile named '{name}'; saved: {names}"
            ));
        }
        config.active = name.to_string();
    }
    if !config.is_initialized() {
        anyhow::bail!(
            "WMS not initialized. Call the wms_init tool first with the server \
             URL (e.g. https://ogc.shyftwx.com/ogc/WMS)."
        );
    }
    let caps = wc::get_capabilities(&config)?;
    Ok((config, SmartQueryResolver::new(caps)))
}

fn resolve_edr<'a>(
    resolver: &'a EdrResolver,
    query: &str,
) -> std::result::Result<ogcli_core::edr::resolver::ResolvedEdr, McpError> {
    use ogcli_core::edr::resolver::AmbiguousCollectionError;
    resolver.resolve(query).map_err(|e| {
        let alts = e
            .downcast_ref::<AmbiguousCollectionError>()
            .map(|a| {
                if a.alternatives.is_empty() {
                    "(none)".to_string()
                } else {
                    a.alternatives.join(", ")
                }
            })
            .unwrap_or_default();
        if alts.is_empty() {
            invalid(format!("{e:#}"))
        } else {
            invalid(format!("{e} Alternatives: {alts}"))
        }
    })
}

fn resolve_wms<'a>(
    resolver: &'a SmartQueryResolver,
    query: &str,
) -> std::result::Result<ResolvedQuery, McpError> {
    resolver.resolve(query).map_err(|e| invalid(e.to_string()))
}

fn summary_json(body: &[u8]) -> serde_json::Value {
    match summarize_coverage(body) {
        None => serde_json::json!({"note": "non-JSON response"}),
        Some(s) => serde_json::json!({
            "type": s.coverage_type,
            "axes": s.axes,
            "parameters": s.parameters,
        }),
    }
}

async fn run_blocking<T, F>(f: F) -> std::result::Result<T, McpError>
where
    T: Send + 'static,
    F: FnOnce() -> Result<T> + Send + 'static,
{
    tokio::task::spawn_blocking(f)
        .await
        .map_err(|e| McpError::internal_error(format!("join error: {e}"), None))?
        .map_err(mcp_err)
}

/// Map a tool error to a model-visible isError result (matching the
/// Python MCP behavior) rather than a protocol-level error.
async fn tool_call<T, F>(f: F) -> Result<CallToolResult, McpError>
where
    T: ToolOutput + Send + 'static,
    F: FnOnce() -> Result<T> + Send + 'static,
{
    match run_blocking(f).await {
        Ok(value) => Ok(value.into_call_tool_result()),
        Err(e) => Ok(CallToolResult::error(vec![Content::text(e.message.to_string())])),
    }
}

/// Tools produce either a plain CallToolResult or a JSON value dump.
trait ToolOutput {
    fn into_call_tool_result(self) -> CallToolResult;
}

impl ToolOutput for CallToolResult {
    fn into_call_tool_result(self) -> CallToolResult {
        self
    }
}

impl ToolOutput for serde_json::Value {
    fn into_call_tool_result(self) -> CallToolResult {
        text_result(self)
    }
}

// ---------------------------------------------------------------------------
// Tool argument structs (rmcp tools take Parameters<T>)
// ---------------------------------------------------------------------------

#[derive(Debug, Default, Deserialize, JsonSchema)]
struct EdrProfileArgs {
    #[serde(default)]
    profile: Option<String>,
}

#[derive(Debug, Default, Deserialize, JsonSchema)]
struct EdrInitArgs {
    url: String,
    #[serde(default)]
    api_key: Option<String>,
}

#[derive(Debug, Default, Deserialize, JsonSchema)]
struct EdrCollectionsArgs {
    #[serde(default)]
    query: Option<String>,
    #[serde(default)]
    limit: Option<usize>,
    #[serde(default)]
    profile: Option<String>,
}

#[derive(Debug, Default, Deserialize, JsonSchema)]
struct EdrDescribeArgs {
    query: String,
    #[serde(default)]
    profile: Option<String>,
}

#[derive(Debug, Default, Deserialize, JsonSchema)]
struct EdrQueryArgs {
    query: String,
    #[serde(default)]
    query_type: Option<String>,
    #[serde(default)]
    coords: Option<String>,
    #[serde(default)]
    bbox: Option<String>,
    #[serde(default)]
    datetime: Option<String>,
    #[serde(default)]
    z: Option<String>,
    #[serde(default)]
    param: Option<String>,
    #[serde(default)]
    within: Option<String>,
    #[serde(default)]
    width: Option<String>,
    #[serde(default)]
    instance: Option<String>,
    #[serde(default)]
    save: Option<bool>,
    #[serde(default)]
    profile: Option<String>,
}

#[derive(Debug, Default, Deserialize, JsonSchema)]
struct EdrInstancesArgs {
    query: String,
    #[serde(default)]
    limit: Option<usize>,
    #[serde(default)]
    profile: Option<String>,
}

#[derive(Debug, Default, Deserialize, JsonSchema)]
struct EdrLocationsArgs {
    query: String,
    #[serde(default)]
    location_id: Option<String>,
    #[serde(default)]
    limit: Option<usize>,
    #[serde(default)]
    profile: Option<String>,
}

#[derive(Debug, Default, Deserialize, JsonSchema)]
struct EdrHammerArgs {
    query: String,
    #[serde(default)]
    dry_run: Option<bool>,
    #[serde(default)]
    limit: Option<usize>,
    #[serde(default)]
    workers: Option<usize>,
    #[serde(default)]
    query_type: Option<String>,
    #[serde(default)]
    max_timesteps: Option<usize>,
    #[serde(default)]
    max_params: Option<usize>,
    #[serde(default)]
    max_levels: Option<usize>,
    #[serde(default)]
    points: Option<usize>,
    #[serde(default)]
    save: Option<bool>,
    #[serde(default)]
    profile: Option<String>,
}

#[derive(Debug, Default, Deserialize, JsonSchema)]
struct WmsInitArgs {
    url: String,
}

#[derive(Debug, Default, Deserialize, JsonSchema)]
struct WmsLayersArgs {
    #[serde(default)]
    query: Option<String>,
    #[serde(default)]
    limit: Option<usize>,
    #[serde(default)]
    profile: Option<String>,
}

#[derive(Debug, Default, Deserialize, JsonSchema)]
struct WmsDescribeArgs {
    query: String,
    #[serde(default)]
    profile: Option<String>,
}

#[derive(Debug, Default, Deserialize, JsonSchema)]
struct WmsMapArgs {
    query: String,
    #[serde(default)]
    width: Option<i64>,
    #[serde(default)]
    height: Option<i64>,
    #[serde(default)]
    bbox: Option<String>,
    #[serde(default)]
    image: Option<bool>,
    #[serde(default)]
    profile: Option<String>,
}

#[derive(Debug, Default, Deserialize, JsonSchema)]
struct WmsUrlArgs {
    query: String,
    #[serde(default)]
    request_type: Option<String>,
    #[serde(default)]
    width: Option<i64>,
    #[serde(default)]
    height: Option<i64>,
    #[serde(default)]
    profile: Option<String>,
}

#[derive(Debug, Default, Deserialize, JsonSchema)]
struct WmsHammerArgs {
    query: String,
    #[serde(default)]
    dry_run: Option<bool>,
    #[serde(default)]
    limit: Option<usize>,
    #[serde(default)]
    workers: Option<usize>,
    #[serde(default)]
    profile: Option<String>,
}

// ---------------------------------------------------------------------------
// Server
// ---------------------------------------------------------------------------

#[derive(Debug, Clone)]
struct OgcliServer {
    tool_router: ToolRouter<Self>,
}

#[tool_router]
impl OgcliServer {
    fn new() -> Self {
        Self {
            tool_router: Self::tool_router(),
        }
    }

    // -- EDR ---------------------------------------------------------

    #[tool(description = "Show the current EDR server configuration and collection summary.")]
    async fn edr_status(params: Parameters<EdrProfileArgs>) -> Result<CallToolResult, McpError> {
        let EdrProfileArgs { profile } = params.0;
        tool_call(move || {
            let config = ec::load_config();
            if let Some(name) = profile.as_deref().filter(|p| !p.is_empty()) {
                if !config.has_profile(name) {
                    return Err(anyhow::anyhow!(
                        "No EDR profile named '{name}'; saved: {}",
                        config.profile_names().join(", ")
                    ));
                }
            }
            if !config.is_initialized() {
                return Ok(text_result(serde_json::json!({
                    "initialized": false,
                    "hint": "call edr_init with a server URL first"
                })));
            }
            let caps = ec::get_capabilities(&config)?;
            Ok(text_result(serde_json::json!({
                "initialized": true,
                "profile": config.active,
                "server": config.server_url(),
                "n_collections": caps.collections.len(),
                "query_types": caps.query_types(),
            })))
        }).await
    }

    #[tool(description = "Initialize the EDR context from a server URL (fetches /collections). \
                          Changes the shared ogcli EDR config (~/.wms/edr_config.json) used by \
                          both this server and the edr CLI.")]
    async fn edr_init(params: Parameters<EdrInitArgs>) -> Result<CallToolResult, McpError> {
        let EdrInitArgs { url, api_key } = params.0;
        tool_call(move || {
            let mut config = ec::load_config();
            ec::init_from_server(&url, api_key.as_deref().unwrap_or(""), &mut config, None)?;
            let caps = ec::get_capabilities(&config)?;
            Ok(text_result(serde_json::json!({
                "initialized": true,
                "profile": config.active,
                "server": config.server_url(),
                "n_collections": caps.collections.len(),
                "query_types": caps.query_types(),
            })))
        }).await
    }

    #[tool(description = "List EDR collections, optionally fuzzy-filtered. The query matches \
                          collection ids, titles, and parameter names, e.g. 'hrrr surface', \
                          'guam', 'temp'.")]
    async fn edr_collections(params: Parameters<EdrCollectionsArgs>) -> Result<CallToolResult, McpError> {
        let EdrCollectionsArgs { query, limit, profile } = params.0;
        tool_call(move || {
            let limit = limit.unwrap_or(20);
            let (_, resolver) = edr_context(profile.as_deref())?;
            let results = resolver.list_collections(query.as_deref(), limit);
            let collections: Vec<serde_json::Value> = results
                .iter()
                .map(|(i, score)| {
                    let c = resolver.collection(*i);
                    serde_json::json!({
                        "id": c.id,
                        "title": c.title,
                        "score": (score * 10.0).round() / 10.0,
                        "query_types": c.data_queries,
                        "parameters": c.parameter_names.keys().collect::<Vec<_>>(),
                        "timesteps": c.n_timesteps(),
                        "levels": c.n_levels(),
                        "temporal": [c.temporal_start(), c.temporal_end()],
                        "bbox": c.bbox,
                    })
                })
                .collect();
            Ok(text_result(serde_json::json!({
                "query": query,
                "count": collections.len(),
                "collections": collections,
            })))
        }).await
    }

    #[tool(description = "Show full details of one EDR collection (fuzzy-matched): extent, \
                          temporal/vertical values, parameters with units, and query types.")]
    async fn edr_describe(params: Parameters<EdrDescribeArgs>) -> Result<CallToolResult, McpError> {
        let EdrDescribeArgs { query, profile } = params.0;
        tool_call(move || {
            let (_, resolver) = edr_context(profile.as_deref())?;
            let resolved = resolve_edr(&resolver, &query)?;
            let c = resolver.collection(resolved.collection);
            let mut params = serde_json::Map::new();
            for (name, p) in &c.parameter_names {
                params.insert(
                    name.clone(),
                    serde_json::json!({"label": p.label, "unit": p.unit}),
                );
            }
            Ok(text_result(serde_json::json!({
                "id": c.id,
                "title": c.title,
                "description": c.description,
                "confidence": resolved.confidence.round(),
                "query_types": c.data_queries,
                "bbox": c.bbox,
                "crs": c.crs,
                "temporal": {
                    "start": c.temporal_start(),
                    "end": c.temporal_end(),
                    "n_timesteps": c.n_timesteps(),
                    "sample_values": c.temporal_values.iter().take(10).collect::<Vec<_>>(),
                },
                "vertical": {
                    "values": c.vertical_values.iter().take(30).collect::<Vec<_>>(),
                    "unit": c.vertical_unit,
                },
                "parameters": params,
            })))
        }).await
    }

    #[tool(description = "Run an EDR query and return a compact summary of the data. \
                          query_type: position | radius | area | cube | trajectory | corridor. \
                          coords: 'lon,lat' or 'lon1,lat1;lon2,lat2' or WKT. datetime: 'latest', \
                          '+6h', ISO instant, or 'a/b'. z: '850', '850mb', or 'low/high'. \
                          param: parameter name(s). within: radius e.g. '25km'. width: corridor \
                          width e.g. '10km'. Returns axes + per-parameter value ranges; the full \
                          payload is saved to disk when save=true and the path is included.")]
    async fn edr_query(params: Parameters<EdrQueryArgs>) -> Result<CallToolResult, McpError> {
        let EdrQueryArgs { query, query_type, coords, bbox, datetime, z, param, within, width, instance, save, profile } = params.0;
        tool_call(move || {
            let query_type = query_type.unwrap_or_else(|| "position".into());
            let save = save.unwrap_or(true);
            let (config, resolver) = edr_context(profile.as_deref())?;
            let resolved = resolve_edr(&resolver, &query)?;
            let collection = resolver.collection(resolved.collection).clone();

            let opts = QueryOptions {
                coords,
                bbox,
                datetime,
                z,
                param,
                within,
                width,
                instance: instance.clone(),
                crs: None,
                format: None,
            };
            let built = build_query(&collection, &resolved, &query_type, &opts, &resolver)
                .map_err(|e| invalid(format!("{e:#}")))?;

            let client = ogcli_core::edr::client::EdrClient::new(
                config.server_url(),
                config.api_key(),
            );
            let (body, content_type) = client.query(
                &built.collection_id,
                &built.query_type,
                built.instance.as_deref(),
                &built.params,
            )?;

            let mut result = serde_json::json!({
                "collection": built.collection_id,
                "query_type": built.query_type,
                "params": serde_json::Map::from_iter(
                    built.params.iter().filter(|(_, v)| !v.is_empty())
                        .map(|(k, v)| (k.clone(), serde_json::json!(v)))
                ),
                "content_type": content_type,
                "bytes": body.len(),
                "warnings": built.warnings,
                "summary": summary_json(&body),
            });
            if save {
                let ext = if content_type.to_lowercase().contains("json") {
                    "json"
                } else {
                    "bin"
                };
                let path = save_payload(
                    &format!("{}_{}", built.collection_id, built.query_type),
                    &body,
                    ext,
                )?;
                result["saved_to"] = serde_json::json!(path);
            }
            Ok(text_result(result))
        }).await
    }

    #[tool(description = "List model-run instances (e.g. individual HRRR runs) for a collection. \
                          Use an instance id in edr_query's instance parameter.")]
    async fn edr_instances(params: Parameters<EdrInstancesArgs>) -> Result<CallToolResult, McpError> {
        let EdrInstancesArgs { query, limit, profile } = params.0;
        tool_call(move || {
            let limit = limit.unwrap_or(20);
            let (config, resolver) = edr_context(profile.as_deref())?;
            let resolved = resolve_edr(&resolver, &query)?;
            let c = resolver.collection(resolved.collection);
            let client =
                ogcli_core::edr::client::EdrClient::new(config.server_url(), config.api_key());
            let data = client.get_instances(&c.id)?;
            let empty = Vec::new();
            let list = data
                .get("instances")
                .and_then(|v| v.as_array())
                .unwrap_or(&empty);
            let instances: Vec<serde_json::Value> = list
                .iter()
                .take(limit)
                .map(|i| {
                    serde_json::json!({
                        "id": i.get("id").and_then(|v| v.as_str()),
                        "title": i.get("title").and_then(|v| v.as_str()).unwrap_or(""),
                    })
                })
                .collect();
            Ok(text_result(serde_json::json!({
                "collection": c.id,
                "count": list.len(),
                "instances": instances,
            })))
        }).await
    }

    #[tool(description = "List named locations for a collection (stations, cities, ...). \
                          With location_id, fetches the data at that location instead.")]
    async fn edr_locations(params: Parameters<EdrLocationsArgs>) -> Result<CallToolResult, McpError> {
        let EdrLocationsArgs { query, location_id, limit, profile } = params.0;
        tool_call(move || {
            let limit = limit.unwrap_or(20);
            let (config, resolver) = edr_context(profile.as_deref())?;
            let resolved = resolve_edr(&resolver, &query)?;
            let c = resolver.collection(resolved.collection).clone();
            let client =
                ogcli_core::edr::client::EdrClient::new(config.server_url(), config.api_key());

            if let Some(location_id) = location_id {
                let (body, ctype) = client.get_location(&c.id, &location_id, &[])?;
                let path = save_payload(
                    &format!("{}_{}", c.id, location_id),
                    &body,
                    if ctype.to_lowercase().contains("json") { "json" } else { "bin" },
                )?;
                return Ok(text_result(serde_json::json!({
                    "collection": c.id,
                    "location": location_id,
                    "content_type": ctype,
                    "bytes": body.len(),
                    "summary": summary_json(&body),
                    "saved_to": path,
                })));
            }

            let data = client.get_locations(&c.id)?;
            let empty = Vec::new();
            let features = data
                .get("features")
                .and_then(|v| v.as_array())
                .unwrap_or(&empty);
            let locations: Vec<serde_json::Value> = features
                .iter()
                .take(limit)
                .map(|f| {
                    serde_json::json!({
                        "id": f.get("id").and_then(|v| v.as_str()),
                        "name": f.get("properties")
                            .and_then(|p| p.get("name"))
                            .and_then(|v| v.as_str())
                            .unwrap_or(""),
                        "coords": f.get("geometry")
                            .and_then(|g| g.get("coordinates")),
                    })
                })
                .collect();
            Ok(text_result(serde_json::json!({
                "collection": c.id,
                "count": features.len(),
                "locations": locations,
            })))
        }).await
    }

    #[tool(description = "Load-test EDR collections with a full dimension sweep \
                          (collections x parameters x timesteps x z-levels x random points). \
                          SAFETY: dry_run=true (the default) previews URLs without sending \
                          anything. Confirm the count looks reasonable, then re-call with \
                          dry_run=false. Ask the user before runs larger than ~100 requests. \
                          Limits are capped at 500 requests / 64 workers. Returns per-run \
                          stats: ok/failed counts, throughput, latency percentiles, HTTP \
                          status breakdown.")]
    async fn edr_hammer(params: Parameters<EdrHammerArgs>) -> Result<CallToolResult, McpError> {
        let EdrHammerArgs { query, dry_run, limit, workers, query_type, max_timesteps, max_params, max_levels, points, save, profile } = params.0;
        tool_call(move || {
            let dry_run = dry_run.unwrap_or(true);
            let (limit, workers, notes) =
                clamp_hammer(limit.unwrap_or(100), workers.unwrap_or(20));
            let query_type = query_type.unwrap_or_else(|| "position".into());

            let (config, resolver) = edr_context(profile.as_deref())?;
            let matching: Vec<usize> = resolver
                .list_collections(Some(&query), 200)
                .into_iter()
                .filter(|(_, score)| *score > 50.0)
                .map(|(i, _)| i)
                .collect();
            if matching.is_empty() {
                return Err(anyhow::anyhow!("No collections match '{query}'"));
            }

            let mut collections = resolver.capabilities.collections.clone();
            let ids: Vec<String> =
                matching.iter().map(|&i| collections[i].id.clone()).collect();
            collections.retain(|c| ids.contains(&c.id));

            let mut hammer = ogcli_core::edr::hammer::EdrHammer::new(
                config.server_url(),
                config.api_key(),
                &collections,
            );
            hammer.workers = workers;
            hammer.query_type = query_type.clone();
            hammer.output_dir = output_dir().join("hammer");
            hammer.save = save.unwrap_or(false);
            hammer.dry_run = dry_run;
            hammer.max_timesteps = max_timesteps.unwrap_or(0);
            hammer.max_params = max_params.unwrap_or(0);
            hammer.max_levels = max_levels.unwrap_or(0);
            hammer.points = points.unwrap_or(1);
            hammer.limit = limit;
            hammer.generate();

            let collection_names: Vec<&String> =
                collections.iter().map(|c| &c.id).collect();

            if dry_run {
                let sample: Vec<String> = hammer
                    .requests
                    .iter()
                    .take(5)
                    .map(|r| {
                        let qs: Vec<String> = r
                            .params
                            .iter()
                            .filter(|(_, v)| !v.is_empty())
                            .map(|(k, v)| format!("{k}={v}"))
                            .collect();
                        format!("{}?{}", r.url, qs.join("&"))
                    })
                    .collect();
                return Ok(text_result(serde_json::json!({
                    "dry_run": true,
                    "note": "preview only - re-call with dry_run=false to send",
                    "collections": collection_names,
                    "total_requests": hammer.requests.len(),
                    "sample_urls": sample,
                    "notes": notes,
                })));
            }

            let stats = hammer.run()?;
            Ok(text_result(serde_json::json!({
                "collections": collection_names,
                "total_requests": stats.total,
                "ok": stats.success,
                "failed": stats.failed,
                "status_breakdown": stats.status_counts,
                "duration_s": (stats.duration_s() * 100.0).round() / 100.0,
                "req_per_s": (stats.total as f64 / stats.duration_s() * 10.0).round() / 10.0,
                
                "notes": notes,
            })))
        }).await
    }

    // -- WMS ---------------------------------------------------------

    #[tool(description = "Show the current WMS server configuration and layer summary.")]
    async fn wms_status(params: Parameters<EdrProfileArgs>) -> Result<CallToolResult, McpError> {
        let EdrProfileArgs { profile } = params.0;
        tool_call(move || {
            let mut config = wc::load_config();
            if let Some(name) = profile.as_deref().filter(|p| !p.is_empty()) {
                if !config.has_profile(name) {
                    return Err(anyhow::anyhow!(
                        "No WMS profile named '{name}'; saved: {}",
                        config.profile_names().join(", ")
                    ));
                }
                config.active = name.to_string();
            }
            if !config.is_initialized() {
                return Ok(text_result(serde_json::json!({
                    "initialized": false,
                    "hint": "call wms_init with a server URL first"
                })));
            }
            let caps = wc::get_capabilities(&config)?;
            Ok(text_result(serde_json::json!({
                "initialized": true,
                "profile": config.active,
                "server": config.server_url(),
                "service": caps.service_title,
                "n_layers": caps.queryable_layers().len(),
            })))
        }).await
    }

    #[tool(description = "Initialize the WMS context from a server URL (fetches \
                          GetCapabilities). Changes the shared ogcli WMS config (~/.wms/).")]
    async fn wms_init(params: Parameters<WmsInitArgs>) -> Result<CallToolResult, McpError> {
        let WmsInitArgs { url } = params.0;
        tool_call(move || {
            let mut config = wc::load_config();
            wc::init(&url, None, &mut config, None)?;
            let caps = wc::get_capabilities(&config)?;
            Ok(text_result(serde_json::json!({
                "initialized": true,
                "profile": config.active,
                "server": config.server_url(),
                "service": caps.service_title,
                "n_layers": caps.queryable_layers().len(),
            })))
        }).await
    }

    #[tool(description = "List WMS layers, optionally fuzzy-filtered ('gfs temp', 'cloud'). \
                          Layer names encode model + parameter (e.g. GFS_Temperature_F); \
                          common dimensions are RUN/FORECAST/ELEVATION.")]
    async fn wms_layers(params: Parameters<WmsLayersArgs>) -> Result<CallToolResult, McpError> {
        let WmsLayersArgs { query, limit, profile } = params.0;
        tool_call(move || {
            let limit = limit.unwrap_or(20);
            let (_, resolver) = wms_context(profile.as_deref())?;
            let results = match query.as_deref().filter(|q| !q.is_empty()) {
                Some(q) => {
                    let (_, _, clean) = SmartQueryResolver::extract_tokens(q);
                    resolver.search(&clean, limit)
                }
                None => resolver
                    .layers
                    .iter()
                    .enumerate()
                    .take(limit)
                    .map(|(i, _)| (0.0, i))
                    .collect(),
            };
            let layers: Vec<serde_json::Value> = results
                .iter()
                .map(|(score, i)| {
                    let l = &resolver.layers[*i];
                    serde_json::json!({
                        "name": l.name,
                        "title": l.title,
                        "score": (score * 10.0).round() / 10.0,
                        "dimensions": l.dimensions.keys().collect::<Vec<_>>(),
                        "styles": l.styles.iter().map(|s| s.name.clone()).collect::<Vec<_>>(),
                    })
                })
                .collect();
            Ok(text_result(serde_json::json!({
                "query": query,
                "count": layers.len(),
                "layers": layers,
            })))
        }).await
    }

    #[tool(description = "Show full details of one WMS layer (fuzzy-matched): dimensions \
                          with available values, styles, bbox, CRS options.")]
    async fn wms_describe(params: Parameters<WmsDescribeArgs>) -> Result<CallToolResult, McpError> {
        let WmsDescribeArgs { query, profile } = params.0;
        tool_call(move || {
            let (_, resolver) = wms_context(profile.as_deref())?;
            let resolved = resolve_wms(&resolver, &query)?;
            let layer = &resolver.layers[resolved.layer];
            let dims: serde_json::Map<String, serde_json::Value> = layer
                .dimensions
                .iter()
                .map(|(name, dim)| {
                    (
                        name.clone(),
                        serde_json::json!({
                            "default": dim.default,
                            "n_values": dim.values.len(),
                            "sample_values": dim.values.iter().take(10).collect::<Vec<_>>(),
                        }),
                    )
                })
                .collect();
            Ok(text_result(serde_json::json!({
                "name": layer.name,
                "title": layer.title,
                "abstract": layer.abstract_text,
                "confidence": resolved.confidence.round(),
                "dimensions": dims,
                "styles": layer.styles.iter().map(|s| serde_json::json!({
                    "name": s.name, "title": s.title,
                })).collect::<Vec<_>>(),
                "bbox": layer.geographic_bbox.as_ref().map(|bb| {
                    vec![bb.minx, bb.miny, bb.maxx, bb.maxy]
                }),
                "crs": layer.crs_list.iter().take(10).collect::<Vec<_>>(),
            })))
        }).await
    }

    #[tool(description = "Fetch a WMS map image (GetMap) and return it inline as a PNG the \
                          model can view, plus resolution details. query: fuzzy layer match, \
                          e.g. 'gfs temp f 6h' (natural dimension syntax supported: f/c units, \
                          6h forecast, 12z run, 850 level). bbox: 'minx,miny,maxx,maxy' \
                          (default: layer extent). The image is also saved to \
                          ~/.wms/mcp_output/ and the path is in the text part.")]
    async fn wms_map(params: Parameters<WmsMapArgs>) -> Result<CallToolResult, McpError> {
        let WmsMapArgs { query, width, height, bbox, image, profile } = params.0;
        tool_call(move || {
            let width = width.unwrap_or(800);
            let height = height.unwrap_or(600);
            let include_image = image.unwrap_or(true);
            let (config, resolver) = wms_context(profile.as_deref())?;
            let resolved = resolve_wms(&resolver, &query)?;
            let layer = &resolver.layers[resolved.layer];
            let layer_name = layer.name.clone().unwrap_or_default();

            let use_bbox: Vec<f64> = match bbox.as_deref() {
                Some(b) => {
                    let vals: Vec<f64> =
                        b.split(',').filter_map(|v| v.trim().parse().ok()).collect();
                    if vals.len() != 4 {
                        return Err(anyhow::anyhow!(
                            "invalid bbox '{b}' (use minx,miny,maxx,maxy)"
                        ));
                    }
                    vals
                }
                None => vec![
                    resolved.bbox.0, resolved.bbox.1, resolved.bbox.2, resolved.bbox.3,
                ],
            };

            let mut params: Vec<(String, String)> = vec![
                ("SERVICE".into(), "WMS".into()),
                ("VERSION".into(), "1.3.0".into()),
                ("REQUEST".into(), "GetMap".into()),
                ("LAYERS".into(), layer_name.clone()),
                ("BBOX".into(), use_bbox.iter().map(|v| format!("{v}")).collect::<Vec<_>>().join(",")),
                ("WIDTH".into(), width.to_string()),
                ("HEIGHT".into(), height.to_string()),
                ("CRS".into(), resolved.crs.clone()),
                ("FORMAT".into(), "image/png".into()),
                ("STYLES".into(), resolved.style.clone()),
            ];
            for (dim, value) in &resolved.dimensions {
                params.push((ResolvedQuery::dimension_param_name(dim).to_uppercase(), value.clone()));
            }

            let url = url_with_params(config.server_url(), &params);
            let client = reqwest::blocking::Client::builder()
                .timeout(std::time::Duration::from_secs(30))
                .build()?;
            let resp = client
                .get(&url)
                .send()
                .map_err(|e| anyhow::anyhow!("fetching map: {e}"))?;
            let status = resp.status();
            if !status.is_success() {
                return Err(anyhow::anyhow!("HTTP {status} fetching map"));
            }
            let png = resp.bytes()?.to_vec();
            let saved = save_payload(&format!("map_{layer_name}"), &png, "png")?;

            let info = serde_json::json!({
                "layer": layer_name,
                "style": resolved.style,
                "dimensions": serde_json::Map::from_iter(
                    resolved.dimensions.iter().map(|(k, v)| (k.clone(), serde_json::json!(v)))
                ),
                "bbox": use_bbox,
                "size": [width, height],
                "bytes": png.len(),
                "saved_to": saved,
                "confidence": resolved.confidence.round(),
                "warnings": resolved.warnings,
            });

            let mut content = vec![Content::text(dump(&info))];
            if include_image {
                let b64 = base64::engine::general_purpose::STANDARD.encode(&png);
                content.push(Content::image(b64, "image/png"));
            }
            Ok(CallToolResult::success(content))
        }).await
    }

    #[tool(description = "Build a WMS request URL without fetching (map | tile | legend).")]
    async fn wms_url(params: Parameters<WmsUrlArgs>) -> Result<CallToolResult, McpError> {
        let WmsUrlArgs { query, request_type, width, height, profile } = params.0;
        tool_call(move || {
            let request_type = request_type.unwrap_or_else(|| "map".into());
            let width = width.unwrap_or(800);
            let height = height.unwrap_or(600);
            let (config, resolver) = wms_context(profile.as_deref())?;
            let resolved = resolve_wms(&resolver, &query)?;
            let layer = &resolver.layers[resolved.layer];
            let layer_name = layer.name.clone().unwrap_or_default();
            let mut params: Vec<(String, String)> = match request_type.as_str() {
                "map" => vec![
                    ("SERVICE".into(), "WMS".into()),
                    ("VERSION".into(), "1.3.0".into()),
                    ("REQUEST".into(), "GetMap".into()),
                    ("LAYERS".into(), layer_name.clone()),
                    ("BBOX".into(), format!("{},{},{},{}", resolved.bbox.0, resolved.bbox.1, resolved.bbox.2, resolved.bbox.3)),
                    ("WIDTH".into(), width.to_string()),
                    ("HEIGHT".into(), height.to_string()),
                    ("CRS".into(), resolved.crs.clone()),
                    ("FORMAT".into(), "image/png".into()),
                    ("STYLES".into(), resolved.style.clone()),
                ],
                "tile" => vec![
                    ("SERVICE".into(), "WMS".into()),
                    ("VERSION".into(), "1.3.0".into()),
                    ("REQUEST".into(), "GetGTile".into()),
                    ("LAYER".into(), layer_name.clone()),
                    ("TILEZOOM".into(), "3".into()),
                    ("TILEROW".into(), "4".into()),
                    ("TILECOL".into(), "4".into()),
                    ("CRS".into(), "EPSG:900913".into()),
                    ("FORMAT".into(), "image/png".into()),
                    ("STYLE".into(), resolved.style.clone()),
                ],
                "legend" => vec![
                    ("SERVICE".into(), "WMS".into()),
                    ("VERSION".into(), "1.3.0".into()),
                    ("REQUEST".into(), "GetLegendGraphic".into()),
                    ("LAYER".into(), layer_name.clone()),
                    ("STYLE".into(), resolved.style.clone()),
                    ("FORMAT".into(), "image/png".into()),
                ],
                other => anyhow::bail!("unknown request_type '{other}'"),
            };
            if matches!(request_type.as_str(), "map" | "tile") {
                for (dim, value) in &resolved.dimensions {
                    params.push((ResolvedQuery::dimension_param_name(dim).to_uppercase(), value.clone()));
                }
            }
            Ok(text_result(serde_json::json!({
                "url": url_with_params(config.server_url(), &params),
            })))
        }).await
    }

    #[tool(description = "Load-test WMS layers matching a query (GetMap sweep across dimension \
                          combinations). SAFETY: dry_run=true (the default) previews without \
                          sending anything. Confirm the count, then re-call with dry_run=false. \
                          Ask the user before runs larger than ~100 requests. Capped at 500 \
                          requests / 64 workers.")]
    async fn wms_hammer(params: Parameters<WmsHammerArgs>) -> Result<CallToolResult, McpError> {
        let WmsHammerArgs { query, dry_run, limit, workers, profile } = params.0;
        tool_call(move || {
            let dry_run = dry_run.unwrap_or(true);
            let (limit, workers, notes) =
                clamp_hammer(limit.unwrap_or(100), workers.unwrap_or(20));
            let (config, resolver) = wms_context(profile.as_deref())?;

            let (_, _, clean) = SmartQueryResolver::extract_tokens(&query);
            let matching: Vec<usize> = resolver
                .search(&clean, 100)
                .into_iter()
                .filter(|(score, _)| *score > 50.0)
                .map(|(_, i)| i)
                .collect();
            if matching.is_empty() {
                return Err(anyhow::anyhow!("No layers match '{query}'"));
            }

            // Generate GetMap requests: latest RUN x each FORECAST value
            // (x each ELEVATION value), default style, layer extent.
            struct Req {
                url: String,
                layer: String,
                dimensions: serde_json::Value,
            }
            let client = reqwest::blocking::Client::builder()
                .timeout(std::time::Duration::from_secs(30))
                .build()?;
            let mut requests: Vec<Req> = Vec::new();
            for &i in &matching {
                let layer = &resolver.layers[i];
                let layer_name = layer.name.clone().unwrap_or_default();
                let resolved = resolver
                    .resolve(&layer_name)
                    .map_err(|e| anyhow::anyhow!("{e}"))?;
                let mut combos: Vec<Vec<(String, String)>> = vec![Vec::new()];
                for dim_name in ["RUN", "FORECAST", "ELEVATION", "TIME"] {
                    let Some(dim) = layer.dimensions.get(dim_name) else {
                        continue;
                    };
                    let values: Vec<String> = if dim_name == "RUN" {
                        dim.values.iter().max().cloned().into_iter().collect()
                    } else if dim.values.is_empty() {
                        vec![]
                    } else {
                        dim.values.clone()
                    };
                    if values.is_empty() {
                        continue;
                    }
                    let mut next = Vec::new();
                    for combo in &combos {
                        for v in &values {
                            let mut c = combo.clone();
                            c.push((dim_name.to_string(), v.clone()));
                            next.push(c);
                        }
                    }
                    combos = next;
                }
                for combo in combos {
                    let mut params: Vec<(String, String)> = vec![
                        ("SERVICE".into(), "WMS".into()),
                        ("VERSION".into(), "1.3.0".into()),
                        ("REQUEST".into(), "GetMap".into()),
                        ("LAYERS".into(), layer_name.clone()),
                        ("BBOX".into(), format!(
                            "{},{},{},{}",
                            resolved.bbox.0, resolved.bbox.1, resolved.bbox.2, resolved.bbox.3
                        )),
                        ("WIDTH".into(), "800".into()),
                        ("HEIGHT".into(), "600".into()),
                        ("CRS".into(), resolved.crs.clone()),
                        ("FORMAT".into(), "image/png".into()),
                        ("STYLES".into(), resolved.style.clone()),
                    ];
                    let mut dims = serde_json::Map::new();
                    for (dim, value) in &combo {
                        params.push((
                            ResolvedQuery::dimension_param_name(dim).to_uppercase(),
                            value.clone(),
                        ));
                        dims.insert(dim.clone(), serde_json::json!(value));
                    }
                    requests.push(Req {
                        url: url_with_params(config.server_url(), &params),
                        layer: layer_name.clone(),
                        dimensions: serde_json::Value::Object(dims),
                    });
                }
            }
            let truncated = requests.len() > limit;
            requests.truncate(limit);

            let layer_names: Vec<&String> =
                matching.iter().map(|&i| resolver.layers[i].name.as_ref().unwrap()).collect();

            if dry_run {
                return Ok(text_result(serde_json::json!({
                    "dry_run": true,
                    "note": "preview only - re-call with dry_run=false to send",
                    "layers": layer_names,
                    "total_requests": requests.len(),
                    "truncated_to_limit": truncated,
                    "sample_requests": requests.iter().take(5).map(|r| serde_json::json!({
                        "layer": r.layer, "dimensions": r.dimensions,
                    })).collect::<Vec<_>>(),
                    "notes": notes,
                })));
            }

            // Execute with a simple thread pool
            use std::sync::atomic::{AtomicUsize, Ordering};
            use std::sync::Mutex;
            let next = AtomicUsize::new(0);
            let stats = Mutex::new((0usize, 0usize, 0u64)); // ok, failed, bytes
            let start = std::time::Instant::now();
            std::thread::scope(|scope| {
                for _ in 0..workers {
                    scope.spawn(|| loop {
                        let i = next.fetch_add(1, Ordering::Relaxed);
                        if i >= requests.len() {
                            break;
                        }
                        let req = &requests[i];
                        if let Ok(resp) = client.get(&req.url).send() {
                            let ok = resp.status().is_success();
                            let bytes = resp.bytes().map(|b| b.len() as u64).unwrap_or(0);
                            let mut st = stats.lock().unwrap();
                            if ok {
                                st.0 += 1;
                            } else {
                                st.1 += 1;
                            }
                            st.2 += bytes;
                        } else {
                            stats.lock().unwrap().1 += 1;
                        }
                    });
                }
            });
            let (ok, failed, bytes) = stats.into_inner().unwrap();
            let duration = start.elapsed().as_secs_f64();
            Ok(text_result(serde_json::json!({
                "layers": layer_names,
                "total_requests": requests.len(),
                "ok": ok,
                "failed": failed,
                "duration_s": (duration * 100.0).round() / 100.0,
                "req_per_s": (requests.len() as f64 / duration * 10.0).round() / 10.0,
                "data_kb": (bytes as f64 / 1024.0 * 10.0).round() / 10.0,
                "truncated_to_limit": truncated,
                "notes": notes,
            })))
        }).await
    }
}

#[tool_handler]
impl ServerHandler for OgcliServer {
    fn get_info(&self) -> ServerInfo {
        ServerInfo {
            protocol_version: ProtocolVersion::V_2025_06_18,
            capabilities: ServerCapabilities::builder().enable_tools().build(),
            server_info: Implementation {
                name: "ogcli".into(),
                title: Some("ogcli (wms + edr)".into()),
                version: env!("CARGO_PKG_VERSION").into(),
                ..Default::default()
            },
            instructions: Some(INSTRUCTIONS.into()),
            ..Default::default()
        }
    }
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let service = serve_server(OgcliServer::new(), stdio()).await?;
    service.waiting().await?;
    Ok(())
}
