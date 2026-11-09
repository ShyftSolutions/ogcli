//! EDR hammer: full-dimension-sweep load generator.
//!
//! Rust port of `src/edr/hammer.py` using a scoped thread pool with
//! atomic work claiming. RPS pacing is computed per-request-index
//! (allowed start = t0 + i/rps), giving drift-free global pacing
//! without a coordinator thread.

use std::collections::HashMap;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use anyhow::Result;

use crate::edr::client::EdrClient;
use crate::edr::parser::EdrCollection;

/// One planned EDR request.
#[derive(Debug, Clone)]
pub struct HammerRequest {
    pub collection: String,
    pub query_type: String,
    pub params: Vec<(String, String)>,
    pub url: String,
    pub label: String,
}

/// Run statistics (Python-parity report/histogram).
#[derive(Debug, Default)]
pub struct HammerStats {
    pub total: usize,
    pub success: usize,
    pub failed: usize,
    pub bytes: u64,
    pub latencies: Vec<f64>,
    pub status_counts: HashMap<String, usize>,
    pub warmup: usize,
    pub start: Option<Instant>,
    pub end: Option<Instant>,
}

impl HammerStats {
    pub fn record(&mut self, ok: bool, nbytes: u64, latency: f64,
                  status: Option<u16>, is_warmup: bool) {
        if ok {
            self.success += 1;
        } else {
            self.failed += 1;
        }
        self.bytes += nbytes;
        if is_warmup {
            self.warmup += 1;
        } else {
            self.latencies.push(latency);
        }
        if let Some(status) = status {
            *self.status_counts.entry(status.to_string()).or_insert(0) += 1;
        }
    }

    fn sorted_latencies(&self) -> Vec<f64> {
        let mut s = self.latencies.clone();
        s.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
        s
    }

    fn pct(&self, frac: f64) -> f64 {
        let s = self.sorted_latencies();
        if s.is_empty() {
            return 0.0;
        }
        let idx = ((s.len() as f64 * frac) as usize).min(s.len() - 1);
        s[idx]
    }

    pub fn duration_s(&self) -> f64 {
        match (self.start, self.end) {
            (Some(a), Some(b)) => b.duration_since(a).as_secs_f64(),
            _ => 0.0,
        }
    }

    /// ASCII latency histogram lines.
    pub fn histogram(&self, buckets: usize, width: usize) -> Vec<String> {
        let s = self.sorted_latencies();
        if s.is_empty() {
            return Vec::new();
        }
        let (lo, hi) = (s[0], *s.last().unwrap());
        let hi = if hi <= lo { lo + 1e-9 } else { hi };
        let mut counts = vec![0usize; buckets];
        for v in &s {
            let idx = (((v - lo) / (hi - lo) * buckets as f64) as usize)
                .min(buckets - 1);
            counts[idx] += 1;
        }
        let peak = *counts.iter().max().unwrap().max(&1);
        counts
            .iter()
            .enumerate()
            .map(|(i, c)| {
                let lo_ms = (lo + (hi - lo) * i as f64 / buckets as f64) * 1000.0;
                let hi_ms = (lo + (hi - lo) * (i + 1) as f64 / buckets as f64) * 1000.0;
                let bar = "█".repeat(c * width / peak);
                format!("  {lo_ms:6.0}─{hi_ms:<6.0}ms │ {bar} {c}")
            })
            .collect()
    }

    /// Human-readable final report (Python parity).
    pub fn report(&self) -> String {
        let mut lines = vec![format!(
            "Requests: {} total, {} ok, {} failed",
            self.total, self.success, self.failed
        )];
        if self.warmup > 0 {
            lines.push(format!(
                "Warmup:   {} requests excluded from latency stats",
                self.warmup
            ));
        }
        if !self.status_counts.is_empty() {
            let mut codes: Vec<(&String, &usize)> = self.status_counts.iter().collect();
            codes.sort();
            let breakdown = codes
                .iter()
                .map(|(c, n)| format!("{c}: {n}"))
                .collect::<Vec<_>>()
                .join("  ");
            lines.push(format!("Status:   {breakdown}"));
        }
        let dur = self.duration_s();
        if dur > 0.0 {
            lines.push(format!(
                "Duration: {dur:.1}s  ({:.1} req/s)",
                self.total as f64 / dur
            ));
        } else {
            lines.push("Duration: -".into());
        }
        lines.push(format!("Data:     {:.1} KB", self.bytes as f64 / 1024.0));
        if !self.latencies.is_empty() {
            let avg = self.latencies.iter().sum::<f64>() / self.latencies.len() as f64;
            let max = self.latencies.iter().cloned().fold(0.0_f64, f64::max);
            lines.push(format!(
                "Latency:  avg {:.0}ms  p50 {:.0}ms  p95 {:.0}ms  max {:.0}ms",
                avg * 1000.0,
                self.pct(0.50) * 1000.0,
                self.pct(0.95) * 1000.0,
                max * 1000.0
            ));
            lines.push("Histogram (ms):".into());
            lines.extend(self.histogram(8, 28));
        }
        lines.join("\n")
    }

    /// Machine-readable results (for --report).
    pub fn to_report_json(&self) -> serde_json::Value {
        let dur = self.duration_s();
        let round1 = |v: f64| ((v * 1000.0 * 10.0).round() / 10.0).to_string().parse::<f64>().unwrap_or(0.0);
        serde_json::json!({
            "total": self.total,
            "ok": self.success,
            "failed": self.failed,
            "warmup_excluded": self.warmup,
            "status_counts": self.status_counts,
            "data_kb": ((self.bytes as f64 / 1024.0) * 10.0).round() / 10.0,
            "duration_s": ((dur * 100.0).round() / 100.0),
            "rps_actual": if dur > 0.0 {
                serde_json::json!(((self.total as f64 / dur) * 10.0).round() / 10.0)
            } else { serde_json::Value::Null },
            "latency_ms": if self.latencies.is_empty() {
                serde_json::Value::Null
            } else {
                let avg = self.latencies.iter().sum::<f64>() / self.latencies.len() as f64;
                let min = self.latencies.iter().cloned().fold(f64::MAX, f64::min);
                let max = self.latencies.iter().cloned().fold(f64::MIN, f64::max);
                serde_json::json!({
                    "avg": round1(avg),
                    "min": round1(min),
                    "p50": round1(self.pct(0.50)),
                    "p95": round1(self.pct(0.95)),
                    "max": round1(max),
                })
            },
        })
    }
}

fn sample_evenly(values: &[String], limit: usize) -> Vec<String> {
    if limit == 0 || values.len() <= limit {
        return values.to_vec();
    }
    let step = values.len() as f64 / limit as f64;
    (0..limit)
        .map(|i| values[(i as f64 * step) as usize].clone())
        .collect()
}

/// Sample param names down to `limit` (empty string = no filter).
fn sample_params(names: &[String], limit: usize) -> Vec<String> {
    if names.is_empty() {
        return vec![String::new()];
    }
    let sampled = sample_evenly(names, limit);
    if sampled.is_empty() {
        vec![String::new()]
    } else {
        sampled
    }
}

pub struct EdrHammer<'a> {
    pub client: EdrClient,
    pub collections: &'a [EdrCollection],
    pub workers: usize,
    pub query_type: String,
    pub output_dir: PathBuf,
    pub save: bool,
    pub dry_run: bool,
    pub max_timesteps: usize,
    pub max_params: usize,
    pub max_levels: usize,
    pub points: usize,
    pub limit: usize,       // 0 = unlimited (bounded by duration or sweep)
    pub duration: Duration, // zero = single pass
    pub rps: f64,           // 0 = unpaced
    pub warmup: usize,
    pub requests: Vec<HammerRequest>,
}

impl<'a> EdrHammer<'a> {
    pub fn new(base_url: &str, api_key: &str, collections: &'a [EdrCollection]) -> Self {
        EdrHammer {
            client: EdrClient::new(base_url, api_key),
            collections,
            workers: 20,
            query_type: "position".into(),
            output_dir: PathBuf::from("edr_hammer_output"),
            save: true,
            dry_run: false,
            max_timesteps: 0,
            max_params: 0,
            max_levels: 0,
            points: 1,
            limit: 0,
            duration: Duration::ZERO,
            rps: 0.0,
            warmup: 0,
            requests: Vec::new(),
        }
    }

    fn geometry_param(&self, c: &EdrCollection) -> Option<(String, String)> {
        if self.query_type == "cube" {
            let bbox = c.bbox.as_ref()?;
            if bbox.len() == 4 {
                return Some((
                    "bbox".into(),
                    format!("{},{},{},{}", bbox[0], bbox[1], bbox[2], bbox[3]),
                ));
            }
            return None;
        }
        let bbox = match c.bbox.as_ref() {
            Some(b) if b.len() == 4 => b,
            _ => {
                let (lon, lat) = c.center()?;
                return Some(("coords".into(), format!("POINT({lon} {lat})")));
            }
        };
        let (minx, miny, maxx, maxy) = (bbox[0], bbox[1], bbox[2], bbox[3]);
        if self.query_type == "area" {
            return Some((
                "coords".into(),
                format!(
                    "POLYGON(({minx} {miny},{maxx} {miny},{maxx} {maxy},{minx} {maxy},{minx} {miny}))"
                ),
            ));
        }
        // Point-based: random point inside the bbox, padded 5%
        let pad_x = (maxx - minx) * 0.05;
        let pad_y = (maxy - miny) * 0.05;
        let lon = rand_range(minx + pad_x, maxx - pad_x);
        let lat = rand_range(miny + pad_y, maxy - pad_y);
        Some(("coords".into(), format!("POINT({lon:.4} {lat:.4})")))
    }

    /// Generate the full sweep of requests.
    pub fn generate(&mut self) -> &[HammerRequest] {
        self.requests.clear();
        for c in self.collections {
            if !c.data_queries.is_empty() && !c.supports(&self.query_type) {
                continue;
            }
            let timesteps = sample_evenly(&c.temporal_values, self.max_timesteps);
            let levels = sample_evenly(&c.vertical_values, self.max_levels);
            let param_names: Vec<String> = {
                let mut names: Vec<String> =
                    c.parameter_names.keys().cloned().collect();
                names.sort();
                names
            };
            let params = sample_params(&param_names, self.max_params);

            for _ in 0..self.points.max(1) {
                let Some((gk, gv)) = self.geometry_param(c) else {
                    continue;
                };
                for param in &params {
                    // Without advertised timesteps/levels, one (None) slot
                    let t_axis = if timesteps.is_empty() {
                        vec![String::new()]
                    } else {
                        timesteps.clone()
                    };
                    let z_axis = if levels.is_empty() {
                        vec![String::new()]
                    } else {
                        levels.clone()
                    };
                    for t in &t_axis {
                        for z in &z_axis {
                            let mut label = c.id.clone();
                            if !param.is_empty() {
                                label.push(':');
                                label.push_str(param);
                            }
                            if !t.is_empty() {
                                label.push(':');
                                label.push_str(t);
                            }
                            if !z.is_empty() {
                                label.push_str(&format!(":z{z}"));
                            }
                            self.requests.push(HammerRequest {
                                collection: c.id.clone(),
                                query_type: self.query_type.clone(),
                                params: vec![
                                    (gk.clone(), gv.clone()),
                                    ("datetime".into(), t.clone()),
                                    ("z".into(), z.clone()),
                                    ("parameter-name".into(), param.clone()),
                                ],
                                url: self.client.collections_url(
                                    &c.id,
                                    Some(&self.query_type),
                                    None,
                                ),
                                label,
                            });
                        }
                    }
                }
            }
        }
        if self.limit > 0 && self.requests.len() > self.limit {
            self.requests.truncate(self.limit);
        }
        &self.requests
    }

    /// Total request count that bounds the run, if any.
    /// - Duration mode: None (cycle until deadline) unless a limit caps it
    /// - Single-pass mode (default): the generated request count
    fn effective_total(&self) -> Option<usize> {
        if !self.duration.is_zero() {
            if self.limit > 0 {
                Some(self.limit)
            } else {
                None
            }
        } else {
            Some(self.requests.len())
        }
    }

    /// Execute the sweep with a progress bar. Returns stats.
    pub fn run(&mut self) -> Result<HammerStats> {
        if self.requests.is_empty() {
            self.generate();
        }
        if self.dry_run {
            println!("Dry run: {} requests would be sent", self.requests.len());
            for req in self.requests.iter().take(20) {
                let qs: Vec<String> = req
                    .params
                    .iter()
                    .filter(|(_, v)| !v.is_empty())
                    .map(|(k, v)| format!("{k}={v}"))
                    .collect();
                println!("  {}?{}", req.url, qs.join("&"));
            }
            if self.requests.len() > 20 {
                println!("  ... and {} more", self.requests.len() - 20);
            }
            let mut stats = HammerStats::default();
            stats.total = self.requests.len();
            return Ok(stats);
        }

        use indicatif::{ProgressBar, ProgressStyle};
        let known_total = !self.duration.is_zero() || self.limit > 0;
        let mode = if self.rps > 0.0 {
            format!("{} req/s target", self.rps)
        } else {
            format!("{} workers", self.workers)
        };
        let bar = if known_total || !self.requests.is_empty() {
            let total = if self.duration.is_zero() {
                self.requests.len() as u64
            } else {
                0
            };
            if total > 0 {
                let bar = ProgressBar::new(total);
                bar.set_style(
                    ProgressStyle::with_template(
                        "{spinner:.cyan} EDR Hammer ({msg}) {wide_bar:.cyan} \
                         {pos}/{len} [{elapsed_precise}] {per_sec}",
                    )
                    .unwrap()
                    .progress_chars("█░"),
                );
                bar
            } else {
                self.spinner(&mode)
            }
        } else {
            self.spinner(&mode)
        };
        bar.set_message(mode);

        let stats_mu = Mutex::new(HammerStats::default());
        let next = AtomicUsize::new(0);
        let start = Instant::now();
        let deadline = if self.duration.is_zero() {
            None
        } else {
            Some(start + self.duration)
        };
        // Single-pass mode (no duration): run the generated requests
        // exactly once. Duration mode: cycle until the deadline, with
        // limit capping if set. A bare run with neither limit nor
        // duration must NOT cycle forever.
        let effective_total = self.effective_total();

        std::thread::scope(|scope| {
            for _ in 0..self.workers.max(1) {
                scope.spawn(|| loop {
                    let i = next.fetch_add(1, Ordering::Relaxed);
                    if let Some(total) = effective_total {
                        if i >= total {
                            break;
                        }
                    } else if deadline.is_some_and(|dl| Instant::now() >= dl) {
                        break;
                    }
                    let req = &self.requests[i % self.requests.len()];
                    if self.rps > 0.0 {
                        let allowed =
                            start + Duration::from_secs_f64(i as f64 / self.rps);
                        let now = Instant::now();
                        if allowed > now {
                            std::thread::sleep(allowed - now);
                        }
                    }
                    let t0 = Instant::now();
                    let (status, body, ctype) =
                        self.client.query_raw(&req.url, &req.params);
                    let latency = t0.elapsed().as_secs_f64();
                    let ok = status >= 200 && status < 300;
                    if !ok {
                        bar.suspend(|| {
                            eprintln!("  ✗ [{}] {}: HTTP {}", status, req.label, status);
                        });
                    }
                    if self.save && ok && !body.is_empty() {
                        let ext = if ctype.to_lowercase().contains("json") {
                            "json"
                        } else if ctype.to_lowercase().contains("netcdf") {
                            "nc"
                        } else {
                            "bin"
                        };
                        let _ = std::fs::create_dir_all(&self.output_dir);
                        let path = self
                            .output_dir
                            .join(format!("{}.{}", sanitize(&req.label), ext));
                        let _ = std::fs::write(path, &body);
                    }
                    {
                        let mut st = stats_mu.lock().unwrap();
                        st.total += 1;
                        st.record(
                            ok,
                            body.len() as u64,
                            latency,
                            if status > 0 { Some(status) } else { None },
                            i < self.warmup,
                        );
                    }
                    bar.inc(1);
                    if deadline.is_some_and(|dl| Instant::now() >= dl) {
                        break;
                    }
                });
            }
        });

        bar.finish_and_clear();
        let mut st = stats_mu.into_inner().unwrap();
        st.start = Some(start);
        st.end = Some(Instant::now());
        Ok(st)
    }

    fn spinner(&self, mode: &str) -> indicatif::ProgressBar {
        use indicatif::{ProgressBar, ProgressStyle};
        let bar = ProgressBar::new_spinner();
        bar.set_style(
            ProgressStyle::with_template(&format!(
                "{{spinner:.cyan}} EDR Hammer ({mode}) {{pos}} requests \
                 [{{elapsed_precise}}] {{per_sec}}"
            ))
            .unwrap(),
        );
        bar
    }

    /// Write the --report JSON.
    pub fn write_report(&self, stats: &HammerStats, path: &Path) -> Result<()> {
        let mut f = std::fs::File::create(path)?;
        writeln!(f, "{}", serde_json::to_string_pretty(&stats.to_report_json())?)?;
        Ok(())
    }
}

fn sanitize(name: &str) -> String {
    name.replace(':', "-")
        .replace('/', "-")
        .replace(' ', "_")
        .replace('?', "")
        .replace('&', "_")
}

/// Random in [lo, hi) without a rand dependency (xorshift seeded
/// from the clock, mixed with the thread id).
fn rand_range(lo: f64, hi: f64) -> f64 {
    use std::cell::Cell;
    thread_local! {
        static SEED: Cell<u64> = const { Cell::new(0) };
    }
    SEED.with(|s| {
        let mut x = s.get();
        if x == 0 {
            let now = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos() as u64)
                .unwrap_or(0x9E37_79B9_7F4A_7C15);
            let tid = format!("{:?}", std::thread::current().id());
            let tid = tid.bytes().fold(0xcbf2_9ce4_8422_2325u64, |h, b| {
                (h ^ b as u64).wrapping_mul(0x1000_0000_01b3)
            });
            x = now ^ tid;
            x = x.max(1);
        }
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        s.set(x);
        lo + (x as f64 / u64::MAX as f64) * (hi - lo)
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::edr::parser::parse_collections;

    const FIXTURE: &str = r#"
    {
      "collections": [
        {
          "id": "test-surface",
          "title": "Test Surface",
          "extent": {
            "spatial": {"bbox": [[-110.0, 35.0, -100.0, 42.0]]},
            "temporal": {
              "interval": [["2026-11-04T12:00:00Z", "2026-11-04T18:00:00Z"]],
              "values": ["2026-11-04T12:00:00Z", "2026-11-04T15:00:00Z",
                         "2026-11-04T18:00:00Z"]
            }
          },
          "data_queries": {"position": {}, "area": {}},
          "parameter_names": {"TMP": {"id": "TMP"}, "RH": {"id": "RH"}}
        },
        {
          "id": "test-isobaric",
          "extent": {
            "spatial": {"bbox": [[-110.0, 35.0, -100.0, 42.0]]},
            "temporal": {"interval": [["a", "b"]]},
            "vertical": {"interval": [[500, 850]], "values": [500, 850]}
          },
          "data_queries": {"position": {}, "cube": {}},
          "parameter_names": {"HGT": {"id": "HGT"}}
        }
      ]
    }"#;

    fn collections() -> Vec<EdrCollection> {
        parse_collections(FIXTURE, "").unwrap().collections
    }

    fn hammer(collections: &[EdrCollection]) -> EdrHammer<'_> {
        let mut h = EdrHammer::new("https://example.com/edr", "", collections);
        h.save = false;
        h
    }

    #[test]
    fn generate_full_sweep() {
        let cols = collections();
        let mut h = hammer(&cols);
        let reqs = h.generate().to_vec();
        // surface: 3 timesteps x 2 params = 6; isobaric: 1 x 1 x 2 levels = 2
        assert_eq!(reqs.len(), 8);
        let surface: Vec<_> = reqs.iter().filter(|r| r.collection == "test-surface").collect();
        assert_eq!(surface.len(), 6);
        assert!(surface.iter().all(|r| r.params[0].0 == "coords"
            && r.params[0].1.starts_with("POINT(")));
        assert!(surface
            .iter()
            .all(|r| r.params[3].1 == "TMP" || r.params[3].1 == "RH"));
    }

    #[test]
    fn generate_cube_uses_bbox() {
        let cols = collections();
        let mut h = hammer(&cols);
        h.query_type = "cube".into();
        let reqs = h.generate().to_vec();
        assert!(reqs.iter().all(|r| r.collection == "test-isobaric"));
        assert!(reqs
            .iter()
            .all(|r| r.params[0] == ("bbox".to_string(), "-110,35,-100,42".to_string())));
    }

    #[test]
    fn generate_sampling_and_limit() {
        let cols = collections();
        let mut h = hammer(&cols);
        h.max_timesteps = 2;
        h.max_params = 1;
        h.limit = 3;
        let reqs = h.generate().to_vec();
        assert_eq!(reqs.len(), 3);
        let surface_params: Vec<&String> = reqs
            .iter()
            .filter(|r| r.collection == "test-surface")
            .map(|r| &r.params[3].1)
            .collect();
        // max_params=1 -> exactly one distinct parameter for surface
        let unique: std::collections::HashSet<&String> =
            surface_params.into_iter().collect();
        assert_eq!(unique.len(), 1);
    }

    #[test]
    fn generate_skips_unsupported_query_type() {
        let cols = collections();
        let mut h = hammer(&cols);
        h.query_type = "radius".into();
        assert!(h.generate().is_empty());
    }

    #[test]
    fn effective_total_bounds_the_run() {
        let cols = collections();
        let mut h = hammer(&cols);
        h.generate();
        // Default single pass: exactly the generated requests, never more
        assert_eq!(h.effective_total(), Some(h.requests.len()));

        // Explicit limit truncates in single-pass too (generate applies it)
        h.limit = 3;
        h.generate();
        assert_eq!(h.effective_total(), Some(3));

        // Duration mode without limit: open-ended (bounded by deadline)
        h.limit = 0;
        h.duration = std::time::Duration::from_secs(5);
        assert_eq!(h.effective_total(), None);

        // Duration mode with a cap
        h.limit = 7;
        assert_eq!(h.effective_total(), Some(7));
    }

    #[test]
    fn stats_record_report_and_histogram() {
        let mut stats = HammerStats::default();
        stats.start = Some(Instant::now() - Duration::from_secs(2));
        stats.end = Some(Instant::now());
        stats.total = 4;
        stats.record(true, 1024, 0.1, Some(200), false);
        stats.record(true, 1024, 0.3, Some(200), false);
        stats.record(false, 0, 0.2, Some(500), false);
        stats.record(true, 1024, 0.1, Some(200), true); // warmup

        assert_eq!(stats.warmup, 1);
        assert_eq!(stats.latencies.len(), 3);
        let report = stats.report();
        assert!(report.contains("4 total, 3 ok, 1 failed"));
        assert!(report.contains("200: 3"));
        assert!(report.contains("500: 1"));
        assert!(report.contains("Histogram"));
        assert_eq!(stats.histogram(4, 10).len(), 4);

        let json = stats.to_report_json();
        assert_eq!(json["total"], 4);
        assert_eq!(json["warmup_excluded"], 1);
        assert_eq!(json["latency_ms"]["p50"], 200.0);
    }
}
