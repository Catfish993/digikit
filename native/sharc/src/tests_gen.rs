//! Fuzz test of the generated lowering (`fast/lower/gen_forms.rs`, written by
//! tools/sharc_lowergen.py from tools/sharc_core): random encodings of each
//! generated form run on the interpreter and on the fast tier, one step at a
//! time, and the machine state must agree. The hand-written lowering is run
//! on the same vectors to count how many each accepts.
//!
//! `cargo test --lib gen_lowering -- --nocapture` prints the per-form table.

use crate::fast::lower::GEN_FORCE;
use crate::mem::Mem;
use crate::rt::*;
use crate::tests_gen_programs::PROGRAMS;
use crate::tests_gen_vectors::VECTORS;
use std::collections::BTreeMap;
use std::sync::atomic::Ordering;

/// `GEN_FORCE` is process-wide: the tests that switch it run one at a time.
static SERIAL: std::sync::Mutex<()> = std::sync::Mutex::new(());

const SPIN_ALIAS: u32 = 0x2800_0000;
const DATA: u32 = 0x25_f000;
const DATA_WORDS: u32 = 0x100;
const PEYEN: u32 = 0x20_0000;

/// A tiny deterministic generator (xorshift64*).
struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u32 {
        self.0 ^= self.0 >> 12;
        self.0 ^= self.0 << 25;
        self.0 ^= self.0 >> 27;
        (self.0.wrapping_mul(0x2545_f491_4f6c_dd1d) >> 32) as u32
    }
    fn below(&mut self, n: u32) -> u32 {
        self.next() % n
    }
}

/// `JUMP` to itself (48 bits), the spin after the instruction under test.
const SPIN: [u16; 3] = [0x073e, 0, 0];

fn engine(parcels: &[u16], mode1: u32, seed: u64, fast: bool) -> crate::Engine {
    engine_cfg(parcels, mode1, seed, fast, 0)
}

/// CFG 1: the runtime's own configuration (bank, stack and timer models).
fn engine_cfg(parcels: &[u16], mode1: u32, seed: u64, fast: bool, cfg: u8) -> crate::Engine {
    let mut bytes = Vec::new();
    for p in parcels.iter().chain(SPIN.iter()) {
        bytes.extend_from_slice(&p.to_le_bytes());
    }
    let mut mem = Mem::new();
    mem.load(SPIN_ALIAS, &bytes);
    mem.reset();
    let mut e = crate::Engine::new(mem);
    assert_eq!(e.set_option(4, 1), 0);
    if cfg == 1 {
        for key in [19, 20] {
            assert_eq!(e.set_option(key, 1), 0);
        }
    }
    e.s.r[114] = V::c(mode1 as Int); // MODE1
    e.s.r[122] = V::c(0); // IRPTL
    e.s.r[123] = V::c(0); // IMASK
    e.s.r[124] = V::c(0); // IMASKP
    // R, I, M, B, L, S and the flag registers known.
    for code in (0..96).chain(118..122) {
        e.s.r[code] = V::c(0);
    }
    for k in 0..DATA_WORDS {
        let a = e.s.mem.canonical_of(DATA + 4 * k);
        for b in 0..4u32 {
            let v = (k as u8).wrapping_mul(37).wrapping_add(b as u8 * 11 + 3);
            e.s.mem.write_byte(a + b, v);
        }
    }
    let mut rng = Rng(seed | 1);
    for c in 0..16 {
        e.s.r[c] = V::c(rng.next() as Int); // R
        e.s.r[80 + c] = V::c(rng.next() as Int); // S
        // I: inside the data block, word aligned.
        e.s.r[16 + c] = V::c((DATA + 0x100 + 4 * rng.below(0x30)) as Int);
        // M: small, both signs.
        e.s.r[32 + c] = V::c((rng.below(13) as i32 - 6) as u32 as Int);
    }
    if fast {
        let mut fe =
            crate::fast::FastEngine::new(Box::new(crate::fast::interp::InterpBackend), &[0]);
        fe.force_cfg(true);
        e.set_fast(Some(Box::new(fe)));
    }
    e
}

fn same(plain: &crate::Engine, fast: &crate::Engine, why: &str) {
    let (p, f) = (&plain.s, &fast.s);
    assert_eq!(p.icount, f.icount, "icount {why}");
    assert_eq!(p.pc_sw, f.pc_sw, "pc {why}");
    for code in 0..NUREG {
        assert_eq!(p.r[code], f.r[code], "r{code} {why}");
    }
    assert_eq!(p.pending, f.pending, "pending {why}");
    let mut diffs = Vec::new();
    for k in 0..DATA_WORDS * 4 {
        let a = p.mem.canonical_of(DATA + k);
        if p.mem.byte(a) != f.mem.byte(a) {
            diffs.push((k, p.mem.byte(a), f.mem.byte(a)));
        }
    }
    if !diffs.is_empty() {
        eprintln!("I0-15 {:x?}", (16..32).map(|c| p.r[c].b).collect::<Vec<_>>());
    }
    assert!(diffs.is_empty(), "data bytes (offset, plain, fast) {diffs:x?} {why}");
}

/// (vectors, ran in the fast tier) per form for the current lowering.
fn run_all(label: &str) -> BTreeMap<&'static str, (u32, u32)> {
    let mut table: BTreeMap<&'static str, (u32, u32)> = BTreeMap::new();
    let only = std::env::var("GEN_FORMS").ok();
    for (vi, (form, parcels, words)) in VECTORS.iter().enumerate() {
        if only.as_ref().is_some_and(|o| !o.split(',').any(|f| f == *form)) {
            continue;
        }
        let n = *parcels as usize;
        let code = &words[..n];
        for (mode1, cfg) in [(0, 0), (PEYEN, 0), (0, 1), (PEYEN, 1)] {
            for k in 0..2u64 {
                let seed = 0x9e37_79b9_7f4a_7c15u64 ^ ((vi as u64) << 8) ^ k;
                let why = format!("{label} {form} {code:04x?} mode1 {mode1:#x} cfg {cfg} seed {k}");
                let mut plain = engine_cfg(code, mode1, seed, false, cfg);
                let mut fast = engine_cfg(code, mode1, seed, true, cfg);
                // Overlapping encodings: the decoder's pick is the form.
                match crate::fast::region::decode_one(&plain.s, 0) {
                    Ok(dec) if dec.form == *form => {}
                    _ => continue,
                }
                let a = plain.step(1);
                let b = fast.step(1);
                assert_eq!(a, b, "{why}");
                if plain.halt.is_some() || fast.halt.is_some() {
                    assert_eq!(
                        format!("{:?}", plain.halt),
                        format!("{:?}", fast.halt),
                        "{why}"
                    );
                    continue;
                }
                same(&plain, &fast, &why);
                // And the spin that follows, in one more chunk of 3.
                assert_eq!(plain.step(3), 3, "{why}: {:?}", plain.halt);
                assert_eq!(fast.step(3), 3, "{why}: {:?}", fast.halt);
                same(&plain, &fast, &why);
                let st = fast.fast.as_ref().unwrap().fast_stats().unwrap();
                let e = table.entry(form).or_default();
                e.0 += 1;
                if st.insns > 0 {
                    e.1 += 1;
                }
            }
        }
    }
    table
}

#[test]
fn gen_lowering_matches_the_interpreter() {
    let _serial = SERIAL.lock().unwrap_or_else(|e| e.into_inner());
    GEN_FORCE.store(1, Ordering::Relaxed);
    let hand = run_all("hand");
    GEN_FORCE.store(2, Ordering::Relaxed);
    let gen_ = run_all("gen");
    GEN_FORCE.store(0, Ordering::Relaxed);
    eprintln!("{:8} {:>6} {:>6} {:>6}", "form", "n", "hand", "gen");
    for (form, (n, g)) in &gen_ {
        let h = hand[form].1;
        eprintln!("{form:8} {n:6} {h:6} {g:6}");
    }
}

// -- whole programs -------------------------------------------------------------

/// An engine over a program (parcels), then the spin. Registers: R and S small
/// floats (or random bits), I12/I13 and M5 as the SIMD loop expects.
fn program_engine(
    parcels: &[u16],
    floats: bool,
    seed: u64,
    entries: &[u32],
    fast: bool,
    cfg: u8,
) -> crate::Engine {
    let mut e = engine_cfg(parcels, 0, seed, false, cfg);
    // The spin that `engine` placed follows the program already.
    let mut rng = Rng(seed ^ 0x5555);
    for k in 0..DATA_WORDS {
        let a = e.s.mem.canonical_of(DATA + 4 * k);
        let v: u32 = if floats {
            // 1.0 + a few ulps of spread, either sign.
            let f = 1.0f32 + (rng.below(1000) as f32) * 0.001;
            (if rng.below(2) == 0 { f } else { -f }).to_bits()
        } else {
            rng.next()
        };
        for (b, byte) in v.to_le_bytes().iter().enumerate() {
            e.s.mem.write_byte(a + b as u32, *byte);
        }
    }
    for c in 0..16 {
        e.s.r[c] = V::c(if floats { (1.5f32 + c as f32).to_bits() } else { rng.next() } as Int);
        e.s.r[80 + c] = V::c(if floats { (0.5f32 + c as f32).to_bits() } else { rng.next() } as Int);
    }
    e.s.r[16 + 12] = V::c((DATA + 0x40) as Int); // I12
    e.s.r[16 + 13] = V::c((DATA + 0x180) as Int); // I13
    e.s.r[32 + 5] = V::c(2); // M5
    if fast {
        let mut fe =
            crate::fast::FastEngine::new(Box::new(crate::fast::interp::InterpBackend), entries);
        fe.force_cfg(true);
        e.set_fast(Some(Box::new(fe)));
    }
    e
}

#[test]
fn gen_programs_match_the_interpreter() {
    let _serial = SERIAL.lock().unwrap_or_else(|e| e.into_inner());
    GEN_FORCE.store(2, Ordering::Relaxed);
    for (name, parcels) in PROGRAMS {
        for (floats, cfg) in [(true, 0), (false, 0), (true, 1), (false, 1)] {
            let seed = 0x1234_5678u64;
            // One entry at the start (the region covers the whole program).
            let entries = [0u32];
            let mut best = 0u64;
            for chunk in [1u32, 2, 3, 5, 7, 11, 64, 1000] {
                let mut plain = program_engine(parcels, floats, seed, &entries, false, cfg);
                let mut fast = program_engine(parcels, floats, seed, &entries, true, cfg);
                let total = 400u32;
                let mut done = 0;
                while done < total {
                    let n = chunk.min(total - done);
                    let why = format!("{name} floats {floats} cfg {cfg} chunk {chunk} after {done}");
                    assert_eq!(plain.step(n), n, "{why}: {:?}", plain.halt);
                    assert_eq!(fast.step(n), n, "{why}: {:?}", fast.halt);
                    done += n;
                    same(&plain, &fast, &why);
                }
                let st = fast.fast.as_ref().unwrap().fast_stats().unwrap();
                best = best.max(st.insns);
            }
            eprintln!("{name} floats {floats} cfg {cfg}: fast tier ran {best} instructions");
            assert!(best > 0, "{name}: the fast tier never ran");
        }
    }
    GEN_FORCE.store(0, Ordering::Relaxed);
}
