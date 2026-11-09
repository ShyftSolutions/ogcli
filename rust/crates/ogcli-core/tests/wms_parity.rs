//! WMS resolver parity tests - ported from tests/test_smart_resolver.py
//! using the same capabilities fixture.

use std::path::Path;

use ogcli_core::wms::parser::parse_capabilities;
use ogcli_core::wms::resolver::SmartQueryResolver;

fn resolver() -> SmartQueryResolver {
    let xml = std::fs::read_to_string(
        Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/capabilities.xml"),
    )
    .expect("fixture capabilities.xml");
    SmartQueryResolver::new(parse_capabilities(&xml).unwrap())
}

#[test]
fn basic_query() {
    let r = resolver();
    let res = r.resolve("gfs temp").unwrap();
    let layer = &r.layers[res.layer];
    assert!(layer.name.clone().unwrap_or_default().starts_with("GFS_Temperature"));
    assert!(res.confidence >= 80.0);
    assert!(!res.style.is_empty());
    assert!(!res.crs.is_empty());
}

#[test]
fn unit_preferences() {
    let r = resolver();
    let res_f = r.resolve("gfs temp f").unwrap();
    let name = r.layers[res_f.layer].name.clone().unwrap();
    assert!(name.contains("_F") || name.contains("Fahrenheit"), "got {name}");

    let res_c = r.resolve("gfs temp c").unwrap();
    let name_c = r.layers[res_c.layer].name.clone().unwrap();
    assert!(name_c.contains("_C") || name_c.contains("Celsius"), "got {name_c}");
}

#[test]
fn time_expressions() {
    let r = resolver();
    let get = |q: &str, dim: &str| {
        let res = r.resolve(q).unwrap();
        res.dimensions
            .iter()
            .find(|(n, _)| n == dim)
            .map(|(_, v)| v.clone())
    };

    assert_eq!(get("gfs temp 6h", "FORECAST").as_deref(), Some("PT6H"));
    assert_eq!(get("gfs temp 12h", "FORECAST").as_deref(), Some("PT12H"));
    // 2d auto-corrected to the closest advertised duration (PT48H)
    let two_d = get("gfs temp 2d", "FORECAST").unwrap();
    assert!(two_d == "P2D" || two_d == "PT48H", "got {two_d}");
    // 12z -> most recent 12Z run
    let run = get("gfs temp 12z", "RUN").unwrap();
    assert!(run.contains("T12:00:00Z"), "got {run}");
    // latest -> latest run
    assert!(get("gfs temp latest", "RUN").is_some());
}

#[test]
fn multi_dimension_order_independent() {
    let r = resolver();
    for q in ["gfs temp f 12z 6h", "gfs temp 6h f 12z"] {
        let res = r.resolve(q).unwrap();
        let name = r.layers[res.layer].name.clone().unwrap();
        assert!(name.contains("_F") || name.contains("Fahrenheit"), "{q} -> {name}");
        let forecast = res
            .dimensions
            .iter()
            .find(|(n, _)| n == "FORECAST")
            .map(|(_, v)| v.clone())
            .unwrap();
        assert_eq!(forecast, "PT6H");
        assert!(res.dimensions.iter().any(|(n, _)| n == "RUN"));
    }
}

#[test]
fn elevation_warning_or_dimension() {
    let r = resolver();
    let res = r.resolve("gfs temp 850").unwrap();
    let has_elevation = res.dimensions.iter().any(|(n, _)| n == "ELEVATION");
    assert!(has_elevation || res.warnings.iter().any(|w| w.contains("ELEVATION")));
}

#[test]
fn confidence_threshold_rejects_garbage() {
    let r = resolver();
    assert!(r.resolve("asdfqwerzxcv").is_err());
}

#[test]
fn token_extraction() {
    let (dims, units, clean) =
        SmartQueryResolver::extract_tokens("gfs temp f 12z 6h 850mb");
    assert_eq!(clean, "gfs temp");
    assert!(dims.contains_key("RUN"));
    assert!(dims.contains_key("FORECAST"));
    assert!(dims.contains_key("ELEVATION"));
    assert_eq!(units, vec!["fahrenheit".to_string()]);
}

#[test]
fn case_insensitivity() {
    let r = resolver();
    for q in ["gfs temp", "GFS TEMP", "GfS TeMp"] {
        let res = r.resolve(q).unwrap();
        assert!(res.confidence >= 80.0, "{q} -> {:.0}%", res.confidence);
    }
}

#[test]
fn dimension_defaults_filled() {
    let r = resolver();
    let res = r.resolve("gfs temp").unwrap();
    // RUN defaults to the latest run; FORECAST to PT0S
    assert!(res.dimensions.iter().any(|(n, _)| n == "RUN"));
    assert!(res.dimensions.iter().any(|(n, _)| n == "FORECAST"));
}
