//! Module upload over USB SysEx (docs/design/digikit-upload.md): the device side.
//!
//! `dkrt_midi_rx` is called from the firmware's MIDI task (hook `dkrt.hook.midi_rx`, `src/cf/tramp.s`) with every SysEx message that
//! came in on USB cable 0, after the firmware gathered it and before its router. A message that starts `F0 7D 64 6B` is ours: it is
//! handled here (one request, one reply, in order) and the router never sees it; any other message returns 0 and takes the stock route.
//!
//! Store writes go through the firmware's block write, only to store sectors: staged data and tables in `[1, LIMIT)`, the two header
//! copies at sector 0 and `HEADER_B`. Never the selection record (the two sectors after `HEADER_B`). Rules: no `match` on integers (a
//! dense one becomes a jump table, which is not ColdFire code), no `core::fmt`, no slice indexing (a bounds panic hangs the task).

use crate::sha256::Sha;
use crate::{block_read, block_write, boot, midi_send, rd32, wr32};
use core::ptr::{addr_of, addr_of_mut};
use dkrt_sdk::store::{self, Hdr, ENTRY, FIRST, HEADER_B, LIMIT};
use dkrt_sdk::{E_NOT_FOUND, OK, RUNTIME_VERSION};

pub const E_MSG: i32 = -50;
pub const E_PATH: i32 = -51;
pub const E_FULL: i32 = -52;
pub const E_STATE: i32 = -53;
pub const E_OFFSET: i32 = -54;
pub const E_LENGTH: i32 = -55;
pub const E_HASH: i32 = -56;
pub const E_WRITE: i32 = -57;
pub const E_READ: i32 = -58;
pub const E_NOFILE: i32 = -59;
pub const E_PROTO: i32 = -60;
pub const E_NOSTORE: i32 = -61;

const CMD_INFO: u32 = 0x01;
const CMD_LIST: u32 = 0x02;
const CMD_BEGIN: u32 = 0x10;
const CMD_DATA: u32 = 0x11;
const CMD_END: u32 = 0x12;
const CMD_DELETE: u32 = 0x13;
const CMD_CLEAR: u32 = 0x14;
const CMD_ABORT: u32 = 0x15;

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

/// Send the reply for `cmd`/`seq`: status, then the `extra` bytes already in `RBODY[1..1+extra]`.
unsafe fn reply(cmd: u32, seq: u32, status: i32, extra: usize) -> i32 {
    let rb = addr_of_mut!(RBODY) as *mut u8;
    *rb = status as u8;
    let w = addr_of_mut!(RWIRE) as *mut u8;
    *w = 0xf0;
    *w.add(1) = 0x7d;
    *w.add(2) = 0x64;
    *w.add(3) = 0x6b;
    *w.add(4) = 1;
    *w.add(5) = ((cmd | 0x40) & 0x7f) as u8;
    *w.add(6) = (seq & 0x7f) as u8;
    let n = pack(rb, 1 + extra, w.add(7));
    *w.add(7 + n) = 0xf7;
    midi_send((8 + n) as u32, w, 2);
    DKRT_UPLOAD_COUNT = DKRT_UPLOAD_COUNT.wrapping_add(1);
    DKRT_UPLOAD_LAST = status;
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
}

/// Read the current store; its table is then at `buf`. A card with nothing in the range is an empty store (`used` 1, `seq` 0).
/// `Err(E_NOSTORE)`: a header is there but no copy is usable (or the table is more than 256 entries).
unsafe fn state(buf: *mut u8) -> Result<St, i32> {
    match store::open(rd, buf, MAX_CHUNK as usize) {
        Ok(t) => {
            if t.count > TABLE_MAX {
                return Err(E_NOSTORE);
            }
            Ok(St { used: t.used, seq: t.seq, count: t.count, sectors: t.sectors })
        }
        Err(e) => {
            if e == E_NOT_FOUND {
                Ok(St { used: 1, seq: 0, count: 0, sectors: 0 })
            } else {
                Err(E_NOSTORE)
            }
        }
    }
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
    block_write(sector, bytes, buf) == 0
}

/// Write a header sector to copy A (`b` false) or B.
unsafe fn wr_header(b: bool, buf: *const u8) -> bool {
    let rel = if b { HEADER_B } else { 0 };
    card_write(FIRST + rel, 512, buf)
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
    *rb.add(1) = 1; // protocol 1
    *rb.add(2) = (RUNTIME_VERSION >> 8) as u8;
    *rb.add(3) = RUNTIME_VERSION as u8;
    let id: &[u8; 4] = if crate::device() == dkrt_sdk::DEVICE_DT2 { b"dt2\0" } else { b"dn2\0" };
    copy(rb.add(4), id.as_ptr(), 4);
    put32(1 + 8, free);
    put32(1 + 12, MAX_CHUNK);
    let flags = (boot::info().safe & 1) | if up().open { 2 } else { 0 };
    *rb.add(16) = flags as u8;
    reply(CMD_INFO, seq, OK, 17)
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
    let ver = *msg.add(4) as u32;
    let cmd = *msg.add(5) as u32;
    let seq = *msg.add(6) as u32;
    if ver != 1 {
        return reply(cmd, seq, E_PROTO, 0);
    }
    let wire = len - 8;
    if wire > REQ_WIRE_MAX || unpacked_len(wire as usize) > BODY_MAX {
        return reply(cmd, seq, E_MSG, 0);
    }
    let buf = buffer();
    let blen = unpack(msg.add(7), wire as usize, buf.add(BODY_AT));
    // Every command but DATA has a small body. Copy it out: the commands read the table into the front of the buffer.
    let mut req = [0u8; 160];
    if cmd != CMD_DATA {
        if blen > req.len() {
            return reply(cmd, seq, E_MSG, 0);
        }
        copy(req.as_mut_ptr(), buf.add(BODY_AT), blen);
    }
    let body = req.as_ptr();
    // an if chain, not a `match` (a jump table is not ColdFire code)
    if cmd == CMD_INFO {
        cmd_info(seq, buf)
    } else if cmd == CMD_LIST {
        cmd_list(seq, buf, body, blen)
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
    } else {
        reply(cmd, seq, E_PROTO, 0)
    }
}

#[allow(dead_code)]
fn _keep(_: *const u8) {
    let _ = addr_of!(UP);
}
