//! Module upload over USB SysEx (docs/design/digikit-upload.md): the device side.
//!
//! `dkrt_midi_rx` is called from the firmware's MIDI task (hook `dkrt.hook.midi_rx`, `src/cf/tramp.s`) with every SysEx message that
//! came in on USB cable 0, after the firmware gathered it and before its router. A message that starts `F0 7D 64 6B` is ours: it is
//! handled here (one request, one reply, in order) and the router never sees it; any other message returns 0 and takes the stock route.
//!
//! A reply is sent only when the firmware's TX ring has room (`tx_room`), so a host that does not read its input cannot hang the MIDI task.
//!
//! Store writes go through the firmware's block write, only to store sectors: staged data and tables in `[1, LIMIT)`, the two header
//! copies at sector 0 and `HEADER_B`. Never the selection record (the two sectors after `HEADER_B`). Rules: no `match` on integers (a
//! dense one becomes a jump table, which is not ColdFire code), no `core::fmt`, no slice indexing (a bounds panic hangs the task).

use crate::sha256::Sha;
use crate::{block_read, block_write, boot, card, dlog, midi_send, rd32, tx_in_flight, wr32};
use core::ptr::{addr_of, addr_of_mut};
use dkrt_sdk::store::{self, Hdr, ENTRY, FIRST, HEADER_B, LIMIT};
use dkrt_sdk::proto::*;
use dkrt_sdk::{logk, E_NOT_FOUND, OK, RUNTIME_VERSION};

// The protocol numbers (commands, the `E_*` statuses, `VERSION`, the INFO capability bits) are `dkrt_sdk::proto`, written by `patchkit gen`
// from `[protocol]` of `sdk/sdk.toml`. E_INUSE: the card's store area is not ours and not blank, the runtime writes nothing there
// (finding 46). E_PEEK: the range is not inside a range the device file lists, or the length is 0 or over 512.

/// Largest DATA chunk (INFO says so) and the unpacked body we accept: offset (4) + chunk.
pub const MAX_CHUNK: u32 = 32768;
/// The unpacked request body starts here in the buffer, so that a DATA chunk (after its 4-byte offset) is 16-byte aligned.
const BODY_AT: usize = 12;
const BODY_MAX: usize = 4 + MAX_CHUNK as usize;
/// The buffer: 32 KiB of body room, then a scratch region (table read-back, header sector).
const SCRATCH_AT: usize = 16 + MAX_CHUNK as usize + 16;
const SCRATCH_BYTES: usize = 4096;
const BUF_BYTES: usize = SCRATCH_AT + SCRATCH_BYTES;

/// The buffer: static (bss of this object, so patchkit reserves it with the code), 16-byte aligned. A static address lets the write-watch
/// of the proofs leave out the card reads that land here by DMA, as it does for `DKRT_STAGE`.
#[repr(C, align(16))]
pub struct UploadBuf([u8; BUF_BYTES]);
#[no_mangle]
pub static mut DKRT_UPLOAD_BUF: UploadBuf = UploadBuf([0; BUF_BYTES]);
/// The table must fit in the 32 KiB: at most 256 entries.
const TABLE_MAX: u32 = (MAX_CHUNK as usize / ENTRY) as u32;
/// Wire bytes of a reply are at most 1 KiB: the unpacked body at most this.
const REPLY_BODY_MAX: usize = 880;
const REQ_WIRE_MAX: u32 = 40 * 1024;
const CLEAR_MAGIC: u32 = 0x434c_5221;

#[repr(C)]
struct Up {
    open: bool,
    have_last: bool,
    path: [u8; 88],
    size: u32,
    sha: [u8; 32],
    /// Next expected byte offset.
    next: u32,
    last_off: u32,
    last_len: u32,
    /// First staged sector, relative to the store (the store's used sectors at BEGIN).
    start: u32,
}

static mut UP: Up = Up {
    open: false,
    have_last: false,
    path: [0; 88],
    size: 0,
    sha: [0; 32],
    next: 0,
    last_off: 0,
    last_len: 0,
    start: 0,
};
/// Reply under construction: the unpacked body, then the wire message.
static mut RBODY: [u8; REPLY_BODY_MAX] = [0; REPLY_BODY_MAX];
static mut RWIRE: [u8; 1100] = [0; 1100];
/// Test knob (the kept power-cut scenarios poke it): when not 0, the upload path performs that many card writes since boot and then
/// refuses every further one (`E_WRITE`), so a snapshot of the card is the state a power cut after the Nth write leaves.
/// `DKRT_UPLOAD_WRITES` counts the writes done.
#[no_mangle]
pub static mut DKRT_UPLOAD_CUT: u32 = 0;
#[no_mangle]
pub static mut DKRT_UPLOAD_WRITES: u32 = 0;
/// Test knob: when not 0, the Nth card write since boot (counted in `DKRT_UPLOAD_WRITES`) is torn when it is one sector: only its first
/// 256 bytes reach the card (a header then fails its CRC), and every later write is refused as with `DKRT_UPLOAD_CUT`.
#[no_mangle]
pub static mut DKRT_UPLOAD_TEAR: crate::boot::Knob = crate::boot::Knob(0);
#[repr(C, align(16))]
struct Sec([u8; 512]);
static mut TEAR_BUF: Sec = Sec([0; 512]);
/// Replies dropped because the TX ring had no room (the host retries).
#[no_mangle]
pub static mut DKRT_UPLOAD_DROPPED: u32 = 0;
/// Requests handled and the last status (the proofs peek these).
#[no_mangle]
pub static mut DKRT_UPLOAD_COUNT: u32 = 0;
#[no_mangle]
pub static mut DKRT_UPLOAD_LAST: i32 = 0;

fn up() -> &'static mut Up {
    unsafe { &mut *addr_of_mut!(UP) }
}

// ---- 8-in-7 --------------------------------------------------------------------------------------------------------------

/// Unpack `n` wire bytes into `dst`; returns the unpacked length. Group: a collector byte, then up to 7 bytes; byte j (1..7) takes
/// bit 7 from collector bit (7 - j).
unsafe fn unpack(src: *const u8, n: usize, dst: *mut u8) -> usize {
    let mut i = 0;
    let mut o = 0;
    while i < n {
        let c = *src.add(i);
        i += 1;
        let mut j = 1;
        while j <= 7 && i < n {
            let b = *src.add(i) & 0x7f;
            *dst.add(o) = b | (((c >> (7 - j)) & 1) << 7);
            o += 1;
            i += 1;
            j += 1;
        }
    }
    o
}

/// Unpacked length of `n` wire bytes.
fn unpacked_len(n: usize) -> usize {
    n - (n + 7) / 8
}

/// Pack `n` bytes into `dst`; returns the wire length.
unsafe fn pack(src: *const u8, n: usize, dst: *mut u8) -> usize {
    let mut i = 0;
    let mut o = 0;
    while i < n {
        let c_at = o;
        o += 1;
        let mut c = 0u8;
        let mut j = 1;
        while j <= 7 && i < n {
            let b = *src.add(i);
            c |= (b >> 7) << (7 - j);
            *dst.add(o) = b & 0x7f;
            o += 1;
            i += 1;
            j += 1;
        }
        *dst.add(c_at) = c;
    }
    o
}

// ---- replies -------------------------------------------------------------------------------------------------------------

/// The most bytes the firmware may hold unsent in its USB MIDI TX ring (4 KiB) when a reply is sent. The TX function spins, with the MIDI
/// task in it, while the bytes queued plus this word exceed 0xffb, and the USB stack has 31 transfer buffers that a send takes one of (and
/// waits for when none is free): our smallest reply is a 16-byte transfer, so 256 bytes cap the replies waiting at 16 and leave the
/// stock traffic (clock, notes: 4 bytes a transfer) its share. Above this the reply is dropped.
const TX_BUSY_MAX: u32 = 256;
/// The ring's size less its own margin (the function's `0xffb`).
const TX_RING: u32 = 0xff0;

/// Is there room to send a reply of `wire` bytes without the firmware's TX function waiting? It packs 3 bytes of a SysEx message into
/// each 4-byte event, and waits only when the bytes in flight plus what it queued reach the ring size (finding 43).
unsafe fn tx_room(wire: usize) -> bool {
    let need = ((wire as u32 + 2) / 3 + 1) * 4;
    match tx_in_flight() {
        Some(f) => f <= TX_BUSY_MAX && f + need <= TX_RING,
        None => false, // the anchor does not point at a counter: no reply rather than a possible hang
    }
}

/// Send the reply for `cmd`/`seq`: status, then the `extra` bytes already in `RBODY[1..1+extra]`.
unsafe fn reply(cmd: u8, seq: u32, status: i32, extra: usize) -> i32 {
    let rb = addr_of_mut!(RBODY) as *mut u8;
    *rb = status as u8;
    let w = addr_of_mut!(RWIRE) as *mut u8;
    *w = 0xf0;
    *w.add(1) = 0x7d;
    *w.add(2) = 0x64;
    *w.add(3) = 0x6b;
    *w.add(4) = VERSION;
    *w.add(5) = (cmd | 0x40) & 0x7f;
    *w.add(6) = (seq & 0x7f) as u8;
    let n = pack(rb, 1 + extra, w.add(7));
    *w.add(7 + n) = 0xf7;
    DKRT_UPLOAD_LAST = status;
    if cmd != CMD_INFO && cmd != CMD_LOG && cmd != CMD_PEEK && cmd != CMD_LOGCFG && cmd != CMD_MODULES && cmd != CMD_CAPACITY {
        dlog::ev(logk::UPLOAD, if status == 0 { dlog::INFO } else { logk::F_ERROR }, [cmd as u32, status as u32, DKRT_UPLOAD_WRITES, 0, 0]);
    }
    if !tx_room(8 + n) {
        DKRT_UPLOAD_DROPPED = DKRT_UPLOAD_DROPPED.wrapping_add(1);
        return 1; // the host reads nothing, or slowly: its retry asks again
    }
    midi_send((8 + n) as u32, w, 2);
    DKRT_UPLOAD_COUNT = DKRT_UPLOAD_COUNT.wrapping_add(1);
    1
}

unsafe fn put32(at: usize, v: u32) {
    wr32((addr_of_mut!(RBODY) as *mut u8).add(at), v);
}

// ---- the store ----------------------------------------------------------------------------------------------------------

/// What the current header says.
struct St {
    used: u32,
    seq: u32,
    count: u32,
    sectors: u32,
    /// Which header copy it came from: 0 = A, 1 = B.
    copy: u32,
    /// The other copy says the same (nothing to repair).
    agree: bool,
}

/// Read the current store; its table is then at `buf`. A card with nothing in the range is an empty store (`used` 1, `seq` 0).
/// `Err(E_NOSTORE)`: a header is there but no copy is usable (or the table is more than 256 entries).
unsafe fn state(buf: *mut u8) -> Result<St, i32> {
    match store::open(rd, buf, MAX_CHUNK as usize) {
        Ok(t) => {
            if t.count > TABLE_MAX {
                return Err(E_NOSTORE);
            }
            Ok(St { used: t.used, seq: t.seq, count: t.count, sectors: t.sectors, copy: t.copy, agree: t.agree })
        }
        Err(e) => {
            if e == E_NOT_FOUND {
                // "No store" is an empty range (or one with only a selection record, which also makes the card ours). Bytes in a header sector that are
                // not a store header are somebody's data: a write would overwrite them, so refuse (finding 46, card safety).
                if !header_sectors_blank(buf.add(SCRATCH_AT)) {
                    return Err(E_NOSTORE);
                }
                Ok(St { used: 1, seq: 0, count: 0, sectors: 0, copy: 0, agree: true })
            } else {
                Err(E_NOSTORE)
            }
        }
    }
}

/// Do both header sectors (A, B) read all 0x00 or all 0xFF? A sector that cannot be read does not count as blank.
unsafe fn header_sectors_blank(scratch: *mut u8) -> bool {
    let mut k = 0;
    while k < 2 {
        let rel = if k == 0 { 0 } else { HEADER_B };
        if block_read(FIRST + rel, 512, scratch) != 0 || !card::blank(scratch) {
            return false;
        }
        k += 1;
    }
    true
}

unsafe extern "C" fn rd(sector: u32, bytes: u32, buf: *mut u8) -> i32 {
    block_read(sector, bytes, buf)
}

/// Write `sectors` at store sector `rel` (data and tables only: `[1, LIMIT)`).
unsafe fn wr_data(rel: u32, sectors: u32, buf: *const u8) -> bool {
    if rel < 1 || sectors == 0 || rel > LIMIT || sectors > LIMIT - rel || sectors > 128 {
        return false;
    }
    card_write(FIRST + rel, sectors * 512, buf)
}

/// The only place the upload path writes the card.
unsafe fn card_write(sector: u32, bytes: u32, buf: *const u8) -> bool {
    let cut = core::ptr::read_volatile(addr_of!(DKRT_UPLOAD_CUT));
    if cut != 0 && DKRT_UPLOAD_WRITES >= cut {
        return false;
    }
    DKRT_UPLOAD_WRITES = DKRT_UPLOAD_WRITES.wrapping_add(1);
    let tear = core::ptr::read_volatile(addr_of!(DKRT_UPLOAD_TEAR.0));
    if tear != 0 && DKRT_UPLOAD_WRITES == tear && bytes == 512 {
        let t = addr_of_mut!(TEAR_BUF) as *mut u8;
        copy(t, buf, 256);
        zero(t.add(256), 256);
        block_write(sector, 512, t);
        core::ptr::write_volatile(addr_of_mut!(DKRT_UPLOAD_CUT), DKRT_UPLOAD_WRITES);
        return false;
    }
    block_write(sector, bytes, buf) == 0
}

/// Write a header sector to copy A (`b` false) or B.
unsafe fn wr_header(b: bool, buf: *const u8) -> bool {
    let rel = if b { HEADER_B } else { 0 };
    let ok = card_write(FIRST + rel, 512, buf);
    crate::store::figures_stale(); // the HEALTH page's used and free figures come from the headers
    ok
}

fn sectors_of(bytes: u32) -> u32 {
    (bytes + 511) / 512
}

fn table_sectors(count: u32) -> u32 {
    (count * ENTRY as u32 + 511) / 512
}

/// Free bytes for files, keeping room for a table of `count + 1` entries.
fn free_bytes(st: &St) -> u32 {
    let room = table_sectors(st.count + 1);
    let free = LIMIT.saturating_sub(st.used).saturating_sub(room);
    free * 512
}

/// A store path: 1 to 87 bytes, no empty, `.` or `..` part, and nothing below 0x20 or 0x7f (the store itself allows any byte but
/// NUL; 7-bit text keeps the paths printable on the hosts).
unsafe fn path_ok(p: *const u8, max: usize) -> Option<usize> {
    let mut n = 0;
    while n < max && *p.add(n) != 0 {
        n += 1;
    }
    if n == 0 || n > 87 || n >= max {
        return None;
    }
    let mut part = 0usize;
    let mut dots = 0usize;
    let mut i = 0;
    while i <= n {
        let c = if i < n { *p.add(i) } else { b'/' };
        if i < n && (c < 0x20 || c == 0x7f) {
            return None;
        }
        if c == b'/' {
            if part == 0 || (dots == part && part <= 2) {
                return None;
            }
            part = 0;
            dots = 0;
        } else {
            part += 1;
            if c == b'.' {
                dots += 1;
            }
        }
        i += 1;
    }
    Some(n)
}

/// Compare NUL-terminated `a` with the 88-byte path field `b`: negative, 0, positive (byte order).
unsafe fn cmp_path(a: *const u8, b: *const u8) -> i32 {
    let mut i = 0;
    while i < 88 {
        let x = *a.add(i);
        let y = *b.add(i);
        if x != y {
            return x as i32 - y as i32;
        }
        if x == 0 {
            return 0;
        }
        i += 1;
    }
    0
}

unsafe fn copy(dst: *mut u8, src: *const u8, n: usize) {
    let mut i = 0;
    while i < n {
        *dst.add(i) = *src.add(i);
        i += 1;
    }
}

unsafe fn zero(dst: *mut u8, n: usize) {
    let mut i = 0;
    while i < n {
        *dst.add(i) = 0;
        i += 1;
    }
}

/// Move `n` bytes from `src` to `dst` where `dst` may overlap above or below `src`.
unsafe fn mv(dst: *mut u8, src: *const u8, n: usize) {
    if (dst as usize) < (src as usize) {
        copy(dst, src, n);
    } else {
        let mut i = n;
        while i > 0 {
            i -= 1;
            *dst.add(i) = *src.add(i);
        }
    }
}

// ---- both headers before space is reused -------------------------------------------------------------------------------------

/// The header sector `rel` as a `Hdr`, `None` when it is not a usable header (the scratch region of `buf` is used).
unsafe fn header_at(rel: u32, scratch: *mut u8) -> Option<Hdr> {
    if block_read(FIRST + rel, 512, scratch) != 0 {
        return None;
    }
    store::parse_header(scratch)
}

/// Make the two header copies say the same thing (finding 43, "CLEAR double fault"). A cut between header B and header A leaves the
/// copies different, the lagging one pointing at the table of the store before. That is harmless while no sector is reused, but a store
/// that starts over (CLEAR) stages its next file over exactly those sectors; if that upload's header B then tears, the lagging A would be
/// the only copy left and would point at overwritten sectors. So BEGIN, which is where staging starts, first rewrites the lagging or
/// unusable copy from the winner. `false`: that write failed (nothing is staged then). A card with no store has nothing to do.
unsafe fn sync_headers(buf: *mut u8, st: &St) -> bool {
    // `store::open` has read both headers already: a healthy store costs nothing here
    if st.seq == 0 || st.agree {
        return true;
    }
    let scratch = buf.add(SCRATCH_AT);
    let win_rel = if st.copy == 1 { HEADER_B } else { 0 };
    let w = match header_at(win_rel, scratch) {
        Some(h) => h,
        None => return false,
    };
    let h = Hdr { version: 2, ..w };
    store::encode_header(&h, scratch);
    wr_header(st.copy == 0, scratch)
}

// ---- commit -------------------------------------------------------------------------------------------------------------

/// The table in `buf` (`count` entries, padded to its sectors with zeros) goes to store sector `at`; read it back and check its
/// CRC; then header B and header A with `seq + 1`. Returns a status. `used` is the new end of the used sectors.
unsafe fn commit(buf: *mut u8, count: u32, at: u32, used_after_tables: u32, seq: u32) -> i32 {
    let ts = table_sectors(count);
    zero(buf.add(count as usize * ENTRY), ts as usize * 512 - count as usize * ENTRY);
    let crc = store::crc32_of(buf, ts as usize * 512);
    if ts != 0 {
        if !wr_data(at, ts, buf) {
            return E_WRITE;
        }
        // read the table back and check its CRC, through the scratch region
        let scratch = buf.add(SCRATCH_AT);
        let mut c = 0xffff_ffffu32;
        let mut done = 0;
        while done < ts {
            let n = if ts - done > 8 { 8 } else { ts - done };
            if block_read(FIRST + at + done, n * 512, scratch) != 0 {
                return E_READ;
            }
            c = store::crc32(c, scratch, n as usize * 512);
            done += n;
        }
        if !c != crc {
            return E_WRITE;
        }
    }
    let h = Hdr { version: 2, count, sectors: ts, used: used_after_tables, table_crc: crc, table_first: at, seq: seq + 1 };
    let hb = buf.add(SCRATCH_AT);
    store::encode_header(&h, hb);
    if !wr_header(true, hb) {
        return E_WRITE;
    }
    if !wr_header(false, hb) {
        return E_WRITE;
    }
    OK
}

// ---- commands -------------------------------------------------------------------------------------------------------------

unsafe fn cmd_info(seq: u32, buf: *mut u8) -> i32 {
    let st = state(buf);
    let free = match &st {
        Ok(s) => free_bytes(s),
        Err(_) => 0,
    };
    let rb = (addr_of_mut!(RBODY) as *mut u8).add(1);
    *rb = 0;
    *rb.add(1) = VERSION; // protocol 1
    *rb.add(2) = (RUNTIME_VERSION >> 8) as u8;
    *rb.add(3) = RUNTIME_VERSION as u8;
    wr32(rb.add(4), crate::device_name()); // the device's short name from its device file
    put32(1 + 8, free);
    put32(1 + 12, MAX_CHUNK);
    let flags = (boot::info().safe & 1)
        | if up().open { 2 } else { 0 }
        | if card::writable() { 0 } else { 4 }
        | if dlog::debug_on() { 8 } else { 0 }
        | if crate::crash::seen().found != 0 { 16 } else { 0 };
    *rb.add(16) = flags as u8;
    let (oldest, newest) = dlog::range();
    wr32(rb.add(17), oldest);
    wr32(rb.add(21), newest);
    // appended after the log range: the whole packed runtime version (bytes 2-3 keep its low 16 bits) and the capability bits
    wr32(rb.add(25), RUNTIME_VERSION);
    wr32(rb.add(29), CAP_LOG | CAP_PEEK | CAP_LOGCFG | CAP_SELECT | CAP_RESTART | CAP_MODULES | CAP_CAPACITY);
    reply(CMD_INFO, seq, OK, 33)
}

/// MODULES: the loaded modules and what they hold. Body: `u8 start` (a module record index). Reply: `u32 arena_free, arena_largest, dsp_free,
/// dsp_largest`, `u8 total` (loaded modules), `u8 n` (records in this reply), then per record `u8 index`, `name[32]`, `u32 version`, 8 contract bytes
/// (api, machine, fx, service as major, minor), `u32 cf_base, cf_size, dsp_off, dsp_len, rows` (voice rows as a mask), `u8 n_types` and that many machine
/// type bytes (at most 8: the machine rows of the module, then its voice rows' types), `u32 faults` (the guard's count for those types), `u8 flags` (bit 0:
/// the guard switched one of them off). A reply holds the records from `start` on that fit; the host asks again from the last index + 1 until it has `total`.
unsafe fn cmd_modules(seq: u32, body: *const u8, blen: usize) -> i32 {
    if blen < 1 {
        return reply(CMD_MODULES, seq, E_MSG, 0);
    }
    let start = *body as usize;
    let s = crate::st();
    let rb = (addr_of_mut!(RBODY) as *mut u8).add(1);
    let (af, al) = crate::arena::stats();
    let (df, dl) = crate::dsp::area_stats();
    wr32(rb, af);
    wr32(rb.add(4), al);
    wr32(rb.add(8), df);
    wr32(rb.add(12), dl);
    let mut total = 0u8;
    let mut i = 0;
    while i < crate::MAX_MODULES {
        if s.modules[i].used != 0 {
            total += 1;
        }
        i += 1;
    }
    *rb.add(16) = total;
    let mut at = 18usize;
    let mut n = 0u8;
    let d = crate::dsp::ds();
    i = start;
    while i < crate::MAX_MODULES {
        let m = &s.modules[i];
        if m.used == 0 {
            i += 1;
            continue;
        }
        // the machine types: rows of the registry, then voice rows (pinned numbers of a DSP-only module)
        let mut types = [0u8; 8];
        let mut nt = crate::registry::types_of(i as u8 + 1, &mut types);
        let mut r = 0;
        while r < crate::dsp::MAX_ROWS {
            if d.row_owner[r] == i as u8 + 1 && d.row_type[r] < 256 && nt < 8 {
                let t = d.row_type[r] as u8;
                let mut dup = false;
                let mut j = 0;
                while j < nt {
                    dup |= types[j] == t;
                    j += 1;
                }
                if !dup {
                    types[nt] = t;
                    nt += 1;
                }
            }
            r += 1;
        }
        if at + 71 + nt > REPLY_BODY_MAX - 1 {
            break;
        }
        *rb.add(at) = i as u8;
        copy(rb.add(at + 1), m.name.as_ptr(), 32);
        wr32(rb.add(at + 33), m.version);
        copy(rb.add(at + 37), (*addr_of!(crate::DKRT_CONTRACTS))[i].as_ptr(), 8);
        wr32(rb.add(at + 45), m.base);
        wr32(rb.add(at + 49), m.size);
        wr32(rb.add(at + 53), d.mod_off[i]);
        wr32(rb.add(at + 57), d.mod_len[i]);
        wr32(rb.add(at + 61), d.mod_rows[i]);
        *rb.add(at + 65) = nt as u8;
        let mut faults = 0u32;
        let mut off = false;
        let mut j = 0;
        while j < nt {
            *rb.add(at + 66 + j) = types[j];
            faults = faults.wrapping_add(dlog::guard_faults(types[j] as u32));
            off |= dlog::guard_disabled(types[j] as u32);
            j += 1;
        }
        wr32(rb.add(at + 66 + nt), faults);
        *rb.add(at + 70 + nt) = off as u8;
        at += 71 + nt;
        n += 1;
        i += 1;
    }
    *rb.add(17) = n;
    reply(CMD_MODULES, seq, OK, at)
}

/// CAPACITY: per resource of the runtime, its limit, what is used and the largest free piece. Body: none. Reply: `u8 version` (1), `u8 n`, then
/// `n` entries of `u8 kind` (`CAPK_*`, sdk/sdk.toml `[capacity.kinds]`), `u32 limit`, `u32 used`, `u32 largest` (0 where a largest piece has
/// no meaning). A host skips a kind it does not know; new kinds are appended.
unsafe fn cmd_capacity(seq: u32) -> i32 {
    let s = crate::st();
    let (af, al) = crate::arena::stats();
    let (df, dl) = crate::dsp::area_stats();
    let area = crate::dsp::area_bytes();
    let arena = s.arena_hi - s.arena_lo;
    let mut mods = 0;
    let mut i = 0;
    while i < crate::MAX_MODULES {
        if s.modules[i].used != 0 {
            mods += 1;
        }
        i += 1;
    }
    let (vrows, vused) = crate::dsp::rows_counts();
    let [mach, param, page] = crate::registry::counts();
    let stage = crate::STAGE_BYTES as u32;
    let entries: [(u8, u32, u32, u32); 11] = [
        (CAPK_MODULES, crate::MAX_MODULES as u32, mods, 0),
        (CAPK_SELECTION, dkrt_sdk::SEL_MAX as u32, boot::selected(), 0),
        (CAPK_ARENA, arena, arena - af, al),
        (CAPK_STAGE, stage, 0, stage),
        (CAPK_DSP_AREA, area, area - df, dl),
        (CAPK_MACHINE_ROWS, mach.0, mach.1, 0),
        (CAPK_VOICE_ROWS, vrows, vused, 0),
        (CAPK_PARAM_ROWS, param.0, param.1, 0),
        (CAPK_PAGE_ROWS, page.0, page.1, 0),
        (CAPK_FX_SLOTS, dkrt_sdk::device::LIMIT_FX_SLOTS, 0, 0),
        (CAPK_MAP_ENTRIES, dkrt_sdk::sel::MAP_N as u32, crate::assign::map_used(), 0),
    ];
    let rb = (addr_of_mut!(RBODY) as *mut u8).add(1);
    *rb = 1;
    *rb.add(1) = entries.len() as u8;
    let mut at = 2usize;
    let mut k = 0;
    while k < entries.len() {
        let (kind, limit, used, largest) = entries[k];
        *rb.add(at) = kind;
        wr32(rb.add(at + 1), limit);
        wr32(rb.add(at + 5), used);
        wr32(rb.add(at + 9), largest);
        at += 13;
        k += 1;
    }
    reply(CMD_CAPACITY, seq, OK, at)
}

unsafe fn cmd_list(seq: u32, buf: *mut u8, body: *const u8, blen: usize) -> i32 {
    if blen < 4 {
        return reply(CMD_LIST, seq, E_MSG, 0);
    }
    let first = rd32(body);
    let st = match state(buf) {
        Ok(s) => s,
        Err(e) => return reply(CMD_LIST, seq, e, 0),
    };
    let rb = (addr_of_mut!(RBODY) as *mut u8).add(1);
    wr32(rb, st.count);
    let mut at = 5;
    let mut n = 0u32;
    let mut i = first;
    while i < st.count {
        let e = buf.add(i as usize * ENTRY);
        let mut pl = 0;
        while pl < 88 && *e.add(pl) != 0 {
            pl += 1;
        }
        let need = pl + 1 + 4 + 32;
        if at + need > REPLY_BODY_MAX - 1 {
            break;
        }
        copy(rb.add(at), e, pl);
        *rb.add(at + pl) = 0;
        copy(rb.add(at + pl + 1), e.add(92), 4);
        copy(rb.add(at + pl + 5), e.add(96), 32);
        at += need;
        n += 1;
        i += 1;
    }
    *rb.add(4) = n as u8;
    reply(CMD_LIST, seq, OK, at)
}

unsafe fn cmd_begin(seq: u32, buf: *mut u8, body: *const u8, blen: usize) -> i32 {
    up().open = false;
    if !card::writable() {
        return reply(CMD_BEGIN, seq, E_INUSE, 0);
    }
    let pl = match path_ok(body, blen) {
        Some(n) => n,
        None => return reply(CMD_BEGIN, seq, if blen < 1 + 4 + 32 { E_MSG } else { E_PATH }, 0),
    };
    if blen != pl + 1 + 4 + 32 {
        return reply(CMD_BEGIN, seq, E_MSG, 0);
    }
    let size = rd32(body.add(pl + 1));
    let st = match state(buf) {
        Ok(s) => s,
        Err(e) => return reply(CMD_BEGIN, seq, e, 0),
    };
    if !sync_headers(buf, &st) {
        return reply(CMD_BEGIN, seq, E_WRITE, 0);
    }
    let need = sectors_of(size) as u64 + table_sectors(st.count + 1) as u64;
    if st.count >= TABLE_MAX || st.used as u64 + need > LIMIT as u64 {
        put32(1, free_bytes(&st));
        return reply(CMD_BEGIN, seq, E_FULL, 4);
    }
    let u = up();
    zero(u.path.as_mut_ptr(), 88);
    copy(u.path.as_mut_ptr(), body, pl);
    u.size = size;
    copy(u.sha.as_mut_ptr(), body.add(pl + 5), 32);
    u.next = 0;
    u.have_last = false;
    u.last_off = 0;
    u.last_len = 0;
    u.start = st.used;
    u.open = true;
    reply(CMD_BEGIN, seq, OK, 0)
}

unsafe fn cmd_data(seq: u32, buf: *mut u8, blen: usize) -> i32 {
    let u = up();
    if !u.open {
        return reply(CMD_DATA, seq, E_STATE, 0);
    }
    if blen < 5 {
        return reply(CMD_DATA, seq, E_MSG, 0);
    }
    let body = buf.add(BODY_AT);
    let off = rd32(body);
    let len = (blen - 4) as u32;
    if len > MAX_CHUNK {
        return reply(CMD_DATA, seq, E_LENGTH, 0);
    }
    if u.have_last && off == u.last_off && len == u.last_len {
        return reply(CMD_DATA, seq, OK, 0); // a retry of the previous chunk
    }
    if off != u.next {
        return reply(CMD_DATA, seq, E_OFFSET, 0);
    }
    let end = off as u64 + len as u64;
    if end > u.size as u64 || (len & 511 != 0 && end != u.size as u64) {
        return reply(CMD_DATA, seq, E_LENGTH, 0);
    }
    let data = buf.add(BODY_AT + 4);
    let sectors = sectors_of(len);
    zero(data.add(len as usize), (sectors * 512 - len) as usize);
    if !wr_data(u.start + off / 512, sectors, data) {
        return reply(CMD_DATA, seq, E_WRITE, 0);
    }
    u.have_last = true;
    u.last_off = off;
    u.last_len = len;
    u.next = end as u32;
    reply(CMD_DATA, seq, OK, 0)
}

unsafe fn cmd_end(seq: u32, buf: *mut u8) -> i32 {
    let u = up();
    if !u.open {
        return reply(CMD_END, seq, E_STATE, 0);
    }
    // END closes the upload whatever the result
    u.open = false;
    if u.next != u.size {
        return reply(CMD_END, seq, E_LENGTH, 0);
    }
    // read the staged file back from the card and check its sha256
    let mut sha = Sha::new();
    let mut left = u.size;
    let mut sector = u.start;
    while left > 0 {
        let n = if left > MAX_CHUNK { MAX_CHUNK } else { left };
        let secs = sectors_of(n);
        if block_read(FIRST + sector, secs * 512, buf.add(16)) != 0 {
            return reply(CMD_END, seq, E_READ, 0);
        }
        sha.update(buf.add(16), n);
        left -= n;
        sector += secs;
    }
    let mut got = [0u8; 32];
    sha.finish(got.as_mut_ptr());
    let mut i = 0;
    while i < 32 {
        if got[i] != u.sha[i] {
            return reply(CMD_END, seq, E_HASH, 0);
        }
        i += 1;
    }
    // the new table: the current one with the entry replaced or inserted, sorted by path
    let st = match state(buf) {
        Ok(s) => s,
        Err(e) => return reply(CMD_END, seq, e, 0),
    };
    if st.used != u.start || st.count >= TABLE_MAX {
        return reply(CMD_END, seq, E_STATE, 0);
    }
    let mut idx = 0u32;
    let mut found = false;
    while idx < st.count {
        let c = cmp_path(u.path.as_ptr(), buf.add(idx as usize * ENTRY));
        if c == 0 {
            found = true;
            break;
        }
        if c < 0 {
            break;
        }
        idx += 1;
    }
    let mut count = st.count;
    let e = buf.add(idx as usize * ENTRY);
    if !found {
        mv(e.add(ENTRY), e, (st.count - idx) as usize * ENTRY);
        count += 1;
    }
    zero(e, ENTRY);
    copy(e, u.path.as_ptr(), 88);
    wr32(e.add(88), u.start);
    wr32(e.add(92), u.size);
    copy(e.add(96), u.sha.as_ptr(), 32);
    let at = u.start + sectors_of(u.size);
    let used = at + table_sectors(count);
    if used > LIMIT {
        return reply(CMD_END, seq, E_FULL, 0);
    }
    let r = commit(buf, count, at, used, st.seq);
    reply(CMD_END, seq, r, 0)
}

unsafe fn cmd_delete(seq: u32, buf: *mut u8, body: *const u8, blen: usize) -> i32 {
    up().open = false;
    if !card::writable() {
        return reply(CMD_DELETE, seq, E_INUSE, 0);
    }
    let pl = match path_ok(body, blen) {
        Some(n) if n + 1 == blen => n,
        _ => return reply(CMD_DELETE, seq, if blen < 2 { E_MSG } else { E_PATH }, 0),
    };
    let _ = pl;
    let st = match state(buf) {
        Ok(s) => s,
        Err(e) => return reply(CMD_DELETE, seq, e, 0),
    };
    let mut idx = 0u32;
    while idx < st.count {
        if cmp_path(body, buf.add(idx as usize * ENTRY)) == 0 {
            break;
        }
        idx += 1;
    }
    if idx == st.count {
        return reply(CMD_DELETE, seq, E_NOFILE, 0);
    }
    let e = buf.add(idx as usize * ENTRY);
    mv(e, e.add(ENTRY), (st.count - idx - 1) as usize * ENTRY);
    let count = st.count - 1;
    let at = st.used;
    let used = at + table_sectors(count);
    if used > LIMIT {
        return reply(CMD_DELETE, seq, E_FULL, 0);
    }
    let r = commit(buf, count, at, used, st.seq);
    reply(CMD_DELETE, seq, r, 0)
}

unsafe fn cmd_clear(seq: u32, buf: *mut u8, body: *const u8, blen: usize) -> i32 {
    up().open = false;
    if !card::writable() {
        return reply(CMD_CLEAR, seq, E_INUSE, 0);
    }
    if blen != 4 || rd32(body) != CLEAR_MAGIC {
        return reply(CMD_CLEAR, seq, E_MSG, 0);
    }
    // CLEAR works on a damaged store too: the new sequence number is above every copy that is readable
    let seq_now = match state(buf) {
        Ok(s) => s.seq,
        Err(_) => highest_seq(buf),
    };
    let r = commit(buf, 0, 1, 1, seq_now);
    reply(CMD_CLEAR, seq, r, 0)
}

/// The higher `seq` of the two header copies that parse (0 when none).
unsafe fn highest_seq(buf: *mut u8) -> u32 {
    let mut best = 0;
    let mut k = 0;
    while k < 2 {
        let rel = if k == 0 { 0 } else { HEADER_B };
        if block_read(FIRST + rel, 512, buf) == 0 {
            if let Some(h) = store::parse_header(buf) {
                if h.seq > best {
                    best = h.seq;
                }
            }
        }
        k += 1;
    }
    best
}

extern "C" {
    /// Per-device data from the device file (`[digikit] peek`), defined by `src/cf/<dev>/device.s`: the number of ranges and `lo, hi` pairs.
    static dkrt_peek_n: u32;
    static dkrt_peek_ranges: [u32; 8];
}

/// Events in one LOG reply: the reply body holds a status byte, `oldest`, `newest`, a count and 32 bytes each, in at most `REPLY_BODY_MAX`.
const LOG_PER_REPLY: u32 = ((REPLY_BODY_MAX - 1 - 9) / 32) as u32;

/// LOG: body `u32 from_seq`. Reply: `u32 oldest, u32 newest, u8 n`, then `n` events of 32 bytes (`seq, time, kind u16, flags u16, a0..a4`, all
/// big endian) from `from_seq` (or from the oldest kept, when `from_seq` is older), as many as fit one reply. Nothing is sent unless a host asks.
unsafe fn cmd_log(seq: u32, body: *const u8, blen: usize) -> i32 {
    if blen < 4 {
        return reply(CMD_LOG, seq, E_MSG, 0);
    }
    let from = rd32(body);
    let (oldest, newest) = dlog::range();
    let rb = (addr_of_mut!(RBODY) as *mut u8).add(1);
    wr32(rb, oldest);
    wr32(rb.add(4), newest);
    let mut n = 0u32;
    if newest != 0 {
        let mut s = if from < oldest { oldest } else { from };
        while s <= newest && n < LOG_PER_REPLY {
            if dlog::copy_event(s, rb.add(9 + 32 * n as usize)) {
                n += 1;
            }
            s += 1;
        }
    }
    *rb.add(8) = n as u8;
    reply(CMD_LOG, seq, OK, 9 + 32 * n as usize)
}

/// Is `[addr, addr + len)` inside one of the ranges of the device file?
unsafe fn peek_allowed(addr: u32, len: u32) -> bool {
    let n = core::ptr::read_volatile(addr_of!(dkrt_peek_n)) as usize;
    let t = addr_of!(dkrt_peek_ranges) as *const u32;
    let end = match addr.checked_add(len) {
        Some(e) => e,
        None => return false,
    };
    let mut i = 0;
    while i < n && i < 4 {
        let lo = core::ptr::read_volatile(t.add(2 * i));
        let hi = core::ptr::read_volatile(t.add(2 * i + 1));
        if addr >= lo && end <= hi {
            return true;
        }
        i += 1;
    }
    false
}

/// PEEK: body `u32 addr, u16 len` (1 to 512). Reply: `u32 addr, u16 len`, then the bytes. Reads RAM only, inside the ranges the device file lists
/// (`[digikit] peek`): no peripheral registers.
unsafe fn cmd_peek(seq: u32, body: *const u8, blen: usize) -> i32 {
    if blen != 6 {
        return reply(CMD_PEEK, seq, E_MSG, 0);
    }
    let addr = rd32(body);
    let len = ((*body.add(4) as u32) << 8) | *body.add(5) as u32;
    if len == 0 || len > 512 || !peek_allowed(addr, len) {
        return reply(CMD_PEEK, seq, E_PEEK, 0);
    }
    let rb = (addr_of_mut!(RBODY) as *mut u8).add(1);
    wr32(rb, addr);
    *rb.add(4) = (len >> 8) as u8;
    *rb.add(5) = len as u8;
    let mut i = 0;
    while i < len as usize {
        *rb.add(6 + i) = core::ptr::read_volatile((addr as usize + i) as *const u8);
        i += 1;
    }
    reply(CMD_PEEK, seq, OK, 6 + len as usize)
}

/// LOGCFG: body `u8 op` (0 get, 1 set); a set adds `u8 default` and 8 bytes, the level of module record 0 to 7 (0xff: use the default). Levels are 0 to 4.
/// Reply (both ops): `u8 default`, 8 module levels, `u8 debug_on`.
unsafe fn cmd_logcfg(seq: u32, body: *const u8, blen: usize) -> i32 {
    if blen < 1 {
        return reply(CMD_LOGCFG,seq, E_MSG, 0);
    }
    let op = *body;
    if op == 1 {
        if blen != 10 {
            return reply(CMD_LOGCFG,seq, E_MSG, 0);
        }
        let mut i = 0;
        while i < 9 {
            let v = *body.add(1 + i);
            if i == 0 {
                if v > 4 {
                    return reply(CMD_LOGCFG,seq, E_PROTO, 0);
                }
            } else if v > 4 && v != 0xff {
                return reply(CMD_LOGCFG,seq, E_PROTO, 0);
            }
            i += 1;
        }
        dlog::set_level_default(*body.add(1));
        i = 0;
        while i < 8 {
            dlog::set_level_module(i, *body.add(2 + i));
            i += 1;
        }
    } else if op != 0 {
        return reply(CMD_LOGCFG,seq, E_PROTO, 0);
    }
    let rb = (addr_of_mut!(RBODY) as *mut u8).add(1);
    *rb = dlog::level_default();
    let mut i = 0;
    while i < 8 {
        *rb.add(1 + i) = dlog::level_module(i);
        i += 1;
    }
    *rb.add(9) = if dlog::debug_on() { 1 } else { 0 };
    reply(CMD_LOGCFG,seq, OK, 10)
}

/// SELECT (runtime 4): save the selection record, the same `boot::selection_set` the menu's APPLY & RESTART calls (so the card range check and
/// the "blank or ours" rule apply). Body: `u8 n`, then `n` store paths, each NUL terminated. Status: 0, `E_MSG` (malformed), `E_INUSE` (card
/// not writable), or the selection code (`E_SEL_COUNT`, `E_SEL_PATH`, `E_SEL_WRITE`, `E_CARD_RO`). Takes effect at the next boot.
unsafe fn cmd_select(seq: u32, body: *const u8, blen: usize) -> i32 {
    if blen < 1 {
        return reply(CMD_SELECT, seq, E_MSG, 0);
    }
    if !card::writable() {
        return reply(CMD_SELECT, seq, E_INUSE, 0);
    }
    let n = *body as usize;
    if n > dkrt_sdk::SEL_MAX {
        return reply(CMD_SELECT, seq, dkrt_sdk::E_SEL_COUNT, 0);
    }
    // static, not on the MIDI task's stack (the record is about 450 bytes)
    let sel = &mut *addr_of_mut!(SELBUF);
    *sel = core::mem::zeroed();
    let mut at = 1usize;
    let mut k = 0usize;
    while k < n {
        let mut len = 0usize;
        while at + len < blen && *body.add(at + len) != 0 {
            len += 1;
        }
        if at + len >= blen {
            return reply(CMD_SELECT, seq, E_MSG, 0); // no NUL
        }
        if len >= dkrt_sdk::SEL_PATH {
            return reply(CMD_SELECT, seq, dkrt_sdk::E_SEL_PATH, 0);
        }
        let mut i = 0;
        while i < len {
            sel.paths[k][i] = *body.add(at + i);
            i += 1;
        }
        at += len + 1;
        k += 1;
    }
    if at != blen {
        return reply(CMD_SELECT, seq, E_MSG, 0);
    }
    sel.count = n as u32;
    let r = boot::selection_set(sel);
    reply(CMD_SELECT, seq, r, 0)
}

static mut SELBUF: dkrt_sdk::DkrtSelection = dkrt_sdk::DkrtSelection { count: 0, flags: 0, paths: [[0; dkrt_sdk::SEL_PATH]; dkrt_sdk::SEL_MAX] };

/// Ticks to wait for the reply to leave the TX ring, at most, before the restart goes anyway.
const RESTART_TICKS_MAX: u32 = 4000;
/// 0: no restart pending; else 1 + the UI passes waited so far.
static mut RESTART_WAIT: u32 = 0;

/// RESTART (runtime 4): the reply goes out first; the soft reset (`dkrt_restart`, the one APPLY & RESTART calls) runs from the UI task's next
/// passes (`restart_poll`), once the TX ring has drained. Status: 0, or `E_STATE` while an upload is open. No body.
unsafe fn cmd_restart(seq: u32) -> i32 {
    if up().open {
        return reply(CMD_RESTART, seq, E_STATE, 0);
    }
    RESTART_WAIT = 1;
    reply(CMD_RESTART, seq, OK, 0)
}

/// Called once per UI pass (`dkrt_tick`). Runs a pending RESTART after its reply has been sent. `dkrt_restart` returns only when it refuses
/// (a card transfer in flight): the request is then dropped and logged as an error; the host sees the runtime still running and asks again.
pub unsafe fn restart_poll() {
    if RESTART_WAIT == 0 {
        return;
    }
    RESTART_WAIT += 1;
    let drained = matches!(tx_in_flight(), Some(0));
    if (drained && RESTART_WAIT > 3) || RESTART_WAIT > RESTART_TICKS_MAX {
        RESTART_WAIT = 0;
        let r = crate::dkrt_restart();
        dlog::ev(logk::UPLOAD, logk::F_ERROR, [CMD_RESTART as u32, r as u32, 0, 0, 0]);
    }
}

unsafe fn buffer() -> *mut u8 {
    addr_of_mut!(DKRT_UPLOAD_BUF) as *mut u8
}

/// The hook: `msg` is one gathered SysEx message, F0 to F7 inclusive. Returns 1 when it was ours (the router must not see it).
#[no_mangle]
pub unsafe extern "C" fn dkrt_midi_rx(msg: *const u8, len: u32, _source: u32) -> i32 {
    if len < 5 || *msg != 0xf0 || *msg.add(1) != 0x7d || *msg.add(2) != 0x64 || *msg.add(3) != 0x6b {
        return 0;
    }
    crate::init_once();
    if len < 8 || *msg.add(len as usize - 1) != 0xf7 {
        return reply(0, 0, E_MSG, 0);
    }
    let ver = *msg.add(4);
    let cmd = *msg.add(5);
    let seq = *msg.add(6) as u32;
    if ver != VERSION {
        return reply(cmd, seq, E_PROTO, 0);
    }
    let wire = len - 8;
    if wire > REQ_WIRE_MAX || unpacked_len(wire as usize) > BODY_MAX {
        return reply(cmd, seq, E_MSG, 0);
    }
    let buf = buffer();
    let blen = unpack(msg.add(7), wire as usize, buf.add(BODY_AT));
    // Every command but DATA and SELECT has a small body. Copy it out: the commands read the table into the front of the buffer.
    // (SELECT reads its paths in place: up to 8 paths of 56 bytes, and it never touches the table.)
    let mut req = [0u8; 160];
    if cmd != CMD_DATA && cmd != CMD_SELECT {
        if blen > req.len() {
            return reply(cmd, seq, E_MSG, 0);
        }
        copy(req.as_mut_ptr(), buf.add(BODY_AT), blen);
    }
    let body = if cmd == CMD_SELECT { buf.add(BODY_AT) as *const u8 } else { req.as_ptr() };
    // an if chain, not a `match` (a jump table is not ColdFire code)
    if cmd == CMD_INFO {
        cmd_info(seq, buf)
    } else if cmd == CMD_LIST {
        cmd_list(seq, buf, body, blen)
    } else if cmd == CMD_MODULES {
        cmd_modules(seq, body, blen)
    } else if cmd == CMD_CAPACITY {
        cmd_capacity(seq)
    } else if cmd == CMD_BEGIN {
        cmd_begin(seq, buf, body, blen)
    } else if cmd == CMD_DATA {
        cmd_data(seq, buf, blen)
    } else if cmd == CMD_END {
        cmd_end(seq, buf)
    } else if cmd == CMD_DELETE {
        cmd_delete(seq, buf, body, blen)
    } else if cmd == CMD_CLEAR {
        cmd_clear(seq, buf, body, blen)
    } else if cmd == CMD_ABORT {
        up().open = false;
        reply(CMD_ABORT, seq, OK, 0)
    } else if cmd == CMD_LOG {
        cmd_log(seq, body, blen)
    } else if cmd == CMD_PEEK {
        cmd_peek(seq, body, blen)
    } else if cmd == CMD_LOGCFG {
        cmd_logcfg(seq, body, blen)
    } else if cmd == CMD_SELECT {
        cmd_select(seq, body, blen)
    } else if cmd == CMD_RESTART {
        cmd_restart(seq)
    } else {
        reply(cmd, seq, E_PROTO, 0)
    }
}

#[allow(dead_code)]
fn _keep(_: *const u8) {
    let _ = addr_of!(UP);
}
