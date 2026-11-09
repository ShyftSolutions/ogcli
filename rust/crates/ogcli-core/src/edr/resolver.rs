//! EDR fuzzy resolver - a faithful port of `src/edr/resolver.py`.
//!
//! Two-phase resolution: extract datetime/z tokens, fuzzy-match the
//! collection (parameter tokens contribute at 85% weight), then map
//! leftover tokens to a parameter of the chosen collection.
//!
//! The weights and thresholds here are empirically tuned against real
//! servers; the fixture tests lock the behavior in.

use std::collections::HashMap;
use anyhow::{bail, Result};
use chrono::{DateTime, Utc};

use crate::edr::parser::{EdrCapabilities, EdrCollection};
use crate::fuzz;

#[derive(Debug, Clone, Default)]
pub struct ResolvedEdr {
    pub collection: usize, // index into capabilities.collections
    pub parameter: Option<String>,
    pub confidence: f64,
    pub warnings: Vec<String>,
    pub alternatives: Vec<String>,
    pub extracted: HashMap<String, String>, // "datetime" / "z" -> raw token
}

#[derive(Debug, thiserror::Error)]
#[error("{message}")]
pub struct AmbiguousCollectionError {
    pub message: String,
    pub alternatives: Vec<String>,
}

pub struct EdrResolver {
    pub capabilities: EdrCapabilities,
    pub confidence_threshold: f64,
    /// {collection id: tokens}
    index: HashMap<String, Vec<String>>,
    /// {collection id: parameter tokens}
    param_index: HashMap<String, Vec<String>>,
}

/// Tokenize for searching: split on `_`, `-`, `/`, camelCase
/// boundaries, and letter/digit boundaries; lowercase.
pub fn tokenize(text: &str) -> Vec<String> {
    let mut out = String::with_capacity(text.len());
    let chars: Vec<char> = text.chars().collect();
    for (i, &c) in chars.iter().enumerate() {
        let prev = if i > 0 { Some(chars[i - 1]) } else { None };
        let split_before = match prev {
            Some(p) => {
                // camelCase boundary
                (p.is_lowercase() && c.is_uppercase())
                    // letter/digit boundary
                    || (p.is_alphabetic() && c.is_numeric())
                    || (p.is_numeric() && c.is_alphabetic())
            }
            None => false,
        };
        if matches!(c, '_' | '-' | '/') {
            out.push(' ');
        } else {
            if split_before {
                out.push(' ');
            }
            out.push(c);
        }
    }
    out.to_lowercase()
        .split_whitespace()
        .map(|s| s.to_string())
        .collect()
}

/// Best match score between a query token and item tokens (0.0-1.0).
/// Priority order mirrors the Python implementation:
/// exact > prefix > superstring > substring > fuzzy.
pub fn token_match(qt: &str, tokens: &[String]) -> f64 {
    let mut best: f64 = 0.0;
    for it in tokens {
        if qt == it {
            return 1.0;
        }
        if it.starts_with(qt) && qt.chars().count() >= 2 {
            best = best.max(0.9);
        } else if qt.starts_with(it) && it.chars().count() >= 2 {
            best = best.max(0.8);
        } else if qt.chars().count() >= 3 && it.contains(qt) {
            best = best.max(0.85);
        } else if qt.chars().count() >= 3 && it.chars().count() >= 3 {
            let r = fuzz::ratio(qt, it);
            if r > 80.0 {
                best = best.max(r / 100.0 * 0.7);
            }
        }
    }
    best
}

impl EdrResolver {
    pub fn new(capabilities: EdrCapabilities) -> Self {
        let confidence_threshold = 50.0;
        let mut index = HashMap::new();
        let mut param_index = HashMap::new();
        for c in &capabilities.collections {
            index.insert(
                c.id.clone(),
                tokenize(&format!("{} {}", c.id, c.title)),
            );
            let param_text = c
                .parameter_names
                .iter()
                .map(|(name, p)| format!("{} {} {}", name, p.label, p.observed_property))
                .collect::<Vec<_>>()
                .join(" ");
            param_index.insert(c.id.clone(), tokenize(&param_text));
        }
        EdrResolver {
            capabilities,
            confidence_threshold,
            index,
            param_index,
        }
    }

    pub fn collection(&self, idx: usize) -> &EdrCollection {
        &self.capabilities.collections[idx]
    }

    /// Score query tokens against a collection's tokens (+params).
    fn score(
        &self,
        query_tokens: &[String],
        tokens: &[String],
        param_tokens: Option<&Vec<String>>,
    ) -> f64 {
        if query_tokens.is_empty() {
            return 0.0;
        }
        let mut matches = 0.0;
        let mut all_matched = true;
        for qt in query_tokens {
            let mut best = token_match(qt, tokens);
            if let Some(pt) = param_tokens {
                best = best.max(token_match(qt, pt) * 0.85);
            }
            if best <= 0.0 {
                all_matched = false;
            }
            matches += best;
        }
        let mut token_score = matches / query_tokens.len() as f64 * 100.0;
        if all_matched {
            token_score = (token_score + 5.0).min(100.0);
        }
        // Partial ratio is only a tiebreaker (scaled down so short ids
        // don't score high without token support)
        let query = query_tokens.join(" ");
        let text = tokens.join(" ");
        let partial = fuzz::partial_ratio(&query, &text) * 0.45;
        token_score.max(partial)
    }

    /// List collections, optionally fuzzy-filtered.
    /// Returns (collection index, score), best first.
    pub fn list_collections(&self, query: Option<&str>, limit: usize) -> Vec<(usize, f64)> {
        let Some(query) = query.filter(|q| !q.trim().is_empty()) else {
            return self
                .capabilities
                .collections
                .iter()
                .enumerate()
                .take(limit)
                .map(|(i, _)| (i, 0.0))
                .collect();
        };
        let (_, clean) = extract_tokens(query);
        let query_tokens = tokenize(&clean);
        let mut scored: Vec<(usize, f64)> = self
            .capabilities
            .collections
            .iter()
            .enumerate()
            .filter_map(|(i, c)| {
                let score = self.score(
                    &query_tokens,
                    self.index.get(&c.id).unwrap(),
                    self.param_index.get(&c.id),
                );
                if score > 0.0 {
                    Some((i, score))
                } else {
                    None
                }
            })
            .collect();
        scored.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
        scored.truncate(limit);
        scored
    }

    /// Resolve a user query to a collection (+ optional parameter).
    pub fn resolve(&self, query: &str) -> Result<ResolvedEdr> {
        let (extracted, clean) = extract_tokens(query);
        let query_tokens = tokenize(&clean);

        let results = self.list_collections(Some(&clean), 10);
        if results.is_empty() {
            let alternatives = self
                .list_collections(None, 5)
                .into_iter()
                .map(|(i, _)| self.collection(i).id.clone())
                .collect::<Vec<_>>();
            bail!(AmbiguousCollectionError {
                message: format!("No collections found matching '{clean}'"),
                alternatives,
            });
        }

        let (idx, score) = results[0];
        if score < self.confidence_threshold {
            let alternatives = results
                .iter()
                .take(5)
                .map(|(i, _)| self.collection(*i).id.clone())
                .collect::<Vec<_>>();
            bail!(AmbiguousCollectionError {
                message: format!(
                    "No high-confidence match for '{query}' (best: {} at {:.0}%)",
                    self.collection(idx).id,
                    score
                ),
                alternatives,
            });
        }

        let collection = self.collection(idx);
        let (parameter, param_warning) =
            self.match_parameter(collection, &query_tokens);
        let mut warnings = Vec::new();
        if let Some(w) = param_warning {
            warnings.push(w);
        }

        Ok(ResolvedEdr {
            collection: idx,
            parameter,
            confidence: score,
            warnings,
            alternatives: results[1..results.len().min(6)]
                .iter()
                .map(|(i, _)| self.collection(*i).id.clone())
                .collect(),
            extracted,
        })
    }

    /// Map leftover query tokens against the collection's parameters
    /// (max-aggregate; a solid single word match counts).
    fn match_parameter(
        &self,
        collection: &EdrCollection,
        query_tokens: &[String],
    ) -> (Option<String>, Option<String>) {
        if collection.parameter_names.is_empty() || query_tokens.is_empty() {
            return (None, None);
        }
        let collection_tokens = self.index.get(&collection.id).unwrap();
        let leftovers: Vec<&String> = query_tokens
            .iter()
            .filter(|qt| token_match(qt, collection_tokens) == 0.0)
            .collect();
        if leftovers.is_empty() {
            return (None, None);
        }

        let mut best_param: Option<String> = None;
        let mut best_score = 0.0;
        for (pname, param) in &collection.parameter_names {
            let p_tokens = tokenize(&format!(
                "{} {} {}",
                pname, param.label, param.observed_property
            ));
            // Max-aggregate (0-1) scaled to 0-100, matching the Python
            // implementation
            let agg = leftovers
                .iter()
                .map(|qt| token_match(qt, &p_tokens))
                .fold(0.0_f64, f64::max)
                * 100.0;
            let joined = leftovers
                .iter()
                .map(|s| s.as_str())
                .collect::<Vec<_>>()
                .join(" ");
            let score = agg.max(fuzz::partial_ratio(&joined, &p_tokens.join(" ")) * 0.45);
            if score > best_score {
                best_score = score;
                best_param = Some(pname.clone());
            }
        }

        // Require a solid match; param names are often contractions
        // (temp vs TMP fuzzy-matches at ~60)
        if best_score >= 55.0 {
            (best_param, None)
        } else {
            (None, None)
        }
    }

    // -- Natural dimension syntax -----------------------------------

    /// Resolve a datetime expression against the collection's extent.
    /// Returns (datetime param value, warnings).
    pub fn resolve_datetime(
        &self,
        expr: &str,
        collection: &EdrCollection,
    ) -> (Option<String>, Vec<String>) {
        let mut warnings = Vec::new();
        let expr = expr.trim();
        if expr.is_empty() || expr.eq_ignore_ascii_case("none") {
            return (None, warnings);
        }
        if expr.eq_ignore_ascii_case("latest") {
            return match collection.temporal_end() {
                Some(end) => (Some(end.to_string()), warnings),
                None => (
                    None,
                    vec!["Collection has no temporal extent; ignoring datetime".into()],
                ),
            };
        }

        // Offset from the start of the extent (forecast-hour semantics)
        if let Some(offset) = parse_duration(expr) {
            let Some(start) = collection.temporal_start() else {
                return (
                    None,
                    vec!["Collection has no temporal extent; ignoring datetime offset".into()],
                );
            };
            let Ok(base) = parse_iso(start) else {
                return (None, vec![format!("Cannot parse temporal extent value '{start}'")]);
            };
            let mut target = base + offset;
            if let Some(end) = collection.temporal_end() {
                if let Ok(end_dt) = parse_iso(end) {
                    if target > end_dt {
                        target = end_dt;
                        warnings.push(format!("Clamped datetime to extent end {end}"));
                    }
                }
            }
            let value = format_iso(target);
            warnings.push(format!("datetime: {value} ({expr} from {start})"));
            return (Some(value), warnings);
        }

        // Passthrough: ISO instant or interval
        (Some(expr.to_string()), warnings)
    }

    /// Resolve a vertical level expression, snapping to the nearest
    /// advertised level.
    pub fn resolve_z(
        &self,
        expr: &str,
        collection: &EdrCollection,
    ) -> (Option<String>, Vec<String>) {
        let mut warnings = Vec::new();
        let expr = expr.trim();
        if expr.is_empty() || expr.eq_ignore_ascii_case("none") {
            return (None, warnings);
        }

        if let Some((lo, high)) = expr.split_once('/') {
            let lo = strip_unit(lo);
            let high = strip_unit(high);
            return (Some(format!("{lo}/{high}")), warnings);
        }

        let value = strip_unit(expr);
        let Ok(num) = value.parse::<f64>() else {
            warnings.push(format!("z value '{expr}' not numeric; passing through as-is"));
            return (Some(expr.to_string()), warnings);
        };

        if !collection.vertical_values.is_empty() {
            let parse = |s: &str| s.parse::<f64>().ok();
            if let Some(nearest) = collection
                .vertical_values
                .iter()
                .filter_map(|v| parse(v).map(|n| (v, n)))
                .min_by(|a, b| {
                    (a.1 - num)
                        .abs()
                        .partial_cmp(&(b.1 - num).abs())
                        .unwrap_or(std::cmp::Ordering::Equal)
                })
                .map(|(s, _)| s.clone())
            {
                let nearest_num: f64 = nearest.parse().unwrap_or(num);
                if (nearest_num - num).abs() > f64::EPSILON {
                    warnings.push(format!("z: snapped {expr} -> {nearest}"));
                }
                return (Some(nearest), warnings);
            }
        }

        let value = if num == num.trunc() && num.abs() < i64::MAX as f64 {
            format!("{}", num as i64)
        } else {
            format!("{num}")
        };
        (Some(value), warnings)
    }
}

/// Extract datetime/z dimension tokens from a query ->
/// (extracted dims, clean query).
pub fn extract_tokens(query: &str) -> (HashMap<String, String>, String) {
    use std::sync::OnceLock;
    static DATETIME_RE: OnceLock<regex::Regex> = OnceLock::new();
    static Z_RE: OnceLock<regex::Regex> = OnceLock::new();
    let datetime_re = DATETIME_RE.get_or_init(|| {
        regex::Regex::new(r"(?i)\b(latest|\+?\d+[hd]|PT\d+[HMS]|P\d+[DWM])\b").unwrap()
    });
    let z_re = Z_RE.get_or_init(|| {
        regex::Regex::new(r"(?i)\b(\d{2,6}(?:mb|hpa|m)?(?:/\d{2,6}(?:mb|hpa|m)?)?)\b")
            .unwrap()
    });

    let mut extracted = HashMap::new();
    let mut clean = query.to_string();

    if let Some(m) = datetime_re.find(&clean) {
        let range = m.range();
        let value = m.as_str().to_string();
        extracted.insert("datetime".into(), value);
        clean.replace_range(range, " ");
    }
    if let Some(m) = z_re.find(&clean) {
        let range = m.range();
        let value = m.as_str().to_string();
        extracted.insert("z".into(), value);
        clean.replace_range(range, " ");
    }

    (extracted, clean.split_whitespace().collect::<Vec<_>>().join(" "))
}

/// Parse a natural duration expression ('6h', '+6h', 'PT6H', '2d',
/// 'P2D', '30m') to a chrono Duration. None if not a duration.
pub fn parse_duration(expr: &str) -> Option<chrono::Duration> {
    let expr = expr.trim().trim_start_matches('+').to_lowercase();
    let secs = |n: f64| chrono::Duration::seconds(n as i64);

    // PT6H / PT30M / PT10S
    if let Some(rest) = expr.strip_prefix("pt") {
        let mut total = 0.0_f64;
        let mut matched = false;
        let mut num = String::new();
        for ch in rest.chars() {
            if ch.is_ascii_digit() || ch == '.' {
                num.push(ch);
            } else {
                if let Ok(n) = num.parse::<f64>() {
                    let mult = match ch {
                        'h' => 3600.0,
                        'm' => 60.0,
                        's' => 1.0,
                        _ => 0.0,
                    };
                    if mult > 0.0 {
                        total += n * mult;
                        matched = true;
                    }
                }
                num.clear();
            }
        }
        if matched && num.is_empty() {
            return Some(secs(total));
        }
        return None;
    }
    // P2D / P1W
    if let Some(rest) = expr.strip_prefix('p') {
        if let Some(days) = rest.strip_suffix('d').and_then(|n| n.parse::<f64>().ok()) {
            return Some(secs(days * 86400.0));
        }
        if let Some(weeks) = rest.strip_suffix('w').and_then(|n| n.parse::<f64>().ok()) {
            return Some(secs(weeks * 7.0 * 86400.0));
        }
        return None;
    }
    // 6h / 2d / 30s / 45m
    if let Some(hours) = expr.strip_suffix('h').and_then(|n| n.parse::<f64>().ok()) {
        return Some(secs(hours * 3600.0));
    }
    if let Some(days) = expr.strip_suffix('d').and_then(|n| n.parse::<f64>().ok()) {
        return Some(secs(days * 86400.0));
    }
    if let Some(minutes) = expr.strip_suffix('m').and_then(|n| n.parse::<f64>().ok()) {
        return Some(secs(minutes * 60.0));
    }
    if let Some(seconds) = expr.strip_suffix('s').and_then(|n| n.parse::<f64>().ok()) {
        return Some(secs(seconds));
    }
    None
}

fn parse_iso(value: &str) -> Result<DateTime<Utc>> {
    Ok(DateTime::parse_from_rfc3339(value)?.with_timezone(&Utc))
}

fn format_iso(dt: DateTime<Utc>) -> String {
    dt.format("%Y-%m-%dT%H:%M:%SZ").to_string()
}

fn strip_unit(expr: &str) -> String {
    let e = expr.trim().to_lowercase();
    for suffix in ["mb", "hpa", "m"] {
        if let Some(stripped) = e.strip_suffix(suffix) {
            return stripped.trim().to_string();
        }
    }
    e
}
/// ('lon,lat' -> POINT; 'a,b;c,d' -> LINESTRING; WKT passthrough).
/// Parse WKT-ish coordinates input into WKT
/// ('lon,lat' -> POINT; 'a,b;c,d' -> LINESTRING; WKT passthrough).
pub fn parse_coords(coords: &str) -> Result<(String, Vec<String>)> {
    let warnings = Vec::new();
    let s = coords.trim();
    let upper = s.to_uppercase();
    for wkt_type in ["POINT", "POLYGON", "LINESTRING", "MULTIPOINT", "MULTIPOLYGON"] {
        if upper.starts_with(&format!("{}(", wkt_type))
            || upper.starts_with(&format!("{} (", wkt_type))
        {
            return Ok((s.to_string(), warnings));
        }
    }

    let mut points: Vec<(f64, f64)> = Vec::new();
    for chunk in s.split(';') {
        let chunk = chunk.trim();
        if chunk.is_empty() {
            continue;
        }
        let mut parsed = None;
        for sep in [',', ' '] {
            if let Some((lon, lat)) = chunk.split_once(sep) {
                if let (Ok(lon), Ok(lat)) = (lon.trim().parse::<f64>(), lat.trim().parse::<f64>())
                {
                    parsed = Some((lon, lat));
                }
                break;
            }
        }
        match parsed {
            Some(p) => points.push(p),
            None => bail!("cannot parse coords '{coords}' - use 'lon,lat'"),
        }
    }

    if points.is_empty() {
        bail!("cannot parse coords '{coords}'");
    }
    if points.len() == 1 {
        let (lon, lat) = points[0];
        Ok((format!("POINT({lon} {lat})"), warnings))
    } else {
        let joined = points
            .iter()
            .map(|(lon, lat)| format!("{lon} {lat}"))
            .collect::<Vec<_>>()
            .join(",");
        Ok((format!("LINESTRING({joined})"), warnings))
    }
}

/// Convert 'minx,miny,maxx,maxy' to a rectangular WKT POLYGON.
pub fn bbox_to_wkt(bbox: &str) -> Option<String> {
    let parts: Vec<&str> = bbox.split(',').collect();
    if parts.len() != 4 {
        return None;
    }
    let vals: Vec<f64> = parts.iter().filter_map(|p| p.trim().parse().ok()).collect();
    if vals.len() != 4 || vals[0] >= vals[2] || vals[1] >= vals[3] {
        return None;
    }
    Some(format!(
        "POLYGON(({} {},{} {},{} {},{} {},{} {}))",
        vals[0], vals[1], vals[2], vals[1], vals[2], vals[3], vals[0], vals[3], vals[0], vals[1]
    ))
}
