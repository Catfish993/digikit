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

/// Result codes of `dkrt_load` and `Api::store_read` (negative = error).
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
pub const E_BUSY: i32 = -12; // a task is still inside the module (a boot-time DSP failure that takes the module down again)
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
pub const E_TYPE_IN_USE: i32 = -26; // archived with hot reload (docs/plans/digikit-hot-reload.md): no code returns it now; the number stays reserved

/// The boot-time load of a module with DSP code answers `LOADING`: its ColdFire part is live, the DSP part follows over the first
/// UI-loop passes (the transfer takes many audio blocks). The final result is in `DkrtBootInfo::results`. A module whose DSP part is
/// still going is reported by `dkrt_module` with `state` `MOD_LOADING`.
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
    /// `STATE_IDLE` or `STATE_LOADING` (a module's DSP code is being sent).
    pub state: u32,
    /// Bytes of the DSP area (0: the image has none) and of it free; the largest free range.
    pub dsp_size: u32,
    pub dsp_free: u32,
    pub dsp_largest: u32,
}

pub const STATE_IDLE: u32 = 0;
pub const STATE_LOADING: u32 = 1;
pub const STATE_UNLOADING: u32 = 2; // archived with hot reload; never reported now

/// `DkrtModule::state`.
pub const MOD_LOADED: u32 = 0;
pub const MOD_LOADING: u32 = 1;

/// `dkrt_health` result (finding 41). The caller owns the memory; the layout only grows at the end. A figure is valid when its flag is
/// set; the others are 0. Loads are in tenths of a percent (0 to 1000).
#[repr(C)]
pub struct DkrtHealth {
    pub magic: u32,   // 'HLTH'
    pub version: u32, // 1
    pub flags: u32,   // `HEALTH_*`
    /// DSP blocks seen since boot (1500 per second) and windows (one second each) closed.
    pub blocks: u32,
    pub windows: u32,
    /// CPU: load of the last window (`HEALTH_CPU`), the idle-loop iterations in it and the best window so far (100 % idle).
    pub cpu_x10: u32,
    pub idle_win: u32,
    pub idle_ref: u32,
    /// DSP block handler, cycles per block (the DSP's own EMUCLK difference): average and maximum of the last window, the cycles a
    /// block has (`DSP_BLOCK_CYCLES`), and both as tenths of a percent of it (`HEALTH_DSP`).
    pub dsp_avg: u32,
    pub dsp_max: u32,
    pub dsp_period: u32,
    pub dsp_avg_x10: u32,
    pub dsp_max_x10: u32,
    /// The firmware heap (`HEALTH_HEAP`): bytes in all, free, the largest free block, live allocations, free blocks walked.
    pub heap_total: u32,
    pub heap_free: u32,
    pub heap_largest: u32,
    pub heap_allocs: u32,
    pub heap_free_blocks: u32,
    /// The runtime's module arena and the DSP area (0: the image has none): bytes in all, free, the largest free range.
    pub arena_total: u32,
    pub arena_free: u32,
    pub arena_largest: u32,
    pub dsp_area_total: u32,
    pub dsp_area_free: u32,
    pub dsp_area_largest: u32,
}

pub const HEALTH_MAGIC: u32 = 0x484c_5448; // "HLTH"
/// `DkrtHealth::flags`: the image has `core/health`; the CPU load is valid (a second window closed); the DSP figures are valid
/// (a window with counts); the heap figures are valid; the heap walk stopped at its bound (the free figure is a lower bound);
/// the free lists looked corrupt (the walk stopped).
pub const HEALTH_MODULE: u32 = 1;
pub const HEALTH_CPU: u32 = 2;
pub const HEALTH_DSP: u32 = 4;
pub const HEALTH_HEAP: u32 = 8;
pub const HEALTH_HEAP_CUT: u32 = 16;
pub const HEALTH_HEAP_BAD: u32 = 32;
/// Cycles in one DSP block (64 samples at 48 kHz) at the 1 GHz core clock of the emulator's model. The real clock of the device is
/// not known here (finding 41, open): the DSP percentages scale with it.
pub const DSP_BLOCK_CYCLES: u32 = 666_667;

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

/// Mailbox commands (emulator and test use). `CMD_LOAD` calls `dkrt_load`, which answers `E_BOOT_ONLY` once the boot is over; command
/// 2 was `CMD_UNLOAD` (archived with hot reload, tag `digikit-hot-reload-v1`) and is answered `E_FORMAT`.
pub const CMD_LOAD: u32 = 1;
/// `arg`: store paths, each NUL terminated, the list ended by an empty path (an empty list clears the selection):
/// `dkrt_selection_set`. The answer is its result.
pub const CMD_SELECT: u32 = 3;
/// `dkrt_restart`; the answer is only seen when it refuses.
pub const CMD_RESTART: u32 = 4;
/// Write ONE copy of the selection record from `arg` (`arg[0]` = copy 0 or 1, then the paths as for `CMD_SELECT`) with sequence
/// number `seq + 1`: lets a test make the state between the two writes of `dkrt_selection_set` (a power cut).
pub const CMD_SELECT_ONE: u32 = 5;
/// Run `dkrt_health` into the exported `DKRT_HEALTH` (the proof reads it; `result` is 0).
pub const CMD_HEALTH: u32 = 6;

/// Codes of boot-time loading (-40 to -49, finding 37).
pub const E_SEL_PATH: i32 = -40; // a path of the selection is empty, too long or has a bad character
pub const E_SEL_COUNT: i32 = -41; // more than `SEL_MAX` paths
pub const E_SEL_WRITE: i32 = -42; // the firmware's block write failed
pub const E_CARD_BUSY: i32 = -43; // `dkrt_restart`: a card transfer is in flight
pub const E_SAFE_MODE: i32 = -44; // this boot is in safe mode: no module was loaded
pub const E_BOOT_ONLY: i32 = -45; // `dkrt_load` after the boot hook: the module set is fixed per session (hot reload is archived)

/// The selection (what the next boot loads), the layout the menu passes to `dkrt_selection_set` and gets from
/// `dkrt_selection_get`. Paths are store paths ("modules/dyn-pluck.dkm"), NUL terminated, at most `SEL_PATH - 1` characters.
pub const SEL_MAX: usize = 8;
pub const SEL_PATH: usize = 56;
#[repr(C)]
pub struct DkrtSelection {
    /// Number of paths in use, 0 to `SEL_MAX`.
    pub count: u32,
    /// Bit 0: start in safe mode once (the boot that honours it clears the bit).
    pub flags: u32,
    pub paths: [[u8; SEL_PATH]; SEL_MAX],
}

/// Why a boot ran in safe mode (`DkrtBootInfo::reason`).
pub const SAFE_NONE: u32 = 0;
pub const SAFE_COUNTER: u32 = 1; // two boots in a row did not reach the UI loop
pub const SAFE_KEY: u32 = 2; // the safe-mode key was held at power-on
pub const SAFE_FLAG: u32 = 3; // the selection record asked for it (`DkrtSelection::flags` bit 0)
/// The key held at power-on for safe mode: RECORD (panel button 19, `tools/patchkit/pk_harness.py BUTTONS`).
pub const SAFE_KEY_CODE: u32 = 19;
/// Boots in a row that did not reach the UI loop after which the next one is safe mode.
pub const SAFE_AFTER: u32 = 2;

/// `dkrt_boot_info` result: this boot.
#[repr(C)]
pub struct DkrtBootInfo {
    pub magic: u32, // 'DKBT'
    pub version: u32,
    /// 1 when this boot loaded nothing because of safe mode, and `SAFE_*` why.
    pub safe: u32,
    pub reason: u32,
    /// The boot counter as the record held it at the start of this boot (before the runtime raised it).
    pub counter: u32,
    /// Sequence number of the record in use (0: no valid record) and which copy held it (0 = A, 1 = B).
    pub seq: u32,
    pub copy: u32,
    /// Modules selected, and how many of them loaded without an error.
    pub count: u32,
    pub loaded: u32,
    /// 1 once the UI loop has run (the boot counter is back to 0).
    pub ui_reached: u32,
    /// `OK`, a negative code, or `LOADING` (the CF part is live, the DSP part is still going) per selected module, in selection order.
    pub results: [i32; SEL_MAX],
    /// Reserved (0). The boot does not wait for DSP code: the frames that carry it start later, so a module with DSP code is
    /// reported `LOADING` until its DSP part is in, over the first UI-loop passes.
    pub dsp_wait: u32,
}

/// The selection record (docs/design/digikit-store.md): one sector, big endian, in two copies at the end of the store range.
pub mod sel {
    use super::*;

    pub const MAGIC: u32 = 0x444b_534c; // "DKSL"
    pub const VERSION: u32 = 1;
    /// Absolute sectors of the two copies (devices/*.toml `[card.store]`: `end_sector - 2` and `end_sector - 1`).
    pub const SECTOR_A: u32 = store::FIRST + store::SECTORS - 2;
    pub const SECTOR_B: u32 = store::FIRST + store::SECTORS - 1;

    #[derive(Clone, Copy)]
    pub struct Record {
        pub seq: u32,
        pub boot_counter: u32,
        pub flags: u32,
        pub count: u32,
        pub paths: [[u8; SEL_PATH]; SEL_MAX],
    }

    impl Record {
        pub const fn empty() -> Record {
            Record { seq: 0, boot_counter: 0, flags: 0, count: 0, paths: [[0; SEL_PATH]; SEL_MAX] }
        }
    }

    pub fn crc32(p: &[u8]) -> u32 {
        let mut crc = 0xffff_ffffu32;
        for &b in p {
            crc ^= b as u32;
            let mut k = 0;
            while k < 8 {
                crc = (crc >> 1) ^ (0xedb8_8320 & 0u32.wrapping_sub(crc & 1));
                k += 1;
            }
        }
        !crc
    }

    fn put(b: &mut [u8], off: usize, v: u32) {
        b[off] = (v >> 24) as u8;
        b[off + 1] = (v >> 16) as u8;
        b[off + 2] = (v >> 8) as u8;
        b[off + 3] = v as u8;
    }
    fn get(b: &[u8], off: usize) -> u32 {
        ((b[off] as u32) << 24) | ((b[off + 1] as u32) << 16) | ((b[off + 2] as u32) << 8) | (b[off + 3] as u32)
    }

    pub fn encode(r: &Record, out: &mut [u8; 512]) {
        let mut i = 0;
        while i < 512 {
            out[i] = 0;
            i += 1;
        }
        put(out, 0, MAGIC);
        put(out, 4, VERSION);
        put(out, 8, r.seq);
        put(out, 12, r.boot_counter);
        put(out, 16, r.flags);
        put(out, 20, r.count);
        let mut k = 0;
        while k < SEL_MAX {
            let mut j = 0;
            while j < SEL_PATH {
                out[32 + k * SEL_PATH + j] = r.paths[k][j];
                j += 1;
            }
            k += 1;
        }
        let c = crc32(&out[..508]);
        put(out, 508, c);
    }

    /// A valid record: magic, version, CRC, count, every used path NUL terminated.
    pub fn decode(b: &[u8; 512]) -> Option<Record> {
        if get(b, 0) != MAGIC || get(b, 4) != VERSION || get(b, 508) != crc32(&b[..508]) {
            return None;
        }
        let count = get(b, 20);
        if count as usize > SEL_MAX {
            return None;
        }
        let mut r = Record::empty();
        r.seq = get(b, 8);
        r.boot_counter = get(b, 12);
        r.flags = get(b, 16);
        r.count = count;
        let mut k = 0;
        while k < SEL_MAX {
            let mut j = 0;
            let mut nul = false;
            while j < SEL_PATH {
                let c = b[32 + k * SEL_PATH + j];
                r.paths[k][j] = c;
                nul |= c == 0;
                j += 1;
            }
            if (k as u32) < count && (!nul || r.paths[k][0] == 0) {
                return None;
            }
            k += 1;
        }
        Some(r)
    }
}

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

#[cfg(test)]
mod tests {
    use super::sel::*;
    use super::*;

    fn example() -> Record {
        let mut r = Record::empty();
        r.seq = 7;
        r.boot_counter = 1;
        r.count = 2;
        r.paths[0][..13].copy_from_slice(b"modules/a.dkm");
        r.paths[1][..13].copy_from_slice(b"modules/b.dkm");
        r
    }

    /// The same record as `tools/patchkit/pk_dkrt_boot.py make_record(["modules/a.dkm", "modules/b.dkm"], seq=7, counter=1)`:
    /// the layout of docs/design/digikit-store.md; the CRC is the one that Python's zlib computes.
    #[test]
    fn the_record_has_the_documented_bytes() {
        let mut b = [0u8; 512];
        encode(&example(), &mut b);
        assert_eq!(&b[0..4], b"DKSL");
        assert_eq!(&b[4..24], &[0, 0, 0, 1, 0, 0, 0, 7, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 2]);
        assert_eq!(&b[32..45], b"modules/a.dkm");
        assert_eq!(&b[32 + SEL_PATH..45 + SEL_PATH], b"modules/b.dkm");
        assert_eq!(&b[508..512], &0xce5a_5f11u32.to_be_bytes());
    }

    #[test]
    fn decode_takes_what_encode_made_and_refuses_damage() {
        let mut b = [0u8; 512];
        encode(&example(), &mut b);
        let r = decode(&b).unwrap();
        assert_eq!((r.seq, r.boot_counter, r.flags, r.count), (7, 1, 0, 2));
        assert_eq!(&r.paths[1][..13], b"modules/b.dkm");
        // a flipped bit anywhere before the CRC, a bad magic, a count over 8, a used path with no NUL
        for at in [0usize, 9, 40, 300, 507] {
            let mut c = b;
            c[at] ^= 1;
            assert!(decode(&c).is_none(), "byte {at}");
        }
        let mut c = [0u8; 512];
        assert!(decode(&c).is_none());
        let mut r2 = example();
        r2.count = 9;
        encode(&r2, &mut c);
        assert!(decode(&c).is_none());
        let mut r3 = example();
        r3.paths[0] = [b'x'; SEL_PATH];
        encode(&r3, &mut c);
        assert!(decode(&c).is_none());
    }

    #[test]
    fn the_record_sectors_are_the_last_two_of_the_store_range() {
        assert_eq!(SECTOR_A, 0x3dffe);
        assert_eq!(SECTOR_B, 0x3dfff);
        assert_eq!(store::FIRST + store::SECTORS, 0x3e000);
    }
}
