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
    /// The module store (`HEALTH_STORE`; version 3 of this struct): **KiB** (a store can be 22 GiB) the store may use in all (header, tables and files), the
    /// current header says are used, and a new file may still take in all (the room for the table's next entry kept back; not one contiguous piece).
    pub store_total: u32,
    pub store_used: u32,
    pub store_free: u32,
}

/// A store file for the DIGIKIT menu (`dkrt_store_list`): its path (at most 63 bytes, NUL-terminated), the first sector (relative to the store) and
/// the size in bytes.
#[repr(C)]
pub struct DkrtStoreEntry {
    pub path: [u8; 64],
    pub first: u32,
    pub size: u32,
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
    // The absolute sectors of the two copies are `store::Layout::sel_a` and `sel_b`: the end of the store area, which differs per device.

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

/// The module and sample store, version 3 (docs/design/digikit-store.md).
pub mod store;

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
/// The card is smaller than the area the device file gives the store (its last sector cannot be read): there is no store, the runtime reads and
/// writes nothing there, and the boot goes on.
pub const CARD_TOO_SMALL: u32 = 5;

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
}
