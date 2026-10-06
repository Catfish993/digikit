//! Opt-in live coupling of the ColdFire DSPI2 link to the native SHARC+
//! engine (cargo feature `sharc`).
//!
//! One ColdFire DSPI2 frame arrives every 88,000 ColdFire instructions
//! (1.5 kHz, one 32-sample 48 kHz audio block). For each frame the peer
//! 1. delivers it through the engine's SPI2 DMA model and takes the reply,
//! 2. runs the DSP for `period` instructions (the DSP clock / 1.5 kHz,
//!    666,667 at an assumed 1 GHz), noting how many instructions ran before
//!    the RTOS idle loop was reached,
//! 3. completes one SPORT4 audio block and appends its 32 stereo samples to
//!    a shared PCM buffer.
//!
//! The engine is generic over [`DspEngine`] so the plumbing is testable
//! without firmware.

use std::{
    cell::RefCell,
    collections::BTreeMap,
    fs::File,
    io::{BufWriter, Write},
    rc::Rc,
    sync::{Arc, Mutex, mpsc},
    thread,
    time::Instant,
};

use periph::dspi::Peer;

/// DN2 RTOS idle loop, short-word PCs (inclusive start, exclusive end).
pub const DN2_IDLE_RANGE: (u32, u32) = (0xb8_8a49, 0xb8_8abb);
/// Head of the idle task's spin loop (a linked call to `0xb88a49`).
pub const DN2_IDLE_HEAD: u32 = 0xb8_8aab;
/// 1 GHz DSP clock / 1.5 kHz frame rate.
pub const DEFAULT_PERIOD: u32 = 666_667;
const CHUNK: u32 = 1024;
/// Words in one SPORT4A block: 32 stereo samples, L even / R odd.
pub const BLOCK_WORDS: usize = 64;

pub fn fnv1a(data: &[u8]) -> u64 {
    data.iter().fold(0xcbf2_9ce4_8422_2325, |h, &b| {
        (h ^ b as u64).wrapping_mul(0x100_0000_01b3)
    })
}

/// What the peer needs from a DSP engine.
pub trait DspEngine {
    fn spi2_exchange(&mut self, frame: &[u8]) -> Result<Vec<u8>, String>;
    /// Run up to `n` instructions; fewer than `n` means the engine stopped.
    fn step(&mut self, n: u32) -> u32;
    /// Like `step(n)`, but also end at the first instruction boundary whose
    /// PC lies in `range` (start inclusive, end exclusive). None: not
    /// supported (the peer then steps in chunks). Results never depend on
    /// step sizes, so this only saves the chunk boundaries' cost.
    fn step_until_in(&mut self, _n: u32, _range: (u32, u32)) -> Option<u32> {
        None
    }
    /// `step_until_in` that stops only at a boundary whose instruction count
    /// is a positive multiple of `chunk` past `base_icount`, which is an
    /// `icount()` value. None: not supported (the peer steps in chunks).
    fn step_until_in_aligned(
        &mut self,
        _n: u32,
        _range: (u32, u32),
        _chunk: u32,
        _base_icount: u64,
    ) -> Option<u32> {
        None
    }
    /// One SPORT4 block (zero input); `Ok(None)` while the SPORTs are off.
    fn sport_block(&mut self) -> Result<Option<Vec<u8>>, String>;
    /// Short-word PC.
    fn pc(&self) -> u32;
    /// Halt reason, once stopped.
    fn halt_reason(&self) -> Option<String>;
    /// Canonical state blob (hosts that snapshot); empty when unsupported.
    fn export(&self) -> Vec<u8> {
        Vec::new()
    }
    /// Executed-instruction counter (hosts that snapshot).
    fn icount(&self) -> u64 {
        0
    }
    /// Instruction counts by execution tier, where the engine keeps them.
    fn tier_counters(&self) -> Option<sharc_native::TierCounters> {
        None
    }
    /// Aggregate model-gate counters, copied into ordinary worker reports.
    fn model_stats(&self) -> Option<sharc_native::ModelStats> {
        None
    }
    /// Enable or clear opt-in per-entry dispatch profiling.
    fn set_region_profile(&mut self, _enabled: bool) {}
    /// Cumulative opt-in per-entry dispatch profile.
    fn region_profile(&self) -> Option<sharc_native::RegionProfile> {
        None
    }
    /// Enable the existing exact execution maps. The profile is frequency
    /// data only; it adds no per-instruction timer reads.
    fn set_execution_profile(&mut self, _enabled: bool) -> bool {
        false
    }
    /// Exact execution maps, when enabled.
    fn execution_profile(&self) -> Option<ExecutionProfile> {
        None
    }
    /// One 32-bit word of DSP data memory at `addr`, a byte address as the
    /// findings write them (e.g. 0x25f180; consecutive words are 4 apart).
    /// None: unknown or unmodeled, or not supported by the engine.
    fn peek_word(&self, _addr: u32) -> Option<u32> {
        None
    }
}

/// Opt-in diagnostics of the DSP worker (and the synchronous peer):
/// `DIGI_DSPI_LOG=path` appends every DSPI frame to a binary log, and
/// `DIGI_DSP_WATCH=addr[:n],...` with `DIGI_DSP_WATCH_OUT=path.csv` writes
/// watched DSP words after every frame's render.
///
/// Log format: magic `DSPILOG1`, then per frame `u64 BE index`, `u8
/// attached`, `u32 BE tx length`, tx bytes, `u32 BE reply length`, reply bytes.
struct Diag {
    log: Option<BufWriter<File>>,
    log_index: u64,
    watch: Option<Watch>,
}

struct Watch {
    out: BufWriter<File>,
    addrs: Vec<u32>,
    index: u64,
}

/// Parse `addr[:n],...` (hex, `0x` optional, `n` decimal words, default 1,
/// max 256) into the byte address of every watched word. Addresses are the
/// byte addresses `Engine::peek` takes (it reads with `normal_word` false, so
/// no word-to-byte conversion happens); consecutive words are 4 apart.
fn parse_watch(spec: &str) -> Result<Vec<u32>, String> {
    let mut addrs = Vec::new();
    for item in spec.split(',').map(str::trim).filter(|s| !s.is_empty()) {
        let (a, n) = match item.split_once(':') {
            Some((a, n)) => (
                a,
                n.parse::<u32>()
                    .map_err(|_| format!("bad count in {item:?}"))?,
            ),
            None => (item, 1),
        };
        if !(1..=256).contains(&n) {
            return Err(format!("count {n} out of range 1..=256 in {item:?}"));
        }
        let a = a
            .strip_prefix("0x")
            .or_else(|| a.strip_prefix("0X"))
            .unwrap_or(a);
        let base = u32::from_str_radix(a, 16).map_err(|_| format!("bad address in {item:?}"))?;
        addrs.extend((0..n).map(|k| base.wrapping_add(4 * k)));
    }
    if addrs.is_empty() {
        return Err("no addresses".into());
    }
    Ok(addrs)
}

impl Diag {
    fn from_env() -> Self {
        let mut log = None;
        if let Ok(path) = std::env::var("DIGI_DSPI_LOG") {
            match File::create(&path).and_then(|f| {
                let mut w = BufWriter::new(f);
                w.write_all(b"DSPILOG1")?;
                Ok(w)
            }) {
                Ok(w) => log = Some(w),
                Err(e) => eprintln!("sharc_peer: DIGI_DSPI_LOG {path}: {e}"),
            }
        }
        let spec = std::env::var("DIGI_DSP_WATCH").ok();
        let out = std::env::var("DIGI_DSP_WATCH_OUT").ok();
        let mut watch = None;
        match (spec, out) {
            (Some(spec), Some(path)) => match parse_watch(&spec) {
                Ok(addrs) => match File::create(&path).and_then(|f| {
                    let mut w = BufWriter::new(f);
                    let cols: Vec<String> = addrs.iter().map(|a| format!("0x{a:x}")).collect();
                    writeln!(w, "frame,{}", cols.join(","))?;
                    Ok(w)
                }) {
                    Ok(out) => {
                        watch = Some(Watch {
                            out,
                            addrs,
                            index: 0,
                        })
                    }
                    Err(e) => eprintln!("sharc_peer: DIGI_DSP_WATCH_OUT {path}: {e}"),
                },
                Err(e) => eprintln!("sharc_peer: DIGI_DSP_WATCH: {e}"),
            },
            (None, None) => {}
            _ => eprintln!(
                "sharc_peer: DIGI_DSP_WATCH and DIGI_DSP_WATCH_OUT must both be set; watch disabled"
            ),
        }
        Self {
            log,
            log_index: 0,
            watch,
        }
    }

    /// Append one DSPI frame record (call after the exchange).
    fn log_frame(&mut self, attached: bool, tx: &[u8], reply: &[u8]) {
        let index = self.log_index;
        self.log_index += 1;
        let Some(w) = &mut self.log else { return };
        let res = (|| {
            w.write_all(&index.to_be_bytes())?;
            w.write_all(&[attached as u8])?;
            w.write_all(&(tx.len() as u32).to_be_bytes())?;
            w.write_all(tx)?;
            w.write_all(&(reply.len() as u32).to_be_bytes())?;
            w.write_all(reply)
        })();
        if let Err(e) = res {
            eprintln!("sharc_peer: DIGI_DSPI_LOG write failed, log disabled: {e}");
            self.log = None;
        }
    }

    /// Append one CSV row of the watched words (call after the render).
    fn watch_frame<E: DspEngine>(&mut self, engine: &E) {
        let Some(wt) = &mut self.watch else { return };
        let mut row = format!("{}", wt.index);
        wt.index += 1;
        for &a in &wt.addrs {
            row.push(',');
            match engine.peek_word(a) {
                Some(v) => row.push_str(&format!("0x{v:08x}|{:e}", f32::from_bits(v))),
                None => row.push('?'),
            }
        }
        row.push('\n');
        if let Err(e) = wt.out.write_all(row.as_bytes()) {
            eprintln!("sharc_peer: DIGI_DSP_WATCH_OUT write failed, watch disabled: {e}");
            self.watch = None;
        }
    }
}

impl Drop for Diag {
    fn drop(&mut self) {
        if let Some(w) = &mut self.log {
            let _ = w.flush();
        }
        if let Some(wt) = &mut self.watch {
            let _ = wt.out.flush();
        }
    }
}

/// Existing Engine option-26 maps, captured only by an explicit worker request.
#[derive(Clone, Debug)]
pub struct ExecutionProfile {
    /// Kinds 0..4: coverage, interpreter entries, transitions, block exits,
    /// and zero-progress entry bails. Each is tab/space-separated text from
    /// `Engine::profile_text`, not a timing measurement.
    pub maps: [String; 5],
}

/// Per-frame record.
#[derive(Clone, Debug, Default)]
pub struct FrameStat {
    pub first_word: u16,
    /// FNV-1a 64 of the TX frame and of the reply.
    pub tx_hash: u64,
    pub reply_hash: u64,
    pub reply_nonzero_bytes: u32,
    /// DSP instructions run before the first chunk ending in the idle range
    /// (the whole period if idle was never reached).
    pub busy: u32,
    pub reached_idle: bool,
    pub executed: u32,
    /// None: SPORTs not running; zeros were appended.
    pub block: bool,
}

/// Cumulative host/worker timing for the opt-in threaded DSP link profiler.
/// Durations are nanoseconds and overlap across pipelined frames, so they are
/// explanatory stage totals rather than additive wall time.
#[derive(Clone, Copy, Debug, Default)]
pub struct ThreadedTimingProfile {
    pub frames_sent: u64,
    pub frames_completed: u64,
    pub host_enqueue_ns: u64,
    pub host_reply_wait_ns: u64,
    pub host_done_wait_ns: u64,
    pub host_collect_ns: u64,
    pub worker_queue_ns: u64,
    pub worker_spi_ns: u64,
    pub worker_dsp_ns: u64,
    pub worker_sport_ns: u64,
}

impl ThreadedTimingProfile {
    fn add_worker(&mut self, timing: WorkerTiming) {
        self.frames_completed = self.frames_completed.saturating_add(1);
        self.worker_queue_ns = self.worker_queue_ns.saturating_add(timing.queue_ns);
        self.worker_spi_ns = self.worker_spi_ns.saturating_add(timing.spi_ns);
        self.worker_dsp_ns = self.worker_dsp_ns.saturating_add(timing.dsp_ns);
        self.worker_sport_ns = self.worker_sport_ns.saturating_add(timing.sport_ns);
    }
}

#[derive(Clone, Copy, Debug, Default)]
struct WorkerTiming {
    queue_ns: u64,
    spi_ns: u64,
    dsp_ns: u64,
    sport_ns: u64,
}

fn elapsed_ns(start: Instant) -> u64 {
    start.elapsed().as_nanos().min(u64::MAX as u128) as u64
}

#[derive(Default)]
pub struct Shared {
    /// L/R interleaved, `-(q / 2^31)`.
    pub pcm: Vec<f32>,
    /// Raw Q31 words as the engine returned them.
    pub raw: Vec<i32>,
    pub frames: Vec<FrameStat>,
    pub first_words: BTreeMap<u16, u64>,
    pub halted: Option<String>,
    pub dsp_instructions: u64,
    pub nonzero_replies: u64,
    pub missing_blocks: u64,
    /// While false, frames get zero replies and the DSP does not run.
    pub attached: bool,
}

/// Everything about one frame that depends on the DSP engine. It is the
/// same code in the synchronous peer and in the DSP worker thread, so the
/// two cannot drift apart. It never touches [`Shared`].
struct Core<E: DspEngine> {
    engine: E,
    period: u32,
    idle: (u32, u32),
    /// Mirror of `Shared::halted` (only the core ever sets it).
    halted: Option<String>,
}

/// What the exchange half of a frame leaves for the render half.
struct Exchanged {
    stat: FrameStat,
    /// Detached or halted: zero reply, zero audio, no DSP time.
    skipped: bool,
}

/// What one frame adds to [`Shared`] once it has been rendered.
struct Done {
    stat: FrameStat,
    skipped: bool,
    block: Option<Vec<u8>>,
    halted: Option<String>,
    timing: Option<WorkerTiming>,
}

impl<E: DspEngine> Core<E> {
    fn new(engine: E, period: u32) -> Self {
        Self {
            engine,
            period,
            idle: DN2_IDLE_RANGE,
            halted: None,
        }
    }

    /// Run the DSP for one period; returns (executed, busy, reached_idle).
    fn run_period(&mut self) -> (u32, u32, bool) {
        let mut done = 0u32;
        let mut busy = None;
        let in_idle = |pc: u32, idle: (u32, u32)| (idle.0..idle.1).contains(&pc);
        // `busy` is the first chunk end (a multiple of CHUNK, or the period
        // end) with the PC in the idle range. No chunk end before the PC
        // first enters the range can be one, so run to its first entry at
        // once; if that is not a chunk end, run to the first chunk end with
        // the PC in the range (the engine looks at the same ends, without
        // cutting the generated blocks at the ones before). Engines without
        // these stops are stepped in chunks below.
        let base = self.engine.icount();
        if let Some(ran) = self.engine.step_until_in(self.period, self.idle) {
            done = ran;
            if !in_idle(self.engine.pc(), self.idle) {
                // The period ended (or the engine stopped) first.
                return (done, done, false);
            }
            if (done > 0 && done % CHUNK == 0) || done == self.period {
                busy = Some(done);
            } else if let Some(more) =
                self.engine
                    .step_until_in_aligned(self.period - done, self.idle, CHUNK, base)
            {
                // Stopped at a chunk end or the period end in the range
                // (busy, as in the chunk loop, even if the engine stopped
                // there), or elsewhere out of it (not busy).
                done += more;
                if in_idle(self.engine.pc(), self.idle) {
                    busy = Some(done);
                }
            }
        }
        while done < self.period {
            // Once idle is reached the chunking only costs time (the idle
            // skip is per step call); results do not depend on step sizes.
            let n = if busy.is_some() {
                self.period - done
            } else {
                ((done / CHUNK + 1) * CHUNK).min(self.period) - done
            };
            let ran = self.engine.step(n);
            done += ran;
            if busy.is_none() && in_idle(self.engine.pc(), self.idle) {
                busy = Some(done);
            }
            if ran < n {
                break;
            }
        }
        (done, busy.unwrap_or(done), busy.is_some())
    }

    /// First half: deliver the frame through the SPI2 model, take the reply.
    fn exchange(
        &mut self,
        tx: &[u8],
        attached: bool,
        timing: Option<&mut WorkerTiming>,
    ) -> (Vec<u8>, Exchanged) {
        let first_word = tx.get(..2).map_or(0, |b| u16::from_be_bytes([b[0], b[1]]));
        let mut stat = FrameStat {
            first_word,
            tx_hash: fnv1a(tx),
            ..FrameStat::default()
        };
        if !attached || self.halted.is_some() {
            return (
                vec![0; tx.len()],
                Exchanged {
                    stat,
                    skipped: true,
                },
            );
        }
        let spi_start = timing.as_ref().map(|_| Instant::now());
        let exchange = self.engine.spi2_exchange(tx);
        if let (Some(timing), Some(start)) = (timing, spi_start) {
            timing.spi_ns = timing.spi_ns.saturating_add(elapsed_ns(start));
        }
        let reply = match exchange {
            Ok(r) if r.len() == tx.len() => r,
            Ok(r) => {
                eprintln!("sharc_peer: reply length {} != {}", r.len(), tx.len());
                self.halted = Some("bad reply length".into());
                vec![0; tx.len()]
            }
            Err(e) => {
                eprintln!("sharc_peer: spi2_exchange failed: {e}");
                self.halted = Some(e);
                vec![0; tx.len()]
            }
        };
        stat.reply_hash = fnv1a(&reply);
        stat.reply_nonzero_bytes = reply.iter().filter(|&&b| b != 0).count() as u32;
        (
            reply,
            Exchanged {
                stat,
                skipped: false,
            },
        )
    }

    /// Second half: run the DSP for the period and take the audio block.
    /// Reads only the engine state left by the exchange.
    fn render(&mut self, ex: Exchanged, mut timing: Option<&mut WorkerTiming>) -> Done {
        let Exchanged { mut stat, skipped } = ex;
        if skipped {
            return Done {
                stat,
                skipped,
                block: None,
                halted: self.halted.clone(),
                timing: timing.copied(),
            };
        }
        let mut halted = self.halted.clone();
        if halted.is_none() {
            let dsp_start = timing.as_ref().map(|_| Instant::now());
            let (executed, busy, idle) = self.run_period();
            if let (Some(timing), Some(start)) = (timing.as_deref_mut(), dsp_start) {
                timing.dsp_ns = timing.dsp_ns.saturating_add(elapsed_ns(start));
            }
            stat.executed = executed;
            stat.busy = busy;
            stat.reached_idle = idle;
            halted = self.engine.halt_reason();
            if halted.is_some() {
                eprintln!(
                    "sharc_peer: DSP halted: {}",
                    halted.as_deref().unwrap_or("")
                );
            }
        }
        let block = if halted.is_none() {
            let sport_start = timing.as_ref().map(|_| Instant::now());
            let block = self.engine.sport_block();
            if let (Some(timing), Some(start)) = (timing.as_deref_mut(), sport_start) {
                timing.sport_ns = timing.sport_ns.saturating_add(elapsed_ns(start));
            }
            match block {
                Ok(b) => b,
                Err(e) => {
                    eprintln!("sharc_peer: sport_block failed: {e}");
                    halted = Some(e);
                    None
                }
            }
        } else {
            None
        };
        if halted.is_some() && self.halted.is_none() {
            self.halted = halted;
        }
        Done {
            stat,
            skipped,
            block,
            halted: self.halted.clone(),
            timing: timing.copied(),
        }
    }
}

/// Append one finished frame to the shared record.
fn commit(sh: &mut Shared, d: Done) {
    let Done {
        stat,
        skipped,
        block,
        halted,
        timing: _,
    } = d;
    *sh.first_words.entry(stat.first_word).or_default() += 1;
    if skipped {
        sh.pcm.extend_from_slice(&[0.0; BLOCK_WORDS]);
        sh.raw.extend_from_slice(&[0; BLOCK_WORDS]);
        sh.frames.push(stat);
        return;
    }
    let mut stat = stat;
    sh.nonzero_replies += (stat.reply_nonzero_bytes != 0) as u64;
    sh.dsp_instructions += stat.executed as u64;
    if halted.is_some() && sh.halted.is_none() {
        sh.halted = halted;
    }
    match block {
        Some(b) if b.len() == BLOCK_WORDS * 4 => {
            stat.block = true;
            for w in b.chunks_exact(4) {
                let q = i32::from_le_bytes([w[0], w[1], w[2], w[3]]);
                sh.raw.push(q);
                sh.pcm.push(-(q as f64 / 2_147_483_648.0) as f32);
            }
        }
        _ => {
            sh.missing_blocks += 1;
            sh.raw.extend_from_slice(&[0; BLOCK_WORDS]);
            sh.pcm.extend_from_slice(&[0.0; BLOCK_WORDS]);
        }
    }
    sh.frames.push(stat);
}

/// The synchronous peer: the whole frame runs inside the ColdFire bus write.
pub struct SharcPeer<E: DspEngine> {
    core: Core<E>,
    shared: Rc<RefCell<Shared>>,
    diag: Diag,
}

impl<E: DspEngine> SharcPeer<E> {
    pub fn new(engine: E, period: u32) -> (Self, Rc<RefCell<Shared>>) {
        let shared = Rc::new(RefCell::new(Shared {
            attached: true,
            ..Shared::default()
        }));
        let peer = Self {
            core: Core::new(engine, period),
            shared: shared.clone(),
            diag: Diag::from_env(),
        };
        (peer, shared)
    }

    pub fn set_idle_range(&mut self, range: (u32, u32)) {
        self.core.idle = range;
    }

    fn frame(&mut self, tx: &[u8]) -> Vec<u8> {
        let attached = self.shared.borrow().attached;
        let (reply, ex) = self.core.exchange(tx, attached, None);
        self.diag.log_frame(attached, tx, &reply);
        let done = self.core.render(ex, None);
        self.diag.watch_frame(&self.core.engine);
        commit(&mut self.shared.borrow_mut(), done);
        reply
    }
}

impl<E: DspEngine + 'static> SharcPeer<E> {
    pub fn boxed(self) -> Box<dyn Peer> {
        Box::new(self)
    }
}

impl<E: DspEngine> Peer for SharcPeer<E> {
    fn exchange(&mut self, tx: &[u8]) -> Vec<u8> {
        self.frame(tx)
    }
}

enum Msg {
    Frame {
        tx: Vec<u8>,
        attached: bool,
        enqueued: Option<Instant>,
    },
    Export,
    Report,
    RegionProfile(bool),
    ExecutionProfile {
        enabled: bool,
        ack: mpsc::Sender<bool>,
    },
    ExecutionProfileReport,
}

/// Most per-period records the worker keeps (about 22 minutes of audio).
const PERIOD_RECORD_LIMIT: usize = 2_000_000;

/// Frames between the counters the worker publishes (0.1 s of audio).
const LIVE_REPORT_FRAMES: u64 = 150;

/// What the DSP worker reports about itself on request. Reading it is cheap
/// and does not touch DSP state.
#[derive(Clone, Debug, Default)]
pub struct WorkerReport {
    /// CPU time of the worker thread so far (CLOCK_THREAD_CPUTIME_ID).
    pub cpu_ns: u64,
    pub tiers: Option<sharc_native::TierCounters>,
    /// Aggregate model-gate counters. Unlike execution-map text, this is a
    /// small Copy value and is available to ordinary boundary reports.
    pub model_stats: Option<sharc_native::ModelStats>,
    /// Per-entry dispatch costs, only after `set_region_profile(true)`.
    pub region_profile: Option<sharc_native::RegionProfile>,
    /// Wall time of each frame the worker handled (SPI exchange + DSP period +
    /// SPORT block), in nanoseconds, in order; only with link timing, at most
    /// [`PERIOD_RECORD_LIMIT`] entries.
    pub period_ns: Vec<u32>,
    /// Frames the worker handled, including ones past the record limit.
    pub periods: u64,
}

/// ColdFire-thread side of the threaded peer.
struct Link {
    tx: Option<mpsc::Sender<Msg>>,
    reply_rx: mpsc::Receiver<Vec<u8>>,
    done_rx: mpsc::Receiver<Done>,
    export_rx: mpsc::Receiver<(Vec<u8>, u64)>,
    report_rx: mpsc::Receiver<WorkerReport>,
    execution_profile_rx: mpsc::Receiver<Option<ExecutionProfile>>,
    frames_sent: u64,
    /// Frames sent whose [`Done`] has not been committed yet.
    outstanding: usize,
    timing: Option<ThreadedTimingProfile>,
    live: Arc<Mutex<Option<WorkerReport>>>,
    shared: Rc<RefCell<Shared>>,
    worker: Option<thread::JoinHandle<()>>,
}

impl Link {
    fn commit_ready(&mut self, block: bool) {
        while self.outstanding > 0 {
            let done_wait_start = if block && self.timing.is_some() {
                Some(Instant::now())
            } else {
                None
            };
            let done = if block {
                self.done_rx.recv().ok()
            } else {
                self.done_rx.try_recv().ok()
            };
            if let (Some(profile), Some(start)) = (&mut self.timing, done_wait_start) {
                profile.host_done_wait_ns =
                    profile.host_done_wait_ns.saturating_add(elapsed_ns(start));
            }
            let Some(d) = done else { break };
            let collect_start = self.timing.as_ref().map(|_| Instant::now());
            let worker_timing = d.timing;
            commit(&mut self.shared.borrow_mut(), d);
            if let Some(profile) = &mut self.timing {
                if let Some(timing) = worker_timing {
                    profile.add_worker(timing);
                }
                if let Some(start) = collect_start {
                    profile.host_collect_ns =
                        profile.host_collect_ns.saturating_add(elapsed_ns(start));
                }
            }
            self.outstanding -= 1;
        }
    }

    fn worker_died(&mut self, len: usize) -> Vec<u8> {
        let mut sh = self.shared.borrow_mut();
        if sh.halted.is_none() {
            sh.halted = Some("DSP worker thread stopped".into());
        }
        sh.attached = false;
        vec![0; len]
    }
}

impl Drop for Link {
    fn drop(&mut self) {
        self.tx = None; // the worker's recv fails and the thread ends
        if let Some(w) = self.worker.take() {
            let _ = w.join();
        }
    }
}

/// Threaded peer. Frame N: the ColdFire thread sends the frame to the DSP
/// worker and waits only for the reply, which the worker produces after it
/// has finished rendering frame N-1 (one thread, in order), so the reply and
/// every DSP state it depends on are exactly those of the synchronous peer.
/// The worker then renders period N while the ColdFire thread continues to
/// frame N+1. [`Shared`] receives each frame's PCM and statistics in order,
/// when the ColdFire thread next looks (`exchange`, [`ThreadedHandle::sync`]);
/// the DSP engine is created inside the worker thread, so it need not be
/// `Send`.
pub struct ThreadedPeer {
    link: Rc<RefCell<Link>>,
}

/// Host-side handle to a [`ThreadedPeer`] that the ColdFire emulator owns.
#[derive(Clone)]
pub struct ThreadedHandle {
    link: Rc<RefCell<Link>>,
}

impl ThreadedPeer {
    /// Start the worker. `make` runs on the worker thread and builds the
    /// engine there (an error is returned here).
    pub fn spawn<E, F>(
        make: F,
        period: u32,
        idle: (u32, u32),
    ) -> Result<(Self, ThreadedHandle, Rc<RefCell<Shared>>), String>
    where
        E: DspEngine + 'static,
        F: FnOnce() -> Result<E, String> + Send + 'static,
    {
        Self::spawn_with_timing(make, period, idle, false)
    }

    /// Start the worker with optional cumulative host/worker timing.
    pub fn spawn_with_timing<E, F>(
        make: F,
        period: u32,
        idle: (u32, u32),
        timing_enabled: bool,
    ) -> Result<(Self, ThreadedHandle, Rc<RefCell<Shared>>), String>
    where
        E: DspEngine + 'static,
        F: FnOnce() -> Result<E, String> + Send + 'static,
    {
        let (tx, rx) = mpsc::channel::<Msg>();
        let (reply_tx, reply_rx) = mpsc::channel::<Vec<u8>>();
        let (done_tx, done_rx) = mpsc::channel::<Done>();
        let (export_tx, export_rx) = mpsc::channel::<(Vec<u8>, u64)>();
        let (report_tx, report_rx) = mpsc::channel::<WorkerReport>();
        let (execution_profile_tx, execution_profile_rx) =
            mpsc::channel::<Option<ExecutionProfile>>();
        let (ready_tx, ready_rx) = mpsc::channel::<Result<(), String>>();
        let live = Arc::new(Mutex::new(None::<WorkerReport>));
        let live_worker = Arc::clone(&live);
        let worker = thread::Builder::new()
            .name("sharc-dsp".into())
            .spawn(move || {
                // Keep the DSP worker on performance cores (QOS_CLASS_USER_INTERACTIVE
                // in <sys/qos.h>); the default QoS lets macOS schedule it on
                // efficiency cores under load.
                #[cfg(target_os = "macos")]
                {
                    unsafe extern "C" {
                        fn pthread_set_qos_class_self_np(
                            qos_class: u32,
                            relative_priority: i32,
                        ) -> i32;
                    }
                    // SAFETY: affects only the calling thread's scheduling class.
                    let _ = unsafe { pthread_set_qos_class_self_np(0x21, 0) };
                }
                let engine = match make() {
                    Ok(e) => {
                        let _ = ready_tx.send(Ok(()));
                        e
                    }
                    Err(e) => {
                        let _ = ready_tx.send(Err(e));
                        return;
                    }
                };
                let mut core = Core::new(engine, period);
                core.idle = idle;
                // Flushed when the worker ends (Drop), including early returns.
                let mut diag = Diag::from_env();
                let mut period_ns: Vec<u32> = Vec::new();
                let mut periods = 0u64;
                let mut frames_done = 0u64;
                while let Ok(msg) = rx.recv() {
                    match msg {
                        Msg::Frame {
                            tx,
                            attached,
                            enqueued,
                        } => {
                            let mut timing = enqueued.map(|start| WorkerTiming {
                                queue_ns: elapsed_ns(start),
                                ..WorkerTiming::default()
                            });
                            let (reply, ex) = core.exchange(&tx, attached, timing.as_mut());
                            diag.log_frame(attached, &tx, &reply);
                            if reply_tx.send(reply).is_err() {
                                return;
                            }
                            let done = core.render(ex, timing.as_mut());
                            diag.watch_frame(&core.engine);
                            if let Some(t) = &done.timing {
                                periods += 1;
                                if period_ns.len() < PERIOD_RECORD_LIMIT {
                                    let busy = t
                                        .spi_ns
                                        .saturating_add(t.dsp_ns)
                                        .saturating_add(t.sport_ns);
                                    period_ns.push(busy.min(u32::MAX as u64) as u32);
                                }
                            }
                            if done_tx.send(done).is_err() {
                                return;
                            }
                            frames_done += 1;
                            if frames_done % LIVE_REPORT_FRAMES == 0 {
                                let report = WorkerReport {
                                    cpu_ns: crate::cputime::thread_cpu_ns(),
                                    tiers: core.engine.tier_counters(),
                                    model_stats: core.engine.model_stats(),
                                    periods: frames_done,
                                    ..WorkerReport::default()
                                };
                                if let Ok(mut slot) = live_worker.lock() {
                                    *slot = Some(report);
                                }
                            }
                        }
                        Msg::Report => {
                            let _ = report_tx.send(WorkerReport {
                                cpu_ns: crate::cputime::thread_cpu_ns(),
                                tiers: core.engine.tier_counters(),
                                model_stats: core.engine.model_stats(),
                                region_profile: core.engine.region_profile(),
                                period_ns: period_ns.clone(),
                                periods,
                            });
                        }
                        Msg::Export => {
                            let _ = export_tx.send((core.engine.export(), core.engine.icount()));
                        }
                        Msg::RegionProfile(enabled) => core.engine.set_region_profile(enabled),
                        Msg::ExecutionProfile { enabled, ack } => {
                            let _ = ack.send(core.engine.set_execution_profile(enabled));
                        }
                        Msg::ExecutionProfileReport => {
                            let _ = execution_profile_tx.send(core.engine.execution_profile());
                        }
                    }
                }
            })
            .map_err(|e| format!("spawn DSP worker: {e}"))?;
        ready_rx
            .recv()
            .map_err(|_| "DSP worker died at start".to_string())??;
        let shared = Rc::new(RefCell::new(Shared {
            attached: true,
            ..Shared::default()
        }));
        let link = Rc::new(RefCell::new(Link {
            tx: Some(tx),
            reply_rx,
            done_rx,
            export_rx,
            report_rx,
            execution_profile_rx,
            frames_sent: 0,
            outstanding: 0,
            timing: timing_enabled.then(ThreadedTimingProfile::default),
            live,
            shared: shared.clone(),
            worker: Some(worker),
        }));
        Ok((Self { link: link.clone() }, ThreadedHandle { link }, shared))
    }

    pub fn boxed(self) -> Box<dyn Peer> {
        Box::new(self)
    }
}

impl Peer for ThreadedPeer {
    fn exchange(&mut self, tx: &[u8]) -> Vec<u8> {
        let mut l = self.link.borrow_mut();
        let attached = l.shared.borrow().attached;
        let enqueue_start = l.timing.as_ref().map(|_| Instant::now());
        let tx = tx.to_vec();
        let tx_len = tx.len();
        let enqueued = l.timing.as_ref().map(|_| Instant::now());
        let sent = l.tx.as_ref().is_some_and(|c| {
            c.send(Msg::Frame {
                tx,
                attached,
                enqueued,
            })
            .is_ok()
        });
        if let (Some(profile), Some(start)) = (&mut l.timing, enqueue_start) {
            profile.host_enqueue_ns = profile.host_enqueue_ns.saturating_add(elapsed_ns(start));
        }
        if !sent {
            return l.worker_died(tx_len);
        }
        l.outstanding += 1;
        l.frames_sent += 1;
        if let Some(profile) = &mut l.timing {
            profile.frames_sent = profile.frames_sent.saturating_add(1);
        }
        let reply_wait_start = l.timing.as_ref().map(|_| Instant::now());
        match l.reply_rx.recv() {
            Ok(r) => {
                if let (Some(profile), Some(start)) = (&mut l.timing, reply_wait_start) {
                    profile.host_reply_wait_ns =
                        profile.host_reply_wait_ns.saturating_add(elapsed_ns(start));
                }
                l.commit_ready(false);
                r
            }
            Err(_) => {
                if let (Some(profile), Some(start)) = (&mut l.timing, reply_wait_start) {
                    profile.host_reply_wait_ns =
                        profile.host_reply_wait_ns.saturating_add(elapsed_ns(start));
                }
                l.worker_died(tx_len)
            }
        }
    }
}

impl ThreadedHandle {
    /// Enable or clear the worker's opt-in per-entry dispatch profile.
    pub fn set_region_profile(&self, enabled: bool) -> bool {
        self.link
            .borrow()
            .tx
            .as_ref()
            .is_some_and(|c| c.send(Msg::RegionProfile(enabled)).is_ok())
    }
    /// Enable the exact, non-timing execution maps and wait for worker order.
    pub fn set_execution_profile(&self, enabled: bool) -> bool {
        let (ack_tx, ack_rx) = mpsc::channel();
        let sent = self.link.borrow().tx.as_ref().is_some_and(|c| {
            c.send(Msg::ExecutionProfile {
                enabled,
                ack: ack_tx,
            })
            .is_ok()
        });
        sent && ack_rx.recv().unwrap_or(false)
    }
    /// Commit completed frames without waiting; streaming hosts use this between steps.
    /// Call `sync` instead before reading final totals or exporting state.
    pub fn poll(&self) {
        self.link.borrow_mut().commit_ready(false);
    }

    /// Wait for the DSP to finish every frame sent so far and commit them to
    /// [`Shared`]. Call before reading `Shared` for totals or a snapshot.
    pub fn sync(&self) {
        self.link.borrow_mut().commit_ready(true);
    }

    /// Frames handed to the DSP worker so far (one per DSPI2 exchange).
    pub fn frames_sent(&self) -> u64 {
        self.link.borrow().frames_sent
    }

    /// The worker's own report (thread CPU time, tier counters, per-period
    /// wall times). Waits for the frames in flight, like `sync`; `None` if the
    /// worker has stopped.
    pub fn worker_report(&self) -> Option<WorkerReport> {
        let mut l = self.link.borrow_mut();
        l.commit_ready(true);
        if !l.tx.as_ref().is_some_and(|c| c.send(Msg::Report).is_ok()) {
            return None;
        }
        l.report_rx.recv().ok()
    }

    /// The worker's counters as of its last publication (every
    /// [`LIVE_REPORT_FRAMES`] frames; `periods` counts frames handled, and
    /// `period_ns` and `region_profile` are empty). Unlike
    /// [`Self::worker_report`] this never waits for the pipeline, so polling
    /// it does not disturb the timing it reports.
    pub fn live_report(&self) -> Option<WorkerReport> {
        self.link.borrow().live.lock().ok()?.clone()
    }

    /// Fetch the exact maps after draining frames. This is intentionally a
    /// separate request from the ordinary worker report so default reports
    /// never clone profile text.
    pub fn execution_profile(&self) -> Option<ExecutionProfile> {
        let mut l = self.link.borrow_mut();
        l.commit_ready(true);
        if !l
            .tx
            .as_ref()
            .is_some_and(|c| c.send(Msg::ExecutionProfileReport).is_ok())
        {
            return None;
        }
        l.execution_profile_rx.recv().ok().flatten()
    }

    /// Cumulative opt-in timing, or `None` when the peer was started without it.
    pub fn timing_profile(&self) -> Option<ThreadedTimingProfile> {
        self.link.borrow().timing
    }

    /// Canonical DSP state and instruction counter after every frame sent so
    /// far (empty / 0 when the engine does not export).
    pub fn export(&self) -> (Vec<u8>, u64) {
        let mut l = self.link.borrow_mut();
        l.commit_ready(true);
        let sent = l.tx.as_ref().is_some_and(|c| c.send(Msg::Export).is_ok());
        if !sent {
            return (Vec::new(), 0);
        }
        l.export_rx.recv().unwrap_or_default()
    }
}

/// The native engine as a [`DspEngine`].
pub struct NativeDsp(pub sharc_native::Engine);

impl DspEngine for NativeDsp {
    fn spi2_exchange(&mut self, frame: &[u8]) -> Result<Vec<u8>, String> {
        self.0
            .spi2_exchange(frame)
            .map_err(|t| format!("spi2_exchange trap {}", sharc_native::trap_name(t)))
    }
    fn step(&mut self, n: u32) -> u32 {
        self.0.step(n)
    }
    fn step_until_in(&mut self, n: u32, range: (u32, u32)) -> Option<u32> {
        Some(self.0.step_until_in(n, range.0, range.1))
    }
    fn step_until_in_aligned(
        &mut self,
        n: u32,
        range: (u32, u32),
        chunk: u32,
        base_icount: u64,
    ) -> Option<u32> {
        Some(
            self.0
                .step_until_in_aligned(n, range.0, range.1, chunk, base_icount),
        )
    }
    fn sport_block(&mut self) -> Result<Option<Vec<u8>>, String> {
        self.0
            .sport_block(None)
            .map_err(|t| format!("sport_block trap {}", sharc_native::trap_name(t)))
    }
    fn pc(&self) -> u32 {
        self.0.s.pc_sw as u32
    }
    fn halt_reason(&self) -> Option<String> {
        self.0.halt.clone()
    }
    fn export(&self) -> Vec<u8> {
        self.0.export()
    }
    fn icount(&self) -> u64 {
        self.0.s.icount
    }
    fn tier_counters(&self) -> Option<sharc_native::TierCounters> {
        Some(self.0.tier_counters())
    }
    fn model_stats(&self) -> Option<sharc_native::ModelStats> {
        Some(self.0.model_stats)
    }
    fn set_region_profile(&mut self, enabled: bool) {
        self.0.set_region_profile(enabled);
    }
    fn region_profile(&self) -> Option<sharc_native::RegionProfile> {
        self.0.region_profile()
    }
    fn set_execution_profile(&mut self, enabled: bool) -> bool {
        self.0.set_option(26, enabled as i64) == 0
    }
    fn peek_word(&self, addr: u32) -> Option<u32> {
        self.0.peek(addr as u64, 4).ok().flatten()
    }
    fn execution_profile(&self) -> Option<ExecutionProfile> {
        Some(ExecutionProfile {
            maps: [
                self.0.profile_text(0)?,
                self.0.profile_text(1)?,
                self.0.profile_text(2)?,
                self.0.profile_text(3)?,
                self.0.profile_text(4)?,
            ],
        })
    }
}

/// A native engine that the host keeps a handle to, so it can export the
/// canonical state (coupled snapshots) while the peer owns the engine.
#[derive(Clone)]
pub struct SharedDsp(pub Rc<RefCell<sharc_native::Engine>>);

impl SharedDsp {
    pub fn new(e: sharc_native::Engine) -> Self {
        Self(Rc::new(RefCell::new(e)))
    }
    /// Canonical state blob of the engine right now.
    pub fn export(&self) -> Vec<u8> {
        self.0.borrow().export()
    }
}

impl DspEngine for SharedDsp {
    fn spi2_exchange(&mut self, frame: &[u8]) -> Result<Vec<u8>, String> {
        self.0
            .borrow_mut()
            .spi2_exchange(frame)
            .map_err(|t| format!("spi2_exchange trap {}", sharc_native::trap_name(t)))
    }
    fn step(&mut self, n: u32) -> u32 {
        self.0.borrow_mut().step(n)
    }
    fn step_until_in(&mut self, n: u32, range: (u32, u32)) -> Option<u32> {
        Some(self.0.borrow_mut().step_until_in(n, range.0, range.1))
    }
    fn step_until_in_aligned(
        &mut self,
        n: u32,
        range: (u32, u32),
        chunk: u32,
        base_icount: u64,
    ) -> Option<u32> {
        Some(
            self.0
                .borrow_mut()
                .step_until_in_aligned(n, range.0, range.1, chunk, base_icount),
        )
    }
    fn sport_block(&mut self) -> Result<Option<Vec<u8>>, String> {
        self.0
            .borrow_mut()
            .sport_block(None)
            .map_err(|t| format!("sport_block trap {}", sharc_native::trap_name(t)))
    }
    fn pc(&self) -> u32 {
        self.0.borrow().s.pc_sw as u32
    }
    fn halt_reason(&self) -> Option<String> {
        self.0.borrow().halt.clone()
    }
    fn export(&self) -> Vec<u8> {
        self.0.borrow().export()
    }
    fn icount(&self) -> u64 {
        self.0.borrow().s.icount
    }
    fn tier_counters(&self) -> Option<sharc_native::TierCounters> {
        Some(self.0.borrow().tier_counters())
    }
    fn model_stats(&self) -> Option<sharc_native::ModelStats> {
        Some(self.0.borrow().model_stats)
    }
    fn set_region_profile(&mut self, enabled: bool) {
        self.0.borrow_mut().set_region_profile(enabled);
    }
    fn region_profile(&self) -> Option<sharc_native::RegionProfile> {
        self.0.borrow().region_profile()
    }
    fn set_execution_profile(&mut self, enabled: bool) -> bool {
        self.0.borrow_mut().set_option(26, enabled as i64) == 0
    }
    fn peek_word(&self, addr: u32) -> Option<u32> {
        self.0.borrow().peek(addr as u64, 4).ok().flatten()
    }
    fn execution_profile(&self) -> Option<ExecutionProfile> {
        let e = self.0.borrow();
        Some(ExecutionProfile {
            maps: [
                e.profile_text(0)?,
                e.profile_text(1)?,
                e.profile_text(2)?,
                e.profile_text(3)?,
                e.profile_text(4)?,
            ],
        })
    }
}

/// Remove two sources of unknown ("taint") state from a freshly imported DN2
/// state so that code reading them stays on the fast tiers. Both are
/// modelling choices; `SHARC_IMPORT_RAW=1` skips them.
///
/// 1. Special registers (MRF, MRB, MSF, MSB, BFFWRP, BFF_HI, BFF_LO) that are
///    only partly known in the snapshot become fully known: known bits are
///    kept, unknown bits are 0. The structural MODE1STK, PCSTK and the stack
///    mirrors are not touched.
/// 2. Unmapped low-alias space: the absent bytes of [0x28000004, 0x28000100)
///    read as known zero instead of unknown. This is a modelling choice for
///    memory the snapshot does not hold; the owner decided it reads zero.
fn concretise_import(e: &mut sharc_native::Engine) {
    use sharc_native::rt::{MR, MR_MASK, Spec, V};
    for i in 0..e.s.special.len() {
        let present = e.s.special_present[i];
        let new = match e.s.special[i] {
            Spec::V(v) if present && v.is_c() => continue,
            Spec::V(v) => Spec::V(V::c(v.b as _)),
            Spec::M(m) if m.known() => continue,
            Spec::M(m) => Spec::M(MR::new(MR_MASK, m.bits)),
        };
        e.s.special[i] = new;
        e.s.special_present[i] = true;
    }
    for a in 0x2800_0004u64..0x2800_0100 {
        if matches!(e.peek(a, 1), Ok(None)) {
            e.poke(a, &[0], 1);
        }
    }
}

/// Build the DN2 continuation engine from a packed image blob
/// (`tools/sharc_pack_image.py`) and a canonical state blob, with the option
/// set the audio runs use. `clock_base` seeds the diagnostic instruction
/// clock (EMUCLK).
pub fn open_dn2_engine(
    image: &[u8],
    state: &[u8],
    clock_base: u64,
) -> Result<sharc_native::Engine, String> {
    let mut e = sharc_native::Engine::from_image(image).map_err(|c| format!("image error {c}"))?;
    e.import(state)
        .map_err(|c| format!("state import error {c}"))?;
    // 4 runtime decode, 5 instruction clock, 6 clock base, 9 software IRQs,
    // 10 explicit memory model off, 11 approx recips, 12 assume NW32,
    // 21 core timer, 22 peripheral model (19/20 come from the state).
    let mut options = vec![
        (4, 1),
        (5, 1),
        (6, clock_base as i64),
        (9, 1),
        (10, 0),
        (11, 1),
        (12, 1),
        (21, 1),
        (22, 1),
    ];
    // 23-25 idle-loop skip (exact: see docs/findings/07-emulator.md);
    // DSP_IDLE_SKIP=0 turns it off.
    if std::env::var("DSP_IDLE_SKIP").as_deref() != Ok("0") {
        options.extend([
            (23, DN2_IDLE_HEAD as i64),
            (24, DN2_IDLE_RANGE.0 as i64),
            (25, DN2_IDLE_RANGE.1 as i64),
        ]);
    }
    for (k, v) in options {
        if e.set_option(k, v) != 0 {
            return Err(format!("set_option {k} rejected"));
        }
    }
    // After the options: the explicit memory model (10) decides whether an
    // absent byte reads as unknown.
    if std::env::var("SHARC_IMPORT_RAW").as_deref() != Ok("1") {
        concretise_import(&mut e);
    }
    Ok(e)
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Mock {
        pc: u32,
        idle_after: u32,
        ran: u32,
        halt_at_frame: Option<u32>,
        frames: u32,
        reply_byte: u8,
    }

    impl DspEngine for Mock {
        fn spi2_exchange(&mut self, f: &[u8]) -> Result<Vec<u8>, String> {
            self.frames += 1;
            self.ran = 0;
            Ok(vec![self.reply_byte; f.len()])
        }
        fn step(&mut self, n: u32) -> u32 {
            self.ran += n;
            self.pc = if self.ran >= self.idle_after {
                DN2_IDLE_RANGE.0 + 3
            } else {
                0x1c0000
            };
            n
        }
        fn sport_block(&mut self) -> Result<Option<Vec<u8>>, String> {
            if self.halt_at_frame == Some(self.frames) {
                return Err("boom".into());
            }
            let mut b = Vec::new();
            for i in 0..BLOCK_WORDS as i32 {
                // L = -(2^30), R = 2^30 on every frame.
                let w: i32 = if i % 2 == 0 { -(1 << 30) } else { 1 << 30 };
                b.extend_from_slice(&w.to_le_bytes());
            }
            Ok(Some(b))
        }
        fn pc(&self) -> u32 {
            self.pc
        }
        fn halt_reason(&self) -> Option<String> {
            None
        }
    }

    fn mock(halt_at_frame: Option<u32>) -> Mock {
        Mock {
            pc: 0,
            idle_after: 10_000,
            ran: 0,
            halt_at_frame,
            frames: 0,
            reply_byte: 0xab,
        }
    }

    /// A PC that is in the idle range exactly while the instruction count
    /// lies in one of `idle` ([start, end) intervals), optionally able to
    /// stop at the first such boundary (`fast`), and halting at `halt`.
    struct Spans {
        t: u32,
        idle: Vec<(u32, u32)>,
        halt: Option<u32>,
        fast: bool,
    }

    impl Spans {
        fn idle_at(&self, t: u32) -> bool {
            self.idle.iter().any(|&(a, b)| (a..b).contains(&t))
        }
        fn run(&mut self, n: u32, stop_in_idle: bool) -> u32 {
            let start = self.t;
            for _ in 0..n {
                if stop_in_idle && self.idle_at(self.t) {
                    break;
                }
                if self.halt == Some(self.t) {
                    break;
                }
                self.t += 1;
            }
            self.t - start
        }
    }

    impl DspEngine for Spans {
        fn spi2_exchange(&mut self, f: &[u8]) -> Result<Vec<u8>, String> {
            Ok(vec![0; f.len()])
        }
        fn step(&mut self, n: u32) -> u32 {
            self.run(n, false)
        }
        fn step_until_in(&mut self, n: u32, _range: (u32, u32)) -> Option<u32> {
            self.fast.then(|| self.run(n, true))
        }
        fn sport_block(&mut self) -> Result<Option<Vec<u8>>, String> {
            Ok(None)
        }
        fn pc(&self) -> u32 {
            if self.idle_at(self.t) {
                DN2_IDLE_RANGE.0
            } else {
                0x1c0000
            }
        }
        fn halt_reason(&self) -> Option<String> {
            (self.halt == Some(self.t)).then(|| "halt".into())
        }
    }

    #[test]
    fn stopping_at_idle_entry_finds_the_same_chunk_end() {
        let period = 10_000;
        let cases: Vec<(Vec<(u32, u32)>, Option<u32>)> = vec![
            (vec![], None),
            (vec![(0, period)], None),
            (vec![(2048, period)], None),
            (vec![(2047, period)], None),
            (vec![(2049, period)], None),
            (vec![(3000, 3100), (5000, period)], None),
            (vec![(3000, 3072), (3073, period)], None),
            (vec![(3000, 3073), (3080, period)], None),
            (vec![(9999, period)], None),
            (vec![(period - 10, period + 1)], None),
            (vec![(5000, period)], Some(4000)),
            (vec![(5000, period)], Some(5500)),
            (vec![(1000, 1500), (6000, period)], Some(7000)),
        ];
        for (idle, halt) in cases {
            let mut got = Vec::new();
            for fast in [false, true] {
                let mut core = Core::new(
                    Spans {
                        t: 0,
                        idle: idle.clone(),
                        halt,
                        fast,
                    },
                    period,
                );
                got.push(core.run_period());
            }
            assert_eq!(got[0], got[1], "idle {idle:?} halt {halt:?}");
        }
    }

    #[test]
    fn frame_flow_pcm_and_busy() {
        let (mut peer, sh) = SharcPeer::new(mock(None), 20_000);
        let mut tx = vec![0u8; 2748];
        tx[1] = 3;
        let reply = peer.exchange(&tx);
        assert_eq!(reply.len(), 2748);
        assert!(reply.iter().all(|&b| b == 0xab));
        let sh = sh.borrow();
        assert_eq!(sh.pcm.len(), BLOCK_WORDS);
        assert_eq!(sh.pcm[0], 0.5);
        assert_eq!(sh.pcm[1], -0.5);
        assert_eq!(sh.raw[0], -(1 << 30));
        let f = &sh.frames[0];
        assert_eq!(f.first_word, 3);
        assert_eq!(f.reply_nonzero_bytes, 2748);
        assert!(f.reached_idle);
        // first 1024-instruction chunk ending at or past 10,000
        assert_eq!(f.busy, 10 * 1024);
        assert_eq!(f.executed, 20_000);
        assert_eq!(sh.dsp_instructions, 20_000);
        assert_eq!(sh.first_words[&3], 1);
    }

    #[test]
    fn engine_error_returns_zeros_and_stays_halted() {
        let (mut peer, sh) = SharcPeer::new(mock(Some(1)), 4096);
        let r = peer.exchange(&[0u8; 8]);
        assert_eq!(r, vec![0xab; 8]); // reply was taken before the block failed
        assert_eq!(sh.borrow().halted.as_deref(), Some("boom"));
        let r = peer.exchange(&[0u8; 8]);
        assert_eq!(r, vec![0; 8]);
        let sh = sh.borrow();
        assert_eq!(sh.frames.len(), 2);
        assert_eq!(sh.pcm.len(), 2 * BLOCK_WORDS);
        assert!(sh.pcm[BLOCK_WORDS..].iter().all(|&x| x == 0.0));
    }

    #[test]
    fn detached_gives_zero_reply_without_running() {
        let (mut peer, sh) = SharcPeer::new(mock(None), 4096);
        sh.borrow_mut().attached = false;
        assert_eq!(peer.exchange(&[1u8; 4]), vec![0; 4]);
        assert_eq!(sh.borrow().dsp_instructions, 0);
        assert_eq!(peer.core.engine.frames, 0);
    }
}

#[cfg(test)]
mod threaded_tests {
    use super::*;

    /// State-dependent engine: every reply and block depends on everything
    /// that happened before, so any reordering shows up in the output.
    struct Chain {
        h: u64,
        halt_after: Option<u32>,
        frames: u32,
        pc: u32,
    }

    impl Chain {
        fn new(halt_after: Option<u32>) -> Self {
            Chain {
                h: 0x1234_5678,
                halt_after,
                frames: 0,
                pc: 0,
            }
        }
        fn mix(&mut self, v: u64) {
            self.h = (self.h ^ v).wrapping_mul(0x100_0000_01b3).rotate_left(13);
        }
    }

    impl DspEngine for Chain {
        fn spi2_exchange(&mut self, f: &[u8]) -> Result<Vec<u8>, String> {
            self.frames += 1;
            self.mix(fnv1a(f));
            let h = self.h;
            Ok((0..f.len()).map(|i| (h >> ((i % 8) * 8)) as u8).collect())
        }
        fn step(&mut self, n: u32) -> u32 {
            self.mix(n as u64);
            self.pc = if self.h & 3 == 0 {
                DN2_IDLE_RANGE.0 + 1
            } else {
                0x1c0000
            };
            n
        }
        fn sport_block(&mut self) -> Result<Option<Vec<u8>>, String> {
            if self.halt_after == Some(self.frames) {
                return Err("chain halt".into());
            }
            self.mix(7);
            let mut b = Vec::new();
            for i in 0..BLOCK_WORDS as u64 {
                self.mix(i);
                b.extend_from_slice(&((self.h >> 20) as i32).to_le_bytes());
            }
            Ok(Some(b))
        }
        fn pc(&self) -> u32 {
            self.pc
        }
        fn halt_reason(&self) -> Option<String> {
            None
        }
        fn export(&self) -> Vec<u8> {
            self.h.to_le_bytes().to_vec()
        }
        fn icount(&self) -> u64 {
            self.frames as u64
        }
    }

    fn tx_for(i: u32) -> Vec<u8> {
        let mut tx = vec![0u8; 64];
        tx[0..2].copy_from_slice(&((i % 5) as u16).to_be_bytes());
        tx[8..12].copy_from_slice(&i.to_le_bytes());
        tx
    }

    type Stat = (u16, u64, u64, u32, u32, bool, u32, bool);
    /// (replies, pcm bits, raw, per-frame stats, counters, halted)
    type Out = (
        Vec<Vec<u8>>,
        Vec<u32>,
        Vec<i32>,
        Vec<Stat>,
        (u64, u64, u64),
        Option<String>,
    );

    fn collect(sh: &Shared, replies: Vec<Vec<u8>>) -> Out {
        (
            replies,
            sh.pcm.iter().map(|x| x.to_bits()).collect(),
            sh.raw.clone(),
            sh.frames
                .iter()
                .map(|f| {
                    (
                        f.first_word,
                        f.tx_hash,
                        f.reply_hash,
                        f.reply_nonzero_bytes,
                        f.busy,
                        f.reached_idle,
                        f.executed,
                        f.block,
                    )
                })
                .collect(),
            (sh.dsp_instructions, sh.nonzero_replies, sh.missing_blocks),
            sh.halted.clone(),
        )
    }

    /// Frames 40..60 are sent detached.
    fn script(i: u32) -> bool {
        !(40..60).contains(&i)
    }

    fn run_sync(n: u32, halt: Option<u32>) -> Out {
        let (mut peer, sh) = SharcPeer::new(Chain::new(halt), 5000);
        let mut replies = Vec::new();
        for i in 0..n {
            sh.borrow_mut().attached = script(i);
            replies.push(peer.exchange(&tx_for(i)));
        }
        let sh = sh.borrow();
        collect(&sh, replies)
    }

    fn run_threaded(n: u32, halt: Option<u32>, poll: bool) -> (Out, (Vec<u8>, u64)) {
        let (mut peer, handle, sh) =
            ThreadedPeer::spawn(move || Ok(Chain::new(halt)), 5000, DN2_IDLE_RANGE).unwrap();
        let mut replies = Vec::new();
        for i in 0..n {
            sh.borrow_mut().attached = script(i);
            replies.push(peer.exchange(&tx_for(i)));
            if poll && i % 17 == 0 {
                handle.sync();
            }
        }
        let exp = handle.export();
        let sh = sh.borrow();
        (collect(&sh, replies), exp)
    }

    #[test]
    fn threaded_matches_sync() {
        for halt in [None, Some(150)] {
            let want = run_sync(300, halt);
            for poll in [false, true] {
                let (got, exp) = run_threaded(300, halt, poll);
                assert!(got == want, "halt={halt:?} poll={poll}");
                assert_eq!(exp.0.len(), 8);
                assert_eq!(exp.1, if halt.is_some() { 150 } else { 280 });
            }
        }
        // The script really exercised the detach and halt paths.
        let w = run_sync(300, Some(150));
        assert_eq!(w.5.as_deref(), Some("chain halt"));
        assert!(w.3[45].2 == 0 && !w.3[45].7);
        assert!(w.3[100].7);
    }

    #[test]
    fn threaded_engine_start_error_is_reported() {
        let r = ThreadedPeer::spawn(
            || Err::<Chain, _>("no engine".to_string()),
            1,
            DN2_IDLE_RANGE,
        );
        assert_eq!(r.err().as_deref(), Some("no engine"));
    }
}

/// `Core::run_period` against the plain chunked loop on the real DN2 engine
/// (private fixtures; run with `--ignored`, see the test).
#[cfg(test)]
mod aligned_stop_tests {
    use super::*;

    /// The native engine with only `step`: `run_period` falls back to the
    /// chunk loop alone, the reference the stops must not differ from.
    struct ChunkedOnly(NativeDsp);

    impl DspEngine for ChunkedOnly {
        fn spi2_exchange(&mut self, frame: &[u8]) -> Result<Vec<u8>, String> {
            self.0.spi2_exchange(frame)
        }
        fn step(&mut self, n: u32) -> u32 {
            self.0.step(n)
        }
        fn sport_block(&mut self) -> Result<Option<Vec<u8>>, String> {
            self.0.sport_block()
        }
        fn pc(&self) -> u32 {
            self.0.pc()
        }
        fn halt_reason(&self) -> Option<String> {
            self.0.halt_reason()
        }
        fn export(&self) -> Vec<u8> {
            self.0.export()
        }
        fn icount(&self) -> u64 {
            self.0.icount()
        }
    }

    fn read_frames(path: &str) -> Vec<Vec<u8>> {
        let data = std::fs::read(path).expect("frames file");
        let u32_at = |o: usize| u32::from_le_bytes(data[o..o + 4].try_into().unwrap()) as usize;
        let mut out = Vec::new();
        let mut o = 4;
        for _ in 0..u32_at(0) {
            let len = u32_at(o);
            out.push(data[o + 4..o + 4 + len].to_vec());
            o += 4 + len;
        }
        out
    }

    fn env(name: &str) -> String {
        std::env::var(name).unwrap_or_else(|_| panic!("set {name}"))
    }

    /// DN2_EQUIV_IMAGE (packed image), DN2_EQUIV_STATE (canonical state the
    /// frames START continues), DN2_EQUIV_FRAMES (`u32 count`, then `u32
    /// length, bytes` per DSPI2 TX frame); optional DN2_EQUIV_START /
    /// DN2_EQUIV_END (default 4600 / 5600) and DN2_EQUIV_CLOCK. Every period
    /// must return the same (executed, busy, reached_idle) and the engines
    /// must end in the same state.
    #[test]
    #[ignore = "needs the DN2 replay fixtures"]
    fn run_period_matches_chunk_loop() {
        let image = std::fs::read(env("DN2_EQUIV_IMAGE")).unwrap();
        let state = std::fs::read(env("DN2_EQUIV_STATE")).unwrap();
        let frames = read_frames(&env("DN2_EQUIV_FRAMES"));
        let num = |k: &str, d: usize| std::env::var(k).map_or(d, |v| v.parse().unwrap());
        let (start, end) = (num("DN2_EQUIV_START", 4600), num("DN2_EQUIV_END", 5600));
        let clock = num("DN2_EQUIV_CLOCK", 573_627_620) as u64;
        let period = 667_000;
        let open = || NativeDsp(open_dn2_engine(&image, &state, clock).unwrap());
        let mut new = Core::new(open(), period);
        let mut old = Core::new(ChunkedOnly(open()), period);
        let (mut busy_before_end, mut nonidle) = (0, 0);
        for (i, frame) in frames.iter().enumerate().take(end).skip(start) {
            let (r_new, _) = new.exchange(frame, true, None);
            let (r_old, _) = old.exchange(frame, true, None);
            assert_eq!(r_new, r_old, "SPI2 reply, frame {i}");
            let got = new.run_period();
            let want = old.run_period();
            assert_eq!(got, want, "(executed, busy, reached_idle), frame {i}");
            busy_before_end += (want.2 && want.1 < want.0) as u32;
            nonidle += !want.2 as u32;
            assert_eq!(
                new.engine.sport_block().unwrap(),
                old.engine.sport_block().unwrap(),
                "SPORT4 block, frame {i}"
            );
        }
        assert!(new.engine.halt_reason().is_none() && old.engine.halt_reason().is_none());
        new.engine.0.export_ranges = true;
        old.engine.0.0.export_ranges = true;
        let (a, b) = (new.engine.export(), old.engine.export());
        assert!(a == b, "final state differs");
        eprintln!(
            "{} periods equal; idle reached before the period end in {busy_before_end}, idle never reached in \
             {nonidle}; state fnv {:016x} ({} bytes)",
            end - start,
            fnv1a(&a),
            a.len()
        );
        assert!(busy_before_end > 0, "the idle stops were never exercised");
    }
}
