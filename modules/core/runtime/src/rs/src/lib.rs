//! core/runtime: the digikit runtime (dkrt) v0. Code, data and the module arena live in `cf.expansion`.
//!
//! What it does (docs/plans/digikit-runtime.md, docs/design/digikit-runtime.md): reads `.dkm` module files from the card
//! module store with the firmware's own block read, checks them, copies and relocates them into a private arena, flushes
//! the caches, calls `init`, binds the module's slots. A hook in the UI task's event loop (`dkrt_tick`) polls a mailbox
//! for host-requested loads and calls slot 0.
//!
//! Rules: task context only (never from an interrupt); no `core::fmt`; every `extern "C"` function takes and returns
//! integers or pointers. Built with rustc_codegen_gcc for -mcpu=54418 (finding 26).
#![no_std]
#![allow(static_mut_refs)]

use core::panic::PanicInfo;
use core::ptr::{addr_of, addr_of_mut, read_volatile, write_volatile};
use dkrt_sdk::*;

mod arena;
pub mod boot;
mod dsp;
mod loader;
pub mod registry;
mod sha256;
mod store;

#[panic_handler]
fn panic(_: &PanicInfo) -> ! {
    loop {}
}

/// Arena bytes. A sub-region of `cf.expansion` that is part of this object's bss: patchkit places it with the object, so no
/// static object of any module can overlap it, and the reset-path loader zeroes it.
pub const ARENA_BYTES: usize = 512 * 1024;
/// Staging buffer for a `.dkm` file (and the store directory): files are at most this size in v0.
pub const STAGE_BYTES: usize = 64 * 1024;
pub const MAX_MODULES: usize = 8;
pub const LOG_ENTRIES: usize = 16;
pub const STATE_MAGIC: u32 = 0x444b_5254; // "DKRT"

#[repr(C, align(16))]
pub struct Arena(pub [u8; ARENA_BYTES]);
#[repr(C, align(16))]
pub struct Stage(pub [u8; STAGE_BYTES]);

#[no_mangle]
pub static mut DKRT_ARENA: Arena = Arena([0; ARENA_BYTES]);
#[no_mangle]
pub static mut DKRT_STAGE: Stage = Stage([0; STAGE_BYTES]);

#[repr(C)]
#[derive(Clone, Copy)]
pub struct Module {
    pub used: u32,
    pub name: [u8; 32],
    pub version: u32,
    pub base: u32,
    pub size: u32,
    pub fini: u32,
    pub slot_mask: u32,
    pub slot_fn: [u32; SLOTS],
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct LogEntry {
    pub msg: [u8; 24],
    pub value: u32,
}

/// All runtime state, one object a host can peek (`DKRT_STATE` in plan.json symbols).
#[repr(C)]
pub struct State {
    pub magic: u32,
    pub runtime: u32,
    pub ticks: u32,
    pub last_result: i32,
    pub loads: u32,
    pub slot_calls: u32,
    /// Head of the arena free list (address of the first free block, 0 = none).
    pub free_head: u32,
    pub arena_lo: u32,
    pub arena_hi: u32,
    pub log_next: u32,
    /// Function pointers bound to slots (0 = empty); one aligned 32-bit store publishes or clears one.
    pub slots: [u32; SLOTS],
    /// Tasks currently inside a slot function.
    pub entered: [u32; SLOTS],
    pub modules: [Module; MAX_MODULES],
    pub log: [LogEntry; LOG_ENTRIES],
}

#[no_mangle]
pub static mut DKRT_STATE: State = unsafe { core::mem::zeroed() };
/// The store path each module record was loaded from (kept apart from `State`, whose layout the proof tools peek).
#[no_mangle]
pub static mut DKRT_PATHS: [[u8; 64]; MAX_MODULES] = [[0; 64]; MAX_MODULES];
#[no_mangle]
pub static mut DKRT_MAILBOX: Mailbox = Mailbox {
    cmd: 0,
    seq: 0,
    result: 0,
    arg: [0; 96],
};

extern "C" {
    /// Per-device constant, defined by `src/cf/<dev>/device.s`.
    static dkrt_device_id: u32;
    /// `cpushl` over a byte range (both caches), `src/cf/tramp.s`.
    fn dkrt_flush(start: u32, len: u32);
    // Firmware functions: addresses are anchors resolved at patch time.
    fn fw_block_read();
    /// The firmware's eMMC block write (anchor `dkrt.fw.block_write`): `int f(u32 sector, u32 bytes, const void *buf)`, 0 = ok.
    fn fw_block_write();
    /// `src/cf/tramp.s`: mask interrupts (returns the old status register), restore it, and the soft reset.
    fn dkrt_irq_mask() -> u32;
    fn dkrt_irq_restore(sr: u32);
    fn dkrt_soft_reset() -> !;
    fn fw_alloc();
    fn fw_free();
    /// The firmware's "serialize the live project into the persistent arena" (anchor `dkrt.fw.project_serialize`): `int f(void)`.
    fn fw_project_serialize();
    /// The firmware's normal writer of the boot record (anchor `dkrt.fw.boot_record_write`): `void f(void)`; one shot per boot.
    fn fw_boot_record_write();
    // Helpers from compiler_builtins, linked into this object.
    fn memcpy(d: *mut u8, s: *const u8, n: usize) -> *mut u8;
    fn memset(d: *mut u8, c: i32, n: usize) -> *mut u8;
    fn memcmp(a: *const u8, b: *const u8, n: usize) -> i32;
    fn __muldi3();
    fn __udivdi3();
    fn __umoddi3();
}

pub fn st() -> &'static mut State {
    unsafe { &mut *addr_of_mut!(DKRT_STATE) }
}

pub fn flush(start: u32, len: u32) {
    unsafe { dkrt_flush(start, len) }
}

pub fn device() -> u32 {
    unsafe { read_volatile(addr_of!(dkrt_device_id)) }
}

pub unsafe fn block_read(sector: u32, bytes: u32, buf: *mut u8) -> i32 {
    let f: unsafe extern "C" fn(u32, u32, *mut u8) -> i32 =
        core::mem::transmute(fw_block_read as unsafe extern "C" fn());
    f(sector, bytes, buf)
}

/// Write `bytes` (a multiple of 512) from `buf` (16-byte aligned) to `sector`. Only `boot.rs` calls this, for the two sectors of the
/// selection record.
pub unsafe fn block_write(sector: u32, bytes: u32, buf: *const u8) -> i32 {
    let f: unsafe extern "C" fn(u32, u32, *const u8) -> i32 =
        core::mem::transmute(fw_block_write as unsafe extern "C" fn());
    f(sector, bytes, buf)
}

pub unsafe fn rd32(p: *const u8) -> u32 {
    ((*p as u32) << 24)
        | ((*p.add(1) as u32) << 16)
        | ((*p.add(2) as u32) << 8)
        | (*p.add(3) as u32)
}

pub unsafe fn wr32(p: *mut u8, v: u32) {
    *p = (v >> 24) as u8;
    *p.add(1) = (v >> 16) as u8;
    *p.add(2) = (v >> 8) as u8;
    *p.add(3) = v as u8;
}

/// Compare two NUL-terminated strings.
pub unsafe fn cstr_eq(a: *const u8, b: *const u8) -> bool {
    let mut i = 0;
    loop {
        let x = *a.add(i);
        if x != *b.add(i) {
            return false;
        }
        if x == 0 {
            return true;
        }
        i += 1;
    }
}

/// Length of a NUL-terminated string, at most `max`.
pub unsafe fn cstr_len(a: *const u8, max: usize) -> usize {
    let mut i = 0;
    while i < max && *a.add(i) != 0 {
        i += 1;
    }
    i
}

// ---- imports -------------------------------------------------------------------------------------------------------

#[repr(C)]
pub struct Import {
    pub name: [u8; 28],
    pub addr: *const (),
}
pub struct Imports(pub [Import; 9]);
unsafe impl Sync for Imports {}

const fn nm(s: &[u8]) -> [u8; 28] {
    let mut o = [0u8; 28];
    let mut i = 0;
    while i < s.len() {
        o[i] = s[i];
        i += 1;
    }
    o
}

/// The names a module may import. Firmware functions come from patch-time anchors (`dkrt.fw.*`); the helpers are this
/// object's own copies, so a module need not carry them.
pub static IMPORTS: Imports = Imports([
    Import {
        name: nm(b"fw_block_read"),
        addr: fw_block_read as *const (),
    },
    Import {
        name: nm(b"fw_alloc"),
        addr: fw_alloc as *const (),
    },
    Import {
        name: nm(b"fw_free"),
        addr: fw_free as *const (),
    },
    Import {
        name: nm(b"memcpy"),
        addr: memcpy as *const (),
    },
    Import {
        name: nm(b"memset"),
        addr: memset as *const (),
    },
    Import {
        name: nm(b"memcmp"),
        addr: memcmp as *const (),
    },
    Import {
        name: nm(b"__muldi3"),
        addr: __muldi3 as *const (),
    },
    Import {
        name: nm(b"__udivdi3"),
        addr: __udivdi3 as *const (),
    },
    Import {
        name: nm(b"__umoddi3"),
        addr: __umoddi3 as *const (),
    },
]);

/// Address of the import `name` (NUL-terminated), 0 when the table has none.
pub unsafe fn import_addr(name: *const u8) -> u32 {
    let mut i = 0;
    while i < IMPORTS.0.len() {
        if cstr_eq(IMPORTS.0[i].name.as_ptr(), name) {
            return IMPORTS.0[i].addr as usize as u32;
        }
        i += 1;
    }
    0
}

// ---- the API table ---------------------------------------------------------------------------------------------------

unsafe extern "C" fn api_import(name: *const u8) -> u32 {
    import_addr(name)
}
unsafe extern "C" fn api_alloc(size: u32) -> *mut u8 {
    arena::alloc(size)
}
unsafe extern "C" fn api_free(p: *mut u8) {
    arena::free(p);
}
unsafe extern "C" fn api_store_read(path: *const u8, dst: *mut u8, cap: u32) -> i32 {
    store::read_file(path, dst, cap)
}
unsafe extern "C" fn api_log(msg: *const u8, value: u32) {
    log(msg, value);
}

/// The table is built at first use: pointers to the helpers are not constants.
#[no_mangle]
pub static mut dkrt_api: Api = Api {
    version: API_VERSION,
    size: core::mem::size_of::<Api>() as u32,
    device: 0,
    runtime: RUNTIME_VERSION,
    import: api_import,
    alloc: api_alloc,
    free: api_free,
    store_read: api_store_read,
    log: api_log,
    memcpy: 0,
    memset: 0,
    memcmp: 0,
    muldi3: 0,
    udivdi3: 0,
    umoddi3: 0,
};

pub unsafe fn log(msg: *const u8, value: u32) {
    let s = st();
    let e = &mut s.log[(s.log_next as usize) % LOG_ENTRIES];
    let mut i = 0;
    while i < 23 && *msg.add(i) != 0 {
        e.msg[i] = *msg.add(i);
        i += 1;
    }
    while i < 24 {
        e.msg[i] = 0;
        i += 1;
    }
    e.value = value;
    s.log_next = s.log_next.wrapping_add(1);
}

/// First-use setup: arena free list, API table fields, magic.
pub unsafe fn init_once() {
    let s = st();
    if s.magic == STATE_MAGIC {
        return;
    }
    arena::init();
    let a = &mut *addr_of_mut!(dkrt_api);
    a.device = device();
    a.memcpy = import_addr(b"memcpy\0".as_ptr()) as usize;
    a.memset = import_addr(b"memset\0".as_ptr()) as usize;
    a.memcmp = import_addr(b"memcmp\0".as_ptr()) as usize;
    a.muldi3 = import_addr(b"__muldi3\0".as_ptr()) as usize;
    a.udivdi3 = import_addr(b"__udivdi3\0".as_ptr()) as usize;
    a.umoddi3 = import_addr(b"__umoddi3\0".as_ptr()) as usize;
    s.runtime = RUNTIME_VERSION;
    s.magic = STATE_MAGIC;
}

// ---- the public C ABI ----------------------------------------------------------------------------------------------------

/// Load the module file `path` (NUL-terminated, relative to the store root, for example "modules/dyn-demo.dkm").
/// Returns 0 or a negative `E_*` code; on an error nothing was left behind. Task context only; not reentrant.
#[no_mangle]
pub unsafe extern "C" fn dkrt_load(path: *const u8) -> i32 {
    init_once();
    let r = loader::load(path);
    st().last_result = r;
    r
}

/// Unload the module called `name` (NUL-terminated): clear its slots, wait for tasks inside it, `fini`, free.
#[no_mangle]
pub unsafe extern "C" fn dkrt_unload(name: *const u8) -> i32 {
    init_once();
    let r = loader::unload(name);
    st().last_result = r;
    r
}

/// Fill `out` (a `DkrtStatus`).
#[no_mangle]
pub unsafe extern "C" fn dkrt_status(out: *mut DkrtStatus) {
    init_once();
    let s = st();
    let (free, largest) = arena::stats();
    let mut n = 0;
    let mut i = 0;
    while i < MAX_MODULES {
        if s.modules[i].used != 0 {
            n += 1;
        }
        i += 1;
    }
    let o = &mut *out;
    o.magic = s.magic;
    o.runtime = s.runtime;
    o.device = device();
    o.arena_base = s.arena_lo;
    o.arena_size = s.arena_hi - s.arena_lo;
    o.arena_free = free;
    o.arena_largest = largest;
    o.modules = n;
    o.ticks = s.ticks;
    o.last_result = s.last_result;
    o.loads = s.loads;
    o.slot_calls = s.slot_calls;
    o.state = dsp::state();
    o.queued = dsp::ds().queued;
    o.dsp_size = dsp::area_bytes();
    let (dfree, dlargest) = dsp::area_stats();
    o.dsp_free = dfree;
    o.dsp_largest = dlargest;
}

/// The `index`th loaded module (in load-slot order) into `out`; returns 0, or `E_NOT_FOUND` past the end.
#[no_mangle]
pub unsafe extern "C" fn dkrt_module(index: u32, out: *mut DkrtModule) -> i32 {
    init_once();
    let s = st();
    let mut seen = 0;
    let mut i = 0;
    while i < MAX_MODULES {
        let m = &s.modules[i];
        if m.used != 0 {
            if seen == index {
                let o = &mut *out;
                o.name = m.name;
                o.version = m.version;
                o.base = m.base;
                o.size = m.size;
                o.slots = m.slot_mask;
                o.path = (*addr_of!(DKRT_PATHS))[i];
                o.state = if m.used == 2 { MOD_LOADING } else { MOD_LOADED };
                o.dsp_size = dsp::ds().mod_len[i];
                return OK;
            }
            seen += 1;
        }
        i += 1;
    }
    E_NOT_FOUND
}

// ---- boot-time loading (finding 37) ---------------------------------------------------------------------------------------

/// `dkrt_selection_get(out)`: the saved selection (what the next boot loads). Always 0.
#[no_mangle]
pub unsafe extern "C" fn dkrt_selection_get(out: *mut DkrtSelection) -> i32 {
    init_once();
    boot::selection_get(out)
}

/// `dkrt_selection_set(sel)`: save the selection (two copies, sequence number, CRC); no effect until the next boot. 0 or `E_SEL_*`.
#[no_mangle]
pub unsafe extern "C" fn dkrt_selection_set(sel: *const DkrtSelection) -> i32 {
    init_once();
    boot::selection_set(sel)
}

/// `dkrt_boot_info(out)`: safe mode and why, the boot counter, each selected module's result at this boot.
#[no_mangle]
pub unsafe extern "C" fn dkrt_boot_info(out: *mut DkrtBootInfo) {
    init_once();
    boot::boot_info(out)
}

/// What `dkrt_restart` does to keep the working state (finding 40): bit 0 serializes the live project into the persistent arena
/// (`fw_project_serialize`), bit 1 writes the arena to the next boot record (`fw_boot_record_write`, the firmware's own writer for
/// the OS-update path). The default is 0 (no save; the menu asks for a project save first): finding 40 has no edit that the save keeps,
/// and the serialize may race the firmware's BgWorker. 3 runs both steps, for the experiments of finding 40 (a host poke).
#[no_mangle]
pub static mut DKRT_SAVE_MODE: Aligned4 = Aligned4(0);

/// A word that is 4-aligned (the m68k ABI aligns `u32` to 2; the host pokes this one with aligned stores).
#[repr(C, align(4))]
pub struct Aligned4(pub u32);

/// Save the working state the way the firmware does before it flashes an update: the project into the arena, the arena into
/// the boot record. Runs in the UI task with interrupts enabled (the writer takes the eMMC mutex itself).
unsafe fn save_working_state() {
    let mode = read_volatile(addr_of!(DKRT_SAVE_MODE.0));
    if mode & 1 != 0 {
        let f: unsafe extern "C" fn() -> i32 =
            core::mem::transmute(fw_project_serialize as unsafe extern "C" fn());
        f();
    }
    if mode & 2 != 0 {
        let f: unsafe extern "C" fn() =
            core::mem::transmute(fw_boot_record_write as unsafe extern "C" fn());
        f();
    }
}

/// ESDHC0 present-state register (`PRSSTAT`, both devices): bits 0 to 2 are command inhibit, data inhibit and data line active.
const ESDHC_PRSSTAT: u32 = 0xFC0C_C024;

unsafe fn card_busy() -> bool {
    read_volatile(ESDHC_PRSSTAT as *const u32) & 7 != 0
}

/// `dkrt_restart()`: save the working state (`save_working_state`), then the firmware's own soft reset: interrupts masked, console drained,
/// DDR word 0 cleared, `0x80` into `RCM.RCR`. Returns only when it refuses (`E_CARD_BUSY`: a card transfer is in flight; the
/// check is made again with interrupts masked, when no other task can start one).
#[no_mangle]
pub unsafe extern "C" fn dkrt_restart() -> i32 {
    init_once();
    if card_busy() {
        return E_CARD_BUSY;
    }
    save_working_state();
    let sr = dkrt_irq_mask();
    if card_busy() {
        dkrt_irq_restore(sr);
        return E_CARD_BUSY;
    }
    dkrt_soft_reset()
}

/// Called from the UI task's event loop (`dkrt_loop_hook`, src/cf/tramp.s) once per pass, before it waits for the next
/// event. Registers d0, d1, a0, a1 are saved by the hook. Polls the mailbox, then calls slot 0.
#[no_mangle]
pub unsafe extern "C" fn dkrt_tick() {
    init_once();
    let s = st();
    s.ticks = s.ticks.wrapping_add(1);
    boot::ui_reached();
    dsp::step();
    let mb = &mut *addr_of_mut!(DKRT_MAILBOX);
    let cmd = read_volatile(addr_of!(mb.cmd));
    if cmd != 0 {
        // an if chain, not a `match`: a dense match becomes a PC-relative jump table, which is not ColdFire code
        let r = if cmd == CMD_LOAD {
            dkrt_load(mb.arg.as_ptr())
        } else if cmd == CMD_UNLOAD {
            dkrt_unload(mb.arg.as_ptr())
        } else if cmd == CMD_SELECT {
            mailbox_select(mb.arg.as_ptr(), 2)
        } else if cmd == CMD_SELECT_ONE {
            mailbox_select(mb.arg.as_ptr().add(1), *mb.arg.as_ptr() as u32)
        } else if cmd == CMD_RESTART {
            dkrt_restart()
        } else {
            E_FORMAT
        };
        write_volatile(addr_of_mut!(mb.result), r);
        write_volatile(addr_of_mut!(mb.seq), mb.seq.wrapping_add(1));
        write_volatile(addr_of_mut!(mb.cmd), 0);
    }
    let f = read_volatile(addr_of!(s.slots[0]));
    if f != 0 {
        write_volatile(addr_of_mut!(s.entered[0]), s.entered[0] + 1);
        let call: unsafe extern "C" fn(*const Api) = core::mem::transmute(f as usize);
        call(addr_of!(dkrt_api));
        write_volatile(addr_of_mut!(s.entered[0]), s.entered[0] - 1);
        s.slot_calls = s.slot_calls.wrapping_add(1);
    }
}

/// `CMD_SELECT` / `CMD_SELECT_ONE`: the mailbox argument is a list of NUL-terminated paths ended by an empty one. `copy` 0 or 1
/// writes that copy only; 2 writes both.
unsafe fn mailbox_select(arg: *const u8, copy: u32) -> i32 {
    let mut sel: DkrtSelection = core::mem::zeroed();
    let mut p = 0usize;
    let mut n = 0usize;
    loop {
        let len = cstr_len(arg.add(p), 95 - p.min(95));
        if len == 0 {
            break;
        }
        if n == SEL_MAX || len >= SEL_PATH {
            return E_SEL_PATH;
        }
        let mut i = 0;
        while i < len {
            sel.paths[n][i] = *arg.add(p + i);
            i += 1;
        }
        n += 1;
        p += len + 1;
        if p >= 95 {
            break;
        }
    }
    sel.count = n as u32;
    if copy == 2 {
        boot::selection_set(&sel)
    } else {
        boot::selection_set_one(copy, &sel)
    }
}
