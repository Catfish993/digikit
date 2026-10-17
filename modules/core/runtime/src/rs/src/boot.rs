//! Boot-time loading (docs/plans/digikit-runtime.md decision 9, Phase 6; finding 37).
//!
//! `dkrt_boot` runs once per boot from the main-OS task, just before the MainScreenView constructor (`src/cf/tramp.s`
//! `dkrt_boot_hook`): the block read, the DSP and the block-frame interrupt work there and the machine view is not built yet.
//! It reads the selection record, decides about safe mode, raises the boot counter, and loads the selected modules in order
//! through the same loader as ever (a module that fails is skipped and reported; the others load). DSP code is sent with
//! `dl_send` and awaited by polling the state machine here (bounded); what has not finished when the bound is reached continues
//! in the UI-loop hook as before. The first UI-loop pass (`ui_reached`) clears the counter.
//!
//! The record is the only thing the runtime writes to the card, and only to its two sectors (`sel::SECTOR_A`/`SECTOR_B`).
use crate::{block_read, block_write, dsp, loader, st};
use core::ptr::{addr_of_mut, read_volatile};
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
};
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
    let ddr = |a: u32| (0x4000_0000..0x4800_0000).contains(&a) && a & 3 == 0;
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

/// First pass of the UI loop: the boot reached it, so the counter goes back to 0.
pub unsafe fn ui_reached() {
    let b = info();
    if b.ui_reached != 0 {
        return;
    }
    b.ui_reached = 1;
    if b.magic != INFO_MAGIC {
        return; // the boot hook did not run (an image patched without the boot site)
    }
    let r = REC;
    if REC_VALID && r.boot_counter != 0 {
        let mut r = r;
        r.boot_counter = 0;
        update(r, REC_COPY, true);
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
    };
}
