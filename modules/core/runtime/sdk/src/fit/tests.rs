extern crate std;

use super::*;
use std::string::String;
use std::{vec, vec::Vec};

/// A minimal JSON reader (the SDK has no dependency, and its tests are also built by plain rustc): objects, arrays, strings, unsigned numbers, null.
#[derive(Debug)]
enum Value {
    Null,
    Num(u64),
    Str(String),
    Arr(Vec<Value>),
    Obj(Vec<(String, Value)>),
}

static NULL: Value = Value::Null;

impl Value {
    fn get(&self, k: &str) -> Option<&Value> {
        match self {
            Value::Obj(o) => o.iter().find(|(n, _)| n == k).map(|(_, v)| v),
            _ => None,
        }
    }
    fn as_u64(&self) -> Option<u64> {
        match self {
            Value::Num(n) => Some(*n),
            _ => None,
        }
    }
    fn as_str(&self) -> Option<&str> {
        match self {
            Value::Str(s) => Some(s),
            _ => None,
        }
    }
    fn as_array(&self) -> Option<&Vec<Value>> {
        match self {
            Value::Arr(a) => Some(a),
            _ => None,
        }
    }
    fn as_object(&self) -> Option<&Vec<(String, Value)>> {
        match self {
            Value::Obj(o) => Some(o),
            _ => None,
        }
    }
    fn is_null(&self) -> bool {
        matches!(self, Value::Null)
    }
}

impl core::ops::Index<&str> for Value {
    type Output = Value;
    fn index(&self, k: &str) -> &Value {
        self.get(k).unwrap_or(&NULL)
    }
}

impl core::fmt::Display for Value {
    fn fmt(&self, f: &mut core::fmt::Formatter) -> core::fmt::Result {
        write!(f, "{self:?}")
    }
}

struct Parser<'a> {
    b: &'a [u8],
    at: usize,
}

impl Parser<'_> {
    fn ws(&mut self) {
        while self.at < self.b.len() && self.b[self.at].is_ascii_whitespace() {
            self.at += 1;
        }
    }
    fn eat(&mut self, c: u8) {
        self.ws();
        assert_eq!(self.b[self.at], c, "json: expected {} at {}", c as char, self.at);
        self.at += 1;
    }
    fn string(&mut self) -> String {
        self.eat(b'"');
        let start = self.at;
        while self.b[self.at] != b'"' {
            assert_ne!(self.b[self.at], b'\\', "json: escapes are not read");
            self.at += 1;
        }
        let s = String::from_utf8(self.b[start..self.at].to_vec()).unwrap();
        self.at += 1;
        s
    }
    fn value(&mut self) -> Value {
        self.ws();
        match self.b[self.at] {
            b'{' => {
                self.at += 1;
                let mut o = vec![];
                loop {
                    self.ws();
                    if self.b[self.at] == b'}' {
                        self.at += 1;
                        return Value::Obj(o);
                    }
                    if !o.is_empty() {
                        self.eat(b',');
                    }
                    let k = self.string();
                    self.eat(b':');
                    o.push((k, self.value()));
                }
            }
            b'[' => {
                self.at += 1;
                let mut a = vec![];
                loop {
                    self.ws();
                    if self.b[self.at] == b']' {
                        self.at += 1;
                        return Value::Arr(a);
                    }
                    if !a.is_empty() {
                        self.eat(b',');
                    }
                    a.push(self.value());
                }
            }
            b'"' => Value::Str(self.string()),
            b'n' => {
                self.at += 4;
                Value::Null
            }
            _ => {
                let start = self.at;
                while self.at < self.b.len() && self.b[self.at].is_ascii_digit() {
                    self.at += 1;
                }
                Value::Num(core::str::from_utf8(&self.b[start..self.at]).unwrap().parse().unwrap())
            }
        }
    }
}


fn vectors() -> Value {
    let text = include_str!("../../../../../../sdk/fit-vectors.json");
    Parser { b: text.as_bytes(), at: 0 }.value()
}

/// kind name of `[capacity.kinds]` -> CAPK number
fn kind(name: &str) -> u8 {
    match name {
        "MODULES" => CAPK_MODULES,
        "SELECTION" => CAPK_SELECTION,
        "ARENA" => CAPK_ARENA,
        "STAGE" => CAPK_STAGE,
        "DSP_AREA" => CAPK_DSP_AREA,
        "MACHINE_ROWS" => CAPK_MACHINE_ROWS,
        "VOICE_ROWS" => CAPK_VOICE_ROWS,
        "PARAM_ROWS" => CAPK_PARAM_ROWS,
        "PAGE_ROWS" => CAPK_PAGE_ROWS,
        "FX_SLOTS" => CAPK_FX_SLOTS,
        _ => panic!("kind {name}"),
    }
}

fn caps(base: &Value, over: Option<&Value>) -> Vec<DkrtCap> {
    let mut out: Vec<DkrtCap> = vec![];
    for (name, v) in base.as_object().unwrap() {
        let v = over.and_then(|o| o.get(name)).unwrap_or(v);
        let a = v.as_array().unwrap();
        out.push(DkrtCap {
            kind: kind(name) as u32,
            limit: a[0].as_u64().unwrap() as u32,
            used: a[1].as_u64().unwrap() as u32,
            largest: a[2].as_u64().unwrap() as u32,
        });
    }
    out
}

fn needs(v: &Value) -> Needs {
    let g = |k: &str| v.get(k).and_then(Value::as_u64).unwrap_or(0) as u32;
    Needs {
        file_size: g("file_size"),
        arena: g("ARENA"),
        arena_xfer: g("ARENA_XFER"),
        dsp_area: g("DSP_AREA"),
        dsp_align: g("DSP_ALIGN"),
        machine_rows: g("MACHINE_ROWS"),
        voice_rows: g("VOICE_ROWS"),
        param_rows: g("PARAM_ROWS"),
        page_rows: g("PAGE_ROWS"),
        fx_slots: g("FX_SLOTS"),
    }
}

/// The vector's `want` as (resource name, need, free).
fn short_name(s: &Short) -> &'static str {
    ["MODULES", "SELECTION", "ARENA", "STAGE", "DSP_AREA", "MACHINE_ROWS", "VOICE_ROWS", "PARAM_ROWS", "PAGE_ROWS", "FX_SLOTS"]
        .into_iter()
        .find(|n| kind(n) == s.kind)
        .unwrap()
}

#[test]
fn fits_now_on_the_shared_vectors() {
    let v = vectors();
    for c in v["cases"].as_array().unwrap() {
        let got = fits_now(&needs(&c["needs"]), &caps(&v["caps_base"], c.get("caps")));
        let want = &c["want"];
        match got {
            None => assert!(want.is_null(), "{}: fits, want {want}", c["name"]),
            Some(s) => {
                let w = want.as_array().unwrap_or_else(|| panic!("{}: short {s:?}", c["name"]));
                assert_eq!(
                    (short_name(&s), s.need as u64, s.free as u64),
                    (w[0].as_str().unwrap(), w[1].as_u64().unwrap(), w[2].as_u64().unwrap()),
                    "{}",
                    c["name"]
                );
            }
        }
    }
}

#[test]
fn a_set_loads_its_modules_in_turn() {
    let v = vectors();
    for s in v["sets"].as_array().unwrap() {
        // an entry with `times` stands for that many modules
        let mut set: Vec<Needs> = vec![];
        for n in s["needs"].as_array().unwrap() {
            for _ in 0..n.get("times").and_then(Value::as_u64).unwrap_or(1) {
                set.push(needs(n));
            }
        }
        let mut left = caps(&v["caps_base"], s.get("caps"));
        let mut got = None;
        for (i, nd) in set.iter().enumerate() {
            if let Some(short) = fits_now(nd, &left) {
                got = Some((i as u64, short));
                break;
            }
            take(nd, &mut left);
        }
        let want = &s["want"];
        match got {
            None => assert!(want.is_null(), "{}", s["name"]),
            Some((i, short)) => {
                let w = want.as_array().unwrap_or_else(|| panic!("{}: short {short:?}", s["name"]));
                assert_eq!(
                    (i, short_name(&short), short.need as u64, short.free as u64),
                    (w[0].as_u64().unwrap(), w[1].as_str().unwrap(), w[2].as_u64().unwrap(), w[3].as_u64().unwrap()),
                    "{}",
                    s["name"]
                );
            }
        }
    }
}

#[test]
fn the_header_is_read_at_the_declared_offsets() {
    let mut h = [0u8; DKM_HEADER as usize];
    h[0..4].copy_from_slice(b"DKMF");
    h[4..8].copy_from_slice(&DKM_FORMAT.to_be_bytes());
    h[8..12].copy_from_slice(&700u32.to_be_bytes());
    h[12..16].copy_from_slice(&DKM_HEADER.to_be_bytes());
    for (i, at) in [
        DKM_NEED_ARENA,
        DKM_NEED_ARENA_XFER,
        DKM_NEED_DSP_AREA,
        DKM_NEED_DSP_ALIGN,
        DKM_NEED_MACHINE_ROWS,
        DKM_NEED_VOICE_ROWS,
        DKM_NEED_PARAM_ROWS,
        DKM_NEED_PAGE_ROWS,
        DKM_NEED_FX_SLOTS,
    ]
    .into_iter()
    .enumerate()
    {
        h[at..at + 4].copy_from_slice(&(100 + i as u32).to_be_bytes());
    }
    let n = Needs::from_header(&h).unwrap();
    assert_eq!(
        n,
        Needs {
            file_size: 700,
            arena: 100,
            arena_xfer: 101,
            dsp_area: 102,
            dsp_align: 103,
            machine_rows: 104,
            voice_rows: 105,
            param_rows: 106,
            page_rows: 107,
            fx_slots: 108,
        }
    );
    // another format, a short header or no magic is not read
    let mut old = h;
    old[4..8].copy_from_slice(&1u32.to_be_bytes());
    assert_eq!(Needs::from_header(&old), None);
    assert_eq!(Needs::from_header(&h[..0xa0]), None);
    h[0] = 0;
    assert_eq!(Needs::from_header(&h), None);
}

#[test]
fn take_leaves_the_capacity_it_says() {
    let v = vectors();
    let mut c = caps(&v["caps_base"], None);
    let n = Needs { arena: 32, arena_xfer: 1392, dsp_area: 265600, dsp_align: 64, voice_rows: 1, ..Needs::default() };
    take(&n, &mut c);
    let get = |k: u8| *c.iter().find(|e| e.kind == k as u32).unwrap();
    assert_eq!((get(CAPK_ARENA).used, get(CAPK_ARENA).largest), (1424, 524272 - 1424));
    assert_eq!(get(CAPK_DSP_AREA).used, 265656); // the alignment room is taken too
    assert_eq!((get(CAPK_MODULES).used, get(CAPK_VOICE_ROWS).used), (1, 1));
}

#[test]
fn empty_is_the_capacity_of_a_runtime_that_has_loaded_nothing() {
    let v = vectors();
    let mut c = caps(&v["empty"]["from"], None);
    empty(&mut c);
    let want = caps(&v["empty"]["want"], None);
    assert_eq!(c.len(), want.len());
    for (a, b) in c.iter().zip(&want) {
        assert_eq!((a.kind, a.limit, a.used, a.largest), (b.kind, b.limit, b.used, b.largest));
    }
}
