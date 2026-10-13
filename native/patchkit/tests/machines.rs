//! `core/machines` over the real ports and stock images (skipped when the images are absent): what the framework
//! writes for one and for two added machine types, canonical type numbers, and the refusals.

use patchkit::apply::{self, Applied, Options};
use patchkit::dsp::Stream;
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

fn machine(
    id: &str,
    name: &str,
    clone_of: u32,
    position: Option<usize>,
    selector: Option<u32>,
) -> String {
    let mut s = format!(
        "[[machine]]\nid = \"{id}\"\nname = \"{name}\"\nshort = \"{}\"\nclone_of = {clone_of}\nfields = [248, 249, 0, 251, 252, 253, 0, 254, 10]\n",
        &name[..3.min(name.len())]
    );
    if let Some(p) = position {
        s += &format!("position = {p}\n");
    }
    if let Some(v) = selector {
        s += &format!("selector = {v}\n");
    }
    s
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

fn two() -> Library {
    library(vec![
        machine_module(
            "test/a",
            "[\"core/machines\"]",
            &machine("amach", "Aaa", 6, Some(7), None),
        ),
        machine_module(
            "test/b",
            "[\"core/machines\"]",
            &machine("bmach", "Bbb", 6, None, Some(9)),
        ),
    ])
}

#[test]
fn xslice_gets_type_7_and_the_tables_grow() {
    for file in [DT2_117, DT2_116] {
        let lib = library(vec![]);
        let Some(r) = run(file, &lib, &["core/machines", "machines/xslice"]) else {
            continue;
        };
        let a = r.unwrap();
        assert_eq!(
            a.plan.machines,
            vec![("xslice".to_string(), 7, "machines/xslice".to_string())],
            "{file}"
        );
        let m = Main::new(&a.syx, stock_len(file));
        // list: the stock order (0 1 2 3 6 4 5) with the new type at position 7
        assert_eq!(
            m.u32s(symbol(&a, "machfw_list"), 8),
            vec![0, 1, 2, 3, 6, 4, 5, 7],
            "{file}"
        );
        // rank pairs (type, position) in the same order
        let rank = m.u32s(symbol(&a, "machfw_rank_table"), 16);
        let pairs: Vec<(u32, u32)> = rank.chunks(2).map(|c| (c[0], c[1])).collect();
        assert_eq!(
            pairs,
            vec![
                (0, 0),
                (1, 1),
                (2, 2),
                (3, 3),
                (6, 4),
                (4, 5),
                (5, 6),
                (7, 7)
            ]
        );
        // display names: 8 rows of (long, short, hint); the new row's strings are ours, its hint SLICE's
        let names = m.u32s(symbol(&a, "machfw_name_table"), 24);
        assert_eq!(m.cstr(names[21]), "XSlice");
        assert_eq!(m.cstr(names[22]), "XSL");
        assert_eq!(names[23], names[6 * 3 + 2]);
        // permission masks and per-type bytes: the new row is SLICE's
        let permit = m.u32s(symbol(&a, "machfw_permit"), 8);
        assert_eq!(permit[7], permit[6]);
        let pertype = m.read(symbol(&a, "machfw_pertype"), 8);
        assert_eq!(pertype[7], pertype[6]);
        // descriptor: two name pointers and the nine ID fields the manifest chose
        let desc = m.u32s(symbol(&a, "machfw_desc"), 11);
        assert_eq!(m.cstr(desc[0]), "XSLICE");
        assert_eq!(m.cstr(desc[1]), "XSL");
        assert_eq!(&desc[2..], &[248, 249, 250, 251, 252, 253, 0, 254, 10]);
    }
}

#[test]
fn the_dsp_selector_table_is_relocated_with_the_clone_s_word() {
    let lib = library(vec![]);
    let Some(r) = run(DT2_117, &lib, &["core/machines", "machines/xslice"]) else {
        return;
    };
    let a = r.unwrap();
    let fw = Firmware::parse(&a.syx).unwrap();
    let stream = Stream::parse(fw.section(7).unwrap().data.clone()).unwrap();
    let words: Vec<u32> = stream
        .read(symbol(&a, "machfw_selector"), 32)
        .unwrap()
        .chunks(4)
        .map(|c| u32::from_le_bytes(c.try_into().unwrap()))
        .collect();
    // the stock table is 0 1 2 3 4 0 5; the new type copies MANUAL SLICE's selector (5)
    assert_eq!(&words[..8], &[0, 1, 2, 3, 4, 0, 5, 5]);
}

#[test]
fn two_modules_get_types_7_and_8_in_any_selection_order() {
    let lib = two();
    let (Some(x), Some(y)) = (
        run(DT2_117, &lib, &["test/b", "core/machines", "test/a"]),
        run(DT2_117, &lib, &["core/machines", "test/a", "test/b"]),
    ) else {
        return;
    };
    let (x, y) = (x.unwrap(), y.unwrap());
    assert!(x.syx == y.syx, "selection order changed the bytes");
    assert_eq!(
        x.plan.machines,
        vec![
            ("amach".to_string(), 7, "test/a".to_string()),
            ("bmach".to_string(), 8, "test/b".to_string())
        ]
    );
    let m = Main::new(&x.syx, stock_len(DT2_117));
    assert_eq!(
        m.u32s(symbol(&x, "machfw_list"), 9),
        vec![0, 1, 2, 3, 6, 4, 5, 7, 8]
    );
    // the per-type tables grew by one row each
    assert_eq!(
        m.u32s(symbol(&x, "machfw_permit"), 9)[8],
        m.u32s(symbol(&x, "machfw_permit"), 9)[6]
    );
    // selector: bmach asks for its own word (9), amach copies SLICE's
    let fw = Firmware::parse(&x.syx).unwrap();
    let stream = Stream::parse(fw.section(7).unwrap().data.clone()).unwrap();
    let w: Vec<u32> = stream
        .read(symbol(&x, "machfw_selector"), 36)
        .unwrap()
        .chunks(4)
        .map(|c| u32::from_le_bytes(c.try_into().unwrap()))
        .collect();
    assert_eq!(&w[..9], &[0, 1, 2, 3, 4, 0, 5, 5, 9]);
}

#[test]
fn every_instruction_the_framework_places_decodes() {
    let lib = two();
    let Some(r) = run(DT2_117, &lib, &["core/machines", "test/a", "test/b"]) else {
        return;
    };
    let a = r.unwrap();
    let m = Main::new(&a.syx, stock_len(DT2_117));
    let mut n = 0;
    for w in
        a.plan.writes.iter().filter(|w| {
            w.kind == "place" && w.owner.starts_with("core/machines") && !w.code.is_empty()
        })
    {
        for &(lo, hi) in &w.code {
            let mut at = lo;
            let bytes = m.read(lo as u32, (hi - lo) as usize);
            while at < hi {
                let i = coldfire::decode_at(&bytes, lo as u32, (at - lo) as usize)
                    .unwrap_or_else(|| panic!("{at:#x}: does not decode"));
                at += (i.len as u64).max(2);
                n += 1;
            }
            assert_eq!(at, hi, "the code ends mid-instruction");
        }
    }
    assert!(n > 40, "only {n} instructions");
}

#[test]
fn the_bounds_are_raised_by_the_number_of_added_types() {
    let lib = two();
    let Some(r) = run(DT2_117, &lib, &["core/machines", "test/a", "test/b"]) else {
        return;
    };
    let a = r.unwrap();
    let raw = image(DT2_117).unwrap();
    let stock = Firmware::parse(&raw).unwrap();
    let patched = Firmware::parse(&a.syx).unwrap();
    let (s, p) = (
        &stock.section(3).unwrap().data,
        &patched.section(3).unwrap().data,
    );
    let base = 0x4000_0400u32;
    let port = registry();
    let port = port.find(&stock.syx_sha256).unwrap();
    let at = |name: &str, off: u64| (port.anchor(name).unwrap().addr + off - base as u64) as usize;
    // group: moveq #6,D1 -> moveq #8,D1 (bne.b -> bcs.b)
    let g = at("cf.machines.group", 0);
    assert_eq!((s[g + 1], p[g + 1]), (6, 8));
    assert_eq!((s[g + 4], p[g + 4]), (0x66, 0x65));
    // the sound loader keeps types while (u8)(type + 1) < 8 + N
    let k = at("cf.machines.persist", 1);
    assert_eq!((s[k], p[k]), (8, 10));
    // the list builder allocates room for the longer list
    let l = at("cf.machines.list_alloc", 2);
    assert_eq!((&s[l..l + 2], &p[l..l + 2]), (&[0, 28][..], &[0, 36][..]));
}

#[test]
fn refusals() {
    // no framework selected
    let lib = library(vec![]);
    if let Some(r) = run(DT2_117, &lib, &["machines/xslice"]) {
        assert!(err_of(r).contains("requires core/machines"));
    }
    // the same machine id twice
    let lib = library(vec![
        machine_module(
            "test/a",
            "[\"core/machines\"]",
            &machine("same", "Aaa", 6, None, None),
        ),
        machine_module(
            "test/b",
            "[\"core/machines\"]",
            &machine("same", "Bbb", 6, None, None),
        ),
    ]);
    if let Some(r) = run(DT2_117, &lib, &["core/machines", "test/a", "test/b"]) {
        assert!(err_of(r).contains("machine id same"));
    }
    // a clone of a type that does not exist
    let lib = library(vec![machine_module(
        "test/a",
        "[\"core/machines\"]",
        &machine("far", "Far", 9, None, None),
    )]);
    if let Some(r) = run(DT2_117, &lib, &["core/machines", "test/a"]) {
        assert!(err_of(r).contains("clone_of 9"));
    }
    // a Digitakt II clone (of type 6) is not a Digitone II type: it has five stock machines
    let lib = library(vec![]);
    if let Some(r) = run(DN2_112, &lib, &["core/machines", "machines/xslice"]) {
        assert!(err_of(r).contains("clone_of 6"));
    }
}

fn dn2_clone(id: &str, name: &str, position: Option<usize>, own_dsp: bool) -> String {
    let mut s = format!(
        "[[machine]]\nid = \"{id}\"\nname = \"{name}\"\nshort = \"{}\"\nclone_of = 0\nfields = [0, 0, 0, 0, 0, 0, 0, 0, 10]\n",
        &name[..3.min(name.len())]
    );
    if let Some(p) = position {
        s += &format!("position = {p}\n");
    }
    if own_dsp {
        s += "own_dsp = true\n";
    }
    s
}

#[test]
fn digitone_ii_clone_gets_type_5_and_the_tables_grow() {
    let lib = library(vec![machine_module(
        "test/a",
        "[\"core/machines\"]",
        &dn2_clone("fmc", "FM Clone", Some(4), false),
    )]);
    let Some(r) = run(DN2_112, &lib, &["core/machines", "test/a"]) else {
        return;
    };
    let a = r.unwrap();
    assert_eq!(
        a.plan.machines,
        vec![("fmc".to_string(), 5, "test/a".to_string())]
    );
    let m = Main::new(&a.syx, stock_len(DN2_112));
    // the list: the stock order (0 1 2 3 4) with the new type before MIDI
    let list = m.u32s(symbol(&a, "machfw_list"), 6);
    assert_eq!(list.len(), 6);
    let mut sorted = list.clone();
    sorted.sort();
    assert_eq!(sorted, vec![0, 1, 2, 3, 4, 5]);
    assert_eq!(list[4], 5);
    // the name table: five stock rows then the clone's, in the shifted {name, abbrev, hint} layout of the accessors
    let rows = m.u32s(symbol(&a, "machfw_name_table"), 18);
    assert_eq!(m.cstr(rows[15]), "FM Clone");
    assert_eq!(m.cstr(rows[16]), "FM ");
    // the persistence immediate: `moveq #6,D2` became `moveq #7,D2`
    let w = a
        .plan
        .writes
        .iter()
        .find(|w| w.what.contains("persistence bound"))
        .unwrap();
    assert_eq!(w.new_hex.as_deref(), Some("07"));
}

#[test]
fn digitone_ii_own_dsp_machines_skip_the_frame_type_map() {
    let lib = library(vec![
        machine_module(
            "test/a",
            "[\"core/machines\"]",
            &dn2_clone("fma", "FM A", None, false),
        ),
        machine_module(
            "test/b",
            "[\"core/machines\"]",
            &dn2_clone("fmb", "FM B", None, true),
        ),
    ]);
    let Some(r) = run(DN2_112, &lib, &["core/machines", "test/a", "test/b"]) else {
        return;
    };
    let a = r.unwrap();
    assert_eq!(a.plan.machines.len(), 2);
    assert_eq!((a.plan.machines[0].1, a.plan.machines[1].1), (5, 6));
    // the frame shim maps type 5 only: one `cmpi.b #5,D0b` and no `cmpi.b #6,D0b`
    let m = Main::new(&a.syx, stock_len(DN2_112));
    let shim = m.read(symbol(&a, "machfw_frametype"), 24);
    assert_eq!(&shim[0..4], &[0x10, 0x28, 0x0d, 0x8c]);
    assert_eq!(&shim[4..8], &[0x0c, 0x00, 0x00, 0x05]);
    assert_eq!(&shim[8..10], &[0x66, 0x04]);
    assert_eq!(&shim[14..16], &[0x48, 0x80]); // ext.w D0w follows directly
}

#[test]
fn digitone_ii_1_11_resolves_the_same_sites() {
    let lib = library(vec![machine_module(
        "test/a",
        "[\"core/machines\"]",
        &dn2_clone("fmc", "FM Clone", None, false),
    )]);
    let Some(raw) = image("Digitone_II_OS1.11.syx") else {
        return;
    };
    let ids = vec!["core/machines".to_string(), "test/a".to_string()];
    let r = apply::apply_with(&raw, &registry(), &lib, &ids, Options::default());
    assert!(r.is_ok(), "{:?}", r.err());
}
