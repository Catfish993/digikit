//! The runtime's use of the module store: the format and the reader are `dkrt_sdk::store` (shared with the DIGIKIT menu);
//! this file adds the staging buffer, the file's sha256 check and `Api::store_read`. Reads go through the firmware's block
//! read into the staging buffer (16-byte aligned, multiples of 512 bytes, at most 0x10000 per call).

use crate::sha256::sha256;
use crate::{DKRT_STAGE, STAGE_BYTES};
use core::ptr::addr_of_mut;
use dkrt_sdk::store::{self, File};
use dkrt_sdk::*;

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
