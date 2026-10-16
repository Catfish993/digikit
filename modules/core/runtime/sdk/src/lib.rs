//! dkrt-sdk: the C ABI between the digikit runtime (`core/runtime`) and the modules it loads at run time.
//!
//! Shared by the runtime crate and by module crates (path dependency). `#![no_std]`; every function pointer is a
//! gcc m68k cdecl function (arguments on the stack, result in d0); there are no small-struct results.
//! Format and rules: docs/design/digikit-runtime.md and docs/design/digikit-dkm.md.
#![no_std]

/// Version of the `Api` table layout. A field is only ever appended; `Api::size` tells a module how much is there.
pub const API_VERSION: u32 = 1;
/// Version of the runtime a module was packed against (`runtime_min` in the `.dkm` header). 2: DSP sections and asynchronous loads
/// (finding 32), `register` PAGE records and `own_dsp` (finding 33), codes -25/-26 and -30 to -37.
pub const RUNTIME_VERSION: u32 = 2;

/// `Api::device`.
pub const DEVICE_DT2: u32 = 1;
pub const DEVICE_DN2: u32 = 2;

/// Slots: function pointers the firmware hooks call. A module binds one by exporting `dkm_slot_<n>`.
/// Slot 0 runs once per pass of the UI task's event loop (about 120 Hz), as `extern "C" fn(*const Api)`.
pub const SLOTS: usize = 8;

/// Result codes of `dkrt_load`, `dkrt_unload` and `Api::store_read` (negative = error).
pub const OK: i32 = 0;
pub const E_NOT_FOUND: i32 = -1; // no store, or no such file / module
pub const E_BAD_STORE: i32 = -2; // store header or table check failed
pub const E_READ: i32 = -3; // the firmware's block read failed
pub const E_SHA: i32 = -4; // store sha256 or .dkm sha256 does not match
pub const E_FORMAT: i32 = -5; // magic, version, bounds, alignment of the .dkm
pub const E_RUNTIME: i32 = -6; // the module needs a newer runtime
pub const E_IMPORT: i32 = -7; // an import name is not in the runtime's table
pub const E_NOMEM: i32 = -8; // arena full
pub const E_INIT: i32 = -9; // init returned non-zero
pub const E_LOADED: i32 = -10; // a module of that name is loaded
pub const E_SLOT: i32 = -11; // a slot it binds is taken
pub const E_BUSY: i32 = -12; // a task is still inside the module
pub const E_TOO_BIG: i32 = -13; // larger than the staging buffer
pub const E_TABLE_FULL: i32 = -14; // no free module record

/// Codes of the run-time registry (`register` records, docs/design/digikit-registry.md): -20 to -29. Lane ranges: -30 to
/// -39 are the DSP side's. Additive: a code is never renumbered.
pub const E_NO_REGISTRY: i32 = -20; // the file has register records, the image has no core/dynreg
pub const E_REG_DEVICE: i32 = -21; // the records are for another device
pub const E_TYPE_RANGE: i32 = -22; // a machine type outside the reserved range, or a clone_of that is not built in
pub const E_TYPE_USED: i32 = -23; // the type number is in use
pub const E_PARAM_FULL: i32 = -24; // no free reserved parameter row
pub const E_PAGE_FULL: i32 = -25; // no free reserved page row (Digitone II)
pub const E_TYPE_IN_USE: i32 = -26; // unload refused: a track of the active kit uses a machine of the module

/// `dkrt_load` and `dkrt_unload` answer `LOADING` when they accepted the request and finish it over the next UI-loop passes (a
/// module with DSP code: the transfer takes many audio blocks). The final result is `DkrtStatus::last_result` once
/// `DkrtStatus::state` is back to `STATE_IDLE`. A module that is still loading is reported by `dkrt_module` with `state` `MOD_LOADING`
/// and is not usable: its machine is not listed and its slots are not bound.
pub const LOADING: i32 = 1;

/// Codes of the DSP side of a module (docs/design/digikit-dkm.md, "DSP section"): -30 to -39 belong to the DSP loader.
pub const E_NO_DSP: i32 = -30; // the file has DSP code, the image has no core/dsp-link and core/dyndsp
pub const E_DSP_FORMAT: i32 = -31; // the DSP section is malformed
pub const E_DSP_IMPORT: i32 = -32; // a DSP symbol the object imports is not exported by the image
pub const E_DSP_NOSPACE: i32 = -33; // no free range in the DSP area
pub const E_DSP_ROWS: i32 = -34; // no free voice row
pub const E_DSP_XFER: i32 = -35; // the transfer to the DSP failed or timed out
pub const E_DSP_DEVICE: i32 = -36; // the DSP code is for the other device
pub const E_DSP_TYPE: i32 = -37; // a voice type outside the run-time range, or used by another module

/// The table `init` receives (also reachable as `dkrt_api`). Appended-to only; check `size`.
#[repr(C)]
pub struct Api {
    pub version: u32,
    /// `size_of::<Api>()` in the runtime that built it.
    pub size: u32,
    pub device: u32,
    pub runtime: u32,
    /// Address of a firmware function or runtime helper by name (NUL-terminated); 0 when the image has none.
    pub import: unsafe extern "C" fn(name: *const u8) -> u32,
    /// 16-byte aligned block from the module arena (zero is not guaranteed); null when full.
    pub alloc: unsafe extern "C" fn(size: u32) -> *mut u8,
    pub free: unsafe extern "C" fn(p: *mut u8),
    /// Read a store file into `dst` (at most `cap` bytes); returns the file size, or a negative code.
    pub store_read: unsafe extern "C" fn(path: *const u8, dst: *mut u8, cap: u32) -> i32,
    /// Append `msg` (NUL-terminated, 24 bytes kept) and `value` to the runtime's log ring.
    pub log: unsafe extern "C" fn(msg: *const u8, value: u32),
    pub memcpy: usize,
    pub memset: usize,
    pub memcmp: usize,
    pub muldi3: usize,
    pub udivdi3: usize,
    pub umoddi3: usize,
}

/// `dkrt_status` result. The caller owns the memory; the layout only grows at the end.
#[repr(C)]
pub struct DkrtStatus {
    pub magic: u32, // 'DKRT'
    pub runtime: u32,
    pub device: u32,
    pub arena_base: u32,
    pub arena_size: u32,
    pub arena_free: u32,
    pub arena_largest: u32,
    pub modules: u32,
    pub ticks: u32,
    pub last_result: i32,
    pub loads: u32,
    pub slot_calls: u32,
    /// `STATE_IDLE`, `STATE_LOADING` (a module's DSP code is being sent) or `STATE_UNLOADING`.
    pub state: u32,
    /// Load requests waiting behind the one in progress.
    pub queued: u32,
    /// Bytes of the DSP area (0: the image has none) and of it free; the largest free range.
    pub dsp_size: u32,
    pub dsp_free: u32,
    pub dsp_largest: u32,
}

pub const STATE_IDLE: u32 = 0;
pub const STATE_LOADING: u32 = 1;
pub const STATE_UNLOADING: u32 = 2;

/// `DkrtModule::state`.
pub const MOD_LOADED: u32 = 0;
pub const MOD_LOADING: u32 = 1;

/// `dkrt_module` result: one loaded module.
#[repr(C)]
pub struct DkrtModule {
    pub name: [u8; 32],
    pub version: u32,
    pub base: u32,
    pub size: u32,
    pub slots: u32, // bit n = slot n bound
    /// Store path the module was loaded from (NUL-terminated, at most 63 bytes kept), for example `modules/dyn-demo.dkm`.
    pub path: [u8; 64],
    /// `MOD_LOADED` or `MOD_LOADING`.
    pub state: u32,
    /// Bytes of the module's range in the DSP area (0: none).
    pub dsp_size: u32,
}

/// Text for a result code, for a menu line (upper case, at most 14 characters). A table, not a `match`: the compiler turns a
/// dense `match` into a PC-relative jump table that is not ColdFire code.
pub fn result_text(code: i32) -> &'static str {
    const TEXTS: [&str; 15] = [
        "OK",
        "NOT FOUND",
        "BAD STORE",
        "READ ERROR",
        "BAD SHA",
        "BAD FORMAT",
        "RUNTIME OLD",
        "NO IMPORT",
        "NO MEMORY",
        "INIT FAILED",
        "ALREADY LOADED",
        "SLOT TAKEN",
        "BUSY",
        "TOO BIG",
        "TABLE FULL",
    ];
    const DSP_TEXTS: [&str; 8] = [
        "NO DSP LINK",
        "BAD DSP CODE",
        "DSP IMPORT",
        "DSP FULL",
        "NO VOICE ROW",
        "DSP TRANSFER",
        "WRONG DEVICE",
        "BAD VOICE",
    ];
    if (E_DSP_TYPE..=E_NO_DSP).contains(&code) {
        DSP_TEXTS[(-code - 30) as usize]
    } else if code == LOADING {
        "LOADING"
    } else if (E_TABLE_FULL..=OK).contains(&code) {
        TEXTS[(-code) as usize]
    } else {
        "ERROR"
    }
}

/// Mailbox commands (emulator and test use; a menu calls `dkrt_load` directly).
pub const CMD_LOAD: u32 = 1;
pub const CMD_UNLOAD: u32 = 2;

/// The mailbox the UI-task hook polls. A host writes `arg`, then `cmd`; the runtime runs it, stores `result`, adds one
/// to `seq` and zeroes `cmd`.
#[repr(C)]
pub struct Mailbox {
    pub cmd: u32,
    pub seq: u32,
    pub result: i32,
    pub arg: [u8; 96],
}

/// The card module store reader (docs/design/digikit-store.md): sector 0 header "DKST", a table of 128-byte entries sorted
/// by path, then the files. Shared by the runtime (loads) and the DIGIKIT menu (lists). The caller gives the firmware's block
/// read and a 16-byte aligned buffer; nothing here allocates.
pub mod store {
    use super::*;

    /// First card sector of the store and its length (devices/*.toml `[card.store]`, both devices).
    pub const FIRST: u32 = 0x38000;
    pub const SECTORS: u32 = 0x6000;
    pub const MAGIC: u32 = 0x444b_5354; // "DKST"
    pub const ENTRY: usize = 128;
    /// Bytes of a path in an entry (NUL-terminated inside).
    pub const PATH_BYTES: usize = 88;

    /// The firmware's eMMC block read: `(sector, byte count, buffer)`; 0 = ok. Buffer 16-byte aligned, count a multiple of 512,
    /// at most 0x10000 per call.
    pub type BlockRead = unsafe extern "C" fn(sector: u32, bytes: u32, buf: *mut u8) -> i32;

    pub struct Table {
        pub count: u32,
        pub sectors: u32,
    }

    /// Where a file is and what its table entry says its sha256 is.
    pub struct File {
        pub first: u32,
        pub size: u32,
        pub sha: [u8; 32],
    }

    pub unsafe fn be32(p: *const u8) -> u32 {
        ((*p as u32) << 24) | ((*p.add(1) as u32) << 16) | ((*p.add(2) as u32) << 8) | (*p.add(3) as u32)
    }

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

    /// Read `count` sectors starting `rel` sectors into the store; `cap` is the size of `buf` in bytes.
    pub unsafe fn read_sectors(rd: BlockRead, rel: u32, count: u32, buf: *mut u8, cap: usize) -> i32 {
        if rel >= SECTORS || count > SECTORS - rel || count as usize * 512 > cap || count * 512 > 0x10000 {
            return E_BAD_STORE;
        }
        if rd(FIRST + rel, count * 512, buf) != 0 {
            return E_READ;
        }
        OK
    }

    fn crc32(mut crc: u32, p: *const u8, n: usize) -> u32 {
        let mut i = 0;
        while i < n {
            crc ^= unsafe { *p.add(i) } as u32;
            let mut k = 0;
            while k < 8 {
                crc = (crc >> 1) ^ (0xedb8_8320 & 0u32.wrapping_sub(crc & 1));
                k += 1;
            }
            i += 1;
        }
        crc
    }

    /// Read the header and the table into `buf` (`cap` bytes, at least 512). The table then starts at `buf`; entry `i` is at
    /// `buf + i * ENTRY`. Errors: `E_NOT_FOUND` (no store on the card), `E_READ`, `E_BAD_STORE` (version, counts, table CRC-32,
    /// which covers the zero-padded table sectors), `E_TOO_BIG` (the table does not fit in `cap`).
    pub unsafe fn open(rd: BlockRead, buf: *mut u8, cap: usize) -> Result<Table, i32> {
        let r = read_sectors(rd, 0, 1, buf, cap);
        if r != OK {
            return Err(if r == E_READ { E_READ } else { E_NOT_FOUND });
        }
        if be32(buf) != MAGIC {
            return Err(E_NOT_FOUND);
        }
        if be32(buf.add(4)) != 1 {
            return Err(E_BAD_STORE);
        }
        let count = be32(buf.add(8));
        let sectors = be32(buf.add(12));
        let want_crc = be32(buf.add(20));
        if count == 0 || count > 4096 || sectors != (count * ENTRY as u32 + 511) / 512 {
            return Err(E_BAD_STORE);
        }
        if sectors as usize * 512 > cap {
            return Err(E_TOO_BIG);
        }
        let r = read_sectors(rd, 1, sectors, buf, cap);
        if r != OK {
            return Err(r);
        }
        if !crc32(0xffff_ffff, buf, sectors as usize * 512) != want_crc {
            return Err(E_BAD_STORE);
        }
        Ok(Table { count, sectors })
    }

    /// The NUL-terminated path of entry `i` of the table at `tab`.
    pub unsafe fn path(tab: *const u8, i: usize) -> &'static [u8] {
        let p = tab.add(i * ENTRY);
        let mut n = 0;
        while n < PATH_BYTES - 1 && *p.add(n) != 0 {
            n += 1;
        }
        core::slice::from_raw_parts(p, n)
    }

    pub unsafe fn file(tab: *const u8, i: usize) -> File {
        let e = tab.add(i * ENTRY);
        let mut f = File { first: be32(e.add(88)), size: be32(e.add(92)), sha: [0; 32] };
        let mut k = 0;
        while k < 32 {
            f.sha[k] = *e.add(96 + k);
            k += 1;
        }
        f
    }

    /// Entry of `path` (NUL-terminated) in the table `t` at `tab`.
    pub unsafe fn find(tab: *const u8, t: &Table, path: *const u8) -> Result<File, i32> {
        let mut i = 0;
        while i < t.count as usize {
            if cstr_eq(tab.add(i * ENTRY), path) {
                return Ok(file(tab, i));
            }
            i += 1;
        }
        Err(E_NOT_FOUND)
    }
}
