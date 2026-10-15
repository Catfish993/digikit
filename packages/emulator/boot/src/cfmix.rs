//! Exact executed-instruction mix (feature `cf-mix`, off by default): per PC, how many times the
//! instruction ran, split by execution path (interpreted, fused loop, analytic idle skip, RAM-clear
//! batch), plus taken counts and an estimate of Bcc mispredictions from a 128-entry 2-bit table.
//! It also counts operand references by address region. Used by tools/coldfire/cf_timing.py; the
//! default build has no code from this file.

use std::cell::RefCell;
use std::collections::HashMap;

use coldfire::{Ea, Insn, Operand};
use serde_json::{Value, json};

pub const PATHS: [&str; 4] = ["interp", "fused", "idle", "ram_clear"];
/// Region names for operand references: DDR window, SRAM/high window, peripheral, other.
pub const REGIONS: [&str; 4] = ["ddr", "high_ram", "peripheral", "other"];

pub struct Entry {
    pub insn: Option<Insn>,
    pub n: [u64; 4],
    pub taken: u64,
    pub miss: u64,
}

pub struct Mix {
    pub pcs: HashMap<u32, Entry>,
    /// Bcc 2-bit counters (0,1 not taken; 2,3 taken), indexed by (pc >> 1) & 127.
    table: [u8; 128],
    /// Operand references: [region][0 read, 1 write].
    pub data: [[u64; 2]; 4],
    /// Operand references per 64 KiB page (addr >> 16): [read, write].
    pub pages: HashMap<u32, [u64; 2]>,
}

impl Default for Mix {
    fn default() -> Self {
        Mix {
            pcs: HashMap::new(),
            table: [1; 128],
            data: [[0; 2]; 4],
            pages: HashMap::new(),
        }
    }
}

thread_local! {
    static MIX: RefCell<Mix> = RefCell::new(Mix::default());
}

pub fn reset() {
    MIX.with(|m| *m.borrow_mut() = Mix::default());
}

pub fn seen(pc: u32) -> bool {
    MIX.with(|m| m.borrow().pcs.contains_key(&pc))
}

pub fn region(addr: u32) -> usize {
    match addr {
        0x4000_0000..=0x7fff_ffff => 0,
        0x8000_0000..=0x8bff_ffff => 1,
        0xe000_0000..=0xffff_ffff => 2,
        _ => 3,
    }
}

pub fn data_ref(addr: u32, write: bool) {
    MIX.with(|m| {
        let mut m = m.borrow_mut();
        m.data[region(addr)][usize::from(write)] += 1;
        m.pages.entry(addr >> 16).or_default()[usize::from(write)] += 1;
    });
}

/// Count `n` runs of the instruction at `pc` on `path`. `insn` is decoded on first sight; `taken`
/// is how many of the runs left the PC at a different place than the next instruction (given
/// directly for batched paths, from `next` for one run).
pub fn add(
    pc: u32,
    path: usize,
    n: u64,
    next: Option<u32>,
    taken: u64,
    insn: impl FnOnce() -> Option<Insn>,
) {
    MIX.with(|m| {
        let mut m = m.borrow_mut();
        let m = &mut *m;
        let e = m.pcs.entry(pc).or_insert_with(|| Entry {
            insn: insn(),
            n: [0; 4],
            taken: 0,
            miss: 0,
        });
        // `next`: the PC after one run; a different place than the next instruction is a taken flow.
        let taken = match (next, e.insn) {
            (Some(next), Some(i)) => u64::from(next != pc.wrapping_add(u32::from(i.len))),
            _ => taken,
        };
        e.n[path] += n;
        e.taken += taken;
        if path == 0 && n == 1 && e.insn.is_some_and(|i| i.id() == "bcc") {
            let c = &mut m.table[((pc >> 1) & 127) as usize];
            let predicted_taken = *c >= 2;
            if predicted_taken != (taken == 1) {
                e.miss += 1;
            }
            *c = if taken == 1 {
                (*c + 1).min(3)
            } else {
                c.saturating_sub(1)
            };
        }
    });
}

/// "mode:reg" for register modes, "idx:an:xn:scale", "pcidx:xn:scale"; xn 0-7 is Dn, 8-15 is An.
fn ea_name(e: &Ea) -> Value {
    match e {
        Ea::Dn(r) => json!(format!("dn:{r}")),
        Ea::An(r) => json!(format!("an:{r}")),
        Ea::Ind(r) => json!(format!("ind:{r}")),
        Ea::Post(r) => json!(format!("post:{r}")),
        Ea::Pre(r) => json!(format!("pre:{r}")),
        Ea::Disp(r, _) => json!(format!("disp:{r}")),
        Ea::Idx { scale, xn, an, .. } => json!(format!("idx:{an}:{xn}:{}", 1u32 << scale)),
        Ea::AbsW(a) => json!(format!("absw:{}", *a as i32 as u32)),
        Ea::AbsL(a) => json!(format!("absl:{a}")),
        Ea::PcDisp { .. } => json!("pcdisp"),
        Ea::PcIdx { scale, xn, .. } => json!(format!("pcidx:{xn}:{}", 1u32 << scale)),
        Ea::Imm(_) => json!("imm"),
    }
}

fn op_name(o: &Operand) -> Value {
    match o {
        Operand::None => json!("none"),
        Operand::Ea(e) => ea_name(e),
        Operand::Imm(v) => json!({"m": "q", "v": v}),
        Operand::Target(t) => json!({"m": "target", "t": t}),
        Operand::RegList(m) => json!({"m": "list", "n": m.count_ones()}),
        Operand::Ctrl(c) => json!({"m": "ctrl", "c": c}),
        Operand::Sr => json!("sr"),
        Operand::Ccr => json!("ccr"),
        Operand::Usp => json!("usp"),
        Operand::Macsr => json!("macsr"),
        Operand::Mask => json!("mask"),
        Operand::Acc(_) => json!("acc"),
        Operand::AccExt01 | Operand::AccExt23 => json!("accext"),
        _ => json!("other"),
    }
}

pub fn report() -> Value {
    MIX.with(|m| {
        let m = m.borrow();
        let mut rows: Vec<(&u32, &Entry)> = m.pcs.iter().collect();
        rows.sort_by_key(|r| *r.0);
        let insns: Vec<Value> = rows
            .into_iter()
            .map(|(pc, e)| match e.insn {
                Some(i) => json!({
                    "pc": pc, "id": i.id(), "mn": i.mnemonic(), "size": i.size.suffix(), "len": i.len,
                    "flow": i.flow(), "ops": i.operands().iter().map(op_name).collect::<Vec<_>>(),
                    "n": e.n, "taken": e.taken, "miss": e.miss,
                }),
                None => json!({"pc": pc, "id": null, "n": e.n, "taken": e.taken, "miss": e.miss}),
            })
            .collect();
        json!({
            "paths": PATHS, "regions": REGIONS,
            "data_refs": m.data,
            "pages": m.pages.iter().map(|(p, c)| json!([p, c[0], c[1]])).collect::<Vec<_>>(),
            "insns": insns,
        })
    })
}
