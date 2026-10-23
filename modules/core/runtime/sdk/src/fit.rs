//! The fit rule: does a module, from the needs its `.dkm` header declares, fit the capacity of the device (docs/plans/what-fits.md)?
//!
//! The runtime's CAPACITY entries (`dkrt_capacity`) give limit, used and largest free piece per resource; the header (docs/design/digikit-dkm.md,
//! "Needs") gives what a load takes. `fits_now` compares them and names the first short resource; `take` takes a module's needs out of a capacity,
//! so a set is checked by loading its modules in turn (the sum rule: fragmentation is not modelled, so it is conservative). The rule is written
//! again in apps/web/src/modules/dkfit.ts and tools/device/dkfit.py; `sdk/fit-vectors.json` keeps the three equal.
//!
//! The runtime's module set is fixed per session, so the menu checks a selection against the limits with the other ticked modules taken out
//! (the next boot loads them into an empty runtime), and a host checks against CAPACITY as the device reports it.

use crate::proto::*;
use crate::DkrtCap;

/// The arena's block header (`arena.rs`).
pub const ARENA_HDR: u32 = 16;

/// What a load takes of each resource, from the header of a `.dkm` (`DKM_NEED_*` offsets of `[dkm.needs]` in sdk/sdk.toml).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Needs {
    /// The file's size: it must fit the staging buffer.
    pub file_size: u32,
    pub arena: u32,
    pub arena_xfer: u32,
    pub dsp_area: u32,
    pub dsp_align: u32,
    pub machine_rows: u32,
    pub voice_rows: u32,
    pub param_rows: u32,
    pub page_rows: u32,
    pub fx_slots: u32,
}

fn be(h: &[u8], at: usize) -> u32 {
    u32::from_be_bytes([h[at], h[at + 1], h[at + 2], h[at + 3]])
}

impl Needs {
    /// The needs a `.dkm` declares, from its first `DKM_HEADER` bytes; None when the bytes are not a `.dkm` of this format.
    pub fn from_header(h: &[u8]) -> Option<Needs> {
        if h.len() < DKM_HEADER as usize || be(h, 0) != 0x444b_4d46 || be(h, 4) != DKM_FORMAT || be(h, 0x0c) != DKM_HEADER {
            return None;
        }
        Some(Needs {
            file_size: be(h, 8),
            arena: be(h, DKM_NEED_ARENA),
            arena_xfer: be(h, DKM_NEED_ARENA_XFER),
            dsp_area: be(h, DKM_NEED_DSP_AREA),
            dsp_align: be(h, DKM_NEED_DSP_ALIGN),
            machine_rows: be(h, DKM_NEED_MACHINE_ROWS),
            voice_rows: be(h, DKM_NEED_VOICE_ROWS),
            param_rows: be(h, DKM_NEED_PARAM_ROWS),
            page_rows: be(h, DKM_NEED_PAGE_ROWS),
            fx_slots: be(h, DKM_NEED_FX_SLOTS),
        })
    }

    /// DSP area bytes with the room a placement can lose to alignment (up to `dsp_align - 8` bytes).
    pub fn dsp_need(&self) -> u32 {
        if self.dsp_area == 0 {
            0
        } else {
            self.dsp_area + self.dsp_align.max(8) - 8
        }
    }
}

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

/// The entry of `kind` (all zero when the capacity has none: a resource the device does not have).
fn cap(caps: &[DkrtCap], kind: u8) -> DkrtCap {
    for c in caps {
        if c.kind == kind as u32 {
            return *c;
        }
    }
    DkrtCap { kind: kind as u32, limit: 0, used: 0, largest: 0 }
}

fn free(c: &DkrtCap) -> u32 {
    c.limit.saturating_sub(c.used)
}

/// None when the module fits `caps`, else the first short resource.
pub fn fits_now(n: &Needs, caps: &[DkrtCap]) -> Option<Short> {
    for kind in ORDER {
        let c = cap(caps, kind);
        // an `if` chain, not a `match`: dense arms become a PC-relative jump table, which is not ColdFire code
        let (need, room) = if kind == CAPK_STAGE {
            (n.file_size, c.largest)
        } else if kind == CAPK_MODULES || kind == CAPK_SELECTION {
            (1, free(&c))
        } else if kind == CAPK_DSP_AREA {
            (n.dsp_need(), free(&c).min(c.largest))
        } else if kind == CAPK_ARENA {
            // the module's block and the block of its DSP image are in the arena together until the transfer ends
            let total = n.arena + n.arena_xfer;
            if total > free(&c) {
                return Some(Short { kind, need: total, free: free(&c) });
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
            return Some(Short { kind, need, free: room });
        }
    }
    None
}

/// Take the module's needs out of `caps` (what is left once it is loaded). An entry the capacity lacks is ignored.
pub fn take(n: &Needs, caps: &mut [DkrtCap]) {
    let mut use_ = |kind: u8, amount: u32| {
        for c in caps.iter_mut() {
            if c.kind == kind as u32 {
                c.used += amount;
                c.largest = c.largest.saturating_sub(amount);
            }
        }
    };
    use_(CAPK_MODULES, 1);
    use_(CAPK_SELECTION, 1);
    use_(CAPK_ARENA, n.arena + n.arena_xfer);
    use_(CAPK_DSP_AREA, n.dsp_need());
    use_(CAPK_VOICE_ROWS, n.voice_rows);
    use_(CAPK_MACHINE_ROWS, n.machine_rows);
    use_(CAPK_PARAM_ROWS, n.param_rows);
    use_(CAPK_PAGE_ROWS, n.page_rows);
    use_(CAPK_FX_SLOTS, n.fx_slots);
}

#[cfg(test)]
mod tests;
