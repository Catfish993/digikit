//! The runtime's use of the module store: the format and the reader are `dkrt_sdk::store` (shared with the DIGIKIT menu);
//! this file adds the staging buffer, the file's sha256 check and `Api::store_read`. Reads go through the firmware's block
//! read into the staging buffer (16-byte aligned, multiples of 512 bytes, at most 0x10000 per call).

use crate::sha256::sha256;
use crate::{DKRT_STAGE, STAGE_BYTES};
use core::ptr::{addr_of, addr_of_mut};
use dkrt_sdk::store::{self, File};
use dkrt_sdk::*;

/// 512 bytes, 16-byte aligned, for `store_figures` (UI task or the mailbox; the upload has its own buffer).
#[repr(C, align(16))]
pub struct PeekBuf([u8; 512]);
#[no_mangle]
pub static mut DKRT_STORE_PEEK: PeekBuf = PeekBuf([0; 512]);

pub unsafe fn stage() -> *mut u8 {
    addr_of_mut!(DKRT_STAGE) as *mut u8
}

unsafe extern "C" fn block_read(sector: u32, bytes: u32, buf: *mut u8) -> i32 {
    crate::block_read(sector, bytes, buf)
}

/// Look `path` up. Leaves the table in the staging buffer (the caller must copy what it needs from the result, which is
/// returned by value).
pub unsafe fn find(path: *const u8) -> Result<File, i32> {
    let t = store::open(block_read, stage(), STAGE_BYTES)?;
    store::find(stage(), &t, path)
}

/// Read the file `f` into the staging buffer and check the table's sha256. The file is then at `stage()`.
pub unsafe fn load_to_stage(f: &File) -> i32 {
    if f.size == 0 || f.size as usize > STAGE_BYTES {
        return E_TOO_BIG;
    }
    let sectors = (f.size + 511) / 512;
    let buf = stage();
    // `first` is relative to the start of the store
    let r = store::read_sectors(block_read, f.first, sectors, buf, STAGE_BYTES);
    if r != OK {
        return r;
    }
    let mut got = [0u8; 32];
    sha256(buf, f.size, got.as_mut_ptr());
    if got != f.sha {
        return E_SHA;
    }
    OK
}

/// `Api::store_read`: copy a store file to `dst` (at most `cap` bytes); returns its size or a negative code.
pub unsafe fn read_file(path: *const u8, dst: *mut u8, cap: u32) -> i32 {
    let f = match find(path) {
        Ok(f) => f,
        Err(e) => return e,
    };
    if f.size > cap {
        return E_TOO_BIG;
    }
    let r = load_to_stage(&f);
    if r != OK {
        return r;
    }
    let src = stage();
    let mut i = 0;
    while i < f.size as usize {
        *dst.add(i) = *src.add(i);
        i += 1;
    }
    f.size as i32
}

/// The store figures as the headers last said them, and whether the headers may have changed since. Only an upload writes a header sector
/// (the card is not swapped while the device runs), so the HEALTH page, which asks twice a second, reads the card once and again after an
/// upload wrote a header (`upload.rs` `wr_header` sets `STORE_DIRTY`): a page that read the card on every refresh would cost the UI task
/// card reads that show up in the CPU load it displays.
static mut STORE_DIRTY: bool = true;
static mut STORE_SEEN: Option<(u32, u32)> = None; // (used bytes, free bytes) of a usable header; None: no store

/// An upload wrote (or tried to write) a header sector: the figures are stale. Called from the MIDI task.
pub fn figures_stale() {
    unsafe { core::ptr::write_volatile(addr_of_mut!(STORE_DIRTY), true) }
}

/// Fill the store figures of a `DkrtHealth` from the headers alone (two sectors, no table, and only when an upload changed them): total
/// bytes a store may use, bytes used and bytes a new file may still take. Sets `HEALTH_STORE` when a usable header was read; a card with no
/// store leaves them 0.
pub unsafe fn figures(o: &mut DkrtHealth) {
    o.store_total = store::LIMIT * 512;
    o.store_used = 0;
    o.store_free = 0;
    if core::ptr::read_volatile(addr_of!(STORE_DIRTY)) {
        // clear first: a header written while we read leaves the flag set for the next refresh
        core::ptr::write_volatile(addr_of_mut!(STORE_DIRTY), false);
        STORE_SEEN = match store::peek_header(block_read, addr_of_mut!(DKRT_STORE_PEEK) as *mut u8) {
            Some(h) => Some((h.used * 512, store::free_bytes(h.used, h.count))),
            None => None,
        };
    }
    if let Some((used, free)) = STORE_SEEN {
        o.store_used = used;
        o.store_free = free;
        o.flags |= HEALTH_STORE;
    }
}
