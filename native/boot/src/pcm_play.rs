//! Opt-in playback of emulator PCM through `native/live`'s output path
//! (cargo feature `play`): a bounded lock-free SPSC ring read by the cpal
//! real-time callback (`live_audio::audio`).
//!
//! The emulator runs far slower than real time, so playing the PCM as it is
//! produced is mostly silence. Two modes:
//! - [`Mode::Live`]: open the device at once; whatever the emulator has made
//!   is played, and an empty ring plays silence (an underrun, counted).
//! - [`Mode::Buffer`]: hold the PCM back until N seconds have accumulated
//!   (or the producer is done), then play it in real time.
//!
//! Nothing here blocks the producer: [`PcmPlayer::push`] hands the samples to
//! a feeder thread through an unbounded channel. The feeder resamples
//! (linear) to the device rate and fills the ring; the callback itself only
//! pops. The device is opened on the feeder thread, and failure to open it
//! is reported by [`PcmPlayer::spawn`] without affecting the emulator.

use std::{
    any::Any,
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicU64, Ordering},
        mpsc,
    },
    thread,
    time::Duration,
};

use live_audio::{SpscRing, StereoSample, audio};

/// Source rate of the SHARC SPORT audio.
pub const SOURCE_RATE: u32 = 48_000;

/// Stream errors survived by reopening the default output before the error latches.
const MAX_REOPENS: u32 = 16;

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Mode {
    Live,
    /// Start once this many seconds of audio are queued (or at the end).
    Buffer(f64),
}

/// What playback does when the emulator falls behind real time.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Recovery {
    /// Missing frames play as silence (a tone full of gaps sounds like noise).
    Gap,
    /// Stop with a short fade, wait until this many seconds are queued, resume.
    Rebuffer(f64),
    /// Slow playback to the rate audio arrives (the pitch drops with it),
    /// aiming at about this many seconds queued; rebuffers if the ring still
    /// runs dry.
    Stretch(f64),
}

impl Default for Recovery {
    fn default() -> Self {
        Recovery::Rebuffer(0.5)
    }
}

impl Recovery {
    pub fn name(self) -> &'static str {
        match self {
            Recovery::Gap => "gap",
            Recovery::Rebuffer(_) => "rebuffer",
            Recovery::Stretch(_) => "stretch",
        }
    }
}

/// Slowest playback rate relative to real time when stretching.
const STRETCH_MIN: f64 = 0.80;
/// Fastest playback rate relative to real time when stretching.
const STRETCH_MAX: f64 = 1.02;
/// Fraction of the gap to the wanted rate closed per pump (about 250 ms at 5 ms pumps).
const STRETCH_GAIN: f64 = 0.02;

/// Most sink events kept in [`PlaybackStats::events`].
const SINK_EVENT_LIMIT: usize = 10_000;

/// A change in the output's state, timed from the stream start.
#[derive(Clone, Debug, PartialEq, serde::Serialize)]
pub struct SinkEvent {
    pub at_s: f64,
    /// "underrun" (the ring ran dry) or "resume" (a rebuffer refilled).
    pub kind: &'static str,
    pub queued_device_frames: u64,
    pub feeder_pending_source_frames: u64,
}

/// `DIGI_EMU_SINK_WAV=path`: write what the output device actually played
/// (after resampling, gaps and fades) as a float WAV at the device rate.
struct SinkCapture {
    ring: Arc<SpscRing>,
    path: std::path::PathBuf,
    writer: Option<crate::wav::WavWriter>,
    error: Option<String>,
    scratch: Vec<StereoSample>,
    interleaved: Vec<f32>,
}

impl SinkCapture {
    fn from_env() -> Option<Self> {
        let path = std::env::var_os("DIGI_EMU_SINK_WAV")?;
        Some(Self {
            ring: Arc::new(SpscRing::with_capacity(1 << 17)),
            path: path.into(),
            writer: None,
            error: None,
            scratch: vec![StereoSample::default(); 4096],
            interleaved: Vec::with_capacity(8192),
        })
    }

    /// Move whatever the callback played since the last call into the file.
    fn drain(&mut self, device_rate: u32) {
        if self.error.is_some() {
            return;
        }
        if self.writer.is_none() {
            match crate::wav::WavWriter::create(&self.path, device_rate, 2) {
                Ok(w) => self.writer = Some(w),
                Err(e) => {
                    self.error = Some(format!("error: {}: {e}", self.path.display()));
                    return;
                }
            }
        }
        loop {
            let n = self.ring.pop_into(&mut self.scratch);
            if n == 0 {
                break;
            }
            self.interleaved.clear();
            for s in &self.scratch[..n] {
                self.interleaved.push(s.l);
                self.interleaved.push(s.r);
            }
            if let Some(w) = &mut self.writer
                && let Err(e) = w.write(&self.interleaved)
            {
                self.error = Some(format!("error: {}: {e}", self.path.display()));
                self.writer = None;
                return;
            }
        }
    }

    fn describe(&self) -> String {
        self.error
            .clone()
            .unwrap_or_else(|| self.path.display().to_string())
    }
}

/// Playback options beyond [`Mode`], for [`PcmPlayer::spawn_with_config`].
#[derive(Default)]
pub struct FeedConfig {
    pub recovery: Recovery,
    /// Counters of the output callback, when the opener shares them.
    pub counters: Option<Arc<audio::SinkCounters>>,
    capture: Option<SinkCapture>,
}

/// Queue + linear resampler between the producer and the ring. Pure logic
/// (no threads, no device) so it can be tested headless.
pub struct Feeder {
    ring: Arc<SpscRing>,
    /// Source samples not yet fully consumed; `pos` indexes into it.
    buf: Vec<StereoSample>,
    pos: f64,
    /// Source samples per output sample.
    step: f64,
    /// Samples (at the source rate) queued before playback may start.
    start_at: usize,
    started: bool,
    base_step: f64,
    rate: f64,
    stretch_target: Option<usize>,
}

impl Feeder {
    pub fn new(ring: Arc<SpscRing>, device_rate: u32, mode: Mode) -> Self {
        let start_at = match mode {
            Mode::Live => 0,
            Mode::Buffer(s) => (s * SOURCE_RATE as f64) as usize,
        };
        Feeder {
            ring,
            buf: Vec::new(),
            pos: 0.0,
            step: SOURCE_RATE as f64 / device_rate as f64,
            start_at,
            started: false,
            base_step: SOURCE_RATE as f64 / device_rate as f64,
            rate: 1.0,
            stretch_target: None,
        }
    }

    /// Adapt the resampling rate to keep about `target_frames` device frames
    /// queued (see [`Recovery::Stretch`]).
    pub fn with_stretch(mut self, target_frames: usize) -> Self {
        self.stretch_target = Some(target_frames.max(1));
        self
    }

    /// Playback rate relative to real time (1.0 unless stretching).
    pub fn rate(&self) -> f64 {
        self.rate
    }

    /// Queue interleaved L/R f32 samples.
    pub fn accept(&mut self, interleaved: &[f32]) {
        self.buf.extend(
            interleaved
                .chunks_exact(2)
                .map(|c| StereoSample { l: c[0], r: c[1] }),
        );
    }

    /// Source samples waiting.
    pub fn queued(&self) -> usize {
        self.buf.len()
    }

    pub fn started(&self) -> bool {
        self.started
    }

    /// True once playback may begin: the start threshold is met, or the
    /// producer is `done` and anything at all is queued.
    pub fn may_start(&self, done: bool) -> bool {
        self.started || self.buf.len() >= self.start_at.max(1) || (done && !self.buf.is_empty())
    }

    /// Move as much as fits into the ring; never blocks. Returns the number
    /// of device-rate samples pushed. Does nothing before `may_start`.
    pub fn pump(&mut self, done: bool) -> usize {
        if let Some(target) = self.stretch_target {
            let fill = self.ring.len() as f64 / target as f64;
            let want = fill.clamp(STRETCH_MIN, STRETCH_MAX);
            self.rate += (want - self.rate) * STRETCH_GAIN;
            self.step = self.base_step * self.rate;
        }
        if !self.may_start(done) {
            return 0;
        }
        self.started = true;
        let mut out = Vec::new();
        let room = self.ring.room();
        while out.len() < room {
            let i = self.pos as usize;
            if i + 1 >= self.buf.len() {
                break;
            }
            let f = (self.pos - i as f64) as f32;
            let (a, b) = (self.buf[i], self.buf[i + 1]);
            out.push(StereoSample {
                l: a.l + (b.l - a.l) * f,
                r: a.r + (b.r - a.r) * f,
            });
            self.pos += self.step;
        }
        // Drop consumed samples, keeping the one the next output starts at.
        let keep = (self.pos as usize).min(self.buf.len());
        if keep > 0 {
            self.buf.drain(..keep);
            self.pos -= keep as f64;
        }
        let n = self.ring.push_slice(&out);
        debug_assert_eq!(n, out.len());
        n
    }

    /// Everything queued has reached the ring (the last source sample stays
    /// as interpolation context).
    pub fn drained(&self) -> bool {
        self.buf.len() <= 1
    }
}

/// What a device opener returns: the device rate, a label and a keep-alive
/// (the stream; dropping it stops playback).
pub type Opened = (u32, String, Box<dyn Any>);

/// Opens the output for a given ring. Runs on the feeder thread, twice at
/// most: [`Opener::probe`] before any audio exists, [`Opener::start`] when
/// playback begins.
pub trait Opener {
    /// Device rate and name, without starting a stream.
    fn probe(&mut self) -> Result<(u32, String), String>;
    fn start(&mut self, ring: Arc<SpscRing>, wake: thread::Thread) -> Result<Opened, String>;
    /// Underrun callbacks and maximum callback size (not a rendered-frame count).
    fn counters(&self) -> (u64, u64) {
        (0, 0)
    }
    fn error(&self) -> Option<String> {
        None
    }
    /// After a stream error: clear the error and probe the current default
    /// device again (its rate and name), ready for [`Opener::start`].
    /// `None`: no recovery, so the error stays latched.
    fn reopen(&mut self) -> Option<Result<(u32, String), String>> {
        None
    }
}

/// The default cpal output device through `live_audio::audio`.
#[derive(Default)]
pub struct CpalOpener {
    pending: Option<audio::PendingDevice>,
    underruns: Arc<AtomicU64>,
    callback_max_frames: Arc<AtomicU64>,
    stream_errors: audio::StreamErrors,
    sink: audio::SinkOptions,
    rebuffer_seconds: Option<f64>,
}

impl CpalOpener {
    fn with_sink(sink: audio::SinkOptions, rebuffer_seconds: Option<f64>) -> Self {
        CpalOpener {
            sink,
            rebuffer_seconds,
            ..CpalOpener::default()
        }
    }
}

impl Opener for CpalOpener {
    fn probe(&mut self) -> Result<(u32, String), String> {
        let p = audio::default_output()?;
        let info = (p.sample_rate, p.device_name.clone());
        self.pending = Some(p);
        Ok(info)
    }
    fn start(&mut self, ring: Arc<SpscRing>, wake: thread::Thread) -> Result<Opened, String> {
        let p = self.pending.take().ok_or("device not probed")?;
        let mut sink = self.sink.clone();
        if let Some(seconds) = self.rebuffer_seconds {
            sink.underrun = audio::Underrun::Rebuffer {
                resume_frames: (seconds * f64::from(p.sample_rate)).round() as usize,
            };
        }
        let opened = p.start_with_options(
            ring,
            self.underruns.clone(),
            self.callback_max_frames.clone(),
            wake,
            self.stream_errors.clone(),
            sink,
        )?;
        Ok((
            opened.sample_rate,
            opened.device_name.clone(),
            Box::new(opened),
        ))
    }
    fn counters(&self) -> (u64, u64) {
        (
            self.underruns.load(Ordering::Relaxed),
            self.callback_max_frames.load(Ordering::Relaxed),
        )
    }
    fn error(&self) -> Option<String> {
        self.stream_errors.error()
    }
    fn reopen(&mut self) -> Option<Result<(u32, String), String>> {
        self.stream_errors = audio::StreamErrors::default();
        Some(self.probe())
    }
}

/// Feeder snapshots; no mutex or observation work runs in the audio callback.
#[derive(Clone, Debug, Default, serde::Serialize)]
pub struct PlaybackStats {
    pub stream_started: bool,
    pub source_received_frames: u64,
    pub feeder_pending_source_frames: u64,
    pub queued_device_frames: u64,
    pub underrun_events: u64,
    pub callback_max_frames: u64,
    pub error: Option<String>,
    pub recovery: &'static str,
    pub played_device_frames: u64,
    pub silent_device_frames: u64,
    pub rebuffering: bool,
    pub stretch_rate: f64,
    pub events: Vec<SinkEvent>,
    /// The capture path, or "error: ..." if the capture failed.
    pub sink_capture: Option<String>,
}

/// The audio queue as the feeder last published it (for the spike log).
#[derive(Clone, Copy, Debug, serde::Serialize)]
pub struct QueueState {
    pub queued_device_frames: u64,
    pub feeder_pending_source_frames: u64,
    pub device_rate: u32,
    pub underrun_events: u64,
    pub rebuffering: bool,
}

pub struct PcmPlayer {
    tx: Option<mpsc::Sender<Vec<f32>>>,
    worker: Option<thread::JoinHandle<()>>,
    stop: Arc<AtomicBool>,
    stats: Arc<Mutex<PlaybackStats>>,
    pub device: String,
    pub device_rate: u32,
}

impl PcmPlayer {
    /// Start playback on the default output device. `Err` (no device, no
    /// usable config) leaves nothing running.
    pub fn spawn(mode: Mode) -> Result<Self, String> {
        Self::spawn_with(CpalOpener::default, mode)
    }

    /// Like [`Self::spawn`], with an underrun policy, shared callback
    /// counters and the optional `DIGI_EMU_SINK_WAV` capture.
    pub fn spawn_with_recovery(mode: Mode, recovery: Recovery) -> Result<Self, String> {
        let counters = Arc::new(audio::SinkCounters::default());
        let capture = SinkCapture::from_env();
        let sink = audio::SinkOptions {
            underrun: audio::Underrun::Gap,
            counters: Arc::clone(&counters),
            monitor: capture.as_ref().map(|c| Arc::clone(&c.ring)),
        };
        let rebuffer = match recovery {
            Recovery::Gap => None,
            Recovery::Rebuffer(seconds) => Some(seconds),
            // Stretching keeps the ring near its target; a short refill
            // only catches what the rate control missed.
            Recovery::Stretch(_) => Some(0.05),
        };
        Self::spawn_with_config(
            move || CpalOpener::with_sink(sink, rebuffer),
            mode,
            FeedConfig {
                recovery,
                counters: Some(counters),
                capture,
            },
        )
    }

    /// `make` builds the opener on the feeder thread (device handles need
    /// not be `Send`).
    pub fn spawn_with<O, F>(make: F, mode: Mode) -> Result<Self, String>
    where
        O: Opener,
        F: FnOnce() -> O + Send + 'static,
    {
        Self::spawn_with_config(
            make,
            mode,
            FeedConfig {
                recovery: Recovery::Gap,
                ..FeedConfig::default()
            },
        )
    }

    /// [`Self::spawn_with`] plus playback options.
    pub fn spawn_with_config<O, F>(make: F, mode: Mode, config: FeedConfig) -> Result<Self, String>
    where
        O: Opener,
        F: FnOnce() -> O + Send + 'static,
    {
        let (tx, rx) = mpsc::channel::<Vec<f32>>();
        let (info_tx, info_rx) = mpsc::channel::<Result<(u32, String), String>>();
        let stop = Arc::new(AtomicBool::new(false));
        let stats = Arc::new(Mutex::new(PlaybackStats::default()));
        let worker_stop = stop.clone();
        let worker_stats = stats.clone();
        let worker = thread::Builder::new()
            .name("pcm-feeder".into())
            .spawn(move || {
                feeder_thread(make(), mode, rx, info_tx, worker_stop, worker_stats, config)
            })
            .map_err(|e| format!("spawn feeder: {e}"))?;
        match info_rx.recv() {
            Ok(Ok((device_rate, device))) => Ok(PcmPlayer {
                tx: Some(tx),
                worker: Some(worker),
                device,
                device_rate,
                stop,
                stats,
            }),
            Ok(Err(e)) => {
                let _ = worker.join();
                Err(e)
            }
            Err(_) => Err("feeder thread died".into()),
        }
    }

    /// Queue interleaved L/R samples at 48 kHz. Never blocks.
    pub fn push(&self, interleaved: &[f32]) {
        if let Some(tx) = &self.tx {
            let _ = tx.send(interleaved.to_vec());
        }
    }

    /// Snapshot feeder/device observations. Queues exclude channel messages not yet received.
    pub fn stats(&self) -> PlaybackStats {
        let mut stats = self.stats.lock().expect("playback stats mutex").clone();
        if self
            .worker
            .as_ref()
            .is_some_and(thread::JoinHandle::is_finished)
            && stats.error.is_none()
        {
            stats.error = Some("feeder thread stopped".into());
        }
        stats
    }

    /// The queue fields of [`Self::stats`] without cloning the event list.
    pub fn queue_state(&self) -> QueueState {
        let stats = self.stats.lock().expect("playback stats mutex");
        QueueState {
            queued_device_frames: stats.queued_device_frames,
            feeder_pending_source_frames: stats.feeder_pending_source_frames,
            device_rate: self.device_rate,
            underrun_events: stats.underrun_events,
            rebuffering: stats.rebuffering,
        }
    }

    /// No more audio: play what is queued (even below the buffer threshold)
    /// and return when it has been played out.
    pub fn finish(mut self) {
        self.tx = None;
        if let Some(w) = self.worker.take() {
            let _ = w.join();
        }
    }
}

/// Dropping a session cancels immediately rather than draining its queued audio.
impl Drop for PcmPlayer {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
        self.tx = None;
        if let Some(worker) = self.worker.take() {
            worker.thread().unpark();
            let _ = worker.join();
        }
    }
}

fn feeder_thread<O: Opener>(
    mut opener: O,
    mode: Mode,
    rx: mpsc::Receiver<Vec<f32>>,
    info_tx: mpsc::Sender<Result<(u32, String), String>>,
    stop: Arc<AtomicBool>,
    stats: Arc<Mutex<PlaybackStats>>,
    mut config: FeedConfig,
) {
    let (mut rate, _name) = match opener.probe() {
        Ok(i) => {
            let _ = info_tx.send(Ok(i.clone()));
            i
        }
        Err(e) => {
            let _ = info_tx.send(Err(e));
            return;
        }
    };
    // One second of device-rate audio (rounded up to a power of two).
    let mut ring = Arc::new(SpscRing::with_capacity(rate as usize));
    let new_feeder = |ring: &Arc<SpscRing>, rate: u32, recovery: Recovery| {
        let feeder = Feeder::new(ring.clone(), rate, mode);
        match recovery {
            Recovery::Stretch(seconds) => {
                feeder.with_stretch((seconds * f64::from(rate)).round() as usize)
            }
            _ => feeder,
        }
    };
    let mut feeder = new_feeder(&ring, rate, config.recovery);
    let mut stream_started_at: Option<std::time::Instant> = None;
    let mut last_underruns = 0u64;
    let mut last_waiting = false;
    let mut events: Vec<SinkEvent> = Vec::new();
    let mut stream: Option<Opened> = None;
    let mut done = false;
    let mut received = 0u64;
    let mut reopens = 0u32;
    loop {
        if stop.load(Ordering::Acquire) {
            return;
        }
        // Intake: wait briefly for audio so the loop also services the ring.
        match rx.recv_timeout(Duration::from_millis(5)) {
            Ok(v) => {
                received += (v.len() / 2) as u64;
                feeder.accept(&v);
                while let Ok(v) = rx.try_recv() {
                    if stop.load(Ordering::Acquire) {
                        return;
                    }
                    received += (v.len() / 2) as u64;
                    feeder.accept(&v);
                }
            }
            Err(mpsc::RecvTimeoutError::Timeout) => {}
            Err(mpsc::RecvTimeoutError::Disconnected) => done = true,
        }
        if stop.load(Ordering::Acquire) {
            return;
        }
        if stream.is_none() && feeder.may_start(done) {
            match opener.start(ring.clone(), thread::current()) {
                Ok(s) => {
                    stream = Some(s);
                    stream_started_at.get_or_insert_with(std::time::Instant::now);
                }
                Err(e) => {
                    stats.lock().expect("playback stats mutex").error = Some(e.clone());
                    eprintln!("pcm_play: cannot start output: {e}");
                    return;
                }
            }
        }
        feeder.pump(done);
        if stream_started_at.is_some()
            && let Some(capture) = &mut config.capture
        {
            capture.drain(rate);
        }
        if let Some(error) = opener.error() {
            // CoreAudio reports a default-output change (and a removed
            // device) as a stream error: reopen on the current default
            // device instead of staying silent for the rest of the session.
            drop(stream.take());
            reopens += 1;
            let reopened = if reopens <= MAX_REOPENS {
                opener.reopen()
            } else {
                None
            };
            match reopened {
                Some(Ok((new_rate, name))) => {
                    eprintln!("pcm_play: {error}; reopening on {name} at {new_rate} Hz");
                    if new_rate != rate {
                        rate = new_rate;
                        ring = Arc::new(SpscRing::with_capacity(rate as usize));
                        feeder = new_feeder(&ring, rate, config.recovery);
                    }
                    continue;
                }
                Some(Err(e)) => {
                    stats.lock().expect("playback stats mutex").error =
                        Some(format!("{error}; reopen failed: {e}"));
                    return;
                }
                None => {
                    stats.lock().expect("playback stats mutex").error = Some(error);
                    return;
                }
            }
        }
        let (underrun_events, callback_max_frames) = opener.counters();
        let feeder_pending_source_frames = feeder.queued().saturating_sub(1) as u64;
        let queued_device_frames = ring.len() as u64;
        let (mut played_device_frames, mut silent_device_frames, mut rebuffering) = (0, 0, false);
        if let Some(c) = &config.counters {
            played_device_frames = c.played_frames.load(Ordering::Relaxed);
            silent_device_frames = c.silent_frames.load(Ordering::Relaxed);
            rebuffering = c.waiting.load(Ordering::Relaxed);
        }
        let at_s = stream_started_at.map_or(0.0, |t| t.elapsed().as_secs_f64());
        if underrun_events > last_underruns && events.len() < SINK_EVENT_LIMIT {
            events.push(SinkEvent {
                at_s,
                kind: "underrun",
                queued_device_frames,
                feeder_pending_source_frames,
            });
        }
        if last_waiting && !rebuffering && events.len() < SINK_EVENT_LIMIT {
            events.push(SinkEvent {
                at_s,
                kind: "resume",
                queued_device_frames,
                feeder_pending_source_frames,
            });
        }
        last_underruns = underrun_events;
        last_waiting = rebuffering;
        *stats.lock().expect("playback stats mutex") = PlaybackStats {
            stream_started: stream.is_some(),
            source_received_frames: received,
            feeder_pending_source_frames,
            queued_device_frames,
            underrun_events,
            callback_max_frames,
            error: None,
            recovery: config.recovery.name(),
            played_device_frames,
            silent_device_frames,
            rebuffering,
            stretch_rate: feeder.rate(),
            events: events.clone(),
            sink_capture: config.capture.as_ref().map(SinkCapture::describe),
        };
        if done && feeder.drained() {
            break;
        }
        if done {
            // Ring full and the callback consumes it: wait for room.
            thread::park_timeout(Duration::from_millis(5));
        }
    }
    // Let the device play the ring out (a ring is at most 1 s).
    while !ring.is_empty() {
        if stop.load(Ordering::Acquire) {
            return;
        }
        if let Some(error) = opener.error() {
            stats.lock().expect("playback stats mutex").error = Some(error);
            return;
        }
        thread::sleep(Duration::from_millis(10));
    }
    for _ in 0..10 {
        if stop.load(Ordering::Acquire) {
            return;
        }
        if let Some(error) = opener.error() {
            stats.lock().expect("playback stats mutex").error = Some(error);
            return;
        }
        thread::sleep(Duration::from_millis(10));
    }
    if stream_started_at.is_some()
        && let Some(capture) = &mut config.capture
    {
        capture.drain(rate);
    }
    drop(stream);
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ramp(n: usize) -> Vec<f32> {
        (0..n).flat_map(|i| [i as f32, -(i as f32)]).collect()
    }

    fn drain(ring: &SpscRing) -> Vec<StereoSample> {
        let mut v = vec![StereoSample::default(); ring.len()];
        let n = ring.pop_into(&mut v);
        v.truncate(n);
        v
    }

    #[test]
    fn buffer_mode_waits_for_threshold_then_streams_in_order() {
        let ring = Arc::new(SpscRing::with_capacity(1024));
        let mut f = Feeder::new(ring.clone(), 48_000, Mode::Buffer(0.01)); // 480 samples
        f.accept(&ramp(400));
        assert!(!f.may_start(false));
        assert_eq!(f.pump(false), 0);
        assert!(ring.is_empty());
        f.accept(&ramp(400)[..200]);
        assert!(f.may_start(false));
        // 500 queued, the last one is interpolation context only.
        assert_eq!(f.pump(false), 499);
        let got = drain(&ring);
        assert_eq!(got.len(), 499);
        assert_eq!(got[0], StereoSample { l: 0.0, r: -0.0 });
        assert_eq!(got[10], StereoSample { l: 10.0, r: -10.0 });
    }

    #[test]
    fn never_exceeds_ring_room_and_keeps_the_rest() {
        let ring = Arc::new(SpscRing::with_capacity(64));
        let mut f = Feeder::new(ring.clone(), 48_000, Mode::Live);
        f.accept(&ramp(300));
        assert_eq!(f.pump(false), 64);
        assert_eq!(f.pump(false), 0); // full: no block, no loss
        let a = drain(&ring);
        assert_eq!(a.last().unwrap().l, 63.0);
        assert_eq!(f.pump(false), 64);
        assert_eq!(drain(&ring)[0].l, 64.0);
    }

    #[test]
    fn short_run_plays_at_the_end_and_resamples() {
        let ring = Arc::new(SpscRing::with_capacity(4096));
        let mut f = Feeder::new(ring.clone(), 24_000, Mode::Buffer(60.0));
        f.accept(&ramp(101));
        assert_eq!(f.pump(false), 0);
        assert!(f.may_start(true));
        let n = f.pump(true);
        assert_eq!(n, 50); // 48k -> 24k halves, last sample is context
        let got = drain(&ring);
        assert_eq!(got[3].l, 6.0);
        assert!(f.drained() || f.queued() <= 2);
    }

    #[test]
    fn stretch_slows_down_when_the_ring_stays_low() {
        let ring = Arc::new(SpscRing::with_capacity(4096));
        let mut feeder = Feeder::new(Arc::clone(&ring), SOURCE_RATE, Mode::Live).with_stretch(2048);
        feeder.accept(&vec![0.0f32; 2 * 200_000]);
        let mut sink = vec![StereoSample::default(); 4096];
        for _ in 0..20 {
            feeder.pump(false);
            while ring.pop_into(&mut sink) > 0 {}
        }
        assert!(feeder.rate() < 0.95, "rate {}", feeder.rate());
        assert!(feeder.rate() >= STRETCH_MIN);
    }

    #[test]
    fn stretch_never_runs_faster_than_its_cap() {
        let ring = Arc::new(SpscRing::with_capacity(4096));
        let mut feeder = Feeder::new(Arc::clone(&ring), SOURCE_RATE, Mode::Live).with_stretch(2048);
        feeder.accept(&vec![0.0f32; 2 * 200_000]);
        for _ in 0..200 {
            feeder.pump(false);
        }
        assert!(feeder.rate() > 1.0 && feeder.rate() <= STRETCH_MAX + 1e-12);
    }

    struct FakeOpener {
        rate: u32,
        started: Arc<Mutex<Option<Arc<SpscRing>>>>,
    }
    impl Opener for FakeOpener {
        fn probe(&mut self) -> Result<(u32, String), String> {
            Ok((self.rate, "fake".into()))
        }
        fn start(&mut self, ring: Arc<SpscRing>, _wake: thread::Thread) -> Result<Opened, String> {
            *self.started.lock().unwrap() = Some(ring);
            Ok((self.rate, "fake".into(), Box::new(())))
        }
    }

    struct NoDevice;
    impl Opener for NoDevice {
        fn probe(&mut self) -> Result<(u32, String), String> {
            Err("no default output device".into())
        }
        fn start(&mut self, _: Arc<SpscRing>, _: thread::Thread) -> Result<Opened, String> {
            unreachable!()
        }
    }

    #[test]
    fn player_thread_end_to_end_headless() {
        let slot = Arc::new(Mutex::new(None));
        let p = PcmPlayer::spawn_with(
            {
                let slot = slot.clone();
                move || FakeOpener {
                    rate: 48_000,
                    started: slot,
                }
            },
            Mode::Buffer(0.02), // 960 samples
        )
        .unwrap();
        assert_eq!((p.device.as_str(), p.device_rate), ("fake", 48_000));
        // 500 samples: below the threshold, the device must not start.
        p.push(&ramp(500));
        thread::sleep(Duration::from_millis(60));
        assert!(slot.lock().unwrap().is_none());
        p.push(&ramp(2000)[..1000]);
        // The fake device consumes like the callback would.
        let consumer = {
            let slot = slot.clone();
            thread::spawn(move || {
                let mut got = Vec::new();
                let t0 = std::time::Instant::now();
                while got.len() < 999 && t0.elapsed() < Duration::from_secs(5) {
                    let ring = slot.lock().unwrap().clone();
                    if let Some(r) = ring {
                        let mut b = [StereoSample::default(); 64];
                        let n = r.pop_into(&mut b);
                        got.extend_from_slice(&b[..n]);
                    }
                    thread::sleep(Duration::from_millis(1));
                }
                got
            })
        };
        p.finish();
        let got = consumer.join().unwrap();
        assert_eq!(got.len(), 999);
        // 500 ramp samples then the first 500 again: order preserved.
        assert_eq!(got[499].l, 499.0);
        assert_eq!(got[500].l, 0.0);
    }

    #[test]
    fn missing_device_fails_gracefully() {
        let r = PcmPlayer::spawn_with(|| NoDevice, Mode::Live);
        assert_eq!(r.err().as_deref(), Some("no default output device"));
    }
    #[test]
    fn drop_cancels_a_full_fake_device_and_joins_the_feeder() {
        let slot = Arc::new(Mutex::new(None));
        let player = PcmPlayer::spawn_with(
            {
                let slot = slot.clone();
                move || FakeOpener {
                    rate: 48_000,
                    started: slot,
                }
            },
            Mode::Live,
        )
        .unwrap();
        player.push(&ramp(100_000));
        let deadline = std::time::Instant::now() + Duration::from_secs(2);
        while !player.stats().stream_started && std::time::Instant::now() < deadline {
            thread::sleep(Duration::from_millis(5));
        }
        let stats = player.stats();
        assert!(stats.stream_started);
        assert_eq!(stats.source_received_frames, 100_000);
        assert!(stats.queued_device_frames > 0);
        let start = std::time::Instant::now();
        drop(player); // The fake device never consumes: draining would hang.
        assert!(start.elapsed() < Duration::from_secs(1));
        assert_eq!(
            Arc::strong_count(&slot),
            1,
            "feeder/opener must have been dropped"
        );
    }
    #[test]
    fn drop_before_buffer_threshold_never_starts_the_device() {
        let slot = Arc::new(Mutex::new(None));
        let player = PcmPlayer::spawn_with(
            {
                let slot = slot.clone();
                move || FakeOpener {
                    rate: 48_000,
                    started: slot,
                }
            },
            Mode::Buffer(2.0),
        )
        .unwrap();
        player.push(&ramp(64));
        drop(player);
        assert!(slot.lock().unwrap().is_none());
        assert_eq!(Arc::strong_count(&slot), 1);
    }
    struct BadStart;
    impl Opener for BadStart {
        fn probe(&mut self) -> Result<(u32, String), String> {
            Ok((48_000, "fake".into()))
        }
        fn start(&mut self, _: Arc<SpscRing>, _: thread::Thread) -> Result<Opened, String> {
            Err("fake start failure".into())
        }
    }
    #[test]
    fn late_device_failure_is_observable() {
        let player = PcmPlayer::spawn_with(|| BadStart, Mode::Live).unwrap();
        player.push(&ramp(64));
        let deadline = std::time::Instant::now() + Duration::from_secs(2);
        while player.stats().error.is_none() && std::time::Instant::now() < deadline {
            thread::sleep(Duration::from_millis(5));
        }
        assert_eq!(player.stats().error.as_deref(), Some("fake start failure"));
    }
    struct PostStartError {
        errors: audio::StreamErrors,
    }
    impl Opener for PostStartError {
        fn probe(&mut self) -> Result<(u32, String), String> {
            Ok((48_000, "fake".into()))
        }
        fn start(&mut self, _: Arc<SpscRing>, _: thread::Thread) -> Result<Opened, String> {
            Ok((48_000, "fake".into(), Box::new(())))
        }
        fn error(&self) -> Option<String> {
            self.errors.error()
        }
    }
    #[test]
    fn error_after_successful_start_is_latched_and_finish_does_not_hang() {
        let errors = audio::StreamErrors::default();
        let player = PcmPlayer::spawn_with(
            {
                let errors = errors.clone();
                move || PostStartError { errors }
            },
            Mode::Live,
        )
        .unwrap();
        player.push(&ramp(100_000));
        let deadline = std::time::Instant::now() + Duration::from_secs(2);
        while !player.stats().stream_started && std::time::Instant::now() < deadline {
            thread::sleep(Duration::from_millis(5));
        }
        assert!(player.stats().stream_started);
        assert!(player.stats().error.is_none());
        errors.record(); // Exactly the same signal used by the CPAL error callback.
        let deadline = std::time::Instant::now() + Duration::from_secs(2);
        while player.stats().error.is_none() && std::time::Instant::now() < deadline {
            thread::sleep(Duration::from_millis(5));
        }
        assert_eq!(
            player.stats().error.as_deref(),
            Some("output stream error (1 events; details in stderr)")
        );
        thread::sleep(Duration::from_millis(15));
        assert!(
            player.stats().error.is_some(),
            "error must remain latched after worker exits"
        );
        let start = std::time::Instant::now();
        player.finish(); // Fake stream never drains its ring; error must terminate it.
        assert!(start.elapsed() < Duration::from_secs(1));
    }
    struct ReopenAfterError {
        errors: audio::StreamErrors,
        starts: Arc<AtomicU64>,
    }
    impl Opener for ReopenAfterError {
        fn probe(&mut self) -> Result<(u32, String), String> {
            Ok((48_000, "fake".into()))
        }
        fn start(&mut self, _: Arc<SpscRing>, _: thread::Thread) -> Result<Opened, String> {
            self.starts.fetch_add(1, Ordering::AcqRel);
            Ok((48_000, "fake".into(), Box::new(())))
        }
        fn error(&self) -> Option<String> {
            self.errors.error()
        }
        fn reopen(&mut self) -> Option<Result<(u32, String), String>> {
            self.errors = audio::StreamErrors::default();
            Some(self.probe())
        }
    }
    #[test]
    fn stream_error_reopens_the_default_output() {
        let errors = audio::StreamErrors::default();
        let starts = Arc::new(AtomicU64::new(0));
        let player = PcmPlayer::spawn_with(
            {
                let (errors, starts) = (errors.clone(), starts.clone());
                move || ReopenAfterError { errors, starts }
            },
            Mode::Live,
        )
        .unwrap();
        player.push(&ramp(100_000));
        let deadline = std::time::Instant::now() + Duration::from_secs(2);
        while starts.load(Ordering::Acquire) < 1 && std::time::Instant::now() < deadline {
            thread::sleep(Duration::from_millis(5));
        }
        errors.record(); // The CPAL error callback's signal.
        player.push(&ramp(100_000));
        let deadline = std::time::Instant::now() + Duration::from_secs(2);
        while starts.load(Ordering::Acquire) < 2 && std::time::Instant::now() < deadline {
            thread::sleep(Duration::from_millis(5));
        }
        assert_eq!(starts.load(Ordering::Acquire), 2);
        assert!(player.stats().error.is_none());
        drop(player);
    }
}
