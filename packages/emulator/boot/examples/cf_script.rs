//! Run a patched ColdFire image through a script of memory operations (the digikit runtime proofs).
//!
//! usage: cf_script SYX SCRIPT.json [--base-sha HEX] [--card IMG] [--state FILE]
//!
//! Boots SYX (a patched image: `--base-sha` names the stock firmware it was made from) with the optional card image to ready,
//! or loads `--state` (a save_state blob, written when missing), then runs the script, a JSON array of operations. The
//! `DIGI_WRITE_WATCH*` and `DIGI_FLUSH_CHECK` variables are read at construction; the summary is written at the end.
//!   {"op":"run","insns":N}                          run about N instructions
//!   {"op":"poke","addr":"0x..","hex":"..."}         host write (not seen by the write watch)
//!   {"op":"poke32","addr":"0x..","value":"0x.."}
//!   {"op":"peek","addr":"0x..","len":N,"tag":"x"}   print the bytes as hex
//!   {"op":"peek","ptr":true,...}                    the address is the word stored at "addr" (one level of indirection)
//!   {"op":"call","pc":"0x..","args":[..],"max":N}   call a function (interrupts masked, like cf_call); prints d0, instruction count
//!   {"op":"wait32","addr":"0x..","eq":V | "ne":V,"max":N,"tag":"x"}   run until the word is (not) V, at most N instructions
//! Numbers are strings (hex with 0x or decimal) or integers. Prints one JSON line per operation with the instruction count.

use elektron_native_boot::{CallSpec, Emulator};
use emmc_card::{Card, CardIdentity, RandomAccessRead};
use serde_json::{Value, json};
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

fn num(v: &Value) -> u64 {
    let s = v
        .as_str()
        .map(str::to_owned)
        .unwrap_or_else(|| v.to_string());
    match s.trim().strip_prefix("0x") {
        Some(h) => u64::from_str_radix(h, 16).unwrap(),
        None => s.trim().parse::<u64>().unwrap(),
    }
}

fn flag(args: &[String], name: &str) -> Option<String> {
    args.iter()
        .position(|a| a == name)
        .map(|i| args[i + 1].clone())
}

fn peek(emu: &mut Emulator, addr: u32, len: usize) -> Vec<u8> {
    emu.peek_bytes(addr, len).unwrap()
}

fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() < 3 {
        eprintln!("usage: cf_script SYX SCRIPT.json [--base-sha HEX] [--card IMG] [--state FILE]");
        std::process::exit(2);
    }
    let syx = fs::read(&args[1]).unwrap();
    let script: Value = serde_json::from_slice(&fs::read(&args[2]).unwrap()).unwrap();
    let card = flag(&args, "--card").map(|p| {
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
    let mut emu = match flag(&args, "--base-sha") {
        Some(b) => Emulator::new_patched(&syx, card, &b).unwrap(),
        None => Emulator::new(&syx, card).unwrap(),
    };
    let state = flag(&args, "--state");
    let loaded = match &state {
        Some(p) if fs::metadata(p).is_ok() => {
            emu.load_state(&fs::read(p).unwrap()).unwrap();
            true
        }
        _ => false,
    };
    if !loaded {
        loop {
            let snap = emu.step_chunk(250_000);
            let s = &snap.status;
            if s.ready {
                break;
            }
            if s.error.is_some() || s.icount >= 2_000_000_000 {
                eprintln!("boot failed: {:?} at {}", s.error, s.icount);
                std::process::exit(3);
            }
        }
        if let Some(p) = &state {
            fs::write(p, emu.save_state().unwrap()).unwrap();
        }
    }
    println!("{}", json!({"op": "ready", "icount": emu.icount()}));
    let step = |emu: &mut Emulator, n: u64| {
        let end = emu.icount() + n;
        while emu.icount() < end {
            let left = (end - emu.icount()).min(100_000) as u32;
            let snap = emu.step_chunk(left);
            if let Some(e) = &snap.status.error {
                eprintln!("emulator error: {e}");
                std::process::exit(4);
            }
        }
    };
    for op in script.as_array().unwrap() {
        let name = op["op"].as_str().unwrap();
        let tag = op.get("tag").cloned().unwrap_or(Value::Null);
        match name {
            "run" => {
                let t0 = emu.icount();
                step(&mut emu, num(&op["insns"]));
                println!(
                    "{}",
                    json!({"op": name, "tag": tag, "icount": emu.icount(), "ran": emu.icount() - t0})
                );
            }
            "poke" => {
                let bytes: Vec<u8> = op["hex"]
                    .as_str()
                    .unwrap()
                    .as_bytes()
                    .chunks(2)
                    .map(|c| u8::from_str_radix(std::str::from_utf8(c).unwrap(), 16).unwrap())
                    .collect();
                emu.poke_bytes(num(&op["addr"]) as u32, &bytes).unwrap();
                println!(
                    "{}",
                    json!({"op": name, "tag": tag, "icount": emu.icount()})
                );
            }
            "poke32" => {
                emu.poke32(num(&op["addr"]) as u32, num(&op["value"]) as u32)
                    .unwrap();
                println!(
                    "{}",
                    json!({"op": name, "tag": tag, "icount": emu.icount()})
                );
            }
            "peek" => {
                let mut addr = num(&op["addr"]) as u32;
                if op.get("ptr").is_some_and(|v| v.as_bool() == Some(true)) {
                    addr = u32::from_be_bytes(peek(&mut emu, addr, 4).try_into().unwrap());
                }
                let b = peek(&mut emu, addr, num(&op["len"]) as usize);
                let hex: String = b.iter().map(|x| format!("{x:02x}")).collect();
                println!(
                    "{}",
                    json!({"op": name, "tag": tag, "icount": emu.icount(), "hex": hex})
                );
            }
            "call" => {
                let spec = CallSpec {
                    pc: num(&op["pc"]) as u32,
                    args: op["args"]
                        .as_array()
                        .into_iter()
                        .flatten()
                        .map(|v| num(v) as u32)
                        .collect(),
                    sp: op.get("sp").map_or(0x47fe_0000, |v| num(v) as u32),
                    ret_pc: op.get("ret").map_or(0x47fd_f000, |v| num(v) as u32),
                    sr: op.get("sr").map_or(0x2700, |v| num(v) as u16),
                    max_insns: op.get("max").map_or(50_000_000, num),
                };
                let r = emu.call_function(&spec).unwrap();
                println!(
                    "{}",
                    json!({"op": name, "tag": tag, "icount": emu.icount(), "ok": r.finished && r.error.is_none(), "d0": r.d[0],
                           "insns": r.insns, "error": r.error})
                );
            }
            "wait32" => {
                let addr = num(&op["addr"]) as u32;
                let max = num(&op["max"]);
                let t0 = emu.icount();
                let mut done = false;
                while emu.icount() - t0 < max {
                    step(&mut emu, 20_000);
                    let w = u32::from_be_bytes(peek(&mut emu, addr, 4).try_into().unwrap()) as u64;
                    if op.get("eq").is_some_and(|v| w == num(v))
                        || op.get("ne").is_some_and(|v| w != num(v))
                    {
                        done = true;
                        break;
                    }
                }
                println!(
                    "{}",
                    json!({"op": name, "tag": tag, "icount": emu.icount(), "waited": emu.icount() - t0, "ok": done})
                );
            }
            _ => panic!("unknown op {name}"),
        }
    }
    emu.finish_write_watch();
}
