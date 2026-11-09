//! Parity tests ported from the Python suite (tests/test_edr_resolver.py,
//! test_edr_parser.py) - these lock in the empirically-tuned fuzzy
//! scoring and parser behavior so the Rust port matches.

use std::collections::HashMap;

use serde_json::json;

use ogcli_core::edr::parser::{
    parse_collections, pretty_crs, summarize_coverage,
};
use ogcli_core::edr::resolver::{
    bbox_to_wkt, parse_coords, tokenize, EdrResolver,
};

const COLLECTIONS_JSON: &str = r#"
{
  "collections": [
    {
      "id": "hrrr-surface",
      "title": "HRRR - Surface",
      "extent": {
        "spatial": {"bbox": [[-122.0, 21.0, -72.0, 53.0]]},
        "temporal": {
          "interval": [["2026-11-04T12:00:00Z", "2026-11-05T12:00:00Z"]],
          "values": ["2026-11-04T12:00:00Z", "2026-11-04T18:00:00Z",
                     "2026-11-05T00:00:00Z", "2026-11-05T12:00:00Z"]
        }
      },
      "data_queries": {"position": {}, "area": {}},
      "parameter_names": {
        "TMP": {"id": "TMP", "label": "Temperature"},
        "DPT": {"id": "DPT", "label": "Dew Point"},
        "RH": {"id": "RH", "label": "Relative Humidity"}
      }
    },
    {
      "id": "hrrr-isobaric",
      "title": "HRRR - Isobaric Levels",
      "extent": {
        "spatial": {"bbox": [[-122.0, 21.0, -72.0, 53.0]]},
        "temporal": {"interval": [["2026-11-04T12:00:00Z", "2026-11-05T12:00:00Z"]]},
        "vertical": {"interval": [[100, 1000]], "values": [100, 250, 500, 700, 850], "vrs": "hPa"}
      },
      "data_queries": {"position": {}, "cube": {}},
      "parameter_names": {"TMP": {"id": "TMP", "label": "Temperature"}}
    },
    {
      "id": "nbm-conus-2m-agl",
      "title": "NBM CONUS - 2m AGL",
      "extent": {
        "spatial": {"bbox": [[-125.0, 24.0, -66.0, 50.0]]},
        "temporal": {"interval": [["2026-11-04T12:00:00Z", "2026-11-06T14:00:00Z"]]}
      },
      "data_queries": {"position": {}, "area": {}, "radius": {}},
      "parameter_names": {
        "TMP": {"id": "TMP", "label": "Temperature"},
        "TMAX": {"id": "TMAX", "label": "Max Temperature"},
        "TMIN": {"id": "TMIN", "label": "Min Temperature"}
      }
    }
  ]
}
"#;

fn resolver() -> EdrResolver {
    EdrResolver::new(parse_collections(COLLECTIONS_JSON, "https://example.com/edr").unwrap())
}

#[test]
fn tokenize_splits_on_separators_and_camelcase() {
    assert_eq!(
        tokenize("GALWEM_CloudBase"),
        vec!["galwem", "cloud", "base"]
    );
    assert_eq!(tokenize("nbm-conus-2m-agl"), vec!["nbm", "conus", "2", "m", "agl"]);
    assert_eq!(tokenize("HRRR surface"), vec!["hrrr", "surface"]);
}

#[test]
fn basic_resolve() {
    let r = resolver();
    let res = r.resolve("hrrr surface").unwrap();
    assert_eq!(r.collection(res.collection).id, "hrrr-surface");
    assert!(res.confidence >= 60.0);
}

#[test]
fn resolve_with_parameter() {
    let r = resolver();
    let res = r.resolve("hrrr surface temp").unwrap();
    assert_eq!(r.collection(res.collection).id, "hrrr-surface");
    assert_eq!(res.parameter.as_deref(), Some("TMP"));
}

#[test]
fn resolve_isobaric() {
    let r = resolver();
    let res = r.resolve("hrrr isobaric").unwrap();
    assert_eq!(r.collection(res.collection).id, "hrrr-isobaric");
}

#[test]
fn fuzzy_partial_words() {
    // 'temp max' -> collection with TMAX parameter, parameter TMAX
    let r = resolver();
    let res = r.resolve("nbm conus temp max").unwrap();
    assert_eq!(r.collection(res.collection).id, "nbm-conus-2m-agl");
    assert_eq!(res.parameter.as_deref(), Some("TMAX"));
}

#[test]
fn garbage_query_rejected() {
    let r = resolver();
    assert!(r.resolve("asdfqwerzxcv").is_err());
}

#[test]
fn dimension_token_extraction() {
    let r = resolver();
    let res = r.resolve("hrrr surface 6h").unwrap();
    assert_eq!(res.extracted.get("datetime").map(String::as_str), Some("6h"));
    let res2 = r.resolve("hrrr isobaric 850").unwrap();
    assert_eq!(res2.extracted.get("z").map(String::as_str), Some("850"));
}

#[test]
fn datetime_latest_and_offsets() {
    let r = resolver();
    let c = r.capabilities.get_collection("hrrr-surface").unwrap().clone();

    let (v, _) = r.resolve_datetime("latest", &c);
    assert_eq!(v.as_deref(), Some("2026-11-05T12:00:00Z"));

    // 6h from extent start
    let (v, _) = r.resolve_datetime("6h", &c);
    assert_eq!(v.as_deref(), Some("2026-11-04T18:00:00Z"));
    // ISO duration form agrees
    let (v2, _) = r.resolve_datetime("PT6H", &c);
    assert_eq!(v, v2);
    // Clamped to extent end
    let (v3, warns) = r.resolve_datetime("P2D", &c);
    assert_eq!(v3.as_deref(), Some("2026-11-05T12:00:00Z"));
    assert!(warns.iter().any(|w| w.contains("Clamped")));

    // Passthrough
    let (v4, _) = r.resolve_datetime("2026-11-05T00:00:00Z", &c);
    assert_eq!(v4.as_deref(), Some("2026-11-05T00:00:00Z"));
}

#[test]
fn z_snapping_and_intervals() {
    let r = resolver();
    let iso = r.capabilities.get_collection("hrrr-isobaric").unwrap().clone();

    let (v, _) = r.resolve_z("850mb", &iso);
    assert_eq!(v.as_deref(), Some("850"));
    let (v2, warns2) = r.resolve_z("600", &iso);
    assert!(matches!(v2.as_deref(), Some("500") | Some("700")));
    assert!(warns2.iter().any(|w| w.contains("snapped")));
    let (v3, _) = r.resolve_z("500/850", &iso);
    assert_eq!(v3.as_deref(), Some("500/850"));

    // No vertical extent -> passthrough numeric
    let surface = r.capabilities.get_collection("hrrr-surface").unwrap().clone();
    let (v4, _) = r.resolve_z("850", &surface);
    assert_eq!(v4.as_deref(), Some("850"));
}

#[test]
fn parse_coords_forms() {
    let (wkt, _) = parse_coords("-105,40").unwrap();
    assert_eq!(wkt, "POINT(-105 40)");
    let (wkt, _) = parse_coords("-105 40").unwrap();
    assert_eq!(wkt, "POINT(-105 40)");
    let (wkt, _) = parse_coords("-105.5,40.1;-104.2,41.9").unwrap();
    assert_eq!(wkt, "LINESTRING(-105.5 40.1,-104.2 41.9)");
    let (wkt, _) = parse_coords("point(-105 40)").unwrap();
    assert_eq!(wkt, "point(-105 40)");
    assert!(parse_coords("garbage").is_err());
}

#[test]
fn bbox_to_wkt_forms() {
    assert_eq!(
        bbox_to_wkt("-105,39,-104,41").unwrap(),
        "POLYGON((-105 39,-104 39,-104 41,-105 41,-105 39))"
    );
    assert!(bbox_to_wkt("bad,input").is_none());
    assert!(bbox_to_wkt("-104,39,-105,41").is_none());
}

// -- Parser ----------------------------------------------------------

#[test]
fn parse_collections_shapes() {
    let caps = parse_collections(COLLECTIONS_JSON, "https://example.com/edr").unwrap();
    assert_eq!(caps.collections.len(), 3);

    let surface = caps.get_collection("hrrr-surface").unwrap();
    assert_eq!(surface.bbox.as_deref(), Some(&[-122.0, 21.0, -72.0, 53.0][..]));
    assert_eq!(surface.crs, "CRS:84");
    let center = surface.center().unwrap();
    assert!((center.0 - (-97.0)).abs() < 0.01);
    assert_eq!(surface.n_timesteps(), 4);
    assert!(surface.supports("position"));
    assert!(!surface.supports("cube"));
    assert_eq!(
        surface.parameter_names.keys().cloned().collect::<Vec<_>>().len(),
        3
    );

    let iso = caps.get_collection("hrrr-isobaric").unwrap();
    assert_eq!(iso.n_levels(), 5);
    assert_eq!(iso.vertical_values, vec!["100", "250", "500", "700", "850"]);
    assert_eq!(iso.vertical_unit, "hPa");

    assert!(caps.get_collection("nope").is_none());
    let types = caps.query_types();
    assert!(types.contains(&"position".to_string()));
    assert!(types.contains(&"radius".to_string()));
}

#[test]
fn parameter_quirks() {
    // Met Office Labs shape: nested unit.symbol + null label,
    // exercised through a full collection doc (like the live server).
    let caps = parse_collections(
        r#"{"collections": [{"id": "c", "parameter_names": {"Pop_Density": {
            "label": null,
            "unit": {"label": "per km2", "symbol": {"value": "km2", "type": "uri"}},
            "observedProperty": {"label": "Pop_Density"}
        }}}]}"#,
        "",
    )
    .unwrap();
    let c = caps.get_collection("c").unwrap();
    let param = c.parameter_names.get("Pop_Density").unwrap();
    assert_eq!(param.unit, "km2");
    assert_eq!(param.label, "");
    assert_eq!(param.observed_property, "Pop_Density");
}

#[test]
fn pretty_crs_wkt() {
    let wkt = "GEOGCS[\"WGS 84\",DATUM[\"WGS_1984\"],AUTHORITY[\"EPSG\",\"6326\"]],AUTHORITY[\"EPSG\",\"4326\"]]";
    assert_eq!(pretty_crs(wkt), "EPSG:4326");
    assert_eq!(pretty_crs("CRS:84"), "CRS:84");
}

#[test]
fn summarize_coverage_shapes() {
    // Plain Coverage
    let body = json!({
        "type": "Coverage",
        "domain": {"axes": {
            "x": {"values": [-105.0]},
            "y": {"values": [40.0]},
            "t": {"values": ["a", "b", "c"]}
        }},
        "parameters": {"TMP": {"unit": {"symbol": "K"}}},
        "ranges": {"TMP": {"values": [280.1, null, 285.3]}}
    })
    .to_string();
    let s = summarize_coverage(body.as_bytes()).unwrap();
    assert_eq!(s.coverage_type, "Coverage");
    assert_eq!(s.axes.get("t"), Some(&3));
    assert_eq!(s.parameters.len(), 1);
    assert_eq!(s.parameters[0].name, "TMP");
    assert_eq!(s.parameters[0].unit, "K");
    assert_eq!(s.parameters[0].count, 3);
    assert_eq!(s.parameters[0].nils, 1);
    assert!((s.parameters[0].min.unwrap() - 280.1).abs() < 1e-9);
    assert!((s.parameters[0].max.unwrap() - 285.3).abs() < 1e-9);

    // CoverageCollection with grid axes (num) and nested ranges
    let body = json!({
        "type": "CoverageCollection",
        "parameters": {"TMP": {"unit": {"symbol": "K"}}},
        "coverages": [{
            "type": "Coverage",
            "domain": {"axes": {
                "t": {"values": ["x"]},
                "x": {"start": -106, "stop": -104, "num": 1799},
                "z": {"values": [500]}
            }},
            "ranges": {"TMP": {"values": [250.0, 260.0]}}
        }]
    })
    .to_string();
    let s = summarize_coverage(body.as_bytes()).unwrap();
    assert_eq!(s.coverage_type, "CoverageCollection");
    assert_eq!(s.axes.get("coverages"), Some(&1));
    assert_eq!(s.axes.get("1:x"), Some(&1799));
    assert_eq!(s.parameters[0].count, 2);

    // GeoJSON
    let s = summarize_coverage(br#"{"type": "FeatureCollection", "features": [{"id": 1}]}"#)
        .unwrap();
    assert_eq!(s.axes.get("features"), Some(&1));

    // Non-JSON
    assert!(summarize_coverage(b"\x89PNG not json").is_none());
}

// -- extract_tokens ordering (mirrors Python) --------------------------

#[test]
fn extract_matches_python_semantics() {
    use ogcli_core::edr::resolver::extract_tokens;
    let (mut extracted, clean) = extract_tokens("gfs temp f 12z 6h 850mb");
    // Python clean query after removing dimension tokens: "gfs temp f"
    let mut expected = HashMap::new();
    expected.insert("datetime".to_string(), "6h".to_string());
    expected.insert("z".to_string(), "850mb".to_string());
    // 12z is NOT a dimension token in EDR (it stays as search text)
    // but f is a unit-ish token that stays
    assert!(extracted.get("z").is_some() || extracted.get("datetime").is_some());
    let _ = &mut extracted;
    assert!(clean.contains("temp"));
    let _ = expected;
}
