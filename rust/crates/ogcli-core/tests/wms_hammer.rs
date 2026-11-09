//! WMS hammer + groups tests, using the shared capabilities fixture.

use std::path::Path;

use ogcli_core::wms::hammer::WmsHammer;
use ogcli_core::wms::parser::{layer_groups, parse_capabilities, WmsCapabilities};

fn caps() -> WmsCapabilities {
    let xml = std::fs::read_to_string(
        Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/capabilities.xml"),
    )
    .expect("fixture capabilities.xml");
    parse_capabilities(&xml).unwrap()
}

fn hammer_for<'a>(caps: &'a WmsCapabilities, needle: &str) -> WmsHammer<'a> {
    let layers: Vec<&ogcli_core::wms::parser::Layer> = caps
        .queryable_layers()
        .into_iter()
        .filter(|l| l.name.as_deref().is_some_and(|n| n.contains(needle)))
        .collect();
    WmsHammer::new("http://example.com/wms", layers)
}

#[test]
fn hammer_full_dimension_and_style_sweep() {
    let caps = caps();
    // GFS_Temperature_F: RUN(7) x FORECAST(7) x styles(default,
    // temperature_rainbow) = 98 requests.
    let mut hammer = hammer_for(&caps, "GFS_Temperature_F");
    assert_eq!(hammer.layers.len(), 1);
    hammer.generate();
    assert_eq!(hammer.requests.len(), 7 * 7 * 2, "generated {:?}", hammer.requests.len());

    let req = &hammer.requests[0];
    assert_eq!(req.layer, "GFS_Temperature_F");
    assert!(req.url.starts_with("http://example.com/wms?"));
    for fragment in [
        "REQUEST=GetMap",
        "LAYERS=GFS_Temperature_F",
        "FORMAT=image%2Fpng",
        "DIM_RUN=",
        "DIM_FORECAST=",
        "WIDTH=800",
        "HEIGHT=600",
    ] {
        assert!(req.url.contains(fragment), "url missing {fragment}: {}", req.url);
    }
}

#[test]
fn hammer_max_timesteps_truncates_forecast() {
    let caps = caps();
    let mut hammer = hammer_for(&caps, "GFS_Temperature_F");
    hammer.max_timesteps = 2;
    hammer.generate();
    // RUN(7) x FORECAST(2) x styles(2)
    assert_eq!(hammer.requests.len(), 7 * 2 * 2);

    let forecasts: Vec<&str> = hammer
        .requests
        .iter()
        .filter_map(|r| {
            r.dimensions
                .iter()
                .find(|(k, _)| k == "DIM_FORECAST")
                .map(|(_, v)| v.as_str())
        })
        .collect();
    let uniq: std::collections::BTreeSet<_> = forecasts.into_iter().collect();
    assert_eq!(uniq.len(), 2, "expected 2 sampled forecast values");
}

#[test]
fn hammer_latest_run_uses_last_value() {
    let caps = caps();
    let mut hammer = hammer_for(&caps, "GFS_Temperature_F");
    hammer.latest_run = true;
    hammer.generate();
    // latest RUN(1) x FORECAST(7) x styles(2)
    assert_eq!(hammer.requests.len(), 7 * 2);
    let run = hammer.requests[0]
        .dimensions
        .iter()
        .find(|(k, _)| k == "DIM_RUN")
        .map(|(_, v)| v.clone())
        .unwrap();
    assert_eq!(run, "2026-11-04T12:00:00Z", "must be the last RUN value");
}

#[test]
fn hammer_limit_truncates() {
    let caps = caps();
    let mut hammer = hammer_for(&caps, "GFS_Temperature_F");
    hammer.limit = 3;
    hammer.generate();
    assert_eq!(hammer.requests.len(), 3);
}

#[test]
fn hammer_filename_scheme_matches_python() {
    let caps = caps();
    let mut hammer = hammer_for(&caps, "GFS_Temperature_F");
    hammer.generate();
    let req = &hammer.requests[0];
    assert!(req.filename.starts_with("GFS_Temperature_F_"));
    assert!(req.filename.ends_with(".png"));
    // default style is omitted from the filename
    assert!(!req.filename.contains("default"));
    // dimension params appear as PARAM_value (':' -> '-')
    assert!(req.filename.contains("DIM_RUN_2026-11-04T12-00-00Z")
        || req.filename.contains("DIM_RUN_2026-11-03"), "got {filename}",
        filename = req.filename);
}

#[test]
fn layer_groups_extracts_model_prefixes() {
    let caps = caps();
    let queryable = caps.queryable_layers();
    let groups = layer_groups(&queryable, None);
    assert!(groups.contains_key("GFS"), "groups: {groups:?}");
    assert!(groups.keys().all(|k| k != "Other"), "unexpected Other group");
    assert!(groups["GFS"].iter().all(|n| n.starts_with("GFS_")));

    // Pattern filter (case-insensitive substring)
    let filtered = layer_groups(&queryable, Some("gal"));
    assert!(filtered.contains_key("GALWEM"));
    assert_eq!(filtered.len(), 1);
}

#[test]
fn layer_groups_no_underscore_goes_to_other() {
    // Synthetic layers: one with, one without an underscore.
    let mut with = ogcli_core::wms::parser::Layer::default();
    with.name = Some("MODEL_Param".into());
    let mut without = ogcli_core::wms::parser::Layer::default();
    without.name = Some("overview".into());
    let layers = vec![&with, &without];

    let groups = layer_groups(&layers, None);
    assert_eq!(groups["MODEL"], vec!["MODEL_Param".to_string()]);
    assert_eq!(groups["Other"], vec!["overview".to_string()]);
}
