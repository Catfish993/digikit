//! DN2 0xb82342 (DT2 1.16 0x1cdbb2): a compiled float routine with a prologue
//! that spills 12 registers, a prelude of 52 instructions, a DO loop of 17
//! instructions (3 multiplies and 3 adds per trip pair) run N-2 times, an
//! epilogue and a `JUMP (M14, I12) (DB)` return with RFRAME in its second
//! delay slot. N is the stack argument at I6+4*M6. Instruction counts:
//! N=1: 76, N=2: 97, N>=3: 98 + 17 (N-2).
//!
//! Arguments: R4 the record (also I4), R8 the input stream (I5), R12 the
//! output stream (I3). Everything is a byte address, word-scaled offsets.
//! The routine reproduces the registers (scratch ones too), ASTATX, the loop
//! bookkeeping and the PC; a float operand or result outside the finite
//! normal/zero domain declines.

#![allow(unused_assignments)]

use super::*;

const SHA: &str = "08c13d8a0a0af3f196fcfd484b6f767f495f9b1a9d1632cc9de70d0eec71695c";
/// Stores: 17 + (N) + (N - 2) + 6 per call, at most 2 * MAXN + 25 < MAX_STORES.
const MAXN: u32 = 100;

pub fn table() -> Vec<HleFn> {
    vec![HleFn {
        name: "dn2_b82342",
        entry: 0xb8_2342,
        words: 254,
        sha: SHA,
        run,
    }]
}

/// A float bit pattern outside the finite normal / zero domain.
#[inline(always)]
fn exc(b: u32) -> bool {
    let e = (b >> 23) & 0xff;
    (e == 0xff) | ((e == 0) & (b & 0x7f_ffff != 0))
}

#[inline(always)]
fn dec(b: u32) -> Option<f32> {
    let e = (b >> 23) & 0xff;
    if e == 0xff || (e == 0 && b & 0x7f_ffff != 0) {
        return None;
    }
    Some(f32::from_bits(b))
}

#[inline(always)]
fn fin(r: f32) -> Option<u32> {
    let b = r.to_bits();
    let e = (b >> 23) & 0xff;
    if e == 0xff || (e == 0 && b & 0x7f_ffff != 0) {
        None
    } else {
        Some(b)
    }
}

#[inline(always)]
fn fadd(a: u32, b: u32) -> Option<u32> {
    fin(dec(a)? + dec(b)?)
}

#[inline(always)]
fn fsub(a: u32, b: u32) -> Option<u32> {
    fin(dec(a)? - dec(b)?)
}

/// A product of two nonzero operands that is zero underflowed (MU).
#[inline(always)]
fn fmul(a: u32, b: u32) -> Option<u32> {
    let (fa, fb) = (dec(a)?, dec(b)?);
    let r = fin(fa * fb).or_else(|| w(line!()))?;
    if r & 0x7fff_ffff == 0 && fa != 0.0 && fb != 0.0 {
        return None;
    }
    Some(r)
}

/// Ranges where a normal-word access is a plain byte address scaled by 4
/// (`fast::glue::NW_PLAIN`).
fn nw_plain(lo: u64, len: u64) -> bool {
    const R: [(u64, u64); 5] = [
        (0xE_8000, 0x400_0000),
        (0, 0x3_0000),
        (0x3_2000, 0x9_0000),
        (0x1800_0000, 0x3000_0000),
        (0x4000_0000, 1 << 32),
    ];
    R.iter().any(|&(a, b)| lo >= a && lo + len <= b)
}

#[cold]
fn w<T>(line: u32) -> Option<T> {
    if dbg().why {
        eprintln!("why: declined at line {line}");
    }
    None
}

fn run(s: &mut St, hx: &mut Hx) -> Option<()> {
    let prof = dbg().prof;
    let mut tp = if prof { crate::ticks() } else { 0 };
    macro_rules! mark {
        ($i:expr) => {
            if prof {
                let t = crate::ticks();
                PROF[$i].fetch_add(t - tp, std::sync::atomic::Ordering::Relaxed);
                tp = crate::ticks();
            }
        };
    }
    let why = dbg().why;
    if !state_ok(s) {
        if why {
            let c = &s.cfg;
            eprintln!("why: state_ok fm {} nw32 {} conc {} taint {} bank {} {} {} stack {} {} {} mode1 {:?}", c.fast_mem, c.assume_nw32, c.has_concrete, c.data_memory_tainted, c.bank_model, s.bank_pending_mask, s.bank_requested_mask, c.stack_model, s.pc_stack_pending, s.pc_stack_requested, s.r[MODE1]);
        }
        return w(line!());
    }
    for c in (0..16).chain([18, 19, 21, 22, 38, 39, 46, 50, 51, 52, 53, 54, 70]) {
        if !s.r[c].is_c() {
            if why {
                eprintln!("why: reg {c} not known {:?}", s.r[c]);
            }
            return w(line!());
        }
    }
    // M6 = 1, M7 = -1; no circular buffers on I2..I6.
    if s.r[38].b != 1 || s.r[39].b != u32::MAX || s.r[50..=53].iter().any(|v| v.b != 0) {
        if why {
            eprintln!("why: M6 {:?} M7 {:?} L2-6 {:?} B6 {:#x} I6 {:#x} I7 {:#x} B7 {:#x} L7 {:?} mode1 {:#x}", s.r[38], s.r[39], &s.r[50..=54], s.r[70].b, s.r[22].b, s.r[23].b, s.r[71].b, s.r[55], s.r[MODE1].b);
        }
        return w(line!());
    }
    let (i2o, i3o, i5o, i6) = (s.r[18].b, s.r[19].b, s.r[21].b, s.r[22].b);
    let mut x = [0u32; 16];
    for (k, v) in x.iter_mut().enumerate() {
        *v = s.r[k].b;
    }
    let i4 = x[4];
    // I6 is a circular buffer (the software stack): every address used lies
    // inside it, so the wrapped and the linear address are the same.
    let (b6, l6) = (s.r[70].b as u64, s.r[54].b as u64);
    if l6 != 0 && ((i6 as u64) < b6 + 64 || i6 as u64 + 8 > b6 + 4 * l6) {
        return w(line!());
    }
    if (i4 | i6 | x[8] | x[12]) & 3 != 0 || !nw_plain(i6 as u64 - 64, 72) || !nw_plain(i4 as u64, 208)
    {
        return w(line!());
    }
    let w2 = Win::new(&mut s.mem, i6.wrapping_sub(64), 72, true).or_else(|| w(line!()))?;
    let n = w2.rd(i6 + 4).or_else(|| w(line!()))?;
    let maxn = dbg().maxn.unwrap_or(MAXN);
    if n == 0 || n > maxn {
        return w(line!());
    }
    let (ib, ob) = (x[8], x[12]);
    if !nw_plain(ib as u64, 4 * n as u64) || !nw_plain(ob as u64, 4 * n as u64) {
        return w(line!());
    }
    let insns: u64 = match n {
        1 => 76,
        2 => 97,
        _ => 98 + 17 * (n as u64 - 2),
    };
    if s.icount + insns > s.limit {
        return w(line!());
    }
    let w1 = Win::new(&mut s.mem, i4, 208, true).or_else(|| w(line!()))?;
    let w3 = Win::new(&mut s.mem, ib, 4 * n, false).or_else(|| w(line!()))?;
    let w4 = Win::new(&mut s.mem, ob, 4 * n, true).or_else(|| w(line!()))?;

    mark!(1);
    let mut bad = false;
    macro_rules! fadd {
        ($a:expr, $b:expr) => {{
            let r = (f32::from_bits($a) + f32::from_bits($b)).to_bits();
            bad |= exc(r);
            r
        }};
    }
    macro_rules! fsub {
        ($a:expr, $b:expr) => {{
            let r = (f32::from_bits($a) - f32::from_bits($b)).to_bits();
            bad |= exc(r);
            r
        }};
    }
    macro_rules! fmul {
        ($a:expr, $b:expr) => {{
            let (a, b) = (f32::from_bits($a), f32::from_bits($b));
            let r = (a * b).to_bits();
            bad |= exc(r) | ((r & 0x7fff_ffff == 0) & (a != 0.0) & (b != 0.0));
            r
        }};
    }
    macro_rules! ldr {
        ($w:expr, $a:expr) => {
            $w.rd_in($a)
        };
    }
    macro_rules! ld {
        ($w:expr, $a:expr) => {{
            let v = $w.rd_in($a);
            bad |= exc(v);
            v
        }};
    }
    macro_rules! st {
        ($w:expr, $a:expr, $v:expr) => {
            $w.wr_in($a, $v, hx)
        };
    }
    let mut alu: Option<(FlagKind, [u32; 2])> = None;
    let mut mul: Option<u32> = None;
    macro_rules! falu {
        ($r:expr) => {{
            let r = $r;
            alu = Some((FlagKind::Falu, [r, 0]));
            r
        }};
    }
    macro_rules! fmu {
        ($r:expr) => {{
            let r = $r;
            mul = Some(r);
            r
        }};
    }

    // Prologue: spills.
    x[2] = i2o;
    st!(w2, i6 - 28, x[2]);
    x[2] = i3o;
    st!(w2, i6 - 24, x[2]);
    x[2] = i5o;
    st!(w2, i6 - 20, x[2]);
    st!(w2, i6 - 64, x[3]);
    st!(w2, i6 - 60, x[6]);
    st!(w2, i6 - 56, x[7]);
    st!(w2, i6 - 52, x[9]);
    st!(w2, i6 - 48, x[10]);
    st!(w2, i6 - 44, x[11]);
    st!(w2, i6 - 40, x[13]);
    st!(w2, i6 - 36, x[14]);
    st!(w2, i6 - 32, x[15]);
    x[13] = n;
    let shift = Some(n);
    x[2] = n.leading_zeros();
    let mut i5 = x[8];
    x[1] = ld!(w1, i4 + 112);
    x[0] = ld!(w1, i4 + 140);
    x[2] = ld!(w1, i4 + 168);
    let mut i3 = x[12];
    x[15] = ld!(w1, i4 + 48);
    x[10] = ld!(w1, i4 + 132);
    x[9] = ld!(w1, i4 + 160);
    x[14] = ld!(w1, i4 + 56);
    x[4] = falu!(fadd!(x[15], x[14]));
    x[8] = ld!(w1, i4 + 104);
    st!(w2, i6 - 16, x[1]);
    st!(w2, i6 - 12, x[0]);
    st!(w2, i6 - 8, x[2]);
    let i2 = i4 + 0x4c;
    x[7] = ld!(w1, i4 + 12);
    x[2] = ld!(w1, i2 - 48);
    // 36
    let p = fmul!(x[7], x[2]);
    x[2] = fmu!(p);
    x[12] = ld!(w3, i5);
    i5 += 4;
    // 37
    let p = fmul!(x[15], x[12]);
    x[11] = fmu!(p);
    x[6] = ld!(w1, i4 + 16);
    // 38
    let r = fsub!(x[12], x[2]);
    x[2] = falu!(r);
    x[3] = ld!(w1, i2 - 52);
    // 39
    x[15] = ld!(w1, i2 - 56);
    // 40
    let r = fsub!(x[2], x[6]);
    x[2] = falu!(r);
    x[1] = ld!(w2, i6 - 12);
    // 41
    let r = fadd!(x[1], x[10]);
    x[1] = falu!(r);
    x[0] = ld!(w2, i6 - 8);
    // 42
    let p = fmul!(x[3], x[2]);
    x[12] = fmu!(p);
    // 43
    let r = fadd!(x[0], x[9]);
    st!(w1, i2 + 124, x[12]);
    x[0] = falu!(r);
    // 44
    let p = fmul!(x[15], x[12]);
    x[3] = fmu!(p);
    // 45
    let p = fmul!(x[9], x[12]);
    x[9] = fmu!(p);
    // 46
    let r = fadd!(x[7], x[3]);
    x[7] = falu!(r);
    // 47
    let p = fmul!(x[15], x[7]);
    x[15] = fmu!(p);
    // 48
    let p = fmul!(x[10], x[7]);
    x[10] = fmu!(p);
    // 49
    let r = fadd!(x[15], x[6]);
    x[2] = falu!(r);
    // 50
    let p = fmul!(x[8], x[2]);
    x[6] = fmu!(p);
    // 51
    let r = fadd!(x[6], x[10]);
    x[10] = falu!(r);
    // 52
    let r = fadd!(x[9], x[10]);
    x[10] = falu!(r);
    // 53
    let r = fadd!(x[10], x[11]);
    x[11] = falu!(r);
    // 54: R12 = dec(R13); DM(I3, M6) = R11
    x[12] = x[13].wrapping_sub(1);
    alu = Some((FlagKind::Isub, [x[13], 1]));
    st!(w4, i3, x[11]);
    i3 += 4;
    // 55: JUMP IF EQ (DB); the two slots run either way
    let r = fadd!(x[3], x[7]);
    x[3] = falu!(r);
    let r = fadd!(x[15], x[2]);
    x[15] = falu!(r);
    let mut loop_trips = 0u32;
    if n >= 2 {
        // 58
        let r = fadd!(x[4], x[14]);
        x[9] = falu!(r);
        x[2] = ld!(w1, i2 - 48);
        // 59
        let p = fmul!(x[3], x[2]);
        x[2] = fmu!(p);
        x[13] = ld!(w3, i5);
        i5 += 4;
        // 60
        let p = fmul!(x[4], x[13]);
        x[4] = fmu!(p);
        x[11] = ld!(w1, i2 - 52);
        // 61
        let r = fsub!(x[13], x[2]);
        x[2] = falu!(r);
        x[13] = ld!(w1, i2 - 56);
        // 62
        let r = fsub!(x[2], x[15]);
        x[2] = falu!(r);
        // 63
        let p = fmul!(x[11], x[2]);
        x[6] = fmu!(p);
        // 64
        let p = fmul!(x[13], x[6]);
        x[2] = fmu!(p);
        // 65
        let r = fadd!(x[3], x[2]);
        x[7] = falu!(r);
        // 66
        let r = fadd!(x[2], x[7]);
        x[3] = falu!(r);
        // 67: R2 = dec(R12)
        let c = x[12].wrapping_sub(1);
        alu = Some((FlagKind::Isub, [x[12], 1]));
        x[2] = c;
        // 68: JUMP IF EQ (DB); slots 69, 70
        st!(w1, i2 + 124, x[6]);
        let p = fmul!(x[13], x[7]);
        x[10] = fmu!(p);
        // 71: DO (R2) ... UNTIL LCE
        if c != 0 {
            loop_trips = c;
            for _ in 0..c {
                // L1
                let p = fmul!(x[1], x[7]);
                let a = fadd!(x[10], x[15]);
                x[13] = p;
                x[12] = a;
                x[7] = ld!(w1, i2 - 48);
                // L2
                let p = fmul!(x[3], x[7]);
                x[2] = p;
                x[11] = ld!(w2, i6 - 16);
                // L3
                let a = fadd!(x[8], x[11]);
                x[8] = a;
                x[7] = ld!(w3, i5);
                i5 += 4;
                // L4
                let p = fmul!(x[0], x[6]);
                let a = fadd!(x[10], x[12]);
                x[11] = p;
                x[15] = a;
                x[10] = x[9];
                // L5
                let r = fsub!(x[7], x[2]);
                x[6] = r;
                x[2] = ld!(w1, i2 - 52);
                // L6
                x[12] = fmul!(x[8], x[12]);
                // L7
                x[6] = fsub!(x[6], x[15]);
                // L8
                x[12] = fadd!(x[12], x[13]);
                x[13] = x[4];
                // L9
                let p = fmul!(x[2], x[6]);
                let a = fadd!(x[9], x[14]);
                x[6] = p;
                x[9] = a;
                x[2] = ld!(w1, i2 - 56);
                // L10
                let a = fadd!(x[11], x[12]);
                x[11] = a;
                st!(w1, i2 + 124, x[6]);
                // L11
                x[12] = fmul!(x[2], x[6]);
                // L12
                x[4] = fmul!(x[10], x[7]);
                x[10] = ld!(w2, i6 - 8);
                // L13
                x[7] = fadd!(x[3], x[12]);
                x[3] = ld!(w2, i6 - 12);
                // L14
                x[0] = fadd!(x[10], x[0]);
                // L15
                x[1] = fadd!(x[3], x[1]);
                // L16
                let p = fmul!(x[2], x[7]);
                let a = fadd!(x[11], x[13]);
                x[10] = p;
                x[13] = a;
                // L17
                x[3] = fadd!(x[12], x[7]);
                st!(w4, i3, x[13]);
                i3 += 4;
            }
        }
        // b8240d: A1
        let p = fmul!(x[1], x[7]);
        let a = fadd!(x[10], x[15]);
        x[1] = p;
        x[2] = a;
        x[12] = ld!(w2, i6 - 16);
        // A2
        let p = fmul!(x[0], x[6]);
        let a = fadd!(x[8], x[12]);
        x[0] = p;
        x[12] = a;
        // A3
        let r = fadd!(x[10], x[2]);
        x[15] = falu!(r);
        // A4
        let p = fmul!(x[12], x[2]);
        x[12] = fmu!(p);
        // A5
        let r = fadd!(x[12], x[1]);
        x[1] = falu!(r);
        // A6
        let r = fadd!(x[0], x[1]);
        x[1] = falu!(r);
        // A7
        let r = fadd!(x[1], x[4]);
        x[1] = falu!(r);
        // A8
        st!(w4, i3, x[1]);
        i3 += 4;
    }
    let _ = i3;
    // b8241b
    st!(w1, i4 + 196, x[7]);
    st!(w1, i4 + 192, x[2]);
    st!(w1, i4 + 12, x[3]);
    st!(w1, i4 + 16, x[15]);
    // b82423: the epilogue
    let i12 = ldr!(w2, i6 - 4);
    let i2f = ldr!(w2, i6 - 28);
    let i3f = ldr!(w2, i6 - 24);
    let i5f = ldr!(w2, i6 - 20);
    x[3] = ldr!(w2, i6 - 64);
    x[6] = ldr!(w2, i6 - 60);
    x[7] = ldr!(w2, i6 - 56);
    x[9] = ldr!(w2, i6 - 52);
    x[10] = ldr!(w2, i6 - 48);
    x[11] = ldr!(w2, i6 - 44);
    x[13] = ldr!(w2, i6 - 40);
    x[14] = ldr!(w2, i6 - 36);
    // RETURN (DB): target I12 + M14; slot 1, then RFRAME
    let target = i12.wrapping_add(s.r[46].b) & 0xff_ffff;
    x[15] = ldr!(w2, i6 - 32);
    let i6f = ldr!(w2, i6);
    let _ = (i5f, i5);

    mark!(2);
    if bad {
        return w(line!());
    }
    // Commit: the loop bookkeeping first (the only part that can fail).
    if loop_trips != 0 {
        if let Err(e) = run_do_loop(s, 0xb8_23db, 0xb8_240a, 1, loop_trips) {
            if why {
                eprintln!("why: loop bookkeeping failed: {e:?} depth {} pcstack {} loops {}", s.loop_depth, s.pc_stack.n, s.loops.n);
            }
            return w(line!());
        }
        // DO ... UNTIL LCE loads LCNTR with the count.
        s.r[104] = V::c(loop_trips as Int);
    }
    let a = astatx(s, shift, alu, mul);
    s.r[118] = a;
    for (k, v) in x.iter().enumerate() {
        s.r[k] = V::c(*v as Int);
    }
    s.r[18] = V::c(i2f as Int);
    s.r[19] = V::c(i3f as Int);
    s.r[20] = V::c(i4 as Int);
    s.r[21] = V::c(i5f as Int);
    s.r[28] = V::c(i12 as Int);
    s.r[22] = V::c(i6f as Int);
    s.r[23] = V::c(i6 as Int);
    s.pc_sw = target as Int;
    s.icount += insns;
    s.steps += insns as Int;
    s.at_loaded_entry = false;
    if s.cfg.core_timer {
        s.timer_written = false;
    }
    mark!(3);
    Some(())
}
