//! WMS smart query resolver - port of `src/wms/resolver.py` +
//! `fuzzy.py` + `dimensions.py` (two-phase resolution with natural
//! dimension syntax and unit preference).

use std::sync::OnceLock;

use anyhow::{bail, Result};
use chrono::Utc;

use crate::fuzz;
use crate::wms::parser::{BoundingBox, Layer, WmsCapabilities};

/// Result of smart query resolution.
#[derive(Debug, Clone)]
pub struct ResolvedQuery {
    pub layer: usize, // index into the resolver's flat queryable layer list
    pub style: String,
    pub dimensions: Vec<(String, String)>,
    pub crs: String,
    pub bbox: (f64, f64, f64, f64),
    pub confidence: f64,
    pub warnings: Vec<String>,
    pub alternatives: Vec<String>,
}

impl ResolvedQuery {
    /// WMS parameter name for a dimension (TIME/ELEVATION unprefixed).
    pub fn dimension_param_name(dim_name: &str) -> String {
        if dim_name == "TIME" || dim_name == "ELEVATION" {
            dim_name.to_string()
        } else {
            format!("DIM_{dim_name}")
        }
    }
}

#[derive(Debug, thiserror::Error)]
#[error("{message}")]
pub struct AmbiguousQueryError {
    pub message: String,
    pub alternatives: Vec<String>,
}

/// Common weather model prefixes (fuzzy scoring bonus).
const MODEL_PATTERNS: &[&str] = &[
    "GFS", "GALWEM", "HRRR", "NAM", "RAP", "GEFS", "NAEFS",
    "ECMWF", "UKMET", "NAVGEM", "COAMPS", "WRF", "RTMA",
    "URMA", "NDFD", "NBM", "HREF", "SREF", "GEPS", "CMC",
    "FNMOC", "NOGAPS", "ENS", "MESO", "UPPER_AIR",
];

/// Forecast shortcut table (natural expr -> ISO duration).
fn forecast_shortcuts() -> &'static std::collections::HashMap<&'static str, &'static str> {
    static SHORTCUTS: OnceLock<std::collections::HashMap<&'static str, &'static str>> =
        OnceLock::new();
    SHORTCUTS.get_or_init(|| {
        let mut m = std::collections::HashMap::new();
        for (k, v) in [
            ("0h", "PT0S"), ("1h", "PT1H"), ("3h", "PT3H"), ("6h", "PT6H"),
            ("12h", "PT12H"), ("24h", "P1D"), ("48h", "P2D"),
            ("2d", "P2D"), ("3d", "P3D"), ("4d", "P4D"), ("5d", "P5D"),
        ] {
            m.insert(k, v);
        }
        m
    })
}

fn dimension_re() -> &'static regex::Regex {
    static RE: OnceLock<regex::Regex> = OnceLock::new();
    RE.get_or_init(|| {
        regex::Regex::new(
            r"(?i)\b(\d{1,2}z)\b|\b(latest)\b|\+?(\d+h)\b|\+?(PT\d+H)\b|\+?(\d+d)\b|\+?(P\d+D)\b",
        )
        .unwrap()
    })
}

fn elevation_re() -> &'static regex::Regex {
    static RE: OnceLock<regex::Regex> = OnceLock::new();
    RE.get_or_init(|| regex::Regex::new(r"(?i)\b(\d{3,5}(?:mb|hpa)?)\b").unwrap())
}

fn unit_re(unit: char) -> &'static regex::Regex {
    static F: OnceLock<regex::Regex> = OnceLock::new();
    static C: OnceLock<regex::Regex> = OnceLock::new();
    static K: OnceLock<regex::Regex> = OnceLock::new();
    match unit {
        'f' => F.get_or_init(|| regex::Regex::new(r"(?i)\b(f|fahrenheit)\b").unwrap()),
        'c' => C.get_or_init(|| regex::Regex::new(r"(?i)\b(c|celsius)\b").unwrap()),
        _ => K.get_or_init(|| regex::Regex::new(r"(?i)\b(k|kelvin)\b").unwrap()),
    }
}

/// Tokenize a layer name/title (underscores, camelCase, digit bounds).
fn tokenize(text: &str) -> Vec<String> {
    crate::edr::resolver::tokenize(text)
}

fn extract_model(name: &str) -> Option<String> {
    let upper = name.to_uppercase();
    MODEL_PATTERNS
        .iter()
        .find(|m| upper.contains(*m))
        .map(|m| m.to_string())
}

pub struct SmartQueryResolver {
    pub capabilities: WmsCapabilities,
    pub confidence_threshold: f64,
    /// Flat queryable layers.
    pub layers: Vec<Layer>,
    tokens: Vec<Vec<String>>,
    search_text: Vec<String>,
    models: Vec<Option<String>>,
    names: Vec<String>,
}

impl SmartQueryResolver {
    pub fn new(capabilities: WmsCapabilities) -> Self {
        let layers: Vec<Layer> = capabilities
            .queryable_layers()
            .into_iter()
            .cloned()
            .collect();
        let mut tokens = Vec::with_capacity(layers.len());
        let mut search_text = Vec::with_capacity(layers.len());
        let mut models = Vec::with_capacity(layers.len());
        for layer in &layers {
            let mut t = tokenize(layer.name.as_deref().unwrap_or(""));
            if !layer.title.is_empty() && layer.title != layer.name.as_deref().unwrap_or("") {
                t.extend(tokenize(&layer.title));
            }
            search_text.push(t.join(" ").to_lowercase());
            models.push(extract_model(layer.name.as_deref().unwrap_or("")));
            tokens.push(t);
        }
        let names = layers
            .iter()
            .map(|l| l.name.clone().unwrap_or_default())
            .collect();
        SmartQueryResolver {
            capabilities,
            confidence_threshold: 80.0,
            layers,
            tokens,
            search_text,
            models,
            names,
        }
    }

    /// Search layers, best first (score, layer index).
    pub fn search(&self, query: &str, limit: usize) -> Vec<(f64, usize)> {
        let query_tokens = tokenize(query);
        if query_tokens.is_empty() {
            return Vec::new();
        }
        let query_lower = query_tokens.join(" ");
        let mut results: Vec<(f64, usize)> = self
            .layers
            .iter()
            .enumerate()
            .map(|(i, _)| (self.score(&query_tokens, &query_lower, i), i))
            .filter(|(score, _)| *score > 0.0)
            .collect();
        results.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));
        results.truncate(limit);
        results
    }

    fn score(&self, query_tokens: &[String], query: &str, idx: usize) -> f64 {
        let tokens = &self.tokens[idx];
        let mut matches = 0.0;
        for qt in query_tokens {
            let mut best: f64 = 0.0;
            for it in tokens {
                if qt == it {
                    best = 1.0;
                    break;
                }
                if it.starts_with(qt.as_str()) && qt.chars().count() >= 2 {
                    best = best.max(0.9);
                } else if qt.starts_with(it.as_str()) && it.chars().count() >= 2 {
                    best = best.max(0.8);
                } else if qt.chars().count() >= 3 && it.chars().count() >= 3 {
                    let r = fuzz::ratio(qt, it);
                    if r > 80.0 {
                        best = best.max(r / 100.0 * 0.7);
                    }
                }
            }
            matches += best;
        }
        let mut token_score = matches / query_tokens.len() as f64 * 100.0;
        if !query_tokens.is_empty()
            && query_tokens
                .iter()
                .all(|qt| tokens.iter().any(|it| layer_token_match(qt, it)))
        {
            token_score = (token_score + 5.0).min(100.0);
        }

        // WMS uses partial ratio at 0.9 weight (heavier than EDR)
        let partial = fuzz::partial_ratio(query, &self.search_text[idx]) * 0.9;
        let mut score = token_score.max(partial);

        // Exact name substring bonus
        let upper_name = self.names[idx].to_uppercase();
        if upper_name.contains(&query.replace(' ', "_").to_uppercase()) {
            score = (score + 10.0).min(100.0);
        }
        // Model match at start bonus
        if let Some(model) = &self.models[idx] {
            if let Some(first) = query_tokens.first() {
                if model.to_lowercase().starts_with(first) {
                    score = (score + 5.0).min(100.0);
                }
            }
        }
        score
    }

    /// Resolve a user query to valid WMS request parameters.
    pub fn resolve(&self, query: &str) -> Result<ResolvedQuery> {
        let (dims, units, clean) = SmartQueryResolver::extract_tokens(query);

        let mut results = self.search(&clean, 10);
        if results.is_empty() {
            bail!(AmbiguousQueryError {
                message: format!("No layers found matching '{clean}'"),
                alternatives: vec![],
            });
        }

        // Unit preference: layers matching the unit suffix first
        if !units.is_empty() {
            let suffixes: Vec<Vec<&str>> = units
                .iter()
                .map(|u| match u.as_str() {
                    "fahrenheit" => vec!["_F", "_Fahrenheit", "Fahrenheit"],
                    "celsius" => vec!["_C", "_Celsius", "Celsius"],
                    _ => vec!["_K", "_Kelvin", "Kelvin"],
                })
                .collect();
            let mut matching = Vec::new();
            let mut non_matching = Vec::new();
            for r in results {
                let name = &self.names[r.1];
                let hit = suffixes.iter().any(|s| s.iter().any(|sfx| name.contains(sfx)));
                if hit {
                    matching.push(r);
                } else {
                    non_matching.push(r);
                }
            }
            matching.extend(non_matching);
            results = matching;
        }

        let (best_score, best_idx) = results[0];
        if best_score < self.confidence_threshold {
            let alternatives = results
                .iter()
                .take(5)
                .map(|(_, i)| self.names[*i].clone())
                .collect();
            bail!(AmbiguousQueryError {
                message: format!(
                    "No high-confidence match for '{query}' (best: {} at {:.0}%)",
                    self.names[best_idx], best_score
                ),
                alternatives,
            });
        }

        let (dimensions, warnings) = self.resolve_dimensions(&dims, best_idx);
        let layer = &self.layers[best_idx];

        Ok(ResolvedQuery {
            layer: best_idx,
            style: resolve_style(layer),
            dimensions,
            crs: preferred_crs(layer),
            bbox: layer_bbox(layer, &preferred_crs(layer)),
            confidence: best_score,
            warnings,
            alternatives: results[1..6.min(results.len())]
                .iter()
                .map(|(_, i)| self.names[*i].clone())
                .collect(),
        })
    }

    /// Extract dimension tokens and unit preferences from a query.
    pub fn extract_tokens(
        query: &str,
    ) -> (std::collections::HashMap<String, Vec<String>>, Vec<String>, String) {
        let mut dims: std::collections::HashMap<String, Vec<String>> =
            std::collections::HashMap::new();
        let mut clean = query.to_lowercase();

        // Dimension patterns (RUN/FORECAST/ELEVATION) first
        let re = dimension_re();
        let mut removals: Vec<(usize, usize)> = Vec::new();
        for cap in re.captures_iter(&clean.clone()) {
            let m = cap.get(0).unwrap();
            let dim_name = if cap.get(1).is_some() || cap.get(2).is_some() {
                "RUN"
            } else {
                "FORECAST"
            };
            dims.entry(dim_name.to_string())
                .or_default()
                .push(m.as_str().to_string());
            removals.push((m.start(), m.end()));
        }
        for (start, end) in removals.into_iter().rev() {
            clean.replace_range(start..end, " ");
        }

        // Elevation
        let ere = elevation_re();
        if let Some(m) = ere.find(&clean) {
            let range = m.range();
            let value = m.as_str().to_string();
            clean.replace_range(range, " ");
            dims.entry("ELEVATION".to_string())
                .or_default()
                .push(value);
        }

        // Units (after dimensions so '6h' isn't eaten by unit patterns)
        let mut units = Vec::new();
        for (unit, letter) in [("fahrenheit", 'f'), ("celsius", 'c'), ("kelvin", 'k')] {
            let ure = unit_re(letter);
            if let Some(m) = ure.find(&clean) {
                let range = m.range();
                clean.replace_range(range, " ");
                units.push(unit.to_string());
            }
        }

        let clean = clean.split_whitespace().collect::<Vec<_>>().join(" ");
        (dims, units, clean)
    }

    /// Resolve extracted dimension expressions to valid values,
    /// filling smart defaults, mirroring the Python implementation.
    fn resolve_dimensions(
        &self,
        extracted: &std::collections::HashMap<String, Vec<String>>,
        idx: usize,
    ) -> (Vec<(String, String)>, Vec<String>) {
        let layer = &self.layers[idx];
        let mut resolved: Vec<(String, String)> = Vec::new();
        let mut warnings: Vec<String> = Vec::new();

        let mut order: Vec<&String> = extracted.keys().collect();
        order.sort();
        for dim_name in order {
            let expressions = &extracted[dim_name];
            if !layer.dimensions.contains_key(dim_name.as_str()) {
                warnings.push(format!(
                    "Layer '{}' doesn't have {dim_name} dimension, skipping",
                    self.names[idx]
                ));
                continue;
            }
            let expr = expressions[0].clone();
            let parsed = match dim_name.as_str() {
                "RUN" => {
                    let parsed = parse_run_time(&expr, layer);
                    if let Some(p) = &parsed {
                        if *p != expr {
                            warnings.push(format!(
                                "Using {p} for RUN (requested: {expr})"
                            ));
                        }
                    }
                    parsed
                }
                "FORECAST" => {
                    let parsed = parse_forecast(&expr, layer);
                    if let Some(p) = &parsed {
                        let original_iso = forecast_shortcuts()
                            .get(expr.trim_start_matches('+').to_lowercase().as_str())
                            .map(|s| s.to_string())
                            .or_else(|| parse_forecast_iso(&expr));
                        if let Some(orig) = original_iso {
                            if *p != orig {
                                warnings.push(format!(
                                    "Using {p} for FORECAST (requested: {orig})"
                                ));
                            }
                        }
                    }
                    parsed
                }
                "ELEVATION" => {
                    let parsed = parse_elevation(&expr, layer);
                    if let Some(p) = &parsed {
                        let stripped = expr
                            .to_lowercase()
                            .trim_end_matches("mb")
                            .trim_end_matches("hpa")
                            .trim()
                            .to_string();
                        if *p != stripped {
                            warnings.push(format!(
                                "Using {p} for ELEVATION (requested: {expr})"
                            ));
                        }
                    }
                    parsed
                }
                _ => None,
            };
            if let Some(value) = parsed {
                resolved.push((dim_name.clone(), value));
            }
        }

        // Smart defaults for unspecified dimensions
        let mut dim_names: Vec<&String> = layer.dimensions.keys().collect();
        dim_names.sort();
        for dim_name in dim_names {
            if resolved.iter().any(|(n, _)| n == dim_name) {
                continue;
            }
            let dim = &layer.dimensions[dim_name];
            match dim_name.as_str() {
                "RUN" => {
                    if let Some(latest) = latest_run(layer) {
                        resolved.push((dim_name.clone(), latest));
                    }
                }
                "FORECAST" => {
                    let value = dim
                        .default
                        .clone()
                        .unwrap_or_else(|| "PT0S".to_string());
                    resolved.push((dim_name.clone(), value));
                }
                "TIME" => {
                    let value = dim.default.clone().unwrap_or_else(|| {
                        layer
                            .dimensions
                            .get("TIME")
                            .and_then(|d| d.values.last().cloned())
                            .unwrap_or_default()
                    });
                    if !value.is_empty() {
                        resolved.push((dim_name.clone(), value));
                    }
                }
                _ => {
                    if let Some(default) = &dim.default {
                        resolved.push((dim_name.clone(), default.clone()));
                    }
                }
            }
        }

        (resolved, warnings)
    }
}

fn layer_token_match(qt: &str, it: &str) -> bool {
    qt == it
        || (it.starts_with(qt) && qt.chars().count() >= 2)
        || (qt.starts_with(it) && it.chars().count() >= 2)
        || (qt.chars().count() >= 3
            && it.chars().count() >= 3
            && fuzz::ratio(qt, it) > 80.0)
}

fn resolve_style(layer: &Layer) -> String {
    if layer.styles.is_empty() {
        return "default".into();
    }
    if let Some(s) = layer.styles.iter().find(|s| s.name == "default") {
        return s.name.clone();
    }
    layer.styles[0].name.clone()
}

fn preferred_crs(layer: &Layer) -> String {
    for crs in ["CRS:84", "EPSG:4326", "EPSG:3857", "EPSG:900913"] {
        if layer.crs_list.iter().any(|c| c == crs) {
            return crs.to_string();
        }
    }
    layer.crs_list.first().cloned().unwrap_or_else(|| "CRS:84".into())
}

fn layer_bbox(layer: &Layer, crs: &str) -> (f64, f64, f64, f64) {
    if let Some(bb) = layer.bounding_boxes.get(crs) {
        return (bb.minx, bb.miny, bb.maxx, bb.maxy);
    }
    if let Some(BoundingBox { minx, miny, maxx, maxy, .. }) = layer.geographic_bbox {
        return (minx, miny, maxx, maxy);
    }
    (-180.0, -90.0, 180.0, 90.0)
}

// ---------------------------------------------------------------------------
// Dimension parsing (DimensionParser port)
// ---------------------------------------------------------------------------

fn dim_values(layer: &Layer, name: &str) -> Vec<String> {
    layer
        .dimensions
        .get(name)
        .map(|d| d.values.clone())
        .unwrap_or_default()
}

/// Parse a forecast expression ('6h', 'PT12H', '+24h', '2d') and
/// validate against the layer's FORECAST values.
pub fn parse_forecast(expr: &str, layer: &Layer) -> Option<String> {
    let expr = expr.trim().to_lowercase().replace('+', "");
    let parsed = if let Some(iso) = forecast_shortcuts().get(expr.as_str()) {
        iso.to_string()
    } else if expr.starts_with("pt") || expr.starts_with('p') {
        expr.to_uppercase()
    } else {
        let re = regex::Regex::new(r"^(\d+)([hd])$").unwrap();
        let caps = re.captures(&expr)?;
        let num = &caps[1];
        match &caps[2] {
            "h" => format!("PT{num}H"),
            _ => format!("P{num}D"),
        }
    };
    let values = dim_values(layer, "FORECAST");
    if values.is_empty() {
        return Some(parsed);
    }
    if values.contains(&parsed) {
        return Some(parsed);
    }
    Some(closest_duration(&parsed, &values))
}

fn parse_forecast_iso(expr: &str) -> Option<String> {
    let expr = expr.trim().to_lowercase().replace('+', "");
    if let Some(iso) = forecast_shortcuts().get(expr.as_str()) {
        return Some(iso.to_string());
    }
    if expr.starts_with("pt") || expr.starts_with('p') {
        return Some(expr.to_uppercase());
    }
    let re = regex::Regex::new(r"^(\d+)([hd])$").unwrap();
    let caps = re.captures(&expr)?;
    let num = &caps[1];
    Some(match &caps[2] {
        "h" => format!("PT{num}H"),
        _ => format!("P{num}D"),
    })
}

fn duration_hours(duration: &str) -> f64 {
    let d = duration.to_uppercase();
    if d == "PT0S" {
        return 0.0;
    }
    if let Some(rest) = d.strip_prefix("PT") {
        if let Some(h) = rest.strip_suffix('H') {
            return h.parse().unwrap_or(0.0);
        }
        if let Some(m) = rest.strip_suffix('M') {
            return m.parse::<f64>().unwrap_or(0.0) / 60.0;
        }
    } else if let Some(rest) = d.strip_prefix('P') {
        if let Some(days) = rest.strip_suffix('D') {
            return days.parse::<f64>().unwrap_or(0.0) * 24.0;
        }
    }
    0.0
}

fn closest_duration(target: &str, values: &[String]) -> String {
    let target_h = duration_hours(target);
    values
        .iter()
        .min_by(|a, b| {
            (duration_hours(a) - target_h)
                .abs()
                .partial_cmp(&(duration_hours(b) - target_h).abs())
                .unwrap_or(std::cmp::Ordering::Equal)
        })
        .cloned()
        .unwrap_or_else(|| target.to_string())
}

/// Parse a run time expression ('12z', 'latest', ISO) and validate.
pub fn parse_run_time(expr: &str, layer: &Layer) -> Option<String> {
    let expr = expr.trim().to_lowercase();
    let runs = dim_values(layer, "RUN");

    if expr == "latest" {
        return runs.iter().max().cloned();
    }

    let re = regex::Regex::new(r"^(\d{1,2})z$").unwrap();
    if let Some(caps) = re.captures(&expr) {
        let cycle: u32 = caps[1].parse().ok()?;
        return Some(find_recent_cycle(cycle, &runs));
    }

    let upper = expr.to_uppercase();
    if upper.contains('T') && upper.contains('Z') {
        if runs.contains(&upper) {
            return Some(upper);
        }
        return Some(closest_run(&upper, &runs));
    }
    None
}

fn find_recent_cycle(cycle: u32, runs: &[String]) -> String {
    if runs.is_empty() {
        // Construct a reasonable default from the current UTC time
        let now = Utc::now();
        let hour = cycle.min(23);
        let target = now.date_naive().and_hms_opt(hour, 0, 0).unwrap();
        let target = if target > now.naive_utc() {
            target - chrono::Duration::days(1)
        } else {
            target
        };
        return target.format("%Y-%m-%dT%H:%M:%SZ").to_string();
    }
    let cycle_str = format!("T{cycle:02}:00:00Z");
    let matches: Vec<&String> = runs.iter().filter(|r| r.contains(&cycle_str)).collect();
    if !matches.is_empty() {
        return matches.iter().max().unwrap().to_string();
    }
    // Closest cycle by hour
    let extract_hour = |run: &str| -> u32 {
        run.split('T')
            .nth(1)
            .and_then(|t| t.split(':').next())
            .and_then(|h| h.parse().ok())
            .unwrap_or(0)
    };
    runs.iter()
        .min_by_key(|r| {
            let h = extract_hour(r);
            h.abs_diff(cycle).min(h.abs_diff(cycle.saturating_sub(24)).min(h.abs_diff(cycle + 24)))
        })
        .cloned()
        .unwrap_or_else(|| runs[0].clone())
}

fn closest_run(target: &str, runs: &[String]) -> String {
    let parse = |s: &str| {
        chrono::DateTime::parse_from_rfc3339(&s.replace('Z', "+00:00"))
            .map(|d| d.with_timezone(&Utc))
            .ok()
    };
    let Some(target_dt) = parse(target) else {
        return runs.iter().max().cloned().unwrap_or_default();
    };
    runs.iter()
        .min_by_key(|r| {
            parse(r)
                .map(|d| (d - target_dt).num_seconds().abs())
                .unwrap_or(i64::MAX)
        })
        .cloned()
        .unwrap_or_else(|| runs.iter().max().cloned().unwrap_or_default())
}

fn latest_run(layer: &Layer) -> Option<String> {
    dim_values(layer, "RUN").into_iter().max()
}

/// Parse an elevation expression ('850', '500mb') and snap to the
/// nearest advertised value.
pub fn parse_elevation(expr: &str, layer: &Layer) -> Option<String> {
    let expr = expr
        .trim()
        .to_lowercase()
        .trim_end_matches("mb")
        .trim_end_matches("hpa")
        .trim()
        .to_string();
    if expr.is_empty() || !expr.chars().all(|c| c.is_ascii_digit() || c == '.') {
        return None;
    }
    let values = dim_values(layer, "ELEVATION");
    if values.is_empty() {
        return Some(expr);
    }
    if values.contains(&expr) {
        return Some(expr);
    }
    let target: f64 = expr.parse().ok()?;
    Some(
        values
            .iter()
            .min_by_key(|v| {
                v.parse::<f64>()
                    .map(|n| (n - target).abs() as u64)
                    .unwrap_or(u64::MAX)
            })
            .cloned()
            .unwrap_or(expr),
    )
}
