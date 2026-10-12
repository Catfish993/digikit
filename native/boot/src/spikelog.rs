//! Per-window timing logs for finding real-time spikes, on when
//! `DIGI_SPIKE_LOG=DIR` is set.
//!
//! - The DSP worker writes `DIR/dsp.jsonl`: one record per live-report window
//!   (150 periods, 0.1 audio s) with its busy time, its worst period, the time
//!   it waited for the next frame, and the fast tier's build and compile time.
//! - The coupled run loop writes `DIR/cf.jsonl`: one record per ~20 ms of wall
//!   time with the `step_chunk` time, the gap between calls (time outside the
//!   emulator: the webview pump in the app), the wait for DSP replies and the
//!   audio queue.
//!
//! Both stamp records with one process clock ([`now_ns`]), so
//! `tools/spikes.py` can join them.

use std::fs::File;
use std::io::{BufWriter, Write};
use std::path::PathBuf;
use std::sync::OnceLock;
use std::time::{Duration, Instant};

use serde_json::{Value, json};

/// The wall time of one DSPI2 period (1500 per audio second).
pub const PERIOD_BUDGET_NS: u64 = 1_000_000_000 / 1500;
/// Wall time covered by one `cf.jsonl` record.
const CF_WINDOW_NS: u64 = 20_000_000;
/// Records between flushes, so a killed app keeps all but the last few.
const FLUSH_EVERY: u32 = 16;

fn dir() -> Option<&'static PathBuf> {
    static DIR: OnceLock<Option<PathBuf>> = OnceLock::new();
    DIR.get_or_init(|| {
        std::env::var_os("DIGI_SPIKE_LOG")
            .filter(|v| !v.is_empty())
            .map(PathBuf::from)
    })
    .as_ref()
}

/// True when `DIGI_SPIKE_LOG` is set.
pub fn enabled() -> bool {
    dir().is_some()
}

fn ns(d: Duration) -> u64 {
    d.as_nanos().min(u128::from(u64::MAX)) as u64
}

/// Nanoseconds on the process clock shared by both logs.
pub fn now_ns() -> u64 {
    static T0: OnceLock<Instant> = OnceLock::new();
    ns(T0.get_or_init(Instant::now).elapsed())
}

/// One JSON-lines file in the log directory.
struct SpikeLog {
    out: BufWriter<File>,
    lines: u32,
}

impl SpikeLog {
    /// Opens `DIR/name`, creating DIR. None when the log is off or the file
    /// cannot be created (reported on stderr).
    fn open(name: &str) -> Option<Self> {
        let dir = dir()?;
        let path = dir.join(name);
        match std::fs::create_dir_all(dir).and_then(|()| File::create(&path)) {
            Ok(file) => Some(Self {
                out: BufWriter::new(file),
                lines: 0,
            }),
            Err(e) => {
                eprintln!("spike log {}: {e}", path.display());
                None
            }
        }
    }

    fn write(&mut self, record: &Value) {
        let _ = serde_json::to_writer(&mut self.out, record);
        let _ = self.out.write_all(b"\n");
        self.lines += 1;
        if self.lines % FLUSH_EVERY == 0 {
            let _ = self.out.flush();
        }
    }
}

impl Drop for SpikeLog {
    fn drop(&mut self) {
        let _ = self.out.flush();
    }
}

/// The DSP worker's current window.
pub struct DspWindow {
    log: SpikeLog,
    t0_ns: u64,
    periods: u64,
    busy_ns: u64,
    busy_max_ns: u64,
    over: u64,
    wait_ns: u64,
    wait_max_ns: u64,
    last_end: Option<Instant>,
    cpu0: u64,
    compile0: u64,
    build0: u64,
    built0: u64,
}

impl DspWindow {
    /// None unless `DIGI_SPIKE_LOG` is set. `cpu_ns` is the worker's thread
    /// CPU time now.
    pub fn open(cpu_ns: u64) -> Option<Self> {
        Some(Self {
            log: SpikeLog::open("dsp.jsonl")?,
            t0_ns: now_ns(),
            periods: 0,
            busy_ns: 0,
            busy_max_ns: 0,
            over: 0,
            wait_ns: 0,
            wait_max_ns: 0,
            last_end: None,
            cpu0: cpu_ns,
            compile0: 0,
            build0: 0,
            built0: 0,
        })
    }

    /// A frame arrived: the time since the previous frame ended was spent
    /// waiting for it.
    pub fn frame_start(&mut self) {
        if let Some(end) = self.last_end {
            let wait = ns(end.elapsed());
            self.wait_ns += wait;
            self.wait_max_ns = self.wait_max_ns.max(wait);
        }
    }

    /// A frame was rendered; `busy_ns` is its exchange and render time (None
    /// without link timing).
    pub fn frame_end(&mut self, busy_ns: Option<u64>) {
        self.periods += 1;
        if let Some(busy) = busy_ns {
            self.busy_ns += busy;
            self.busy_max_ns = self.busy_max_ns.max(busy);
            if busy > PERIOD_BUDGET_NS {
                self.over += 1;
            }
        }
        self.last_end = Some(Instant::now());
    }

    /// Writes the window and starts the next. The arguments are running
    /// totals.
    pub fn flush(&mut self, frames: u64, cpu_ns: u64, compile_ns: u64, build_ns: u64, built: u64) {
        let t_ns = now_ns();
        self.log.write(&json!({
            "t0_ns": self.t0_ns,
            "t_ns": t_ns,
            "frames": frames,
            "periods": self.periods,
            "busy_ns": self.busy_ns,
            "busy_max_ns": self.busy_max_ns,
            "over": self.over,
            "wait_ns": self.wait_ns,
            "wait_max_ns": self.wait_max_ns,
            "cpu_ns": cpu_ns.saturating_sub(self.cpu0),
            "compile_ns": compile_ns.saturating_sub(self.compile0),
            "build_ns": build_ns.saturating_sub(self.build0),
            "built": built.saturating_sub(self.built0),
        }));
        self.t0_ns = t_ns;
        self.periods = 0;
        self.busy_ns = 0;
        self.busy_max_ns = 0;
        self.over = 0;
        self.wait_ns = 0;
        self.wait_max_ns = 0;
        self.cpu0 = cpu_ns;
        self.compile0 = compile_ns;
        self.build0 = build_ns;
        self.built0 = built;
    }
}

/// The coupled run loop's current window.
pub struct CfWindow {
    log: SpikeLog,
    t0_ns: u64,
    calls: u64,
    step_ns: u64,
    step_max_ns: u64,
    gap_ns: u64,
    gap_max_ns: u64,
    last_end: Option<Instant>,
    reply0: u64,
}

impl CfWindow {
    /// None unless `DIGI_SPIKE_LOG` is set.
    pub fn open() -> Option<Self> {
        Some(Self {
            log: SpikeLog::open("cf.jsonl")?,
            t0_ns: now_ns(),
            calls: 0,
            step_ns: 0,
            step_max_ns: 0,
            gap_ns: 0,
            gap_max_ns: 0,
            last_end: None,
            reply0: 0,
        })
    }

    /// A `step_chunk` call starts; the time since the previous one returned
    /// was spent outside the emulator.
    pub fn step_start(&mut self) -> Instant {
        let now = Instant::now();
        if let Some(end) = self.last_end {
            let gap = ns(now.duration_since(end));
            self.gap_ns += gap;
            self.gap_max_ns = self.gap_max_ns.max(gap);
        }
        now
    }

    /// The call returned. True when the window is full and should be flushed.
    pub fn step_end(&mut self, start: Instant) -> bool {
        let end = Instant::now();
        let step = ns(end.duration_since(start));
        self.calls += 1;
        self.step_ns += step;
        self.step_max_ns = self.step_max_ns.max(step);
        self.last_end = Some(end);
        now_ns().saturating_sub(self.t0_ns) >= CF_WINDOW_NS
    }

    /// Writes the window and starts the next. `reply_wait_ns` and
    /// `frames_sent` are running totals; `queue` is the audio queue (null
    /// without playback).
    pub fn flush(&mut self, reply_wait_ns: u64, frames_sent: u64, queue: Value) {
        let t_ns = now_ns();
        self.log.write(&json!({
            "t0_ns": self.t0_ns,
            "t_ns": t_ns,
            "calls": self.calls,
            "step_ns": self.step_ns,
            "step_max_ns": self.step_max_ns,
            "gap_ns": self.gap_ns,
            "gap_max_ns": self.gap_max_ns,
            "reply_wait_ns": reply_wait_ns.saturating_sub(self.reply0),
            "frames_sent": frames_sent,
            "queue": queue,
        }));
        self.t0_ns = t_ns;
        self.calls = 0;
        self.step_ns = 0;
        self.step_max_ns = 0;
        self.gap_ns = 0;
        self.gap_max_ns = 0;
        self.reply0 = reply_wait_ns;
    }
}
