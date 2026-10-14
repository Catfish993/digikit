//! Canary probe for candidate RAM regions (driven by tools/coldfire/regions.py --confirm).
//!
//! usage: regions_probe SYX [--card IMG] [--base-sha HEX] [--fill START:LEN]...
//!        [--extra N] [--report-every N] [--event T:CODE:down|up]... [--turn T:ENC:DETENTS]...
//!        [--zero-digest] [--pattern canary|zero] [--expect ADDR:FILE]...
//!
//! Boots SYX with the ColdFire emulator (no DSP, unless built with the `sharc`
//! feature and given --dsp IMAGE --dsp-idle COLD_IDLE: then the DSP of
//! `tools/emulator/ready.py` runs on the DSPI2 link as in `coupled::cold`, and the
//! final report gains a `dsp` line). Every --fill region is
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

/// The DSP of a ready directory on the DSPI2 link (the cold boot of
/// `coupled::cold::boot_to_ready`, without the snapshot).
#[cfg(feature = "sharc")]
mod dsp {
    use elektron_native_boot::{
        Emulator,
        sharc_peer::{
            IdleLoop, NativeDsp, Shared, ThreadedHandle, ThreadedPeer, open_engine_patched,
            period_from_env,
        },
    };
    use std::{cell::RefCell, rc::Rc};

    pub struct Dsp {
        handle: ThreadedHandle,
        shared: Rc<RefCell<Shared>>,
        drained: std::cell::Cell<usize>,
    }

    pub fn attach(emu: &mut Emulator, image: &str, cold_idle: &str) -> Dsp {
        let image = std::fs::read(image).expect("DSP image");
        let sidecar = std::fs::read(cold_idle).expect("cold-idle.dsp");
        assert_eq!(
            &sidecar[..8],
            b"DT2DSP01",
            "cold-idle.dsp is not a DT2DSP01 file"
        );
        let base = u64::from_le_bytes(sidecar[8..16].try_into().unwrap());
        let state = sidecar[24..].to_vec();
        let profile = emu.diagnostics();
        let registry = device_profile::Registry::load().unwrap();
        let dsp = registry
            .dsp_for(&profile.device, &profile.version)
            .cloned()
            .expect("DSP profile");
        let idle = IdleLoop {
            range: (dsp.idle_start, dsp.idle_end),
            head: dsp.idle_head,
        };
        emu.enable_ssi_diagnostic(96_000).unwrap();
        let (peer, handle, shared) = ThreadedPeer::spawn(
            move || open_engine_patched(&image, &state, base, None, idle).map(NativeDsp),
            period_from_env(),
            idle.range,
        )
        .expect("DSP worker");
        if dsp.sample_link == Some(device_profile::SampleLink::Lp0) {
            peer.enable_sample_link();
        }
        {
            let mut shared = shared.borrow_mut();
            shared.attached = true;
            shared.dsp_instructions = 0;
        }
        emu.set_dspi2_peer(peer.boxed());
        Dsp {
            handle,
            shared,
            drained: std::cell::Cell::new(0),
        }
    }

    impl Dsp {
        /// Commit every frame sent so far; the halt reason, if the DSP stopped.
        pub fn sync(&self) -> Option<String> {
            self.handle.sync();
            let mut shared = self.shared.borrow_mut();
            // Nobody listens to the output: keep the host buffers small.
            self.drained.set(self.drained.get() + shared.frames.len());
            shared.frames.clear();
            shared.pcm.clear();
            shared.raw.clear();
            shared.halted.clone()
        }
        pub fn frames(&self) -> usize {
            self.drained.get()
        }
        pub fn frames_sent(&self) -> u64 {
            self.handle.frames_sent()
        }
    }
}

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
    let (mut dsp_image, mut dsp_idle): (Option<String>, Option<String>) = (None, None);
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
            "--dsp" => dsp_image = Some(value()),
            "--dsp-idle" => dsp_idle = Some(value()),
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
    #[cfg(feature = "sharc")]
    let dsp = match (&dsp_image, &dsp_idle) {
        (Some(image), Some(idle)) => Some(dsp::attach(&mut emu, image, idle)),
        (None, None) => None,
        _ => panic!("--dsp and --dsp-idle go together"),
    };
    #[cfg(not(feature = "sharc"))]
    assert!(
        dsp_image.is_none() && dsp_idle.is_none(),
        "--dsp needs a build with --features sharc"
    );
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
        #[cfg(feature = "sharc")]
        if let Some(halt) = dsp.as_ref().and_then(dsp::Dsp::sync) {
            println!(
                "{{\"error\":\"DSP halted: {halt}\",\"icount\":{}}}",
                s.icount
            );
            break;
        }
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
    #[cfg(feature = "sharc")]
    if let Some(dsp) = &dsp {
        let halted = dsp.sync();
        println!(
            "{{\"dsp\":{{\"frames\":{},\"frames_sent\":{},\"halted\":{}}}}}",
            dsp.frames(),
            dsp.frames_sent(),
            halted.map_or("null".to_string(), |h| format!("{h:?}"))
        );
    }
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
