//! test/rust-demo: Rust on the ColdFire. Placed in cf.expansion; called in place by the test harness.
//!
//! Built with rustc_codegen_gcc for -mcpu=54418 (`patchkit build`, finding 26). Every exported function
//! takes and returns integers or pointers: no struct results on the C boundary (finding 23, S1b).
#![no_std]

use core::panic::PanicInfo;

#[panic_handler]
fn panic(_: &PanicInfo) -> ! {
    loop {}
}

const MAGIC: u32 = 0x5255_5354; // "RUST"

/// Read-only table (rodata).
#[no_mangle]
pub static PK_RS_TABLE: [u32; 8] = [3, 1, 4, 1, 5, 9, 2, 6];

/// Zero after the loader's bss clear. Word 0 is `MAGIC` after the first tick, word 1 the tick count,
/// word 2 the checksum of the last `pk_rs_selftest`.
#[no_mangle]
pub static mut PK_RS_STATE: [u32; 4] = [0; 4];

/// Count a call: stores the magic and the new count, returns the count.
#[no_mangle]
pub extern "C" fn pk_rs_tick() -> u32 {
    unsafe {
        let s = &raw mut PK_RS_STATE;
        let n = (*s)[1].wrapping_add(1);
        (*s)[0] = MAGIC;
        (*s)[1] = n;
        n
    }
}

/// 64-bit FNV-1a over `n` bytes, folded to 32 bits. The u64 multiply goes to the multiply helper.
#[no_mangle]
pub unsafe extern "C" fn pk_rs_hash(p: *const u8, n: u32) -> u32 {
    let bytes = core::slice::from_raw_parts(p, n as usize);
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for &b in bytes {
        h = (h ^ b as u64).wrapping_mul(0x0000_0100_0000_01b3);
    }
    (h >> 32) as u32 ^ h as u32
}

fn op_inc(x: u32) -> u32 {
    x.wrapping_add(1)
}
fn op_rot(x: u32) -> u32 {
    x.rotate_left(7)
}
fn op_neg(x: u32) -> u32 {
    x.wrapping_neg()
}

static OPS: [fn(u32) -> u32; 3] = [op_inc, op_rot, op_neg];

/// A match plus a call through a function pointer from a table.
#[no_mangle]
pub extern "C" fn pk_rs_apply(op: u32, x: u32) -> u32 {
    match op {
        0..=2 => OPS[op as usize](x),
        3 => x / 10,
        4 => x % 7,
        5 => ((x as u64 * x as u64) >> 16) as u32,
        6 => ((x as u64).wrapping_mul(1_000_003) / 7919) as u32, // 64-bit divide helper
        _ => x,
    }
}

/// Sum of the table through the iterator adaptors, the hash of its bytes, one op of each kind; stores the result
/// in `PK_RS_STATE[2]` and returns it.
#[no_mangle]
pub unsafe extern "C" fn pk_rs_selftest() -> u32 {
    let sum: u32 = PK_RS_TABLE.iter().copied().sum();
    let h = pk_rs_hash(PK_RS_TABLE.as_ptr() as *const u8, 32);
    let mut acc = h ^ sum;
    let mut op = 0;
    while op < 7 {
        acc = pk_rs_apply(op, acc);
        op += 1;
    }
    (*(&raw mut PK_RS_STATE))[2] = acc;
    acc
}
