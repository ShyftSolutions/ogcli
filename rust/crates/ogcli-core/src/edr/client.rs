//! EDR HTTP client - blocking, with the cross-server quirk handling
//! ported from `src/edr/client.py`:
//! - distance retry: unit-suffixed within/corridor-width ('100km')
//!   retried as bare meters on 400/422 (Met Office Labs)
//! - corridor width split into corridor-width + width-units
//! - error detail extracted from detail/description/message/title keys

use std::time::Duration;

use anyhow::Result;
use reqwest::blocking::Client;

use crate::edr::config::check_response;

/// Unit -> meters conversion for distance parameters.
const DISTANCE_UNITS: &[(&str, f64)] = &[
    ("km", 1000.0), ("kilometers", 1000.0), ("kilometres", 1000.0),
    ("m", 1.0), ("meters", 1.0), ("metres", 1.0),
    ("mi", 1609.344), ("miles", 1609.344),
    ("nmi", 1852.0), ("ft", 0.3048), ("feet", 0.3048),
];

/// Convert '100km' -> 100000.0. None if the value has no unit suffix
/// (already numeric) or can't be parsed.
pub fn distance_to_meters(value: &str) -> Option<f64> {
    let value = value.trim();
    let split = value
        .char_indices()
        .find(|(i, c)| *i > 0 && c.is_ascii_alphabetic())
        .map(|(i, _)| i);
    let split = split?;
    let (num, unit) = value.split_at(split);
    let num: f64 = num.trim().parse().ok()?;
    let mult = DISTANCE_UNITS
        .iter()
        .find(|(u, _)| *u == unit.trim().to_lowercase().as_str())
        .map(|(_, m)| *m)?;
    Some(num * mult)
}

/// Build corridor width parameters from a user expression.
/// Splits '10km' into corridor-width=10 + width-units=km (the form most
/// servers require); passes anything else through as corridor-width.
pub fn corridor_params(width: &str) -> Vec<(String, String)> {
    let w = width.trim();
    if let Some(meters) = distance_to_meters(w) {
        // Numeric + unit: split into value + unit
        let split = w
            .char_indices()
            .find(|(i, c)| *i > 0 && c.is_ascii_alphabetic())
            .map(|(i, _)| i)
            .unwrap_or(w.len());
        let (num, unit) = w.split_at(split);
        let _ = meters;
        vec![
            ("corridor-width".into(), num.trim().to_string()),
            ("width-units".into(), unit.trim().to_lowercase()),
        ]
    } else {
        vec![("corridor-width".into(), w.to_string())]
    }
}

pub struct EdrClient {
    pub base_url: String,
    pub api_key: String,
    client: Client,
}

impl EdrClient {
    pub fn new(base_url: &str, api_key: &str) -> Self {
        let client = Client::builder()
            .timeout(Duration::from_secs(30))
            .user_agent("edr-cli/0.1-rust")
            .build()
            .expect("reqwest client");
        EdrClient {
            base_url: base_url.trim_end_matches('/').to_string(),
            api_key: api_key.to_string(),
            client,
        }
    }

    /// Build a /collections/{id}[/{instance}].../{query_type} URL.
    pub fn collections_url(
        &self,
        collection_id: &str,
        query_type: Option<&str>,
        instance: Option<&str>,
    ) -> String {
        let mut url = format!("{}/collections/{collection_id}", self.base_url);
        if let Some(instance) = instance {
            url.push_str(&format!("/instances/{instance}"));
        }
        if let Some(qt) = query_type {
            url.push_str(&format!("/{qt}"));
        }
        url
    }

    fn get(&self, url: &str, params: &[(String, String)]) -> Result<reqwest::blocking::Response> {
        let mut req = self.client.get(url).header("Accept",
            "application/json, application/vnd.cov+json, application/geo+json");
        let filtered: Vec<(&str, &str)> = params
            .iter()
            .filter(|(k, v)| !k.is_empty() && !v.is_empty())
            .map(|(k, v)| (k.as_str(), v.as_str()))
            .collect();
        if !self.api_key.is_empty() {
            let mut all = vec![("apikey", self.api_key.as_str())];
            all.extend(filtered.iter().copied());
            req = req.query(&all);
        } else if !filtered.is_empty() {
            req = req.query(&filtered);
        }
        let resp = req.send()?;
        check_response(resp)
    }

    /// Run an EDR query. Returns (body bytes, content type).
    /// Retries once with bare-meter distances when the server rejects
    /// unit-suffixed values with 400/422.
    pub fn query(
        &self,
        collection_id: &str,
        query_type: &str,
        instance: Option<&str>,
        params: &[(String, String)],
    ) -> Result<(Vec<u8>, String)> {
        let url = self.collections_url(collection_id, Some(query_type), instance);
        let resp = self.get_soft(&url, params);
        let mut resp = resp;
        if resp.status().as_u16() == 400 || resp.status().as_u16() == 422 {
            if let Some(converted) = convert_distances(params) {
                resp = self.get_soft(&url, &converted);
            }
        }
        let resp = check_response(resp)?;
        let content_type = resp
            .headers()
            .get(reqwest::header::CONTENT_TYPE)
            .and_then(|v| v.to_str().ok())
            .unwrap_or("")
            .to_string();
        Ok((resp.bytes()?.to_vec(), content_type))
    }

    /// GET that returns the raw response without raising on HTTP errors
    /// (for the retry path).
    fn get_soft(
        &self,
        url: &str,
        params: &[(String, String)],
    ) -> reqwest::blocking::Response {
        let mut req = self.client.get(url).header("Accept",
            "application/json, application/vnd.cov+json, application/geo+json");
        let filtered: Vec<(&str, &str)> = params
            .iter()
            .filter(|(k, v)| !k.is_empty() && !v.is_empty())
            .map(|(k, v)| (k.as_str(), v.as_str()))
            .collect();
        if !self.api_key.is_empty() {
            let mut all = vec![("apikey", self.api_key.as_str())];
            all.extend(filtered.iter().copied());
            req = req.query(&all);
        } else if !filtered.is_empty() {
            req = req.query(&filtered);
        }
        req.send().expect("request sent")
    }

    /// Execute one request without raising: returns
    /// (status, body, content_type). Status 0 = transport error
    /// (body carries the error message).
    pub fn query_raw(
        &self,
        url: &str,
        params: &[(String, String)],
    ) -> (u16, Vec<u8>, String) {
        let mut resp = self.get_soft(url, params);
        if resp.status().as_u16() == 400 || resp.status().as_u16() == 422 {
            if let Some(converted) = convert_distances(params) {
                resp = self.get_soft(url, &converted);
            }
        }
        let status = resp.status().as_u16();
        let content_type = resp
            .headers()
            .get(reqwest::header::CONTENT_TYPE)
            .and_then(|v| v.to_str().ok())
            .unwrap_or("")
            .to_string();
        match resp.bytes() {
            Ok(b) => (status, b.to_vec(), content_type),
            Err(e) => (0, e.to_string().into_bytes(), String::new()),
        }
    }

    /// List model-run instances for a collection.
    pub fn get_instances(&self, collection_id: &str) -> Result<serde_json::Value> {
        let url = self.collections_url(collection_id, Some("instances"), None);
        Ok(self.get(&url, &[])?.json()?)
    }

    /// List named locations for a collection.
    pub fn get_locations(&self, collection_id: &str) -> Result<serde_json::Value> {
        let url = self.collections_url(collection_id, Some("locations"), None);
        Ok(self.get(&url, &[])?.json()?)
    }

    /// Query data at a named location. Returns (body, content type).
    pub fn get_location(
        &self,
        collection_id: &str,
        location_id: &str,
        params: &[(String, String)],
    ) -> Result<(Vec<u8>, String)> {
        let url = format!(
            "{}/collections/{collection_id}/locations/{location_id}",
            self.base_url
        );
        let resp = self.get(&url, params)?;
        let content_type = resp
            .headers()
            .get(reqwest::header::CONTENT_TYPE)
            .and_then(|v| v.to_str().ok())
            .unwrap_or("")
            .to_string();
        Ok((resp.bytes()?.to_vec(), content_type))
    }
}

/// Convert unit-suffixed distance params ('100km') to bare meters.
/// Returns a new params list, or None if nothing to convert.
fn convert_distances(params: &[(String, String)]) -> Option<Vec<(String, String)>> {
    let mut converted = params.to_vec();
    let mut changed = false;
    for (key, value) in converted.iter_mut() {
        if key == "within" || key == "corridor-width" {
            if let Some(meters) = distance_to_meters(value) {
                *value = format!("{meters:.0}");
                changed = true;
            }
        }
    }
    if changed { Some(converted) } else { None }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn distance_conversions() {
        assert_eq!(distance_to_meters("100km"), Some(100000.0));
        assert_eq!(distance_to_meters("10 km"), Some(10000.0));
        assert_eq!(distance_to_meters("25m"), Some(25.0));
        assert!((distance_to_meters("10mi").unwrap() - 16093.44).abs() < 0.01);
        assert_eq!(distance_to_meters("100000"), None);
        assert_eq!(distance_to_meters("100foos"), None);
    }

    #[test]
    fn corridor_param_split() {
        assert_eq!(
            corridor_params("10km"),
            vec![
                ("corridor-width".to_string(), "10".to_string()),
                ("width-units".to_string(), "km".to_string()),
            ]
        );
        assert_eq!(
            corridor_params("5000"),
            vec![("corridor-width".to_string(), "5000".to_string())]
        );
    }

    #[test]
    fn collections_url_building() {
        let c = EdrClient::new("https://example.com/edr/", "");
        assert_eq!(
            c.collections_url("hrrr-surface", Some("position"), None),
            "https://example.com/edr/collections/hrrr-surface/position"
        );
        assert_eq!(
            c.collections_url("edr/daily", Some("locations"), None),
            "https://example.com/edr/collections/edr/daily/locations"
        );
        assert_eq!(
            c.collections_url("c", Some("position"), Some("2026-11-04T12:00:00Z")),
            "https://example.com/edr/collections/c/instances/2026-11-04T12:00:00Z/position"
        );
    }
}
