//! The SHARC "fast tier": a load-time translator that turns a hot region of
//! the loaded DSP image into native code and falls back, exactly, to the
//! existing paths (the generated blocks, the interpreter).
//!
//! Layers, all independent of any firmware address or table:
//!  * `region`/`lower`: decode a DO-loop body from loaded memory with the
//!    generic decoder and lower it to the kernel IR (`ir`);
//!  * `ir`: a backend-neutral loop kernel (u32/f32/u64 values, variables,
//!    guards, window loads and stores). `interp` is a reference interpreter;
//!    the Cranelift backend lives in `native/sharc-fast-cl`, a WebAssembly
//!    backend can be a second one;
//!  * `glue`: the entry checks, window resolution and exact state
//!    reconstruction around a kernel call;
//!  * `FastEngine`: the per-engine cache of regions by entry PC, behind the
//!    `FastTier` hook of `Engine::step`.
//!
//! Entry PCs come from run-time configuration: `Engine::set_fast`, or the
//! `SHARC_FAST_REGIONS` environment variable (comma separated hex PCs) read
//! by `Engine::from_image`. Off by default.

pub mod cfg;
pub mod decode_view;
pub mod glue;
pub mod interp;
pub mod ir;
pub mod lower;
pub mod plugin;
pub mod region;

use crate::rt::St;
use ir::*;
use std::sync::Mutex;

/// What an address-valued quantity is relative to: nothing (absolute), or a
/// register's value at entry.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Base {
    None,
    Reg(u8),
}

/// How an instruction's ASTATX effect is computed from its remembered
/// sources (`glue::apply_flag`).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FlagKind {
    /// Float ALU result bits: AZ AN from the bits, AC AV AS AI cleared, AF set.
    Falu,
    /// Float multiply result bits: MN from the sign, MV MU MI cleared.
    Fmul,
    /// Short float multiply: MN MV MU MI become unknown.
    FmulForget,
    /// Adder flags of `a + b` / `a - b`; AS AI AF cleared.
    Iadd,
    Isub,
    /// Float dual add/subtract: AZ and AN ORed over the two results (the
    /// sources); the rest as `Falu`.
    FaluOr,
    /// Fixed dual add/subtract: AC AV AN AZ of the add and of the subtract
    /// ORed; the sources are the two operands.
    IaddSubOr,
    /// Logical result: AN AZ, the rest of the ALU bits cleared.
    Logical,
    /// Shifter: SZ from the (shifted) value, SV fixed, SS cleared.
    Shift {
        sv: bool,
    },
    Fext {
        sv: bool,
    },
    /// Float abs: AF set, AZ from the result bits, AN cleared, AS from the
    /// input sign (sources: result, input).
    Fabs,
    /// comp, compu, fcomp (source: value = eq | lt << 2 | gt << 31): AZ AN
    /// from the value, AF set for the float form, AC AV AS AI cleared. The
    /// CACC shift register is not part of this effect: the kernel tracks it
    /// in `PSEUDO_CACC` (`glue::apply_cacc`).
    Compare {
        float: bool,
    },
    /// btst (source: bit 0 = SZ, bit 1 = SV), SS cleared.
    Btst,
    /// leftz (source: the operand): SZ = its MSB, SV = it is zero.
    Leftz,
    /// Register-amount shift (sources: shifted value, amount > 0): SZ from
    /// the shifted value, SV from the amount, SS cleared.
    ShiftDyn,
    /// recips: AC AS AI AV AZ cleared, AN from the input sign (source: the
    /// input); AF unchanged. It defines only part of the ALU group, so a
    /// kernel that tracks flags lazily (one last writer per group) refuses it.
    Recips,
}

/// The largest writer id within a flag group (`FlagKind::group_id`).
pub const MAX_FLAG_ID: u32 = 10;

impl FlagKind {
    /// The flag group the writer belongs to and its id there (1..=4), as the
    /// kernel records them (`lower::cond`).
    pub fn group_id(self) -> (u32, u32) {
        match self {
            FlagKind::Falu => (0, 1),
            FlagKind::Iadd => (0, 2),
            FlagKind::Isub => (0, 3),
            FlagKind::Logical => (0, 4),
            FlagKind::FaluOr => (0, 5),
            FlagKind::IaddSubOr => (0, 6),
            FlagKind::Fabs => (0, 7),
            FlagKind::Compare { float: false } => (0, 8),
            FlagKind::Compare { float: true } => (0, 9),
            FlagKind::Recips => (0, 10),
            FlagKind::Fmul => (1, 1),
            FlagKind::FmulForget => (1, 2),
            FlagKind::Shift { sv: false } => (2, 1),
            FlagKind::Shift { sv: true } => (2, 2),
            FlagKind::Fext { sv: false } => (2, 3),
            FlagKind::Fext { sv: true } => (2, 4),
            FlagKind::Btst => (2, 5),
            FlagKind::Leftz => (2, 6),
            FlagKind::ShiftDyn => (2, 7),
        }
    }

    pub fn from_group_id(group: u32, id: u32) -> Option<FlagKind> {
        Some(match (group, id) {
            (0, 1) => FlagKind::Falu,
            (0, 2) => FlagKind::Iadd,
            (0, 3) => FlagKind::Isub,
            (0, 4) => FlagKind::Logical,
            (0, 5) => FlagKind::FaluOr,
            (0, 6) => FlagKind::IaddSubOr,
            (0, 7) => FlagKind::Fabs,
            (0, 8) => FlagKind::Compare { float: false },
            (0, 9) => FlagKind::Compare { float: true },
            (0, 10) => FlagKind::Recips,
            (1, 1) => FlagKind::Fmul,
            (1, 2) => FlagKind::FmulForget,
            (2, 1) => FlagKind::Shift { sv: false },
            (2, 2) => FlagKind::Shift { sv: true },
            (2, 3) => FlagKind::Fext { sv: false },
            (2, 4) => FlagKind::Fext { sv: true },
            (2, 5) => FlagKind::Btst,
            (2, 6) => FlagKind::Leftz,
            (2, 7) => FlagKind::ShiftDyn,
            _ => return None,
        })
    }
}

#[derive(Clone, Copy, Debug)]
pub struct FlagWriter {
    pub insn: u32,
    pub kind: FlagKind,
    /// Flag-source slots (context `fsrc` indices); 255 for none.
    pub fsrc: [u8; 2],
}

/// A condition on the engine state a region needs.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Req {
    /// The register is fully known.
    Known(u8),
    /// The register is known and has this value (baked into the kernel).
    Eq(u8, u32),
    /// Every address in `base + [lo, hi] + ts*t` (t over the iterations)
    /// lies in the byte-address range where normal-word accesses are plain,
    /// scaled by 4.
    NwPlain {
        base: Base,
        lo: i64,
        hi: i64,
        ts: i64,
        nf: i64,
    },
    /// `MODE1 & mask == value` (rounding and saturation modes the lowering
    /// was made for).
    Mode1 { mask: u32, value: u32 },
    /// The ASTATX bits in `mask` are known (a condition reads them).
    FlagsKnown(u32),
    /// The register is known and its value lies in `lo..=hi` (unsigned): an
    /// index register inside its circular buffer, which is what bounds every
    /// address the wrapped updates produce.
    Within { c: u8, lo: u32, hi: u32 },
}

/// A memory window the region touches: the bytes
/// `base + [lo, hi) + ts*t`, to be resolved to a host pointer per call.
#[derive(Clone, Copy, Debug)]
pub struct WinSpec {
    pub base: Base,
    pub lo: i64,
    pub hi: i64,
    pub ts: i64,
    /// Coefficient of the (symbolic) iteration count: see `lower::Abs`.
    pub nf: i64,
    pub read: bool,
    pub write: bool,
}

/// The engine's view of the fast tier (`Engine::fast`).
pub trait FastTier: Send {
    /// Is PC an entry the tier handles?
    fn wants(&self, pc: u32) -> bool;
    /// Run the region at PC if it applies. `Some(exit code)` only when at
    /// least one instruction completed; `None` leaves the state untouched.
    fn run(&mut self, s: &mut St, pc: u32) -> Option<u32>;
    /// Generated blocks call each other directly (`EXIT_CHAIN`), which would
    /// carry execution into an entry PC without passing `Engine::step`: when
    /// true the engine ends each chain at the first link, so every block
    /// boundary is a dispatch and the tier sees every entry.
    fn caps_chain(&self) -> bool {
        false
    }
    /// A one-paragraph summary for logs.
    fn report(&self) -> String {
        String::new()
    }
    /// Does the tier pick its own regions (`note`)?
    fn auto(&self) -> bool {
        false
    }
    /// Should the engine also report the entries of generated blocks at
    /// their dispatch (`note`), not only interpreter boundaries?
    fn auto_dispatch(&self) -> bool {
        false
    }
    /// The engine reached a candidate region start at an interpreter
    /// boundary (the target of a taken branch, call or loop-back) or at a
    /// generated-block dispatch. Only called when `auto()`; must be cheap.
    fn note(&mut self, _pc: u32) {}
    /// A line for logs about the engine as a whole (instructions per tier).
    fn engine_report(&self, _total: u64, _interp: u64, _aot: u64) -> String {
        String::new()
    }
    /// Cumulative counters (instructions run by the tier, builds, compile time).
    fn fast_stats(&self) -> Option<FastStats> {
        None
    }
}

#[derive(Clone, Debug, Default)]
pub struct FastStats {
    pub built: u64,
    pub refused: u64,
    pub calls: u64,
    pub runs: u64,
    pub insns: u64,
    pub declined: [u64; 5],
    pub compile_ns: u64,
    pub build_ns: u64,
    /// Auto selection: PCs armed (counter passed the threshold), regions
    /// dropped as unprofitable, refusals by normalized reason.
    pub armed: u64,
    pub unprofitable: u64,
    pub cached_refusals: u64,
    pub reasons: std::collections::BTreeMap<String, u64>,
}

/// Hot-region selection (`SHARC_FAST_AUTO=threshold,min_insns[,blocks]`).
///
/// Candidates are the PCs the engine reports through `note`: the target of a
/// non-sequential PC change at an interpreter boundary (taken branch, call,
/// return, DO-loop loop-back) and the entry of a generated block at its
/// dispatch. Each is counted in a small direct-mapped table with lossy
/// replacement (a resident count only decays when another PC collides, so a
/// hot PC outlives cold ones). When a count reaches `threshold` the PC joins
/// the region list and the next boundary at it builds a region (loop shape,
/// then cfg). A refusal removes the PC again, remembering the MODE1 and the
/// code generation it was refused under; a region is dropped when, after a
/// warm-up of calls, the instructions it ran per call stay under
/// `min_insns` (the per-call fixed cost is not paid back). Generated-block
/// entries are counted too only with `blocks` (a region then replaces its
/// block; the compile cost is not paid back where blocks already are fast).
struct Auto {
    threshold: u32,
    min_insns: u64,
    blocks: bool,
    table: Box<[(u32, u32)]>,
    /// PC -> (MODE1 bits, dec_gen, tries) of the last refusal.
    tried: std::collections::HashMap<u32, (u32, u64, u8)>,
}

const AUTO_BITS: u32 = 13;
const AUTO_WARMUP: u64 = 32;
const AUTO_TRIES: u8 = 3;
pub const AUTO_DEFAULT_THRESHOLD: u32 = 256;
pub const AUTO_DEFAULT_MIN_INSNS: u64 = 64;

impl Auto {
    fn new(threshold: u32, min_insns: u64, blocks: bool) -> Auto {
        Auto {
            threshold: threshold.max(1),
            min_insns,
            blocks,
            table: vec![(u32::MAX, 0); 1 << AUTO_BITS].into_boxed_slice(),
            tried: Default::default(),
        }
    }
}

/// `SHARC_FAST_AUTO=threshold,min_insns[,blocks]` -> (threshold,
/// min_insns, blocks); None when unset or `0`.
pub fn auto_config() -> Option<(u32, u64, bool)> {
    let v = std::env::var("SHARC_FAST_AUTO").ok()?;
    let v = v.trim();
    if v.is_empty() || v == "0" {
        return None;
    }
    let mut it = v.split(',');
    let t = it
        .next()
        .and_then(|x| x.trim().parse().ok())
        .unwrap_or(AUTO_DEFAULT_THRESHOLD);
    let m = it
        .next()
        .and_then(|x| x.trim().parse().ok())
        .unwrap_or(AUTO_DEFAULT_MIN_INSNS);
    let b = it.next().is_some_and(|x| x.trim() == "1");
    Some((t, m, b))
}

fn reason_class(r: &str) -> String {
    let mut out = String::new();
    let mut digits = false;
    for c in r.chars().take(60) {
        if c.is_ascii_digit() || c == 'x' && digits {
            if !digits {
                out.push('#');
            }
            digits = true;
        } else {
            digits = false;
            out.push(c);
        }
    }
    out
}

#[derive(Default)]
struct Slot {
    region: Option<region::Region>,
    kernel: Option<Box<dyn CompiledKernel>>,
    refused: Option<String>,
    rebuilds: u32,
    transient: u32,
    /// Selected by `Auto` (dropped again on refusal or low yield).
    auto: bool,
    calls: u64,
    insns: u64,
}

const BLOOM_WORDS: usize = 1024;

/// Multiplicative hash for PC keys.
#[derive(Default)]
pub struct PcHash(u64);

impl std::hash::Hasher for PcHash {
    fn finish(&self) -> u64 {
        self.0 ^ (self.0 >> 32)
    }
    fn write(&mut self, bytes: &[u8]) {
        for &b in bytes {
            self.0 = (self.0 ^ b as u64).wrapping_mul(0x9e37_79b9_7f4a_7c15);
        }
    }
    fn write_u32(&mut self, v: u32) {
        self.0 = (v as u64).wrapping_mul(0x9e37_79b9_7f4a_7c15);
    }
}

pub struct FastEngine {
    backend: Box<dyn KernelBackend>,
    /// Entry PCs and their regions, in step; `bloom` is a 64 Kbit filter of
    /// the PCs so `wants` (asked at every block boundary) is one test, and
    /// `lookup` maps a PC to its index for the few that pass it.
    pcs: Vec<u32>,
    slots: Vec<Slot>,
    bloom: Box<[u64]>,
    bloom_stale: u32,
    lookup: std::collections::HashMap<u32, usize, std::hash::BuildHasherDefault<PcHash>>,
    ctx: glue::Ctx,
    pub stats: FastStats,
    log: bool,
    cap_chain: bool,
    force_cfg: bool,
    auto: Option<Auto>,
}

// SAFETY: a FastEngine is owned by one Engine and used from one thread at a
// time (the engine is moved between threads, never shared); its compiled
// kernels are plain code and the buffers they touch are passed per call.
unsafe impl Send for FastEngine {}

type BackendFactory = fn() -> Box<dyn KernelBackend>;
static BACKEND: Mutex<Option<BackendFactory>> = Mutex::new(None);

/// Register the kernel backend `SHARC_FAST_REGIONS` engines use (the
/// Cranelift crate calls this); without one the reference interpreter runs.
pub fn register_backend(f: BackendFactory) {
    *BACKEND.lock().unwrap() = Some(f);
}

/// The registered backend, else the shared library named by
/// `SHARC_FAST_BACKEND`, else the (slow, exact) reference interpreter.
pub fn default_backend() -> Box<dyn KernelBackend> {
    if let Some(f) = *BACKEND.lock().unwrap() {
        return f();
    }
    #[cfg(unix)]
    if let Ok(path) = std::env::var("SHARC_FAST_BACKEND") {
        match plugin::PluginBackend::open(&path) {
            Ok(b) => return Box::new(b),
            Err(e) => eprintln!("fast tier: {e}; using the reference interpreter"),
        }
    }
    Box::new(interp::InterpBackend)
}

impl FastEngine {
    pub fn new(backend: Box<dyn KernelBackend>, pcs: &[u32]) -> FastEngine {
        let mut list: Vec<u32> = pcs.to_vec();
        list.sort_unstable();
        list.dedup();
        let mut fe = FastEngine {
            backend,
            bloom: vec![0u64; BLOOM_WORDS].into_boxed_slice(),
            bloom_stale: 0,
            lookup: Default::default(),
            slots: list.iter().map(|_| Slot::default()).collect(),
            pcs: list,
            ctx: glue::Ctx::default(),
            stats: FastStats::default(),
            log: {
                let on = std::env::var_os("SHARC_FAST_LOG").is_some();
                glue::set_shape_log(on);
                on
            },
            // SHARC_FAST_CHAIN=0 keeps chaining (entries reached by a chain
            // are then missed).
            cap_chain: std::env::var("SHARC_FAST_CHAIN").map_or(true, |v| v != "0"),
            force_cfg: std::env::var("SHARC_FAST_SHAPE").is_ok_and(|v| v == "cfg"),
            auto: None,
        };
        fe.rebuild_bloom();
        fe
    }

    /// Also pick regions at run time (see `Auto`).
    pub fn with_auto(mut self, threshold: u32, min_insns: u64, blocks: bool) -> FastEngine {
        self.auto = Some(Auto::new(threshold, min_insns, blocks));
        if !blocks && self.pcs.is_empty() {
            // Only interpreter boundaries are noted and no entry is fixed in
            // advance: generated blocks may keep chaining.
            self.cap_chain = false;
        }
        self
    }

    fn rebuild_bloom(&mut self) {
        self.bloom.fill(0);
        for &pc in &self.pcs {
            let (w, m) = Self::bit(pc);
            self.bloom[w] |= m;
        }
        self.lookup.clear();
        for (i, &pc) in self.pcs.iter().enumerate() {
            self.lookup.insert(pc, i);
        }
        self.bloom_stale = 0;
    }

    /// Forget the slot of an auto-selected PC.
    fn drop_slot(&mut self, pc: u32) {
        let Some(i) = self.lookup.remove(&pc) else {
            return;
        };
        self.pcs.swap_remove(i);
        self.slots.swap_remove(i);
        if let Some(&moved) = self.pcs.get(i) {
            self.lookup.insert(moved, i);
        }
        // The filter keeps the removed PC's bit (a false positive costs one
        // lookup); rebuild it now and then.
        self.bloom_stale += 1;
        if self.bloom_stale >= 256 {
            self.rebuild_bloom();
        }
    }

    #[inline(always)]
    fn bit(pc: u32) -> (usize, u64) {
        let h = pc.wrapping_mul(0x9e37_79b1) >> 16;
        ((h >> 6) as usize, 1u64 << (h & 63))
    }

    #[inline(always)]
    fn index(&self, pc: u32) -> Option<usize> {
        let (w, m) = Self::bit(pc);
        if self.bloom[w] & m == 0 {
            return None;
        }
        self.lookup.get(&pc).copied()
    }

    pub fn backend_name(&self) -> &'static str {
        self.backend.name()
    }

    /// Skip the loop shape and build CFG regions only (tests, comparisons).
    pub fn force_cfg(&mut self, on: bool) {
        self.force_cfg = on;
    }

    /// Entry PCs whose region was refused, with the reason.
    pub fn refusals(&self) -> Vec<(u32, String)> {
        self.pcs
            .iter()
            .zip(&self.slots)
            .filter_map(|(pc, s)| s.refused.clone().map(|r| (*pc, r)))
            .collect()
    }

    /// The built region at PC (tests and reports).
    pub fn region(&self, pc: u32) -> Option<&region::Region> {
        self.slots[self.index(pc)?].region.as_ref()
    }

    fn build(&mut self, s: &mut St, pc: u32) -> Option<()> {
        let t0 = std::time::Instant::now();
        // The remaining iterations of the active DO loop that starts here,
        // else (or when that is refused) the CFG shape from this pc.
        // SHARC_FAST_SHAPE=cfg skips the loop shape (tests, comparisons).
        let loop_try = if self.force_cfg {
            None
        } else {
            Some(region::build_loop(s, pc))
        };
        let mut no_loop = false;
        let built = match loop_try {
            Some(Ok(r)) => Ok(r),
            other => {
                let loop_err = match other {
                    Some(Err(e)) if e.0.starts_with("no active loop") => {
                        no_loop = true;
                        None
                    }
                    Some(Err(e)) => Some(e),
                    _ => None,
                };
                match region::build_cfg(s, pc) {
                    Ok(r) => Ok(r),
                    Err(e) => Err(match loop_err {
                        Some(l) => {
                            lower::Refuse(format!("loop shape: {}; cfg shape: {}", l.0, e.0))
                        }
                        None => e,
                    }),
                }
            }
        };
        self.stats.build_ns += t0.elapsed().as_nanos() as u64;
        let slot = &mut self.slots[self.lookup.get(&pc).copied()?];
        match built {
            Err(e) => {
                if no_loop && slot.transient < 4 {
                    // The loop shape needs an active loop here, which may
                    // be the case on another visit: try again later.
                    slot.transient += 1;
                    return None;
                }
                if self.log {
                    eprintln!("fast: {pc:#x} refused: {}", e.0);
                }
                self.stats.refused += 1;
                *self.stats.reasons.entry(reason_class(&e.0)).or_default() += 1;
                slot.refused = Some(e.0);
                None
            }
            Ok(mut r) => {
                let t1 = std::time::Instant::now();
                let k = self.backend.compile(&r.kernel);
                self.stats.compile_ns += t1.elapsed().as_nanos() as u64;
                match k {
                    Err(e) => {
                        if self.log {
                            eprintln!("fast: {pc:#x} compile failed: {e}");
                        }
                        self.stats.refused += 1;
                        *self
                            .stats
                            .reasons
                            .entry(format!("compile: {}", reason_class(&e)))
                            .or_default() += 1;
                        slot.refused = Some(e);
                        None
                    }
                    Ok(k) => {
                        for &(at, _) in &r.code_words {
                            s.mem.watch_sw(at);
                        }
                        r.verified_gen = s.mem.dec_gen;
                        if self.log {
                            eprintln!(
                                "fast: {pc:#x} built: {} insns, {} windows, {} reqs ({} circular), kernel {} ops",
                                r.len(),
                                r.wins.len(),
                                r.reqs.len(),
                                r.within.len(),
                                r.kernel.inst_count()
                            );
                        }
                        self.stats.built += 1;
                        slot.region = Some(r);
                        slot.kernel = Some(k);
                        Some(())
                    }
                }
            }
        }
    }
}

/// The failing requirement is only "this index register lies inside its
/// circular buffer" (`Req::Within`): a property of the state, not a baked
/// constant, so re-making the region from it gives the same region again.
fn req_is_within(s: &St, r: &region::Region) -> bool {
    for &c in &r.known {
        if s.r[c as usize].m != u32::MAX {
            return false;
        }
    }
    if r.eqs.iter().any(|&(c, k)| {
        let v = s.r[c as usize];
        v.m != u32::MAX || v.b != k
    }) {
        return false;
    }
    r.within.iter().any(|&(c, lo, hi)| {
        let v = s.r[c as usize];
        !(v.is_c() && (lo..=hi).contains(&v.b))
    })
}

/// Which kind of requirement a `Decline::Req` was (diagnostics).
fn req_failure(s: &St, r: &region::Region) -> String {
    for &c in &r.known {
        if s.r[c as usize].m != u32::MAX {
            return format!("known r{c}");
        }
    }
    for &(c, k) in &r.eqs {
        let v = s.r[c as usize];
        if v.m != u32::MAX || v.b != k {
            return format!(
                "eq r{c} {k:#x} -> {:#x} (mode1 {:#x})",
                v.b,
                s.r[crate::rt::MODE1].b
            );
        }
    }
    for q in &r.nw {
        if !glue::eval_req(s, q, 1) {
            return "nw-plain address".into();
        }
    }
    for &(c, lo, hi) in &r.within {
        let v = s.r[c as usize];
        if !(v.is_c() && (lo..=hi).contains(&v.b)) {
            return format!("within r{c} {lo:#x}..{hi:#x} -> {:#x}", v.b);
        }
    }
    "misc (mode1/flags)".into()
}

fn mode1_bits(s: &St) -> u32 {
    let m = s.r[crate::rt::MODE1];
    if m.is_c() { m.b as u32 } else { u32::MAX }
}

impl FastEngine {
    /// A counter reached the threshold: make the PC an entry.
    fn arm(&mut self, pc: u32) {
        if self.index(pc).is_some() {
            return;
        }
        if self
            .auto
            .as_ref()
            .is_some_and(|a| a.tried.get(&pc).is_some_and(|t| t.2 >= AUTO_TRIES))
        {
            return;
        }
        self.stats.armed += 1;
        if self.log {
            eprintln!("fast: {pc:#x} armed");
        }
        self.lookup.insert(pc, self.pcs.len());
        self.pcs.push(pc);
        self.slots.push(Slot {
            auto: true,
            ..Slot::default()
        });
        let (w, m) = Self::bit(pc);
        self.bloom[w] |= m;
    }
}

impl Drop for FastEngine {
    fn drop(&mut self) {
        if self.log && (self.stats.calls > 0 || self.stats.armed > 0) {
            eprintln!("{}", self.report());
        }
    }
}

impl FastTier for FastEngine {
    #[inline(always)]
    fn wants(&self, pc: u32) -> bool {
        self.index(pc).is_some()
    }

    fn run(&mut self, s: &mut St, pc: u32) -> Option<u32> {
        let ix = self.index(pc)?;
        if self.slots[ix].refused.is_some() {
            return None;
        }
        if self.slots[ix].region.is_none() {
            let m1 = mode1_bits(s);
            if self.slots[ix].auto
                && let Some(a) = &self.auto
                && a.tried
                    .get(&pc)
                    .is_some_and(|&(m, g, _)| m == m1 && g == s.mem.dec_gen)
            {
                // Refused before under the same MODE1 and code.
                self.stats.cached_refusals += 1;
                self.drop_slot(pc);
                return None;
            }
            if self.build(s, pc).is_none() {
                if self.slots[ix].auto && self.slots[ix].refused.is_some() {
                    let gen_ = s.mem.dec_gen;
                    if let Some(a) = &mut self.auto {
                        let t = a.tried.entry(pc).or_insert((m1, gen_, 0));
                        *t = (m1, gen_, t.2.saturating_add(1));
                    }
                    self.drop_slot(pc);
                }
                return None;
            }
        }
        let slot = &mut self.slots[ix];
        let region = slot.region.as_mut()?;
        if s.mem.dec_gen != region.verified_gen {
            let same = region
                .code_words
                .iter()
                .all(|&(at, w)| s.mem.read_sw(at).unwrap_or(0) == w);
            if same {
                region.verified_gen = s.mem.dec_gen;
            } else {
                slot.region = None;
                slot.kernel = None;
                return None;
            }
        }
        self.stats.calls += 1;
        let before = s.icount;
        let kernel = slot.kernel.as_deref()?;
        let res = if region.cfg.is_some() {
            glue::run_cfg(s, region, kernel, &mut self.ctx)
        } else {
            glue::run(s, region, kernel, &mut self.ctx)
        };
        if self.log {
            eprintln!(
                "fast: run {pc:#x}: icount {before} -> {} (limit {}) {:?} pc {:#x} pending {:?}",
                s.icount,
                s.limit,
                res.as_ref().map(|c| *c).map_err(|d| *d),
                s.pc_sw,
                s.pending
            );
        }
        slot.calls += 1;
        slot.insns += s.icount - before;
        let mut drop_after = false;
        if slot.auto
            && slot.calls >= AUTO_WARMUP
            && slot.calls.is_power_of_two()
            && let Some(a) = &mut self.auto
            && slot.insns < a.min_insns * slot.calls
        {
            // Too little work per call to pay the fixed call cost.
            if self.log {
                eprintln!(
                    "fast: {pc:#x} dropped: {} insns in {} calls",
                    slot.insns, slot.calls
                );
            }
            self.stats.unprofitable += 1;
            a.tried
                .insert(pc, (mode1_bits(s), s.mem.dec_gen, AUTO_TRIES));
            drop_after = true;
        }
        let ret = match res {
            Ok(code) => {
                self.stats.runs += 1;
                self.stats.insns += s.icount - before;
                Some(code)
            }
            Err(d) => {
                self.stats.declined[d as usize] += 1;
                if self.log && d == glue::Decline::Req {
                    let why = req_failure(s, region);
                    *self
                        .stats
                        .reasons
                        .entry(format!("decline req: {why}"))
                        .or_default() += 1;
                }
                if d == glue::Decline::Req && slot.rebuilds < 16 && !req_is_within(s, region) {
                    // A baked constant changed: re-make the region from
                    // this state, a bounded number of times.
                    slot.rebuilds += 1;
                    let n = slot.rebuilds;
                    slot.region = None;
                    slot.kernel = None;
                    if self.build(s, pc).is_some() {
                        self.slots[ix].rebuilds = n;
                    } else if self.slots[ix].auto && self.slots[ix].refused.is_some() {
                        let (m1, g) = (mode1_bits(s), s.mem.dec_gen);
                        if let Some(a) = &mut self.auto {
                            a.tried.insert(pc, (m1, g, AUTO_TRIES));
                        }
                        drop_after = true;
                    }
                }
                None
            }
        };
        if drop_after {
            self.drop_slot(pc);
        }
        ret
    }

    fn caps_chain(&self) -> bool {
        self.cap_chain
    }

    fn auto(&self) -> bool {
        self.auto.is_some()
    }

    fn auto_dispatch(&self) -> bool {
        self.auto.as_ref().is_some_and(|a| a.blocks)
    }

    #[inline(always)]
    fn note(&mut self, pc: u32) {
        let Some(a) = &mut self.auto else { return };
        let e = &mut a.table[(pc.wrapping_mul(0x9e37_79b1) >> (32 - AUTO_BITS)) as usize];
        if e.0 != pc {
            // Lossy replacement: a resident count decays per collision.
            if e.1 <= 1 {
                *e = (pc, 1);
            } else {
                e.1 -= 1;
            }
            return;
        }
        if e.1 >= a.threshold {
            return;
        }
        e.1 += 1;
        if e.1 == a.threshold {
            self.arm(pc);
        }
    }

    fn fast_stats(&self) -> Option<FastStats> {
        Some(self.stats.clone())
    }

    fn engine_report(&self, total: u64, interp: u64, blocks: u64) -> String {
        let f = self.stats.insns;
        // Instructions run by the fast tier also count as block instructions.
        let aot = blocks.saturating_sub(f);
        let pct = |n: u64| 100.0 * n as f64 / total.max(1) as f64;
        format!(
            "fast engine: {total} insns: fast tier {f} ({:.2}%), aot {aot} ({:.2}%), interpreter {interp} ({:.2}%)",
            pct(f),
            pct(aot),
            pct(interp)
        )
    }

    fn report(&self) -> String {
        let s = &self.stats;
        let mut reasons: Vec<_> = s.reasons.iter().collect();
        reasons.sort_by(|a, b| b.1.cmp(a.1));
        let reasons: Vec<String> = reasons
            .iter()
            .take(40)
            .map(|(r, n)| format!("{n}x {r}"))
            .collect();
        format!(
            "fast tier [{}]: {} armed, {} built ({} ms compile, {} ms build), {} refused ({} cached), {} unprofitable; {} calls: {} ran ({} insns), declined shape/req/window/budget/exit0 = {:?}; refusals: [{}]",
            self.backend.name(),
            s.armed,
            s.built,
            s.compile_ns / 1_000_000,
            s.build_ns / 1_000_000,
            s.refused,
            s.cached_refusals,
            s.unprofitable,
            s.calls,
            s.runs,
            s.insns,
            s.declined,
            reasons.join("; "),
        )
    }
}

/// `SHARC_FAST_REGIONS=0x1c399a,...` (hex entry PCs) as a fast tier.
/// `SHARC_FAST_AUTO=threshold,min_insns[,blocks]` adds run-time selection.
pub fn from_env() -> Option<FastEngine> {
    let text = std::env::var("SHARC_FAST_REGIONS").unwrap_or_default();
    let pcs: Vec<u32> = text
        .split(',')
        .filter(|x| !x.trim().is_empty())
        .filter_map(|x| u32::from_str_radix(x.trim().trim_start_matches("0x"), 16).ok())
        .collect();
    let auto = auto_config();
    if pcs.is_empty() && auto.is_none() {
        return None;
    }
    let f = FastEngine::new(default_backend(), &pcs);
    Some(match auto {
        Some((t, m, b)) => f.with_auto(t, m, b),
        None => f,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Every writer id of a group is within `MAX_FLAG_ID` and round-trips.
    #[test]
    fn flag_ids_round_trip() {
        for g in 0..3 {
            let mut n = 0;
            for id in 1..=MAX_FLAG_ID + 4 {
                if let Some(k) = FlagKind::from_group_id(g, id) {
                    assert!(id <= MAX_FLAG_ID, "group {g} id {id} above MAX_FLAG_ID");
                    assert_eq!(k.group_id(), (g, id));
                    n += 1;
                }
            }
            assert!(n > 0);
        }
    }
}
