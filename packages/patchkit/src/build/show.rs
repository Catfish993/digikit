//! `patchkit dko show FILE`: a readable summary of an object.

use crate::dko::Dko;
use std::collections::BTreeMap;
use std::fmt::Write;

/// The summary: arch, toolchain record, sections, symbols and relocations (counted by type; listed with `relocs`).
pub fn summary(o: &Dko, relocs: bool) -> String {
    let mut s = String::new();
    let _ = writeln!(s, "arch        {}  (format {})", o.arch, o.format);
    let _ = writeln!(s, "source      {}", o.source_sha256);
    let _ = writeln!(s, "toolchain");
    let w = o.toolchain.keys().map(|k| k.len()).max().unwrap_or(0);
    for (k, v) in &o.toolchain {
        let _ = writeln!(s, "  {k:<w$}  {v}");
    }
    let _ = writeln!(s, "sections    {}", o.sections.len());
    for x in &o.sections {
        let _ = writeln!(
            s,
            "  {:<40} {:<5} align {:<3} size {}",
            x.name, x.kind, x.align, x.size
        );
    }
    let glob = o.symbols.iter().filter(|x| x.global).count();
    let undef = o.symbols.iter().filter(|x| x.section.is_none()).count();
    let _ = writeln!(
        s,
        "symbols     {} ({glob} global, {} local, {undef} undefined)",
        o.symbols.len(),
        o.symbols.len() - glob
    );
    for x in &o.symbols {
        let scope = if x.global { "global" } else { "local " };
        match &x.section {
            Some(sec) => {
                let _ = writeln!(s, "  {scope} {:<48} {sec}+{:#x}", x.name, x.value);
            }
            None => {
                let _ = writeln!(s, "  {scope} {:<48} (undefined)", x.name);
            }
        }
    }
    let mut by_type: BTreeMap<&str, usize> = BTreeMap::new();
    for r in &o.relocs {
        *by_type.entry(r.rtype.as_str()).or_default() += 1;
    }
    let counts: Vec<String> = by_type.iter().map(|(t, n)| format!("{t} x{n}")).collect();
    let _ = writeln!(s, "relocs      {} ({})", o.relocs.len(), counts.join(", "));
    if relocs {
        for r in &o.relocs {
            let _ = writeln!(
                s,
                "  {}+{:#x}  {}  {}{:+}",
                r.section, r.offset, r.rtype, r.symbol, r.addend
            );
        }
    }
    s
}
