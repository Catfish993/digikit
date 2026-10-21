//! dkrt-sdk: the C ABI between the digikit runtime (`core/runtime`) and the modules it loads at run time.
//!
//! Shared by the runtime crate and by module crates (path dependency). `#![no_std]`; every function pointer is a
//! gcc m68k cdecl function (arguments on the stack, result in d0); there are no small-struct results.
//! Format and rules: docs/design/digikit-runtime.md and docs/design/digikit-dkm.md.
#![no_std]

/// Version of the `Api` table layout. A field is only ever appended; `Api::size` tells a module how much is there.
pub const API_VERSION: u32 = 1;
/// Version of the runtime a module was packed against (`runtime_min` in the `.dkm` header). 2: DSP sections and asynchronous loads
/// (finding 32), `register` PAGE records and `own_dsp` (finding 33), codes -25/-26 and -30 to -37. 3: card safety (codes -46/-47, -62), the
/// debug log and its SysEx commands LOG and PEEK, `dkrt_diag`, `dkrt_debug_set`, the crash record (finding 46). 4: SELECT and RESTART.
/// 5: the DSP guard (finding 51): voice rows of 16 words with the knob ranges, DSP sections of version 2 (voices of 48 bytes), the 24-word
/// DSP diagnostics with the guard's fault words, the log events `MACHINE_FAULT` and `MACHINE_DISABLED`.
/// Versions 1 to 5 were plain integers (they read as 0.0.1 to 0.0.5 packed).
/// 0.1.0 = semantic version; contract versions in the .dkm header (a module is refused only on a contract mismatch, docs/design/module-sdk.md 11.5 D3); LOGCFG, log levels and sources, Api.log_lv.
pub use gen_sdk::RUNTIME_VERSION;

/// Facts from the device files (`devices/*.toml`) and `core/machines` types.toml that every device agrees on, written by
/// `patchkit gen` (the `[[gen]]` of core/runtime's module.toml; docs/design/device-data.md). `Api::device` is the device's
/// `[digikit] id`, read from the runtime's device object, not a constant here.
pub mod device {
    include!("device.rs");
}

/// Slots: function pointers the firmware hooks call. A module binds one by exporting `dkm_slot_<n>`.
/// Slot 0 runs once per pass of the UI task's event loop (about 120 Hz), as `extern "C" fn(*const Api)`.
pub const SLOTS: usize = 8;

/// Result codes of `dkrt_load` and `Api::store_read` (negative = error).
pub const OK: i32 = 0;
// The codes themselves are written by `patchkit gen` from `[load.status]` of `sdk/sdk.toml` (`gen_sdk.rs`, module `load`, with their
// texts): -1 to -14 `dkrt_load`; -20 to -29 the run-time registry (`register` records, docs/design/digikit-registry.md); -30 to -39 the
// DSP side's (docs/design/digikit-dkm.md, "DSP section"); -40 to -49 boot-time loading (finding 37). Additive: a code is never renumbered.
pub use gen_sdk::load::*;

/// The boot-time load of a module with DSP code answers `LOADING`: its ColdFire part is live, the DSP part follows over the first
/// UI-loop passes (the transfer takes many audio blocks). The final result is in `DkrtBootInfo::results`. A module whose DSP part is
/// still going is reported by `dkrt_module` with `state` `MOD_LOADING`.
pub const LOADING: i32 = 1;

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
    /// Like `log`, with a level: 0 error, 1 warn, 2 info, 3 debug, 4 trace (above 4: 4).
    pub log_lv: unsafe extern "C" fn(level: u32, msg: *const u8, value: u32),
    /// (api 1.2) Register the SysEx commands `first .. first + count` with `handler` (docs/design/module-sdk.md, "Service commands"). The range must
    /// lie inside one of the ranges sdk/sdk.toml `[protocol.ranges]` reserves and overlap no registered one; else it returns `proto::E_CMD_RANGE`.
    /// Call it from `init`; the range is released when the module's `fini` has run. Returns 0.
    pub register_cmds: unsafe extern "C" fn(first: u32, count: u32, handler: Option<ServiceCmd>) -> i32,
}

/// A service command handler (`Api::register_cmds`). It runs in the firmware's MIDI task: short, no waiting. `body` is the request body
/// (`len` bytes, at most 160); the reply body goes to `out` (at most 879 bytes). Returns the number of reply bytes (0 or more: status ok), or a
/// negative status (a `proto::E_*` code or its own: sent as the reply's status with no body).
pub type ServiceCmd = unsafe extern "C" fn(cmd: u32, seq: u32, body: *const u8, len: u32, out: *mut u8) -> i32;

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

/// One entry of `dkrt_capacity` (the CAPACITY command's entries; `kind` is a `proto::CAPK_*` number). `largest` is 0 where a largest piece has no
/// meaning. The layout only grows at the end.
#[repr(C)]
#[derive(Clone, Copy)]
pub struct DkrtCap {
    pub kind: u32,
    pub limit: u32,
    pub used: u32,
    pub largest: u32,
}
/// Room for the entries a caller of `dkrt_capacity` gives (the runtime has 11 kinds; new kinds are appended).
pub const CAP_MAX: usize = 16;

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
    /// The module store (`HEALTH_STORE`; version 2 of this struct): bytes the store may use in all (header, tables and files), bytes the
    /// current header says are used, and bytes a new file may still take (the room for the table's next entry kept back).
    pub store_total: u32,
    pub store_used: u32,
    pub store_free: u32,
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
/// The store figures are valid (a store header was read; a card without a store has none).
pub const HEALTH_STORE: u32 = 64;
/// Cycles in one DSP block (32 samples at 48 kHz, 1500 blocks per second) at the 1 GHz core clock of the emulator's model
/// (`[board]` of the device files). The real clock of the device is not known **[O]** (finding 41): the DSP percentages scale with it.
pub const DSP_BLOCK_CYCLES: u32 = device::DSP_BLOCK_CYCLES;

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
/// Test hook: `arg[0..4]` = sector, `arg[4..8]` = byte count (big endian); the runtime calls its one card-write function with the staging
/// buffer and answers its result (0, `E_RANGE` or `E_CARD_RO`). The proof uses it with sectors outside the store range and on a card that
/// is not ours; it never targets a sector of a store that is ours.
pub const CMD_CARD_PROBE: u32 = 7;
/// Test hook: `arg[0]` = 1 or 0: `dkrt_debug_set` (the menu's DEBUG row); the answer is its result.
pub const CMD_DEBUG: u32 = 8;

/// The selection (what the next boot loads), the layout the menu passes to `dkrt_selection_set` and gets from
/// `dkrt_selection_get`. Paths are store paths ("modules/dyn-pluck.dkm"), NUL terminated, at most `SEL_PATH - 1` characters.
pub const SEL_MAX: usize = gen_sdk::limits::SEL_MAX; // `[limits.max] modules` of sdk/sdk.toml (the record's format)
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
/// The key held at power-on for safe mode (`[digikit] safe_key` of the device files; RECORD, panel button 19).
pub const SAFE_KEY_CODE: u32 = device::SAFE_KEY_CODE;
/// Boots in a row that did not stay up for `STABLE_BLOCKS` after which the next one is safe mode.
pub const SAFE_AFTER: u32 = 2;
/// The stable period: DSP blocks (1500 per second) of UI loop after which the boot counter is cleared. 45,000 blocks are 30 seconds
/// (finding 37). A boot that dies before then counts as bad.
pub const STABLE_BLOCKS: u32 = 45_000;

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
    /// 1 once the boot counter is back to 0: after `STABLE_BLOCKS` DSP blocks of UI loop in a normal boot, at the first UI-loop pass in a
    /// safe boot (finding 37, "Safe mode after the first UI pass"). Appended; the layout only grows at the end.
    pub stable: u32,
}

/// The selection record (docs/design/digikit-store.md): one sector, big endian, in two copies at the end of the store range.
pub mod sel {
    use super::*;

    pub const MAGIC: u32 = 0x444b_534c; // "DKSL"
    pub const VERSION: u32 = 1;
    /// Absolute sectors of the two copies (devices/*.toml `[card.store]`: `end_sector - 2` and `end_sector - 1`).
    pub const SECTOR_A: u32 = device::STORE_SEL_A;
    pub const SECTOR_B: u32 = device::STORE_SEL_B;

    /// `Record::flags` bit 0: start in safe mode once. Bit 1: the debug log is OFF (the zero value of the field means ON, so an older record
    /// and a card with no record log by default).
    pub const FLAG_SAFE_ONCE: u32 = 1;
    pub const FLAG_DEBUG_OFF: u32 = 2;

    #[derive(Clone, Copy)]
    pub struct Record {
        pub seq: u32,
        pub boot_counter: u32,
        pub flags: u32,
        pub count: u32,
        /// The stable period of this card in DSP blocks (bytes 24 to 27 of the record, reserved zero before): 0 takes `STABLE_BLOCKS`. A
        /// setting for tests and for people who debug a module; nothing in the menu writes it.
        pub stable_blocks: u32,
        /// The sticky machine types (docs/design/module-sdk.md 11.5 D7): up to 8 entries, each `hash24 << 8 | type`, 0 = empty. Entry 0 is
        /// bytes 28 to 31 of the record, entries 1 to 7 bytes 480 to 507.
        pub map: [u32; MAP_N],
        pub paths: [[u8; SEL_PATH]; SEL_MAX],
    }

    /// Entries of the sticky type map.
    pub const MAP_N: usize = crate::gen_sdk::limits::MAP_N; // `[limits.max] map_entries` of sdk/sdk.toml

    /// Byte offset of map entry `i`.
    pub const fn map_at(i: usize) -> usize {
        if i == 0 { 28 } else { 480 + 4 * (i - 1) }
    }

    impl Record {
        pub const fn empty() -> Record {
            Record { seq: 0, boot_counter: 0, flags: 0, count: 0, stable_blocks: 0, map: [0; MAP_N], paths: [[0; SEL_PATH]; SEL_MAX] }
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
        put(out, 24, r.stable_blocks);
        let mut m = 0;
        while m < MAP_N {
            put(out, map_at(m), r.map[m]);
            m += 1;
        }
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
        r.stable_blocks = get(b, 24);
        let mut m = 0;
        while m < MAP_N {
            r.map[m] = get(b, map_at(m));
            m += 1;
        }
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

    /// First card sector of the store and its length (devices/*.toml `[card.store]`, the same on every device).
    pub const FIRST: u32 = device::STORE_FIRST;
    pub const SECTORS: u32 = device::STORE_SECTORS;
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
        /// End of the store's used sectors (header A, tables and data, as the header says), relative to the store.
        pub used: u32,
        pub seq: u32,
        pub table_first: u32,
        pub version: u32,
        /// 0 = copy A, 1 = copy B.
        pub copy: u32,
        /// The other copy is a usable version 2 header that says the same (the upload's BEGIN repairs the store when it does not).
        pub agree: bool,
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

    /// CRC-32 (IEEE, reflected) before the final inversion; `crc32_of` is the finished value (zlib's).
    pub fn crc32(mut crc: u32, p: *const u8, n: usize) -> u32 {
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

    pub fn crc32_of(p: *const u8, n: usize) -> u32 {
        !crc32(0xffff_ffff, p, n)
    }

    /// Sector of header copy B, relative to the store (`devices/*.toml [card.store] header_b_sector` - `first_sector`): the sector just
    /// before the selection record. Files end before it: `LIMIT` sectors may hold the header A, a table and data.
    pub const HEADER_B: u32 = device::STORE_HEADER_B;
    pub const LIMIT: u32 = HEADER_B;

    /// May a card write of `bytes` (a multiple of 512, not 0) starting at absolute `sector` happen? Only inside the store range
    /// `[FIRST, FIRST + SECTORS)` of the device file: the runtime's one card-write function refuses anything else (finding 46).
    pub const fn write_allowed(sector: u32, bytes: u32) -> bool {
        if bytes == 0 || bytes & 511 != 0 {
            return false;
        }
        let n = bytes / 512;
        sector >= FIRST && n <= SECTORS && sector - FIRST <= SECTORS - n
    }

    /// One header copy as decoded (version 1: `table_first` 1, `seq` 1).
    #[derive(Clone, Copy)]
    pub struct Hdr {
        pub version: u32,
        pub count: u32,
        pub sectors: u32,
        pub used: u32,
        pub table_crc: u32,
        pub table_first: u32,
        pub seq: u32,
    }

    /// Decode a header sector: magic, version 1 or 2 and, for version 2, the CRC over bytes 0..508. `None` when any fails.
    pub unsafe fn parse_header(p: *const u8) -> Option<Hdr> {
        if be32(p) != MAGIC {
            return None;
        }
        let version = be32(p.add(4));
        let mut h = Hdr {
            version,
            count: be32(p.add(8)),
            sectors: be32(p.add(12)),
            used: be32(p.add(16)),
            table_crc: be32(p.add(20)),
            table_first: 1,
            seq: 1,
        };
        if version == 2 {
            if be32(p.add(508)) != crc32_of(p, 508) {
                return None;
            }
            h.table_first = be32(p.add(24));
            h.seq = be32(p.add(28));
        } else if version != 1 {
            return None;
        }
        Some(h)
    }

    unsafe fn put32(p: *mut u8, v: u32) {
        *p = (v >> 24) as u8;
        *p.add(1) = (v >> 16) as u8;
        *p.add(2) = (v >> 8) as u8;
        *p.add(3) = v as u8;
    }

    /// Write the 512 bytes of a version 2 header copy to `out`.
    pub unsafe fn encode_header(h: &Hdr, out: *mut u8) {
        let mut i = 0;
        while i < 512 {
            *out.add(i) = 0;
            i += 1;
        }
        put32(out, MAGIC);
        put32(out.add(4), 2);
        put32(out.add(8), h.count);
        put32(out.add(12), h.sectors);
        put32(out.add(16), h.used);
        put32(out.add(20), h.table_crc);
        put32(out.add(24), h.table_first);
        put32(out.add(28), h.seq);
        put32(out.add(508), crc32_of(out, 508));
    }

    /// Which copy to try first: `true` = B. B (version 2 only) wins when A is absent or version 1, or when its `seq` is higher.
    pub fn prefer_b(a: Option<&Hdr>, b: Option<&Hdr>) -> bool {
        match (a, b) {
            (_, None) => false,
            (None, Some(_)) => true,
            (Some(a), Some(b)) => a.version == 1 || b.seq > a.seq,
        }
    }

    /// Read the header (choosing between copy A at sector 0 and copy B at `HEADER_B`) and the table into `buf` (`cap` bytes, at least
    /// 512). The table then starts at `buf`; entry `i` is at `buf + i * ENTRY`. A copy is usable when its magic, version, header CRC
    /// (version 2) and table CRC hold; the usable copy with the higher `seq` is taken (equal: A; a version 1 A loses to a usable B).
    /// Errors: `E_NOT_FOUND` (no store on the card), `E_READ`, `E_BAD_STORE` (no usable copy), `E_TOO_BIG` (the table does not fit in `cap`).
    pub unsafe fn open(rd: BlockRead, buf: *mut u8, cap: usize) -> Result<Table, i32> {
        let r = read_sectors(rd, 0, 1, buf, cap);
        if r != OK {
            return Err(if r == E_READ { E_READ } else { E_NOT_FOUND });
        }
        let a_magic = be32(buf) == MAGIC;
        let a = parse_header(buf);
        let r = read_sectors(rd, HEADER_B, 1, buf, cap);
        let b = if r == OK { parse_header(buf).filter(|h| h.version == 2) } else { None };
        if a.is_none() && b.is_none() {
            return Err(if a_magic { E_BAD_STORE } else { E_NOT_FOUND });
        }
        let first_b = prefer_b(a.as_ref(), b.as_ref());
        let mut err = E_BAD_STORE;
        let mut n = 0;
        while n < 2 {
            let pick = if (n == 0) == first_b { b } else { a };
            let which = if (n == 0) == first_b { 1 } else { 0 };
            n += 1;
            let h = match pick {
                Some(h) => h,
                None => continue,
            };
            match load_table(rd, &h, buf, cap) {
                Ok(()) => {
                    let other = if which == 1 { a } else { b };
                    let agree = match other {
                        Some(o) => {
                            o.version == 2
                                && o.seq == h.seq
                                && o.count == h.count
                                && o.sectors == h.sectors
                                && o.used == h.used
                                && o.table_crc == h.table_crc
                                && o.table_first == h.table_first
                        }
                        None => false,
                    };
                    return Ok(Table {
                        count: h.count,
                        sectors: h.sectors,
                        used: h.used,
                        seq: h.seq,
                        table_first: h.table_first,
                        version: h.version,
                        copy: which,
                        agree,
                    })
                }
                Err(e) => err = e,
            }
        }
        Err(err)
    }

    /// What the two header sectors say right now, as four words (the header CRC, or for a version 1 header its table CRC, of A and of B; 0
    /// for a sector that is not a header). Every commit of a store changes at least one of them (the sequence number is under the CRC).
    /// `None`: a read failed. `scratch` is 512 bytes, 16-byte aligned.
    pub unsafe fn fingerprint(rd: BlockRead, scratch: *mut u8) -> Option<[u32; 4]> {
        let mut out = [0u32; 4];
        let mut k = 0;
        while k < 2 {
            let rel = if k == 0 { 0 } else { HEADER_B };
            if read_sectors(rd, rel, 1, scratch, 512) != OK {
                return None;
            }
            if be32(scratch) == MAGIC {
                out[2 * k] = be32(scratch.add(508)) ^ be32(scratch.add(20)).rotate_left(7) ^ be32(scratch.add(28)).rotate_left(13);
                out[2 * k + 1] = be32(scratch.add(4)) ^ be32(scratch.add(16)).rotate_left(3);
            }
            k += 1;
        }
        Some(out)
    }

    /// How many times `open_stable` reads again before it gives the last answer.
    pub const STABLE_TRIES: u32 = 4;

    /// `open` that a commit running at the same time cannot tear. The upload (the MIDI task) writes a table, then header B, then header
    /// A; after a CLEAR it may even stage over the old table. A reader in another task would see a table that does not match its header.
    /// The rule: read the headers' fingerprint, `open`, read the fingerprint again; when it changed (a commit ran in between), try again.
    /// No lock: neither task ever waits for the other, and a reader that keeps losing the race (more than `STABLE_TRIES` commits in a row,
    /// which one host cannot do) returns what it read last. `scratch` as for `fingerprint`; `buf` must not overlap it.
    pub unsafe fn open_stable(rd: BlockRead, buf: *mut u8, cap: usize, scratch: *mut u8) -> Result<Table, i32> {
        let mut tries = 0;
        loop {
            let before = fingerprint(rd, scratch);
            let r = open(rd, buf, cap);
            if tries >= STABLE_TRIES || before.is_none() {
                return r;
            }
            if fingerprint(rd, scratch) == before {
                return r;
            }
            tries += 1;
        }
    }

    /// The header a store reader would use, without reading its table: the usable copy (magic, version, header CRC) with the higher
    /// `seq`. For a figure on a page (used and free space) that must not read a whole table twice a second. `buf` is 512 bytes, aligned.
    pub unsafe fn peek_header(rd: BlockRead, buf: *mut u8) -> Option<Hdr> {
        if read_sectors(rd, 0, 1, buf, 512) != OK {
            return None;
        }
        let a = parse_header(buf);
        if read_sectors(rd, HEADER_B, 1, buf, 512) != OK {
            return a;
        }
        let b = parse_header(buf).filter(|h| h.version == 2);
        if prefer_b(a.as_ref(), b.as_ref()) {
            b
        } else {
            a
        }
    }

    /// Sectors of the table for `count` entries.
    pub const fn table_sectors(count: u32) -> u32 {
        (count * ENTRY as u32 + 511) / 512
    }

    /// Bytes a new file may still take when the header says `used` sectors and `count` files: the room for the table of `count + 1`
    /// entries is kept back (the upload's `free_bytes`).
    pub const fn free_bytes(used: u32, count: u32) -> u32 {
        let room = table_sectors(count + 1);
        let free = LIMIT.saturating_sub(used).saturating_sub(room);
        free * 512
    }

    /// Read the table `h` points at into `buf` and check the counts and the CRC.
    unsafe fn load_table(rd: BlockRead, h: &Hdr, buf: *mut u8, cap: usize) -> Result<(), i32> {
        if h.count > 4096
            || h.sectors != (h.count * ENTRY as u32 + 511) / 512
            || h.table_first < 1
            || h.used < h.table_first
            || h.used - h.table_first < h.sectors
            || h.used > LIMIT
        {
            return Err(E_BAD_STORE);
        }
        if h.sectors as usize * 512 > cap {
            return Err(E_TOO_BIG);
        }
        if h.sectors != 0 {
            let r = read_sectors(rd, h.table_first, h.sectors, buf, cap);
            if r != OK {
                return Err(r);
            }
        }
        if crc32_of(buf, h.sectors as usize * 512) != h.table_crc {
            return Err(E_BAD_STORE);
        }
        Ok(())
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

/// State of the card's store area as the runtime found it at boot (`DkrtDiag::card_state`; docs/findings/46-digikit-debug.md).
pub const CARD_UNKNOWN: u32 = 0;
/// A valid store header, or a valid selection copy: the area is ours.
pub const CARD_OURS: u32 = 1;
/// No structure of ours, and every sector of the range reads all 0x00 or all 0xFF: the first write may claim it.
pub const CARD_BLANK: u32 = 2;
/// Something else is there: the runtime never writes the card.
pub const CARD_FOREIGN: u32 = 3;
/// A read of the area failed: the runtime cannot tell, so it never writes the card.
pub const CARD_UNREADABLE: u32 = 4;

/// `dkrt_diag` result (finding 46): what the debug log and the card check found. The caller owns the memory; the layout only grows at the end.
#[repr(C)]
pub struct DkrtDiag {
    pub magic: u32, // 'DIAG'
    pub version: u32,
    /// `CARD_*`, the first sector (relative to the store) that was not blank (`u32::MAX`: none) and a crc32 of it, card writes refused
    /// (range or read-only), and reads that failed in the check.
    pub card_state: u32,
    pub card_first_nonblank: u32,
    pub card_hash: u32,
    pub card_refused: u32,
    pub card_read_errors: u32,
    /// 1 when the debug log keeps every event (SETTINGS > DIGIKIT > DEBUG), 0 when it keeps errors only.
    pub debug_on: u32,
    /// Sequence numbers of the oldest and the newest event in the log ring (0, 0: empty).
    pub log_oldest: u32,
    pub log_newest: u32,
    /// 1 when `dl_diag` is in the image and answered; 1 when the heartbeat watch decided the DSP stopped; the last heartbeat word.
    pub dsp_diag_ok: u32,
    pub dsp_stopped: u32,
    pub dsp_heartbeat: u32,
    /// A crash record from the boot before was found at this boot: 1, its program counter, vector and the module slot it was in
    /// (`u32::MAX`: none).
    pub crash_found: u32,
    pub crash_pc: u32,
    pub crash_vec: u32,
    /// 1 when the keep-probe word of the boot before was still in RAM at this boot (RAM survived the reset that led here).
    pub ram_kept: u32,
    /// The DSP's last breadcrumb when the watch decided it stopped (0 before): stage | voice << 8 | machine type << 16 (finding 48).
    pub dsp_crumb: u32,
}

pub const DIAG_MAGIC: u32 = 0x4449_4147; // "DIAG"

/// Event kinds of the debug log (docs/findings/46-digikit-debug.md has the argument meanings; `tools/device/dklog.py` the readable names).
/// An event is 32 bytes: `seq`, `time` (the firmware's block counter, 1500 per second), `kind`, `flags` (bit 0: an error, kept when debug is
/// off), five argument words.
/// Written by `patchkit gen` from `sdk/sdk.toml` (`gen_sdk.rs`), with the contract version numbers (`contract`).
mod gen_sdk;
pub use gen_sdk::{contract, logk, proto};

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

    /// The sticky type map: entry 0 is bytes 28 to 31, entries 1 to 7 are bytes 480 to 507 (docs/design/digikit-store.md); the same bytes as
    /// `make_record(..., tmap=...)` in tests/patchkit/test_patchkit_dkrt_boot.py. The words before and after keep their places.
    #[test]
    fn the_sticky_map_is_in_the_reserved_bytes() {
        let mut r = example();
        r.map = [0xe53b_3a10, 0xcf31_7d11, 0, 0, 0, 0, 0, 0x17];
        let mut b = [0u8; 512];
        encode(&r, &mut b);
        assert_eq!(&b[28..32], &0xe53b_3a10u32.to_be_bytes());
        assert_eq!(&b[480..484], &0xcf31_7d11u32.to_be_bytes());
        assert_eq!(&b[504..508], &0x17u32.to_be_bytes());
        assert_eq!(&b[24..28], &[0, 0, 0, 0]); // stable_blocks stays where it was
        assert_eq!(&b[32..45], b"modules/a.dkm");
        assert_eq!(decode(&b).unwrap().map, r.map);
        // a record without a map (the builder's, an older runtime's) has an empty one
        encode(&example(), &mut b);
        assert_eq!(decode(&b).unwrap().map, [0; MAP_N]);
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
    fn card_writes_are_allowed_only_inside_the_store_range() {
        let (lo, n) = (store::FIRST, store::SECTORS);
        assert!(store::write_allowed(lo, 512));
        assert!(store::write_allowed(lo + n - 1, 512));
        assert!(store::write_allowed(lo, n * 512));
        assert!(store::write_allowed(SECTOR_A, 1024)); // both record sectors
        assert!(!store::write_allowed(lo - 1, 512));
        assert!(!store::write_allowed(lo + n, 512));
        assert!(!store::write_allowed(lo + n - 1, 1024)); // runs past the end
        assert!(!store::write_allowed(lo - 1, 1024)); // starts before and runs in
        assert!(!store::write_allowed(lo, (n + 1) * 512));
        assert!(!store::write_allowed(lo, 0));
        assert!(!store::write_allowed(lo, 100));
        assert!(!store::write_allowed(0, 512));
        assert!(!store::write_allowed(u32::MAX, 512));
        assert!(!store::write_allowed(u32::MAX - 1, u32::MAX & !511));
    }

    #[test]
    fn the_record_sectors_are_the_last_two_of_the_store_range() {
        assert_eq!(SECTOR_A, 0x3dffe);
        assert_eq!(SECTOR_B, 0x3dfff);
        assert_eq!(store::FIRST + store::SECTORS, 0x3e000);
    }

    // ---- the store reader: header copies A and B, version 1 and 2 ----

    extern crate std;
    use std::sync::Mutex;
    use std::vec::Vec;

    static CARD: Mutex<Vec<u8>> = Mutex::new(Vec::new());
    static LOCK: Mutex<()> = Mutex::new(());

    unsafe extern "C" fn rd(sector: u32, bytes: u32, buf: *mut u8) -> i32 {
        let c = CARD.lock().unwrap();
        let at = (sector - store::FIRST) as usize * 512;
        core::ptr::copy_nonoverlapping(c.as_ptr().add(at), buf, bytes as usize);
        0
    }

    fn put(rel: u32, b: &[u8]) {
        let mut c = CARD.lock().unwrap();
        let at = rel as usize * 512;
        c[at..at + b.len()].copy_from_slice(b);
    }

    fn table_with(paths: &[&[u8]]) -> (Vec<u8>, u32) {
        let n = paths.len();
        let sectors = (n * store::ENTRY + 511) / 512;
        let mut t = std::vec![0u8; sectors * 512];
        for (i, p) in paths.iter().enumerate() {
            t[i * store::ENTRY..i * store::ENTRY + p.len()].copy_from_slice(p);
            t[i * store::ENTRY + 91] = 7; // first sector 7
        }
        let crc = store::crc32_of(t.as_ptr(), t.len());
        (t, crc)
    }

    fn hdr(version: u32, count: u32, tf: u32, seq: u32, crc: u32) -> store::Hdr {
        store::Hdr { version, count, sectors: (count * 128 + 511) / 512, used: tf + (count * 128 + 511) / 512 + 10, table_crc: crc, table_first: tf, seq }
    }

    /// Lay out a card: table `paths` at `tf`; header A and B as given (None = zero).
    fn card(a: Option<store::Hdr>, b: Option<store::Hdr>, tf: u32, paths: &[&[u8]]) -> u32 {
        *CARD.lock().unwrap() = std::vec![0u8; store::SECTORS as usize * 512];
        let (t, crc) = table_with(paths);
        put(tf, &t);
        let mut s = [0u8; 512];
        for (h, at) in [(a, 0), (b, store::HEADER_B)] {
            if let Some(mut h) = h {
                h.table_crc = crc;
                if h.version == 2 {
                    unsafe { store::encode_header(&h, s.as_mut_ptr()) };
                } else {
                    s.fill(0);
                    s[0..4].copy_from_slice(b"DKST");
                    s[4..8].copy_from_slice(&1u32.to_be_bytes());
                    s[8..12].copy_from_slice(&h.count.to_be_bytes());
                    s[12..16].copy_from_slice(&h.sectors.to_be_bytes());
                    s[16..20].copy_from_slice(&h.used.to_be_bytes());
                    s[20..24].copy_from_slice(&crc.to_be_bytes());
                }
                put(at, &s);
            }
        }
        crc
    }

    fn open() -> Result<(u32, u32, u32), i32> {
        let mut buf = std::vec![0u8; 4096];
        let t = unsafe { store::open(rd, buf.as_mut_ptr(), buf.len()) }?;
        Ok((t.seq, t.copy, t.version))
    }

    #[test]
    fn the_header_is_chosen_by_seq_and_a_wins_a_tie() {
        let _g = LOCK.lock().unwrap();
        let h = |seq| hdr(2, 1, 1, seq, 0);
        card(Some(h(1)), Some(h(1)), 1, &[b"a"]);
        assert_eq!(open(), Ok((1, 0, 2)));
        card(Some(h(1)), Some(h(2)), 1, &[b"a"]);
        assert_eq!(open(), Ok((2, 1, 2)));
        card(Some(h(3)), Some(h(2)), 1, &[b"a"]);
        assert_eq!(open(), Ok((3, 0, 2)));
    }

    #[test]
    fn the_reader_says_whether_the_two_copies_agree() {
        let _g = LOCK.lock().unwrap();
        let agree = || {
            let mut buf = std::vec![0u8; 4096];
            unsafe { store::open(rd, buf.as_mut_ptr(), buf.len()) }.unwrap().agree
        };
        let h = |seq| hdr(2, 1, 1, seq, 0);
        card(Some(h(2)), Some(h(2)), 1, &[b"a"]);
        assert!(agree());
        // a cut between header B and header A: B is ahead
        card(Some(h(1)), Some(h(2)), 1, &[b"a"]);
        assert!(!agree());
        // B torn: A alone
        card(Some(h(2)), Some(h(2)), 1, &[b"a"]);
        CARD.lock().unwrap()[store::HEADER_B as usize * 512 + 9] ^= 1;
        assert!(!agree());
        // no B (a card made before version 2, or the first commit cut): not equal
        card(Some(h(1)), None, 1, &[b"a"]);
        assert!(!agree());
        // a version 1 A and a usable B of the same content is still a repair (A is rewritten as version 2)
        card(Some(hdr(1, 1, 1, 1, 0)), Some(hdr(2, 1, 1, 1, 0)), 1, &[b"a"]);
        assert!(!agree());
    }

    #[test]
    fn a_torn_copy_falls_back_to_the_other() {
        let _g = LOCK.lock().unwrap();
        let h = |seq| hdr(2, 1, 1, seq, 0);
        card(Some(h(1)), Some(h(2)), 1, &[b"a"]);
        // flip a bit in B: the header CRC fails, A is used
        CARD.lock().unwrap()[store::HEADER_B as usize * 512 + 9] ^= 1;
        assert_eq!(open(), Ok((1, 0, 2)));
        // B good, A torn
        card(Some(h(1)), Some(h(2)), 1, &[b"a"]);
        CARD.lock().unwrap()[30] ^= 1;
        assert_eq!(open(), Ok((2, 1, 2)));
        // B names a table that is not there (header B written, table torn): table CRC fails, A is used
        card(Some(h(1)), Some(h(2)), 1, &[b"a"]);
        let mut b = h(2);
        b.table_first = 40;
        b.used = 60;
        let mut s = [0u8; 512];
        unsafe { store::encode_header(&b, s.as_mut_ptr()) };
        put(store::HEADER_B, &s);
        assert_eq!(open(), Ok((1, 0, 2)));
        // both torn
        CARD.lock().unwrap()[30] ^= 1;
        assert_eq!(open(), Err(E_BAD_STORE));
    }

    #[test]
    fn version_1_reads_and_an_empty_range_is_no_store() {
        let _g = LOCK.lock().unwrap();
        card(Some(hdr(1, 1, 1, 1, 0)), None, 1, &[b"a"]);
        assert_eq!(open(), Ok((1, 0, 1)));
        // a usable B beats a version 1 A
        card(Some(hdr(1, 1, 1, 1, 0)), Some(hdr(2, 1, 1, 2, 0)), 1, &[b"a"]);
        assert_eq!(open(), Ok((2, 1, 2)));
        card(None, None, 1, &[b"a"]);
        assert_eq!(open(), Err(E_NOT_FOUND));
        // an empty store (CLEAR): version 2, no entries
        card(Some(hdr(2, 0, 1, 5, 0)), Some(hdr(2, 0, 1, 5, 0)), 1, &[]);
        assert_eq!(open(), Ok((5, 0, 2)));
    }

    // ---- a reader against a commit that runs in another task ----

    /// A commit in slow motion: step `k` of `writes` (store sector, bytes) is applied just before read number `at + k * gap`; with `gap` 0
    /// all of them are applied at once, between two reads (the reader was slow, the other task was not).
    struct Hook {
        reads: u32,
        at: u32,
        gap: u32,
        done: usize,
        writes: Vec<(u32, Vec<u8>)>,
    }
    static HOOK: Mutex<Option<Hook>> = Mutex::new(None);

    unsafe extern "C" fn rd_hooked(sector: u32, bytes: u32, buf: *mut u8) -> i32 {
        let mut now: Vec<(u32, Vec<u8>)> = Vec::new();
        {
            let mut g = HOOK.lock().unwrap();
            if let Some(h) = g.as_mut() {
                let n = h.reads;
                h.reads += 1;
                while h.done < h.writes.len() && n >= h.at + h.done as u32 * h.gap {
                    now.push(h.writes[h.done].clone());
                    h.done += 1;
                }
            }
        }
        for (rel, data) in now {
            put(rel, &data);
        }
        rd(sector, bytes, buf)
    }

    fn header_bytes(count: u32, tf: u32, seq: u32, crc: u32) -> Vec<u8> {
        let h = store::Hdr {
            version: 2,
            count,
            sectors: (count * 128 + 511) / 512,
            used: tf + (count * 128 + 511) / 512 + 10,
            table_crc: crc,
            table_first: tf,
            seq,
        };
        let mut s = std::vec![0u8; 512];
        unsafe { store::encode_header(&h, s.as_mut_ptr()) };
        s
    }

    /// (seq, count) of a read through `reader`, or the error.
    fn read_with(stable: bool) -> Result<(u32, u32), i32> {
        let mut buf = std::vec![0u8; 4096];
        let mut scratch = std::vec![0u8; 1024];
        // 16-byte aligned scratch inside the vector
        let sp = ((scratch.as_mut_ptr() as usize + 15) & !15) as *mut u8;
        let t = unsafe {
            if stable {
                store::open_stable(rd_hooked, buf.as_mut_ptr(), buf.len(), sp)
            } else {
                store::open(rd_hooked, buf.as_mut_ptr(), buf.len())
            }
        }?;
        Ok((t.seq, t.count))
    }

    /// Every interleaving of `writes` with the reads of one open: the answer is the old store or the new one, never an error.
    /// Returns how many interleavings made the plain `open` fail (a torn read).
    fn interleave(setup: &dyn Fn(), writes: &[(u32, Vec<u8>)], old: (u32, u32), new: (u32, u32)) -> usize {
        let mut plain_failures = 0;
        for (at, gap) in (0..14).flat_map(|at| (0..3).map(move |gap| (at, gap))) {
            for stable in [false, true] {
                setup();
                *HOOK.lock().unwrap() = Some(Hook { reads: 0, at, gap, done: 0, writes: writes.to_vec() });
                let r = read_with(stable);
                *HOOK.lock().unwrap() = None;
                if stable {
                    let r = r.unwrap_or_else(|e| panic!("open_stable failed (error {e}) when the commit starts at read {at}, gap {gap}"));
                    assert!(r == old || r == new, "open_stable read {r:?} at read {at}, gap {gap}");
                } else if r.is_err() {
                    plain_failures += 1;
                }
            }
        }
        *HOOK.lock().unwrap() = None;
        plain_failures
    }

    #[test]
    fn a_commit_during_a_read_never_shows_a_torn_table() {
        let _g = LOCK.lock().unwrap();
        // an upload commit: the new table is already on the card, then header B, then header A
        let (t2, crc2) = table_with(&[b"a", b"b"]);
        let setup = || {
            card(Some(hdr(2, 1, 1, 1, 0)), Some(hdr(2, 1, 1, 1, 0)), 1, &[b"a"]);
            put(40, &t2);
        };
        let writes = std::vec![(store::HEADER_B, header_bytes(2, 40, 2, crc2)), (0, header_bytes(2, 40, 2, crc2))];
        interleave(&setup, &writes, (1, 1), (2, 2));
    }

    #[test]
    fn staging_over_the_old_table_after_a_clear_never_shows_a_torn_table() {
        let _g = LOCK.lock().unwrap();
        // CLEAR commits an empty store (B, then A); the next upload stages over sector 1, where the old table was
        let empty_crc = store::crc32_of(core::ptr::null(), 0);
        let setup = || {
            card(Some(hdr(2, 3, 1, 1, 0)), Some(hdr(2, 3, 1, 1, 0)), 1, &[b"a", b"b", b"c"]);
        };
        let writes = std::vec![
            (store::HEADER_B, header_bytes(0, 1, 2, empty_crc)),
            (0, header_bytes(0, 1, 2, empty_crc)),
            (1, std::vec![0xaa; 512]),
        ];
        let torn = interleave(&setup, &writes, (1, 3), (2, 0));
        // the rule is needed: some interleavings tear a plain `open`
        assert!(torn > 0, "no interleaving tore the plain reader: the test does not exercise the race");
    }

    #[test]
    fn open_stable_on_a_quiet_store_reads_the_store() {
        let _g = LOCK.lock().unwrap();
        // no commit: one try, the store as it is
        card(Some(hdr(2, 1, 1, 1, 0)), Some(hdr(2, 1, 1, 1, 0)), 1, &[b"a"]);
        *HOOK.lock().unwrap() = None;
        assert_eq!(read_with(true), Ok((1, 1)));
    }

    #[test]
    fn the_header_peek_takes_the_newer_usable_copy() {
        let _g = LOCK.lock().unwrap();
        card(Some(hdr(2, 1, 1, 1, 0)), Some(hdr(2, 1, 1, 2, 0)), 1, &[b"a"]);
        let mut b = std::vec![0u8; 1024];
        let p = ((b.as_mut_ptr() as usize + 15) & !15) as *mut u8;
        let h = unsafe { store::peek_header(rd, p) }.unwrap();
        assert_eq!(h.seq, 2);
        assert_eq!(store::free_bytes(h.used, h.count), (store::LIMIT - h.used - 1) * 512);
        card(None, None, 1, &[]);
        assert!(unsafe { store::peek_header(rd, p) }.is_none());
    }

    #[test]
    fn header_b_is_the_sector_before_the_selection_record() {
        assert_eq!(store::HEADER_B, store::SECTORS - 2 - 1);
        assert_eq!(store::FIRST + store::HEADER_B, 0x3dffd);
    }
}
