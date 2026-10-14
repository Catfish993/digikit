//! Cold-boot a .syx on the ColdFire core only (no DSP) and save the OLED frame.
//!
//! usage: boot_frame SYX BASE_SHA256|- OUT.frame [EXTRA_AFTER_READY=20000000]
//!
//! `-` boots a stock firmware (`Emulator::new`); a SHA-256 boots a patched build of that known
//! firmware (`Emulator::new_patched`). The frame is the raw 128x64 OLED as the emulator keeps it
//! (byte (7 - y/8) + 8*x, bit y%8); `tools/patchkit/patched_image.py` turns it into a PNG.
//! Prints `ready_at=N` and a final `end ...` line like `card_boot`.

use elektron_native_boot::Emulator;
use std::{env, fs};

fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() < 4 {
        eprintln!("usage: boot_frame SYX BASE_SHA256|- OUT.frame [EXTRA]");
        std::process::exit(2);
    }
    let syx = fs::read(&args[1]).unwrap();
    let extra: u64 = args.get(4).map_or(20_000_000, |s| s.parse().unwrap());
    let mut emu = if args[2] == "-" {
        Emulator::new(&syx, None).unwrap()
    } else {
        Emulator::new_patched(&syx, None, &args[2]).unwrap()
    };
    let mut ready_at = None;
    loop {
        let snap = emu.step_chunk(250_000);
        let s = &snap.status;
        if s.ready && ready_at.is_none() {
            ready_at = Some(s.icount);
            println!("ready_at={}", s.icount);
        }
        if s.error.is_some()
            || s.icount >= 1_500_000_000
            || ready_at.is_some_and(|r| s.icount - r >= extra)
        {
            println!(
                "end icount={} ready={} main_ui={} error={:?}",
                s.icount, s.ready, s.main_ui_reached, s.error
            );
            break;
        }
    }
    match emu.current_frame() {
        Some((revision, frame)) => {
            fs::write(&args[3], frame).unwrap();
            println!("frame revision={revision} bytes={}", frame.len());
        }
        None => {
            eprintln!("no frame was produced");
            std::process::exit(1);
        }
    }
}
