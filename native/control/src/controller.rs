//! The emulator controller: one owner thread for the (`!Send`) coupled
//! runtime, driven by commands. The desktop app's webview commands and the
//! protocol's RPC requests are two front ends to the same loop; both are
//! plain messages on one channel, so any host (desktop, daemon, a browser
//! worker with its own dispatcher) uses the same controller.
//!
//! The loop serves a command whenever one is waiting and otherwise advances
//! the guest by one chunk when a job (`step`, `run_until`, a cold boot) or the
//! free-run mode asks for it, so the UI and status requests stay live while a
//! job runs. See `docs/design/emulator-control.md`.

use std::{
    fs::File,
    io::{Read, Seek, SeekFrom},
    path::{Path, PathBuf},
    sync::{Arc, Mutex, mpsc},
    thread,
    time::Instant,
};

use digi_coupled::{
    AudioOptions, CoupledRuntime as Runtime, STEP_CHUNK,
    scenario::{self, Action, Scenario},
};
use elektron_native_boot::{Snapshot, dsp_patch::DspPatch};
use emmc_card::{Card, CardIdentity, RandomAccessRead};
use serde_json::{Value, json};

use crate::{
    events::Events,
    firmware::{self, Analysis},
    proto::{Params, RpcError, RpcResult, base64, code},
};

pub const MAX_SYX: usize = 32 * 1024 * 1024;
/// Most guest instructions a cold boot may take before it is called stuck.
const BOOT_LIMIT: u64 = 3_000_000_000;

struct FileBacking {
    file: Mutex<File>,
    len: u64,
    error: Arc<Mutex<Option<String>>>,
}
impl RandomAccessRead for FileBacking {
    fn len(&self) -> u64 {
        self.len
    }
    fn read_at(&self, offset: u64, destination: &mut [u8]) -> usize {
        let mut file = match self.file.lock() {
            Ok(file) => file,
            Err(_) => return 0,
        };
        if let Err(error) = file
            .seek(SeekFrom::Start(offset))
            .and_then(|_| file.read_exact(destination))
        {
            *self.error.lock().expect("backing error mutex") = Some(error.to_string());
            return 0;
        }
        destination.len()
    }
}
type ReadError = Arc<Mutex<Option<String>>>;

fn card(path: PathBuf) -> Result<(Card, ReadError), String> {
    let metadata = std::fs::metadata(&path).map_err(|e| format!("card image: {e}"))?;
    if !metadata.is_file() || metadata.len() % 512 != 0 {
        return Err("card image must be a regular 512-byte aligned file".into());
    }
    let identity = CardIdentity::from_env()?;
    let blocks = u32::try_from(metadata.len() / 512).map_err(|_| "card image is too large")?;
    if blocks > identity.blocks() {
        return Err(format!(
            "card image has {blocks:#x} sectors, more than the {} identity's {:#x}",
            identity.name(),
            identity.blocks()
        ));
    }
    let error = Arc::new(Mutex::new(None));
    let backing = FileBacking {
        file: Mutex::new(File::open(path).map_err(|e| format!("card image: {e}"))?),
        len: metadata.len(),
        error: error.clone(),
    };
    Ok((
        Card::with_backing(identity, Some(Box::new(backing)))
            .map_err(|e| format!("card: {e:?}"))?,
        error,
    ))
}

fn card_for(image: Option<PathBuf>) -> Result<(Option<Card>, Option<ReadError>), String> {
    match image {
        Some(path) => card(path).map(|(card, error)| (Some(card), Some(error))),
        None => Ok((None, None)),
    }
}

fn runtime_from(
    bytes: &[u8],
    image: Option<PathBuf>,
    audio: Option<&AudioOptions>,
) -> Result<(Runtime, Option<ReadError>), String> {
    let (card, error) = card_for(image)?;
    Runtime::new(bytes, card, audio).map(|runtime| (runtime, error))
}

pub type Reply = mpsc::Sender<Result<Snapshot, String>>;
pub type JsonReply = mpsc::Sender<Result<Value, String>>;
pub type RpcReply = mpsc::Sender<RpcResult>;

/// What a host sends the controller. The first group is the desktop app's
/// webview protocol (session-checked, one `Snapshot` per answer); `Rpc` is the
/// control protocol.
pub enum Command {
    Diagnostics {
        session: u64,
        reply: JsonReply,
    },
    AudioStatus {
        session: u64,
        reply: JsonReply,
    },
    Startup {
        reply: Reply,
    },
    Load {
        session: u64,
        bytes: Vec<u8>,
        card: Option<PathBuf>,
        reply: Reply,
    },
    Restart {
        session: u64,
        next_session: u64,
        reply: Reply,
    },
    Step {
        session: u64,
        budget: u32,
        reply: Reply,
    },
    Button {
        session: u64,
        code: u8,
        down: bool,
        reply: Reply,
    },
    Turn {
        session: u64,
        encoder: u8,
        detents: i32,
        reply: Reply,
    },
    Tap {
        session: u64,
        code: u8,
        hold: u32,
        reply: Reply,
    },
    Stop {
        session: u64,
        reply: Reply,
    },
    Rpc {
        method: String,
        params: Value,
        reply: RpcReply,
    },
}

/// Who advances the guest.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    /// The webview pump's `emu_step` (the desktop default).
    Ui,
    /// Nobody; `emu_step` returns the status without stepping.
    Paused,
    /// The controller, chunk after chunk, whenever it is idle.
    Free,
}

impl Mode {
    pub fn name(self) -> &'static str {
        match self {
            Mode::Ui => "ui",
            Mode::Paused => "paused",
            Mode::Free => "free",
        }
    }
    pub fn parse(text: &str) -> Option<Self> {
        match text {
            "ui" => Some(Mode::Ui),
            "paused" => Some(Mode::Paused),
            "free" => Some(Mode::Free),
            _ => None,
        }
    }
}

/// A scenario to run in the `Step` path (the desktop `--scenario` mode); the
/// controller raises `scenario.done` when it ends.
pub struct ScenarioSetup {
    pub scenario: Scenario,
}

pub fn load_scenario(spec: &str) -> Result<Scenario, String> {
    if Path::new(spec).is_file() {
        let text = std::fs::read_to_string(spec).map_err(|e| format!("scenario file: {e}"))?;
        let value: Value =
            serde_json::from_str(&text).map_err(|e| format!("scenario file: {e}"))?;
        let found = scenario::find_scenario(&value)
            .ok_or("scenario file has no scenario (need \"scenarios\" or a scenario object)")?;
        scenario::parse_scenario(found)
    } else {
        scenario::parse_scenarios(scenario::DEFAULT_SCENARIOS)?
            .into_iter()
            .find(|candidate| candidate.name == spec)
            .ok_or_else(|| format!("unknown scenario: {spec}"))
    }
}

pub struct Config {
    pub audio: Option<AudioOptions>,
    pub scenario: Option<ScenarioSetup>,
    pub mode: Mode,
    /// Where patched-firmware snapshots are cached (`<run_dir>/patched`).
    pub run_dir: PathBuf,
    pub events: Events,
    /// Called on the controller thread when `quit` arrives, after the reply.
    pub on_quit: Option<Box<dyn Fn() + Send>>,
}

impl Config {
    pub fn new(audio: Option<AudioOptions>) -> Self {
        Self {
            audio,
            scenario: None,
            mode: Mode::Ui,
            run_dir: PathBuf::from("out/native/run"),
            events: Events::default(),
            on_quit: None,
        }
    }
}

/// A cloneable way to talk to a running controller.
#[derive(Clone)]
pub struct Handle {
    sender: mpsc::Sender<Command>,
    events: Events,
}

impl Handle {
    pub fn sender(&self) -> &mpsc::Sender<Command> {
        &self.sender
    }
    pub fn events(&self) -> &Events {
        &self.events
    }
    /// Run one protocol method and wait for its result. A job method
    /// (`step`, `run_until`, a cold-boot `firmware.load`) answers when it ends.
    pub fn call(&self, method: &str, params: Value) -> RpcResult {
        let (reply, response) = mpsc::channel();
        self.sender
            .send(Command::Rpc {
                method: method.to_string(),
                params,
                reply,
            })
            .map_err(|_| RpcError::io("emulator controller stopped"))?;
        response
            .recv()
            .map_err(|_| RpcError::io("emulator controller dropped the reply"))?
    }
}

/// Start the controller thread.
pub fn spawn(config: Config) -> (Handle, thread::JoinHandle<()>) {
    let (sender, receiver) = mpsc::channel();
    let events = config.events.clone();
    let join = thread::Builder::new()
        .name("digi-controller".into())
        .spawn(move || Actor::new(receiver, config).run())
        .expect("spawn controller thread");
    (Handle { sender, events }, join)
}

/// What the controller knows about the firmware it is running.
struct Loaded {
    /// The .syx the DSP audio profile and the device profile belong to, when
    /// known (a first load of an already patched .syx has none).
    base: Option<Vec<u8>>,
    base_sha: String,
    /// The firmware as running now (the base plus injected changes).
    current: Vec<u8>,
    card: Option<PathBuf>,
    base_analysis: Option<Analysis>,
    current_analysis: Option<Analysis>,
}

impl Loaded {
    fn stock(bytes: Vec<u8>, card: Option<PathBuf>) -> Self {
        Self {
            base_sha: firmware::sha256_hex(&bytes),
            base: Some(bytes.clone()),
            current: bytes,
            card,
            base_analysis: None,
            current_analysis: None,
        }
    }
    fn ensure_analysis(&mut self) -> Result<(), String> {
        if self.current_analysis.is_none() {
            self.current_analysis = Some(Analysis::of(&self.current)?);
        }
        if self.base_analysis.is_none() {
            self.base_analysis = match &self.base {
                Some(base) if *base == self.current => self.current_analysis.clone(),
                Some(base) => Some(Analysis::of(base)?),
                None => None,
            };
        }
        Ok(())
    }
}

struct Scheduled {
    at: u64,
    action: Action,
}

enum Until {
    Elapsed(u64),
    Icount(u64),
    Frames(u64),
}

enum JobKind {
    Steps {
        end: u64,
    },
    Until(Until),
    /// A cold boot to the ready state; saves the snapshot cache when given.
    Boot {
        cache: Option<PathBuf>,
        limit: u64,
    },
}

struct Job {
    kind: JobKind,
    reply: RpcReply,
    started: Instant,
    wall_limit: Option<f64>,
    /// Fields merged into the final result (the `firmware.load` report).
    partial: Value,
    /// A scenario-style label for events.
    label: &'static str,
}

struct ScenarioRun {
    scenario: Scenario,
    next: usize,
    started: Option<Instant>,
}

struct Actor {
    rx: mpsc::Receiver<Command>,
    audio: Option<AudioOptions>,
    run_dir: PathBuf,
    events: Events,
    on_quit: Option<Box<dyn Fn() + Send>>,
    runtime: Option<Runtime>,
    /// The session id the webview protocol checks.
    session: u64,
    /// Raised whenever the runtime is replaced, whoever asked.
    epoch: u64,
    loaded: Option<Loaded>,
    read_error: Option<ReadError>,
    startup_snapshot: Option<Snapshot>,
    scenario_run: Option<ScenarioRun>,
    mode: Mode,
    job: Option<Job>,
    schedule: Vec<Scheduled>,
    /// A frame a control chunk produced that the webview has not seen yet.
    held_frame: Option<Vec<u8>>,
    quit: bool,
}

fn no_firmware<T>() -> Result<T, RpcError> {
    Err(RpcError::no_firmware())
}

#[cfg(feature = "coupled-audio")]
fn frames(runtime: &Runtime) -> u64 {
    runtime.frames_sent()
}
#[cfg(not(feature = "coupled-audio"))]
fn frames(_runtime: &Runtime) -> u64 {
    0
}

impl Actor {
    fn new(rx: mpsc::Receiver<Command>, config: Config) -> Self {
        Self {
            rx,
            audio: config.audio,
            run_dir: config.run_dir,
            events: config.events,
            on_quit: config.on_quit,
            runtime: None,
            session: 0,
            epoch: 0,
            loaded: None,
            read_error: None,
            startup_snapshot: None,
            scenario_run: config.scenario.map(|setup| ScenarioRun {
                scenario: setup.scenario,
                next: 0,
                started: None,
            }),
            mode: config.mode,
            job: None,
            schedule: Vec::new(),
            held_frame: None,
            quit: false,
        }
    }

    fn run(mut self) {
        while !self.quit {
            let busy = self.runtime.is_some() && (self.job.is_some() || self.mode == Mode::Free);
            let command = if busy {
                match self.rx.try_recv() {
                    Ok(command) => Some(command),
                    Err(mpsc::TryRecvError::Empty) => None,
                    Err(mpsc::TryRecvError::Disconnected) => break,
                }
            } else {
                match self.rx.recv() {
                    Ok(command) => Some(command),
                    Err(_) => break,
                }
            };
            match command {
                Some(command) => self.command(command),
                None => self.advance(),
            }
        }
        if let Some(job) = self.job.take() {
            let _ = job
                .reply
                .send(Err(RpcError::io("controller stopped during a job")));
        }
    }

    // ----- shared helpers -------------------------------------------------

    fn emit(&self, kind: &str, data: Value) {
        let (icount, elapsed) = self
            .runtime
            .as_ref()
            .map_or((0, 0), |r| (r.icount(), r.elapsed_instructions()));
        let mut event = json!({
            "kind": kind, "session": self.session, "epoch": self.epoch,
            "icount": icount, "elapsed": elapsed,
        });
        if let Value::Object(map) = data {
            for (key, value) in map {
                event[key] = value;
            }
        }
        self.events.emit(event);
    }

    fn envelope(&self, mut result: Value) -> Value {
        let (icount, elapsed) = self
            .runtime
            .as_ref()
            .map_or((0, 0), |r| (r.icount(), r.elapsed_instructions()));
        if !result.is_object() {
            result = json!({ "value": result });
        }
        result["session"] = self.session.into();
        result["epoch"] = self.epoch.into();
        result["icount"] = icount.into();
        result["elapsed"] = elapsed.into();
        result["mode"] = self.mode.name().into();
        result
    }

    /// Give the webview a frame a control chunk consumed.
    fn with_held(&mut self, mut snapshot: Snapshot) -> Snapshot {
        if snapshot.frame.is_none() {
            snapshot.frame = self.held_frame.take();
        } else {
            self.held_frame = None;
        }
        snapshot
    }

    fn card_error(&self) -> Option<String> {
        self.read_error
            .as_ref()
            .and_then(|error| error.lock().ok().and_then(|mut slot| slot.take()))
    }

    fn install(&mut self, runtime: Runtime, error: Option<ReadError>) {
        self.runtime = Some(runtime);
        self.read_error = error;
        self.schedule.clear();
        self.held_frame = None;
        self.epoch += 1;
    }

    // ----- webview commands (the pre-existing protocol) --------------------

    fn command(&mut self, command: Command) {
        let command = match command {
            Command::Diagnostics {
                session: token,
                reply,
            } => {
                let result = if token != self.session {
                    Err("stale emulator session".into())
                } else {
                    self.runtime
                        .as_mut()
                        .map(Runtime::diagnostics)
                        .ok_or_else(|| "No firmware selected".into())
                };
                let _ = reply.send(result);
                return;
            }
            Command::AudioStatus {
                session: token,
                reply,
            } => {
                let result = if token != self.session {
                    Err("stale emulator session".into())
                } else {
                    self.runtime
                        .as_mut()
                        .map(|runtime| runtime.audio_status().unwrap_or(Value::Null))
                        .ok_or_else(|| "No firmware selected".into())
                };
                let _ = reply.send(result);
                return;
            }
            Command::Rpc {
                method,
                params,
                reply,
            } => {
                self.rpc(&method, &params, reply);
                return;
            }
            command => command,
        };
        if !matches!(&command, Command::Load { .. } | Command::Startup { .. }) {
            self.startup_snapshot = None;
        }
        let (request_session, reply) = match &command {
            Command::Diagnostics { .. } | Command::AudioStatus { .. } | Command::Rpc { .. } => {
                unreachable!("handled above")
            }
            Command::Startup { reply } => (self.session, reply.clone()),
            Command::Load { session, reply, .. }
            | Command::Restart { session, reply, .. }
            | Command::Step { session, reply, .. }
            | Command::Button { session, reply, .. }
            | Command::Turn { session, reply, .. }
            | Command::Tap { session, reply, .. }
            | Command::Stop { session, reply } => (*session, reply.clone()),
        };
        if request_session != self.session
            && !matches!(command, Command::Load { .. } | Command::Stop { .. })
        {
            let _ = reply.send(Err("stale emulator session".into()));
            return;
        }
        let result = match command {
            Command::Diagnostics { .. } | Command::AudioStatus { .. } | Command::Rpc { .. } => {
                unreachable!("handled above")
            }
            Command::Startup { .. } => self
                .startup_snapshot
                .take()
                .map(Ok)
                .unwrap_or_else(|| self.ui_snapshot("No startup firmware selected")),
            Command::Load {
                session: next,
                bytes,
                card: image,
                ..
            } => self.ui_load(next, bytes, image),
            Command::Restart { next_session, .. } => self.ui_restart(next_session),
            Command::Step { budget, .. } => self.ui_step(budget),
            Command::Button { code, down, .. } => {
                self.ui_input(|runtime| runtime.button(code, down))
            }
            Command::Turn {
                encoder, detents, ..
            } => self.ui_input(|runtime| runtime.turn(encoder, detents)),
            Command::Tap { code, hold, .. } => self.ui_tap(code, hold),
            Command::Stop { .. } => {
                if request_session < self.session {
                    Err("stale emulator session".into())
                } else {
                    self.session = request_session;
                    let result = self.ui_snapshot("No firmware selected");
                    if result.is_ok() {
                        self.runtime = None;
                        self.loaded = None;
                        self.job = None;
                    }
                    result
                }
            }
        };
        let _ = reply.send(result);
    }

    fn ui_snapshot(&mut self, missing: &str) -> Result<Snapshot, String> {
        let snapshot = self
            .runtime
            .as_mut()
            .ok_or_else(|| missing.to_string())
            .map(Runtime::snapshot)?;
        Ok(self.with_held(snapshot))
    }

    fn ui_load(
        &mut self,
        next: u64,
        bytes: Vec<u8>,
        image: Option<PathBuf>,
    ) -> Result<Snapshot, String> {
        self.startup_snapshot = None;
        if next <= self.session {
            return Err("stale emulator session".into());
        }
        self.runtime = None;
        self.read_error = None;
        self.session = next;
        self.loaded = None;
        self.job = None;
        if bytes.len() > MAX_SYX {
            return Err("firmware image exceeds 32 MiB cache limit".into());
        }
        let (runtime, error) = runtime_from(&bytes, image.clone(), self.audio.as_ref())?;
        self.loaded = Some(Loaded::stock(bytes, image));
        self.install(runtime, error);
        let snapshot = self.runtime.as_mut().expect("new runtime").snapshot();
        self.startup_snapshot = Some(snapshot.clone());
        self.emit("session.changed", json!({"reason": "load"}));
        Ok(snapshot)
    }

    fn ui_restart(&mut self, next_session: u64) -> Result<Snapshot, String> {
        let Some(loaded) = self.loaded.as_ref() else {
            return Err("No firmware selected".into());
        };
        if next_session <= self.session {
            return Err("stale emulator session".into());
        }
        // The webview restarts from the base firmware (as it always has);
        // an injected DSP change is re-applied by `firmware.load`, not here.
        let (bytes, image) = (
            loaded
                .base
                .clone()
                .unwrap_or_else(|| loaded.current.clone()),
            loaded.card.clone(),
        );
        let (runtime, error) = runtime_from(&bytes, image, self.audio.as_ref())?;
        self.session = next_session;
        self.loaded = self.loaded.take().map(|mut l| {
            l.current = bytes;
            l.current_analysis = None;
            l
        });
        self.install(runtime, error);
        self.emit("session.changed", json!({"reason": "restart"}));
        Ok(self.runtime.as_mut().expect("restarted runtime").snapshot())
    }

    fn ui_input(
        &mut self,
        action: impl FnOnce(&mut Runtime) -> Result<(), String>,
    ) -> Result<Snapshot, String> {
        let runtime = self
            .runtime
            .as_mut()
            .ok_or_else(|| "No firmware selected".to_string())?;
        action(runtime)?;
        let snapshot = runtime.snapshot();
        Ok(self.with_held(snapshot))
    }

    fn ui_tap(&mut self, code: u8, hold: u32) -> Result<Snapshot, String> {
        let runtime = self
            .runtime
            .as_mut()
            .ok_or_else(|| "No firmware selected".to_string())?;
        runtime.button(code, true)?;
        let held = (hold.clamp(1, 100_000_000)) as u64;
        let initial = runtime.snapshot();
        let start = initial.status.icount;
        let mut status = initial.status;
        let mut frame = initial.frame;
        let result = loop {
            let remaining = held.saturating_sub(status.icount.saturating_sub(start));
            if remaining == 0 {
                break Ok(Snapshot { status, frame });
            }
            let snapshot = runtime.step_chunk(remaining.min(u64::from(STEP_CHUNK)) as u32);
            if let Some(error) = snapshot.status.error.clone() {
                break Err(error);
            }
            status = snapshot.status;
            if snapshot.frame.is_some() {
                frame = snapshot.frame;
            }
        };
        let released = runtime.button(code, false);
        match (result, released) {
            (Ok(snapshot), Ok(())) => Ok(self.with_held(snapshot)),
            (Err(error), _) | (_, Err(error)) => Err(error),
        }
    }

    fn ui_step(&mut self, budget: u32) -> Result<Snapshot, String> {
        if self.runtime.is_none() {
            return Err("No firmware selected".into());
        }
        // Something else advances the guest: report, do not step.
        if self.mode != Mode::Ui || self.job.is_some() {
            return self.ui_snapshot("No firmware selected");
        }
        let mut budget = budget;
        if self.scenario_run.is_some() {
            budget = self.scenario_tick(budget)?;
        }
        let runtime = self.runtime.as_mut().expect("runtime");
        let snapshot = runtime.step_chunk(budget.min(STEP_CHUNK));
        if let Some(error) = self.card_error() {
            Err(format!("card image read: {error}"))
        } else {
            Ok(self.with_held(snapshot))
        }
    }

    /// The desktop `--scenario` mode: fire due events, finish the run with a
    /// `scenario.done` event, and clip the chunk so exact events land exactly.
    fn scenario_tick(&mut self, mut budget: u32) -> Result<u32, String> {
        use scenario::When;
        let (Some(runtime), Some(run)) = (self.runtime.as_mut(), self.scenario_run.as_mut()) else {
            return Ok(budget);
        };
        let started = *run.started.get_or_insert_with(Instant::now);
        let elapsed = runtime.elapsed_instructions();
        while let Some(event) = run.scenario.events.get(run.next).copied() {
            let due = match event.when {
                When::Instr(n) => elapsed >= n,
                When::Frames(n) => frames(runtime) >= n,
            };
            if !due {
                break;
            }
            scenario::fire(runtime, event.action)?;
            run.next += 1;
        }
        let done = run.scenario.instructions.is_some_and(|n| elapsed >= n)
            || run
                .scenario
                .seconds
                .is_some_and(|s| frames(runtime) as f64 >= s * scenario::FRAMES_PER_SECOND);
        if done {
            let report = json!({
                "scenario": run.scenario.name,
                "wall_seconds": started.elapsed().as_secs_f64(),
                "elapsed_instructions": elapsed,
                "audio": runtime.audio_status().unwrap_or(Value::Null),
                "diagnostics": runtime.diagnostics(),
            });
            self.scenario_run = None;
            self.emit("scenario.done", json!({"report": report}));
            return Ok(0);
        }
        if run.scenario.exact_events
            && let Some(event) = run.scenario.events.get(run.next)
            && let When::Instr(n) = event.when
            && n > elapsed
        {
            budget = budget.min(u32::try_from(n - elapsed).unwrap_or(u32::MAX));
        }
        Ok(budget)
    }

    // ----- jobs and free running ------------------------------------------

    fn fire_due(&mut self) -> Result<(), RpcError> {
        let Some(runtime) = self.runtime.as_mut() else {
            return Ok(());
        };
        let elapsed = runtime.elapsed_instructions();
        while self.schedule.first().is_some_and(|s| s.at <= elapsed) {
            let due = self.schedule.remove(0);
            scenario::fire(runtime, due.action).map_err(RpcError::bad_params)?;
        }
        Ok(())
    }

    /// One chunk of the active job, or of free running.
    fn advance(&mut self) {
        if let Err(error) = self.fire_due() {
            self.finish_job(Err(error));
            return;
        }
        if let Some(done) = self.job_progress() {
            self.finish_job(done);
            return;
        }
        let Some(runtime) = self.runtime.as_mut() else {
            return;
        };
        let (icount, elapsed) = (runtime.icount(), runtime.elapsed_instructions());
        let mut budget = u64::from(STEP_CHUNK);
        if let Some(job) = &self.job {
            budget = budget.min(match &job.kind {
                JobKind::Steps { end } => end.saturating_sub(icount),
                JobKind::Until(Until::Elapsed(n)) => n.saturating_sub(elapsed),
                JobKind::Until(Until::Icount(n)) => n.saturating_sub(icount),
                JobKind::Until(Until::Frames(_)) => budget,
                JobKind::Boot { limit, .. } => limit.saturating_sub(icount),
            });
        }
        if let Some(next) = self.schedule.first() {
            budget = budget.min(next.at.saturating_sub(elapsed));
        }
        let snapshot = runtime.step_chunk(budget.max(1) as u32);
        if snapshot.frame.is_some() {
            self.held_frame = snapshot.frame.clone();
        }
        if let Some(error) = snapshot.status.error.clone() {
            self.finish_job(Err(RpcError::new(code::GUEST_FAULT, error)));
            self.mode = Mode::Paused;
            return;
        }
        if let Some(error) = self.card_error() {
            self.finish_job(Err(RpcError::io(format!("card image read: {error}"))));
            self.mode = Mode::Paused;
        }
    }

    /// Whether the active job is finished before another chunk.
    fn job_progress(&mut self) -> Option<RpcResult> {
        let job = self.job.as_ref()?;
        let runtime = self.runtime.as_mut()?;
        let (icount, elapsed) = (runtime.icount(), runtime.elapsed_instructions());
        if let Some(limit) = job.wall_limit
            && job.started.elapsed().as_secs_f64() > limit
        {
            return Some(Err(RpcError::new(code::TIMEOUT, "wall-clock timeout")));
        }
        match &job.kind {
            JobKind::Steps { end } => (icount >= *end).then(|| Ok(json!({}))),
            JobKind::Until(Until::Elapsed(n)) => (elapsed >= *n).then(|| Ok(json!({}))),
            JobKind::Until(Until::Icount(n)) => (icount >= *n).then(|| Ok(json!({}))),
            JobKind::Until(Until::Frames(n)) => (frames(runtime) >= *n).then(|| Ok(json!({}))),
            JobKind::Boot { cache, limit } => {
                if runtime.status().ready {
                    let saved = match cache {
                        Some(path) => save_cache(runtime, path),
                        None => Ok(None),
                    };
                    runtime.rebase_clock();
                    Some(saved.map(|cache| json!({"booted": true, "cache_saved": cache})))
                } else if icount >= *limit {
                    Some(Err(RpcError::new(
                        code::TIMEOUT,
                        format!("not ready after {limit} instructions"),
                    )))
                } else {
                    None
                }
            }
        }
    }

    fn finish_job(&mut self, result: RpcResult) {
        let Some(job) = self.job.take() else {
            return;
        };
        let seconds = job.started.elapsed().as_secs_f64();
        let label = job.label;
        let result = result.map(|mut value| {
            if let (Value::Object(into), Value::Object(from)) = (&mut value, &job.partial) {
                for (key, v) in from {
                    into.entry(key.clone()).or_insert_with(|| v.clone());
                }
            }
            if value.get("seconds").is_none() || label == "firmware.load" {
                value["seconds"] = seconds.into();
            }
            self.envelope(value)
        });
        let result = result.map_err(|mut error| {
            let status = self.envelope(json!({}));
            error.data = Some(error.data.take().unwrap_or(status));
            error
        });
        if label == "firmware.load" {
            let ok = result.is_ok();
            self.emit("firmware.loaded", json!({"ok": ok, "seconds": seconds}));
        }
        let _ = job.reply.send(result);
    }

    // ----- the control protocol --------------------------------------------

    fn rpc(&mut self, method: &str, params: &Value, reply: RpcReply) {
        let p = Params(params);
        let outcome: Result<Option<Value>, RpcError> = match method {
            "status" => self.rpc_status().map(Some),
            "run.mode" => self.rpc_run_mode(&p).map(Some),
            "step" => self.rpc_step(&p, &reply),
            "run_until" => self.rpc_run_until(&p, &reply),
            "firmware.load" => self.rpc_firmware_load(&p, &reply),
            "restart" => self.rpc_restart(&reply),
            "input.button" | "input.turn" => self.rpc_input(method, &p).map(Some),
            "input.tap" => self.rpc_tap(&p).map(Some),
            "input.schedule" => self.rpc_schedule(&p).map(Some),
            "inputs.recorded" => self.rpc_recorded().map(Some),
            "framebuffer.get" => self.rpc_framebuffer().map(Some),
            "diagnostics" => self.with_runtime(|r| Ok(r.diagnostics())).map(Some),
            "audio.report" => self
                .with_runtime(|r| Ok(r.audio_status().unwrap_or(Value::Null)))
                .map(|audio| Some(json!({ "audio": audio }))),
            "state.digest" => self
                .with_runtime(|r| {
                    r.state_digest()
                        .map(|digest| json!({"digest": digest}))
                        .map_err(|e| RpcError::new(code::GUEST_FAULT, e))
                })
                .map(Some),
            "pcm.tap" => self.rpc_pcm_tap(&p).map(Some),
            "cf.profile" => self
                .with_runtime(|r| {
                    #[cfg(feature = "cf-profile")]
                    {
                        let mut v = r.cf_profile();
                        v["controller_cpu_ns"] =
                            elektron_native_boot::thread_cpu_ns().into();
                        Ok(v)
                    }
                    #[cfg(not(feature = "cf-profile"))]
                    {
                        let _ = r;
                        Err(RpcError::new(code::METHOD_NOT_FOUND, "built without cf-profile"))
                    }
                })
                .map(Some),
            "dsp.patch" => self.rpc_dsp_patch(&p).map(Some),
            "quit" => {
                self.quit = true;
                self.runtime = None;
                Ok(Some(json!({"quitting": true})))
            }
            other => Err(RpcError::new(
                code::METHOD_NOT_FOUND,
                format!("unknown method {other}"),
            )),
        };
        match outcome {
            Ok(Some(value)) => {
                let _ = reply.send(Ok(self.envelope(value)));
            }
            Ok(None) => {} // a job now holds the reply
            Err(error) => {
                let _ = reply.send(Err(error));
            }
        }
        if self.quit
            && let Some(hook) = self.on_quit.take()
        {
            hook();
        }
    }

    fn with_runtime<T>(
        &mut self,
        action: impl FnOnce(&mut Runtime) -> Result<T, RpcError>,
    ) -> Result<T, RpcError> {
        match self.runtime.as_mut() {
            Some(runtime) => action(runtime),
            None => no_firmware(),
        }
    }

    fn start_job(
        &mut self,
        kind: JobKind,
        reply: &RpcReply,
        wall_limit: Option<f64>,
        partial: Value,
        label: &'static str,
    ) -> Result<(), RpcError> {
        if self.job.is_some() {
            return Err(RpcError::new(code::BUSY, "another job is running"));
        }
        self.job = Some(Job {
            kind,
            reply: reply.clone(),
            started: Instant::now(),
            wall_limit,
            partial,
            label,
        });
        Ok(())
    }

    fn rpc_status(&mut self) -> RpcResult {
        let loaded = self.loaded.as_ref().map(|l| {
            json!({
                "base_sha256": l.base_sha,
                "syx_sha256": firmware::sha256_hex(&l.current),
                "injected": l.base.as_ref() != Some(&l.current),
            })
        });
        let job = self
            .job
            .as_ref()
            .map(|j| json!({"kind": j.label, "wall_seconds": j.started.elapsed().as_secs_f64()}));
        let scheduled = self.schedule.len();
        let Some(runtime) = self.runtime.as_mut() else {
            return Ok(json!({"firmware": null, "job": job}));
        };
        let cf = serde_json::to_value(runtime.status()).unwrap_or(Value::Null);
        let audio = summarize_audio(runtime);
        let frame = runtime.current_frame().map(|(revision, _)| revision);
        Ok(json!({
            "controller_cpu_ns": elektron_native_boot::thread_cpu_ns(),
            "firmware": loaded, "cf": cf, "audio": audio, "job": job,
            "scheduled_inputs": scheduled, "frame_revision": frame,
            "dsp_frames": frames(runtime),
        }))
    }

    fn rpc_run_mode(&mut self, p: &Params) -> RpcResult {
        let mode = p.str("mode")?;
        let mode =
            Mode::parse(mode).ok_or_else(|| RpcError::bad_params("mode is ui, free or paused"))?;
        let previous = std::mem::replace(&mut self.mode, mode);
        self.emit("mode", json!({"mode": mode.name()}));
        Ok(json!({"previous": previous.name()}))
    }

    fn rpc_step(&mut self, p: &Params, reply: &RpcReply) -> Result<Option<Value>, RpcError> {
        let n = p.u64("instructions")?;
        let icount = self.with_runtime(|r| Ok(r.icount()))?;
        if n == 0 {
            return Ok(Some(json!({"stepped": 0})));
        }
        let wall = p.opt_f64("timeout_wall_s")?;
        self.start_job(
            JobKind::Steps { end: icount + n },
            reply,
            wall,
            json!({"requested": n}),
            "step",
        )?;
        Ok(None)
    }

    fn rpc_run_until(&mut self, p: &Params, reply: &RpcReply) -> Result<Option<Value>, RpcError> {
        let (icount, elapsed) =
            self.with_runtime(|r| Ok((r.icount(), r.elapsed_instructions())))?;
        let until = if let Some(n) = p.opt_u64("elapsed")? {
            Until::Elapsed(n)
        } else if let Some(n) = p.opt_u64("icount")? {
            Until::Icount(n)
        } else if let Some(n) = p.opt_u64("frames")? {
            Until::Frames(n)
        } else if let Some(s) = p.opt_f64("audio_s")? {
            Until::Frames((s * scenario::FRAMES_PER_SECOND).round() as u64)
        } else {
            return Err(RpcError::bad_params(
                "run_until needs one of elapsed, icount, frames, audio_s",
            ));
        };
        let _ = (icount, elapsed);
        if matches!(until, Until::Frames(_))
            && !self.runtime.as_ref().is_some_and(Runtime::has_audio)
        {
            return Err(RpcError::new(
                code::AUDIO_UNAVAILABLE,
                "frames and audio_s need a DSP audio session",
            ));
        }
        let wall = p.opt_f64("timeout_wall_s")?;
        self.start_job(JobKind::Until(until), reply, wall, json!({}), "run_until")?;
        Ok(None)
    }

    fn rpc_input(&mut self, method: &str, p: &Params) -> RpcResult {
        let action = if method == "input.button" {
            Action::Button {
                code: p.u8("code")?,
                down: p.opt_bool("down")?.unwrap_or(true),
            }
        } else {
            Action::Turn {
                encoder: p.u8("encoder")?,
                detents: i32::try_from(p.opt_i64("detents")?.unwrap_or(1))
                    .map_err(|_| RpcError::bad_params("detents out of range"))?,
            }
        };
        let at = p.opt_u64("at_instr")?;
        self.place_input(at, action)
            .map(|at| json!({"scheduled_at": at}))
    }

    /// Fire now (no `at`, or already due) or queue for `at`; the instruction
    /// count the input lands at.
    fn place_input(&mut self, at: Option<u64>, action: Action) -> Result<u64, RpcError> {
        let runtime = self.runtime.as_mut().ok_or_else(RpcError::no_firmware)?;
        let elapsed = runtime.elapsed_instructions();
        match at {
            None => {
                scenario::fire(runtime, action).map_err(RpcError::bad_params)?;
                Ok(elapsed)
            }
            Some(at) if at < elapsed => Err(RpcError::bad_params(format!(
                "at_instr {at} is in the past (elapsed {elapsed})"
            ))),
            Some(at) if at == elapsed => {
                scenario::fire(runtime, action).map_err(RpcError::bad_params)?;
                Ok(at)
            }
            Some(at) => {
                let index = self.schedule.partition_point(|s| s.at <= at);
                self.schedule.insert(index, Scheduled { at, action });
                Ok(at)
            }
        }
    }

    fn rpc_tap(&mut self, p: &Params) -> RpcResult {
        let code = p.u8("code")?;
        let hold = p
            .opt_u64("hold")?
            .unwrap_or(40_000_000)
            .clamp(1, 100_000_000);
        let at = p.opt_u64("at_instr")?;
        let down = self.place_input(at, Action::Button { code, down: true })?;
        let up = self.place_input(Some(down + hold), Action::Button { code, down: false })?;
        Ok(json!({"scheduled_at": down, "release_at": up}))
    }

    fn rpc_schedule(&mut self, p: &Params) -> RpcResult {
        let events = p
            .get("events")
            .and_then(Value::as_array)
            .ok_or_else(|| RpcError::bad_params("events must be an array"))?;
        let parsed = scenario::parse_scenario(&json!({
            "name": "schedule", "instructions": 1, "events": events,
        }))
        .map_err(RpcError::bad_params)?;
        let mut placed = 0;
        for event in parsed.events {
            match event.when {
                scenario::When::Instr(at) => {
                    self.place_input(Some(at), event.action)?;
                    placed += 1;
                }
                scenario::When::Frames(_) => {
                    return Err(RpcError::bad_params("input.schedule takes at_instr events"));
                }
            }
        }
        Ok(json!({"placed": placed}))
    }

    fn rpc_recorded(&mut self) -> RpcResult {
        self.with_runtime(|r| Ok(json!({"scenario": r.recorded_scenario()})))
    }

    fn rpc_framebuffer(&mut self) -> RpcResult {
        self.with_runtime(|r| {
            let (revision, bytes) = r
                .current_frame()
                .map(|(revision, bytes)| (revision, bytes.to_vec()))
                .ok_or_else(|| RpcError::new(code::GUEST_FAULT, "no frame yet"))?;
            Ok(json!({
                "revision": revision, "width": 128, "height": 64,
                "format": "oled-1bpp-column-major",
                "note": "byte (7 - y/8) + 8*x, bit y%8",
                "data_b64": base64(&bytes),
            }))
        })
    }

    fn rpc_pcm_tap(&mut self, p: &Params) -> RpcResult {
        #[cfg(feature = "coupled-audio")]
        {
            let action = p.opt_str("action")?.unwrap_or("report");
            self.with_runtime(|r| {
                if !r.has_audio() {
                    return Err(RpcError::new(
                        code::AUDIO_UNAVAILABLE,
                        "no DSP audio session",
                    ));
                }
                match action {
                    "start" => {
                        r.observe_pcm(false);
                        Ok(json!({"started": true}))
                    }
                    "report" => {
                        r.sync_audio();
                        let tap = r
                            .pcm_tap()
                            .ok_or_else(|| RpcError::bad_params("pcm.tap was not started"))?;
                        Ok(json!({
                            "samples": tap.count, "seconds": tap.seconds(),
                            "rms": tap.rms(), "peak": tap.peak,
                            "nonzero": tap.nonzero, "non_finite": tap.non_finite,
                            "sha256": tap.sha256_hex(),
                            "window_rms": tap.window_rms,
                        }))
                    }
                    _ => Err(RpcError::bad_params("action is start or report")),
                }
            })
        }
        #[cfg(not(feature = "coupled-audio"))]
        {
            let _ = p;
            Err(RpcError::new(
                code::AUDIO_UNAVAILABLE,
                "this build has no coupled audio",
            ))
        }
    }

    fn rpc_dsp_patch(&mut self, p: &Params) -> RpcResult {
        let text = match (p.opt_str("text")?, p.opt_str("file")?) {
            (Some(text), _) => text.to_string(),
            (None, Some(file)) => {
                std::fs::read_to_string(file).map_err(|e| RpcError::io(format!("{file}: {e}")))?
            }
            _ => return Err(RpcError::bad_params("dsp.patch needs text or file")),
        };
        let patch = DspPatch::parse(&text).map_err(RpcError::bad_params)?;
        if let (Some(base), Some(loaded)) = (&patch.base_sha256, &self.loaded)
            && *base != loaded.base_sha
            && !base.bytes().all(|b| b == b'0')
        {
            return Err(RpcError::new(
                code::FIRMWARE_REJECTED,
                "the patch's BASE is not the running firmware",
            ));
        }
        let started = Instant::now();
        let injected = self.with_runtime(|r| {
            r.inject_dsp_patch(patch)
                .map_err(|e| RpcError::new(code::AUDIO_UNAVAILABLE, e))
        })?;
        Ok(json!({"injected": injected, "seconds": started.elapsed().as_secs_f64()}))
    }

    fn rpc_restart(&mut self, reply: &RpcReply) -> Result<Option<Value>, RpcError> {
        let Some(loaded) = self.loaded.as_ref() else {
            return no_firmware();
        };
        let params = json!({
            "bytes": true, "force": true, "start": "auto",
            "card": loaded.card.as_ref().map(|c| c.display().to_string()),
        });
        let current = loaded.current.clone();
        self.firmware_load(&Params(&params), current, reply)
    }

    fn rpc_firmware_load(
        &mut self,
        p: &Params,
        reply: &RpcReply,
    ) -> Result<Option<Value>, RpcError> {
        let path = p.str("syx")?;
        let bytes = std::fs::read(path).map_err(|e| RpcError::io(format!("{path}: {e}")))?;
        self.firmware_load(p, bytes, reply)
    }

    /// `firmware.load`: decide how to get from the running firmware to `bytes`
    /// (see the design's "Firmware injection"), do it, and report.
    fn firmware_load(
        &mut self,
        p: &Params,
        bytes: Vec<u8>,
        reply: &RpcReply,
    ) -> Result<Option<Value>, RpcError> {
        let rejected = |m: String| RpcError::new(code::FIRMWARE_REJECTED, m);
        if self.job.is_some() {
            return Err(RpcError::new(
                code::BUSY,
                "a job is running; wait for it before loading firmware",
            ));
        }
        if bytes.len() > MAX_SYX {
            return Err(rejected("firmware image exceeds 32 MiB".into()));
        }
        let started = Instant::now();
        let start = p.opt_str("start")?.unwrap_or("auto");
        if !matches!(start, "auto" | "snapshot" | "cold") {
            return Err(RpcError::bad_params("start is auto, snapshot or cold"));
        }
        let force = p.opt_bool("force")?.unwrap_or(false);
        let base_param = p.opt_str("base_sha256")?.map(str::to_ascii_lowercase);
        let card_param = p.opt_str("card")?.map(PathBuf::from);
        let patch = match p.opt_str("dsp_patch")? {
            Some(file) => {
                let text = std::fs::read_to_string(file)
                    .map_err(|e| RpcError::io(format!("{file}: {e}")))?;
                Some(DspPatch::parse(&text).map_err(RpcError::bad_params)?)
            }
            None => None,
        };
        let new = Analysis::of(&bytes).map_err(&rejected)?;
        if let Some(patch) = &patch
            && let Some(base) = &patch.base_sha256
            && *base != new.sha256
            && !base.bytes().all(|b| b == b'0')
        {
            return Err(rejected(format!(
                "dsp_patch BASE {base} is not this .syx ({})",
                new.sha256
            )));
        }
        let mut report = json!({
            "syx_sha256": new.sha256, "start_requested": start,
            "main_changed": false, "sections_changed": [],
            "dsp": null, "audio": false, "cache_hit": false, "snapshot_ok": false,
        });

        // Running firmware to compare with.
        if self.loaded.is_some() {
            let loaded = self.loaded.as_mut().expect("loaded");
            loaded.ensure_analysis().map_err(&rejected)?;
        }
        let have_runtime = self.runtime.is_some() && self.loaded.is_some();
        if let (Some(base), Some(loaded)) = (&base_param, &self.loaded)
            && have_runtime
            && *base != loaded.base_sha
        {
            return Err(rejected(format!(
                "base_sha256 {base} is not the running firmware's base {}",
                loaded.base_sha
            )));
        }

        let job_started = if !have_runtime {
            // First load: stock when the file is a known one (or base_sha256
            // names itself), otherwise a patched build of `base_sha256`.
            let base_sha = base_param.clone().unwrap_or_else(|| new.sha256.clone());
            let card = card_param.clone();
            if base_sha == new.sha256 {
                self.load_stock(bytes, card, new, &mut report, start)?
            } else {
                self.load_patched(bytes, card, new, base_sha, None, &mut report, start, reply)?
            }
        } else {
            let loaded = self.loaded.as_ref().expect("loaded");
            let running = loaded.current_analysis.clone().expect("analysis");
            let card = card_param.or_else(|| loaded.card.clone());
            let base_analysis = loaded.base_analysis.clone();
            let base_bytes = loaded.base.clone();
            let base_sha = loaded.base_sha.clone();
            let changed = running.changed_sections(&new);
            report["sections_changed"] = json!(changed);
            let main_changed = changed.contains(&firmware::SECTION_MAIN);
            report["main_changed"] = main_changed.into();
            if !main_changed && !force {
                self.load_hot(bytes, new, running, &mut report)?
            } else {
                let stock_main = base_analysis
                    .as_ref()
                    .is_some_and(|b| b.main_sha256() == new.main_sha256());
                match base_bytes {
                    Some(base_bytes) if stock_main => self.load_stock_over(
                        base_bytes,
                        bytes,
                        card,
                        new,
                        base_analysis,
                        &mut report,
                        start,
                    )?,
                    base_bytes => self.load_patched(
                        bytes,
                        card,
                        new,
                        base_sha,
                        base_bytes,
                        &mut report,
                        start,
                        reply,
                    )?,
                }
            }
        };
        // A patch file on top of the .syx.
        if let Some(patch) = patch
            && !job_started
        {
            let ops = patch.ops.len();
            let injected = self
                .runtime
                .as_mut()
                .ok_or_else(RpcError::no_firmware)?
                .inject_dsp_patch(patch)
                .map_err(|e| RpcError::new(code::AUDIO_UNAVAILABLE, e))?;
            report["dsp_patch_file"] = json!({"ops": ops, "injected": injected});
            if report["start_used"] == "none" {
                report["start_used"] = "hot-dsp".into();
            }
        }
        if job_started {
            // The cold boot answers when it is ready.
            return Ok(None);
        }
        report["seconds"] = started.elapsed().as_secs_f64().into();
        self.emit(
            "firmware.loaded",
            json!({"ok": true, "start_used": report["start_used"]}),
        );
        Ok(Some(report))
    }

    /// First load of a known firmware, as the webview does it.
    fn load_stock(
        &mut self,
        bytes: Vec<u8>,
        card: Option<PathBuf>,
        new: Analysis,
        report: &mut Value,
        start: &str,
    ) -> Result<bool, RpcError> {
        if start == "cold" {
            return Err(RpcError::bad_params(
                "start cold needs a patched MAIN; a stock firmware restores its snapshot",
            ));
        }
        let (runtime, error) = runtime_from(&bytes, card.clone(), self.audio.as_ref())
            .map_err(|e| RpcError::new(code::FIRMWARE_REJECTED, e))?;
        report["audio"] = runtime.has_audio().into();
        report["snapshot_ok"] = runtime.has_audio().into();
        report["start_used"] = if runtime.has_audio() {
            "snapshot"
        } else {
            "cold"
        }
        .into();
        let mut loaded = Loaded::stock(bytes, card);
        loaded.base_analysis = Some(new.clone());
        loaded.current_analysis = Some(new);
        self.loaded = Some(loaded);
        self.install(runtime, error);
        self.session = self.session.max(1);
        self.emit("session.changed", json!({"reason": "firmware.load"}));
        Ok(false)
    }

    /// The running firmware has the same MAIN as `new` or `new`'s MAIN is the
    /// base's: rebuild from the base (snapshot restore with audio) and
    /// re-apply the DSP difference.
    fn load_stock_over(
        &mut self,
        base_bytes: Vec<u8>,
        bytes: Vec<u8>,
        card: Option<PathBuf>,
        new: Analysis,
        base_analysis: Option<Analysis>,
        report: &mut Value,
        start: &str,
    ) -> Result<bool, RpcError> {
        if start == "cold" {
            return Err(RpcError::bad_params(
                "start cold is for a patched MAIN; this MAIN is the base's",
            ));
        }
        let (runtime, error) = runtime_from(&base_bytes, card.clone(), self.audio.as_ref())
            .map_err(|e| RpcError::new(code::FIRMWARE_REJECTED, e))?;
        report["audio"] = runtime.has_audio().into();
        report["snapshot_ok"] = runtime.has_audio().into();
        report["start_used"] = if runtime.has_audio() {
            "snapshot"
        } else {
            "cold"
        }
        .into();
        let base_sha = firmware::sha256_hex(&base_bytes);
        self.loaded = Some(Loaded {
            base: Some(base_bytes),
            base_sha,
            current: bytes.clone(),
            card,
            base_analysis: base_analysis.clone(),
            current_analysis: Some(new.clone()),
        });
        self.install(runtime, error);
        self.emit("session.changed", json!({"reason": "firmware.load"}));
        // DSP words that differ from the base.
        if let (Some(base), Some(new_dsp)) = (base_analysis.and_then(|b| b.dsp), new.dsp)
            && base != new_dsp
        {
            let diff = firmware::dsp_diff(&base, &new_dsp)
                .map_err(|e| RpcError::new(code::FIRMWARE_REJECTED, e))?;
            report["dsp"] = self.inject_diff(diff)?;
        }
        Ok(false)
    }

    /// MAIN is the same: no restart; hot-inject the DSP words that changed.
    fn load_hot(
        &mut self,
        bytes: Vec<u8>,
        new: Analysis,
        running: Analysis,
        report: &mut Value,
    ) -> Result<bool, RpcError> {
        let dsp_changed = running
            .changed_sections(&new)
            .contains(&firmware::SECTION_DSP);
        report["start_used"] = "none".into();
        if dsp_changed {
            let has_audio = self.runtime.as_ref().is_some_and(Runtime::has_audio);
            report["audio"] = has_audio.into();
            match (has_audio, &running.dsp, &new.dsp) {
                (true, Some(old), Some(new_dsp)) => {
                    let diff = firmware::dsp_diff(old, new_dsp)
                        .map_err(|e| RpcError::new(code::FIRMWARE_REJECTED, e))?;
                    report["dsp"] = self.inject_diff(diff)?;
                    report["start_used"] = "hot-dsp".into();
                }
                (false, ..) => {
                    report["dsp"] = json!({"applied": false, "reason": "no DSP audio session"});
                }
                _ => {
                    return Err(RpcError::new(
                        code::FIRMWARE_REJECTED,
                        "the firmware has no DSP section to diff",
                    ));
                }
            }
        } else {
            report["audio"] = self.runtime.as_ref().is_some_and(Runtime::has_audio).into();
        }
        if let Some(loaded) = self.loaded.as_mut() {
            loaded.current = bytes;
            loaded.current_analysis = Some(new);
        }
        Ok(false)
    }

    fn inject_diff(&mut self, diff: firmware::DspDiff) -> Result<Value, RpcError> {
        let (changed, dropped, ranges) = (diff.changed_bytes, diff.dropped_bytes, diff.ranges);
        let injected = if diff.patch.ops.is_empty() {
            json!({"ops": 0})
        } else {
            self.runtime
                .as_mut()
                .ok_or_else(RpcError::no_firmware)?
                .inject_dsp_patch(diff.patch)
                .map_err(|e| RpcError::new(code::AUDIO_UNAVAILABLE, e))?
        };
        Ok(json!({
            "applied": true, "changed_bytes": changed, "ranges": ranges,
            "dropped_bytes": dropped, "injected": injected,
        }))
    }

    /// A MAIN the profile does not know: boot the patched build of
    /// `base_sha` (ColdFire only), from the snapshot cache when there is one
    /// and otherwise cold, saving the cache once it is ready.
    #[allow(clippy::too_many_arguments)]
    fn load_patched(
        &mut self,
        bytes: Vec<u8>,
        card: Option<PathBuf>,
        new: Analysis,
        base_sha: String,
        base_bytes: Option<Vec<u8>>,
        report: &mut Value,
        start: &str,
        reply: &RpcReply,
    ) -> Result<bool, RpcError> {
        let rejected = |m: String| RpcError::new(code::FIRMWARE_REJECTED, m);
        let main_sha = new
            .main_sha256()
            .ok_or_else(|| rejected("the firmware has no MAIN section".into()))?
            .to_string();
        let cache = self
            .run_dir
            .join("patched")
            .join(format!("{main_sha}.snap"));
        let (card_obj, error) = card_for(card.clone()).map_err(&rejected)?;
        let mut runtime = Runtime::new_patched(&bytes, card_obj, &base_sha).map_err(&rejected)?;
        let cache_hit = start != "cold" && cache.is_file();
        report["audio"] = false.into();
        report["dsp"] = json!({
            "applied": false,
            "reason": "a patched MAIN boots the ColdFire side only; DSP state cannot be rebuilt",
        });
        let mut boot_job = false;
        if cache_hit {
            match std::fs::read(&cache)
                .map_err(|e| e.to_string())
                .and_then(|data| runtime.restore_state(&data))
            {
                Ok(()) => {
                    report["start_used"] = "snapshot-cache".into();
                    report["cache_hit"] = true.into();
                    report["snapshot_ok"] = true.into();
                }
                Err(e) => {
                    if start == "snapshot" {
                        return Err(rejected(format!("cached snapshot unusable: {e}")));
                    }
                    // A fresh emulator for the cold start: a failed restore
                    // may have changed the state.
                    let (card_obj, new_error) = card_for(card.clone()).map_err(&rejected)?;
                    runtime =
                        Runtime::new_patched(&bytes, card_obj, &base_sha).map_err(&rejected)?;
                    report["cache_note"] = format!("cached snapshot unusable: {e}").into();
                    boot_job = true;
                    let _ = new_error;
                }
            }
        } else if start == "snapshot" {
            return Err(rejected(format!(
                "no cached snapshot for this MAIN ({}); start snapshot never boots cold",
                cache.display()
            )));
        } else {
            boot_job = true;
        }
        if boot_job {
            report["start_used"] = "cold".into();
        }
        self.loaded = Some(Loaded {
            base: base_bytes,
            base_sha,
            current: bytes,
            card,
            base_analysis: None,
            current_analysis: Some(new),
        });
        self.install(runtime, error);
        self.session = self.session.max(1);
        self.emit("session.changed", json!({"reason": "firmware.load"}));
        if boot_job {
            let partial = report.clone();
            self.start_job(
                JobKind::Boot {
                    cache: Some(cache),
                    limit: BOOT_LIMIT,
                },
                reply,
                None,
                partial,
                "firmware.load",
            )?;
            return Ok(true);
        }
        Ok(false)
    }
}

/// Save a ready runtime's ColdFire snapshot; the path on success.
fn save_cache(runtime: &mut Runtime, path: &Path) -> Result<Option<String>, RpcError> {
    let data = runtime
        .save_state()
        .map_err(|e| RpcError::new(code::GUEST_FAULT, format!("snapshot: {e}")))?;
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| RpcError::io(format!("{parent:?}: {e}")))?;
    }
    let partial = path.with_extension("snap.part");
    std::fs::write(&partial, data).map_err(|e| RpcError::io(format!("{partial:?}: {e}")))?;
    std::fs::rename(&partial, path).map_err(|e| RpcError::io(format!("{path:?}: {e}")))?;
    Ok(Some(path.display().to_string()))
}

/// The few audio fields a status poll wants (never syncs the DSP).
fn summarize_audio(runtime: &mut Runtime) -> Value {
    let Some(report) = runtime.audio_status() else {
        return Value::Null;
    };
    json!({
        "frames": report["frames"], "missing_blocks": report["missing_blocks"],
        "halted": report["halted"], "sink_connected": report["sink_connected"],
        "source_audio_seconds": report["source_audio_seconds"],
        "dsp_tiers": report["dsp_tiers"],
    })
}
