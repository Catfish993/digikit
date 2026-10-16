//! The module store reader (docs/design/digikit-store.md): sector 0 header "DKST", a table of 128-byte entries sorted by
//! path, then the files. Read with the firmware's block read through the staging buffer (16-byte aligned, multiples of
//! 512 bytes, at most 0x10000 per call).

use crate::sha256::sha256;
use crate::{block_read, cstr_eq, rd32, DKRT_STAGE, STAGE_BYTES};
use core::ptr::addr_of_mut;
use dkrt_sdk::*;

/// First card sector of the store and its length (devices/*.toml `[card.store]`, both devices).
pub const STORE_FIRST: u32 = 0x38000;
pub const STORE_SECTORS: u32 = 0x6000;
const MAGIC: u32 = 0x444b_5354; // "DKST"
const ENTRY: usize = 128;

pub struct Found {
    pub first: u32,
    pub size: u32,
    pub sha: [u8; 32],
}

pub unsafe fn stage() -> *mut u8 {
    addr_of_mut!(DKRT_STAGE) as *mut u8
}

unsafe fn read_sectors(rel: u32, count: u32, buf: *mut u8) -> i32 {
    if rel >= STORE_SECTORS || count > STORE_SECTORS - rel || count * 512 > STAGE_BYTES as u32 {
        return E_BAD_STORE;
    }
    if block_read(STORE_FIRST + rel, count * 512, buf) != 0 {
        return E_READ;
    }
    OK
}

fn crc32_update(mut crc: u32, p: *const u8, n: usize) -> u32 {
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

/// Look `path` up. Leaves the table in the staging buffer (the caller must copy what it needs from the result, which is
/// returned by value).
pub unsafe fn find(path: *const u8) -> Result<Found, i32> {
    let buf = stage();
    let r = read_sectors(0, 1, buf);
    if r != OK {
        return Err(if r == E_READ { E_READ } else { E_NOT_FOUND });
    }
    if rd32(buf) != MAGIC {
        return Err(E_NOT_FOUND); // no store on this card
    }
    if rd32(buf.add(4)) != 1 {
        return Err(E_BAD_STORE);
    }
    let count = rd32(buf.add(8));
    let tsec = rd32(buf.add(12));
    let want_crc = rd32(buf.add(20));
    if count == 0
        || count as usize * ENTRY > STAGE_BYTES - 512
        || tsec != (count * ENTRY as u32 + 511) / 512
    {
        return Err(E_BAD_STORE);
    }
    let r = read_sectors(1, tsec, buf);
    if r != OK {
        return Err(r);
    }
    if !crc32_update(0xffff_ffff, buf, tsec as usize * 512) != want_crc {
        return Err(E_BAD_STORE);
    }
    let mut i = 0;
    while i < count as usize {
        let e = buf.add(i * ENTRY);
        if cstr_eq(e, path) {
            let mut f = Found {
                first: rd32(e.add(88)),
                size: rd32(e.add(92)),
                sha: [0; 32],
            };
            let mut k = 0;
            while k < 32 {
                f.sha[k] = *e.add(96 + k);
                k += 1;
            }
            return Ok(f);
        }
        i += 1;
    }
    Err(E_NOT_FOUND)
}

/// Read the file `f` into the staging buffer and check the table's sha256. The file is then at `stage()`.
pub unsafe fn load_to_stage(f: &Found) -> i32 {
    if f.size == 0 || f.size as usize > STAGE_BYTES {
        return E_TOO_BIG;
    }
    let sectors = (f.size + 511) / 512;
    let buf = stage();
    // `first` is relative to the start of the store
    let r = read_sectors(f.first, sectors, buf);
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
