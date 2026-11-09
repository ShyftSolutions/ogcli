//! WMS hammer: GetMap dimension-sweep load generator.
//!
//! Rust port of `src/wms/hammer.py` covering the paths the
//! `wms hammer` CLI uses: for each layer, cross-product the
//! dimension values (RUN x FORECAST/TIME x ELEVATION x ...) with
//! each style, then fetch the images with a scoped thread pool
//! using atomic work claiming (same pattern as the EDR hammer).
//!
//! Not ported from the Python class: tile-mode / random-tiles,
//! glob filtering, `--list-layers` and single-layer targeting -
//! the Python CLI wrapper never exposes them (it overrides the
//! layer list with fuzzy matches).

use std::path::PathBuf;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;
use std::time::Instant;

use anyhow::Result;

use crate::wms::parser::Layer;
use crate::wms::resolver::ResolvedQuery;

/// Fixed sample station (Python parity: CRS:84[-107.4229;43.6178]).
const SAMPLE_STATION: &str = "CRS:84[-107.4229;43.6178]";

/// Fixed sample linestring for cross-section (PLACE) layers. The
/// Python version draws random points from a fixed RNG seed; any
/// valid linestring serves the same purpose.
const SAMPLE_LINESTRING: &str = "CRS:84[-107.423;43.618] CRS:84[-95.993;36.154] CRS:84[-84.432;33.749]";

/// One planned WMS GetMap request.
#[derive(Debug, Clone)]
pub struct HammerRequest {
    pub layer: String,
    pub style: String,
    /// (param name, value) e.g. ("DIM_RUN", "2026-11-04T12:00:00Z")
    pub dimensions: Vec<(String, String)>,
    /// Full query params (SERVICE, VERSION, ..., plus dimensions).
    pub params: Vec<(String, String)>,
    /// Fully built URL.
    pub url: String,
    /// Save filename (Python's scheme: layer[_style][_DIM_val...].png).
    pub filename: String,
}

/// Run statistics.
#[derive(Debug, Default)]
pub struct HammerStats {
    pub total: usize,
    pub success: usize,
    pub failed: usize,
    pub bytes: u64,
    pub start: Option<Instant>,
    pub end: Option<Instant>,
}

impl HammerStats {
    pub fn duration_s(&self) -> f64 {
        match (self.start, self.end) {
            (Some(a), Some(b)) => b.duration_since(a).as_secs_f64(),
            _ => 0.0,
        }
    }
}

pub struct WmsHammer<'a> {
    pub base_url: String,
    pub layers: Vec<&'a Layer>,
    pub workers: usize,
    pub width: i64,
    pub height: i64,
    /// Cap FORECAST/TIME values per layer (0 = all).
    pub max_timesteps: usize,
    /// Use only the latest RUN value.
    pub latest_run: bool,
    /// Save fetched images to `output_dir`.
    pub save: bool,
    /// Cap total requests (0 = unlimited).
    pub limit: usize,
    pub output_dir: PathBuf,
    pub requests: Vec<HammerRequest>,
}

impl<'a> WmsHammer<'a> {
    pub fn new(base_url: &str, layers: Vec<&'a Layer>) -> Self {
        WmsHammer {
            base_url: base_url.to_string(),
            layers,
            workers: 20,
            width: 800,
            height: 600,
            max_timesteps: 0,
            latest_run: false,
            save: true,
            limit: 0,
            output_dir: PathBuf::from("wms_output"),
            requests: Vec::new(),
        }
    }

    /// Dimension values to sweep for a layer, in stable order:
    /// (param name, values). Mirrors the Python hammer's known-dim
    /// list; dimensions it does not know about are skipped.
    fn dim_values(&self, layer: &Layer) -> Vec<(String, Vec<String>)> {
        let mut out: Vec<(String, Vec<String>)> = Vec::new();
        let mut push = |dim_name: &str, values: Vec<String>| {
            if !values.is_empty() {
                out.push((
                    ResolvedQuery::dimension_param_name(dim_name).to_uppercase(),
                    values,
                ));
            }
        };

        let get = |name: &str| layer.dimensions.get(name);

        if let Some(run) = get("RUN") {
            let values = if self.latest_run {
                run.values.last().cloned().into_iter().collect()
            } else {
                run.values.clone()
            };
            let values = if values.is_empty() {
                run.default.clone().into_iter().collect()
            } else {
                values
            };
            push("RUN", values);
        }
        for name in ["FORECAST", "TIME"] {
            if let Some(dim) = get(name) {
                let mut values = dim.values.clone();
                if self.max_timesteps > 0 && values.len() > self.max_timesteps {
                    values.truncate(self.max_timesteps);
                }
                if values.is_empty() {
                    values = dim.default.clone().into_iter().collect();
                }
                push(name, values);
            }
        }
        if let Some(elev) = get("ELEVATION") {
            let mut values = elev.values.clone();
            if values.is_empty() {
                values = elev.default.clone().into_iter().collect();
            }
            push("ELEVATION", values);
        }
        if get("STATION").is_some() {
            push(
                "STATION",
                vec![SAMPLE_STATION.to_string()],
            );
        }
        if get("PLACE").is_some() {
            push(
                "PLACE",
                vec![SAMPLE_LINESTRING.to_string()],
            );
        }
        for name in ["ISOTHERM", "THRESHOLD"] {
            if let Some(dim) = get(name) {
                let mut values = dim.values.clone();
                if values.is_empty() {
                    values = dim.default.clone().into_iter().collect();
                }
                push(name, values);
            }
        }
        out
    }

    /// Bounding box for a layer: geographic, else first advertised,
    /// else the whole world (Python parity).
    fn layer_bbox(layer: &Layer) -> (f64, f64, f64, f64) {
        if let Some(bb) = &layer.geographic_bbox {
            return (bb.minx, bb.miny, bb.maxx, bb.maxy);
        }
        if let Some(bb) = layer.bounding_boxes.values().next() {
            return (bb.minx, bb.miny, bb.maxx, bb.maxy);
        }
        (-180.0, -90.0, 180.0, 90.0)
    }

    /// Generate the full sweep of requests (styles x dimension combos).
    pub fn generate(&mut self) -> &[HammerRequest] {
        use crate::edr::query::urlencode;
        self.requests.clear();
        for layer in &self.layers {
            let Some(layer_name) = layer.name.clone() else {
                continue;
            };
            let styles: Vec<String> = if layer.styles.is_empty() {
                vec!["default".to_string()]
            } else {
                layer.styles.iter().map(|s| s.name.clone()).collect()
            };
            let crs = layer
                .crs_list
                .first()
                .cloned()
                .unwrap_or_else(|| "CRS:84".to_string());
            let bbox = Self::layer_bbox(layer);
            let dims = self.dim_values(layer);

            // Cartesian product of dimension values.
            let mut combos: Vec<Vec<(String, String)>> = vec![Vec::new()];
            for (param, values) in &dims {
                let mut next = Vec::with_capacity(combos.len() * values.len());
                for combo in &combos {
                    for value in values {
                        let mut c = combo.clone();
                        c.push((param.clone(), value.clone()));
                        next.push(c);
                    }
                }
                combos = next;
            }

            for style in &styles {
                for combo in &combos {
                    let mut params: Vec<(String, String)> = vec![
                        ("SERVICE".into(), "WMS".into()),
                        ("VERSION".into(), "1.3.0".into()),
                        ("REQUEST".into(), "GetMap".into()),
                        ("LAYERS".into(), layer_name.clone()),
                        (
                            "BBOX".into(),
                            format!("{},{},{},{}", bbox.0, bbox.1, bbox.2, bbox.3),
                        ),
                        ("WIDTH".into(), self.width.to_string()),
                        ("HEIGHT".into(), self.height.to_string()),
                        ("CRS".into(), crs.clone()),
                        ("FORMAT".into(), "image/png".into()),
                        ("STYLES".into(), style.clone()),
                    ];
                    params.extend(combo.iter().cloned());

                    // Python filename scheme: layer[_style][_PARAM_val...].png
                    let mut parts = vec![layer_name.clone()];
                    if style != "default" {
                        parts.push(style.clone());
                    }
                    for (param, value) in combo {
                        let clean = value.replace(':', "-").replace('/', "-").replace(' ', "_");
                        parts.push(format!("{param}_{clean}"));
                    }
                    let filename = format!("{}.png", parts.join("_"));

                    let qs: Vec<String> = params
                        .iter()
                        .map(|(k, v)| format!("{}={}", urlencode(k), urlencode(v)))
                        .collect();
                    let url = format!("{}?{}", self.base_url, qs.join("&"));
                    self.requests.push(HammerRequest {
                        layer: layer_name.clone(),
                        style: style.clone(),
                        dimensions: combo.clone(),
                        params,
                        url,
                        filename,
                    });
                }
            }
        }
        if self.limit > 0 && self.requests.len() > self.limit {
            self.requests.truncate(self.limit);
        }
        &self.requests
    }

    /// Execute the sweep with a progress bar, saving images when
    /// enabled. Failures print to stderr as they happen (Python
    /// parity) and are counted in the returned stats.
    pub fn run(&mut self) -> Result<HammerStats> {
        if self.requests.is_empty() {
            self.generate();
        }
        use indicatif::{ProgressBar, ProgressStyle};

        let client = reqwest::blocking::Client::builder()
            .timeout(std::time::Duration::from_secs(30))
            .user_agent("wms-cli/0.1-rust")
            .build()?;

        let bar = ProgressBar::new(self.requests.len() as u64);
        bar.set_style(
            ProgressStyle::with_template(
                "{spinner:.cyan} WMS Hammer ({msg}) {wide_bar:.cyan} \
                 {pos}/{len} [{elapsed_precise}] {per_sec}",
            )
            .unwrap()
            .progress_chars("█░"),
        );
        bar.set_message(format!("{} workers", self.workers));

        let stats_mu = Mutex::new(HammerStats::default());
        let next = AtomicUsize::new(0);
        let start = Instant::now();

        std::thread::scope(|scope| {
            for _ in 0..self.workers.max(1) {
                scope.spawn(|| loop {
                    let i = next.fetch_add(1, Ordering::Relaxed);
                    if i >= self.requests.len() {
                        break;
                    }
                    let req = &self.requests[i];
                    match client.get(&req.url).send() {
                        Ok(resp) => {
                            let status = resp.status();
                            let ok = status.is_success();
                            match resp.bytes() {
                                Ok(body) => {
                                    let nbytes = body.len() as u64;
                                    if ok {
                                        if self.save {
                                            let path = self.output_dir.join(&req.filename);
                                            if let Some(parent) = path.parent() {
                                                let _ = std::fs::create_dir_all(parent);
                                            }
                                            if let Err(e) = std::fs::write(&path, &body) {
                                                eprintln!("✗ {}: {e}", req.layer);
                                            }
                                        }
                                        let mut st = stats_mu.lock().unwrap();
                                        st.success += 1;
                                        st.bytes += nbytes;
                                    } else {
                                        eprintln!("✗ {}: HTTP {status}", req.layer);
                                        stats_mu.lock().unwrap().failed += 1;
                                    }
                                }
                                Err(e) => {
                                    eprintln!("✗ {}: {e}", req.layer);
                                    stats_mu.lock().unwrap().failed += 1;
                                }
                            }
                        }
                        Err(e) => {
                            eprintln!("✗ {}: {e}", req.layer);
                            stats_mu.lock().unwrap().failed += 1;
                        }
                    }
                    bar.inc(1);
                });
            }
        });
        bar.finish_and_clear();

        let mut stats = stats_mu.into_inner().unwrap();
        stats.total = self.requests.len();
        stats.start = Some(start);
        stats.end = Some(Instant::now());
        Ok(stats)
    }
}
