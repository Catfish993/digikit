//! SHARC+ objects. The source text (`#include` expansion and `#ifdef` selection) and its hash are made here, so that a freshness check
//! needs no Python; the assembler, the fix-ups and the instruction re-decode stay in `tools/patchkit/pk_sharc.py` (a child process,
//! `pk_sharc object`) until the own assembler of phase 6a replaces them.

use super::toolchain::{self as tc, Env, TempDir};
use crate::dko::Dko;
use crate::error::*;
use std::path::{Path, PathBuf};

/// The include directories of a SHARC+ object of the module: its generated and own sources, then the SDK's. The directories of the
/// sources come after these.
pub fn include_dirs(root: &Path, moddir: &Path) -> Vec<PathBuf> {
    let mut v = vec![moddir.join("build/gen"), moddir.join("src")];
    v.extend(tc::global_include_dirs(false).iter().map(|d| root.join(d)));
    v
}

/// `Path.read_text()`: UTF-8, with `\r\n` and `\r` read as `\n`.
fn read_text(p: &Path) -> Result<String> {
    let t = tc::read_text(p)?;
    Ok(if t.contains('\r') {
        t.replace("\r\n", "\n").replace('\r', "\n")
    } else {
        t
    })
}

/// `#include "name"` alone on a line (trailing blanks allowed): the name.
fn include_name(line: &str) -> Option<&str> {
    let rest = line.strip_prefix("#include")?;
    let t = rest.trim_start_matches(|c: char| c.is_whitespace());
    if t.len() == rest.len() {
        return None;
    }
    let t = t.strip_prefix('"')?;
    let end = t.find('"')?;
    if end == 0 || !t[end + 1..].chars().all(|c| c == ' ' || c == '\t') {
        return None;
    }
    Some(&t[..end])
}

/// Replace `#include "name"` lines by the file's text (searched in `dirs`).
pub fn expand_includes(text: &str, dirs: &[PathBuf], depth: u32) -> Result<String> {
    if depth > 4 {
        return Err(err!("#include nests deeper than 4 levels"));
    }
    let mut out = String::with_capacity(text.len());
    for (i, line) in text.split('\n').enumerate() {
        if i > 0 {
            out.push('\n');
        }
        match include_name(line) {
            Some(name) => {
                let found = dirs.iter().map(|d| d.join(name)).find(|f| f.is_file());
                let Some(f) = found else {
                    return Err(err!(
                        "#include \"{name}\" not found in {:?}",
                        dirs.iter()
                            .map(|d| d.display().to_string())
                            .collect::<Vec<_>>()
                    ));
                };
                out.push_str(&expand_includes(&read_text(&f)?, dirs, depth + 1)?);
            }
            None => out.push_str(line),
        }
    }
    Ok(out)
}

/// `#ifdef NAME` / `#ifndef NAME` / `#else` / `#endif` lines (alone on their line, nestable). A line outside the taken branches is
/// dropped together with the directive lines.
pub fn apply_conditionals(text: &str, defines: &[String]) -> Result<String> {
    let mut out: Vec<&str> = vec![];
    let mut stack: Vec<bool> = vec![];
    for (n, line) in text.split('\n').enumerate() {
        let n = n + 1;
        let Some((kind, name)) = directive(line) else {
            if stack.iter().all(|b| *b) {
                out.push(line);
            }
            continue;
        };
        match kind {
            "ifdef" | "ifndef" => {
                let Some(name) = name else {
                    return Err(err!("line {n}: #{kind} needs a name"));
                };
                stack.push(defines.iter().any(|d| d == name) == (kind == "ifdef"));
            }
            _ if stack.is_empty() => return Err(err!("line {n}: #{kind} without #ifdef")),
            "else" => {
                let l = stack.len() - 1;
                stack[l] = !stack[l];
            }
            _ => {
                stack.pop();
            }
        }
    }
    if !stack.is_empty() {
        return Err(err!("#ifdef without #endif"));
    }
    Ok(out.join("\n"))
}

/// `^#(ifdef|ifndef|else|endif)(?:[ \t]+(\w+))?[ \t]*$`
fn directive(line: &str) -> Option<(&'static str, Option<&str>)> {
    let rest = line.strip_prefix('#')?;
    for k in ["ifdef", "ifndef", "else", "endif"] {
        let Some(after) = rest.strip_prefix(k) else {
            continue;
        };
        let t = after.trim_end_matches([' ', '\t']);
        if t.is_empty() {
            return Some((k, None));
        }
        let name = t.trim_start_matches([' ', '\t']);
        if name.len() < t.len()
            && !name.is_empty()
            && name.chars().all(|c| c.is_alphanumeric() || c == '_')
        {
            return Some((k, Some(name)));
        }
    }
    None
}

/// The text of a SHARC+ object: the sources joined, `#include`s expanded, the `#ifdef` alternatives selected.
pub fn source_text(
    root: &Path,
    moddir: &Path,
    sources: &[PathBuf],
    defines: &[String],
) -> Result<String> {
    let mut dirs = include_dirs(root, moddir);
    for p in sources {
        let d = p.parent().unwrap_or(Path::new(".")).to_path_buf();
        if !dirs.contains(&d) {
            dirs.push(d);
        }
    }
    let mut texts = vec![];
    for p in sources {
        texts.push(read_text(p)?);
    }
    apply_conditionals(&expand_includes(&texts.join("\n"), &dirs, 0)?, defines)
}

/// The `source_sha256` of a SHARC+ object: the SHA-256 of that text.
pub fn source_hash(text: &str) -> String {
    sha256_hex(text.as_bytes())
}

/// Assemble and verify `text` (`pk_sharc object`).
pub fn sharc_object(env: &Env, text: &str) -> Result<Dko> {
    let tmp = TempDir::new("pk-sharc-text-")?;
    let f = tmp.path().join("module.s");
    std::fs::write(&f, text).map_err(|e| err!("{}: {e}", f.display()))?;
    let o = std::process::Command::new("uv")
        .args([
            "run",
            "python",
            "-m",
            "tools.patchkit.pk_sharc",
            "object",
            "--text",
        ])
        .arg(&f)
        .current_dir(&env.root)
        .output()
        .map_err(|e| {
            err!("uv: {e} (the SHARC+ assembler runs as a Python tool: tools/patchkit/pk_sharc.py)")
        })?;
    if !o.status.success() {
        let msg = String::from_utf8_lossy(&o.stderr);
        return Err(err!("{}", msg.trim_end()));
    }
    Ok(serde_json::from_slice(&o.stdout)?)
}
