//! Opt-in per-entry dispatch costs for coupled-run diagnostic builds.

/// Cumulative cost of dispatching one entry PC in a run-time tier.
///
/// `instructions` is the `icount` delta across the invocation. A fast-tier
/// call is recorded even when it returns `None` or makes no progress.
#[derive(Clone, Copy, Debug, Default)]
pub struct RegionCost {
    pub calls: u64,
    pub zero_progress_calls: u64,
    pub instructions: u64,
    pub wall_ns: u64,
}

/// Cumulative costs for the two dispatch tiers at one entry PC.
#[derive(Clone, Copy, Debug, Default)]
pub struct RegionEntryProfile {
    pub fast: RegionCost,
    pub aot: RegionCost,
}

/// Sparse pages over the SHARC+'s 24-bit short-word PC space.
#[derive(Clone, Debug)]
pub struct RegionProfile {
    pages: Vec<Option<Box<[RegionEntryProfile; 256]>>>,
}

impl Default for RegionProfile {
    fn default() -> Self {
        Self {
            pages: vec![None; 1 << 16],
        }
    }
}

impl RegionProfile {
    pub(crate) fn record(&mut self, pc: u32, fast: bool, instructions: u64, wall_ns: u64) {
        let pc = (pc & 0x00ff_ffff) as usize;
        let page = &mut self.pages[pc >> 8];
        let entry = &mut page.get_or_insert_with(|| Box::new([RegionEntryProfile::default(); 256]))
            [pc & 0xff];
        let cost = if fast {
            &mut entry.fast
        } else {
            &mut entry.aot
        };
        cost.calls = cost
            .calls
            .checked_add(1)
            .expect("region profile calls overflow");
        if instructions == 0 {
            cost.zero_progress_calls = cost
                .zero_progress_calls
                .checked_add(1)
                .expect("region profile zero-progress calls overflow");
        }
        cost.instructions = cost
            .instructions
            .checked_add(instructions)
            .expect("region profile instructions overflow");
        cost.wall_ns = cost
            .wall_ns
            .checked_add(wall_ns)
            .expect("region profile wall time overflow");
    }

    /// Entries with at least one measured dispatch, in short-word PC order.
    pub fn entries(&self) -> impl Iterator<Item = (u32, &RegionEntryProfile)> {
        self.pages
            .iter()
            .enumerate()
            .flat_map(|(page_index, page)| {
                page.iter().flat_map(move |entries| {
                    entries
                        .iter()
                        .enumerate()
                        .filter_map(move |(offset, entry)| {
                            (entry.fast.calls != 0 || entry.aot.calls != 0)
                                .then_some((((page_index << 8) | offset) as u32, entry))
                        })
                })
            })
    }
}
