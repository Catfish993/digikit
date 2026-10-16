//! dkrt-sdk: the C ABI between the digikit runtime (`core/runtime`) and the modules it loads at run time.
//!
//! Shared by the runtime crate and by module crates (path dependency). `#![no_std]`; every function pointer is a
//! gcc m68k cdecl function (arguments on the stack, result in d0); there are no small-struct results.
//! Format and rules: docs/design/digikit-runtime.md and docs/design/digikit-dkm.md.
#![no_std]

/// Version of the `Api` table layout. A field is only ever appended; `Api::size` tells a module how much is there.
pub const API_VERSION: u32 = 1;
/// Version of the runtime a module was packed against (`runtime_min` in the `.dkm` header).
pub const RUNTIME_VERSION: u32 = 1;

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
}

/// `dkrt_module` result: one loaded module.
#[repr(C)]
pub struct DkrtModule {
    pub name: [u8; 32],
    pub version: u32,
    pub base: u32,
    pub size: u32,
    pub slots: u32, // bit n = slot n bound
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
