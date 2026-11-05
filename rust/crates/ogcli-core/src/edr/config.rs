//! EDR configuration: multi-profile, wire-compatible with the Python
//! implementation's `~/.wms/edr_config.json` (profiles map plus a flat
//! mirror of the active profile) and per-profile collection caches.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime};

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};

use crate::common::config_dir;

pub const DEFAULT_CACHE_TTL: Duration = Duration::from_secs(600);

pub fn edr_config_file() -> PathBuf {
    config_dir().join("edr_config.json")
}

pub fn edr_cache_dir() -> PathBuf {
    config_dir().join("edr_cache")
}

/// Pre-profiles cache location (migrated for the "default" profile).
pub fn legacy_cache_file() -> PathBuf {
    config_dir().join("edr_collections_cache.json")
}

/// One saved EDR server.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct EdrProfile {
    #[serde(default)]
    pub server_url: String,
    #[serde(default)]
    pub api_key: String,
    #[serde(default = "default_cache_ttl")]
    pub cache_ttl: u64,
}

fn default_cache_ttl() -> u64 {
    600
}

/// Multi-profile EDR configuration.
#[derive(Debug, Clone, Default)]
pub struct EdrConfig {
    pub active: String,
    pub profiles: HashMap<String, EdrProfile>,
}

impl EdrConfig {
    pub fn new() -> Self {
        Self {
            active: "default".into(),
            profiles: HashMap::new(),
        }
    }

    pub fn has_profile(&self, name: &str) -> bool {
        self.profiles.contains_key(name)
    }

    pub fn profile(&self) -> Option<&EdrProfile> {
        self.profiles.get(&self.active)
    }

    pub fn ensure_profile(&mut self, name: Option<&str>) -> &mut EdrProfile {
        let name = name.unwrap_or(&self.active);
        self.profiles.entry(name.to_string()).or_default()
    }

    pub fn set_active(&mut self, name: &str) -> Result<()> {
        if !self.has_profile(name) {
            bail!("no profile named '{name}'");
        }
        self.active = name.to_string();
        Ok(())
    }

    pub fn profile_names(&self) -> Vec<String> {
        self.profiles.keys().cloned().collect()
    }

    pub fn server_url(&self) -> &str {
        self.profile().map(|p| p.server_url.as_str()).unwrap_or("")
    }

    pub fn api_key(&self) -> &str {
        self.profile().map(|p| p.api_key.as_str()).unwrap_or("")
    }

    pub fn cache_ttl(&self) -> Duration {
        self.profile()
            .map(|p| Duration::from_secs(if p.cache_ttl == 0 { 600 } else { p.cache_ttl }))
            .unwrap_or(DEFAULT_CACHE_TTL)
    }

    pub fn is_initialized(&self) -> bool {
        self.profile().is_some_and(|p| !p.server_url.is_empty())
    }
}

/// Raw on-disk shape: profiles form OR legacy flat form (a file can
/// contain both - the Python implementation writes a flat mirror).
#[derive(Debug, Default, Deserialize)]
struct RawConfig {
    #[serde(default)]
    active: Option<String>,
    #[serde(default)]
    profiles: Option<HashMap<String, EdrProfile>>,
    // Legacy flat fields
    #[serde(default)]
    server_url: Option<String>,
    #[serde(default)]
    api_key: Option<String>,
    #[serde(default)]
    cache_ttl: Option<u64>,
}

pub fn validate_profile_name(name: &str) -> Result<()> {
    let valid = !name.is_empty()
        && name.chars().next().is_some_and(|c| c.is_ascii_alphanumeric())
        && name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_');
    if !valid {
        bail!("invalid profile name {name:?}: use letters, numbers, '-' or '_'");
    }
    Ok(())
}

pub fn load_config() -> EdrConfig {
    load_config_from(&edr_config_file())
}

fn load_config_from(path: &Path) -> EdrConfig {
    let Ok(text) = std::fs::read_to_string(path) else {
        return EdrConfig::new();
    };
    let raw: RawConfig = match serde_json::from_str(&text) {
        Ok(r) => r,
        Err(_) => return EdrConfig::new(),
    };

    if let Some(profiles) = raw.profiles.filter(|p| !p.is_empty()) {
        let active = raw
            .active
            .filter(|a| profiles.contains_key(a))
            .unwrap_or_else(|| profiles.keys().min().cloned().unwrap_or_else(|| "default".into()));
        EdrConfig { active, profiles }
    } else {
        // Legacy flat config -> "default" profile
        let mut profiles = HashMap::new();
        profiles.insert(
            "default".to_string(),
            EdrProfile {
                server_url: raw.server_url.unwrap_or_default(),
                api_key: raw.api_key.unwrap_or_default(),
                cache_ttl: raw.cache_ttl.unwrap_or(600),
            },
        );
        EdrConfig {
            active: "default".into(),
            profiles,
        }
    }
}

/// Save to disk: profiles map plus a flat mirror of the active profile
/// (keeps the pre-profiles Python reader working).
pub fn save_config(config: &EdrConfig) -> Result<()> {
    save_config_to(&edr_config_file(), config)
}

fn save_config_to(path: &Path, config: &EdrConfig) -> Result<()> {
    let mut obj = serde_json::Map::new();
    obj.insert(
        "active".into(),
        serde_json::Value::String(config.active.clone()),
    );
    let profiles: serde_json::Map<String, _> = config
        .profiles
        .iter()
        .map(|(name, p)| (name.clone(), serde_json::to_value(p).unwrap()))
        .collect();
    obj.insert("profiles".into(), serde_json::Value::Object(profiles));
    if let Some(p) = config.profile() {
        if let Some(flat) = serde_json::to_value(p).ok().and_then(|v| v.as_object().cloned()) {
            for (k, v) in flat {
                obj.insert(k, v);
            }
        }
    }
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(path, serde_json::to_string_pretty(&serde_json::Value::Object(obj))?)?;
    Ok(())
}

/// Per-profile collections cache path, migrating the legacy cache file
/// for the "default" profile on first use.
pub fn edr_cache_file_for(config: &EdrConfig, profile: Option<&str>) -> PathBuf {
    let name = profile.unwrap_or(&config.active);
    let path = edr_cache_dir().join(format!("{name}.json"));
    if name == "default" && !path.exists() && legacy_cache_file().exists() {
        let _ = std::fs::create_dir_all(edr_cache_dir());
        let _ = std::fs::copy(legacy_cache_file(), &path);
    }
    path
}

/// Clear ALL EDR collections caches. Returns true if any existed.
pub fn clear_all_caches() -> bool {
    let mut removed = false;
    if edr_cache_dir().exists() {
        let _ = std::fs::remove_dir_all(edr_cache_dir());
        removed = true;
    }
    if legacy_cache_file().exists() {
        let _ = std::fs::remove_file(legacy_cache_file());
        removed = true;
    }
    removed
}

fn cache_is_valid(path: &Path, ttl: Duration) -> bool {
    let Ok(meta) = std::fs::metadata(path) else {
        return false;
    };
    let Ok(modified) = meta.modified() else {
        return false;
    };
    modified
        .elapsed()
        .map(|age| age < ttl)
        .unwrap_or(false)
}

fn touch(path: &Path) {
    // Update mtime to now (sliding-window TTL)
    let now = filetime::FileTime::now();
    let _ = filetime::set_file_mtime(path, now);
}

/// Normalize a user-supplied EDR base URL.
pub fn normalize_base_url(url: &str) -> String {
    let mut url = url.trim().trim_end_matches('/').to_string();
    if url.ends_with("/collections") {
        url.truncate(url.len() - "/collections".len());
    }
    url
}

/// Get collections JSON for the active profile (sliding-TTL cache).
pub fn get_collections_json(config: &EdrConfig) -> Result<String> {
    if !config.is_initialized() {
        bail!("EDR not initialized. Run 'edr init <url>' first.");
    }
    let cache_path = edr_cache_file_for(config, None);
    if cache_is_valid(&cache_path, config.cache_ttl()) {
        if let Ok(text) = std::fs::read_to_string(&cache_path) {
            touch(&cache_path);
            return Ok(text);
        }
    }
    let text = fetch_collections_blocking(config.server_url(), config.api_key())?;
    if let Some(parent) = cache_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(&cache_path, &text)?;
    Ok(text)
}

/// Parse the active profile's collections into capabilities.
pub fn get_capabilities(config: &EdrConfig) -> Result<crate::edr::parser::EdrCapabilities> {
    let json = get_collections_json(config)?;
    crate::edr::parser::parse_collections(&json, config.server_url())
}

fn blocking_client() -> reqwest::blocking::Client {
    reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(30))
        .user_agent("edr-cli/0.1-rust")
        .build()
        .expect("reqwest client")
}

/// Blocking /collections fetch (used by sync CLI paths).
pub fn fetch_collections_blocking(base_url: &str, api_key: &str) -> Result<String> {
    let url = format!("{}/collections", base_url.trim_end_matches('/'));
    let mut req = blocking_client().get(&url).header("Accept", "application/json");
    if !api_key.is_empty() {
        req = req.query(&[("apikey", api_key)]);
    }
    let resp = req.send().with_context(|| format!("fetching {url}"))?;
    let resp = check_response(resp)?;
    Ok(resp.text()?)
}

/// Raise with the server's error detail (detail/description/message/
/// title keys) when present.
pub fn check_response(resp: reqwest::blocking::Response) -> Result<reqwest::blocking::Response> {
    let status = resp.status();
    if !status.is_success() {
        let detail = resp
            .json::<serde_json::Value>()
            .ok()
            .and_then(|err| {
                ["detail", "description", "message", "title"]
                    .iter()
                    .find_map(|k| err.get(k).and_then(|v| v.as_str()))
                    .map(String::from)
            })
            .unwrap_or_default();
        if !detail.is_empty() {
            bail!("HTTP {status}: {detail}");
        }
        bail!("HTTP {status} error");
    }
    Ok(resp)
}

/// Re-fetch collections for the active profile (`edr init` with no URL).
pub fn refresh_active_profile(config: &mut EdrConfig) -> Result<()> {
    if !config.is_initialized() {
        bail!(
            "profile '{}' has no server URL. Run 'edr init <url>' first.",
            config.active
        );
    }
    let text = fetch_collections_blocking(config.server_url(), config.api_key())?;
    let cache_path = edr_cache_file_for(config, None);
    if let Some(parent) = cache_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(cache_path, text)?;
    Ok(())
}

/// Initialize a profile from a server: fetch landing (best-effort) and
/// /collections, validate, cache, save. Mirrors the Python behavior of
/// `--save` making the profile active.
pub fn init_from_server(
    server_url: &str,
    api_key: &str,
    config: &mut EdrConfig,
    save_as: Option<&str>,
) -> Result<()> {
    if let Some(name) = save_as {
        validate_profile_name(name)?;
    }
    let api_key = if api_key.is_empty() {
        std::env::var("EDR_API_KEY").unwrap_or_default()
    } else {
        api_key.to_string()
    };

    let base_url = normalize_base_url(server_url);

    // Landing page is best-effort
    let _ = blocking_client()
        .get(&base_url)
        .header("Accept", "application/json")
        .send();

    let collections_json = fetch_collections_blocking(&base_url, &api_key)?;
    let caps = crate::edr::parser::parse_collections(&collections_json, &base_url)?;
    if caps.collections.is_empty() {
        bail!("no collections found at {base_url}/collections - is this an EDR server?");
    }

    let name = save_as
        .map(|s| s.to_string())
        .unwrap_or_else(|| {
            if config.profiles.is_empty() {
                "default".to_string()
            } else {
                config.active.clone()
            }
        });
    validate_profile_name(&name)?;
    {
        let profile = config.ensure_profile(Some(&name));
        profile.server_url = base_url.clone();
        profile.api_key = api_key;
    }
    config.active = name.clone();

    let cache_path = edr_cache_file_for(config, Some(&name));
    if let Some(parent) = cache_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(&cache_path, &collections_json)?;

    save_config(config)?;
    Ok(())
}

/// Time a cache file was last modified (for `cache status`).
pub fn cache_mtime(path: &Path) -> Option<SystemTime> {
    std::fs::metadata(path).ok()?.modified().ok()
}
