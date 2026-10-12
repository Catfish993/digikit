//! HLE: hand-written native implementations of hot SHARC+ DSP functions.
//!
//! An `HleFn` maps a function entry PC to a Rust routine. The engine tries it
//! where it would try the fast tier (`Engine::step`, same gates: models, loop
//! deferral, instruction budget), and it must leave the machine exactly as the
//! firmware would after the function returned, or decline with the state
//! untouched. Three guards stand between the firmware and the routine:
//!
//!  * a SHA-256 of the function's code words in DSP memory (patched firmware
//!    falls back; rechecked whenever a store touches the watched code pages);
//!  * the machine state the routine was written for (known registers, MODE1,
//!    no circular buffers, plain RAM windows, instruction budget);
//!  * the value domain: every float operand and result must be a finite normal
//!    or a zero, so no flag other than the plain ones can differ. A denormal,
//!    NaN, infinity, overflow or underflow declines (the normal path then runs
//!    the whole call).
//!
//! `SHARC_HLE=1` turns the table on (all functions) or `SHARC_HLE=name,...`
//! selects some. `SHARC_HLE_CHECK=N[,V]` runs the exactness checker at the
//! first N calls (and every 1/1000 later one up to a cap): it runs the
//! routine and the interpreter (the generated core, one instruction at a time)
//! from the same entry state, compares everything, and rolls both back, so the
//! checked run is unchanged. Each captured call is also checked V times with
//! synthetic inputs (zeros, denormals, NaN, infinity, extreme and random
//! values, other trip counts). `SHARC_HLE_LOG=1` prints the statistics at exit.

use crate::fast::glue::apply_flag;
use crate::fast::FlagKind;
use crate::generated::core_g::{sequencer, state};
use crate::rt::*;
use crate::{EXIT_NEXT, mem::Mem, sha256};
use std::collections::HashMap;

mod dn2_b82342;

/// Phase ticks of the routine (`SHARC_HLE_PROF`): windows, compute, commit.
pub(crate) static PROF: [std::sync::atomic::AtomicU64; 4] = [const { std::sync::atomic::AtomicU64::new(0) }; 4];

/// Diagnostic switches, read once (the routines are on the hot path).
pub(crate) struct Dbg {
    pub prof: bool,
    pub why: bool,
    pub trace: bool,
    pub maxn: Option<u32>,
}

pub(crate) fn dbg() -> &'static Dbg {
    static D: std::sync::OnceLock<Dbg> = std::sync::OnceLock::new();
    D.get_or_init(|| Dbg {
        prof: std::env::var_os("SHARC_HLE_PROF").is_some(),
        why: std::env::var_os("SHARC_HLE_WHY").is_some(),
        trace: std::env::var_os("SHARC_HLE_TRACE").is_some(),
        maxn: std::env::var("SHARC_HLE_MAXN").ok().and_then(|v| v.parse().ok()),
    })
}

/// A routine: Some(()) when it completed (state updated), None to decline.
pub type RunFn = fn(&mut St, &mut Hx) -> Option<()>;

/// Most stores one call of a routine makes (the undo log is a fixed array).
const MAX_STORES: usize = 256;

/// Per-call context: memory write undo log, optional write log.
pub struct Hx {
    undo: Box<[(*mut u8, u32); MAX_STORES]>,
    n: usize,
    /// (byte address, new word, old word) of every store, with the checker.
    pub log: Option<Vec<(u32, u32, u32)>>,
}

impl Hx {
    fn new() -> Hx {
        Hx {
            undo: Box::new([(std::ptr::null_mut(), 0); MAX_STORES]),
            n: 0,
            log: None,
        }
    }
    fn reset(&mut self) {
        self.n = 0;
    }
    fn rollback(&mut self) {
        while self.n > 0 {
            self.n -= 1;
            let (p, old) = self.undo[self.n];
            // SAFETY: p came from a resolved window and is good for 4 bytes.
            unsafe { (p as *mut u32).write_unaligned(old.to_le()) };
        }
        if let Some(l) = &mut self.log {
            l.clear();
        }
    }
}

/// A resolved memory window (`Mem::window`): the bytes [base, base+len).
#[derive(Clone, Copy)]
pub struct Win {
    pub base: u32,
    pub len: u32,
    pub p: *mut u8,
}

impl Win {
    pub fn new(mem: &mut Mem, base: u32, len: u32, write: bool) -> Option<Win> {
        let p = mem.window(base, len, true, write)?;
        Some(Win { base, len, p })
    }
    /// Checked read (None outside the window).
    #[inline(always)]
    pub fn rd(&self, a: u32) -> Option<u32> {
        let o = a.wrapping_sub(self.base);
        if o > self.len - 4 {
            if dbg().why {
                eprintln!("why: rd {a:#x} outside window {:#x}+{:#x}", self.base, self.len);
            }
            return None;
        }
        Some(self.rd_in(a))
    }
    /// Read of an address the caller has proved lies in the window.
    #[inline(always)]
    pub fn rd_in(&self, a: u32) -> u32 {
        let o = a.wrapping_sub(self.base);
        debug_assert!(o <= self.len - 4);
        // SAFETY: the caller proved o + 4 <= len, the window's extent.
        u32::from_le(unsafe { (self.p.add(o as usize) as *const u32).read_unaligned() })
    }
    /// Store to an address the caller has proved lies in the window; the old
    /// word goes to the undo log (the routines never make more than
    /// `MAX_STORES`).
    #[inline(always)]
    pub fn wr_in(&self, a: u32, v: u32, hx: &mut Hx) {
        let o = a.wrapping_sub(self.base);
        debug_assert!(o <= self.len - 4 && hx.n < MAX_STORES);
        // SAFETY: the caller proved o + 4 <= len; hx.n < MAX_STORES by the
        // routine's store count.
        unsafe {
            let p = self.p.add(o as usize);
            let old = u32::from_le((p as *const u32).read_unaligned());
            *hx.undo.get_unchecked_mut(hx.n) = (p, old);
            hx.n += 1;
            if let Some(l) = &mut hx.log {
                l.push((a, v, old));
            }
            (p as *mut u32).write_unaligned(v.to_le());
        }
    }
}

pub struct HleFn {
    pub name: &'static str,
    /// Entry PC (short words).
    pub entry: u32,
    /// The code words [entry, entry + words) whose SHA-256 must be `sha`.
    pub words: u32,
    pub sha: &'static str,
    pub run: RunFn,
}

#[derive(Default, Clone, Copy, Debug)]
pub struct HleStats {
    pub calls: u64,
    pub done: u64,
    pub declined: u64,
    pub guard: u64,
    pub insns: u64,
    pub ns: u64,
}

#[derive(Default)]
pub struct CheckStats {
    pub cases: u64,
    pub real: u64,
    pub synthetic: u64,
    pub done: u64,
    pub declined: u64,
    pub mismatches: u64,
    pub first: Vec<String>,
}

struct Check {
    first: u64,
    variants: u32,
    seen: u64,
    rng: u64,
    stats: CheckStats,
}

pub struct Hle {
    fns: Vec<HleFn>,
    ok: Vec<bool>,
    checked_gen: u64,
    pub stats: Vec<HleStats>,
    check: Option<Check>,
    interp: fn(&mut St) -> R<()>,
    time: bool,
    pub watched: bool,
    hx: Hx,
}

fn env_selected(name: &str) -> bool {
    match std::env::var("SHARC_HLE") {
        Ok(v) => {
            let v = v.trim();
            !(v.is_empty() || v == "0") && (v == "1" || v.split(',').any(|x| x.trim() == name))
        }
        Err(_) => false,
    }
}

impl Hle {
    /// The table for this run, or None when `SHARC_HLE` is unset.
    pub fn from_env(interp: fn(&mut St) -> R<()>) -> Option<Hle> {
        let fns: Vec<HleFn> = dn2_b82342::table()
            .into_iter()
            .filter(|f| env_selected(f.name))
            .collect();
        if fns.is_empty() {
            return None;
        }
        let check = std::env::var("SHARC_HLE_CHECK").ok().and_then(|v| {
            let mut it = v.split(',');
            let first: u64 = it.next()?.trim().parse().ok()?;
            let variants: u32 = it.next().and_then(|x| x.trim().parse().ok()).unwrap_or(0);
            (first > 0).then_some(Check {
                first,
                variants,
                seen: 0,
                rng: 0x9e37_79b9_7f4a_7c15,
                stats: CheckStats::default(),
            })
        });
        let n = fns.len();
        Some(Hle {
            ok: vec![false; n],
            checked_gen: u64::MAX,
            stats: vec![HleStats::default(); n],
            fns,
            check,
            interp,
            time: std::env::var_os("SHARC_HLE_TIME").is_some(),
            watched: false,
            hx: Hx::new(),
        })
    }

    /// Entry PCs, for the chain-stop set.
    pub fn entries(&self) -> impl Iterator<Item = u32> + '_ {
        self.fns.iter().map(|f| f.entry)
    }

    /// Watch the code pages so a store to the function re-runs the guard.
    pub fn watch(&self, mem: &mut Mem) {
        for f in &self.fns {
            let lo = 0x2800_0000 + f.entry * 2;
            mem.watch_code(lo, lo + f.words * 2);
            if (0xb8_0000..0xc0_0000).contains(&f.entry) {
                let a = 0x2000_0000 + (f.entry - 0xb8_0000) * 2;
                mem.watch_code(a, a + f.words * 2);
            }
        }
    }

    #[inline(always)]
    pub fn wants(&self, pc: u32) -> bool {
        self.fns.iter().any(|f| f.entry == pc)
    }

    fn guard(&mut self, mem: &Mem) {
        if self.checked_gen == mem.code_gen {
            return;
        }
        self.checked_gen = mem.code_gen;
        for (i, f) in self.fns.iter().enumerate() {
            let mut bytes = Vec::with_capacity(f.words as usize * 2);
            let mut full = true;
            for k in 0..f.words {
                match mem.read_sw(f.entry + k) {
                    Some(w) => bytes.extend_from_slice(&w.to_le_bytes()),
                    None => {
                        full = false;
                        break;
                    }
                }
            }
            let mut h = sha256::Sha256::new();
            h.update(&bytes);
            let got = crate::frames::hex(&h.finish());
            self.ok[i] = full && got == f.sha;
            if std::env::var_os("SHARC_HLE_LOG").is_some() {
                eprintln!(
                    "hle: {} code guard {} (sha {})",
                    f.name,
                    if self.ok[i] { "ok" } else { "MISMATCH" },
                    got
                );
            }
        }
    }

    /// Try the routine at PC. Some(EXIT_NEXT) when it ran; None declines with
    /// the state untouched.
    pub fn run(&mut self, s: &mut St, pc: u32) -> Option<u32> {
        let i = self.fns.iter().position(|f| f.entry == pc)?;
        if !self.watched {
            self.watch(&mut s.mem);
            self.watched = true;
        }
        self.guard(&s.mem);
        self.stats[i].calls += 1;
        if !self.ok[i] {
            self.stats[i].guard += 1;
            return None;
        }
        if s.pending.is_some() || s.pc_sw != pc as Int {
            self.stats[i].guard += 1;
            return None;
        }
        if self.check.is_some() {
            self.check_call(s, i);
        }
        if dbg().trace && self.stats[i].calls <= 40 {
            eprintln!(
                "trace: call {} icount {} depth {} loops {} pcstk {} callstk {} slots {:?} stkyx {:?} astatx {:?} pend {:?}",
                self.stats[i].calls, s.icount, s.loop_depth, s.loops.n, s.pc_stack.n, s.call_stack.n,
                s.loop_slots.items().len(), s.r[120], s.r[118], s.pending
            );
        }
        let t0 = if self.time { crate::ticks() } else { 0 };
        let before = s.icount;
        self.hx.reset();
        let f = self.fns[i].run;
        match f(s, &mut self.hx) {
            Some(()) => {
                if dbg().trace && self.stats[i].calls <= 12 {
                    let mut h = 1469598103934665603u64;
                    for v in s.r.iter() {
                        h = (h ^ v.b as u64).wrapping_mul(1099511628211);
                        h = (h ^ v.m as u64).wrapping_mul(1099511628211);
                    }
                    eprintln!(
                        "trace: after call {} icount {} regs {h:016x} depth {} slots {:?} pc {:#x}",
                        self.stats[i].calls, s.icount, s.loop_depth, s.loop_slots.a, s.pc_sw
                    );
                }
                let st = &mut self.stats[i];
                st.done += 1;
                st.insns += s.icount - before;
                if self.time {
                    st.ns += crate::ticks() - t0;
                }
                Some(EXIT_NEXT)
            }
            None => {
                self.hx.rollback();
                self.stats[i].declined += 1;
                None
            }
        }
    }

    pub fn report(&self) -> String {
        let mut out = String::new();
        for (f, st) in self.fns.iter().zip(&self.stats) {
            out += &format!(
                "hle {}: calls {} done {} declined {} guard {} insns {}{}\n",
                f.name,
                st.calls,
                st.done,
                st.declined,
                st.guard,
                st.insns,
                if st.ns > 0 {
                    format!(" ticks {} ({:.1} per call)", st.ns, st.ns as f64 / st.done.max(1) as f64)
                } else {
                    String::new()
                }
            );
        }
        if dbg().prof {
            let g = |i: usize| PROF[i].load(std::sync::atomic::Ordering::Relaxed);
            let n = self.stats.first().map_or(1, |s| s.done.max(1)) as f64;
            out += &format!(
                "hle prof ticks per call: pre {:.0} windows {:.0} compute {:.0} commit {:.0}\n",
                g(0) as f64 / n, g(1) as f64 / n, g(2) as f64 / n, g(3) as f64 / n
            );
        }
        if let Some(c) = &self.check {
            let k = &c.stats;
            out += &format!(
                "hle check: {} cases ({} captured, {} synthetic): {} run by both, {} declined, {} MISMATCHES\n",
                k.cases, k.real, k.synthetic, k.done, k.declined, k.mismatches
            );
            for m in &k.first {
                out += &format!("  {m}\n");
            }
        }
        out
    }

    // -- the checker ---------------------------------------------------------

    fn check_call(&mut self, s: &mut St, i: usize) {
        let Some(c) = self.check.as_mut() else { return };
        c.seen += 1;
        let capture = c.seen <= c.first || (c.seen % 1000 == 0 && c.stats.real < c.first * 4);
        if !capture {
            return;
        }
        let variants = c.variants;
        let run = self.fns[i].run;
        let interp = self.interp;
        let name = self.fns[i].name;
        let mut rng = c.rng;
        let mut results: Vec<(bool, Outcome)> = Vec::new();
        for v in 0..=variants {
            let mut undo: Vec<(u32, u32)> = Vec::new();
            if v > 0 {
                mutate(s, &mut undo, &mut rng, v);
            }
            let out = check_case(s, run, interp);
            results.push((v > 0, out));
            for &(a, w) in undo.iter().rev() {
                s.mem.fast_write_word(a, w);
            }
        }
        let c = self.check.as_mut().unwrap();
        c.rng = rng;
        for (synthetic, out) in results {
            let k = &mut c.stats;
            k.cases += 1;
            if synthetic {
                k.synthetic += 1;
            } else {
                k.real += 1;
            }
            match out {
                Outcome::Declined => k.declined += 1,
                Outcome::Equal => k.done += 1,
                Outcome::Differs(why) => {
                    k.done += 1;
                    k.mismatches += 1;
                    if k.first.len() < 12 {
                        k.first.push(format!(
                            "{name} case {} ({}): {why}",
                            k.cases,
                            if synthetic { "synthetic" } else { "captured" }
                        ));
                    }
                }
            }
        }
    }
}

enum Outcome {
    Declined,
    Equal,
    Differs(String),
}

/// The architectural state an entry-to-return call can change.
struct Snap {
    r: Box<[V; NUREG]>,
    bank_alt: [V; 96],
    bank: (Int, Int, Int),
    loop_slots: Stk<(V, V), 6>,
    loops: Stk<Loop, MAX_LOOPS>,
    call_stack: Box<Stk<Int, MAX_CALLS>>,
    pc_stack: Box<Stk<Int, MAX_CALLS>>,
    status_stack: Stk<(V, V, V), MAX_STATUS>,
    special: [Spec; 7],
    special_present: [bool; 7],
    pc_sw: Int,
    pending: Option<Pending>,
    steps: Int,
    at_loaded_entry: bool,
    icount: u64,
    loop_depth: Int,
    pc_stack_ctl: (Int, Int),
    timer_written: bool,
    loops_ok: bool,
    mmrs: Vec<(u32, V)>,
}

impl Snap {
    fn take(s: &St) -> Snap {
        Snap {
            r: Box::new(s.r),
            bank_alt: s.bank_alt,
            bank: (s.bank_active_mask, s.bank_pending_mask, s.bank_requested_mask),
            loop_slots: s.loop_slots,
            loops: s.loops,
            call_stack: Box::new(s.call_stack),
            pc_stack: Box::new(s.pc_stack),
            status_stack: s.status_stack,
            special: s.special,
            special_present: s.special_present,
            pc_sw: s.pc_sw,
            pending: s.pending,
            steps: s.steps,
            at_loaded_entry: s.at_loaded_entry,
            icount: s.icount,
            loop_depth: s.loop_depth,
            pc_stack_ctl: (s.pc_stack_pending, s.pc_stack_requested),
            timer_written: s.timer_written,
            loops_ok: s.loops_ok,
            mmrs: s.mmrs.clone(),
        }
    }
    fn restore(&self, s: &mut St) {
        s.r = *self.r;
        s.bank_alt = self.bank_alt;
        (s.bank_active_mask, s.bank_pending_mask, s.bank_requested_mask) = self.bank;
        s.loop_slots = self.loop_slots;
        s.loops = self.loops;
        s.call_stack = *self.call_stack;
        s.pc_stack = *self.pc_stack;
        s.status_stack = self.status_stack;
        s.special = self.special;
        s.special_present = self.special_present;
        s.pc_sw = self.pc_sw;
        s.pending = self.pending;
        s.steps = self.steps;
        s.at_loaded_entry = self.at_loaded_entry;
        s.icount = self.icount;
        s.loop_depth = self.loop_depth;
        (s.pc_stack_pending, s.pc_stack_requested) = self.pc_stack_ctl;
        s.timer_written = self.timer_written;
        s.loops_ok = self.loops_ok;
        s.mmrs.clone_from(&self.mmrs);
    }
    /// The first difference from OTHER, as text.
    fn diff(&self, o: &Snap) -> Option<String> {
        // The models' per-instruction registers are the engine's to set after
        // a call (`Engine::block_models_after`).
        let skip = [111usize, 105, 106];
        let mut d = Vec::new();
        for c in 0..NUREG {
            if skip.contains(&c) {
                continue;
            }
            if self.r[c] != o.r[c] {
                d.push(format!(
                    "r[{c}] {:#x}/{:#x} vs {:#x}/{:#x}",
                    self.r[c].b, self.r[c].m, o.r[c].b, o.r[c].m
                ));
            }
        }
        if self.bank_alt != o.bank_alt || self.bank != o.bank {
            d.push("bank".into());
        }
        if self.loop_slots.a != o.loop_slots.a || self.loop_slots.n != o.loop_slots.n {
            d.push(format!(
                "loop_slots {:?} vs {:?}",
                self.loop_slots.items(),
                o.loop_slots.items()
            ));
        }
        if self.loops.items() != o.loops.items() {
            d.push(format!("loops {:?} vs {:?}", self.loops.items(), o.loops.items()));
        }
        if self.call_stack.n != o.call_stack.n || self.call_stack.a != o.call_stack.a {
            d.push("call_stack".into());
        }
        if self.pc_stack.n != o.pc_stack.n || self.pc_stack.a != o.pc_stack.a {
            d.push(format!(
                "pc_stack {:?} vs {:?}",
                &self.pc_stack.a[..4],
                &o.pc_stack.a[..4]
            ));
        }
        if self.status_stack.n != o.status_stack.n || self.status_stack.a != o.status_stack.a {
            d.push("status_stack".into());
        }
        if self.mmrs != o.mmrs {
            d.push("mmrs".into());
        }
        if self.special != o.special || self.special_present != o.special_present {
            d.push("special".into());
        }
        if self.pc_sw != o.pc_sw {
            d.push(format!("pc {:#x} vs {:#x}", self.pc_sw, o.pc_sw));
        }
        if self.pending != o.pending {
            d.push(format!("pending {:?} vs {:?}", self.pending, o.pending));
        }
        if self.steps != o.steps {
            d.push(format!("steps {} vs {}", self.steps, o.steps));
        }
        if self.at_loaded_entry != o.at_loaded_entry {
            d.push("at_loaded_entry".into());
        }
        if self.icount != o.icount {
            d.push(format!("icount {} vs {}", self.icount, o.icount));
        }
        if self.loop_depth != o.loop_depth {
            d.push(format!("loop_depth {} vs {}", self.loop_depth, o.loop_depth));
        }
        if self.pc_stack_ctl != o.pc_stack_ctl {
            d.push("pc_stack_ctl".into());
        }
        if self.timer_written != o.timer_written {
            d.push("timer_written".into());
        }
        if self.loops_ok != o.loops_ok {
            d.push("loops_ok".into());
        }
        (!d.is_empty()).then(|| d.join("; "))
    }
}

fn raw(s: &St) -> Vec<u8> {
    let n = std::mem::size_of::<St>();
    // SAFETY: a byte view of the struct, for diagnostics only.
    unsafe { std::slice::from_raw_parts(s as *const St as *const u8, n) }.to_vec()
}

fn raw_diff(a: &[u8], b: &[u8]) -> String {
    let mut out = Vec::new();
    let mut i = 0;
    while i < a.len() {
        if a[i] != b[i] {
            let j = (i..a.len()).take_while(|&k| a[k] != b[k]).count() + i;
            out.push(format!("{i:#x}..{j:#x}"));
            i = j;
        } else {
            i += 1;
        }
    }
    out.join(",")
}

/// HLE and the interpreter from the same entry state; the state is restored.
fn check_case(s: &mut St, run: RunFn, interp: fn(&mut St) -> R<()>) -> Outcome {
    let snap0 = Snap::take(s);
    let mut hx = Hx::new();
    hx.log = Some(Vec::new());
    if run(s, &mut hx).is_none() {
        hx.rollback();
        snap0.restore(s);
        return Outcome::Declined;
    }
    let snap_h = Snap::take(s);
    let raw_h = if std::env::var_os("SHARC_HLE_RAW").is_some() { raw(s) } else { Vec::new() };
    let hlog = hx.log.take().unwrap_or_default();
    hx.log = Some(Vec::new());
    hx.rollback();
    snap0.restore(s);
    // The interpreter, one instruction at a time, until the PC the routine
    // left at (and no transfer pending).
    let target = snap_h.pc_sw;
    s.probe = Some(Vec::new());
    s.probe_bad = false;
    let mut err = None;
    let mut n = 0;
    loop {
        if let Err(t) = interp(s) {
            err = Some(format!("interpreter trap {t:?}"));
            break;
        }
        n += 1;
        if s.pc_sw == target && s.pending.is_none() {
            break;
        }
        if n > 20_000 {
            err = Some("interpreter did not reach the return PC".into());
            break;
        }
    }
    let snap_r = Snap::take(s);
    if !raw_h.is_empty() {
        let rr = raw(s);
        let d = raw_diff(&raw_h, &rr);
        if !d.is_empty() {
            eprintln!("raw St diff: {d}");
        }
    }
    let probe = s.probe.take().unwrap_or_default();
    // Final reference bytes at the written addresses, then roll them back.
    let mut ref_final: HashMap<u32, u8> = HashMap::new();
    let mut ref_orig: HashMap<u32, u8> = HashMap::new();
    for &(a, old, _) in &probe {
        ref_orig.entry(a).or_insert(old);
    }
    for &(a, _, _) in &probe {
        ref_final.insert(a, s.mem.byte(a));
    }
    for &(a, byte, flags) in probe.iter().rev() {
        s.mem.restore(a, byte, flags);
    }
    snap0.restore(s);
    if let Some(e) = err {
        return Outcome::Differs(e);
    }
    let mut why = Vec::new();
    if let Some(d) = snap_h.diff(&snap_r) {
        why.push(d);
    }
    // Memory: the union of the written bytes.
    let mut h_final: HashMap<u32, u8> = HashMap::new();
    let mut h_orig: HashMap<u32, u8> = HashMap::new();
    for &(a, new, old) in &hlog {
        for k in 0..4u32 {
            // The interpreter's probe keys low pages by their short-word alias.
            let ca = s.mem.canonical_of(a + k);
            h_orig.entry(ca).or_insert((old >> (8 * k)) as u8);
            h_final.insert(ca, (new >> (8 * k)) as u8);
        }
    }
    let mut addrs: Vec<u32> = ref_final.keys().chain(h_final.keys()).copied().collect();
    addrs.sort_unstable();
    addrs.dedup();
    let mut bad = 0;
    for a in addrs {
        let r = ref_final.get(&a).or_else(|| h_orig.get(&a));
        let h = h_final.get(&a).or_else(|| ref_orig.get(&a));
        if let (Some(r), Some(h)) = (r, h)
            && r != h
        {
            if bad < 8 {
                why.push(format!("mem[{a:#x}] hle {h:#x} ref {r:#x}"));
            }
            bad += 1;
        }
    }
    if !why.is_empty() && std::env::var_os("SHARC_HLE_DUMP").is_some() {
        for &(a, new, old) in &hlog {
            let rw: Vec<String> = (0..4)
                .map(|k| match ref_final.get(&s.mem.canonical_of(a + k)) {
                    Some(b) => format!("{b:02x}"),
                    None => "--".into(),
                })
                .collect();
            eprintln!(
                "dump: store {a:#x} hle {new:#010x} (old {old:#010x}) ref bytes {}",
                rw.join(" ")
            );
        }
    }
    if !why.is_empty() {
        why.push(format!(
            "entry R4 {:#x} R8 {:#x} R12 {:#x} I6 {:#x} bad bytes {bad}",
            snap0.r[4].b, snap0.r[8].b, snap0.r[12].b, snap0.r[22].b
        ));
    }
    if why.is_empty() {
        Outcome::Equal
    } else {
        Outcome::Differs(why.join("; "))
    }
}

fn next(rng: &mut u64) -> u64 {
    *rng ^= *rng << 13;
    *rng ^= *rng >> 7;
    *rng ^= *rng << 17;
    *rng
}

/// Float bit patterns that matter: zeros, denormals, extremes, NaN, infinity.
const SPECIAL: [u32; 20] = [
    0x0000_0000,
    0x8000_0000,
    0x0000_0001,
    0x8000_0001,
    0x007f_ffff,
    0x0080_0000,
    0x8080_0000,
    0x7f7f_ffff,
    0xff7f_ffff,
    0x7f80_0000,
    0xff80_0000,
    0x7fc0_0000,
    0xffff_ffff,
    0x3f80_0000,
    0xbf80_0000,
    0x3f00_0000,
    0x0100_0000,
    0x7e80_0000,
    0x3400_0000,
    0x4b00_0000,
];

/// Mutate the inputs of the call at the engine's current PC: float words in
/// the data record (I4 = R4), the input and output streams and the trip
/// count; UNDO gets (address, old word) for each word changed.
fn mutate(s: &mut St, undo: &mut Vec<(u32, u32)>, rng: &mut u64, variant: u32) {
    let i4 = s.r[4].b; // R4: the record
    let i5 = s.r[8].b; // R8: input stream
    let i3 = s.r[12].b; // R12: output stream
    let i6 = s.r[22].b;
    let m6 = s.r[38].b;
    let count = next(rng) % 3 + 1 + (variant as u64 % 2);
    let mut put = |s: &mut St, a: u32, v: u32, undo: &mut Vec<(u32, u32)>| {
        if let Some(old) = s.mem.fast_read_word(a)
            && s.mem.fast_write_word(a, v).is_some()
        {
            undo.push((a, old));
        }
    };
    let pick = |rng: &mut u64| -> u32 {
        let r = next(rng);
        match r % 6 {
            0 | 1 => SPECIAL[(r >> 8) as usize % SPECIAL.len()],
            2 => {
                // a random normal, wide exponent range
                let e = 0x40 + ((r >> 8) % 0x80) as u32;
                (((r >> 20) as u32 & 1) << 31) | (e << 23) | ((r >> 24) as u32 & 0x7f_ffff)
            }
            3 => {
                // a random normal near one
                let e = 0x78 + ((r >> 8) % 0x10) as u32;
                (((r >> 20) as u32 & 1) << 31) | (e << 23) | ((r >> 24) as u32 & 0x7f_ffff)
            }
            4 => {
                // tiny normal (products underflow)
                let e = 1 + ((r >> 8) % 0x40) as u32;
                (((r >> 20) as u32 & 1) << 31) | (e << 23) | ((r >> 24) as u32 & 0x7f_ffff)
            }
            _ => {
                // huge normal (sums and products overflow)
                let e = 0xa0 + ((r >> 8) % 0x5f) as u32;
                (((r >> 20) as u32 & 1) << 31) | (e << 23) | ((r >> 24) as u32 & 0x7f_ffff)
            }
        }
    };
    for _ in 0..count {
        let r = next(rng);
        match r % 10 {
            0 => {
                // the trip count (stack argument)
                let ns = [0u32, 1, 2, 3, 4, 5, 8, 17, 32, 33, 64, 100, 101, 200];
                let a = i6.wrapping_add(m6.wrapping_mul(4));
                put(s, a, ns[(r >> 8) as usize % ns.len()], undo);
            }
            1..=4 => {
                let w = (r >> 8) as u32 % 52;
                let v = pick(rng);
                put(s, i4.wrapping_add(4 * w), v, undo);
            }
            5..=7 => {
                let w = (r >> 8) as u32 % 40;
                let v = pick(rng);
                put(s, i5.wrapping_add(4 * w), v, undo);
            }
            _ => {
                let w = (r >> 8) as u32 % 40;
                let v = pick(rng);
                put(s, i3.wrapping_add(4 * w), v, undo);
            }
        }
    }
}

// -- loop bookkeeping (the interpreter's DO and loop end, as the fast tier's
//    glue does them) ---------------------------------------------------------

const STKYX: usize = 120;
const CURLCNTR: usize = 103;
const BIT26: u32 = 1 << 26;

/// A DO loop `start..=end` of REM iterations started and run to its end.
pub(crate) fn run_do_loop(s: &mut St, start: u32, end: u32, mode: i64, rem: u32) -> R<()> {
    // The core's state functions journal their writes: one unit, undone whole
    // on failure, and dropped from the journal when it is kept.
    s.begin();
    match run_do_loop_inner(s, start, end, mode, rem) {
        Ok(()) => {
            s.commit_host();
            Ok(())
        }
        Err(t) => {
            s.rollback();
            Err(t)
        }
    }
}

fn run_do_loop_inner(s: &mut St, start: u32, end: u32, mode: i64, rem: u32) -> R<()> {
    // push (sites::push_loop)
    s.r[CURLCNTR] = V::c(rem as Int);
    let c = s.r[STKYX];
    s.r[STKYX] = V {
        b: c.b & !BIT26,
        m: c.m | BIT26,
    };
    if s.cfg.stack_model {
        state::_push_loop_resource(s)?;
        let slot = (
            state::_packed_counter_laddr(s, end as Int)?,
            V::c(rem as Int),
        );
        let at = (s.loop_depth - 1) as usize;
        s.loop_slots.a[at] = slot;
        state::_sync_empty_loop_registers(s)?;
    }
    s.loops.push_raw(Loop {
        start_sw: start as i64,
        end_sw: end as i64,
        remaining: rem as i64,
        mode,
    })?;
    state::_push_pc_stack(s, start as Int)?;
    // run to the end: the last iteration's counter is 1
    s.loops.a[s.loops.n - 1].remaining = 1;
    if s.cfg.stack_model {
        let at = (s.loop_depth - 1) as usize;
        s.loop_slots.a[at].1 = V::c(1);
    } else {
        s.r[CURLCNTR] = V::c(1);
    }
    // pop (sites::pop_loop)
    s.loops.n -= 1;
    if s.cfg.stack_model {
        state::_pop_loop_resource(s)?;
    }
    let depth = state::_pc_stack_depth(s)?;
    if depth == 0 || (state::_pc_stack_top(s)? & 0xff_ffff) != start as Int {
        return Err(TRAP_INDEX);
    }
    sequencer::_pop_pc_stack(s)?;
    if !s.cfg.stack_model {
        let top = s.loops.items().last().map(|l| l.remaining);
        s.r[CURLCNTR] = match top {
            Some(r) => V::c(r as Int),
            None => V::c(0xFFFF_FFFF),
        };
        if s.loops.n == 0 {
            let c = s.r[STKYX];
            s.r[STKYX] = V {
                b: c.b | BIT26,
                m: c.m | BIT26,
            };
        }
    }
    Ok(())
}

/// ASTATX after the last writers of each flag group.
pub(crate) fn astatx(
    s: &St,
    shift: Option<u32>,
    alu: Option<(FlagKind, [u32; 2])>,
    mul: Option<u32>,
) -> V {
    let mut a = s.r[118];
    if let Some(op) = shift {
        a = apply_flag(a, FlagKind::Leftz, [op, 0]);
    }
    if let Some((k, src)) = alu {
        a = apply_flag(a, k, src);
    }
    if let Some(p) = mul {
        a = apply_flag(a, FlagKind::Fmul, [p, 0]);
    }
    a
}

/// Is the engine in the state every routine here is written for?
pub(crate) fn state_ok(s: &St) -> bool {
    let cfg = &s.cfg;
    if !(cfg.fast_mem && cfg.assume_nw32 && cfg.has_concrete && !cfg.data_memory_tainted) {
        return false;
    }
    if cfg.bank_model && (s.bank_pending_mask >= 0 || s.bank_requested_mask >= 0) {
        return false;
    }
    if cfg.stack_model && (s.pc_stack_pending >= 0 || s.pc_stack_requested >= 0) {
        return false;
    }
    let mode1 = s.r[MODE1];
    // Known; no PEYEN (bit 21), no broadcast loads (22, 23).
    mode1.is_c() && mode1.b & (7 << 21) == 0
}
