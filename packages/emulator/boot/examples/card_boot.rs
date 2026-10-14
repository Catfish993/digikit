//! Boot a firmware with a +Drive card image and look for sample names in RAM.
//!
//! usage: card_boot SYX CARD.img|none [EXTRA_AFTER_READY=100000000] NAME...
//! Boots with `Emulator::new(syx, Some(card))` (the desktop app's card path),
//! runs EXTRA instructions past ready, then searches the machine state for
//! each NAME (ASCII and UTF-16LE/BE). A name the firmware loaded from the
//! card's directory records shows up; with a blank card it does not.

use elektron_native_boot::Emulator;
use emmc_card::{Card, CardIdentity, RandomAccessRead};
use std::{
    env, fs,
    io::{Read, Seek, SeekFrom},
    sync::Mutex,
};

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

fn count(haystack: &[u8], needle: &[u8]) -> usize {
    haystack
        .windows(needle.len())
        .filter(|w| *w == needle)
        .count()
}

fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() < 4 {
        eprintln!("usage: card_boot SYX CARD.img|none [EXTRA] NAME...");
        std::process::exit(2);
    }
    let syx = fs::read(&args[1]).unwrap();
    let (extra, names) = match args[3].parse::<u64>() {
        Ok(n) => (n, &args[4..]),
        Err(_) => (100_000_000, &args[3..]),
    };
    let card = if args[2] == "none" {
        None
    } else {
        let file = fs::File::open(&args[2]).unwrap();
        let len = file.metadata().unwrap().len();
        let identity = CardIdentity::from_env().unwrap();
        Some(
            Card::with_backing(
                identity,
                Some(Box::new(Backing {
                    file: Mutex::new(file),
                    len,
                })),
            )
            .unwrap(),
        )
    };
    let mut emu = Emulator::new(&syx, card).unwrap();
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
                "end icount={} ready={} main_ui={} fs_verified={:?} error={:?}",
                s.icount, s.ready, s.main_ui_reached, s.filesystem_verified, s.error
            );
            break;
        }
    }
    let state = emu.save_state().unwrap();
    for name in names {
        let ascii = name.as_bytes().to_vec();
        let le: Vec<u8> = name.encode_utf16().flat_map(|c| c.to_le_bytes()).collect();
        let be: Vec<u8> = name.encode_utf16().flat_map(|c| c.to_be_bytes()).collect();
        println!(
            "name {name:?}: ascii={} utf16le={} utf16be={}",
            count(&state, &ascii),
            count(&state, &le),
            count(&state, &be)
        );
    }
}
