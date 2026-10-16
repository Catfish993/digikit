//! The run-time registry: machines and parameters that a loaded `.dkm` adds to the rows `core/dynreg` reserved at patch
//! time (docs/design/digikit-registry.md). Everything here runs in the UI task, from `dkrt_load` and `dkrt_unload`.
//!
//! The runtime does not link to `core/machines` or `core/params`. `core/dynreg` exports one table, `dynreg_info`, with the
//! addresses of the rows and tables and the row layouts; the link is optional, so an image with the runtime and without
//! `core/dynreg` loads modules that carry no `register` records and refuses the ones that do (`E_NO_REGISTRY`).
//!
//! Rules: all checks run before the first write (`check`); rows are written in an order that a reader never sees a
//! half-filled machine (the name pointer, which marks a row as used, is the last store, one aligned 32-bit word); the
//! strings the rows point to live in this module's own state, never in the arena, so unload cannot leave a dangling pointer.
use crate::{cstr_len, rd32};
use core::ptr::{addr_of, addr_of_mut, read_volatile, write_volatile};
use dkrt_sdk::*;

pub const E_NO_REGISTRY: i32 = -20; // the file has register records, the image has no core/dynreg
pub const E_REG_DEVICE: i32 = -21; // the records are for another device
pub const E_TYPE_RANGE: i32 = -22; // a machine type outside the reserved range, or a clone_of that is not built in
pub const E_TYPE_USED: i32 = -23; // the type number is in use
pub const E_PARAM_FULL: i32 = -24; // no free reserved parameter row

const REG_MAGIC: u32 = 0x444b_5247; // "DKRG"
const INFO_MAGIC: u32 = 0x4459_4e52; // "DYNR"
const KIND_PARAM: u32 = 1;
const KIND_MACHINE: u32 = 2;
const LEN_PARAM: u32 = 0x50;
const LEN_MACHINE: u32 = 0x6c;
const KNOB_PARAM: u32 = 0x8000_0000;
const MAX_RECORDS: u32 = 24;
const MAX_MACH: usize = 8;
const MAX_PARAM: usize = 16;
const MACH_TEXT: usize = 24;
const REP_SIZE: usize = 48;

extern "C" {
    /// Data symbol of core/dynreg; resolves to 0 when the module is not in the image (`[object] optional`).
    fn dynreg_info();
}

/// Words of `dynreg_info` (byte offsets / 4).
mod w {
    pub const MAGIC: usize = 0;
    pub const DEVICE: usize = 2;
    pub const M_ROWS: usize = 3;
    pub const M_ROW_SIZE: usize = 4;
    pub const M_FIRST: usize = 5;
    pub const M_DYN0: usize = 7;
    pub const M_DYN_N: usize = 8;
    pub const NAMES: usize = 9;
    pub const PERMIT: usize = 10;
    pub const PERTYPE: usize = 11;
    pub const O_LONG: usize = 12;
    pub const O_SHORT: usize = 13;
    pub const O_F0: usize = 14;
    pub const O_CLONE: usize = 15;
    pub const O_POS: usize = 16;
    pub const O_OWN: usize = 17;
    pub const O_NAME: usize = 18;
    pub const O_SNAME: usize = 19;
    pub const P_TABLE: usize = 20;
    pub const P_ROWS: usize = 21;
    pub const P_ROW_SIZE: usize = 22;
    pub const P_FIRST: usize = 23;
    pub const P_DYN0: usize = 25;
    pub const P_DYN_N: usize = 26;
    pub const PF_SIZE: usize = 27;
    pub const P_TEMPLATE_OFF: usize = 28;
    pub const EMPTY: usize = 29;
    pub const SPEC: usize = 30; // 15 words
    pub const KNOBS: usize = 45;
    pub const SLOT_BASE: usize = 46;
    pub const RESERVE_TEMPLATE: usize = 47;
    pub const VIEW_VAR: usize = 48;
    pub const VIEW_BUILD: usize = 49;
}

#[repr(C, align(4))]
struct Strings {
    mach_name: [[u8; MACH_TEXT]; MAX_MACH],
    mach_short: [[u8; MACH_TEXT]; MAX_MACH],
    rep_long: [[u8; REP_SIZE]; MAX_MACH],
    rep_short: [[u8; REP_SIZE]; MAX_MACH],
    param_name: [[u8; 32]; MAX_PARAM],
    param_short: [[u8; 16]; MAX_PARAM],
}

/// The registry's state: who owns each reserved row (module record index + 1, 0 = free) and the strings the rows use.
#[repr(C, align(4))]
pub struct RegState {
    pub magic: u32,
    pub registers: u32,
    pub removes: u32,
    pub mach_owner: [u8; MAX_MACH],
    pub param_owner: [u8; MAX_PARAM],
    /// An empty std::string representation (length 0, capacity 0, reference count -1, NUL) for a free machine row.
    empty_rep: [u32; 4],
    s: Strings,
}

#[no_mangle]
pub static mut DKRT_REG: RegState = unsafe { core::mem::zeroed() };

fn rs() -> &'static mut RegState {
    unsafe { &mut *addr_of_mut!(DKRT_REG) }
}

unsafe fn info() -> *const u32 {
    let a = core::hint::black_box(dynreg_info as usize);
    if a == 0 {
        return core::ptr::null();
    }
    let p = a as *const u32;
    if read_volatile(p.add(w::MAGIC)) != INFO_MAGIC {
        return core::ptr::null();
    }
    p
}

unsafe fn iw(i: *const u32, k: usize) -> u32 {
    read_volatile(i.add(k))
}

unsafe fn rd(a: u32) -> u32 {
    read_volatile(a as *const u32)
}

unsafe fn wr(a: u32, v: u32) {
    write_volatile(a as *mut u32, v)
}

/// A NUL-terminated printable-ASCII text of exactly `field` bytes, 1 to `field - 1` characters.
unsafe fn text_ok(p: *const u8, field: usize) -> bool {
    let n = cstr_len(p, field);
    if n == 0 || n == field {
        return false;
    }
    let mut i = 0;
    while i < field {
        let c = *p.add(i);
        if i < n {
            if !(0x20..0x7f).contains(&c) {
                return false;
            }
        } else if c != 0 {
            return false;
        }
        i += 1;
    }
    true
}

unsafe fn copy_text(dst: *mut u8, src: *const u8, n: usize) {
    let mut i = 0;
    while i < n {
        *dst.add(i) = *src.add(i);
        i += 1;
    }
}

/// Build a std::string representation {len, cap, refcount -1, upper-cased chars, 0} at `rep`; returns the chars' address.
unsafe fn make_rep(rep: *mut u8, src: *const u8) -> u32 {
    let n = cstr_len(src, MACH_TEXT) as u32;
    crate::wr32(rep, n);
    crate::wr32(rep.add(4), n);
    crate::wr32(rep.add(8), 0xffff_ffff);
    let mut i = 0;
    while i < n as usize {
        let c = *src.add(i);
        *rep.add(12 + i) = if c.is_ascii_lowercase() { c - 32 } else { c };
        i += 1;
    }
    *rep.add(12 + n as usize) = 0;
    rep as u32 + 12
}

/// Walk the records of a validated section. Calls `f(kind, body)` for each.
unsafe fn each(reg: *const u8, mut f: impl FnMut(u32, *const u8)) {
    let n = rd32(reg.add(12));
    let mut at = 16usize;
    let mut i = 0;
    while i < n {
        let kind = rd32(reg.add(at));
        let len = rd32(reg.add(at + 4)) as usize;
        f(kind, reg.add(at + 8));
        at += len;
        i += 1;
    }
}

/// Validate the `register` records of a module file. `reg` points into the file (the staging buffer). Nothing is written.
pub unsafe fn check(reg: *const u8, size: u32) -> i32 {
    if size == 0 {
        return OK;
    }
    let inf = info();
    if inf.is_null() {
        return E_NO_REGISTRY;
    }
    // the table must fit this registry's state (a table that says more rows than the state has would index past it)
    if iw(inf, w::M_DYN_N) as usize > MAX_MACH || iw(inf, w::P_DYN_N) as usize > MAX_PARAM {
        return E_NO_REGISTRY;
    }
    if size < 16 || rd32(reg) != REG_MAGIC || rd32(reg.add(4)) != 1 {
        return E_FORMAT;
    }
    if rd32(reg.add(8)) != iw(inf, w::DEVICE) {
        return E_REG_DEVICE;
    }
    let n = rd32(reg.add(12));
    if n > MAX_RECORDS {
        return E_FORMAT;
    }
    // pass 1: layout, counts
    let mut at: u32 = 16;
    let (mut np, mut nm) = (0u32, 0u32);
    let mut i = 0;
    while i < n {
        if at > size || size - at < 8 {
            return E_FORMAT;
        }
        let kind = rd32(reg.add(at as usize));
        let len = rd32(reg.add(at as usize + 4));
        let want = match kind {
            KIND_PARAM => LEN_PARAM,
            KIND_MACHINE => LEN_MACHINE,
            _ => return E_FORMAT,
        };
        if len != want || size - at < len {
            return E_FORMAT;
        }
        if kind == KIND_PARAM {
            np += 1;
        } else {
            nm += 1;
        }
        at += len;
        i += 1;
    }
    if at != size || np as usize > MAX_PARAM || nm as usize > MAX_MACH {
        return E_FORMAT;
    }
    // pass 2: the records
    let (p_first, knobs, slot_base) = (iw(inf, w::P_FIRST), iw(inf, w::KNOBS), iw(inf, w::SLOT_BASE));
    let (dyn0, dyn_n, m_first) = (iw(inf, w::M_DYN0), iw(inf, w::M_DYN_N), iw(inf, w::M_FIRST));
    let s = rs();
    let mut seen_types = [0u32; MAX_MACH];
    let mut ns = 0usize;
    let mut bad = OK;
    let mut pi = 0u32;
    each(reg, |kind, b| {
        if bad != OK {
            return;
        }
        if kind == KIND_PARAM {
            let index = rd32(b);
            let (template, slot) = (rd32(b.add(4)), rd32(b.add(8)));
            let (min, max) = (rd32(b.add(12)) as i32, rd32(b.add(16)) as i32);
            if index != pi
                || template >= p_first
                || slot < slot_base
                || slot >= slot_base + knobs
                || min > max
                || !text_ok(b.add(24), 32)
                || !text_ok(b.add(56), 16)
            {
                bad = E_FORMAT;
            }
            pi += 1;
        } else {
            let t = rd32(b);
            let clone = rd32(b.add(4));
            if rd32(b.add(8)) != 255 || rd32(b.add(12)) != 0 || rd32(b.add(16)) > 0xffff {
                bad = E_FORMAT;
                return;
            }
            if t < dyn0 || t >= dyn0 + dyn_n || clone >= m_first {
                bad = E_TYPE_RANGE;
                return;
            }
            let mut j = 0;
            while j < ns {
                if seen_types[j] == t {
                    bad = E_FORMAT;
                    return;
                }
                j += 1;
            }
            seen_types[ns] = t;
            ns += 1;
            let k = (t - dyn0) as usize;
            let row = iw(inf, w::M_ROWS) + (t - m_first) * iw(inf, w::M_ROW_SIZE);
            let name = rd(row + iw(inf, w::O_NAME));
            if s.mach_owner[k] != 0 || (name != 0 && *(name as *const u8) != 0) {
                bad = E_TYPE_USED;
                return;
            }
            let mut j = 0;
            while j < knobs as usize {
                let kn = rd32(b.add(20 + 4 * j));
                if kn & KNOB_PARAM != 0 {
                    if (kn & !KNOB_PARAM) >= np {
                        bad = E_FORMAT;
                        return;
                    }
                } else if kn >= p_first {
                    bad = E_FORMAT;
                    return;
                }
                j += 1;
            }
            if !text_ok(b.add(52), MACH_TEXT) || !text_ok(b.add(52 + MACH_TEXT), MACH_TEXT) {
                bad = E_FORMAT;
            }
        }
    });
    if bad != OK {
        return bad;
    }
    // free reserved parameter rows
    let mut free = 0u32;
    let mut k = 0;
    while k < iw(inf, w::P_DYN_N) as usize && k < MAX_PARAM {
        if s.param_owner[k] == 0 {
            free += 1;
        }
        k += 1;
    }
    if free < np {
        return E_PARAM_FULL;
    }
    OK
}

/// Write one 60-byte parameter table row from its lowering spec (the template's words, the record's values).
unsafe fn write_param_row(
    inf: *const u32,
    code: u32,
    template: u32,
    slot: u32,
    min: u32,
    max: u32,
    def: u32,
    name: u32,
    short: u32,
) {
    let table = iw(inf, w::P_TABLE);
    let size = iw(inf, w::PF_SIZE);
    let row = table + code * size;
    let tpl = table + template * size;
    // values by spec code 0x100 ..: min, max, default, name, short, mirror index, kind word, 0x80 + slot (no `match`: a
    // jump table would put data into the code section)
    let t = ((slot + 1) >> 2) & 1;
    let u = 1i32.wrapping_sub((slot >> 2) as i32);
    let kv = (0x10u32 + slot).wrapping_add((t as i32).wrapping_mul(u).wrapping_mul(0xffec) as u32);
    let vals: [u32; 8] = [
        min,
        max,
        def,
        name,
        short,
        25 + slot + (slot >> 2) * 2,
        (kv << 16) | (rd(tpl + 0x18) & 0xffff),
        0x80 + slot,
    ];
    let mut i = 0;
    while i < 15 {
        let spec = iw(inf, w::SPEC + i);
        let v = if spec <= 0x3c {
            rd(tpl + spec)
        } else {
            vals[(spec as usize - 0x100) & 7]
        };
        wr(row + 4 * i as u32, v);
        i += 1;
    }
    // the registry row (core/params): name, short, template, slot, min, max, default; pf_t2 reads `template`
    let reg_row = iw(inf, w::P_ROWS) + (code - iw(inf, w::P_FIRST)) * iw(inf, w::P_ROW_SIZE);
    wr(reg_row, name);
    wr(reg_row + 4, short);
    wr(reg_row + iw(inf, w::P_TEMPLATE_OFF), template);
    wr(reg_row + iw(inf, w::P_TEMPLATE_OFF) + 4, slot);
    wr(reg_row + iw(inf, w::P_TEMPLATE_OFF) + 8, min);
    wr(reg_row + iw(inf, w::P_TEMPLATE_OFF) + 12, max);
    wr(reg_row + iw(inf, w::P_TEMPLATE_OFF) + 16, def);
}

/// The firmware builds the machine selection view's item list once, from the list getter. After the rows changed, build it
/// again (it clears its items first); nothing to do while the view does not exist yet.
unsafe fn refresh_view(inf: *const u32) {
    let var = iw(inf, w::VIEW_VAR);
    let build = iw(inf, w::VIEW_BUILD);
    if var == 0 || build == 0 {
        return;
    }
    let view = rd(var);
    if view != 0 {
        let f: unsafe extern "C" fn(u32) = core::mem::transmute(build as usize);
        f(view);
    }
}

/// Fill the reserved rows from the (checked) records. `owner` = module record index + 1.
pub unsafe fn apply(owner: u8, reg: *const u8, size: u32) -> i32 {
    if size == 0 {
        return OK;
    }
    let inf = info();
    if inf.is_null() {
        return E_NO_REGISTRY;
    }
    let s = rs();
    let n = rd32(reg.add(12));
    // parameters first: their codes are what the machines' knobs name
    let mut codes = [0u32; MAX_PARAM];
    let mut np = 0usize;
    each(reg, |kind, b| {
        if kind != KIND_PARAM {
            return;
        }
        let mut k = 0;
        while k < MAX_PARAM && s.param_owner[k] != 0 {
            k += 1;
        }
        if k == MAX_PARAM {
            return; // cannot happen after `check`
        }
        s.param_owner[k] = owner;
        let code = iw(inf, w::P_DYN0) + k as u32;
        codes[np] = code;
        np += 1;
        copy_text(s.s.param_name[k].as_mut_ptr(), b.add(24), 32);
        copy_text(s.s.param_short[k].as_mut_ptr(), b.add(56), 16);
        write_param_row(
            inf,
            code,
            rd32(b.add(4)),
            rd32(b.add(8)),
            rd32(b.add(12)),
            rd32(b.add(16)),
            rd32(b.add(20)),
            s.s.param_name[k].as_ptr() as u32,
            s.s.param_short[k].as_ptr() as u32,
        );
    });
    let _ = n;
    // machines
    let knobs = iw(inf, w::KNOBS) as usize;
    each(reg, |kind, b| {
        if kind != KIND_MACHINE {
            return;
        }
        let t = rd32(b);
        let clone = rd32(b.add(4));
        let k = (t - iw(inf, w::M_DYN0)) as usize;
        let row = iw(inf, w::M_ROWS) + (t - iw(inf, w::M_FIRST)) * iw(inf, w::M_ROW_SIZE);
        let f0 = row + iw(inf, w::O_F0);
        let mut j = 0;
        while j < knobs {
            let kn = rd32(b.add(20 + 4 * j));
            wr(f0 + 4 * j as u32, if kn & KNOB_PARAM != 0 { codes[(kn & !KNOB_PARAM) as usize] } else { kn });
            j += 1;
        }
        wr(f0 + 4 * knobs as u32, rd32(b.add(16))); // the level, the field after the knobs
        wr(row + iw(inf, w::O_CLONE), clone);
        wr(row + iw(inf, w::O_POS), rd32(b.add(8)));
        wr(row + iw(inf, w::O_OWN), 0);
        // strings
        copy_text(s.s.mach_name[k].as_mut_ptr(), b.add(52), MACH_TEXT);
        copy_text(s.s.mach_short[k].as_mut_ptr(), b.add(52 + MACH_TEXT), MACH_TEXT);
        let long = make_rep(s.s.rep_long[k].as_mut_ptr(), s.s.mach_name[k].as_ptr());
        let short = make_rep(s.s.rep_short[k].as_mut_ptr(), s.s.mach_short[k].as_ptr());
        wr(row + iw(inf, w::O_LONG), long);
        wr(row + iw(inf, w::O_SHORT), short);
        wr(row + iw(inf, w::O_SNAME), s.s.mach_short[k].as_ptr() as u32);
        // the per-type tables: the clone's row, our names
        let nrow = iw(inf, w::NAMES) + 12 * t;
        wr(nrow, s.s.mach_name[k].as_ptr() as u32);
        wr(nrow + 4, s.s.mach_short[k].as_ptr() as u32);
        wr(nrow + 8, rd(iw(inf, w::NAMES) + 12 * clone + 8));
        wr(iw(inf, w::PERMIT) + 4 * t, rd(iw(inf, w::PERMIT) + 4 * clone));
        let pt = iw(inf, w::PERTYPE);
        if pt != 0 {
            *((pt + t) as *mut u8) = *((pt + clone) as *const u8);
        }
        // publish: the name pointer marks the row as used (one aligned store)
        s.mach_owner[k] = owner;
        wr(row + iw(inf, w::O_NAME), s.s.mach_name[k].as_ptr() as u32);
    });
    s.registers = s.registers.wrapping_add(1);
    refresh_view(inf);
    OK
}

/// Give back every row `owner` filled: the machines first (hidden, then reset to a gap row), then the parameters.
pub unsafe fn remove(owner: u8) {
    let inf = info();
    if inf.is_null() {
        return;
    }
    let s = rs();
    let empty = iw(inf, w::EMPTY);
    let knobs = iw(inf, w::KNOBS) as usize;
    let first = iw(inf, w::M_FIRST);
    let mut k = 0;
    while k < MAX_MACH {
        if s.mach_owner[k] == owner {
            let t = iw(inf, w::M_DYN0) + k as u32;
            let row = iw(inf, w::M_ROWS) + (t - first) * iw(inf, w::M_ROW_SIZE);
            wr(row + iw(inf, w::O_NAME), empty); // hide first
            s.empty_rep = [0, 0, 0xffff_ffff, 0];
            let rep = addr_of!(DKRT_REG.empty_rep) as u32 + 12;
            wr(row + iw(inf, w::O_LONG), rep);
            wr(row + iw(inf, w::O_SHORT), rep);
            wr(row + iw(inf, w::O_SNAME), empty);
            let f0 = row + iw(inf, w::O_F0);
            let mut j = 0;
            while j < knobs {
                wr(f0 + 4 * j as u32, 0);
                j += 1;
            }
            wr(f0 + 4 * knobs as u32, 10);
            wr(row + iw(inf, w::O_CLONE), 0);
            wr(row + iw(inf, w::O_OWN), 0);
            let nrow = iw(inf, w::NAMES) + 12 * t;
            wr(nrow, empty);
            wr(nrow + 4, empty);
            wr(nrow + 8, rd(iw(inf, w::NAMES) + 8));
            wr(iw(inf, w::PERMIT) + 4 * t, rd(iw(inf, w::PERMIT)));
            let pt = iw(inf, w::PERTYPE);
            if pt != 0 {
                *((pt + t) as *mut u8) = *(pt as *const u8);
            }
            s.mach_owner[k] = 0;
        }
        k += 1;
    }
    k = 0;
    while k < MAX_PARAM {
        if s.param_owner[k] == owner {
            write_param_row(
                inf,
                iw(inf, w::P_DYN0) + k as u32,
                iw(inf, w::RESERVE_TEMPLATE),
                iw(inf, w::SLOT_BASE),
                0,
                0,
                0,
                empty,
                empty,
            );
            s.param_owner[k] = 0;
        }
        k += 1;
    }
    s.removes = s.removes.wrapping_add(1);
    refresh_view(inf);
}
