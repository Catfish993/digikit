//! The `.dkm` loader (docs/design/digikit-dkm.md). Every check runs before anything is written to the arena.

use crate::sha256::Sha;
use crate::{
    arena, cstr_eq, cstr_len, dkrt_api, flush, import_addr, init_once, rd32, st, store, wr32,
    Module, MAX_MODULES, RUNTIME_VERSION,
};
use core::ptr::{addr_of, addr_of_mut, read_volatile, write_volatile};
use dkrt_sdk::*;

const MAGIC: u32 = 0x444b_4d46; // "DKMF"
const HEADER: u32 = 0xa0;
const NONE: u32 = 0xffff_ffff;
const SPIN: u32 = 2_000_000;

// header field offsets (big endian)
const H_SIZE: usize = 0x08;
const H_HDRSIZE: usize = 0x0c;
const H_SHA: usize = 0x10;
const H_RUNTIME: usize = 0x30;
const H_VERSION: usize = 0x34;
const H_NAME: usize = 0x40;
const H_IMAGE_OFF: usize = 0x60;
const H_IMAGE_SIZE: usize = 0x64;
const H_BSS: usize = 0x68;
const H_ALIGN: usize = 0x6c;
const H_RELOC_OFF: usize = 0x70;
const H_RELOC_N: usize = 0x74;
const H_IMPORT_OFF: usize = 0x78;
const H_IMPORT_N: usize = 0x7c;
const H_INIT: usize = 0x80;
const H_FINI: usize = 0x84;
const H_SLOT_OFF: usize = 0x88;
const H_SLOT_N: usize = 0x8c;

unsafe fn in_file(off: u32, len: u32, size: u32) -> bool {
    off <= size && len <= size - off
}

/// Validate and load the `.dkm` at `path`. See `dkrt_load`.
pub unsafe fn load(path: *const u8) -> i32 {
    let found = match store::find(path) {
        Ok(f) => f,
        Err(e) => return e,
    };
    let r = store::load_to_stage(&found);
    if r != OK {
        return r;
    }
    let f = store::stage() as *const u8;
    let size = found.size;

    // ---- checks (nothing is written outside the staging buffer) ----
    if size < HEADER || rd32(f) != MAGIC || rd32(f.add(4)) != 1 {
        return E_FORMAT;
    }
    if rd32(f.add(H_SIZE)) != size || rd32(f.add(H_HDRSIZE)) != HEADER {
        return E_FORMAT;
    }
    // the file's own sha256, over the file with the sha field read as zeros
    let mut want = [0u8; 32];
    let mut i = 0;
    while i < 32 {
        want[i] = *f.add(H_SHA + i);
        i += 1;
    }
    let mut sha = Sha::new();
    sha.update(f, H_SHA as u32);
    sha.update_zeros(32);
    sha.update(f.add(H_SHA + 32), size - (H_SHA as u32 + 32));
    let mut got = [0u8; 32];
    sha.finish(got.as_mut_ptr());
    if got != want {
        return E_SHA;
    }
    if rd32(f.add(H_RUNTIME)) > RUNTIME_VERSION {
        return E_RUNTIME;
    }
    let name = f.add(H_NAME);
    let nlen = cstr_len(name, 32);
    if nlen == 0 || nlen == 32 {
        return E_FORMAT;
    }
    let image_off = rd32(f.add(H_IMAGE_OFF));
    let image_size = rd32(f.add(H_IMAGE_SIZE));
    let bss = rd32(f.add(H_BSS));
    let align = rd32(f.add(H_ALIGN));
    let reloc_off = rd32(f.add(H_RELOC_OFF));
    let reloc_n = rd32(f.add(H_RELOC_N));
    let import_off = rd32(f.add(H_IMPORT_OFF));
    let import_n = rd32(f.add(H_IMPORT_N));
    let init = rd32(f.add(H_INIT));
    let fini = rd32(f.add(H_FINI));
    let slot_off = rd32(f.add(H_SLOT_OFF));
    let slot_n = rd32(f.add(H_SLOT_N));
    if align == 0
        || align > 16
        || align & (align - 1) != 0
        || image_size == 0
        || image_size > size
        || bss > crate::ARENA_BYTES as u32
    {
        return E_FORMAT;
    }
    if !in_file(image_off, image_size, size)
        || image_off & 15 != 0
        || reloc_n > 4096
        || import_n > 64
        || slot_n > SLOTS as u32
        || !in_file(reloc_off, reloc_n * 8, size)
        || !in_file(import_off, import_n * 32, size)
        || !in_file(slot_off, slot_n * 8, size)
    {
        return E_FORMAT;
    }
    let total = image_size + bss;
    if (init != NONE && init >= image_size) || (fini != NONE && fini >= image_size) {
        return E_FORMAT;
    }
    // imports: every name must be in the runtime's table
    let mut import_addrs = [0u32; 64];
    i = 0;
    while i < import_n as usize {
        let p = f.add(import_off as usize + i * 32);
        if cstr_len(p, 32) == 32 {
            return E_FORMAT;
        }
        let a = import_addr(p);
        if a == 0 {
            return E_IMPORT;
        }
        import_addrs[i] = a;
        i += 1;
    }
    // relocations: kind, bounds, import index
    i = 0;
    while i < reloc_n as usize {
        let e = f.add(reloc_off as usize + i * 8);
        let off = rd32(e);
        let kind = rd32(e.add(4));
        let k = kind >> 24;
        if off > image_size - 4 || (k != 0 && k != 1) || (k == 1 && (kind & 0xff_ffff) >= import_n)
        {
            return E_FORMAT;
        }
        i += 1;
    }
    // slots: index and function offset in range; the slot is free
    let s = st();
    let mut mask = 0u32;
    i = 0;
    while i < slot_n as usize {
        let e = f.add(slot_off as usize + i * 8);
        let idx = rd32(e);
        let fo = rd32(e.add(4));
        if idx >= SLOTS as u32 || fo >= image_size || mask & (1 << idx) != 0 {
            return E_FORMAT;
        }
        if read_volatile(addr_of!(s.slots[idx as usize])) != 0 {
            return E_SLOT;
        }
        mask |= 1 << idx;
        i += 1;
    }
    // already loaded; a free record
    let mut rec: usize = MAX_MODULES;
    i = 0;
    while i < MAX_MODULES {
        if s.modules[i].used != 0 {
            if cstr_eq(s.modules[i].name.as_ptr(), name) {
                return E_LOADED;
            }
        } else if rec == MAX_MODULES {
            rec = i;
        }
        i += 1;
    }
    if rec == MAX_MODULES {
        return E_TABLE_FULL;
    }

    // ---- allocate, copy, relocate, zero bss ----
    let mem = arena::alloc(total);
    if mem.is_null() {
        return E_NOMEM;
    }
    let base = mem as u32;
    i = 0;
    while i < image_size as usize {
        *mem.add(i) = *f.add(image_off as usize + i);
        i += 1;
    }
    i = 0;
    while i < bss as usize {
        *mem.add(image_size as usize + i) = 0;
        i += 1;
    }
    i = 0;
    while i < reloc_n as usize {
        let e = f.add(reloc_off as usize + i * 8);
        let off = rd32(e) as usize;
        let kind = rd32(e.add(4));
        let add = if kind >> 24 == 0 {
            base
        } else {
            import_addrs[(kind & 0xff_ffff) as usize]
        };
        let p = mem.add(off);
        wr32(p, rd32(p).wrapping_add(add));
        i += 1;
    }
    // ---- caches: write the lines back and drop them from both caches ----
    flush(base, total);

    // ---- record, init, publish ----
    let m: &mut Module = &mut s.modules[rec];
    m.used = 1;
    i = 0;
    while i < 32 {
        m.name[i] = *name.add(i);
        i += 1;
    }
    m.version = rd32(f.add(H_VERSION));
    m.base = base;
    m.size = total;
    m.fini = if fini == NONE { 0 } else { base + fini };
    m.slot_mask = 0;
    let mut fns = [0u32; SLOTS];
    i = 0;
    while i < slot_n as usize {
        let e = f.add(slot_off as usize + i * 8);
        let idx = rd32(e) as usize;
        let fnaddr = base + rd32(e.add(4));
        m.slot_fn[idx] = fnaddr;
        fns[idx] = fnaddr;
        i += 1;
    }
    if init != NONE {
        let call: unsafe extern "C" fn(*const Api) -> i32 =
            core::mem::transmute((base + init) as usize);
        let r = call(addr_of!(dkrt_api));
        if r != 0 {
            m.used = 0;
            arena::free(mem);
            return E_INIT;
        }
    }
    let mut idx = 0;
    while idx < SLOTS {
        if mask & (1 << idx) != 0 {
            write_volatile(addr_of_mut!(s.slots[idx]), fns[idx]);
        }
        idx += 1;
    }
    m.slot_mask = mask;
    s.loads = s.loads.wrapping_add(1);
    OK
}

/// See `dkrt_unload`.
pub unsafe fn unload(name: *const u8) -> i32 {
    init_once();
    let s = st();
    let mut rec = MAX_MODULES;
    let mut i = 0;
    while i < MAX_MODULES {
        if s.modules[i].used != 0 && cstr_eq(s.modules[i].name.as_ptr(), name) {
            rec = i;
        }
        i += 1;
    }
    if rec == MAX_MODULES {
        return E_NOT_FOUND;
    }
    let m: &mut Module = &mut s.modules[rec];
    // 1. clear the slots (one aligned store each) so no new call starts
    let mut idx = 0;
    while idx < SLOTS {
        if m.slot_mask & (1 << idx) != 0 {
            write_volatile(addr_of_mut!(s.slots[idx]), 0);
        }
        idx += 1;
    }
    // 2. wait until no task is inside one of its slot functions
    let mut spin = 0;
    loop {
        let mut busy = false;
        idx = 0;
        while idx < SLOTS {
            if m.slot_mask & (1 << idx) != 0 && read_volatile(addr_of!(s.entered[idx])) != 0 {
                busy = true;
            }
            idx += 1;
        }
        if !busy {
            break;
        }
        spin += 1;
        if spin > SPIN {
            // give up: put the slots back, the module stays loaded
            idx = 0;
            while idx < SLOTS {
                if m.slot_mask & (1 << idx) != 0 {
                    write_volatile(addr_of_mut!(s.slots[idx]), m.slot_fn[idx]);
                }
                idx += 1;
            }
            return E_BUSY;
        }
    }
    // 3. fini, 4. free
    if m.fini != 0 {
        let call: unsafe extern "C" fn(*const Api) = core::mem::transmute(m.fini as usize);
        call(addr_of!(dkrt_api));
    }
    arena::free(m.base as *mut u8);
    m.used = 0;
    m.slot_mask = 0;
    OK
}
