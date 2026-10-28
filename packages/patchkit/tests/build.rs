//! `patchkit build`: the canonical JSON writer, the ELF to object conversion, the link and the ISA check, the freshness hash and the
//! build of a small module. Tests that run the m68k-elf toolchain skip when it is not installed (`pk_toolchains m68k`).

use patchkit::build::cf::{self, BASE};
use patchkit::build::elf::{self, RelocTo};
use patchkit::build::toolchain::Env;
use patchkit::build::{self, BuildOptions, Status, canon, rust, sharc};
use patchkit::dko::Dko;
use std::collections::HashMap;
use std::path::{Path, PathBuf};

fn root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../..")
}

/// `data/tiny.s` assembled with -mcpu=54418.
fn tiny() -> Vec<u8> {
    include_bytes!("data/tiny.o").to_vec()
}

fn toolchain() -> Option<Env> {
    let env = Env::new(&root());
    env.m68k("gcc").is_ok().then_some(env)
}

fn find_objects(dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(rd) = std::fs::read_dir(dir) else {
        return;
    };
    for e in rd.flatten() {
        let p = e.path();
        if p.is_dir() {
            find_objects(&p, out);
        } else if p.to_string_lossy().ends_with(".dko.json")
            && p.parent().is_some_and(|d| d.ends_with("build"))
        {
            out.push(p);
        }
    }
}

#[test]
fn committed_objects_are_in_the_canonical_form() {
    let mut files = vec![];
    for r in ["modules", "tests/fixtures/modules"] {
        find_objects(&root().join(r), &mut files);
    }
    assert!(files.len() > 40, "{} objects", files.len());
    for f in files {
        let text = std::fs::read_to_string(&f).unwrap();
        let v: serde_json::Value = serde_json::from_str(&text).unwrap();
        assert!(
            canon::to_string(&v) == text,
            "{} is not canonical",
            f.display()
        );
    }
}

#[test]
fn local_binary_hashes_are_not_part_of_the_comparison() {
    let make = |selas: &str, rev: &str, hex: &str| serde_json::json!({"sections": [{"hex": hex}], "toolchain": {"selache_rev": rev, "selas_sha256": selas, "seldump_sha256": selas}});
    let base = build::comparable(make("s1", "r1", "00"));
    assert_eq!(base, build::comparable(make("s2", "r1", "00")));
    assert_ne!(base, build::comparable(make("s1", "r2", "00")));
    assert_ne!(base, build::comparable(make("s1", "r1", "01")));
}

#[test]
fn make_dependency_lists_are_split_like_make_does() {
    let t = "x: a.c \\\n  b.h c\\ d.h\n";
    assert_eq!(cf::parse_make_deps(t), ["a.c", "b.h", "c d.h"]);
    assert!(cf::parse_make_deps("x:\n").is_empty());
}

#[test]
fn compiler_helper_names() {
    for n in [
        "memcpy",
        "memmove",
        "memset",
        "memcmp",
        "bcmp",
        "__muldi3",
        "__udivdi3",
        "__umoddi3",
        "__ashldi3",
        "__clzsi2",
        "__popcountdi2",
        "__addsf3",
        "__floatsidf",
        "__fixdfsi",
        "__truncdfsf2",
        "__extendsfdf2",
        "__eqdf2",
        "__multi3",
    ] {
        assert!(rust::is_helper(n), "{n}");
    }
    for n in [
        "dkrt_start",
        "memory",
        "__rust_alloc",
        "mem",
        "_RNvC4core3fmt",
        "memcpy2",
    ] {
        assert!(!rust::is_helper(n), "{n}");
    }
}

#[test]
fn ifdef_alternatives_and_includes_of_dsp_sources() {
    let src = "a\n#ifdef X\nb\n#else\nc\n#endif\n#ifndef X\nd\n#endif\ne";
    assert_eq!(sharc::apply_conditionals(src, &[]).unwrap(), "a\nc\nd\ne");
    assert_eq!(
        sharc::apply_conditionals(src, &["X".into()]).unwrap(),
        "a\nb\ne"
    );
    let nested = "#ifdef X\n#ifdef Y\ny\n#else\nnoy\n#endif\n#endif\nz";
    assert_eq!(
        sharc::apply_conditionals(nested, &["X".into()]).unwrap(),
        "noy\nz"
    );
    assert_eq!(
        sharc::apply_conditionals(nested, &["Y".into()]).unwrap(),
        "z"
    );
    assert!(sharc::apply_conditionals("#ifdef X\na", &[]).is_err());
    assert!(sharc::apply_conditionals("#endif", &[]).is_err());
    assert!(sharc::apply_conditionals("#ifdef\nx\n#endif", &[]).is_err());

    let d = std::env::temp_dir().join(format!("pk-build-test-inc-{}", std::process::id()));
    std::fs::create_dir_all(&d).unwrap();
    std::fs::write(d.join("a.inc"), "#define A 1\n").unwrap();
    let out =
        sharc::expand_includes("x\n#include \"a.inc\"\ny", std::slice::from_ref(&d), 0).unwrap();
    assert_eq!(out, "x\n#define A 1\n\ny");
    assert!(
        sharc::expand_includes("#include \"missing.inc\"", std::slice::from_ref(&d), 0).is_err()
    );
    std::fs::remove_dir_all(&d).unwrap();
}

#[test]
fn the_elf_becomes_an_object_prototype() {
    let p = elf::proto_from_elf(&tiny()).unwrap();
    let names: Vec<(&str, &str)> = p
        .sections
        .iter()
        .map(|s| (s.name.as_str(), s.kind))
        .collect();
    assert_eq!(
        names,
        [
            (".text", "text"),
            (".bss", "bss"),
            (".rodata.tiny", "rodata")
        ]
    );
    let sym = |n: &str| {
        p.symbols
            .iter()
            .find(|s| s.name == n)
            .unwrap_or_else(|| panic!("{n}"))
    };
    assert_eq!(sym("tiny_call").section.as_deref(), Some(".text"));
    assert!(sym("tiny_call").global);
    assert_eq!(sym("tiny_word").section.as_deref(), Some(".rodata.tiny"));
    assert!(!sym("tiny_scratch").global);
    assert_eq!(sym("ext_fn").section, None);
    // relocations in the order of the ELF: .rela.text (as the assembler lists them), then .rela.rodata.tiny
    let r: Vec<(&str, u64, &str)> = p
        .relocs
        .iter()
        .map(|r| (r.section.as_str(), r.offset, r.rtype))
        .collect();
    assert_eq!(
        r,
        [
            (".text", 8, "PC32"),
            (".text", 14, "32"),
            (".text", 2, "32"),
            (".rodata.tiny", 4, "32")
        ]
    );
    assert!(matches!(&p.relocs[1].to, RelocTo::Section(s) if s == ".rodata.tiny"));
    assert!(matches!(&p.relocs[0].to, RelocTo::Symbol(s) if s == "ext_near"));
}

#[test]
fn a_big_endian_m68k_object_only() {
    assert!(elf::proto_from_elf(b"not an elf").is_err());
    let mut le = tiny();
    le[5] = 1; // little endian
    assert!(
        elf::proto_from_elf(&le)
            .unwrap_err()
            .0
            .contains("big-endian m68k")
    );
}

fn symmap(names: &[&str]) -> HashMap<String, u32> {
    names
        .iter()
        .enumerate()
        .map(|(i, n)| (n.to_string(), BASE + 0x100 + 8 * i as u32))
        .collect()
}

#[test]
fn link_places_sections_and_applies_relocations() {
    let p = elf::proto_from_elf(&tiny()).unwrap();
    let l = cf::link(&p, BASE, &symmap(&["ext_fn", "ext_near"])).unwrap();
    let w = |a: u32| u32::from_be_bytes(l.image[(a - BASE) as usize..][..4].try_into().unwrap());
    assert_eq!(l.placement[".text"], BASE);
    let word = l.symbols["tiny_word"];
    assert_eq!(w(word), 0x12345678);
    assert_eq!(w(word + 4), l.symbols["tiny_call"]); // the data word holds an address
    // jsr abs.l: the operand is the extern's address; bsr.l: displacement from the extension word
    assert_eq!(w(BASE + 2), BASE + 0x100);
    assert_eq!(
        w(BASE + 8) as i32,
        (BASE + 0x108) as i32 - (BASE + 8) as i32
    );
    assert_eq!(w(BASE + 14), word); // move.l #tiny_word: a section-relative absolute
    assert_eq!(l.relocs_applied["PC32"], 1);
    assert_eq!(l.relocs_applied["32"], 3);
    assert_eq!(l.bss_size, 16);
    // a different base moves only the relocated words
    let m = cf::link(&p, BASE + 0x1000, &symmap(&["ext_fn", "ext_near"])).unwrap();
    assert_eq!(m.image.len(), l.image.len());
    assert_ne!(m.image, l.image);
    // every instruction is ISA_A/B/C
    let hist = cf::check_isa(&l).unwrap();
    assert!(hist.values().sum::<u32>() >= 4, "{hist:?}");
}

#[test]
fn link_refuses_what_it_cannot_resolve() {
    let p = elf::proto_from_elf(&tiny()).unwrap();
    let e = cf::link(&p, BASE, &HashMap::new()).err().unwrap();
    assert!(e.0.contains("unresolved extern symbol"), "{e}");
    // a PC32 reaches everything, but not an absolute 16-bit field: build the case from a PC16 relocation
    let mut q = p.clone();
    q.relocs[0].rtype = "PC16";
    let mut far = symmap(&["ext_fn", "ext_near"]);
    far.insert("ext_near".into(), 0x4000_0000);
    let e = cf::link(&q, BASE, &far).err().unwrap();
    assert!(e.0.contains("out of range"), "{e}");
}

#[test]
fn the_object_carries_its_isa_record() {
    let p = elf::proto_from_elf(&tiny()).unwrap();
    let o = cf::dko_from_proto(&p, "ab".repeat(32), Default::default()).unwrap();
    assert_eq!(o.arch, "cf");
    assert_eq!(o.toolchain["isa_check"], o.isa_digest().unwrap());
    assert!(
        o.toolchain["instructions_redecoded"]
            .parse::<u32>()
            .unwrap()
            >= 4
    );
    // section-relative relocations get a local symbol `.sec.<section>`; symbols are sorted by name
    assert!(
        o.symbols
            .iter()
            .any(|s| s.name == ".sec..rodata.tiny" && !s.global)
    );
    assert!(o.symbols.windows(2).all(|w| w[0].name <= w[1].name));
    assert!(
        o.relocs
            .iter()
            .any(|r| r.rtype == "cf.pc32" && r.symbol == "ext_near")
    );
    o.require_isa_checked().unwrap();
    // the text goes through the canonical writer and parses back
    let text = build::to_text(&o).unwrap();
    assert_eq!(Dko::parse(&text).unwrap(), o);
    assert!(text.starts_with("{\n \"arch\": \"cf\",\n \"format\": 1,\n"));
}

#[test]
fn a_68020_form_is_refused() {
    let Some(env) = toolchain() else { return };
    let d = build_dir("isa");
    let s = d.join("bad.s");
    std::fs::write(&s, ".text\n.globl f\nf:\n  bfextu (%a0){4:8},%d0\n  rts\n").unwrap();
    let o = d.join("bad.o");
    let st = std::process::Command::new(env.m68k("as").unwrap())
        .args(["-m68020", "-o"])
        .arg(&o)
        .arg(&s)
        .status()
        .unwrap();
    assert!(st.success());
    let p = elf::proto_from_elf(&std::fs::read(&o).unwrap()).unwrap();
    let e = cf::dko_from_proto(&p, "00".repeat(32), Default::default())
        .err()
        .unwrap();
    assert!(e.0.contains("outside ISA_A/B/C+EMAC"), "{e}");
    assert!(e.0.contains("illegal encoding"), "{e}");
}

fn build_dir(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("pk-build-test-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

#[test]
fn a_header_change_changes_the_source_hash() {
    let Some(env) = toolchain() else { return };
    let d = build_dir("hash");
    std::fs::write(d.join("a.h"), "#define N 1\n").unwrap();
    std::fs::write(
        d.join("a.c"),
        "#include \"a.h\"\nint f(void) { return N; }\n",
    )
    .unwrap();
    std::fs::write(d.join("b.inc"), ".equ N, 1\n").unwrap();
    std::fs::write(d.join("b.s"), ".include \"b.inc\"\n.text\nnop\n").unwrap();
    let inc = [d.clone()];
    let hash = |f: &str| cf::source_digest(&env, &[d.join(f)], &inc).unwrap();
    let (a, b) = (hash("a.c"), hash("b.s"));
    assert_eq!(a, hash("a.c"));
    std::fs::write(d.join("a.h"), "#define N 2\n").unwrap();
    std::fs::write(d.join("b.inc"), ".equ N, 2\n").unwrap();
    assert_ne!(a, hash("a.c"));
    assert_ne!(b, hash("b.s"));
    let _ = std::fs::remove_dir_all(&d);
}

#[test]
fn a_module_builds_once_and_then_is_unchanged() {
    let Some(_) = toolchain() else { return };
    let d = build_dir("module");
    std::fs::create_dir_all(d.join("src")).unwrap();
    std::fs::write(
        d.join("module.toml"),
        "[module]\nid = \"test/tiny\"\n\n[[object]]\npath = \"build/tiny.dko.json\"\nsources = [\"src/t.c\"]\n",
    )
    .unwrap();
    std::fs::write(
        d.join("src/t.c"),
        "extern int ext(int);\nint twice(int x) { return ext(x) + ext(x); }\n",
    )
    .unwrap();
    let opts = BuildOptions {
        root: root(),
        ..Default::default()
    };
    let r = build::build_module(&d, &opts).unwrap();
    assert_eq!(r.objects[0].status, Status::Built);
    assert!(r.objects[0].instructions.unwrap() > 3);
    let text = std::fs::read_to_string(d.join("build/tiny.dko.json")).unwrap();
    let o = Dko::parse(&text).unwrap();
    o.require_isa_checked().unwrap();
    assert!(
        o.symbols
            .iter()
            .any(|s| s.name == "ext" && s.section.is_none())
    );
    // nothing changed: not compiled again; --force compiles it; the result is the same bytes
    let again = build::build_module(&d, &opts).unwrap();
    assert_eq!(again.objects[0].status, Status::Unchanged);
    assert_eq!(
        build::module_status(&root(), &d).unwrap(),
        [("build/tiny.dko.json".to_string(), true)]
    );
    let forced = build::build_module(
        &d,
        &BuildOptions {
            force: true,
            ..opts.clone()
        },
    )
    .unwrap();
    assert_eq!(forced.objects[0].status, Status::Built);
    assert_eq!(
        std::fs::read_to_string(d.join("build/tiny.dko.json")).unwrap(),
        text
    );
    // --check: current, then stale after a source change
    let check = BuildOptions {
        check: true,
        ..opts.clone()
    };
    assert_eq!(
        build::build_module(&d, &check).unwrap().objects[0].status,
        Status::Current
    );
    std::fs::write(
        d.join("src/t.c"),
        "extern int ext(int);\nint twice(int x) { return ext(x) * 2; }\n",
    )
    .unwrap();
    assert!(!build::module_status(&root(), &d).unwrap()[0].1);
    let e = build::build_module(&d, &check).err().unwrap();
    assert!(e.0.contains("is stale"), "{e}");
    // a build into another directory leaves the module alone
    let out = build_dir("module-out");
    let o2 = BuildOptions {
        out: Some(out.clone()),
        ..opts
    };
    let r = build::build_module(&d, &o2).unwrap();
    assert_eq!(r.objects[0].status, Status::Built);
    assert!(out.join("build/tiny.dko.json").is_file());
    assert_eq!(
        std::fs::read_to_string(d.join("build/tiny.dko.json")).unwrap(),
        text
    );
    let _ = std::fs::remove_dir_all(&d);
    let _ = std::fs::remove_dir_all(&out);
}

#[test]
fn defines_need_an_output_directory() {
    let opts = BuildOptions {
        root: root(),
        defines: vec!["X".into()],
        ..Default::default()
    };
    let e = build::build_module(Path::new("/nonexistent"), &opts)
        .err()
        .unwrap();
    assert!(e.0.contains("--define needs --out"), "{e}");
}
