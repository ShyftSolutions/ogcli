//! WMS GetCapabilities XML parser.
//!
//! Port of `src/wms/parser.py`: parses WMS 1.1.1 / 1.3.0 capabilities
//! into a layer tree, matching elements by local name so both
//! namespaced (`<wms:Layer>`) and plain (`<Layer>`) documents parse.

use std::collections::HashMap;

use anyhow::{bail, Result};

/// Spatial extent in a specific CRS.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct BoundingBox {
    pub crs: String,
    pub minx: f64,
    pub miny: f64,
    pub maxx: f64,
    pub maxy: f64,
}

/// A temporal or other dimension (TIME, ELEVATION, RUN, FORECAST, ...).
#[derive(Debug, Clone, Default)]
pub struct Dimension {
    pub name: String,
    pub units: String,
    pub default: Option<String>,
    pub values: Vec<String>,
}

/// A visual style for a layer.
#[derive(Debug, Clone, Default)]
pub struct Style {
    pub name: String,
    pub title: String,
    pub legend_url: Option<String>,
}

/// A WMS layer (container or queryable).
#[derive(Debug, Clone, Default)]
pub struct Layer {
    pub name: Option<String>,
    pub title: String,
    pub abstract_text: Option<String>,
    pub queryable: bool,

    pub crs_list: Vec<String>,
    pub bounding_boxes: HashMap<String, BoundingBox>,
    pub geographic_bbox: Option<BoundingBox>,

    pub styles: Vec<Style>,
    pub dimensions: HashMap<String, Dimension>,

    pub children: Vec<Layer>,
}

impl Layer {
    pub fn is_queryable(&self) -> bool {
        self.name.as_deref().is_some_and(|n| !n.is_empty())
    }
}

/// Complete parsed GetCapabilities response.
#[derive(Debug, Clone, Default)]
pub struct WmsCapabilities {
    pub version: String,
    pub service_title: String,
    pub service_abstract: Option<String>,
    pub service_url: String,
    pub get_map_formats: Vec<String>,
    pub root_layer: Option<Layer>,
}

/// Group queryable layers by name prefix (`MODEL_Param` -> `MODEL`);
/// names without `_` land in "Other". Optionally case-insensitive
/// substring filter on the group name. Sorted by group (Python
/// `wms groups` parity).
pub fn layer_groups(
    layers: &[&Layer],
    pattern: Option<&str>,
) -> std::collections::BTreeMap<String, Vec<String>> {
    let mut groups: std::collections::BTreeMap<String, Vec<String>> =
        std::collections::BTreeMap::new();
    for layer in layers {
        let Some(name) = layer.name.as_deref().filter(|n| !n.is_empty()) else {
            continue;
        };
        let group = if name.contains('_') {
            name.split('_').next().unwrap_or("Other")
        } else {
            "Other"
        };
        if let Some(p) = pattern {
            if !group.to_lowercase().contains(&p.to_lowercase()) {
                continue;
            }
        }
        groups
            .entry(group.to_string())
            .or_default()
            .push(name.to_string());
    }
    groups
}

impl WmsCapabilities {
    /// All queryable layers as a flat list.
    pub fn queryable_layers(&self) -> Vec<&Layer> {
        let mut out = Vec::new();
        fn collect<'a>(layer: &'a Layer, out: &mut Vec<&'a Layer>) {
            if layer.is_queryable() {
                out.push(layer);
            }
            for child in &layer.children {
                collect(child, out);
            }
        }
        if let Some(root) = &self.root_layer {
            collect(root, &mut out);
        }
        out
    }
}

fn local_name<'a, 'input>(node: roxmltree::Node<'a, 'input>) -> &'input str {
    node.tag_name().name()
}

fn child_by_local<'a, 'input>(node: roxmltree::Node<'a, 'input>, name: &str) -> Option<roxmltree::Node<'a, 'input>> {
    node.children()
        .filter(|c| c.is_element() && local_name(*c) == name)
        .next()
}

fn child_text(node: roxmltree::Node, name: &str) -> Option<String> {
    child_by_local(node, name)
        .and_then(|c| c.text())
        .map(|s| s.to_string())
}

fn children_by_local<'a, 'input>(
    node: roxmltree::Node<'a, 'input>,
    name: &str,
) -> Vec<roxmltree::Node<'a, 'input>> {
    node.children()
        .filter(|c| c.is_element() && local_name(*c) == name)
        .collect()
}

/// Parse a WMS GetCapabilities XML document.
pub fn parse_capabilities(xml: &str) -> Result<WmsCapabilities> {
    let doc = roxmltree::Document::parse(xml)
        .map_err(|e| anyhow::anyhow!("XML parse error: {e}"))?;
    let root = doc.root_element();
    let version = root.attribute("version").unwrap_or("").to_string();
    if version.is_empty() {
        bail!("could not determine WMS version from XML");
    }

    let mut caps = WmsCapabilities {
        version: version.clone(),
        ..Default::default()
    };

    // Service section
    if let Some(service) = child_by_local(root, "Service") {
        caps.service_title = child_text(service, "Title").unwrap_or_default();
        caps.service_abstract = child_text(service, "Abstract");
        if let Some(online) = child_by_local(service, "OnlineResource") {
            caps.service_url = online
                .attributes()
                .find(|a| a.name() == "href")
                .map(|a| a.value().to_string())
                .unwrap_or_default();
        }
    }

    // Capability section
    if let Some(capability) = child_by_local(root, "Capability") {
        if let Some(request) = child_by_local(capability, "Request") {
            if let Some(get_map) = child_by_local(request, "GetMap") {
                for fmt in children_by_local(get_map, "Format") {
                    if let Some(t) = fmt.text() {
                        caps.get_map_formats.push(t.to_string());
                    }
                }
            }
        }
        if let Some(layer_elem) = child_by_local(capability, "Layer") {
            caps.root_layer = Some(parse_layer(layer_elem));
        }
    }

    Ok(caps)
}

fn parse_layer(elem: roxmltree::Node) -> Layer {
    let mut layer = Layer {
        queryable: elem.attribute("queryable") == Some("1"),
        name: child_text(elem, "Name"),
        title: child_text(elem, "Title").unwrap_or_default(),
        abstract_text: child_text(elem, "Abstract"),
        ..Default::default()
    };

    // CRS (1.3.0) / SRS (1.1.1) - local name differs by version
    let mut crs_list = Vec::new();
    for tag in ["CRS", "SRS"] {
        for crs in children_by_local(elem, tag) {
            if let Some(t) = crs.text() {
                crs_list.push(t.to_string());
            }
        }
    }
    layer.crs_list = crs_list;

    // BoundingBoxes
    for bb in children_by_local(elem, "BoundingBox") {
        let crs = bb
            .attribute("CRS")
            .or_else(|| bb.attribute("SRS"))
            .unwrap_or("")
            .to_string();
        let (Some(minx), Some(miny), Some(maxx), Some(maxy)) = (
            bb.attribute("minx").and_then(|v| v.parse().ok()),
            bb.attribute("miny").and_then(|v| v.parse().ok()),
            bb.attribute("maxx").and_then(|v| v.parse().ok()),
            bb.attribute("maxy").and_then(|v| v.parse().ok()),
        ) else {
            continue;
        };
        layer.bounding_boxes.insert(
            crs.clone(),
            BoundingBox { crs, minx, miny, maxx, maxy },
        );
    }

    // Geographic bbox (1.3.0 EX_GeographicBoundingBox / 1.1.1 LatLonBoundingBox)
    if let Some(geo) = child_by_local(elem, "EX_GeographicBoundingBox") {
        let west = child_text(geo, "westBoundLongitude").and_then(|v| v.parse().ok());
        let east = child_text(geo, "eastBoundLongitude").and_then(|v| v.parse().ok());
        let south = child_text(geo, "southBoundLatitude").and_then(|v| v.parse().ok());
        let north = child_text(geo, "northBoundLatitude").and_then(|v| v.parse().ok());
        if let (Some(minx), Some(maxx), Some(miny), Some(maxy)) = (west, east, south, north) {
            layer.geographic_bbox = Some(BoundingBox {
                crs: "EPSG:4326".into(),
                minx, miny, maxx, maxy,
            });
        }
    } else if let Some(ll) = child_by_local(elem, "LatLonBoundingBox") {
        let parse = |k: &str| ll.attribute(k).and_then(|v| v.parse().ok());
        if let (Some(minx), Some(miny), Some(maxx), Some(maxy)) =
            (parse("minx"), parse("miny"), parse("maxx"), parse("maxy"))
        {
            layer.geographic_bbox = Some(BoundingBox {
                crs: "EPSG:4326".into(),
                minx, miny, maxx, maxy,
            });
        }
    }

    // Styles
    for style_elem in children_by_local(elem, "Style") {
        let Some(name) = child_text(style_elem, "Name") else {
            continue;
        };
        let title = child_text(style_elem, "Title").unwrap_or_else(|| name.clone());
        let legend_url = child_by_local(style_elem, "LegendURL")
            .and_then(|lu| child_by_local(lu, "OnlineResource"))
            .and_then(|or| {
                or.attributes()
                    .find(|a| a.name() == "href")
                    .map(|a| a.value().to_string())
            });
        layer.styles.push(Style { name, title, legend_url });
    }

    // Dimensions (inline comma-separated values)
    for dim_elem in children_by_local(elem, "Dimension") {
        let Some(name) = dim_elem.attribute("name").map(|s| s.to_string()) else {
            continue;
        };
        let values: Vec<String> = dim_elem
            .text()
            .unwrap_or("")
            .split(',')
            .map(|v| v.trim().to_string())
            .filter(|v| !v.is_empty())
            .collect();
        layer.dimensions.insert(
            name.clone(),
            Dimension {
                name,
                units: dim_elem.attribute("units").unwrap_or("").to_string(),
                default: dim_elem.attribute("default").map(|s| s.to_string()),
                values,
            },
        );
    }

    // Child layers
    for child_elem in children_by_local(elem, "Layer") {
        layer.children.push(parse_layer(child_elem));
    }

    layer
}
