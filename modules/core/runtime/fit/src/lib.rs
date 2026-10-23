//! The fit rule: does a module, from the needs inferred from its `.dkm` (`dkneeds`), fit the capacity of the device (docs/plans/what-fits.md)?
//!
//! The runtime's CAPACITY entries (`dkrt_capacity`) give limit, used and largest free piece per resource; `dkneeds::needs_of` (docs/design/digikit-dkm.md,
//! "Needs are inferred from the contents") gives what a load takes. `fits_now` compares them and names the first short resource; `take` takes a module's needs out of a capacity,
//! so a set is checked by loading its modules in turn (the sum rule: fragmentation is not modelled, so it is conservative). The rule is written
//! again in apps/web/src/modules/dkfit.ts and tools/device/dkfit.py; `sdk/fit-vectors.json` keeps the three equal.
//!
//! The runtime's module set is fixed per session, so the menu checks a selection against the limits with the other ticked modules taken out
//! (the next boot loads them into an empty runtime), and a host checks against CAPACITY as the device reports it.
#![no_std]

use dkrt_sdk::proto::*;
use dkrt_sdk::DkrtCap;

pub use dkneeds::{Needs, ARENA_HDR};

/// The first short resource: its CAPACITY kind (`CAPK_*`), what the module needs and what is free (bytes, or a count).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Short {
    pub kind: u8,
    pub need: u32,
    pub free: u32,
}

/// The order the first short resource is looked for in: the loader's own order of refusals, with the slots first.
const ORDER: [u8; 10] = [
    CAPK_STAGE,
    CAPK_MODULES,
    CAPK_SELECTION,
    CAPK_DSP_AREA,
    CAPK_ARENA,
    CAPK_VOICE_ROWS,
    CAPK_MACHINE_ROWS,
    CAPK_PARAM_ROWS,
    CAPK_PAGE_ROWS,
    CAPK_FX_SLOTS,
];

/// The entry of `kind`; None when the capacity has none (an older runtime without that kind): it is not judged.
fn cap(caps: &[DkrtCap], kind: u8) -> Option<DkrtCap> {
    for c in caps {
        if c.kind == kind as u32 {
            return Some(*c);
        }
    }
    None
}

fn free(c: &DkrtCap) -> u32 {
    c.limit.saturating_sub(c.used)
}

/// None when the module fits `caps`, else the first short resource.
pub fn fits_now(n: &Needs, caps: &[DkrtCap]) -> Option<Short> {
    for kind in ORDER {
        let Some(c) = cap(caps, kind) else {
            continue;
        };
        // an `if` chain, not a `match`: dense arms become a PC-relative jump table, which is not ColdFire code
        let (need, room) = if kind == CAPK_STAGE {
            (n.file_size, c.largest)
        } else if kind == CAPK_MODULES || kind == CAPK_SELECTION {
            (1, free(&c))
        } else if kind == CAPK_DSP_AREA {
            (n.dsp_need(), free(&c).min(c.largest))
        } else if kind == CAPK_ARENA {
            // the module's block and the block of its DSP image are in the arena together until the transfer ends
            let total = n.arena.saturating_add(n.arena_xfer);
            if total > free(&c) {
                return Some(Short {
                    kind,
                    need: total,
                    free: free(&c),
                });
            }
            (n.arena.max(n.arena_xfer), c.largest + ARENA_HDR)
        } else if kind == CAPK_VOICE_ROWS {
            (n.voice_rows, free(&c))
        } else if kind == CAPK_MACHINE_ROWS {
            (n.machine_rows, free(&c))
        } else if kind == CAPK_PARAM_ROWS {
            (n.param_rows, free(&c))
        } else if kind == CAPK_PAGE_ROWS {
            (n.page_rows, free(&c))
        } else {
            (n.fx_slots, free(&c))
        };
        if need > room {
            return Some(Short {
                kind,
                need,
                free: room,
            });
        }
    }
    None
}

/// Make `caps` the capacity of a runtime that has loaded nothing: every resource unused and its whole limit free, as the next boot sees it (the
/// module set is fixed per session). The arena starts one block header short of its limit and the DSP area 8 bytes short (its first range starts at
/// an offset that is not 8-aligned); the staging buffer is not a resource that a load uses up.
pub fn empty(caps: &mut [DkrtCap]) {
    for c in caps.iter_mut() {
        if c.kind == CAPK_STAGE as u32 {
            continue;
        }
        c.used = 0;
        c.largest = if c.kind == CAPK_ARENA as u32 {
            c.limit.saturating_sub(ARENA_HDR)
        } else if c.kind == CAPK_DSP_AREA as u32 {
            c.limit.saturating_sub(8)
        } else {
            c.limit
        };
    }
}

/// Take the module's needs out of `caps` (what is left once it is loaded). An entry the capacity lacks is ignored.
pub fn take(n: &Needs, caps: &mut [DkrtCap]) {
    let mut use_ = |kind: u8, amount: u32| {
        for c in caps.iter_mut() {
            if c.kind == kind as u32 {
                c.used = c.used.saturating_add(amount);
                c.largest = c.largest.saturating_sub(amount);
            }
        }
    };
    use_(CAPK_MODULES, 1);
    use_(CAPK_SELECTION, 1);
    use_(CAPK_ARENA, n.arena.saturating_add(n.arena_xfer));
    use_(CAPK_DSP_AREA, n.dsp_need());
    use_(CAPK_VOICE_ROWS, n.voice_rows);
    use_(CAPK_MACHINE_ROWS, n.machine_rows);
    use_(CAPK_PARAM_ROWS, n.param_rows);
    use_(CAPK_PAGE_ROWS, n.page_rows);
    use_(CAPK_FX_SLOTS, n.fx_slots);
}

#[cfg(test)]
mod tests;
