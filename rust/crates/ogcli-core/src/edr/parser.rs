//! EDR (OGC API - Environmental Data Retrieval) /collections parser.
//!
//! Mirrors `src/edr/parser.py`: tolerant parsing of varied server JSON
//! shapes into typed collections, plus CoverageJSON summarization.

use std::collections::HashMap;

use serde_json::Value;

/// A parameter (observable) offered by a collection.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Parameter {
    pub id: String,
    pub label: String,
    pub unit: String,
    pub observed_property: String,
}

impl Parameter {
    fn from_value(name: &str, data: &Value) -> Self {
        let get_str = |v: &Value| label_of(v).to_string();
        let unit = match data.get("unit") {
            None => String::new(),
            Some(v) => match v {
                Value::String(_) => get_str(v),
                Value::Object(_) => {
                    let symbol = v.get("symbol");
                    let symbol = match symbol {
                        Some(s) => match s {
                            Value::String(_) => get_str(s),
                            Value::Object(_) => get_str(s),
                            _ => String::new(),
                        },
                        None => String::new(),
                    };
                    if symbol.is_empty() { get_str(v) } else { symbol }
                }
                _ => String::new(),
            },
        };
        let observed_property = data
            .get("observedProperty")
            .map(get_str)
            .unwrap_or_default();
        Parameter {
            id: data
                .get("id")
                .and_then(|v| v.as_str())
                .unwrap_or(name)
                .to_string(),
            label: data
                .get("label")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string(),
            unit,
            observed_property,
        }
    }
}

/// An EDR collection (analogous to a WMS layer).
#[derive(Debug, Clone, Default)]
pub struct EdrCollection {
    pub id: String,
    pub title: String,
    pub description: String,

    pub bbox: Option<Vec<f64>>, // [minx, miny, maxx, maxy]
    pub crs: String,

    pub temporal_intervals: Vec<(Option<String>, Option<String>)>,
    pub temporal_values: Vec<String>,

    pub vertical_values: Vec<String>,
    pub vertical_unit: String,

    pub data_queries: Vec<String>,
    pub parameter_names: HashMap<String, Parameter>,
}

impl EdrCollection {
    pub fn supports(&self, query_type: &str) -> bool {
        self.data_queries.iter().any(|q| q == query_type)
    }

    pub fn temporal_start(&self) -> Option<&str> {
        self.temporal_values.first().map(|s| s.as_str()).or_else(|| {
            self.temporal_intervals.first().and_then(|iv| iv.0.as_deref())
        })
    }

    pub fn temporal_end(&self) -> Option<&str> {
        self.temporal_values.last().map(|s| s.as_str()).or_else(|| {
            self.temporal_intervals.first().and_then(|iv| iv.1.as_deref())
        })
    }

    pub fn n_timesteps(&self) -> usize {
        self.temporal_values.len()
    }

    pub fn n_levels(&self) -> usize {
        self.vertical_values.len()
    }

    pub fn center(&self) -> Option<(f64, f64)> {
        let b = self.bbox.as_ref()?;
        if b.len() < 4 {
            return None;
        }
        Some(((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0))
    }

    /// One-line summary for listings, e.g. `hrrr-surface (68t, 8p)`.
    pub fn summary_line(&self) -> String {
        let mut bits: Vec<String> = Vec::new();
        if self.n_timesteps() > 0 {
            bits.push(format!("{}t", self.n_timesteps()));
        }
        if self.n_levels() > 0 {
            bits.push(format!("{}z", self.n_levels()));
        }
        if !self.parameter_names.is_empty() {
            bits.push(format!("{}p", self.parameter_names.len()));
        }
        if bits.is_empty() {
            self.id.clone()
        } else {
            format!("{} ({})", self.id, bits.join(", "))
        }
    }
}

/// Parsed EDR /collections document.
#[derive(Debug, Clone, Default)]
pub struct EdrCapabilities {
    pub title: String,
    pub description: String,
    pub server_url: String,
    pub collections: Vec<EdrCollection>,
}

impl EdrCapabilities {
    pub fn get_collection(&self, id: &str) -> Option<&EdrCollection> {
        self.collections.iter().find(|c| c.id == id)
    }

    pub fn query_types(&self) -> Vec<String> {
        let mut types: Vec<String> = Vec::new();
        for c in &self.collections {
            for q in &c.data_queries {
                if !types.contains(q) {
                    types.push(q.clone());
                }
            }
        }
        types.sort();
        types
    }
}

/// Extract a human-readable label from nested JSON, e.g.
/// `{"label": {"en": "RH"}}` -> "RH", `{"en": "RH"}` -> "RH".
fn label_of(value: &Value) -> String {
    match value {
        Value::String(s) => s.clone(),
        Value::Object(map) => {
            for key in ["label", "en", "name", "value", "id"] {
                if let Some(v) = map.get(key) {
                    let got = label_of(v);
                    if !got.is_empty() {
                        return got;
                    }
                }
            }
            String::new()
        }
        _ => String::new(),
    }
}

/// Normalize a CRS identifier. Some servers (Met Office Labs) provide
/// full WKT - extract the EPSG authority code for readable display.
pub fn pretty_crs(crs: &str) -> String {
    if crs.starts_with("GEOGCS[")
        || crs.starts_with("PROJCS[")
        || crs.starts_with("GEODCRS[")
        || crs.starts_with("ENGCS[")
    {
        let re = regex::Regex::new(r#"AUTHORITY\["EPSG","(\d+)"\]"#).unwrap();
        if let Some(code) = re.find_iter(crs).last() {
            let digits = &code.as_str()["AUTHORITY[\"EPSG\",\"".len()
                ..code.as_str().len() - "\"]".len()];
            return format!("EPSG:{digits}");
        }
    }
    crs.to_string()
}

/// Parse the /collections JSON document (string or pre-parsed Value).
pub fn parse_collections(data: &str, server_url: &str) -> anyhow::Result<EdrCapabilities> {
    let value: Value = serde_json::from_str(data)?;
    parse_collections_value(&value, server_url)
}

pub fn parse_collections_value(value: &Value, server_url: &str) -> anyhow::Result<EdrCapabilities> {
    let mut caps = EdrCapabilities {
        title: value
            .get("title")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        description: value
            .get("description")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        server_url: server_url.to_string(),
        collections: Vec::new(),
    };

    let empty = Vec::new();
    let collections = value
        .get("collections")
        .and_then(|v| v.as_array())
        .unwrap_or(&empty);

    for cdata in collections {
        if let Ok(c) = parse_collection(cdata) {
            caps.collections.push(c);
        }
    }

    Ok(caps)
}

fn parse_collection(data: &Value) -> anyhow::Result<EdrCollection> {
    let mut c = EdrCollection {
        id: data
            .get("id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        title: data
            .get("title")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        description: data
            .get("description")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        crs: "CRS:84".to_string(),
        ..Default::default()
    };
    if c.id.is_empty() {
        anyhow::bail!("collection has no id");
    }

    if let Some(extent) = data.get("extent").and_then(|v| v.as_object()) {
        // Spatial
        if let Some(spatial) = extent.get("spatial").and_then(|v| v.as_object()) {
            if let Some(crs) = spatial.get("crs").and_then(|v| v.as_str()) {
                c.crs = pretty_crs(crs);
            }
            if let Some(bbox) = spatial
                .get("bbox")
                .and_then(|v| v.as_array())
                .and_then(|arr| arr.first())
                .and_then(|v| v.as_array())
            {
                c.bbox = Some(
                    bbox.iter()
                        .filter_map(|v| v.as_f64())
                        .collect::<Vec<f64>>(),
                );
            }
        }

        // Temporal
        if let Some(temporal) = extent.get("temporal").and_then(|v| v.as_object()) {
            if let Some(intervals) = temporal.get("interval").and_then(|v| v.as_array()) {
                for iv in intervals {
                    if let Some(pair) = iv.as_array() {
                        let lo = pair.first().and_then(|v| v.as_str()).map(String::from);
                        let hi = pair.get(1).and_then(|v| v.as_str()).map(String::from);
                        c.temporal_intervals.push((lo, hi));
                    }
                }
            }
            if let Some(values) = temporal.get("values").and_then(|v| v.as_array()) {
                for v in values {
                    match v {
                        Value::String(s) => c.temporal_values.push(s.clone()),
                        Value::Number(n) => c.temporal_values.push(n.to_string()),
                        _ => {}
                    }
                }
            }
        }

        // Vertical
        if let Some(vertical) = extent.get("vertical").and_then(|v| v.as_object()) {
            if let Some(values) = vertical.get("values").and_then(|v| v.as_array()) {
                for v in values {
                    match v {
                        Value::String(s) => c.vertical_values.push(s.clone()),
                        Value::Number(n) => c.vertical_values.push(n.to_string()),
                        _ => {}
                    }
                }
            }
            if let Some(vrs) = vertical.get("vrs") {
                let vrs_str = match vrs {
                    Value::String(s) => s.clone(),
                    Value::Array(arr) => arr
                        .first()
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .to_string(),
                    _ => String::new(),
                };
                c.vertical_unit = vrs_str;
            }
        }
    }

    // Data queries
    if let Some(queries) = data.get("data_queries").and_then(|v| v.as_object()) {
        c.data_queries = queries.keys().cloned().collect();
    }

    // Parameters
    if let Some(params) = data.get("parameter_names").and_then(|v| v.as_object()) {
        for (pname, pdata) in params {
            let param = match pdata {
                Value::Object(_) => Parameter::from_value(pname, pdata),
                _ => Parameter {
                    id: pname.clone(),
                    ..Default::default()
                },
            };
            c.parameter_names.insert(pname.clone(), param);
        }
    }

    Ok(c)
}

/// Parse a landing page document -> (title, description).
pub fn parse_landing(data: &str) -> anyhow::Result<(String, String)> {
    let value: Value = serde_json::from_str(data)?;
    Ok((
        value
            .get("title")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        value
            .get("description")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
    ))
}

// ---------------------------------------------------------------------------
// CoverageJSON summary
// ---------------------------------------------------------------------------

/// Summary of a CoverageJSON (or GeoJSON) response.
#[derive(Debug, Clone, Default, serde::Serialize)]
pub struct CoverageSummary {
    #[serde(rename = "type")]
    pub coverage_type: String,
    pub axes: HashMap<String, usize>,
    pub parameters: Vec<ParameterSummary>,
}

/// Per-parameter value stats.
#[derive(Debug, Clone, Default, serde::Serialize)]
pub struct ParameterSummary {
    pub name: String,
    pub unit: String,
    pub count: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub min: Option<f64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub max: Option<f64>,
    #[serde(skip_serializing_if = "is_zero")]
    pub nils: usize,
}

fn is_zero(n: &usize) -> bool {
    *n == 0
}

/// Summarize a CoverageJSON (or GeoJSON) payload.
/// Returns None if the payload isn't recognizable JSON.
pub fn summarize_coverage(data: &[u8]) -> Option<CoverageSummary> {
    let value: Value = serde_json::from_slice(data).ok()?;
    let obj = value.as_object()?;

    let mut summary = CoverageSummary {
        coverage_type: obj
            .get("type")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        ..Default::default()
    };

    // GeoJSON feature collections
    if summary.coverage_type == "FeatureCollection" {
        if let Some(features) = obj.get("features").and_then(|v| v.as_array()) {
            summary.axes.insert("features".into(), features.len());
        }
        return Some(summary);
    }

    // CoverageCollection: aggregate nested coverages (first few only)
    let empty = Vec::new();
    let nested_all = obj.get("coverages")
        .or_else(|| obj.get("collections"))
        .and_then(|v| v.as_array())
        .unwrap_or(&empty);
    let nested: Vec<&Value> = nested_all
        .iter()
        .filter(|v| v.is_object())
        .take(3)
        .collect();
    if summary.coverage_type == "CoverageCollection" {
        summary.axes.insert("coverages".into(), nested_all.len());
    }

    // Domain axes (top-level + nested). Grid axes may use start/stop/num
    // instead of explicit values.
    let top = serde_json::Value::Object(obj.clone());
    let mut sources: Vec<(&Value, String)> = vec![(&top, String::new())];
    for (i, c) in nested.iter().enumerate() {
        sources.push((c, format!("{}:", i + 1)));
    }
    for (source, prefix) in &sources {
        if let Some(axes) = source.get("domain").and_then(|v| v.as_object()) {
            if let Some(axes) = axes.get("axes").and_then(|v| v.as_object()) {
                for (name, axis) in axes {
                    let n = if let Some(values) = axis.get("values").and_then(|v| v.as_array()) {
                        values.len()
                    } else if let Some(num) = axis.get("num").and_then(|v| v.as_u64()) {
                        num as usize
                    } else {
                        1
                    };
                    summary.axes.insert(format!("{prefix}{name}"), n);
                }
            }
        }
    }

    // Parameter metadata (name -> unit)
    let mut units: HashMap<String, String> = HashMap::new();
    for source in sources.iter().map(|(s, _)| *s) {
        if let Some(params) = source.get("parameters").and_then(|v| v.as_object()) {
            for (pname, pdata) in params {
                if !units.contains_key(pname) {
                    let unit = pdata
                        .get("unit")
                        .map(|u| match u {
                            Value::String(s) => s.clone(),
                            Value::Object(map) => {
                                // {"symbol": "K"} or nested symbol objects
                                let sym = map
                                    .get("symbol")
                                    .map(label_of)
                                    .unwrap_or_default();
                                if sym.is_empty() { label_of(u) } else { sym }
                            }
                            _ => String::new(),
                        })
                        .unwrap_or_default();
                    units.insert(pname.clone(), unit);
                }
            }
        }
    }

    // Ranges (actual values), aggregated across sources
    #[derive(Default)]
    struct Agg {
        count: usize,
        nils: usize,
        mins: Vec<f64>,
        maxs: Vec<f64>,
    }
    let mut totals: HashMap<String, Agg> = HashMap::new();
    for source in sources.iter().map(|(s, _)| *s) {
        let Some(ranges) = source.get("ranges").and_then(|v| v.as_object()) else {
            continue;
        };
        for (pname, rng) in ranges {
            let Some(values) = rng.get("values").and_then(|v| v.as_array()) else {
                continue;
            };
            let numeric: Vec<f64> = values
                .iter()
                .filter_map(|v| v.as_f64())
                .collect();
            let agg = totals.entry(pname.clone()).or_default();
            agg.count += values.len();
            agg.nils += values.len() - numeric.len();
            if let Some(min) = numeric.iter().cloned().reduce(f64::min) {
                agg.mins.push(min);
            }
            if let Some(max) = numeric.iter().cloned().reduce(f64::max) {
                agg.maxs.push(max);
            }
        }
    }

    for (pname, agg) in &totals {
        summary.parameters.push(ParameterSummary {
            name: pname.clone(),
            unit: units.get(pname).cloned().unwrap_or_default(),
            count: agg.count,
            min: agg.mins.iter().cloned().reduce(f64::min),
            max: agg.maxs.iter().cloned().reduce(f64::max),
            nils: agg.nils,
        });
    }
    if totals.is_empty() {
        for (pname, unit) in &units {
            summary.parameters.push(ParameterSummary {
                name: pname.clone(),
                unit: unit.clone(),
                count: 0,
                min: None,
                max: None,
                nils: 0,
            });
        }
    }

    Some(summary)
}
