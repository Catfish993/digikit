//! `bootstrap-run`: run the device bootstrap (OS section 2) and print a JSON report.
//!
//! ```text
//! bootstrap-run --boot section_2.bin --container ELE3.bin [--hold func] [--midi FILE.syx]
//!     [--press-at-pc 0x800013ac:trig4:down] [--stop-pc 0x80002dc4] [--watch 0x80003bfc]
//!     [--limit N] [--seconds S] [--reboots N] [--dump-ram 0x40000400:LEN:FILE]
//!     [--flash-out FILE] [--panel-out FILE]
//! ```
//!
//! See `lib.rs` for what is executed and what is modelled. Driven by `tools/emulator/bootstrap_run.py`.

use std::collections::BTreeMap;
use std::fs;

use bootstrap_emu::{CONTAINER_FLASH_OFFSET, FLASH_SIZE, Machine, PcKey};

fn num(s: &str) -> Result<u32, String> {
    let t = s.trim();
    let r = match t.strip_prefix("0x") {
        Some(h) => u32::from_str_radix(h, 16),
        None => t.parse(),
    };
    r.map_err(|e| format!("bad number {s:?}: {e}"))
}

fn key_code(s: &str) -> Result<u8, String> {
    let l = s.to_ascii_lowercase();
    if l == "func" {
        return Ok(0x11);
    }
    if let Some(n) = l.strip_prefix("trig") {
        let n: u8 = n.parse().map_err(|_| format!("bad key {s:?}"))?;
        if (1..=16).contains(&n) {
            return Ok(0x18 + n);
        }
    }
    num(s).map(|v| v as u8)
}

fn json_str(s: &str) -> String {
    let mut o = String::from("\"");
    for c in s.chars() {
        match c {
            '"' => o.push_str("\\\""),
            '\\' => o.push_str("\\\\"),
            c if (c as u32) < 0x20 => o.push_str(&format!("\\u{:04x}", c as u32)),
            c => o.push(c),
        }
    }
    o.push('"');
    o
}

fn main() {
    if let Err(e) = real_main() {
        eprintln!("bootstrap-run: {e}");
        std::process::exit(2);
    }
}

fn real_main() -> Result<(), String> {
    let mut a = std::env::args().skip(1);
    let (mut boot, mut container, mut flash_in, mut flash_out, mut midi, mut panel_out) =
        (None, None, None, None, None, None);
    let mut run_main = false;
    let (mut holds, mut stops, mut watches, mut keys, mut dumps) =
        (vec![], vec![], vec![], vec![], vec![]);
    let (mut limit, mut seconds, mut reboots, mut mode) = (200_000_000u64, 60.0f64, 0u32, 1u32);
    while let Some(k) = a.next() {
        let mut v = || a.next().ok_or(format!("{k} needs a value"));
        match k.as_str() {
            "--boot" => boot = Some(v()?),
            "--container" => container = Some(v()?),
            "--flash" => flash_in = Some(v()?),
            "--flash-out" => flash_out = Some(v()?),
            "--midi" => midi = Some(v()?),
            "--panel-out" => panel_out = Some(v()?),
            "--hold" => holds.extend(v()?.split(',').map(String::from)),
            "--stop-pc" => stops.push(num(&v()?)?),
            "--watch" => watches.push(num(&v()?)?),
            "--press-at-pc" => keys.push(v()?),
            "--dump-ram" => dumps.push(v()?),
            "--limit" => limit = v()?.parse().map_err(|e| format!("--limit: {e}"))?,
            "--seconds" => seconds = v()?.parse().map_err(|e| format!("--seconds: {e}"))?,
            "--run-main" => run_main = true,
            "--mode" => mode = num(&v()?)?,
            "--reboots" => reboots = v()?.parse().map_err(|e| format!("--reboots: {e}"))?,
            other => return Err(format!("unknown argument {other}")),
        }
    }
    let boot = fs::read(boot.ok_or("--boot is required")?).map_err(|e| e.to_string())?;
    let mut flash = match &flash_in {
        Some(p) => fs::read(p).map_err(|e| e.to_string())?,
        None => vec![0xff; FLASH_SIZE],
    };
    if let Some(p) = &container {
        let c = fs::read(p).map_err(|e| e.to_string())?;
        let end = CONTAINER_FLASH_OFFSET + c.len();
        if end > flash.len() {
            return Err("container does not fit the flash".into());
        }
        flash[CONTAINER_FLASH_OFFSET..end].copy_from_slice(&c);
    }
    let mut m = Machine::new(&boot, flash)?;
    m.mode = mode;
    m.run_main = run_main;
    m.restart();
    for h in &holds {
        m.board.key(key_code(h)?, true)?;
    }
    for s in &keys {
        let p: Vec<&str> = s.split(':').collect();
        if p.len() != 3 || !matches!(p[2], "down" | "up") {
            return Err(format!("--press-at-pc wants PC:KEY:down|up, got {s:?}"));
        }
        let pc = num(p[0])?;
        m.watch_pc(pc);
        m.pc_keys.push(PcKey {
            pc,
            code: key_code(p[1])?,
            down: p[2] == "down",
        });
    }
    for pc in &watches {
        m.watch_pc(*pc);
    }
    for pc in &stops {
        m.watch_pc(*pc);
        m.stop_pcs.push(*pc);
    }
    if let Some(p) = &midi {
        m.set_midi(&fs::read(p).map_err(|e| e.to_string())?);
    }
    let (end, boots) = m.run(limit, seconds, reboots);
    for d in &dumps {
        let p: Vec<&str> = d.splitn(3, ':').collect();
        if p.len() != 3 {
            return Err(format!("--dump-ram wants ADDR:LEN:FILE, got {d:?}"));
        }
        let data = m.board.read_mem(num(p[0])?, num(p[1])? as usize);
        fs::write(p[2], data).map_err(|e| e.to_string())?;
    }
    if let Some(p) = &flash_out {
        fs::write(p, &m.board.flash).map_err(|e| e.to_string())?;
    }
    if let Some(p) = &panel_out {
        fs::write(p, &m.board.panel_tx).map_err(|e| e.to_string())?;
    }
    let hex = |v: &[u32]| {
        v.iter()
            .map(|x| format!("\"{x:#x}\""))
            .collect::<Vec<_>>()
            .join(",")
    };
    let mut o = String::from("{\n");
    o += &format!(
        "  \"end\": {},\n  \"icount\": {},\n  \"pc\": \"{:#x}\",\n",
        json_str(&end.name()),
        m.icount,
        m.cpu.pc
    );
    o += &format!("  \"sr\": \"{:#06x}\",\n", m.cpu.sr);
    o += &format!(
        "  \"first_exception\": {},\n",
        match m.first_exception {
            Some((v, pc, ic)) => format!("{{\"vector\":{v},\"pc\":\"{pc:#x}\",\"icount\":{ic}}}"),
            None => "null".into(),
        }
    );
    o += &format!(
        "  \"main_os_entry\": {},\n",
        match m.main_os_entry {
            Some((pc, ic)) => format!("{{\"pc\":\"{pc:#x}\",\"icount\":{ic}}}"),
            None => "null".into(),
        }
    );
    let hits: BTreeMap<String, (u64, u64)> = m
        .pc_hits
        .iter()
        .map(|(k, v)| (format!("{k:#x}"), *v))
        .collect();
    o += "  \"pc_hits\": {";
    o += &hits
        .iter()
        .map(|(k, (n, first))| format!("\"{k}\":{{\"count\":{n},\"first_icount\":{first}}}"))
        .collect::<Vec<_>>()
        .join(",");
    o += "},\n  \"boots\": [";
    o += &boots.iter().map(|b| {
        format!(
            "{{\"end\":{},\"icount\":{},\"erased_sectors\":[{}],\"programmed_bytes\":{},\"flash_min\":{},\"flash_max_end\":\"{:#x}\",\"midi_fed\":{},\"panel_commands\":{}}}",
            json_str(&b.end), b.icount, hex(&b.flash_erased), b.flash_programmed,
            b.flash_min.map_or("null".into(), |x| format!("\"{x:#x}\"")), b.flash_max_end, b.midi_fed, b.panel_commands
        )
    }).collect::<Vec<_>>().join(",");
    o += "],\n";
    let fl = &m.board.flash_log;
    o += &format!(
        "  \"flash\": {{\"erased_sectors\":[{}],\"programmed_bytes\":{},\"not_erased_programs\":{},\"min\":{},\"max_end\":\"{:#x}\"}},\n",
        hex(&fl.erased_sectors),
        fl.programmed_bytes,
        fl.not_erased,
        fl.min_addr.map_or("null".into(), |x| format!("\"{x:#x}\"")),
        fl.max_end
    );
    o += &format!(
        "  \"midi_fed\": {},\n  \"panel_tx_bytes\": {},\n",
        m.board.midi_fed,
        m.board.panel_tx.len()
    );
    let unm: Vec<String> = m
        .board
        .unmapped
        .iter()
        .take(16)
        .map(|(k, v)| format!("\"{k:#x}\":{v}"))
        .collect();
    o += &format!("  \"unmapped_pages\": {{{}}}\n}}\n", unm.join(","));
    print!("{o}");
    Ok(())
}
