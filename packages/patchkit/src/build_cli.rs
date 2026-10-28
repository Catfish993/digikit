//! The command line of `patchkit build`, `patchkit dko show` and `patchkit cf-link` (the library is `patchkit::build`).

use patchkit::build::{self, BuildOptions, cf, show};
use patchkit::dko::Dko;
use patchkit::error::{Error, Result};
use std::collections::HashMap;
use std::path::{Path, PathBuf};

fn flag(rest: &[String], name: &str) -> Option<String> {
    rest.iter()
        .position(|a| a == name)
        .and_then(|i| rest.get(i + 1).cloned())
}

fn flags(rest: &[String], name: &str) -> Vec<String> {
    rest.iter()
        .enumerate()
        .filter(|(_, a)| *a == name)
        .filter_map(|(i, _)| rest.get(i + 1).cloned())
        .collect()
}

/// The arguments that are not flags or the values of flags that take one.
fn positional(rest: &[String], valued: &[&str]) -> Vec<String> {
    let mut v = vec![];
    let mut i = 0;
    while i < rest.len() {
        if valued.contains(&rest[i].as_str()) {
            i += 2;
        } else if rest[i].starts_with('-') {
            i += 1;
        } else {
            v.push(rest[i].clone());
            i += 1;
        }
    }
    v
}

pub fn build(root: &Path, rest: &[String]) -> Result<()> {
    const KNOWN: [&str; 8] = [
        "--check", "--force", "--define", "--out", "--status", "--tree", "--all", "--",
    ];
    if let Some(f) = rest
        .iter()
        .find(|a| a.starts_with('-') && !KNOWN.contains(&a.as_str()))
    {
        return Err(Error(format!("build: unknown option {f}")));
    }
    let valued = ["--define", "--out", "--tree"];
    let args = positional(rest, &valued);
    let has = |f: &str| rest.iter().any(|a| a == f);
    let tree = flag(rest, "--tree").map(PathBuf::from);
    // (the name the caller used, the module directory)
    let dirs: Vec<(String, PathBuf)> = if args.is_empty() {
        build::all_modules(root, tree.as_deref())
            .into_iter()
            .map(|d| (d.display().to_string(), d))
            .collect()
    } else {
        args.iter()
            .map(|a| Ok((a.clone(), build::resolve_module(root, a)?)))
            .collect::<Result<_>>()?
    };
    if has("--status") {
        let mut m = serde_json::Map::new();
        for (name, d) in &dirs {
            let ok = build::module_status(root, d)
                .map(|v| v.iter().all(|x| x.1))
                .unwrap_or(false);
            m.insert(
                name.clone(),
                if ok { "unchanged" } else { "needs build" }.into(),
            );
        }
        println!("{}", serde_json::Value::Object(m));
        return Ok(());
    }
    let opts = BuildOptions {
        root: root.to_path_buf(),
        check: has("--check"),
        force: has("--force"),
        defines: flags(rest, "--define")
            .into_iter()
            .map(|d| d.split('=').next().unwrap_or("").to_string())
            .collect(),
        out: flag(rest, "--out").map(PathBuf::from),
    };
    for (_, d) in dirs {
        for line in build::build_module(&d, &opts)?.lines() {
            println!("{line}");
        }
    }
    Ok(())
}

pub fn dko(rest: &[String]) -> Result<()> {
    const U: &str =
        "usage: patchkit dko show FILE.dko.json [--relocs] | patchkit dko isa-digest FILE.dko.json";
    let cmd = rest.first().map(String::as_str);
    if !matches!(cmd, Some("show" | "isa-digest")) {
        return Err(Error(U.into()));
    }
    let file = positional(&rest[1..], &[])
        .into_iter()
        .next()
        .ok_or_else(|| Error(U.into()))?;
    let text = std::fs::read_to_string(&file).map_err(|e| Error(format!("{file}: {e}")))?;
    let o = Dko::parse(&text)?;
    if cmd == Some("isa-digest") {
        println!("{}", o.isa_digest()?);
    } else {
        print!(
            "{}",
            show::summary(&o, rest.iter().any(|a| a == "--relocs"))
        );
    }
    Ok(())
}

fn hex_u32(s: &str) -> Result<u32> {
    u32::from_str_radix(s.trim_start_matches("0x"), 16)
        .map_err(|_| Error(format!("bad hex number {s:?}")))
}

pub fn cf_link(root: &Path, rest: &[String]) -> Result<()> {
    let sources: Vec<PathBuf> = positional(rest, &["--base", "--sym", "-I", "--bin", "--json"])
        .into_iter()
        .map(PathBuf::from)
        .collect();
    if sources.is_empty() {
        return Err(Error("cf-link needs sources".into()));
    }
    let base =
        hex_u32(&flag(rest, "--base").ok_or_else(|| Error("cf-link needs --base HEX".into()))?)?;
    let mut syms: HashMap<String, u32> = HashMap::new();
    for s in flags(rest, "--sym") {
        let (n, v) = s
            .split_once('=')
            .ok_or_else(|| Error(format!("--sym NAME=HEX, got {s:?}")))?;
        syms.insert(n.to_string(), hex_u32(v)?);
    }
    let inc: Vec<PathBuf> = flags(rest, "-I").into_iter().map(PathBuf::from).collect();
    let (l, hist) = build::link_sources(root, &sources, base, &syms, &inc)?;
    if let Some(p) = flag(rest, "--bin") {
        std::fs::write(&p, &l.image).map_err(|e| Error(format!("{p}: {e}")))?;
    }
    let j = serde_json::json!({
        "base": l.base,
        "bss_size": l.bss_size,
        "symbols": l.symbols.iter().collect::<std::collections::BTreeMap<_, _>>(),
        "text_ranges": l.text_ranges,
        "relocs_applied": l.relocs_applied,
        "histogram": hist,
        "instructions": hist.values().sum::<u32>(),
    });
    match flag(rest, "--json") {
        Some(p) => std::fs::write(&p, j.to_string()).map_err(|e| Error(format!("{p}: {e}")))?,
        None => println!("{j}"),
    }
    let _ = cf::BASE;
    Ok(())
}
