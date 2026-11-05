//! Query building: resolve a fuzzy query + CLI options into concrete
//! EDR request parameters. Mirrors `run_edr_query` in the Python CLI.

use anyhow::{bail, Result};

use crate::edr::parser::EdrCollection;
use crate::edr::resolver::{bbox_to_wkt, parse_coords, EdrResolver, ResolvedEdr};

/// CLI options shared by all query commands.
#[derive(Debug, Clone, Default)]
pub struct QueryOptions {
    pub coords: Option<String>,
    pub bbox: Option<String>,
    pub datetime: Option<String>,
    pub z: Option<String>,
    pub param: Option<String>,
    pub within: Option<String>,
    pub width: Option<String>,
    pub instance: Option<String>,
    pub crs: Option<String>,
    pub format: Option<String>,
}

/// A fully-resolved EDR query, ready to send (or print as a URL).
#[derive(Debug, Clone, Default)]
pub struct BuiltQuery {
    pub collection_id: String,
    pub query_type: String,
    pub instance: Option<String>,
    pub params: Vec<(String, String)>,
    pub warnings: Vec<String>,
    /// Resolved values for display
    pub datetime: Option<String>,
    pub z: Option<String>,
    pub param: Option<String>,
}

pub fn validate_bbox(bbox: &str) -> Result<Vec<f64>> {
    let vals: Vec<f64> = bbox
        .split(',')
        .filter_map(|p| p.trim().parse().ok())
        .collect();
    if vals.len() != 4 || vals[0] >= vals[2] || vals[1] >= vals[3] {
        bail!("invalid bbox '{bbox}' (use minx,miny,maxx,maxy)");
    }
    Ok(vals)
}

/// Build a query from a resolved fuzzy match and CLI options.
pub fn build_query(
    collection: &EdrCollection,
    resolved: &ResolvedEdr,
    query_type: &str,
    opts: &QueryOptions,
    resolver: &EdrResolver,
) -> Result<BuiltQuery> {
    if !collection.data_queries.is_empty() && !collection.supports(query_type) {
        bail!(
            "collection '{}' does not support {query_type}; supported: {}",
            collection.id,
            collection.data_queries.join(", ")
        );
    }

    let mut warnings = resolved.warnings.clone();
    let mut params: Vec<(String, String)> = Vec::new();

    // ---- geometry ----
    match query_type {
        "cube" => {
            // Cube queries use bbox=minx,miny,maxx,maxy (not WKT)
            if let Some(bbox) = &opts.bbox {
                validate_bbox(bbox)?;
                params.push(("bbox".into(), bbox.clone()));
            } else if let Some(bbox) = &collection.bbox {
                if bbox.len() == 4 {
                    let s = bbox
                        .iter()
                        .map(|v| format_num(*v))
                        .collect::<Vec<_>>()
                        .join(",");
                    params.push(("bbox".into(), s));
                    warnings.push("bbox: using full collection extent".into());
                } else {
                    bail!("cube requires --bbox (collection has no extent)");
                }
            } else {
                bail!("cube requires --bbox (collection has no extent)");
            }
        }
        _ => {
            if let Some(bbox) = &opts.bbox {
                let Some(wkt) = bbox_to_wkt(bbox) else {
                    bail!("invalid bbox '{bbox}' (use minx,miny,maxx,maxy)");
                };
                params.push(("coords".into(), wkt));
            } else if let Some(coords) = &opts.coords {
                let (wkt, cwarn) = parse_coords(coords)?;
                warnings.extend(cwarn);
                params.push(("coords".into(), wkt));
            } else if query_type == "position" || query_type == "radius" {
                match collection.center() {
                    Some((lon, lat)) => {
                        params.push((
                            "coords".into(),
                            format!("POINT({} {})", format_num(lon), format_num(lat)),
                        ));
                        warnings.push(format!(
                            "coords: using collection center {},{}",
                            format_num(lon),
                            format_num(lat)
                        ));
                    }
                    None => bail!("{query_type} requires --coords"),
                }
            } else {
                bail!("{query_type} requires --coords (or --bbox for area)");
            }
        }
    }

    // ---- datetime (option wins over extracted token) ----
    let dt_expr = opts
        .datetime
        .clone()
        .or_else(|| resolved.extracted.get("datetime").cloned());
    let mut dt_value = None;
    if let Some(expr) = dt_expr {
        let (value, dwarn) = resolver.resolve_datetime(&expr, collection);
        warnings.extend(dwarn);
        if let Some(v) = value {
            params.push(("datetime".into(), v.clone()));
            dt_value = Some(v);
        }
    }

    // ---- z (option wins over extracted token) ----
    let z_expr = opts
        .z
        .clone()
        .or_else(|| resolved.extracted.get("z").cloned());
    let mut z_value = None;
    if let Some(expr) = z_expr {
        let (value, zwarn) = resolver.resolve_z(&expr, collection);
        warnings.extend(zwarn);
        if let Some(v) = value {
            params.push(("z".into(), v.clone()));
            z_value = Some(v);
        }
    }
    // ---- parameter filter ----
    let param_value = opts.param.clone().or_else(|| resolved.parameter.clone());
    if let Some(p) = &param_value {
        params.push(("parameter-name".into(), p.clone()));
    }

    // ---- extras ----
    if let Some(within) = &opts.within {
        params.push(("within".into(), within.clone()));
    }
    if let Some(width) = &opts.width {
        for (k, v) in crate::edr::client::corridor_params(width) {
            params.push((k, v));
        }
    }
    if let Some(crs) = &opts.crs {
        params.push(("crs".into(), crs.clone()));
    }
    if let Some(format) = &opts.format {
        params.push(("f".into(), format.clone()));
    }

    Ok(BuiltQuery {
        collection_id: collection.id.clone(),
        query_type: query_type.to_string(),
        instance: opts.instance.clone(),
        params,
        warnings,
        datetime: dt_value,
        z: z_value,
        param: param_value,
    })
}

/// Format a coordinate number without trailing .0 when integral
/// (POINT(-105 40), not POINT(-105.0 40.0)).
pub fn format_num(v: f64) -> String {
    if v == v.trunc() && v.abs() < 1e15 {
        format!("{}", v as i64)
    } else {
        format!("{v}")
    }
}

/// Build the full URL for a BuiltQuery (for `edr url` and --debug).
pub fn build_url(base_url: &str, query: &BuiltQuery, api_key: &str) -> String {
    let mut url = format!(
        "{}/collections/{}",
        base_url.trim_end_matches('/'),
        query.collection_id
    );
    if let Some(instance) = &query.instance {
        url.push_str(&format!("/instances/{instance}"));
    }
    url.push_str(&format!("/{}", query.query_type));

    let mut pairs: Vec<(String, String)> = query.params.clone();
    if !api_key.is_empty() {
        pairs.push(("apikey".into(), api_key.to_string()));
    }
    if pairs.is_empty() {
        return url;
    }
    let query_string: Vec<String> = pairs
        .iter()
        .map(|(k, v)| format!("{}={}", urlencode(k), urlencode(v)))
        .collect();
    format!("{url}?{}", query_string.join("&"))
}

/// Minimal percent-encoding for query values (spaces, parens, commas
/// and reserved characters), matching urlencode's default behavior
/// closely enough for display and requests.
pub fn urlencode(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(b as char)
            }
            _ => out.push_str(&format!("%{b:02X}")),
        }
    }
    out
}
