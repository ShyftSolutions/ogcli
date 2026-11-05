//! Config/profile tests ported from tests/test_profiles.py.
//!
//! OGCLI_HOME redirects the config dir to a temp directory. These run
//! sequentially in one #[test] because the env var is process-global.

use std::path::PathBuf;

use ogcli_core::edr::config as ec;

fn temp_home(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "ogcli-rust-test-{tag}-{}",
        std::process::id()
    ));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

#[test]
fn edr_config_profiles() {
    let home = temp_home("edr-config");
    std::env::set_var("OGCLI_HOME", &home);
    let home2 = home.clone();

    // -- legacy flat config migrates to "default"
    std::fs::write(
        home.join("edr_config.json"),
        r#"{"server_url": "https://example.com/edr", "api_key": "k", "cache_ttl": 300}"#,
    )
    .unwrap();
    let config = ec::load_config();
    assert_eq!(config.active, "default");
    assert_eq!(config.server_url(), "https://example.com/edr");
    assert_eq!(config.api_key(), "k");
    assert_eq!(config.cache_ttl().as_secs(), 300);

    // -- save/load roundtrip with two profiles + switch
    let mut config = ec::EdrConfig::new();
    config.ensure_profile(Some("folkweather")).server_url = "https://fw.com/edr".into();
    config.ensure_profile(Some("local")).server_url = "http://localhost:8006/ogc/edr".into();
    config.active = "folkweather".into();
    ec::save_config(&config).unwrap();

    let loaded = ec::load_config();
    assert_eq!(loaded.server_url(), "https://fw.com/edr");
    let mut loaded = loaded;
    loaded.set_active("local").unwrap();
    assert_eq!(loaded.server_url(), "http://localhost:8006/ogc/edr");

    // -- flat mirror of the active profile for pre-profiles readers
    let raw: serde_json::Value = serde_json::from_str(
        &std::fs::read_to_string(home.join("edr_config.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(raw["active"], "folkweather");
    assert_eq!(raw["profiles"]["folkweather"]["server_url"], "https://fw.com/edr");
    assert_eq!(raw["server_url"], "https://fw.com/edr");

    // -- cache isolation per profile
    let fa = ec::edr_cache_file_for(&loaded, Some("local"));
    let fb = ec::edr_cache_file_for(&loaded, Some("folkweather"));
    assert_ne!(fa, fb);
    assert!(fa.ends_with("local.json"));
    assert!(fb.ends_with("folkweather.json"));

    // -- legacy cache migrates for "default"
    std::fs::write(home.join("edr_collections_cache.json"), r#"{"collections": []}"#).unwrap();
    let mut def = ec::EdrConfig::new();
    def.ensure_profile(Some("default")).server_url = "https://x/edr".into();
    let migrated = ec::edr_cache_file_for(&def, None);
    assert!(migrated.exists());
    assert_eq!(std::fs::read_to_string(migrated).unwrap(), r#"{"collections": []}"#);

    // -- profile name validation
    assert!(ec::validate_profile_name("prod-2_xy").is_ok());
    for bad in ["", "has space", "slash/name", "col:on"] {
        assert!(ec::validate_profile_name(bad).is_err(), "{bad:?} should be invalid");
    }

    // cleanup
    let _ = home2;
    std::env::remove_var("OGCLI_HOME");
    let _ = std::fs::remove_dir_all(&home);
}
