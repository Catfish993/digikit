//! Headless encoder probe: boot, turn encoders, report what the firmware did.
//!
//! usage: turn_probe SYX [ENC:DETENTS | b:BUTTON_CODE ...]   (e.g. b:2 1:5 9:1)
//! Boots to ready, settles, then for each turn: queues it through
//! `Emulator::turn`, runs 30M instructions and prints the input counters
//! (`input_pending`, `input_irqs`) plus how many OLED pixels changed against
//! the frame before the turn, and an ASCII render of the frame after it.

use elektron_native_boot::Emulator;
use emmc_card::{Card, CardIdentity, RandomAccessRead};
use std::{
    env, fs,
    io::{Read, Seek, SeekFrom},
    sync::Mutex,
};

struct Backing(Mutex<fs::File>, u64);
impl RandomAccessRead for Backing {
    fn len(&self) -> u64 {
        self.1
    }
    fn read_at(&self, offset: u64, out: &mut [u8]) -> usize {
        let Ok(mut f) = self.0.lock() else { return 0 };
        f.seek(SeekFrom::Start(offset))
            .and_then(|_| f.read_exact(out))
            .map(|_| out.len())
            .unwrap_or(0)
    }
}

fn pixel(frame: &[u8], x: usize, y: usize) -> bool {
    // Same packing as the browser's drawFrame: byte (7 - y/8) + 8*x, bit y%8.
    frame[(7 - y / 8) + 8 * x] >> (y % 8) & 1 != 0
}

fn render(frame: &[u8]) {
    for y in (0..64).step_by(2) {
        let line: String = (0..128)
            .map(|x| match (pixel(frame, x, y), pixel(frame, x, y + 1)) {
                (true, true) => '#',
                (true, false) => '\'',
                (false, true) => ',',
                _ => ' ',
            })
            .collect();
        println!("|{}|", line.trim_end());
    }
}

fn run(emu: &mut Emulator, n: u64, last: &mut Option<Vec<u8>>) -> elektron_native_boot::Status {
    let mut done = 0;
    let mut status = None;
    while done < n {
        let snap = emu.step_chunk(250_000);
        done += 250_000;
        if let Some(frame) = snap.frame {
            *last = Some(frame);
        }
        status = Some(snap.status);
    }
    status.unwrap()
}

fn main() {
    let args: Vec<String> = env::args().collect();
    let syx = fs::read(&args[1]).unwrap();
    // CARD=path boots with a +Drive image (see card_boot.rs).
    let card = env::var_os("CARD").map(|p| {
        let file = fs::File::open(p).unwrap();
        let len = file.metadata().unwrap().len();
        Card::with_backing(
            CardIdentity::from_env().unwrap(),
            Some(Box::new(Backing(Mutex::new(file), len))),
        )
        .unwrap()
    });
    let mut emu = Emulator::new(&syx, card).unwrap();
    let mut frame: Option<Vec<u8>> = None;
    // PROBE_PCS=hex,hex,... counts hits from the start of the run.
    if let Ok(list) = env::var("PROBE_PCS") {
        let pcs: Vec<u32> = list
            .split(',')
            .map(|v| u32::from_str_radix(v.trim_start_matches("0x"), 16).unwrap())
            .collect();
        emu.probe_pcs(&pcs);
    }
    loop {
        let snap = emu.step_chunk(250_000);
        if let Some(f) = snap.frame {
            frame = Some(f);
        }
        if snap.status.ready || snap.status.error.is_some() {
            println!(
                "ready icount={} error={:?}",
                snap.status.icount, snap.status.error
            );
            break;
        }
    }
    let s = run(&mut emu, 30_000_000, &mut frame);
    println!(
        "diag@settle: storage_completions={}",
        emu.diagnostics().storage_completions
    );
    println!("settled icount={} input_ready={}", s.icount, s.input_ready);
    render(frame.as_ref().unwrap());
    for (step, spec) in args[2..].iter().enumerate() {
        if let (Some(dir), Some(f)) = (env::var_os("FRAME_DIR"), frame.as_ref()) {
            fs::write(std::path::Path::new(&dir).join(format!("{step:02}.raw")), f).unwrap();
        }
        let (e, d) = spec.split_once(':').unwrap();
        if e == "d" {
            // d:ENC:DETENTS:COUNT:GAP_K -- a drag: COUNT turns of DETENTS, GAP_K thousand
            // instructions apart, then report the frame change against the start.
            let p: Vec<i64> = d.split(':').map(|v| v.parse().unwrap()).collect();
            let before = frame.clone().unwrap();
            for _ in 0..p[2] {
                emu.turn(p[0] as u8, p[1] as i32).unwrap();
                run(&mut emu, (p[3] * 1000) as u64, &mut frame);
            }
            run(&mut emu, 60_000_000, &mut frame);
            let after = frame.clone().unwrap();
            let changed = (0..128)
                .flat_map(|x| (0..64).map(move |y| (x, y)))
                .filter(|&(x, y)| pixel(&before, x, y) != pixel(&after, x, y))
                .count();
            println!("drag {d}: pixels_changed={changed}");
            continue;
        }
        if e == "c" {
            // c:HOLD:TAP chord, e.g. c:17:10 is FUNC+YES.
            let (h, t) = d.split_once(':').unwrap();
            let (h, t): (u8, u8) = (h.parse().unwrap(), t.parse().unwrap());
            emu.button(h, true).unwrap();
            run(&mut emu, 5_000_000, &mut frame);
            emu.button(t, true).unwrap();
            run(&mut emu, 5_000_000, &mut frame);
            emu.button(t, false).unwrap();
            run(&mut emu, 5_000_000, &mut frame);
            emu.button(h, false).unwrap();
            run(&mut emu, 30_000_000, &mut frame);
            println!("chord {h}+{t}");
            continue;
        }
        if e == "w" {
            // w:MILLIONS waits that many million instructions.
            run(&mut emu, d.parse::<u64>().unwrap() * 1_000_000, &mut frame);
            println!("waited {d}M");
            continue;
        }
        if e == "b" {
            // b:CODE taps a panel button.
            let code: u8 = d.parse().unwrap();
            emu.button(code, true).unwrap();
            run(&mut emu, 5_000_000, &mut frame);
            emu.button(code, false).unwrap();
            run(&mut emu, 30_000_000, &mut frame);
            println!("tap button {code}");
            if env::var_os("TURN_RENDER").is_some() {
                render(frame.as_ref().unwrap());
            }
            continue;
        }
        let (e, d): (u8, i32) = (e.parse().unwrap(), d.parse().unwrap());
        let before = frame.clone().unwrap();
        let before_irqs = emu.snapshot().status.input_irqs;
        emu.turn(e, d).unwrap();
        let s = run(&mut emu, 30_000_000, &mut frame);
        let after = frame.clone().unwrap();
        let changed = (0..128)
            .flat_map(|x| (0..64).map(move |y| (x, y)))
            .filter(|&(x, y)| pixel(&before, x, y) != pixel(&after, x, y))
            .count();
        let (mut x0, mut y0, mut x1, mut y1) = (128, 64, 0, 0);
        for x in 0..128 {
            for y in 0..64 {
                if pixel(&before, x, y) != pixel(&after, x, y) {
                    (x0, y0, x1, y1) = (x0.min(x), y0.min(y), x1.max(x), y1.max(y));
                }
            }
        }
        println!("  changed bbox x {x0}..{x1} y {y0}..{y1}");
        println!(
            "turn enc={e} detents={d}: input_pending={} irqs {}->{} pixels_changed={changed} error={:?}",
            s.input_pending, before_irqs, s.input_irqs, s.error
        );
        if env::var_os("TURN_RENDER").is_some() {
            render(&after);
        }
    }
    if let Some(path) = env::var_os("FRAME_OUT") {
        fs::write(path, frame.as_ref().unwrap()).unwrap(); // raw 1024-byte OLED frame
    }
    let end = emu.snapshot().status;
    println!(
        "final: phase={} pc={:#x} error={:?} icount={}",
        end.phase, end.pc, end.error, end.icount
    );
    if env::var_os("PROBE_RETS").is_some() {
        for ((pc, ret, arg), (n, last)) in emu.probe_rets() {
            println!("probe-ret {pc:#x} from {ret:#x} arg={arg:#x} hits={n} last_icount={last}");
        }
    }
    for (pc, n) in emu.probe_hits() {
        println!("probe {pc:#x} hits={n}");
    }
    let d = emu.diagnostics();
    for (v, n) in d.interrupt_deliveries.iter().enumerate() {
        if *n != 0 {
            println!("irq vector {v}: {n}");
        }
    }
    println!(
        "diag: storage_completions={} dma_written_bytes={} dspi_exchanges={} uart_tx_bytes={}",
        d.storage_completions, d.dma_written_bytes, d.dspi_exchanges, d.uart_tx_bytes
    );
    if env::var_os("PC_SAMPLE").is_some() {
        // Histogram of PCs sampled every 997 instructions, to see where the CPU spends time.
        let mut hist = std::collections::BTreeMap::<u32, u32>::new();
        for _ in 0..20000 {
            let st = emu.step_chunk(997).status;
            *hist.entry(st.pc & !1).or_default() += 1;
        }
        let mut v: Vec<_> = hist.into_iter().collect();
        v.sort_by_key(|&(_, n)| std::cmp::Reverse(n));
        for (pc, n) in v.iter().take(12) {
            println!("pc {pc:#x} x{n}");
        }
    }
    // MUTEX=hexaddr,... dumps a mutex (count, owner TCB, wait head, wait tail)
    // and each task's parked PC (TCB+0x48 = saved a7, [a7+4] = return PC).
    if let Ok(list) = env::var("MUTEX") {
        for a in list.split(',') {
            let a = u32::from_str_radix(a.trim_start_matches("0x"), 16).unwrap();
            let w: Vec<u32> = (0..4)
                .map(|i| emu.peek32(a + 4 * i).unwrap_or(0xdead_dead))
                .collect();
            println!(
                "mutex {a:#x}: count={:#x} owner={:#x} head={:#x} tail={:#x}",
                w[0], w[1], w[2], w[3]
            );
            for (label, tcb) in [("owner", w[1]), ("waiter", w[2])] {
                if tcb == 0 {
                    continue;
                }
                let sp = emu.peek32(tcb + 0x48).unwrap_or(0);
                let pc = emu.peek32(sp + 4).unwrap_or(0);
                println!("  {label} tcb={tcb:#x} sp={sp:#x} parked_pc={pc:#x}");
            }
        }
    }
}
