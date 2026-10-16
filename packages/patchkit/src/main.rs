//! `patchkit` command line.

use patchkit::apply;
use patchkit::error::{Error, Result, sha256_hex};
use patchkit::firmware::Firmware;
use patchkit::module::Library;
use patchkit::port::Registry;
use patchkit::verify;
use std::path::{Path, PathBuf};

const USAGE: &str = "usage: patchkit [--root DIR] COMMAND ...

  inspect X.syx                          device, version, port, patch record
  check   --firmware X.syx [--modules a,b]  which modules fit, and why not
  apply   --firmware X.syx --modules a,b [-o OUT.syx] [--plan plan.json] [--no-record (oracles only)]
  (any command that reads modules: [--modules-dir DIR], PATCHKIT_MODULES=dir:dir for extra module roots)
  explain --firmware X.syx (--modules a,b | --plan plan.json)   what a patch does, site by site
  verify  OUT.syx [--base X.syx]         re-decode and check an image
  rebuild X.syx -o OUT.syx               codec round trip (an unchanged image reproduces itself)
  pack    MODULE_ID -o OUT.dkm [--name N] [--runtime-min V]   the module's ColdFire object as a run-time .dkm
  dkm-info FILE.dkm                      check a .dkm and list what it holds
  pack-section IN OUT                    aPLib-pack a raw file as a section stream (for tests)
  list                                   modules under modules/ and ports under devices/ports/
  gen [--check] [--modules a,b]          write (or check) build/gen/ of modules with typed sections: Rust constants,
                                         a SHARC include per device, gallery JSON
";

struct Args {
    root: PathBuf,
    rest: Vec<String>,
}

fn find_root(given: Option<String>) -> PathBuf {
    if let Some(g) = given {
        return PathBuf::from(g);
    }
    let mut d = std::env::current_dir().unwrap_or_default();
    loop {
        if d.join("devices/ports").is_dir() {
            return d;
        }
        if !d.pop() {
            return std::env::current_dir().unwrap_or_default();
        }
    }
}

fn flag(rest: &[String], name: &str) -> Option<String> {
    rest.iter()
        .position(|a| a == name)
        .and_then(|i| rest.get(i + 1).cloned())
}

fn registry(root: &Path) -> Result<Registry> {
    Registry::from_dir(&root.join("devices/ports"))
}

/// `modules/` of the repository, then the extra roots of `--modules-dir DIR` and the PATCHKIT_MODULES
/// environment variable (a path list), in that order. Generated modules (derived from a firmware) live there.
fn library(root: &Path, rest: &[String]) -> Result<Library> {
    let mut lib = Library::scan(&root.join("modules"))?;
    let mut extra: Vec<PathBuf> = flag(rest, "--modules-dir")
        .into_iter()
        .map(PathBuf::from)
        .collect();
    if let Some(v) = std::env::var_os("PATCHKIT_MODULES") {
        extra.extend(std::env::split_paths(&v));
    }
    for d in extra {
        if d.is_dir() {
            lib.modules.extend(Library::scan(&d)?.modules);
        }
    }
    lib.lower(&patchkit::typed::Models::from_dir(&root.join("devices"))?)?;
    Ok(lib)
}

/// The selection plus its requirements; says what it added.
fn with_requires(lib: &Library, ids: Vec<String>) -> Result<Vec<String>> {
    let (all, added) = lib.with_requires(&ids)?;
    if !added.is_empty() {
        println!("added by requires: {}", added.join(", "));
    }
    Ok(all)
}

fn read(p: &str) -> Result<Vec<u8>> {
    std::fs::read(p).map_err(|e| Error(format!("{p}: {e}")))
}

fn write(p: &str, b: &[u8]) -> Result<()> {
    std::fs::write(p, b).map_err(|e| Error(format!("{p}: {e}")))
}

fn need(rest: &[String]) -> Result<&String> {
    rest.first().ok_or_else(|| Error(USAGE.into()))
}

fn run(a: Args) -> Result<()> {
    let cmd = a.rest.first().map(|s| s.as_str()).unwrap_or("");
    let rest = if a.rest.is_empty() {
        &a.rest[..]
    } else {
        &a.rest[1..]
    };
    match cmd {
        "inspect" => {
            let raw = read(need(rest)?)?;
            let fw = Firmware::parse(&raw)?;
            let reg = registry(&a.root)?;
            println!("device    {}", fw.device.short());
            println!("sha256    {}", fw.syx_sha256);
            match reg.find(&fw.syx_sha256) {
                Some(p) => println!("port      {} (stock image)", p.id()),
                None => println!("port      none for this sha256"),
            }
            let rep = verify::verify(&raw, Some(&reg), None)?;
            match rep.record {
                Some(r) => println!(
                    "record    patched from {} with {}",
                    r.base_sha256,
                    r.modules.join(", ")
                ),
                None => println!("record    none"),
            }
            for (id, d, s) in rep.sections {
                println!("section {id}  {d} bytes decoded, {s} stored");
            }
        }
        "list" => {
            let reg = registry(&a.root)?;
            for p in &reg.ports {
                println!("port   {}  {}", p.id(), p.port.syx_sha256);
            }
            let lib = library(&a.root, rest)?;
            for m in &lib.modules {
                println!(
                    "module {}  {}  {}",
                    m.tag(),
                    m.manifest.module.status,
                    m.manifest.module.summary
                );
            }
        }
        "check" | "apply" => {
            let fwp = flag(rest, "--firmware").ok_or_else(|| Error(USAGE.into()))?;
            let ids: Vec<String> = flag(rest, "--modules")
                .unwrap_or_default()
                .split(',')
                .filter(|s| !s.is_empty())
                .map(String::from)
                .collect();
            let raw = read(&fwp)?;
            let reg = registry(&a.root)?;
            let lib = library(&a.root, rest)?;
            let ids = with_requires(&lib, ids)?;
            if cmd == "check" {
                let fw = Firmware::parse(&raw)?;
                let port = reg
                    .find(&fw.syx_sha256)
                    .ok_or_else(|| Error(format!("no port for {}", fw.syx_sha256)))?;
                for m in &lib.modules {
                    match apply::compatible(m, port) {
                        Ok(()) => println!("ok       {}", m.tag()),
                        Err(e) => println!("refused  {e}"),
                    }
                }
                if !ids.is_empty() {
                    apply::apply(&raw, &reg, &lib, &ids)?;
                    println!("the selection applies");
                }
            } else {
                let opts = apply::Options {
                    record: !rest.iter().any(|a| a == "--no-record"),
                };
                let out = apply::apply_with(&raw, &reg, &lib, &ids, opts)?;
                if let Some(o) = flag(rest, "-o") {
                    write(&o, &out.syx)?;
                    println!(
                        "{o}: {} bytes, sha256 {}",
                        out.syx.len(),
                        sha256_hex(&out.syx)
                    );
                }
                if let Some(p) = flag(rest, "--plan") {
                    let j = serde_json::to_string_pretty(&out.plan)?;
                    write(&p, (j + "\n").as_bytes())?;
                }
                let o = out.plan.output.as_ref().unwrap();
                println!(
                    "applied {} to {} {}: {} writes, container {} bytes",
                    out.plan.modules.join(", "),
                    out.plan.base.device,
                    out.plan.base.version,
                    out.plan.writes.len(),
                    o.container_bytes
                );
            }
        }
        "gen" => {
            let lib = library(&a.root, rest)?;
            let models = patchkit::typed::Models::from_dir(&a.root.join("devices"))?;
            let only: Vec<String> = flag(rest, "--modules")
                .unwrap_or_default()
                .split(',')
                .filter(|s| !s.is_empty())
                .map(String::from)
                .collect();
            let check = rest.iter().any(|a| a == "--check");
            let mut stale = 0;
            for m in &lib.modules {
                if !only.is_empty() && !only.iter().any(|o| o == m.id()) {
                    continue;
                }
                for (rel, text) in patchkit::modgen::generate(&lib, &models, m.id())? {
                    let path = a.root.join("modules").join(m.id()).join(&rel);
                    let have = std::fs::read_to_string(&path).ok();
                    if have.as_deref() == Some(text.as_str()) {
                        continue;
                    }
                    if check {
                        println!("stale    {}", path.display());
                        stale += 1;
                    } else {
                        if let Some(d) = path.parent() {
                            std::fs::create_dir_all(d)
                                .map_err(|e| Error(format!("{}: {e}", d.display())))?;
                        }
                        write(&path.to_string_lossy(), text.as_bytes())?;
                        println!("wrote    {}", path.display());
                    }
                }
            }
            if stale > 0 {
                return Err(Error(format!(
                    "{stale} generated file(s) are stale: run `patchkit gen`"
                )));
            }
        }
        "explain" => {
            let fwp = flag(rest, "--firmware").ok_or_else(|| Error(USAGE.into()))?;
            let raw = read(&fwp)?;
            let reg = registry(&a.root)?;
            let lib = library(&a.root, rest)?;
            let plan_path = flag(rest, "--plan");
            let mut ids: Vec<String> = flag(rest, "--modules")
                .unwrap_or_default()
                .split(',')
                .filter(|s| !s.is_empty())
                .map(String::from)
                .collect();
            let old_plan = match &plan_path {
                Some(p) => Some(serde_json::from_slice::<serde_json::Value>(&read(p)?)?),
                None => None,
            };
            if ids.is_empty()
                && let Some(p) = &old_plan
            {
                ids = p["modules"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .filter_map(|m| m.as_str())
                    .map(|m| m.split('@').next().unwrap().to_string())
                    .collect();
            }
            let ids = with_requires(&lib, ids)?;
            let out = apply::apply(&raw, &reg, &lib, &ids)?;
            print!("{}", patchkit::explain::explain(&raw, &out)?);
            if let Some(p) = old_plan {
                let now = serde_json::to_value(&out.plan)?;
                println!(
                    "
plan.json {}",
                    if p == now {
                        "matches this patch exactly"
                    } else {
                        "DIFFERS from this patch"
                    }
                );
                if p != now {
                    std::process::exit(1);
                }
            }
        }
        "verify" => {
            let raw = read(need(rest)?)?;
            let reg = registry(&a.root)?;
            let base = match flag(rest, "--base") {
                Some(b) => Some(read(&b)?),
                None => None,
            };
            let rep = verify::verify(&raw, Some(&reg), base.as_deref())?;
            for c in &rep.checks {
                println!("ok  {c}");
            }
            match &rep.record {
                Some(r) => println!(
                    "ok  record: base {} modules {}",
                    r.base_sha256,
                    r.modules.join(", ")
                ),
                None => println!("--  no patch record (stock image)"),
            }
            println!(
                "verified {} ({} bytes container)",
                rep.device, rep.container_bytes
            );
        }
        "rebuild" => {
            let fw = Firmware::parse(&read(need(rest)?)?)?;
            let o = flag(rest, "-o").ok_or_else(|| Error(USAGE.into()))?;
            write(&o, &fw.build(None)?)?;
        }
        "pack" => {
            let id = need(rest)?;
            let out = flag(rest, "-o").ok_or_else(|| Error(USAGE.into()))?;
            let lib = library(&a.root, rest)?;
            let m = lib
                .get(id)
                .ok_or_else(|| Error(format!("no module {id}")))?;
            let cf: Vec<_> = m
                .objects
                .iter()
                .zip(&m.manifest.object)
                .filter(|(o, _)| o.arch == "cf")
                .collect();
            if cf.len() != 1 {
                return Err(Error(format!(
                    "{id} has {} ColdFire objects; a .dkm packs exactly one",
                    cf.len()
                )));
            }
            let opts = patchkit::dkm::Options {
                name: flag(rest, "--name").unwrap_or_else(|| id.clone()),
                version: patchkit::dkm::parse_version(&m.manifest.module.version)?,
                runtime_min: match flag(rest, "--runtime-min") {
                    Some(v) => v.parse().map_err(|_| Error("--runtime-min: a number".into()))?,
                    None => patchkit::dkm::RUNTIME_VERSION,
                },
            };
            let file = patchkit::dkm::pack(cf[0].0, &opts)?;
            write(&out, &file)?;
            let d = patchkit::dkm::Dkm::parse(&file)?;
            println!(
                "{out}: {} bytes, {} (image {} + bss {}, {} relocations, {} imports), sha256 {}",
                file.len(),
                d.name,
                d.image.len(),
                d.bss,
                d.relocs.len(),
                d.imports.len(),
                d.sha256
            );
        }
        "dkm-info" => {
            let d = patchkit::dkm::Dkm::parse(&read(need(rest)?)?)?;
            println!("name      {}", d.name);
            println!("version   {}.{}.{}", d.version >> 16, d.version >> 8 & 255, d.version & 255);
            println!("runtime   >= {}", d.runtime_min);
            println!("sha256    {}", d.sha256);
            println!("image     {} bytes, bss {}, align {}", d.image.len(), d.bss, d.align);
            println!("relocs    {}", d.relocs.len());
            println!("imports   {}", d.imports.join(", "));
            println!("init      {:?}  fini {:?}", d.init, d.fini);
            println!("slots     {:?}", d.slots);
            println!("register  {} bytes", d.register.len());
        }
        "pack-section" => {
            let data = read(need(rest)?)?;
            let o = rest.get(1).ok_or_else(|| Error(USAGE.into()))?;
            write(o, &patchkit::aplib::pack_section(&data))?;
        }
        _ => {
            eprint!("{USAGE}");
            std::process::exit(2);
        }
    }
    Ok(())
}

fn main() {
    let mut args: Vec<String> = std::env::args().skip(1).collect();
    let mut given = None;
    if args.first().is_some_and(|a| a == "--root") && args.len() >= 2 {
        given = Some(args[1].clone());
        args.drain(0..2);
    }
    let a = Args {
        root: find_root(given),
        rest: args,
    };
    if let Err(e) = run(a) {
        eprintln!("patchkit: {e}");
        std::process::exit(1);
    }
}
