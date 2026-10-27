extern crate std;

use super::*;
use std::format;
use std::string::String;
use std::{vec, vec::Vec};

/// A minimal JSON reader (the crate has no JSON dependency): objects, arrays, strings, unsigned numbers, null.
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
        assert_eq!(
            self.b[self.at], c,
            "json: expected {} at {}",
            c as char, self.at
        );
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
                Value::Num(
                    core::str::from_utf8(&self.b[start..self.at])
                        .unwrap()
                        .parse()
                        .unwrap(),
                )
            }
        }
    }
}

fn vectors() -> Value {
    let text = include_str!("../../../../../sdk/vectors/fit-vectors.json");
    Parser {
        b: text.as_bytes(),
        at: 0,
    }
    .value()
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
        if v.is_null() {
            continue; // the kind is left out
        }
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
    [
        "MODULES",
        "SELECTION",
        "ARENA",
        "STAGE",
        "DSP_AREA",
        "MACHINE_ROWS",
        "VOICE_ROWS",
        "PARAM_ROWS",
        "PAGE_ROWS",
        "FX_SLOTS",
    ]
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
                let w = want
                    .as_array()
                    .unwrap_or_else(|| panic!("{}: short {s:?}", c["name"]));
                assert_eq!(
                    (short_name(&s), s.need as u64, s.free as u64),
                    (
                        w[0].as_str().unwrap(),
                        w[1].as_u64().unwrap(),
                        w[2].as_u64().unwrap()
                    ),
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
                let w = want
                    .as_array()
                    .unwrap_or_else(|| panic!("{}: short {short:?}", s["name"]));
                assert_eq!(
                    (i, short_name(&short), short.need as u64, short.free as u64),
                    (
                        w[0].as_u64().unwrap(),
                        w[1].as_str().unwrap(),
                        w[2].as_u64().unwrap(),
                        w[3].as_u64().unwrap()
                    ),
                    "{}",
                    s["name"]
                );
            }
        }
    }
}

#[test]
fn take_leaves_the_capacity_it_says() {
    let v = vectors();
    let mut c = caps(&v["caps_base"], None);
    let n = Needs {
        arena: 32,
        arena_xfer: 1392,
        dsp_area: 265600,
        dsp_align: 64,
        voice_rows: 1,
        ..Needs::default()
    };
    take(&n, &mut c);
    let get = |k: u8| *c.iter().find(|e| e.kind == k as u32).unwrap();
    assert_eq!(
        (get(CAPK_ARENA).used, get(CAPK_ARENA).largest),
        (1424, 524272 - 1424)
    );
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
        assert_eq!(
            (a.kind, a.limit, a.used, a.largest),
            (b.kind, b.limit, b.used, b.largest)
        );
    }
}

/// A capacity from (kind, limit, used) triples.
fn cap_of(entries: &[(u8, u32, u32)]) -> Vec<DkrtCap> {
    entries
        .iter()
        .map(|&(k, limit, used)| DkrtCap {
            kind: k as u32,
            limit,
            used,
            largest: limit.saturating_sub(used),
        })
        .collect()
}

fn line(caps: &[DkrtCap]) -> String {
    let mut out = [0u8; LINE_MAX];
    let n = load_line(caps, &mut out);
    String::from_utf8(out[..n].to_vec()).unwrap()
}

#[test]
fn the_load_line_on_the_vectors() {
    let v = vectors();
    for case in v["load_lines"].as_array().unwrap() {
        let c = caps(&case["caps"], None);
        assert_eq!(
            line(&c),
            case["want"].as_str().unwrap(),
            "{}",
            case["name"].as_str().unwrap()
        );
        assert!(line(&c).len() <= LINE_MAX);
    }
}

#[test]
fn the_load_line_of_a_selection_is_the_empty_capacity_with_the_ticked_modules_taken() {
    // the same path as the menu's `refit`: limits, nothing used, then each ticked module taken in turn
    let v = vectors();
    let mut c = caps(&v["caps_base"], None);
    empty(&mut c);
    assert!(!line(&c).is_empty());
    let n = Needs {
        arena: 32,
        arena_xfer: 1392,
        dsp_area: 265600,
        dsp_align: 64,
        voice_rows: 1,
        machine_rows: 1,
        ..Needs::default()
    };
    take(&n, &mut c);
    let dsp = c.iter().find(|e| e.kind == CAPK_DSP_AREA as u32).unwrap();
    // 265656 of the limit, rounded up
    let want = (dsp.used * 100).div_ceil(dsp.limit);
    assert!(
        line(&c).starts_with(&format!("DSP {want}%")),
        "{}",
        line(&c)
    );
}
