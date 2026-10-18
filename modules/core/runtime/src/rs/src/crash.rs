//! CPU exception capture (docs/findings/46-digikit-debug.md).
//!
//! The firmware's default exception handler loads a function pointer from a RAM word (anchor `dkrt.fw.exc_hook_lit` is the 4-byte literal of
//! that load) and calls it with the stack pointer, which points at the hardware exception frame; the firmware's own function there prints
//! `EXCEPTION ...` and executes `halt`, so a fault hangs the device until the power is cycled (finding 37). The runtime points the word at
//! `dkrt_exc_hook` (src/cf/tramp.s: it saves d2-d7 and a2-a6, which are still the faulting code's, and calls `dkrt_exc`). `dkrt_exc` writes
//! a crash record into the keep area (`KEEP_BASE`, above `cf.expansion`, which neither the firmware nor the boot code writes), flushes it to RAM
//! and soft-resets, the way `dkrt_restart` does, instead of halting.
//!
//! At the next boot `at_boot` finds the record, turns it into log events (so that `dklog crash` can read it back over the SysEx protocol), clears
//! it and tells the boot code, which counts the crashed boot as bad for safe mode. Whether RAM survives a soft reset on the real device is
//! open **[O]**: a probe word in the same area (`keep_probe`) logs, at every boot, whether the word from the boot before is still there; a
//! restart from the menu is the same reset, so one restart tells.
use crate::dlog;
use crate::{dkrt_flush, dkrt_soft_reset, st};
use core::ptr::{addr_of, addr_of_mut, read_volatile, write_volatile};
use dkrt_sdk::device::{DDR_BYTES, DDR_CACHED_BASE, KEEP_BASE, KEEP_BYTES};
use dkrt_sdk::store::crc32_of;
use dkrt_sdk::*;

pub const MAGIC: u32 = 0x4352_5348; // "CRSH"
const VERSION: u32 = 1;
const PROBE_MAGIC: u32 = 0x4b45_4550; // "KEEP"
/// Where the probe sits in the keep area (the record is below it).
const PROBE_AT: u32 = 0xf00;

// word indices of the record
const W_MAGIC: usize = 0;
const W_VERSION: usize = 1;
const W_TIME: usize = 2;
const W_VEC: usize = 3; // vector | format << 8
const W_SR: usize = 4;
const W_PC: usize = 5;
const W_A7: usize = 6;
const W_STABLE: usize = 7;
const W_ENTERED: usize = 8; // bit n: a task is inside slot n
const W_MODULE: usize = 9; // index of the module whose code holds the PC, u32::MAX: none
const W_FRAME0: usize = 10; // the first frame word as the CPU pushed it
const W_FAULTS: usize = 11; // exceptions seen since the handler was installed (a fault inside the handler)
const W_NAME: usize = 12; // 4 words: 16 bytes of the module's name
const W_REGS: usize = 16; // d0-d7, a0-a7
const W_STACK: usize = 32; // 32 words from the interrupted stack pointer
const STACK_WORDS: usize = 32;
const W_TAILN: usize = 64;
const W_TAIL: usize = 65; // 8 events of 8 words
pub const TAIL: usize = 8;
const W_CRC: usize = W_TAIL + TAIL * 8;
const REC_WORDS: usize = W_CRC + 1;

extern "C" {
    /// The 4-byte literal in the firmware's default exception handler whose value is the address of the hook word (anchor `dkrt.fw.exc_hook_lit`).
    static dkrt_exc_lit: u32;
    /// `src/cf/tramp.s`.
    fn dkrt_exc_hook();
}

fn rec() -> *mut u32 {
    KEEP_BASE as usize as *mut u32
}

/// What `at_boot` found, kept for `dkrt_diag`, the proofs and the boot code.
#[no_mangle]
pub static mut DKRT_CRASH: CrashSeen = CrashSeen { found: 0, pc: 0, vec: 0, module: u32::MAX, stable: 0, ram_kept: 0, installed: 0, bad: 0 };

#[repr(C)]
pub struct CrashSeen {
    pub found: u32,
    pub pc: u32,
    pub vec: u32,
    pub module: u32,
    /// The crashed boot had cleared its boot counter (it was past the stable period) when it crashed.
    pub stable: u32,
    /// The probe word of the boot before was in RAM.
    pub ram_kept: u32,
    /// The hook word has been pointed at `dkrt_exc_hook` (1) / the anchor did not lead to a RAM word (0).
    pub installed: u32,
    /// A record was there but failed its CRC.
    pub bad: u32,
}

pub fn seen() -> &'static CrashSeen {
    unsafe { &*addr_of!(DKRT_CRASH) }
}

/// The address of the hook word the anchor's literal holds, when it lies in DDR and is word aligned.
unsafe fn hook_word() -> Option<*mut u32> {
    let lit = core::hint::black_box(addr_of!(dkrt_exc_lit) as usize) as u32;
    if lit == 0 {
        return None;
    }
    let a = read_volatile(lit as *const u32);
    if !(DDR_CACHED_BASE..DDR_CACHED_BASE + DDR_BYTES).contains(&a) || a & 3 != 0 {
        return None;
    }
    Some(a as usize as *mut u32)
}

/// Point the firmware's exception hook word at `dkrt_exc_hook`. Called every UI pass: the firmware sets the word in its own boot, so the first
/// call must come after that, and a second setting would be caught by the next pass.
pub unsafe fn install() {
    let Some(w) = hook_word() else { return };
    let ours = core::hint::black_box(dkrt_exc_hook as usize) as u32;
    if read_volatile(w) != ours {
        write_volatile(w, ours);
        (*addr_of_mut!(DKRT_CRASH)).installed = 1;
    }
}

static mut IN_EXC: u32 = 0;

/// The exception handler proper (from `dkrt_exc_hook`): `frame` is the hardware exception frame (format/vector/status word, then the PC); the
/// four words below it are d0, d1, a0, a1 as the firmware's default handler saved them; `saved` points at d2-d7, a2-a6 as saved by the hook.
/// Never returns.
#[no_mangle]
pub unsafe extern "C" fn dkrt_exc(frame: *const u32, saved: *const u32) -> ! {
    let r = rec();
    let faults = read_volatile(addr_of!(IN_EXC));
    write_volatile(addr_of_mut!(IN_EXC), faults + 1);
    if faults != 0 {
        // a fault inside the handler: keep the first record (with the count) and reset
        write_volatile(r.add(W_FAULTS), faults + 1);
        dkrt_flush(KEEP_BASE, 64);
        dkrt_soft_reset()
    }
    write_volatile(r.add(W_MAGIC), 0); // an older record is not valid while this one is written
    let w0 = *frame;
    let pc = *frame.add(1);
    let fmt = w0 >> 28;
    let vec = (w0 >> 18) & 0xff;
    write_volatile(r.add(W_VERSION), VERSION);
    write_volatile(r.add(W_TIME), crate::fw_blocks().unwrap_or(0));
    write_volatile(r.add(W_VEC), vec | fmt << 8);
    write_volatile(r.add(W_SR), w0 & 0xffff);
    write_volatile(r.add(W_PC), pc);
    write_volatile(r.add(W_FRAME0), w0);
    write_volatile(r.add(W_FAULTS), 1);
    // the interrupted stack pointer: the frame, 8 bytes, and the alignment padding the CPU recorded in the format
    let a7 = (frame as u32).wrapping_add(8).wrapping_add(fmt.wrapping_sub(4) & 3);
    write_volatile(r.add(W_A7), a7);
    // registers: d0, d1, a0, a1 below the frame (the default handler's movem), d2-d7, a2-a6 from the hook
    let mut i = 0;
    while i < 16 {
        write_volatile(r.add(W_REGS + i), 0);
        i += 1;
    }
    write_volatile(r.add(W_REGS), *frame.sub(4));
    write_volatile(r.add(W_REGS + 1), *frame.sub(3));
    write_volatile(r.add(W_REGS + 8), *frame.sub(2));
    write_volatile(r.add(W_REGS + 9), *frame.sub(1));
    i = 0;
    while i < 6 {
        write_volatile(r.add(W_REGS + 2 + i), *saved.add(i));
        i += 1;
    }
    i = 0;
    while i < 5 {
        write_volatile(r.add(W_REGS + 10 + i), *saved.add(6 + i));
        i += 1;
    }
    write_volatile(r.add(W_REGS + 15), a7);
    // 32 stack words, when the pointer is in DDR and they fit
    i = 0;
    while i < STACK_WORDS {
        let at = a7.wrapping_add(4 * i as u32);
        let ok = a7 & 3 == 0
            && a7 >= DDR_CACHED_BASE
            && at < DDR_CACHED_BASE + DDR_BYTES - 4;
        write_volatile(r.add(W_STACK + i), if ok { read_volatile(at as usize as *const u32) } else { 0 });
        i += 1;
    }
    // the state of the runtime: boot stable, tasks in slots, the module that holds the PC
    let s = st();
    write_volatile(r.add(W_STABLE), crate::boot::info().stable);
    let mut mask = 0u32;
    i = 0;
    while i < SLOTS {
        if read_volatile(addr_of!(s.entered[i])) != 0 {
            mask |= 1 << i;
        }
        i += 1;
    }
    write_volatile(r.add(W_ENTERED), mask);
    write_volatile(r.add(W_MODULE), u32::MAX);
    i = 0;
    while i < 4 {
        write_volatile(r.add(W_NAME + i), 0);
        i += 1;
    }
    let mut m = 0;
    while m < crate::MAX_MODULES {
        let md = &s.modules[m];
        if md.used != 0 && pc >= md.base && pc < md.base.wrapping_add(md.size) {
            write_volatile(r.add(W_MODULE), m as u32);
            i = 0;
            while i < 4 {
                write_volatile(r.add(W_NAME + i), dlog::word_of(md.name.as_ptr(), 4 * i, 32));
                i += 1;
            }
            break;
        }
        m += 1;
    }
    // the last events of the log
    let (_, newest) = dlog::range();
    let mut n = 0u32;
    while n < TAIL as u32 && n < newest {
        let seq = newest - n;
        let mut b = [0u8; 32];
        if dlog::copy_event(seq, b.as_mut_ptr()) {
            let mut k = 0;
            while k < 8 {
                write_volatile(r.add(W_TAIL + 8 * n as usize + k), crate::rd32(b.as_ptr().add(4 * k)));
                k += 1;
            }
            n += 1;
        } else {
            break;
        }
    }
    write_volatile(r.add(W_TAILN), n);
    let mut v = [0u32; REC_WORDS];
    i = 0;
    while i < W_CRC {
        v[i] = read_volatile(r.add(i));
        i += 1;
    }
    v[W_MAGIC] = MAGIC; // the CRC covers the magic, which is written last
    let crc = crc32_of(v.as_ptr() as *const u8, W_CRC * 4);
    write_volatile(r.add(W_CRC), crc);
    write_volatile(r.add(W_MAGIC), MAGIC); // the record is valid from here
    dkrt_flush(KEEP_BASE, (REC_WORDS * 4) as u32);
    dkrt_soft_reset()
}

/// At boot: the keep area may hold the crash record of the boot before. Logs it (events a host can read), clears it, and returns whether it
/// was there. The summary stays in `DKRT_CRASH`.
pub unsafe fn at_boot() -> bool {
    let r = rec() as *const u32;
    let c = &mut *addr_of_mut!(DKRT_CRASH);
    if read_volatile(r.add(W_MAGIC)) != MAGIC {
        return false;
    }
    let mut v = [0u32; REC_WORDS];
    let mut i = 0;
    while i < REC_WORDS {
        v[i] = read_volatile(r.add(i));
        i += 1;
    }
    // clear first: a record that crashes the boot again must not be found a third time
    write_volatile(rec().add(W_MAGIC), 0);
    dkrt_flush(KEEP_BASE, 64);
    if v[W_VERSION] != VERSION || crc32_of(v.as_ptr() as *const u8, W_CRC * 4) != v[W_CRC] {
        c.bad = 1;
        dlog::ev(logk::CRASH_FOUND, logk::F_ERROR, [0, 0, 0, 0, 0xbad]);
        return true;
    }
    c.found = 1;
    c.pc = v[W_PC];
    c.vec = v[W_VEC] & 0xff;
    c.module = v[W_MODULE];
    c.stable = v[W_STABLE];
    let e = logk::F_ERROR;
    dlog::ev(logk::CRASH_FOUND, e, [v[W_PC], v[W_VEC], v[W_SR], v[W_A7], v[W_TIME]]);
    dlog::ev(logk::CRASH_MOD, e, [v[W_MODULE], v[W_ENTERED] | v[W_STABLE] << 16, v[W_NAME], v[W_NAME + 1], v[W_NAME + 2]]);
    dlog::ev(logk::CRASH_MOD, e, [0x100, v[W_FRAME0], v[W_FAULTS], v[W_NAME + 3], 0]);
    let mut k = 0;
    while k < 16 {
        dlog::ev(logk::CRASH_REG, e, [k as u32, v[W_REGS + k], v[W_REGS + k + 1], v[W_REGS + k + 2], v[W_REGS + k + 3]]);
        k += 4;
    }
    k = 0;
    while k < STACK_WORDS {
        dlog::ev(logk::CRASH_STACK, e, [k as u32, v[W_STACK + k], v[W_STACK + k + 1], v[W_STACK + k + 2], v[W_STACK + k + 3]]);
        k += 4;
    }
    // the events before the crash, oldest first, two log events each: (seq, time, kind << 16 | flags) and the five arguments
    let n = v[W_TAILN].min(TAIL as u32) as usize;
    let mut t = n;
    while t > 0 {
        t -= 1;
        let b = W_TAIL + 8 * t;
        dlog::ev(logk::CRASH_TAIL, e, [v[b], v[b + 1], v[b + 2], v[b + 3], v[b + 4]]);
        dlog::ev(logk::CRASH_TAIL, e, [v[b], v[b + 5], v[b + 6], v[b + 7], 1]);
    }
    true
}

/// The probe word: log whether the one from the boot before is still in RAM, then write a new one.
pub unsafe fn keep_probe() {
    let p = (KEEP_BASE + PROBE_AT) as usize as *mut u32;
    let c = &mut *addr_of_mut!(DKRT_CRASH);
    let magic = read_volatile(p);
    let gen = read_volatile(p.add(1));
    let kept = magic == PROBE_MAGIC && read_volatile(p.add(2)) == !gen;
    c.ram_kept = kept as u32;
    dlog::ev(logk::RAM_KEEP, 0, [kept as u32, if kept { gen } else { 0 }, 0, 0, 0]);
    let ngen = if kept { gen.wrapping_add(1) } else { 1 };
    write_volatile(p, PROBE_MAGIC);
    write_volatile(p.add(1), ngen);
    write_volatile(p.add(2), !ngen);
    dkrt_flush(KEEP_BASE + PROBE_AT, 16);
    let _ = KEEP_BYTES;
}
