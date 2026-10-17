//! The DSP side of run-time modules (docs/design/digikit-dkm.md "DSP section", docs/findings/32-runtime-dsp-machines.md).
//!
//! A `.dkm` may carry SHARC+ code and data for the DSP. The runtime places it in a range of the DSP area (the `_dl_area` of
//! `core/dsp-link`, first fit, freed when the DSP part of a boot-time load fails), applies the relocations on the ColdFire, sends the bytes with `dl_send` and
//! publishes the module's voice rows. The transfer takes many audio blocks, so a load is a state machine that the UI-loop hook
//! advances (`step`); `dkrt_load` returns `LOADING` and the machine is listed only when the DSP code is acknowledged
//! and the voice rows are in.
//!
//! Voice rows (the voices registry of `core/dsp-voice`, scanned by the DSP for every voice it renders) are written one word
//! per transfer through window 1 of `core/dsp-link`: the pc first (the row is still free: its type is `0xffffffff`, which no
//! machine type equals), then the type, so the render scan never sees a half-written row. A row is freed the other way
//! round (type to `0xffffffff` first).
//!
//! Nothing here runs unless the file has a DSP section; the link to `core/dsp-link` and `core/dyndsp` is optional.
use crate::{arena, cstr_eq, cstr_len, rd32};
use core::ptr::{addr_of_mut, read_volatile};
use dkrt_sdk::*;

const INFO_MAGIC: u32 = 0x4444_5350; // "DDSP"
const STATE_MAGIC: u32 = 0x444b_4453; // "DKDS"
pub const MAX_ROWS: usize = 8;
const MAX_VOICES: usize = 8;
const MAX_RELOCS: u32 = 4096;
const MAX_IMPORTS: u32 = 32;
const GRANULE: u32 = 64;
const L2_BYTE_BASE: u32 = 0x2000_0000;
const L2_SW_BASE: u32 = 0x00B8_0000;
/// The run-time machine types (`dynamic_first ..= last` of modules/core/machines/types.toml).
const TYPE_FIRST: u32 = 16;
const TYPE_LAST: u32 = 23;
/// A free voice row's type: no machine type equals it.
const FREE_TYPE: u32 = 0xffff_ffff;
/// UI-loop passes a phase may last before the load is given up.
const GUARD_TICKS: u32 = 6000;
/// Polls a blocking one-word write may spin.
const SPIN: u32 = 4_000_000;

pub const PH_IDLE: u32 = 0;
pub const PH_SEND: u32 = 1;
pub const PH_ROWS: u32 = 2;

// words of `dyndsp_info` (byte offset / 4)
const I_MAGIC: usize = 0;
const I_DEVICE: usize = 2;
const I_AREA: usize = 3;
const I_AREA_BYTES: usize = 4;
const I_VROWS: usize = 5;
const I_VFIRST: usize = 6;
const I_VFREE: usize = 7;
const I_NIMPORT: usize = 9;
const I_IMPORTS: usize = 10;

extern "C" {
    // Symbols of core/dsp-link and core/dyndsp; each resolves to 0 when the module is not in the image (`[object] optional`).
    fn dl_send();
    fn dl_send_win();
    fn dl_poll();
    fn dyndsp_info();
}

#[repr(C)]
pub struct DlStatus {
    pub state: u32,
    pub err: u32,
    pub nchunks: u32,
    pub acked: u32,
    pub sent: u32,
    pub resent: u32,
    pub nerr: u32,
}
const DL_DONE: u32 = 2;
const DL_ERROR: u32 = 3;

/// The CF-side half of a load whose DSP part is under way: everything is in place except publishing (the machine row, `init`,
/// the slots).
#[repr(C)]
#[derive(Clone, Copy)]
pub struct Pending {
    pub rec: u32,
    /// Copy of the `register` records (arena block), 0 if none.
    pub reg: u32,
    pub reg_size: u32,
    /// Absolute address of `dkm_init`, 0 if none.
    pub init: u32,
    pub mask: u32,
    pub fns: [u32; SLOTS],
    /// 1: the CF side went live at boot (machine rows, `init`, slots); only the DSP part is left, and a failure takes the module down.
    pub early: u32,
    /// The selection index of a module loaded by the boot hook (`u32::MAX`: none), for the boot report.
    pub boot_idx: u32,
    /// The image to send: byte address in the area, bytes, the arena block holding the relocated copy (set by `launch`).
    pub send_base: u32,
    pub send_len: u32,
    pub send_buf: u32,
}

/// The DSP loader's state, one object a host can peek (`DKRT_DSP` in plan.json symbols).
#[repr(C)]
pub struct DspState {
    pub magic: u32,
    pub phase: u32,
    pub ticks: u32,
    pub step: u32,
    /// A transfer has been started in this step.
    pub started: u32,
    /// The buffer of one-word transfers (`dl_send_win` reads it in place).
    pub word: u32,
    /// Byte address, bytes and arena block of the image `reserve` has prepared and `launch` has not taken yet.
    pub send_base: u32,
    pub send_len: u32,
    pub send_buf: u32,
    pub transfers: u32,
    pub row_writes: u32,
    pub failures: u32,
    pub last_dl_err: u32,
    /// Per module record: byte offset into the area and length of its range (len 0: none), and the rows it owns (bit mask).
    pub mod_off: [u32; crate::MAX_MODULES],
    pub mod_len: [u32; crate::MAX_MODULES],
    pub mod_rows: [u32; crate::MAX_MODULES],
    /// Per voice row: the type claimed for it (`FREE_TYPE` when free), its owner (module record + 1), whether its type word was
    /// written to the DSP (so it needs clearing), and the pc to publish.
    pub row_type: [u32; MAX_ROWS],
    pub row_pc: [u32; MAX_ROWS],
    pub row_owner: [u8; MAX_ROWS],
    pub row_pub: [u8; MAX_ROWS],
    /// The load being sent, and the loads behind it (the boot hook publishes the CF side of every module at once).
    pub pending: Pending,
    pub waiting: [Pending; crate::MAX_MODULES],
    pub nwait: u32,
}

#[no_mangle]
pub static mut DKRT_DSP: DspState = unsafe { core::mem::zeroed() };

pub fn ds() -> &'static mut DspState {
    unsafe { &mut *addr_of_mut!(DKRT_DSP) }
}

fn fn_addr(f: unsafe extern "C" fn()) -> usize {
    core::hint::black_box(f as usize)
}

/// The info table of `core/dyndsp`, or null.
unsafe fn info() -> *const u32 {
    let a = fn_addr(dyndsp_info);
    if a == 0 {
        return core::ptr::null();
    }
    let p = a as *const u32;
    if read_volatile(p.add(I_MAGIC)) != INFO_MAGIC {
        return core::ptr::null();
    }
    p
}

unsafe fn iw(k: usize) -> u32 {
    read_volatile(info().add(k))
}

unsafe fn have_link() -> bool {
    fn_addr(dl_send) != 0 && fn_addr(dl_send_win) != 0 && fn_addr(dl_poll) != 0 && !info().is_null()
}

unsafe fn call_send(dst: u32, buf: *const u8, len: u32) -> i32 {
    let f: unsafe extern "C" fn(u32, *const u8, u32) -> i32 = core::mem::transmute(fn_addr(dl_send));
    f(dst, buf, len)
}

unsafe fn call_send_win(win: u32, dst: u32, buf: *const u32, len: u32) -> i32 {
    let f: unsafe extern "C" fn(u32, u32, *const u32, u32) -> i32 =
        core::mem::transmute(fn_addr(dl_send_win));
    f(win, dst, buf, len)
}

unsafe fn call_poll(s: *mut DlStatus) -> i32 {
    let f: unsafe extern "C" fn(*mut DlStatus) -> i32 = core::mem::transmute(fn_addr(dl_poll));
    f(s)
}

pub unsafe fn init_once() {
    let d = ds();
    if d.magic == STATE_MAGIC {
        return;
    }
    let mut i = 0;
    while i < MAX_ROWS {
        d.row_type[i] = FREE_TYPE;
        i += 1;
    }
    d.magic = STATE_MAGIC;
}

// ---- the area ----------------------------------------------------------------------------------------------------------

/// Bytes of the area (0: the image has no DSP loader).
pub unsafe fn area_bytes() -> u32 {
    if info().is_null() {
        0
    } else {
        iw(I_AREA_BYTES)
    }
}

/// Free bytes in the area and the largest free range, for `dkrt_status`.
pub unsafe fn area_stats() -> (u32, u32) {
    let total = area_bytes();
    if total == 0 {
        return (0, 0);
    }
    let d = ds();
    let mut used = 0;
    let mut i = 0;
    while i < crate::MAX_MODULES {
        used += d.mod_len[i];
        i += 1;
    }
    let mut largest = 0;
    let mut cursor = 0u32;
    loop {
        let mut next = total;
        i = 0;
        while i < crate::MAX_MODULES {
            if d.mod_len[i] != 0 && d.mod_off[i] >= cursor && d.mod_off[i] < next {
                next = d.mod_off[i];
            }
            i += 1;
        }
        if next - cursor > largest {
            largest = next - cursor;
        }
        if next >= total {
            break;
        }
        let mut len = 0;
        i = 0;
        while i < crate::MAX_MODULES {
            if d.mod_len[i] != 0 && d.mod_off[i] == next {
                len = d.mod_len[i];
            }
            i += 1;
        }
        cursor = next + len;
    }
    (total - used, largest)
}

/// First fit: the lowest offset whose byte address is `align`-aligned and whose `len` bytes touch no other range.
unsafe fn alloc_range(len: u32, align: u32) -> Option<u32> {
    let total = iw(I_AREA_BYTES);
    let base = iw(I_AREA);
    let d = ds();
    let mut cand = 0u32;
    loop {
        let b = (base + cand + align - 1) & !(align - 1);
        cand = b - base;
        if len > total || cand > total - len {
            return None;
        }
        let mut moved = false;
        let mut i = 0;
        while i < crate::MAX_MODULES {
            let (o, l) = (d.mod_off[i], d.mod_len[i]);
            if l != 0 && cand < o + l && o < cand + len {
                cand = o + l;
                moved = true;
            }
            i += 1;
        }
        if !moved {
            return Some(cand);
        }
    }
}

// ---- checking a section --------------------------------------------------------------------------------------------------

/// What `check` found in a DSP section; pointers are into the staged file.
pub struct Job {
    pub image: *const u8,
    pub image_size: u32,
    pub bss: u32,
    pub align: u32,
    pub relocs: *const u8,
    pub nreloc: u32,
    pub imports: *const u8,
    pub nimport: u32,
    pub voices: *const u8,
    pub nvoice: u32,
    import_vals: [u32; MAX_IMPORTS as usize],
}

impl Job {
    pub const fn empty() -> Job {
        Job {
            image: core::ptr::null(),
            image_size: 0,
            bss: 0,
            align: 0,
            relocs: core::ptr::null(),
            nreloc: 0,
            imports: core::ptr::null(),
            nimport: 0,
            voices: core::ptr::null(),
            nvoice: 0,
            import_vals: [0; MAX_IMPORTS as usize],
        }
    }
}

unsafe fn in_sec(off: u32, len: u32, size: u32) -> bool {
    off <= size && len <= size - off
}

/// An exported DSP symbol of `dyndsp_info`; 0 when there is none of that name.
unsafe fn dsp_symbol(name: *const u8) -> u32 {
    let n = iw(I_NIMPORT);
    let mut i = 0;
    while i < n {
        let e = (info().add(I_IMPORTS) as *const u8).add(32 * i as usize);
        if cstr_eq(e, name) {
            return read_volatile(e.add(28) as *const u32);
        }
        i += 1;
    }
    0
}

/// Validate the DSP section `[off, off + size)` of the staged file `f` for this runtime and image. Writes only `job`.
pub unsafe fn check(f: *const u8, off: u32, size: u32, file_size: u32, job: &mut Job) -> i32 {
    init_once();
    if !in_sec(off, size, file_size) || size < 0x40 {
        return E_DSP_FORMAT;
    }
    let s = f.add(off as usize);
    if rd32(s) != STATE_MAGIC || rd32(s.add(4)) != 1 {
        return E_DSP_FORMAT;
    }
    if !have_link() {
        return E_NO_DSP;
    }
    if rd32(s.add(8)) != iw(I_DEVICE) {
        return E_DSP_DEVICE;
    }
    let image_size = rd32(s.add(0x0c));
    let bss = rd32(s.add(0x10));
    let align = rd32(s.add(0x14));
    let (image_off, reloc_off, nreloc) = (rd32(s.add(0x18)), rd32(s.add(0x1c)), rd32(s.add(0x20)));
    let (import_off, nimport) = (rd32(s.add(0x24)), rd32(s.add(0x28)));
    let (voice_off, nvoice) = (rd32(s.add(0x2c)), rd32(s.add(0x30)));
    if image_size == 0
        || image_size & 3 != 0
        || bss & 3 != 0
        || !(8..=GRANULE).contains(&align)
        || align & (align - 1) != 0
        || nreloc > MAX_RELOCS
        || nimport > MAX_IMPORTS
        || nvoice as usize > MAX_VOICES
        || !in_sec(image_off, image_size, size)
        || !in_sec(reloc_off, nreloc * 8, size)
        || !in_sec(import_off, nimport * 32, size)
        || !in_sec(voice_off, nvoice * 16, size)
    {
        return E_DSP_FORMAT;
    }
    // the range must fit the area at all (a later load may still find it taken: reserve())
    if (image_size + bss + GRANULE - 1) & !(GRANULE - 1) > iw(I_AREA_BYTES) {
        return E_DSP_NOSPACE;
    }
    let mut i = 0;
    while i < nimport {
        let p = s.add(import_off as usize + 32 * i as usize);
        if cstr_len(p, 32) == 32 {
            return E_DSP_FORMAT;
        }
        let v = dsp_symbol(p);
        if v == 0 {
            return E_DSP_IMPORT;
        }
        job.import_vals[i as usize] = v;
        i += 1;
    }
    i = 0;
    while i < nreloc {
        let e = s.add(reloc_off as usize + 8 * i as usize);
        let o = rd32(e);
        let w = rd32(e.add(4));
        let (kind, class, imp) = (w >> 28, (w >> 24) & 15, w & 0xffff);
        if o > image_size - 6 || kind > 1 || class > 2 || (class == 2 && imp >= nimport) || (kind == 0 && class == 1)
        {
            return E_DSP_FORMAT;
        }
        i += 1;
    }
    // voices: types in the run-time range, entries inside the image, rows available
    let d = ds();
    let (mut free_rows, mut k) = (0, 0);
    while k < MAX_ROWS {
        if d.row_owner[k] == 0 && (k as u32) < iw(I_VFREE) {
            free_rows += 1;
        }
        k += 1;
    }
    if nvoice > free_rows {
        return E_DSP_ROWS;
    }
    i = 0;
    while i < nvoice {
        let e = s.add(voice_off as usize + 16 * i as usize);
        let t = rd32(e);
        let entry = rd32(e.add(4));
        if !(TYPE_FIRST..=TYPE_LAST).contains(&t) || entry >= image_size || entry & 1 != 0 {
            return E_DSP_TYPE;
        }
        k = 0;
        while k < MAX_ROWS {
            if d.row_owner[k] != 0 && d.row_type[k] == t {
                return E_DSP_TYPE; // used by a module that is loaded or loading
            }
            k += 1;
        }
        let mut j = 0;
        while j < i {
            if rd32(s.add(voice_off as usize + 16 * j as usize)) == t {
                return E_DSP_TYPE; // twice in this file
            }
            j += 1;
        }
        i += 1;
    }
    job.image = s.add(image_off as usize);
    job.image_size = image_size;
    job.bss = bss;
    job.align = align;
    job.relocs = s.add(reloc_off as usize);
    job.nreloc = nreloc;
    job.imports = s.add(import_off as usize);
    job.nimport = nimport;
    job.voices = s.add(voice_off as usize);
    job.nvoice = nvoice;
    OK
}

// ---- reserving: range, relocated image, voice rows ------------------------------------------------------------------------

unsafe fn le16(p: *const u8) -> u32 {
    (*p as u32) | ((*p.add(1) as u32) << 8)
}

unsafe fn put_le16(p: *mut u8, v: u32) {
    *p = v as u8;
    *p.add(1) = (v >> 8) as u8;
}

/// Reserve everything a module's DSP part needs: a range in the area, an arena block with the relocated image, the voice rows.
/// Nothing is sent yet. On an error nothing is left reserved.
pub unsafe fn reserve(rec: usize, job: &Job) -> i32 {
    let d = ds();
    let len = (job.image_size + job.bss + GRANULE - 1) & !(GRANULE - 1);
    let off = match alloc_range(len, job.align) {
        Some(o) => o,
        None => return E_DSP_NOSPACE,
    };
    let buf = arena::alloc(job.image_size);
    if buf.is_null() {
        return E_NOMEM;
    }
    let base = iw(I_AREA) + off;
    // copy, then relocate: abs24 / abs32 fields of 48-bit instructions, shifted by where the object sits in L2
    let mut i = 0;
    while i < job.image_size as usize {
        *buf.add(i) = *job.image.add(i);
        i += 1;
    }
    let pc_shift = (base - L2_BYTE_BASE) / 2;
    let byte_shift = base - L2_BYTE_BASE;
    i = 0;
    while i < job.nreloc as usize {
        let e = job.relocs.add(8 * i);
        let o = rd32(e) as usize;
        let w = rd32(e.add(4));
        let (kind, class, imp) = (w >> 28, (w >> 24) & 15, (w & 0xffff) as usize);
        let add = match class {
            0 => pc_shift,
            1 => byte_shift,
            _ => job.import_vals[imp],
        };
        let p = buf.add(o);
        if kind == 0 {
            let v = (((*p.add(2)) as u32) << 16 | le16(p.add(4))).wrapping_add(add);
            if v >= 1 << 24 {
                arena::free(buf);
                return E_DSP_FORMAT;
            }
            *p.add(2) = (v >> 16) as u8;
            put_le16(p.add(4), v & 0xffff);
        } else {
            let v = (le16(p.add(2)) << 16 | le16(p.add(4))).wrapping_add(add);
            put_le16(p.add(2), v >> 16);
            put_le16(p.add(4), v & 0xffff);
        }
        i += 1;
    }
    // claim the voice rows (lowest free ones first)
    let mut claimed = 0u32;
    let mut v = 0;
    let mut row = 0;
    while v < job.nvoice as usize {
        while row < MAX_ROWS && (d.row_owner[row] != 0 || (row as u32) >= iw(I_VFREE)) {
            row += 1;
        }
        let e = job.voices.add(16 * v);
        d.row_type[row] = rd32(e);
        d.row_pc[row] = L2_SW_BASE + (base + rd32(e.add(4)) - L2_BYTE_BASE) / 2;
        d.row_owner[row] = rec as u8 + 1;
        d.row_pub[row] = 0;
        claimed |= 1 << row;
        row += 1;
        v += 1;
    }
    d.mod_off[rec] = off;
    d.mod_len[rec] = len;
    d.mod_rows[rec] = claimed;
    d.send_base = base;
    d.send_len = job.image_size;
    d.send_buf = buf as u32;
    OK
}

/// Give back what `reserve` took for module record `rec` (the rows must have been cleared first).
unsafe fn unreserve(rec: usize) {
    let d = ds();
    let mut k = 0;
    while k < MAX_ROWS {
        if d.row_owner[k] == rec as u8 + 1 {
            d.row_owner[k] = 0;
            d.row_type[k] = FREE_TYPE;
            d.row_pub[k] = 0;
            d.row_pc[k] = 0;
        }
        k += 1;
    }
    d.mod_off[rec] = 0;
    d.mod_len[rec] = 0;
    d.mod_rows[rec] = 0;
}

/// Undo a `reserve` that was never started (the CF side failed after it).
pub unsafe fn cancel(rec: usize) {
    let d = ds();
    if d.send_buf != 0 {
        arena::free(d.send_buf as *mut u8);
        d.send_buf = 0;
    }
    unreserve(rec);
}

// ---- one-word writes into the voices registry ---------------------------------------------------------------------------------

/// Start writing `value` into word `word` of window 1; false when the link refused (busy: try again).
unsafe fn start_word(word: u32, value: u32) -> bool {
    let d = ds();
    // the link sends the bytes of the buffer as little-endian DSP words; this is a big-endian ColdFire long
    d.word = (value << 24) | ((value & 0xff00) << 8) | ((value >> 8) & 0xff00) | (value >> 24); // byte swap by hand: no libgcc here
    call_send_win(1, word, &d.word as *const u32, 4) == 0
}

/// `Some(true)` done, `Some(false)` failed, `None` still running.
unsafe fn poll_done() -> Option<bool> {
    let mut s = DlStatus { state: 0, err: 0, nchunks: 0, acked: 0, sent: 0, resent: 0, nerr: 0 };
    call_poll(&mut s);
    match s.state {
        DL_DONE => Some(true),
        DL_ERROR => {
            ds().last_dl_err = s.err;
            Some(false)
        }
        _ => None,
    }
}

/// Write one word and wait for it (the UI task spins; a few audio blocks). Used on the error paths.
unsafe fn write_word_blocking(word: u32, value: u32) -> bool {
    let mut n = 0;
    while !start_word(word, value) {
        n += 1;
        if n > SPIN {
            return false;
        }
        core::hint::black_box(n);
    }
    ds().row_writes += 1;
    n = 0;
    loop {
        match poll_done() {
            Some(ok) => return ok,
            None => {
                n += 1;
                if n > SPIN {
                    return false;
                }
                core::hint::black_box(n);
            }
        }
    }
}

/// Free the rows module record `rec` published: the type word first, so the scan skips the row at once.
unsafe fn clear_rows(rec: usize) {
    let d = ds();
    let first = iw(I_VFIRST);
    let mut k = 0;
    while k < MAX_ROWS {
        if d.row_owner[k] == rec as u8 + 1 && d.row_pub[k] != 0 {
            write_word_blocking(2 * (first + k as u32), FREE_TYPE);
            d.row_pub[k] = 0;
        }
        k += 1;
    }
}

/// Take the DSP part of module record `rec` down (a failed boot-time load): its voice rows, then its range. (The machine row is already gone.)
pub unsafe fn release(rec: usize) {
    let d = ds();
    if d.mod_len[rec] == 0 {
        return;
    }
    clear_rows(rec);
    unreserve(rec);
}

// ---- the state machine --------------------------------------------------------------------------------------------------------

/// Hand a load over to the state machine: the CF side is complete and described by `p`; `reserve` has run for `p.rec`.
pub unsafe fn launch(mut p: Pending) {
    let d = ds();
    p.send_base = d.send_base;
    p.send_len = d.send_len;
    p.send_buf = d.send_buf;
    d.send_buf = 0;
    if d.phase == PH_IDLE && d.nwait == 0 {
        start(p);
    } else {
        d.waiting[d.nwait as usize] = p;
        d.nwait += 1;
    }
}

unsafe fn start(p: Pending) {
    let d = ds();
    d.pending = p;
    d.phase = PH_SEND;
    d.ticks = 0;
    d.step = 0;
    d.started = 0;
    d.transfers += 1;
}

/// The load in progress is over: start the next waiting one, or go idle.
unsafe fn next_job() {
    let d = ds();
    if d.nwait > 0 {
        let p = d.waiting[0];
        let mut k = 1;
        while k < d.nwait as usize {
            d.waiting[k - 1] = d.waiting[k];
            k += 1;
        }
        d.nwait -= 1;
        start(p);
    } else {
        d.phase = PH_IDLE;
    }
}

unsafe fn fail(code: i32) {
    let d = ds();
    let rec = d.pending.rec as usize;
    clear_rows(rec);
    if d.pending.send_buf != 0 {
        arena::free(d.pending.send_buf as *mut u8);
        d.pending.send_buf = 0;
    }
    // the machine was published at boot: take it down again (rows, `fini`, memory)
    crate::loader::undo_early(rec);
    crate::boot::note_result(d.pending.boot_idx, code);
    d.failures += 1;
    crate::st().last_result = code;
    next_job();
}

/// Advance the load in progress by what is ready now. Called once per UI-loop pass.
pub unsafe fn step() {
    let d = ds();
    if d.magic != STATE_MAGIC {
        return;
    }
    if d.phase == PH_IDLE {
        return;
    }
    d.ticks += 1;
    if d.ticks > GUARD_TICKS {
        fail(E_DSP_XFER);
        return;
    }
    loop {
        match d.phase {
            PH_SEND => {
                if d.started == 0 {
                    let r = call_send(d.pending.send_base, d.pending.send_buf as *const u8, d.pending.send_len);
                    if r == -2 {
                        fail(E_DSP_XFER);
                        return;
                    }
                    if r == 0 {
                        d.started = 1;
                    }
                    return;
                }
                match poll_done() {
                    None => return,
                    Some(false) => {
                        fail(E_DSP_XFER);
                        return;
                    }
                    Some(true) => {
                        arena::free(d.pending.send_buf as *mut u8);
                        d.pending.send_buf = 0;
                        d.phase = PH_ROWS;
                        d.step = 0;
                        d.started = 0;
                        d.ticks = 0;
                    }
                }
            }
            PH_ROWS => {
                // rows of the module in claim order; two steps each: the pc, then the type
                let rec = d.pending.rec as usize;
                let first = iw(I_VFIRST);
                let mut want = d.step;
                let mut row = MAX_ROWS;
                let mut k = 0;
                while k < MAX_ROWS {
                    if d.row_owner[k] == rec as u8 + 1 {
                        if want < 2 {
                            row = k;
                            break;
                        }
                        want -= 2;
                    }
                    k += 1;
                }
                if row == MAX_ROWS {
                    // every row is in: list the machine, run init, publish the slots
                    crate::boot::note_result(d.pending.boot_idx, OK);
                    crate::st().last_result = OK;
                    next_job();
                    return;
                }
                if d.started == 0 {
                    let (word, value) = if want == 0 {
                        (2 * (first + row as u32) + 1, d.row_pc[row])
                    } else {
                        (2 * (first + row as u32), d.row_type[row])
                    };
                    if start_word(word, value) {
                        d.started = 1;
                        d.row_writes += 1;
                        if want == 1 {
                            d.row_pub[row] = 1; // from here the type word may be in the DSP: clear it on failure
                        }
                    }
                    return;
                }
                match poll_done() {
                    None => return,
                    Some(false) => {
                        fail(E_DSP_XFER);
                        return;
                    }
                    Some(true) => {
                        d.step += 1;
                        d.started = 0;
                        d.ticks = 0;
                    }
                }
            }
            _ => return,
        }
    }
}

pub unsafe fn state() -> u32 {
    if ds().phase != PH_IDLE {
        STATE_LOADING
    } else {
        STATE_IDLE
    }
}
