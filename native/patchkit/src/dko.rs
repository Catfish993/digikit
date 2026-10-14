//! `.dko.json`: a relocatable object in canonical JSON (sorted keys, integers only), made by
//! `tools/patchkit/pk_build.py` from a toolchain's output and consumed by the linker.
//!
//! Units: ColdFire sections and symbols are bytes. SHARC+ text sections hold the instruction parcels in
//! boot-stream order (each 16-bit parcel little endian); text symbol values and relocation offsets count
//! parcels; data sections and their symbols count bytes. A SHARC relocation writes the target's natural
//! address: the short-word pc for a text symbol, the byte address for a data symbol or an extern value.

use crate::error::*;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

pub const FORMAT: u32 = 1;

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct Dko {
    pub format: u32,
    /// `cf` or `sharc`
    pub arch: String,
    pub source_sha256: String,
    #[serde(default)]
    pub toolchain: BTreeMap<String, String>,
    pub sections: Vec<DkoSection>,
    pub symbols: Vec<DkoSymbol>,
    #[serde(default)]
    pub relocs: Vec<DkoReloc>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct DkoSection {
    pub name: String,
    /// `text`, `data` or `bss`
    pub kind: String,
    pub align: u32,
    pub size: u32,
    #[serde(default)]
    pub hex: String,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct DkoSymbol {
    pub name: String,
    /// None: an undefined symbol the linker must resolve (an anchor or another object's export).
    pub section: Option<String>,
    pub value: u64,
    pub global: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
pub struct DkoReloc {
    pub section: String,
    pub offset: u64,
    #[serde(rename = "type")]
    pub rtype: String,
    pub symbol: String,
    pub addend: i64,
}

impl Dko {
    pub fn parse(text: &str) -> Result<Dko> {
        let o: Dko = serde_json::from_str(text)?;
        if o.format != FORMAT {
            return Err(err!(
                "object format {} is not supported (expected {FORMAT})",
                o.format
            ));
        }
        if o.arch != "cf" && o.arch != "sharc" {
            return Err(err!("object arch {:?}: expected cf or sharc", o.arch));
        }
        for s in &o.sections {
            if !matches!(s.kind.as_str(), "text" | "data" | "bss") {
                return Err(err!("section {}: kind {:?}", s.name, s.kind));
            }
            if s.kind != "bss" && s.hex.len() != s.size as usize * 2 {
                return Err(err!(
                    "section {}: hex length does not match size {}",
                    s.name,
                    s.size
                ));
            }
            if s.align == 0 || !s.align.is_power_of_two() {
                return Err(err!(
                    "section {}: align {} is not a power of two",
                    s.name,
                    s.align
                ));
            }
        }
        Ok(o)
    }
}
