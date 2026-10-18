//! Boot-time loading (docs/plans/digikit-runtime.md decision 9, Phase 6; finding 37).
//!
//! `dkrt_boot` runs once per boot from the main-OS task, just before the MainScreenView constructor (`src/cf/tramp.s`
//! `dkrt_boot_hook`): the block read, the DSP and the block-frame interrupt work there and the machine view is not built yet.
//! It reads the selection record, decides about safe mode, raises the boot counter, and loads the selected modules in order
//! through the same loader as ever (a module that fails is skipped and reported; the others load). DSP code is sent with
//! `dl_send` and awaited by polling the state machine here (bounded); what has not finished when the bound is reached continues
//! in the UI-loop hook as before. The boot counter is cleared by `ui_pass` once the session has been stable: `STABLE_BLOCKS` DSP blocks (30 s)
//! after the first UI-loop pass (at the first pass itself in a safe boot, where no module runs). A boot that dies before then counts as bad.
//!
//! The record is the only thing the runtime writes to the card, and only to its two sectors (`sel::SECTOR_A`/`SECTOR_B`).
use crate::{block_read, block_write, dsp, loader, st};
use core::ptr::{addr_of, addr_of_mut, read_volatile};
use dkrt_sdk::sel::{self, Record};
use dkrt_sdk::*;

const INFO_MAGIC: u32 = 0x444b_4254; // "DKBT"

#[repr(C, align(16))]
pub struct Sector([u8; 512]);
#[no_mangle]
pub static mut DKRT_SECBUF: Sector = Sector([0; 512]);

#[no_mangle]
pub static mut DKRT_BOOT: DkrtBootInfo = DkrtBootInfo {
    magic: 0,
    version: 1,
    safe: 0,
    reason: 0,
    counter: 0,
    seq: 0,
    copy: 0,
    count: 0,
    loaded: 0,
    ui_reached: 0,
    results: [0; SEL_MAX],
    dsp_wait: 0,
    stable: 0,
};
/// The stable period in DSP blocks (`STABLE_BLOCKS`). A static so that the scenarios can shorten it (they poke it; nothing else writes it).
#[repr(C, align(4))]
pub struct Knob(pub u32); // a ColdFire gcc aligns a u32 to 2: the emulator's poke wants a word
#[no_mangle]
pub static mut DKRT_STABLE_BLOCKS: Knob = Knob(STABLE_BLOCKS);
/// The block count at the first UI-loop pass and the number of passes since (the fallback when the block counter does not move).
#[no_mangle]
pub static mut DKRT_UI_START: Knob = Knob(0);
static mut UI_PASSES: u32 = 0;
/// UI-loop passes after which a block counter that has not moved at all is taken for dead and the counter is cleared as in the first
/// pass of old (the UI loop runs 30 to 120 times a second: 1800 passes are 15 s to a minute).
const DEAD_CLOCK_PASSES: u32 = 1800;
/// The record this boot started from (or an empty one) and the bookkeeping the writes need.
static mut REC: Record = Record::empty();
static mut REC_VALID: bool = false;
static mut REC_COPY: u32 = 0;

extern "C" {
    /// The address of the 4-byte literal `pea <ui queue>` of the UI loop (anchor `dkrt.ui.queue_lit`).
    static dkrt_ui_queue_lit: u32;
}

pub fn info() -> &'static mut DkrtBootInfo {
    unsafe { &mut *addr_of_mut!(DKRT_BOOT) }
}

fn secbuf() -> *mut u8 {
    unsafe { addr_of_mut!(DKRT_SECBUF) as *mut u8 }
}

/// Read one copy (0 = A, 1 = B).
unsafe fn read_copy(i: u32) -> Option<Record> {
    let sector = if i == 0 { sel::SECTOR_A } else { sel::SECTOR_B };
    if block_read(sector, 512, secbuf()) != 0 {
        return None;
    }
    let b = &*(secbuf() as *const [u8; 512]);
    sel::decode(b)
}

/// The record in use: the valid copy with the higher sequence number (A when equal).
unsafe fn current() -> (Option<Record>, u32) {
    let a = read_copy(0);
    let b = read_copy(1);
    match (a, b) {
        (Some(x), Some(y)) => {
            if y.seq > x.seq {
                (Some(y), 1)
            } else {
                (Some(x), 0)
            }
        }
        (Some(x), None) => (Some(x), 0),
        (None, Some(y)) => (Some(y), 1),
        (None, None) => (None, 0),
    }
}

/// Write `r` to copy `i`. Only these two sectors are ever written.
unsafe fn write_copy(i: u32, r: &Record) -> i32 {
    let sector = if i == 0 { sel::SECTOR_A } else { sel::SECTOR_B };
    let b = &mut *(secbuf() as *mut [u8; 512]);
    sel::encode(r, b);
    if block_write(sector, 512, secbuf()) != 0 {
        return E_SEL_WRITE;
    }
    OK
}

unsafe fn path_ok(p: *const u8) -> bool {
    let mut n = 0;
    while n < SEL_PATH {
        let c = *p.add(n);
        if c == 0 {
            return n > 0;
        }
        if !(0x21..0x7f).contains(&c) {
            return false;
        }
        n += 1;
    }
    false
}

/// `dkrt_selection_get`.
pub unsafe fn selection_get(out: *mut DkrtSelection) -> i32 {
    let (r, _) = current();
    let o = &mut *out;
    let r = r.unwrap_or(Record::empty());
    o.count = r.count;
    o.flags = r.flags;
    o.paths = r.paths;
    OK
}

/// `dkrt_selection_set`: both copies, A then B, with the next sequence number. The boot counter carries over.
pub unsafe fn selection_set(s: *const DkrtSelection) -> i32 {
    let s = &*s;
    if s.count as usize > SEL_MAX {
        return E_SEL_COUNT;
    }
    let mut k = 0;
    while k < s.count as usize {
        if !path_ok(s.paths[k].as_ptr()) {
            return E_SEL_PATH;
        }
        k += 1;
    }
    let (cur, _) = current();
    let cur = cur.unwrap_or(Record::empty());
    let mut r = Record::empty();
    r.seq = cur.seq.wrapping_add(1);
    r.boot_counter = cur.boot_counter;
    r.stable_blocks = cur.stable_blocks;
    r.flags = s.flags & 1;
    r.count = s.count;
    k = 0;
    while k < s.count as usize {
        r.paths[k] = s.paths[k];
        k += 1;
    }
    let e = write_copy(0, &r);
    if e != OK {
        return e;
    }
    write_copy(1, &r)
}

/// `CMD_SELECT_ONE`: write copy `i` only, with `seq + 1` (the state of a power cut between the two writes).
pub unsafe fn selection_set_one(i: u32, s: *const DkrtSelection) -> i32 {
    let s = &*s;
    if s.count as usize > SEL_MAX || i > 1 {
        return E_SEL_COUNT;
    }
    let (cur, _) = current();
    let cur = cur.unwrap_or(Record::empty());
    let mut r = Record::empty();
    r.seq = cur.seq.wrapping_add(1);
    r.boot_counter = cur.boot_counter;
    r.stable_blocks = cur.stable_blocks;
    r.flags = s.flags & 1;
    r.count = s.count;
    let mut k = 0;
    while k < s.count as usize {
        r.paths[k] = s.paths[k];
        k += 1;
    }
    write_copy(i, &r)
}

/// Write the record with a changed counter/flags into the copy that is not the current one (the current one stays valid if
/// the write is cut).
unsafe fn update(mut r: Record, current_copy: u32, valid: bool) -> i32 {
    r.seq = if valid { r.seq.wrapping_add(1) } else { 1 };
    let target = if valid { 1 - current_copy } else { 0 };
    let e = write_copy(target, &r);
    if e == OK {
        REC = r;
        REC_VALID = true;
        REC_COPY = target;
    }
    e
}

/// Is the safe-mode key down among the events waiting in the UI queue? (The panel's held keys arrive as key-down events
/// shortly after reset; whether they are queued yet at this point is untested on a device **[O]**.)
unsafe fn key_held() -> bool {
    let lit = core::hint::black_box(&raw const dkrt_ui_queue_lit as usize) as u32;
    if lit == 0 {
        return false;
    }
    let q = read_volatile(lit as *const u32);
    // the firmware's objects live in the cached DDR window (`[board]` of the device files)
    let ddr = |a: u32| {
        (dkrt_sdk::device::DDR_CACHED_BASE..dkrt_sdk::device::DDR_CACHED_BASE + dkrt_sdk::device::DDR_BYTES).contains(&a) && a & 3 == 0
    };
    if !ddr(q) {
        return false;
    }
    let w = |a: u32, k: u32| read_volatile((a + 4 * k) as *const u32);
    let (count, mask, buf, rd) = (w(q, 1), w(q, 4), w(q, 5), w(q, 7));
    if count == 0 || count > 64 || mask > 0xffff || !ddr(buf) {
        return false;
    }
    let mut i = 0;
    while i < count {
        let item = read_volatile((buf + 4 * ((rd + i) & mask)) as *const u32);
        if ddr(item) && read_volatile(item as *const u8) == 0 {
            // a key event: +4 the key, +8 the state
            if w(item, 1) == SAFE_KEY_CODE && w(item, 2) != 0 {
                return true;
            }
        }
        i += 1;
    }
    false
}

/// The boot hook (once per boot, main-OS task).
#[no_mangle]
pub unsafe extern "C" fn dkrt_boot() {
    crate::init_once();
    let b = info();
    if b.magic == INFO_MAGIC {
        return; // the constructor ran once already: a second call is a bug, not a second boot
    }
    b.magic = INFO_MAGIC;
    b.version = 1;
    let (rec, copy) = current();
    let valid = rec.is_some();
    let mut r = rec.unwrap_or(Record::empty());
    b.seq = if valid { r.seq } else { 0 };
    b.copy = copy;
    b.counter = r.boot_counter;
    b.count = r.count;
    REC = r;
    REC_VALID = valid;
    REC_COPY = copy;
    if r.stable_blocks != 0 {
        DKRT_STABLE_BLOCKS.0 = r.stable_blocks;
    }
    // safe mode: the record asked for it, or the key, or too many boots that did not reach the UI loop
    let reason = if r.flags & 1 != 0 {
        SAFE_FLAG
    } else if key_held() {
        SAFE_KEY
    } else if r.boot_counter >= SAFE_AFTER {
        SAFE_COUNTER
    } else {
        SAFE_NONE
    };
    if reason != SAFE_NONE {
        b.safe = 1;
        b.reason = reason;
        if reason == SAFE_FLAG {
            // honoured once: clear the flag (the counter is left as it is; the UI loop clears it)
            r.flags &= !1;
            update(r, copy, valid);
        }
        let mut k = 0;
        while k < SEL_MAX {
            b.results[k] = E_SAFE_MODE;
            k += 1;
        }
        return;
    }
    // raise the counter before anything of a module runs; the first UI-loop pass clears it
    if r.count != 0 || r.boot_counter != 0 {
        r.boot_counter = r.boot_counter.wrapping_add(1);
        update(r, copy, valid);
    }
    // The frames that carry DSP code do not flow yet (they start with the sound engine, later in the task), so a module's CF part
    // goes live now and its DSP part follows over the UI-loop passes (`LOADING` in the report until then).
    loader::EARLY = true;
    let mut k = 0;
    while k < r.count as usize {
        loader::BOOT_IDX = k as u32;
        let res = loader::load(r.paths[k].as_ptr());
        st().last_result = res;
        b.results[k] = res;
        if res == OK {
            b.loaded += 1;
        }
        k += 1;
    }
    loader::EARLY = false;
    loader::BOOT_IDX = u32::MAX;
}

/// The DSP part of the module loaded for selection index `idx` is over with result `r` (called by the DSP loader).
pub unsafe fn note_result(idx: u32, r: i32) {
    if (idx as usize) < SEL_MAX {
        let b = info();
        b.results[idx as usize] = r;
        if r == OK {
            b.loaded += 1;
        }
    }
}

/// Every pass of the UI loop (`dkrt_tick`). The first pass notes the block count; the boot counter goes back to 0 when the session has run
/// `DKRT_STABLE_BLOCKS` blocks past it, or at once in a safe boot (nothing of a module runs there). A hang or a fault in between leaves
/// the counter raised, so the next boots count it: two such boots in a row start the third in safe mode.
pub unsafe fn ui_pass() {
    let b = info();
    if b.stable != 0 {
        return;
    }
    if b.magic != INFO_MAGIC {
        b.ui_reached = 1;
        b.stable = 1;
        return; // the boot hook did not run (an image patched without the boot site)
    }
    let now = crate::fw_blocks().unwrap_or(0);
    if b.ui_reached == 0 {
        b.ui_reached = 1;
        DKRT_UI_START.0 = now;
        UI_PASSES = 0;
    }
    UI_PASSES = UI_PASSES.wrapping_add(1);
    let elapsed = now.wrapping_sub(DKRT_UI_START.0);
    let period = core::ptr::read_volatile(addr_of!(DKRT_STABLE_BLOCKS.0));
    let dead = elapsed == 0 && UI_PASSES >= DEAD_CLOCK_PASSES;
    if b.safe != 0 || elapsed >= period || dead {
        b.stable = 1;
        // The record as the card holds it now, not the one this boot started from: the menu may have saved a selection since.
        let (cur, copy) = current();
        if let Some(mut r) = cur {
            if r.boot_counter != 0 {
                r.boot_counter = 0;
                update(r, copy, true);
            }
        }
    }
}

pub unsafe fn boot_info(out: *mut DkrtBootInfo) {
    let b = info();
    let o = &mut *out;
    *o = DkrtBootInfo {
        magic: b.magic,
        version: b.version,
        safe: b.safe,
        reason: b.reason,
        counter: b.counter,
        seq: b.seq,
        copy: b.copy,
        count: b.count,
        loaded: b.loaded,
        ui_reached: b.ui_reached,
        results: b.results,
        dsp_wait: b.dsp_wait,
        stable: b.stable,
    };
}
