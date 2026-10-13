//! `core/params` over the real ports and stock images (skipped when the images are absent): the relocated table, the
//! repointed references, the raised bounds, the second table's shim, canonical codes and the refusals.

use patchkit::apply::{self, Applied, Options};
use patchkit::firmware::Firmware;
use patchkit::module::{Library, Module};
use patchkit::port::Registry;
use std::path::PathBuf;

fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

fn image(name: &str) -> Option<Vec<u8>> {
    std::fs::read(root().join(name)).ok()
}

const DT2_117: &str = "Digitakt_II_OS1.17.syx";
const DT2_116: &str = "Digitakt_II_OS1.16.syx";
const DN2_112: &str = "Digitone_II_OS1.12.syx";

fn registry() -> Registry {
    Registry::from_dir(&root().join("devices/ports")).unwrap()
}

fn library(extra: Vec<Module>) -> Library {
    let mut lib = Library::scan(&root().join("modules")).unwrap();
    lib.modules.extend(extra);
    lib
}

fn machine_module(id: &str, requires: &str, machines: &str) -> Module {
    let text = format!(
        "[module]\nid = \"{id}\"\nversion = \"1.0.0\"\nname = \"{id}\"\ncategory = \"test\"\nstatus = \"experimental\"\nsummary = \"synthetic\"\nrequires = {requires}\n{machines}\n"
    );
    Module::from_parts(&text, &|_p: &str| Ok(String::new())).unwrap()
}

fn run(file: &str, lib: &Library, ids: &[&str]) -> Option<patchkit::Result<Applied>> {
    let raw = image(file)?;
    let ids: Vec<String> = ids.iter().map(|s| s.to_string()).collect();
    Some(apply::apply_with(
        &raw,
        &registry(),
        lib,
        &ids,
        Options::default(),
    ))
}

/// Memory of the patched MAIN OS: the image plus the segment chain behind it.
struct Main {
    data: Vec<u8>,
    base: u32,
    segs: Vec<(u32, Vec<u8>)>,
}

impl Main {
    fn new(syx: &[u8], stock_len: usize) -> Main {
        let fw = Firmware::parse(syx).unwrap();
        let s = fw.section(3).unwrap();
        let data = s.data.clone();
        let mut segs = vec![];
        let mut at = stock_len;
        loop {
            let len = u32::from_be_bytes(data[at..at + 4].try_into().unwrap());
            if len == u32::MAX {
                break;
            }
            let dest = u32::from_be_bytes(data[at + 4..at + 8].try_into().unwrap());
            segs.push((dest, data[at + 12..at + 12 + len as usize].to_vec()));
            at += 12 + len as usize;
        }
        Main {
            data,
            base: s.dest,
            segs,
        }
    }
    fn read(&self, addr: u32, n: usize) -> Vec<u8> {
        if let Some(o) = addr.checked_sub(self.base)
            && (o as usize + n) <= self.data.len()
        {
            return self.data[o as usize..o as usize + n].to_vec();
        }
        for (d, p) in &self.segs {
            if addr >= *d && (addr - d) as usize + n <= p.len() {
                return p[(addr - d) as usize..(addr - d) as usize + n].to_vec();
            }
        }
        panic!("{addr:#x}+{n} is not in the patched MAIN OS");
    }
    fn u32s(&self, addr: u32, n: usize) -> Vec<u32> {
        self.read(addr, n * 4)
            .chunks(4)
            .map(|c| u32::from_be_bytes(c.try_into().unwrap()))
            .collect()
    }
    fn cstr(&self, addr: u32) -> String {
        (0..64)
            .map(|i| self.read(addr + i, 1)[0])
            .take_while(|&b| b != 0)
            .map(|b| b as char)
            .collect()
    }
}

fn err_of(r: patchkit::Result<Applied>) -> String {
    match r {
        Ok(_) => panic!("expected an error"),
        Err(e) => e.0,
    }
}

fn symbol(a: &Applied, name: &str) -> u32 {
    *a.plan
        .symbols
        .iter()
        .find(|(k, _)| k.ends_with(&format!(":{name}")))
        .unwrap_or_else(|| panic!("no symbol {name}"))
        .1 as u32
}

fn stock_len(file: &str) -> usize {
    Firmware::parse(&image(file).unwrap())
        .unwrap()
        .section(3)
        .unwrap()
        .data
        .len()
}

fn param_module(id: &str, requires: &str, params: &str) -> Module {
    machine_module(id, requires, params)
}

fn param(id: &str, name: &str, slot: u32, template: u32) -> String {
    format!(
        "[[param]]\nid = \"{id}\"\nname = \"{name}\"\nshort = \"{}\"\ntemplate = {template}\nslot = {slot}\nmin = 0\nmax = 32512\ndefault = 16384\n",
        name.to_uppercase()
    )
}

fn two_params() -> Library {
    library(vec![
        param_module(
            "test/pa",
            "[\"core/params\"]",
            &param("pa_x", "Xray", 1, 0xd1),
        ),
        param_module(
            "test/pb",
            "[\"core/params\"]",
            &(param("pb_y", "Yankee", 2, 0xd1) + &param("pb_z", "Zulu", 4, 0xcc)),
        ),
    ])
}

const SET: [&str; 3] = ["core/params", "test/pa", "test/pb"];

#[test]
fn codes_follow_the_stock_count_in_canonical_order() {
    for file in [DT2_117, DT2_116] {
        let lib = two_params();
        let (Some(x), Some(y)) = (
            run(file, &lib, &["test/pb", "core/params", "test/pa"]),
            run(file, &lib, &SET),
        ) else {
            continue;
        };
        let (x, y) = (x.unwrap(), y.unwrap());
        assert!(x.syx == y.syx, "{file}: selection order changed the bytes");
        assert_eq!(
            x.plan.params,
            vec![
                ("pa_x".to_string(), 275, "test/pa".to_string()),
                ("pb_y".to_string(), 276, "test/pb".to_string()),
                ("pb_z".to_string(), 277, "test/pb".to_string()),
            ],
            "{file}"
        );
    }
}

#[test]
fn the_table_is_relocated_with_the_new_rows_and_every_reference_follows() {
    for file in [DT2_117, DT2_116] {
        let lib = two_params();
        let Some(r) = run(file, &lib, &SET) else {
            continue;
        };
        let a = r.unwrap();
        let raw = image(file).unwrap();
        let stock = Firmware::parse(&raw).unwrap();
        let reg = registry();
        let port = reg.find(&stock.syx_sha256).unwrap();
        let t1 = port.anchor("cf.params.t1").unwrap();
        let (old, count) = (t1.addr as u32, (t1.len / 60) as u32);
        let m = Main::new(&a.syx, stock_len(file));
        let new = symbol(&a, "parfw_t1");
        // the stock rows are copied byte for byte
        let sm = &stock.section(3).unwrap().data;
        let off = (old - 0x4000_0400) as usize;
        assert_eq!(
            m.read(new, count as usize * 60),
            sm[off..off + count as usize * 60],
            "{file}"
        );
        // the new rows: template's owner and flag, our mirror (slot 1 -> 26, 2 -> 27, 4 -> 31), range and default
        let row = |code: u32| m.u32s(new + code * 60, 15);
        let (x, y, z) = (row(275), row(276), row(277));
        assert_eq!(&x[0..5], &[0, 26, 0, 32512, 16384], "{file}");
        assert_eq!(&y[1..5], &[27, 0, 32512, 16384]);
        assert_eq!(&z[1..5], &[31, 0, 32512, 16384]);
        assert_eq!(x[7], 0x81, "kind is 0x80 + slot");
        assert_eq!(x[8..10], row(0xd1)[8..10], "the template's words are kept");
        assert_eq!(m.cstr(x[10]), "Xray");
        assert_eq!(m.cstr(x[12]), "XRAY");
        assert_eq!(x[13], row(0xd1)[13], "the template's formatter");
        assert_eq!(m.cstr(z[10]), "Zulu");
        assert_eq!(z[13], row(0xcc)[13]);
        // no reference to the stock table is left in the code, and every site the port counts was changed
        let (p, base) = (&a.syx, 0x4000_0400u32);
        let patched = Firmware::parse(p).unwrap();
        let pm = &patched.section(3).unwrap().data;
        let sites = patchkit::params::scan(sm, base as u64, old as u64, count);
        assert_eq!(
            sites.len() as u64,
            port.anchor("cf.params.sites").unwrap().len
        );
        let left = patchkit::params::scan(pm, base as u64, old as u64, count);
        assert!(
            left.refs.is_empty(),
            "{file}: {} references to the stock table remain",
            left.refs.len()
        );
        for r in &sites.refs {
            let o = (*r as u32 - base) as usize;
            let was = u32::from_be_bytes(sm[o..o + 4].try_into().unwrap());
            let now = u32::from_be_bytes(pm[o..o + 4].try_into().unwrap());
            assert_eq!(now, new + (was - old), "{file}: reference at {r:#x}");
        }
        // clamp bounds: raised by 3, except the one in the second table's accessor
        let t2 = port.anchor("cf.params.t2").unwrap();
        let mut raised = 0;
        for (a, imm) in &sites.bounds {
            let o = (*a as u32 - base) as usize + 2;
            let now = u32::from_be_bytes(pm[o..o + 4].try_into().unwrap());
            if *a >= t2.addr && *a < t2.addr + t2.len {
                continue;
            }
            assert_eq!(now, imm + 3, "{file}: bound at {a:#x}");
            raised += 1;
        }
        assert_eq!(raised, 54, "{file}: 55 bounds, one in the accessor");
    }
}

#[test]
fn the_second_table_accessor_shim_is_in_the_cave_and_decodes() {
    let lib = two_params();
    let Some(r) = run(DT2_117, &lib, &SET) else {
        return;
    };
    let a = r.unwrap();
    let m = Main::new(&a.syx, stock_len(DT2_117));
    let shim = symbol(&a, "parfw_t2_shim");
    let map = symbol(&a, "parfw_t2map");
    assert_eq!(
        m.u32s(map, 3),
        vec![0xd1, 0xd1, 0xcc],
        "added codes answer with their template's object"
    );
    let mut n = 0;
    for w in
        a.plan.writes.iter().filter(|w| {
            w.kind == "place" && w.owner.starts_with("core/params") && !w.code.is_empty()
        })
    {
        for &(lo, hi) in &w.code {
            let bytes = m.read(lo as u32, (hi - lo) as usize);
            let mut at = lo;
            while at < hi {
                let i = coldfire::decode_at(&bytes, lo as u32, (at - lo) as usize)
                    .unwrap_or_else(|| panic!("{at:#x}: does not decode"));
                at += (i.len as u64).max(2);
                n += 1;
            }
            assert_eq!(at, hi);
            assert!(lo <= shim as u64 && (shim as u64) < hi);
        }
    }
    assert!(n >= 10, "only {n} instructions");
}

#[test]
fn refusals() {
    let lib = library(vec![
        param_module("test/orphan", "[]", &param("o", "Orphan", 1, 0xd1)),
        param_module("test/dup", "[\"core/params\"]", &param("pa_x", "Again", 1, 0xd1)),
        param_module("test/bad", "[\"core/params\"]", &param("p", "Bad", 9, 0xd1)),
        param_module("test/badtpl", "[\"core/params\"]", &param("p", "Bad", 1, 999)),
        machine_module(
            "test/ghost",
            "[\"core/machines\", \"core/params\"]",
            "[[machine]]\nid = \"ghost\"\nname = \"Ghost\"\nshort = \"GHO\"\nclone_of = 0\nfields = [202, \"nope\", 0, 0, 0, 0, 0, 209, 10]\n",
        ),
    ])
    .clone();
    let mut lib = lib;
    lib.modules.extend(
        two_params()
            .modules
            .into_iter()
            .filter(|m| m.id() == "test/pa"),
    );
    let err = |ids: &[&str]| run(DT2_117, &lib, ids).map(err_of);
    let Some(e) = err(&["test/orphan"]) else {
        return;
    };
    assert!(e.contains("core/params is not selected"), "{e}");
    let e = err(&["core/params", "test/pa", "test/dup"]).unwrap();
    assert!(e.contains("declared by both"), "{e}");
    let e = err(&["core/params", "test/bad"]).unwrap();
    assert!(e.contains("slot 9"), "{e}");
    let e = err(&["core/params", "test/badtpl"]).unwrap();
    assert!(e.contains("template 999"), "{e}");
    let e = err(&["core/machines", "core/params", "test/ghost"]).unwrap();
    assert!(e.contains("not a parameter of any selected module"), "{e}");
    // Digitone II has no parameter framework
    let e = run(DN2_112, &lib, &["core/params", "test/pa"])
        .map(err_of)
        .unwrap();
    assert!(e.contains("not compatible"), "{e}");
}

#[test]
fn a_machine_names_its_parameters_and_the_descriptor_gets_their_codes() {
    let lib = library(vec![param_module(
        "test/pm",
        "[\"core/machines\", \"core/params\"]",
        &(param("pm_a", "Alpha", 1, 0xd1)
            + &param("pm_b", "Beta", 4, 0xd1)
            + "[[machine]]\nid = \"pmach\"\nname = \"Pm\"\nshort = \"PM\"\nclone_of = 0\nfields = [202, \"pm_a\", 0, 0, \"pm_b\", 0, 0, 209, 10]\n"),
    )]);
    let Some(r) = run(DT2_117, &lib, &["core/machines", "core/params", "test/pm"]) else {
        return;
    };
    let a = r.unwrap();
    let m = Main::new(&a.syx, stock_len(DT2_117));
    let desc = m.u32s(symbol(&a, "machfw_desc"), 11);
    assert_eq!(&desc[2..], &[202, 275, 0, 0, 276, 0, 0, 209, 10]);
    // the type-argument shim answers with the descriptor's own field for the engine slots (the table is 10 words:
    // field byte offsets 8 + 4 n for mirror indexes 25 .. 34, -1 where no field)
    let slots = m.u32s(symbol(&a, "machfw_slotmap"), 10);
    assert_eq!(slots, vec![8, 12, 16, 20, !0, !0, 24, 28, 32, 36]);
}
