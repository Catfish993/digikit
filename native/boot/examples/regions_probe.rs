//! Canary probe for candidate RAM regions (driven by tools/regions.py --confirm).
//!
//! usage: regions_probe SYX [--card IMG] [--base-sha HEX] [--fill START:LEN]...
//!        [--extra N] [--report-every N] [--event T:CODE:down|up]... [--turn T:ENC:DETENTS]...
//!        [--zero-digest] [--pattern canary|zero] [--expect ADDR:FILE]...
//!
//! Boots SYX with the ColdFire emulator (no DSP). Every --fill region is
//! filled with an address-dependent nonzero pattern just after the reset
//! clear returns (pc 0x40000546 on the images we know), so the firmware sees
//! it from the first instruction after the clear on. After ready, --event and
//! --turn inputs fire at T instructions past ready (T in decimal). A JSON line
//! per report gives, for each region, the words that no longer hold the
//! pattern (count, first, last, dirty 4 KiB pages). With --zero-digest the
//! regions are zeroed before the final state digest, so two runs (with and
//! without --fill) are comparable: a difference means the firmware read the
//! region.

use elektron_native_boot::Emulator;
use emmc_card::{Card, CardIdentity, RandomAccessRead};
use std::{
    env, fs,
    io::{Read, Seek, SeekFrom},
    sync::Mutex,
};

const AFTER_CLEAR_PC: u32 = 0x4000_0546;

struct Backing {
    file: Mutex<fs::File>,
    len: u64,
}
impl RandomAccessRead for Backing {
    fn len(&self) -> u64 {
        self.len
    }
    fn read_at(&self, offset: u64, out: &mut [u8]) -> usize {
        let Ok(mut file) = self.file.lock() else {
            return 0;
        };
        file.seek(SeekFrom::Start(offset))
            .and_then(|_| file.read_exact(out))
            .map(|_| out.len())
            .unwrap_or(0)
    }
}

fn pattern(addr: u32) -> u32 {
    if ZERO.load(std::sync::atomic::Ordering::Relaxed) {
        return 0;
    }
    addr.wrapping_mul(0x9E37_79B1) ^ 0xA5C3_5A3C | 0x8000_0001
}

/// `--pattern zero`: fill with zeros (maps the pages like a real fill does),
/// the baseline for the --zero-digest comparison.
static ZERO: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

fn num(s: &str) -> u32 {
    let s = s.trim();
    match s.strip_prefix("0x") {
        Some(h) => u32::from_str_radix(h, 16).unwrap(),
        None => s.parse().unwrap(),
    }
}

enum Input {
    Button(u8, bool),
    Turn(u8, i32),
}

fn report(emu: &mut Emulator, regions: &[(u32, u32)], tag: &str) {
    let icount = emu.icount();
    let mut out = Vec::new();
    for &(start, len) in regions {
        let mut bad = 0u64;
        let (mut first, mut last) = (0u32, 0u32);
        let mut pages: Vec<u32> = Vec::new();
        let mut a = start;
        while a < start + len {
            let ok = emu.peek32(a) == Some(pattern(a));
            if !ok {
                if bad == 0 {
                    first = a;
                }
                last = a;
                bad += 1;
                let p = a & !0xfff;
                if pages.last() != Some(&p) {
                    pages.push(p);
                }
            }
            a += 4;
        }
        out.push(format!(
            "{{\"start\":\"{start:#x}\",\"len\":{len},\"bad_words\":{bad},\"first\":\"{first:#x}\",\"last\":\"{last:#x}\",\"dirty_pages\":[{}]}}",
            pages
                .iter()
                .take(64)
                .map(|p| format!("\"{p:#x}\""))
                .collect::<Vec<_>>()
                .join(",")
        ));
    }
    println!(
        "{{\"report\":\"{tag}\",\"icount\":{icount},\"regions\":[{}]}}",
        out.join(",")
    );
}

fn main() {
    let mut args = env::args().skip(1);
    let syx_path = args.next().expect("usage: regions_probe SYX [options]");
    let mut card_path = None;
    let mut base_sha = None;
    let mut regions: Vec<(u32, u32)> = Vec::new();
    let mut extra = 400_000_000u64;
    let mut report_every = 0u64;
    let mut zero_digest = false;
    let mut inputs: Vec<(u64, Input)> = Vec::new();
    let mut expects: Vec<(u32, String)> = Vec::new();
    while let Some(arg) = args.next() {
        let mut value = || {
            args.next()
                .unwrap_or_else(|| panic!("{arg}: missing value"))
        };
        match arg.as_str() {
            "--card" => card_path = Some(value()),
            "--base-sha" => base_sha = Some(value()),
            "--fill" => {
                let v = value();
                let (s, l) = v.split_once(':').expect("--fill START:LEN");
                let (s, l) = (num(s), num(l));
                assert!(s & 3 == 0 && l & 3 == 0, "--fill must be word aligned");
                regions.push((s, l));
            }
            "--extra" => extra = u64::from(num(&value())),
            "--report-every" => report_every = u64::from(num(&value())),
            "--zero-digest" => zero_digest = true,
            "--expect" => {
                let v = value();
                let (a, f) = v.split_once(':').expect("--expect ADDR:FILE");
                expects.push((num(a), f.to_string()));
            }
            "--pattern" => ZERO.store(value() == "zero", std::sync::atomic::Ordering::Relaxed),
            "--event" => {
                let v = value();
                let p: Vec<&str> = v.split(':').collect();
                inputs.push((
                    u64::from(num(p[0])),
                    Input::Button(num(p[1]) as u8, p[2] == "down"),
                ));
            }
            "--turn" => {
                let v = value();
                let p: Vec<&str> = v.split(':').collect();
                inputs.push((
                    u64::from(num(p[0])),
                    Input::Turn(num(p[1]) as u8, p[2].parse().unwrap()),
                ));
            }
            other => panic!("unknown argument {other}"),
        }
    }
    inputs.sort_by_key(|(t, _)| *t);
    let syx = fs::read(&syx_path).unwrap();
    let card = card_path.map(|p| {
        let file = fs::File::open(&p).unwrap();
        let len = file.metadata().unwrap().len();
        Card::with_backing(
            CardIdentity::from_env().unwrap(),
            Some(Box::new(Backing {
                file: Mutex::new(file),
                len,
            })),
        )
        .unwrap()
    });
    let mut emu = match &base_sha {
        Some(sha) => Emulator::new_patched(&syx, card, sha).unwrap(),
        None => Emulator::new(&syx, card).unwrap(),
    };
    if !emu.run_until_pc(AFTER_CLEAR_PC, 100_000_000) {
        eprintln!("reset clear did not return to {AFTER_CLEAR_PC:#x}");
        std::process::exit(3);
    }
    let t = std::time::Instant::now();
    for &(start, len) in &regions {
        for a in (start..start + len).step_by(4) {
            emu.poke32(a, pattern(a)).unwrap();
        }
    }
    println!(
        "{{\"filled\":{},\"icount\":{},\"secs\":{:.1}}}",
        regions.len(),
        emu.icount(),
        t.elapsed().as_secs_f64()
    );
    let mut ready_at: Option<u64> = None;
    let mut next_input = 0usize;
    let mut next_report = report_every;
    loop {
        let snap = emu.step_chunk(250_000);
        let s = &snap.status;
        if s.ready && ready_at.is_none() {
            ready_at = Some(s.icount);
            println!("{{\"ready_at\":{}}}", s.icount);
            report(&mut emu, &regions, "ready");
        }
        if let Some(at) = ready_at {
            let elapsed = s.icount - at;
            while next_input < inputs.len() && elapsed >= inputs[next_input].0 {
                let result = match inputs[next_input].1 {
                    Input::Button(code, down) => emu.button(code, down),
                    Input::Turn(enc, d) => emu.turn(enc, d),
                };
                if let Err(e) = result {
                    // A code this panel lacks (the scenarios are shared by devices).
                    println!("{{\"input_error\":{e:?},\"at\":{elapsed}}}");
                }
                next_input += 1;
            }
            if report_every != 0 && elapsed >= next_report {
                report(&mut emu, &regions, "periodic");
                next_report += report_every;
            }
        }
        if s.error.is_some() || s.icount >= 6_000_000_000 {
            println!("{{\"error\":{:?},\"icount\":{}}}", s.error, s.icount);
            break;
        }
        if ready_at.is_some_and(|r| s.icount - r >= extra) {
            break;
        }
    }
    report(&mut emu, &regions, "final");
    for (addr, path) in &expects {
        let bytes = fs::read(path).unwrap();
        let mut bad = 0u64;
        let mut first = 0u32;
        for (k, w) in bytes.chunks(4).enumerate() {
            let mut word = [0u8; 4];
            word[..w.len()].copy_from_slice(w);
            let a = addr + 4 * k as u32;
            if emu.peek32(a) != Some(u32::from_be_bytes(word)) {
                if bad == 0 {
                    first = a;
                }
                bad += 1;
            }
        }
        println!(
            "{{\"expect\":\"{addr:#x}\",\"bytes\":{},\"mismatch_words\":{bad},\"first\":\"{first:#x}\"}}",
            bytes.len()
        );
    }
    if zero_digest {
        for &(start, len) in &regions {
            for a in (start..start + len).step_by(4) {
                emu.poke32(a, 0).unwrap();
            }
        }
    }
    let digest = emu.state_digest().unwrap();
    println!("{{\"digest\":\"{digest}\",\"icount\":{}}}", emu.icount());
}
