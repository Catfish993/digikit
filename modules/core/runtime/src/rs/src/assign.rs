//! Sticky machine type numbers (docs/design/module-sdk.md 11.5 D7, docs/design/digikit-registry.md "Machine keys").
//!
//! A run-time machine has no fixed type number. Its `.dkm` carries a machine key, `hash24 << 8` (the hash is the low 24 bits of FNV-1a 32 of
//! `<module id>:<machine id>`), and the runtime gives it a number in the run-time range when it loads the module. The card remembers the
//! numbers in a map of up to 8 entries in the selection record's reserved bytes (`sel::Record::map`, entry = `hash24 << 8 | type`, 0 = empty), so
//! a machine keeps its number while it stays in the map, and a project that names the number keeps playing that machine.
//!
//! `plan` runs once per boot, before the first module loads: it reads the machine keys of every selected module (the staging buffer is
//! free then), assigns all numbers in load order and leaves the table `resolve` answers from. Doing it for the whole selection first is what
//! lets the eviction rule see which map entries belong to modules that load in this boot. The map is written back by the boot hook after the
//! loads, and only when it changed (`dirty`).
//!
//! Rules of the assignment, per machine in load order:
//! 1. the map has an entry for its hash and that number is not taken in this boot: use it (sticky);
//! 2. else the lowest number that no map entry references and this boot has not given out; record the entry;
//! 3. else drop the lowest-index entry whose hash is not among this boot's machines (logged), take its number, record the entry;
//! 4. two machines with the same key in one boot: the second takes a fresh number and is not recorded.
use crate::{dlog, loader};
use core::ptr::addr_of_mut;
use dkrt_sdk::device::{DYN_TYPE_FIRST, DYN_TYPE_LAST};
use dkrt_sdk::sel::{Record, MAP_N};
use dkrt_sdk::*;

/// Machines a boot can name: 8 selected modules with 8 machines each.
const MAX_KEYS: usize = SEL_MAX * 8;
const NONE: u32 = 0xffff_ffff;

/// The boot's assignment, peekable by the proof tools (`DKRT_ASSIGN`): `magic` "DKAS", `n` machines planned, `dirty` 1 when the map changed.
#[repr(C, align(4))]
pub struct State {
    magic: u32,
    n: usize,
    dirty: u32,
    /// The map as it is now (starts as the record's, cleaned).
    map: [u32; MAP_N],
    /// Per planned machine: the selection index of its module, its key, its number (0: none was free).
    idx: [u32; MAX_KEYS],
    key: [u32; MAX_KEYS],
    ty: [u32; MAX_KEYS],
}

#[no_mangle]
pub static mut DKRT_ASSIGN: State = State { magic: 0, n: 0, dirty: 0, map: [0; MAP_N], idx: [0; MAX_KEYS], key: [0; MAX_KEYS], ty: [0; MAX_KEYS] };

fn st() -> &'static mut State {
    unsafe { &mut *addr_of_mut!(DKRT_ASSIGN) }
}

fn in_range(t: u32) -> bool {
    t >= DYN_TYPE_FIRST && t <= DYN_TYPE_LAST
}

fn bit(t: u32) -> u32 {
    1 << (t - DYN_TYPE_FIRST)
}

/// Type numbers referenced by the map.
fn referenced(map: &[u32; MAP_N]) -> u32 {
    let mut m = 0;
    let mut i = 0;
    while i < MAP_N {
        if map[i] != 0 {
            m |= bit(map[i] & 0xff);
        }
        i += 1;
    }
    m
}

fn lowest_free(taken: u32) -> u32 {
    let mut t = DYN_TYPE_FIRST;
    while t <= DYN_TYPE_LAST {
        if taken & bit(t) == 0 {
            return t;
        }
        t += 1;
    }
    0
}

/// The map as read from the record without entries that cannot be right: a type outside the range, no hash, a number or a hash that an earlier
/// entry has.
fn clean(map: &[u32; MAP_N]) -> [u32; MAP_N] {
    let mut out = [0u32; MAP_N];
    let mut seen = 0u32;
    let mut i = 0;
    while i < MAP_N {
        let (h, t) = (map[i] >> 8, map[i] & 0xff);
        let mut dup_hash = false;
        let mut j = 0;
        while j < i {
            dup_hash |= out[j] != 0 && out[j] >> 8 == h;
            j += 1;
        }
        if h != 0 && in_range(t) && seen & bit(t) == 0 && !dup_hash {
            out[i] = map[i];
            seen |= bit(t);
        }
        i += 1;
    }
    out
}

fn find_hash(map: &[u32; MAP_N], h: u32) -> Option<usize> {
    let mut i = 0;
    while i < MAP_N {
        if map[i] != 0 && map[i] >> 8 == h {
            return Some(i);
        }
        i += 1;
    }
    None
}

/// Assign the numbers of every machine of the selected modules. Call once, at the start of the boot load, with the record the boot started from.
pub unsafe fn plan(rec: &Record) {
    let a = st();
    a.magic = 0x444b_4153; // "DKAS"
    a.n = 0;
    a.map = clean(&rec.map);
    // the machines of the selection, in load order
    let mut k = 0;
    while k < rec.count as usize {
        let mut keys = [0u32; 8];
        let n = loader::machine_keys(rec.paths[k].as_ptr(), &mut keys);
        let mut i = 0;
        while i < n && a.n < MAX_KEYS {
            a.idx[a.n] = k as u32;
            a.key[a.n] = keys[i];
            a.ty[a.n] = 0;
            a.n += 1;
            i += 1;
        }
        k += 1;
    }
    let mut used = 0u32;
    let mut e = 0;
    while e < a.n {
        let key = a.key[e];
        let h = key >> 8;
        if key & 0xff != 0 || h == 0 {
            e += 1;
            continue; // a pinned number or nothing to assign
        }
        let mut first = true;
        let mut j = 0;
        while j < e {
            first &= a.key[j] != key;
            j += 1;
        }
        let mut t = 0;
        let mut sticky = 0;
        if first {
            let at = find_hash(&a.map, h);
            if let Some(i) = at {
                let old = a.map[i] & 0xff;
                if used & bit(old) == 0 {
                    t = old;
                    sticky = 1;
                }
            }
            if t == 0 {
                t = lowest_free(used | referenced(&a.map));
                if t == 0 {
                    // no number is unreferenced: drop the lowest entry of a machine that is not in this boot
                    let mut victim = NONE;
                    let mut i = 0;
                    while i < MAP_N && victim == NONE {
                        if a.map[i] != 0 && !wanted(a, a.map[i] >> 8) {
                            victim = i as u32;
                        }
                        i += 1;
                    }
                    if victim != NONE {
                        let v = a.map[victim as usize];
                        dlog::ev(logk::MACHINE_TYPE_EVICTED, logk::F_ERROR, [v >> 8, v & 0xff, 0, 0, 0]);
                        a.map[victim as usize] = 0;
                        t = v & 0xff;
                    }
                }
                if t != 0 {
                    // record the entry: the slot the hash had, else the first empty one
                    let slot = match at {
                        Some(i) => i,
                        None => first_empty(&a.map),
                    };
                    if slot < MAP_N {
                        a.map[slot] = key | t;
                    }
                }
            }
        } else {
            t = lowest_free(used | referenced(&a.map));
            if t == 0 {
                t = lowest_free(used);
            }
        }
        if t != 0 {
            used |= bit(t);
        }
        a.ty[e] = t;
        dlog::ev(logk::MACHINE_TYPE_ASSIGNED, dlog::INFO, [h, t, sticky, 0, 0]);
        e += 1;
    }
    a.dirty = 0;
    let mut i = 0;
    while i < MAP_N {
        a.dirty |= (a.map[i] != rec.map[i]) as u32;
        i += 1;
    }
}

fn first_empty(map: &[u32; MAP_N]) -> usize {
    let mut i = 0;
    while i < MAP_N {
        if map[i] == 0 {
            return i;
        }
        i += 1;
    }
    MAP_N
}

/// Is `h` the hash of a machine of this boot's selection?
fn wanted(a: &State, h: u32) -> bool {
    let mut i = 0;
    while i < a.n {
        if a.key[i] >> 8 == h {
            return true;
        }
        i += 1;
    }
    false
}

/// The type number for machine key `key` of the module being loaded (selection index `loader::BOOT_IDX`): the number `plan` assigned, a pinned
/// number as it is (when it is in the run-time range), 0 when there is none.
pub unsafe fn resolve(key: u32) -> u32 {
    if key & 0xff != 0 {
        return if key >> 8 == 0 && in_range(key) { key } else { 0 };
    }
    if key == 0 {
        return 0;
    }
    let a = st();
    let at = core::ptr::read_volatile(addr_of_mut!(loader::BOOT_IDX));
    let mut i = 0;
    while i < a.n {
        if a.key[i] == key && a.idx[i] == at {
            return a.ty[i];
        }
        i += 1;
    }
    0
}

/// The map to write back when the boot changed it.
pub fn changed_map() -> Option<[u32; MAP_N]> {
    let a = st();
    if a.dirty != 0 {
        Some(a.map)
    } else {
        None
    }
}
