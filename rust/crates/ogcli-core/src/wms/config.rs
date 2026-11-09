//! WMS configuration: multi-profile, wire-compatible with the Python
//! implementation's `~/.wms/config.json` (profiles + flat mirror) and
//! per-profile capabilities caches.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::time::Duration;

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};

use crate::common::config_dir;

pub fn wms_config_file() -> PathBuf {
    config_dir().join("config.json")
}

/// Legacy (default-profile) capabilities cache path.
pub fn caps_cache_file() -> PathBuf {
    config_dir().join("capabilities_cache.xml")
}

/// Per-profile capabilities cache path.
pub fn caps_cache_file_for(profile: &str) -> PathBuf {
    if profile == "default" {
        caps_cache_file()
    } else {
        caps_cache_file().with_file_name(format!("capabilities_cache_{profile}.xml"))
    }
}

/// One saved WMS server.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct WmsProfile {
    #[serde(default)]
    pub source_type: String, // "file" or "server"
    #[serde(default)]
    pub source_path: String,
    #[serde(default)]
    pub server_url: String,
    #[serde(default = "default_format")]
    pub default_format: String,
    #[serde(default = "default_ttl")]
    pub cache_ttl: u64,
}

fn default_format() -> String {
    "image/png".into()
}

fn default_ttl() -> u64 {
    600
}

/// Multi-profile WMS configuration (output_dir is global).
#[derive(Debug, Clone, Default)]
pub struct WmsConfig {
    pub active: String,
    pub profiles: HashMap<String, WmsProfile>,
    pub output_dir: String,
}

impl WmsConfig {
    pub fn new() -> Self {
        WmsConfig {
            active: "default".into(),
            profiles: HashMap::new(),
            output_dir: ".".into(),
        }
    }

    pub fn has_profile(&self, name: &str) -> bool {
        self.profiles.contains_key(name)
    }

    pub fn profile(&self) -> Option<&WmsProfile> {
        self.profiles.get(&self.active)
    }

    pub fn ensure_profile(&mut self, name: Option<&str>) -> &mut WmsProfile {
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

    pub fn source_type(&self) -> &str {
        self.profile().map(|p| p.source_type.as_str()).unwrap_or("")
    }

    pub fn source_path(&self) -> &str {
        self.profile().map(|p| p.source_path.as_str()).unwrap_or("")
    }

    pub fn server_url(&self) -> &str {
        self.profile().map(|p| p.server_url.as_str()).unwrap_or("")
    }

    pub fn cache_ttl(&self) -> Duration {
        self.profile()
            .map(|p| Duration::from_secs(if p.cache_ttl == 0 { 600 } else { p.cache_ttl }))
            .unwrap_or(Duration::from_secs(600))
    }

    pub fn is_initialized(&self) -> bool {
        !self.source_path().is_empty()
    }
}

#[derive(Debug, Default, Deserialize)]
struct RawConfig {
    #[serde(default)]
    active: Option<String>,
    #[serde(default)]
    profiles: Option<HashMap<String, WmsProfile>>,
    #[serde(default)]
    output_dir: Option<String>,
    // Legacy flat fields
    #[serde(default)]
    source_type: Option<String>,
    #[serde(default)]
    source_path: Option<String>,
    #[serde(default)]
    server_url: Option<String>,
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

pub fn load_config() -> WmsConfig {
    load_config_from(&wms_config_file())
}

fn load_config_from(path: &Path) -> WmsConfig {
    let Ok(text) = std::fs::read_to_string(path) else {
        return WmsConfig::new();
    };
    let raw: RawConfig = match serde_json::from_str(&text) {
        Ok(r) => r,
        Err(_) => return WmsConfig::new(),
    };

    if let Some(profiles) = raw.profiles.filter(|p| !p.is_empty()) {
        let active = raw
            .active
            .filter(|a| profiles.contains_key(a))
            .unwrap_or_else(|| {
                profiles.keys().min().cloned().unwrap_or_else(|| "default".into())
            });
        WmsConfig {
            active,
            profiles,
            output_dir: raw.output_dir.unwrap_or_else(|| ".".into()),
        }
    } else {
        let mut profiles = HashMap::new();
        profiles.insert(
            "default".to_string(),
            WmsProfile {
                source_type: raw.source_type.unwrap_or_default(),
                source_path: raw.source_path.unwrap_or_default(),
                server_url: raw.server_url.unwrap_or_default(),
                ..Default::default()
            },
        );
        WmsConfig {
            active: "default".into(),
            profiles,
            output_dir: raw.output_dir.unwrap_or_else(|| ".".into()),
        }
    }
}

/// Save to disk: profiles map plus a flat mirror of the active profile.
pub fn save_config(config: &WmsConfig) -> Result<()> {
    save_config_to(&wms_config_file(), config)
}

fn save_config_to(path: &Path, config: &WmsConfig) -> Result<()> {
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
    obj.insert(
        "output_dir".into(),
        serde_json::Value::String(config.output_dir.clone()),
    );
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

/// Clear ALL capabilities caches (all profiles).
pub fn clear_all_caches() -> bool {
    let mut removed = false;
    if let Ok(entries) = std::fs::read_dir(config_dir()) {
        for entry in entries.flatten() {
            let name = entry.file_name();
            let name = name.to_string_lossy();
            if name.starts_with("capabilities_cache") && name.ends_with(".xml") {
                let _ = std::fs::remove_file(entry.path());
                removed = true;
            }
        }
    }
    removed
}

fn client() -> reqwest::blocking::Client {
    reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(30))
        .user_agent("wms-cli/0.1-rust")
        .build()
        .expect("reqwest client")
}

/// Fetch raw GetCapabilities XML from a WMS server.
pub fn fetch_capabilities_blocking(server_url: &str) -> Result<String> {
    let resp = client()
        .get(server_url)
        .query(&[
            ("SERVICE", "WMS"),
            ("VERSION", "1.3.0"),
            ("REQUEST", "GetCapabilities"),
        ])
        .send()
        .with_context(|| format!("fetching capabilities from {server_url}"))?;
    let status = resp.status();
    if !status.is_success() {
        bail!("HTTP {status} fetching capabilities from {server_url}");
    }
    Ok(resp.text()?)
}

fn cache_is_valid(path: &Path, ttl: Duration) -> bool {
    std::fs::metadata(path)
        .ok()
        .and_then(|m| m.modified().ok())
        .and_then(|t| t.elapsed().ok())
        .is_some_and(|age| age < ttl)
}

fn touch(path: &Path) {
    let _ = filetime::set_file_mtime(path, filetime::FileTime::now());
}

/// Get capabilities XML for the active profile (sliding-TTL cache;
/// local files read directly).
pub fn get_capabilities_xml(config: &WmsConfig) -> Result<String> {
    if !config.is_initialized() {
        bail!("WMS not initialized. Run 'wms init <source>' first.");
    }
    if config.source_type() == "file" {
        let path = Path::new(config.source_path());
        if !path.exists() {
            bail!("capabilities file not found: {}", path.display());
        }
        return Ok(std::fs::read_to_string(path)?);
    }
    if config.source_type() == "server" {
        let cache = caps_cache_file_for(&config.active);
        if cache_is_valid(&cache, config.cache_ttl()) {
            if let Ok(text) = std::fs::read_to_string(&cache) {
                touch(&cache);
                return Ok(text);
            }
        }
        let xml = fetch_capabilities_blocking(config.source_path())?;
        if let Some(parent) = cache.parent() {
            std::fs::create_dir_all(parent)?;
        }
        std::fs::write(&cache, &xml)?;
        return Ok(xml);
    }
    bail!("unknown source type: {}", config.source_type())
}

/// Parse the active profile's capabilities.
pub fn get_capabilities(config: &WmsConfig) -> Result<crate::wms::parser::WmsCapabilities> {
    let xml = get_capabilities_xml(config)?;
    crate::wms::parser::parse_capabilities(&xml)
}

/// Initialize a profile from a server URL or local capabilities file.
pub fn init(
    source: &str,
    server_override: Option<&str>,
    config: &mut WmsConfig,
    save_as: Option<&str>,
) -> Result<()> {
    if let Some(name) = save_as {
        crate::edr::config::validate_profile_name(name)?;
    }

    let (source_type, xml) = if source.starts_with("http://") || source.starts_with("https://") {
        ("server", fetch_capabilities_blocking(source)?)
    } else {
        let path = PathBuf::from(source);
        if !path.exists() {
            bail!("file not found: {}", path.display());
        }
        ("file", std::fs::read_to_string(&path)?)
    };

    let caps = crate::wms::parser::parse_capabilities(&xml)?;
    if caps.root_layer.is_none() {
        bail!("no layers found in capabilities - is this a WMS server?");
    }

    let server_url = server_override
        .map(|s| s.to_string())
        .unwrap_or_else(|| {
            if source_type == "server" {
                source.to_string()
            } else {
                caps.service_url.clone()
            }
        });

    let name = save_as
        .map(|s| s.to_string())
        .unwrap_or_else(|| {
            if config.profiles.is_empty() {
                "default".to_string()
            } else {
                config.active.clone()
            }
        });
    crate::edr::config::validate_profile_name(&name)?;
    {
        let p = config.ensure_profile(Some(&name));
        p.source_type = source_type.to_string();
        p.source_path = source.to_string();
        p.server_url = server_url;
    }
    config.active = name.clone();

    if source_type == "server" {
        let cache = caps_cache_file_for(&name);
        if let Some(parent) = cache.parent() {
            std::fs::create_dir_all(parent)?;
        }
        std::fs::write(cache, &xml)?;
    }

    save_config(config)?;
    Ok(())
}

/// Re-fetch capabilities for the active profile (`wms init` with no source).
pub fn refresh_active_profile(config: &mut WmsConfig) -> Result<()> {
    if !config.is_initialized() {
        bail!(
            "profile '{}' has no source. Run 'wms init <source>' first.",
            config.active
        );
    }
    if config.source_type() == "file" {
        return Ok(());
    }
    let xml = fetch_capabilities_blocking(config.source_path())?;
    let cache = caps_cache_file_for(&config.active);
    if let Some(parent) = cache.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(cache, xml)?;
    Ok(())
}
