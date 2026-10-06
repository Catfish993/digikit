//! DSP memory write-watch (diagnostic). `SHARC_WWATCH=addr[:n],...` (hex byte
//! addresses as `Engine::peek` takes them, optional `:n` consecutive 32-bit
//! words, at most 64 words in all) with `SHARC_WWATCH_OUT=path.csv`.
//!
//! After every dispatch unit the watched words are re-read and each change is
//! logged as `icount,pc_before,unit,addr,old,new`. Units: `i` interpreter
//! single step, `b` generated block, `f` fast tier, `s` idle skip. The
//! granularity is one unit, so a block or fast-tier call reports the PC it
//! started at, not the writing instruction. For exact writer PCs run with
//! `SHARC_AOT=0 SHARC_FAST_REGIONS= SHARC_BLOCKTRANS=` so every instruction is
//! an interpreter step.

use std::io::Write;

const MAX_WORDS: usize = 64;

pub struct WriteWatch {
    out: std::io::BufWriter<std::fs::File>,
    addrs: Vec<u64>,
    last: Vec<Option<u32>>,
    /// The unit awaiting its post-check: (pc before, unit letter).
    pub pending: Option<(u64, char)>,
}

impl WriteWatch {
    pub fn from_env() -> Option<Box<WriteWatch>> {
        let spec = std::env::var("SHARC_WWATCH").ok()?;
        let path = std::env::var_os("SHARC_WWATCH_OUT")?;
        let mut addrs = Vec::new();
        for item in spec.split(',').map(str::trim).filter(|s| !s.is_empty()) {
            let (a, n) = item.split_once(':').unwrap_or((item, "1"));
            let a = a.trim_start_matches("0x");
            let (Ok(a), Ok(n)) = (u64::from_str_radix(a, 16), n.parse::<u64>()) else {
                eprintln!("SHARC_WWATCH: bad item {item:?}; write watch disabled");
                return None;
            };
            for k in 0..n {
                if addrs.len() < MAX_WORDS {
                    addrs.push(a + 4 * k);
                }
            }
        }
        if addrs.is_empty() {
            return None;
        }
        let file = match std::fs::File::create(&path) {
            Ok(f) => f,
            Err(e) => {
                eprintln!("SHARC_WWATCH_OUT {path:?}: {e}; write watch disabled");
                return None;
            }
        };
        let mut out = std::io::BufWriter::new(file);
        let _ = writeln!(out, "icount,pc_before,unit,addr,old,new");
        let last = vec![None; addrs.len()];
        Some(Box::new(WriteWatch {
            out,
            addrs,
            last,
            pending: None,
        }))
    }

    pub fn addrs(&self) -> &[u64] {
        &self.addrs
    }

    /// Record the new values read by the caller (one per watched word) for
    /// the pending unit.
    pub fn record(&mut self, icount: u64, now: &[Option<u32>]) {
        let Some((pc, unit)) = self.pending.take() else {
            return;
        };
        for (i, v) in now.iter().enumerate() {
            if *v != self.last[i] {
                let f = |v: Option<u32>| v.map_or("?".to_string(), |v| format!("{v:#010x}"));
                let _ = writeln!(
                    self.out,
                    "{icount},{pc:#x},{unit},{:#x},{},{}",
                    self.addrs[i],
                    f(self.last[i]),
                    f(*v)
                );
                self.last[i] = *v;
            }
        }
    }
}

impl Drop for WriteWatch {
    fn drop(&mut self) {
        let _ = self.out.flush();
    }
}
