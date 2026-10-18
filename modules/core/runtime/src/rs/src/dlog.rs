//! The debug log (docs/findings/46-digikit-debug.md): a RAM ring of events that a host reads over the SysEx protocol (LOG, PEEK), plus the
//! DSP diagnostics poll and its heartbeat watch.
//!
//! An event is 32 bytes: a sequence number (from 1; 0 is no event), the firmware's block counter (1500 per second) as the time, a kind
//! (`dkrt_sdk::logk`), flags (bit 0: an error) and five argument words. With the DEBUG setting OFF only error events are kept. Nothing is
//! ever sent by itself: the host asks (LOG), and the runtime answers; with nobody asking the log costs the appends and one poll per second.
//!
//! Appends mask interrupts for a few instructions: the UI task, the MIDI task and the crash handler all append.
use crate::{arena, fw_blocks, irq_mask, irq_restore, st};
use core::ptr::{addr_of, addr_of_mut, read_volatile, write_volatile};
use dkrt_sdk::*;

pub const RING: usize = 512;

#[repr(C)]
#[derive(Clone, Copy)]
pub struct Ev {
    pub seq: u32,
    pub time: u32,
    pub kind: u16,
    pub flags: u16,
    pub a: [u32; 5],
}
const EMPTY: Ev = Ev { seq: 0, time: 0, kind: 0, flags: 0, a: [0; 5] };

#[no_mangle]
pub static mut DKRT_LOG: [Ev; RING] = [EMPTY; RING];
/// Events appended so far (the newest event's sequence number).
#[no_mangle]
pub static mut DKRT_LOG_COUNT: u32 = 0;
/// 0: the debug log keeps every event; 1: errors only (`sel::FLAG_DEBUG_OFF` of the selection record, so the card's zero means ON).
static mut DEBUG_OFF: u32 = 0;

pub fn debug_on() -> bool {
    unsafe { read_volatile(addr_of!(DEBUG_OFF)) == 0 }
}

pub fn set_debug(on: bool) {
    unsafe { write_volatile(addr_of_mut!(DEBUG_OFF), if on { 0 } else { 1 }) }
}

/// Append an event. An event without `logk::F_ERROR` is dropped when the debug log is off.
pub unsafe fn ev(kind: u16, flags: u16, a: [u32; 5]) {
    if flags & logk::F_ERROR == 0 && !debug_on() {
        return;
    }
    let time = fw_blocks().unwrap_or(0);
    let sr = irq_mask();
    let n = read_volatile(addr_of!(DKRT_LOG_COUNT)).wrapping_add(1);
    let e = &mut (*addr_of_mut!(DKRT_LOG))[(n as usize - 1) % RING];
    e.seq = 0; // a reader that is cut off here sees no event, not a half-written one
    e.time = time;
    e.kind = kind;
    e.flags = flags;
    e.a = a;
    e.seq = n;
    write_volatile(addr_of_mut!(DKRT_LOG_COUNT), n);
    irq_restore(sr);
}

/// The first four bytes of `s` from `at` as a big-endian word (0 past the end).
pub unsafe fn word_of(s: *const u8, at: usize, max: usize) -> u32 {
    let mut v = 0u32;
    let mut i = 0;
    while i < 4 {
        let b = if at + i < max { *s.add(at + i) } else { 0 };
        v = (v << 8) | b as u32;
        i += 1;
    }
    v
}

/// Sequence numbers of the oldest and newest event kept (0, 0: none).
pub fn range() -> (u32, u32) {
    let n = unsafe { read_volatile(addr_of!(DKRT_LOG_COUNT)) };
    if n == 0 {
        return (0, 0);
    }
    let oldest = if n as usize > RING { n - RING as u32 + 1 } else { 1 };
    (oldest, n)
}

/// Copy the event with sequence `seq` to `dst` as 32 big-endian bytes; false if it is no longer (or not yet) in the ring.
pub unsafe fn copy_event(seq: u32, dst: *mut u8) -> bool {
    let (oldest, newest) = range();
    if seq == 0 || seq < oldest || seq > newest {
        return false;
    }
    let sr = irq_mask();
    let e = (*addr_of!(DKRT_LOG))[(seq as usize - 1) % RING];
    irq_restore(sr);
    if e.seq != seq {
        return false;
    }
    crate::wr32(dst, e.seq);
    crate::wr32(dst.add(4), e.time);
    *dst.add(8) = (e.kind >> 8) as u8;
    *dst.add(9) = e.kind as u8;
    *dst.add(10) = (e.flags >> 8) as u8;
    *dst.add(11) = e.flags as u8;
    let mut i = 0;
    while i < 5 {
        crate::wr32(dst.add(12 + 4 * i), e.a[i]);
        i += 1;
    }
    true
}

// ---- the periodic part (UI task, once per pass) ------------------------------------------------------------------------------

extern "C" {
    /// `core/dsp-link`'s diagnostics: `int dl_diag(u32 *out, u32 n)` writes up to `n` words and returns how many (negative: none). It resolves to 0
    /// when the image has no such function (`[object] optional`).
    fn dl_diag();
}

/// Words asked of `dl_diag`, and the word of its output that the DSP counts up while it runs (the heartbeat). The layout is `core/dsp-link`'s
/// (its README): word 0 is `0xD1A6` with the count in the high half, word 1 the link hook's block beat, word 2 the voice hook's, then the
/// context registers. If it moves the heartbeat, change `DIAG_HEARTBEAT` here and nothing else.
pub const DIAG_WORDS: usize = 14;
pub const DIAG_HEARTBEAT: usize = 1;
/// The low half of word 0 of a diagnostics block.
pub const DIAG_MAGIC: u32 = 0xd1a6;
/// The DSP diagnostics are read once a second (blocks), the health snapshot every ten seconds, the heap walk in every fourth snapshot.
const DIAG_EVERY: u32 = 1500;
const HEALTH_EVERY: u32 = 15000;
/// The heartbeat must have moved within this many blocks (3 s) of the block counter's advance; a heartbeat that never moved gets 20 s.
const STALL_BLOCKS: u32 = 4500;
const NEVER_BLOCKS: u32 = 30000;

struct Watch {
    next_diag: u32,
    next_health: u32,
    snapshots: u32,
    /// `dl_diag` is in the image (decided at the first poll).
    probed: bool,
    present: bool,
    seen_alive: bool,
    hb: u32,
    hb_valid: bool,
    changed_at: u32,
    first_at: u32,
    last: [u32; DIAG_WORDS],
    n: u32,
}
static mut W: Watch = Watch {
    next_diag: 0,
    next_health: 0,
    snapshots: 0,
    probed: false,
    present: false,
    seen_alive: false,
    hb: 0,
    hb_valid: false,
    changed_at: 0,
    first_at: 0,
    last: [0; DIAG_WORDS],
    n: 0,
};
/// 1 once the watch decided the DSP stopped. Read by `dkrt_diag`, the proofs and ABOUT.
#[no_mangle]
pub static mut DKRT_DSP_STOPPED: u32 = 0;

fn due(now: u32, at: u32) -> bool {
    (now.wrapping_sub(at) as i32) >= 0
}

pub fn dsp_stopped() -> bool {
    unsafe { read_volatile(addr_of!(DKRT_DSP_STOPPED)) != 0 }
}

pub fn heartbeat() -> (bool, u32) {
    unsafe { (W.present && W.hb_valid, W.hb) }
}

/// Called once per UI-loop pass.
pub unsafe fn tick() {
    let Some(now) = fw_blocks() else { return };
    let w = &mut *addr_of_mut!(W);
    if !w.probed {
        w.probed = true;
        w.present = core::hint::black_box(dl_diag as usize) != 0;
        w.next_diag = now.wrapping_add(DIAG_EVERY);
        w.next_health = now.wrapping_add(HEALTH_EVERY);
        w.first_at = now;
        w.changed_at = now;
        if !w.present {
            ev(logk::DSP_ABSENT, 0, [0; 5]);
        }
        return;
    }
    if due(now, w.next_diag) {
        w.next_diag = now.wrapping_add(DIAG_EVERY);
        if w.present {
            poll_diag(w, now);
        }
    }
    if due(now, w.next_health) {
        w.next_health = now.wrapping_add(HEALTH_EVERY);
        snapshot(w);
    }
}

unsafe fn poll_diag(w: &mut Watch, now: u32) {
    let f: unsafe extern "C" fn(*mut u32, u32) -> i32 = core::mem::transmute(dl_diag as unsafe extern "C" fn());
    let mut buf = [0u32; DIAG_WORDS];
    let r = f(buf.as_mut_ptr(), DIAG_WORDS as u32);
    if r <= 0 {
        return;
    }
    let n = (r as usize).min(DIAG_WORDS);
    w.n = n as u32;
    w.last = buf;
    // log the words raw, four to an event: argument 0 is the count and the chunk
    let mut c = 0;
    while c * 4 < n {
        let mut a = [(n as u32) << 8 | c as u32, 0, 0, 0, 0];
        let mut j = 0;
        while j < 4 {
            if c * 4 + j < n {
                a[1 + j] = buf[c * 4 + j];
            }
            j += 1;
        }
        ev(logk::DSP_DIAG, 0, a);
        c += 1;
    }
    // the heartbeat watch
    if n > DIAG_HEARTBEAT && buf[0] & 0xffff == DIAG_MAGIC {
        let hb = buf[DIAG_HEARTBEAT];
        if !w.hb_valid || hb != w.hb {
            if w.hb_valid {
                w.seen_alive = true;
            }
            w.hb = hb;
            w.hb_valid = true;
            w.changed_at = now;
            if dsp_stopped() {
                // it moves again
                write_volatile(addr_of_mut!(DKRT_DSP_STOPPED), 0);
                ev(logk::DSP_STOPPED, 0, [hb, 0, 0, 0, 1]);
            }
        } else if !dsp_stopped() {
            let still = now.wrapping_sub(w.changed_at);
            let limit = if w.seen_alive { STALL_BLOCKS } else { NEVER_BLOCKS };
            if still >= limit {
                write_volatile(addr_of_mut!(DKRT_DSP_STOPPED), 1);
                ev(logk::DSP_STOPPED, logk::F_ERROR, [hb, buf[0], buf[1], buf[2], buf[3]]);
                list_modules();
            }
        }
    }
}

/// The loaded modules, one event each (`logk::MOD_LIST`, an error event when the DSP stopped): index, slots mask, 12 bytes of name.
unsafe fn list_modules() {
    let s = st();
    let mut i = 0;
    while i < MAX_MODULES_N {
        let m = &s.modules[i];
        if m.used != 0 {
            ev(
                logk::MOD_LIST,
                logk::F_ERROR,
                [i as u32, m.slot_mask, word_of(m.name.as_ptr(), 0, 32), word_of(m.name.as_ptr(), 4, 32), word_of(m.name.as_ptr(), 8, 32)],
            );
        }
        i += 1;
    }
}
const MAX_MODULES_N: usize = crate::MAX_MODULES;

/// A health snapshot: CPU and DSP load, arena free, and (every fourth time) the free firmware heap in KiB.
unsafe fn snapshot(w: &mut Watch) {
    let walk = w.snapshots % 4 == 0;
    w.snapshots = w.snapshots.wrapping_add(1);
    if !debug_on() {
        return;
    }
    let (afree, _) = arena::stats();
    let mut a = [0u32; 5];
    a[3] = afree;
    a[4] = 0xffff_ffff;
    if crate::health_present() {
        let mut h: DkrtHealth = core::mem::zeroed();
        crate::dkrt_health_light(&mut h, walk);
        a[0] = (h.flags << 16) | h.cpu_x10.min(0xffff);
        a[1] = h.dsp_avg_x10;
        a[2] = h.dsp_max_x10;
        if h.flags & HEALTH_HEAP != 0 {
            a[4] = h.heap_free / 1024;
        }
    }
    ev(logk::HEALTH, 0, a);
}

/// The last words `dl_diag` returned and how many (for the proofs).
pub fn last_diag() -> ([u32; DIAG_WORDS], u32) {
    unsafe { (W.last, W.n) }
}
