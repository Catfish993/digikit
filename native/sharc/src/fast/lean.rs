//! The lean entry contract of a CFG region (`SHARC_FAST_LEAN=1`): the same
//! decisions as `glue::run_cfg`'s entry checks, made cheaper.
//!
//! Every shortcut here is a memo of a pure predicate, so a hit gives the
//! answer the full evaluation would and a miss runs the full evaluation:
//!
//!  * register checks: duplicates on one register are merged into one masked
//!    compare (the checks are conjunctions of masked compares of the same
//!    8 bytes, so OR-ing masks and wants is the same conjunction);
//!  * windows: a window resolved earlier covers any later window inside it
//!    while `Mem::layout_gen` is the same (no page allocated or dropped, no
//!    present/dirty bit cleared; writes only set bits), see `lean_window`.

use super::glue::{RegChk, WinCache};
use super::ir::EntryChecks;
use super::{Base, Req, WinSpec};
use crate::rt::{St, V};
use std::cell::Cell;
use std::sync::OnceLock;

/// `SHARC_FAST_LEAN=1`: use the lean entry contract (read once).
pub fn enabled() -> bool {
    static ON: OnceLock<bool> = OnceLock::new();
    *ON.get_or_init(|| std::env::var("SHARC_FAST_LEAN").is_ok_and(|v| v == "1"))
}

#[inline(always)]
fn chk_ok(s: &St, chk: &[RegChk]) -> bool {
    let mut bad = 0u64;
    let base = std::ptr::from_ref(s).cast::<u8>();
    for c in chk {
        // SAFETY: `off` is the offset of a register of `St::r` (from
        // `glue::reg_checks`); V is 8 bytes of two u32 fields.
        let w = unsafe { base.add(c.off as usize).cast::<u64>().read_unaligned() };
        bad |= w & c.mask ^ c.want;
    }
    bad == 0
}

pub struct Lean {
    /// The checks a backend can run inside the kernel (`checks_entry`).
    pub entry: EntryChecks,
    /// The merged checks on the registers the window and `NwPlain` address
    /// computations read: done in Rust before those are evaluated even when
    /// the kernel checks the rest.
    base_chk: Box<[RegChk]>,
    chk: Box<[RegChk]>,
    /// Counters (reports): window cache hits and misses, interval misses.
    /// Two cached windows per window slot of the region, most recent first.
    pub wc: Box<[[Cell<WinCache>; 2]]>,
    pub win_hit: Cell<u64>,
    pub win_miss: Cell<u64>,
}

impl Lean {
    pub fn new(chk: &[RegChk], reqs: &[Req], wins: &[WinSpec]) -> Lean {
        let wc: Vec<[Cell<WinCache>; 2]> = wins
            .iter()
            .map(|_| [Cell::new(WinCache::NONE), Cell::new(WinCache::NONE)])
            .collect();
        let mut merged: Vec<RegChk> = Vec::new();
        for c in chk {
            // Same register and no contradicting bits: one compare.
            if let Some(m) = merged
                .iter_mut()
                .find(|m| m.off == c.off && (m.want ^ c.want) & m.mask & c.mask == 0)
            {
                m.mask |= c.mask;
                m.want |= c.want;
            } else {
                merged.push(*c);
            }
        }
        merged.sort_by_key(|c| c.off);
        let reg_off =
            |c: u8| (std::mem::offset_of!(St, r) + std::mem::size_of::<V>() * c as usize) as u32;
        let mut base_offs: Vec<u32> = Vec::new();
        let mut add = |b: Base| {
            if let Base::Reg(c) = b {
                base_offs.push(reg_off(c));
            }
        };
        for q in reqs {
            if let Req::NwPlain { base, .. } = *q {
                add(base);
            }
        }
        for w in wins {
            add(w.base);
            if let Some(a) = w.dynamic_anchor {
                add(Base::Reg(a));
            }
        }
        let base_chk: Vec<RegChk> = merged
            .iter()
            .filter(|c| base_offs.contains(&c.off))
            .copied()
            .collect();
        let known_word = super::glue::v_word(0, u32::MAX);
        let m_off = std::mem::offset_of!(V, m) as u32;
        let mut entry = EntryChecks::default();
        for c in &merged {
            if c.mask == known_word && c.want == known_word {
                entry.known_m.push(c.off + m_off);
            } else {
                entry.words.push((c.off, c.mask, c.want));
            }
        }
        Lean {
            wc: wc.into(),
            entry,
            base_chk: base_chk.into(),
            chk: merged.into(),
            win_hit: Cell::new(0),
            win_miss: Cell::new(0),
        }
    }

    /// `glue::regs_ok` on the merged compares.
    #[inline(always)]
    pub fn regs_ok(&self, s: &St) -> bool {
        chk_ok(s, &self.chk)
    }

    /// The merged checks on the address-base registers only.
    #[inline(always)]
    pub fn bases_ok(&self, s: &St) -> bool {
        chk_ok(s, &self.base_chk)
    }
}

/// The host pointer minus the window start for the window SPEC resolves to
/// over `[lo, hi)` (as `ctx.set64(win_ptr)` stores it), with the window's
/// resolved extent. A window inside the cached one reuses its pointer.
#[inline(always)]
pub fn lean_window(
    lean: &Lean,
    mem: &mut crate::mem::Mem,
    cache: &[Cell<WinCache>; 2],
    spec: &WinSpec,
    lo: u32,
    hi: u64,
) -> Option<u64> {
    for slot in cache {
        let c = slot.get();
        if c.gen_ == mem.layout_gen && c.lo <= lo && hi <= c.hi && !c.ptr.is_null() {
            if spec.write {
                mem.note_window_write(lo);
            }
            lean.win_hit.set(lean.win_hit.get() + 1);
            return Some((c.ptr as u64).wrapping_sub(c.lo as u64));
        }
    }
    lean.win_miss.set(lean.win_miss.get() + 1);
    // Resolve the whole page when it qualifies (a window that moves or grows
    // from call to call then stays inside the cached one), else just this
    // window. Either is a window `Mem::window` vouches for in full.
    let page = crate::mem::PAGE_SIZE as u64;
    let plo = lo as u64 & !(page - 1);
    let (clo, chi, ptr) = match (hi <= plo + page)
        .then(|| mem.window(plo as u32, page as u32, spec.read, spec.write))
        .flatten()
    {
        Some(p) => (plo as u32, plo + page, p),
        None => (
            lo,
            hi,
            mem.window(lo, (hi - lo as u64) as u32, spec.read, spec.write)?,
        ),
    };
    // Replace the older entry (slot 0 is the most recent).
    cache[1].set(cache[0].get());
    cache[0].set(WinCache {
        gen_: mem.layout_gen,
        lo: clo,
        hi: chi,
        ptr,
    });
    Some((ptr as u64).wrapping_sub(clo as u64))
}
