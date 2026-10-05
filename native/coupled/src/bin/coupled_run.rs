//! `coupled-run`: the desktop app's coupled run loop without Tauri or a webview.
//!
//! It restores the same DN2 ready session as the launcher (`tools/native_emu.sh`
//! `--auto-coupled`), steps the emulator in the app's [`STEP_CHUNK`]-instruction
//! chunks as fast as it can with optional native playback (the PCM is also measured), and
//! plays a scenario of guest-clock-driven panel events. For each scenario it
//! reports exact per-thread CPU time, the DSP and ColdFire instruction split, the
//! per-period DSP wall histogram against the 0.667 ms deadline, link timing, and
//! the PCM hash, RMS and peak. Measurement adds no per-instruction work: a few
//! timer reads per 0.667 ms period and one hash/sum pass over the PCM.
//!
//! ```text
//! coupled-run --syx Digitone_II_OS1.11.syx \
//!   --dsp-image snapshots/dn2-audio-ready-2026-10-03/digi-audio-dn2-image.bin \
//!   --dsp-state snapshots/dn2-audio-ready-2026-10-03/digi-audio-m5.snap.dsp \
//!   --cf-snapshot snapshots/dn2-audio-ready-2026-10-03/digi-audio-m5.snap \
//!   [--scenarios FILE.json] [--scenario NAME]... [--seconds S] [--playback] [--audio-buffer S] [--no-pcm-tap] [--no-link-timing] [--profile-regions] [--profile-execution] \
//!   [--out report.json] [--list]
//! ```
//!
//! Scenario file: `{"scenarios": [{"name", "description", "seconds" | "instructions",
//! "qa_script", "clip_last_chunk", "expect_sha256", "events": [...]}]}`. An event is `{"at_s" | "at_instr", "button",
//! "down"}` or `{"at_s" | "at_instr", "turn": encoder, "detents": n}`. Times count from
//! the restored ready point: `at_instr` in ColdFire instructions, `at_s` in audio
//! seconds (DSPI2 frames sent / 1500). Events fire at chunk boundaries, before the
//! chunk that follows, so a run is a function of its inputs. `qa_script` also plays
//! the shared QA sequence of `native/boot/examples/common` (NO, NO, encoder A).
//! The default scenarios are `native/coupled/scenarios.json`.

use digi_coupled::{
    AudioOptions, CoupledRuntime, ExecutionProfile, STEP_CHUNK, TierCounters, WorkerReport, common,
};
#[cfg(feature = "region-profile")]
use digi_coupled::{RegionCost, RegionProfile};
use elektron_native_boot::thread_cpu_ns;
use serde_json::{Value, json};
#[cfg(feature = "region-profile")]
use std::collections::BTreeMap;
use std::{
    fs,
    path::{Path, PathBuf},
    process::ExitCode,
    time::Instant,
};

/// DSPI2 frames per audio second (one 32-sample block each).
const FRAMES_PER_SECOND: f64 = 1500.0;
/// The DSP period as wall time: a period slower than this cannot keep up in real time.
const DEADLINE_NS: u64 = 666_667;
const DEFAULT_SCENARIOS: &str = include_str!("../../scenarios.json");

#[derive(Clone, Copy, Debug)]
enum When {
    Instr(u64),
    Frames(u64),
}

#[derive(Clone, Copy, Debug)]
enum Action {
    Button { code: u8, down: bool },
    Turn { encoder: u8, detents: i32 },
}

#[derive(Debug)]
struct Event {
    when: When,
    action: Action,
}

#[derive(Debug)]
struct Scenario {
    name: String,
    description: String,
    /// End after this many audio seconds, or this many ColdFire instructions.
    seconds: Option<f64>,
    instructions: Option<u64>,
    qa_script: bool,
    /// With `instructions`: shorten the last chunk so the run ends exactly there
    /// (the desktop Trig 1 test does); otherwise chunks are always full, as in the app.
    clip_last_chunk: bool,
    /// SHA-256 the run's whole PCM must have (a recorded reference).
    expect_sha256: Option<String>,
    events: Vec<Event>,
}

fn parse_scenarios(text: &str) -> Result<Vec<Scenario>, String> {
    let root: Value = serde_json::from_str(text).map_err(|e| format!("scenario file: {e}"))?;
    let list = root["scenarios"]
        .as_array()
        .ok_or("scenario file needs a \"scenarios\" array")?;
    list.iter().map(parse_scenario).collect()
}

fn parse_scenario(v: &Value) -> Result<Scenario, String> {
    let name = v["name"]
        .as_str()
        .ok_or("scenario without a name")?
        .to_string();
    let err = |m: &str| format!("scenario {name}: {m}");
    let seconds = v["seconds"].as_f64();
    let instructions = v["instructions"].as_u64();
    if seconds.is_none() == instructions.is_none() {
        return Err(err("give exactly one of \"seconds\" and \"instructions\""));
    }
    let mut events = Vec::new();
    for e in v["events"].as_array().map_or(&[][..], Vec::as_slice) {
        let when = match (e["at_s"].as_f64(), e["at_instr"].as_u64()) {
            (Some(s), None) if s >= 0.0 => When::Frames((s * FRAMES_PER_SECOND).round() as u64),
            (None, Some(n)) => When::Instr(n),
            _ => return Err(err("an event needs one of at_s (>= 0) and at_instr")),
        };
        let action = if let Some(code) = e["button"].as_u64() {
            Action::Button {
                code: u8::try_from(code).map_err(|_| err("button code over 255"))?,
                down: e["down"].as_bool().unwrap_or(true),
            }
        } else if let Some(enc) = e["turn"].as_u64() {
            Action::Turn {
                encoder: u8::try_from(enc).map_err(|_| err("encoder over 255"))?,
                detents: i32::try_from(e["detents"].as_i64().unwrap_or(1))
                    .map_err(|_| err("detents out of range"))?,
            }
        } else {
            return Err(err("an event needs \"button\" or \"turn\""));
        };
        events.push(Event { when, action });
    }
    Ok(Scenario {
        description: v["description"].as_str().unwrap_or("").to_string(),
        name,
        seconds,
        instructions,
        qa_script: v["qa_script"].as_bool().unwrap_or(false),
        clip_last_chunk: v["clip_last_chunk"].as_bool().unwrap_or(false),
        expect_sha256: v["expect_sha256"].as_str().map(str::to_string),
        events,
    })
}

struct Args {
    syx: PathBuf,
    image: PathBuf,
    state: PathBuf,
    snapshot: PathBuf,
    scenarios: Option<PathBuf>,
    names: Vec<String>,
    seconds: Option<f64>,
    playback: bool,
    audio_buffer: f64,
    pcm_tap: bool,
    link_timing: bool,
    profile_regions: bool,
    profile_execution: bool,
    out: Option<PathBuf>,
    list: bool,
}

fn parse_args() -> Result<Args, String> {
    let mut a = Args {
        syx: "Digitone_II_OS1.11.syx".into(),
        image: PathBuf::new(),
        state: PathBuf::new(),
        snapshot: PathBuf::new(),
        scenarios: None,
        names: Vec::new(),
        seconds: None,
        playback: false,
        audio_buffer: 0.0,
        pcm_tap: true,
        link_timing: true,
        profile_regions: false,
        profile_execution: false,
        out: None,
        list: false,
    };
    let mut it = std::env::args().skip(1);
    while let Some(flag) = it.next() {
        let mut value = |what: &str| it.next().ok_or(format!("{what} needs a value"));
        match flag.as_str() {
            "--syx" => a.syx = value("--syx")?.into(),
            "--dsp-image" => a.image = value("--dsp-image")?.into(),
            "--dsp-state" => a.state = value("--dsp-state")?.into(),
            "--cf-snapshot" => a.snapshot = value("--cf-snapshot")?.into(),
            "--scenarios" => a.scenarios = Some(value("--scenarios")?.into()),
            "--scenario" => a.names.push(value("--scenario")?),
            "--seconds" => {
                a.seconds = Some(value("--seconds")?.parse().map_err(|_| "bad --seconds")?)
            }
            "--playback" => a.playback = true,
            "--audio-buffer" => {
                a.audio_buffer = value("--audio-buffer")?
                    .parse()
                    .map_err(|_| "bad --audio-buffer")?
            }
            "--no-pcm-tap" => a.pcm_tap = false,
            "--no-link-timing" => a.link_timing = false,
            "--profile-regions" => a.profile_regions = true,
            "--profile-execution" => a.profile_execution = true,
            "--out" => a.out = Some(value("--out")?.into()),
            "--list" => a.list = true,
            other => return Err(format!("unknown option {other}")),
        }
    }
    if !a.list
        && (a.image.as_os_str().is_empty()
            || a.state.as_os_str().is_empty()
            || a.snapshot.as_os_str().is_empty())
    {
        return Err("--dsp-image, --dsp-state and --cf-snapshot are required".into());
    }
    Ok(a)
}

fn percentile(sorted: &[u32], p: f64) -> u32 {
    if sorted.is_empty() {
        return 0;
    }
    sorted[(((sorted.len() - 1) as f64) * p).round() as usize]
}

fn period_histogram(periods: &[u32]) -> Value {
    let mut sorted = periods.to_vec();
    sorted.sort_unstable();
    let over = periods
        .iter()
        .filter(|&&n| u64::from(n) > DEADLINE_NS)
        .count();
    let ms = |n: u32| f64::from(n) / 1e6;
    let mean = periods.iter().map(|&n| f64::from(n)).sum::<f64>() / periods.len().max(1) as f64;
    json!({
        "deadline_ms": DEADLINE_NS as f64 / 1e6,
        "periods": periods.len(),
        "over_deadline": over,
        "over_deadline_fraction": over as f64 / periods.len().max(1) as f64,
        "mean_ms": mean / 1e6,
        "p50_ms": ms(percentile(&sorted, 0.50)),
        "p90_ms": ms(percentile(&sorted, 0.90)),
        "p99_ms": ms(percentile(&sorted, 0.99)),
        "max_ms": ms(sorted.last().copied().unwrap_or(0)),
        "total_ms": periods.iter().map(|&n| u64::from(n)).sum::<u64>() as f64 / 1e6,
    })
}

/// Difference of two numeric JSON objects, field by field.
fn json_delta(after: &Value, before: &Value) -> Value {
    let (Some(a), Some(b)) = (after.as_object(), before.as_object()) else {
        return Value::Null;
    };
    let mut out = serde_json::Map::new();
    for (k, v) in a {
        if let (Some(x), Some(y)) = (v.as_u64(), b.get(k).and_then(Value::as_u64)) {
            out.insert(k.clone(), x.saturating_sub(y).into());
        }
    }
    out.into()
}

fn dsp_split(a: &TierCounters, b: &TierCounters) -> Value {
    let total = a.icount.saturating_sub(b.icount);
    let idle = a.idle_skipped.saturating_sub(b.idle_skipped);
    let interp = a.interpreted.saturating_sub(b.interpreted);
    let blocks = a.block_instructions.saturating_sub(b.block_instructions);
    let fast = a.fast_insns.saturating_sub(b.fast_insns);
    // The fast tier's instructions are counted in block_instructions too.
    let aot = blocks.saturating_sub(fast);
    let other = total.saturating_sub(idle + interp + aot + fast);
    let pct = |n: u64| 100.0 * n as f64 / total.max(1) as f64;
    json!({
        "instructions": total,
        "idle_skip": idle, "interpreter": interp, "aot_block": aot, "fast_tier": fast, "other": other,
        "idle_skip_pct": pct(idle), "interpreter_pct": pct(interp), "aot_block_pct": pct(aot),
        "fast_tier_pct": pct(fast), "other_pct": pct(other),
        "block_entries": a.block_entries.saturating_sub(b.block_entries),
        "block_traps": a.block_traps.saturating_sub(b.block_traps),
        "idle_skips": a.idle_skips.saturating_sub(b.idle_skips),
        "fast_present": a.fast_present,
        "fast_runs": a.fast_runs.saturating_sub(b.fast_runs),
        "fast_calls": a.fast_calls.saturating_sub(b.fast_calls),
        "fast_built_total": a.fast_built, "fast_refused_total": a.fast_refused,
        "fast_armed_total": a.fast_armed,
        "fast_compile_ms_total": a.fast_compile_ns as f64 / 1e6,
        "fast_build_ms_total": a.fast_build_ns as f64 / 1e6,
    })
}

const EXECUTION_PROFILE_MAPS: [(&str, &str); 5] = [
    ("coverage", "pc MODE1 known count"),
    (
        "entries",
        "pc count (interpreter sequence entries, not AOT dispatch attempts)",
    ),
    ("transitions", "from_pc to_pc count"),
    (
        "exits",
        "block_pc exit_kind at_pc count; traps are exit rows",
    ),
    (
        "entry_bails",
        "pc MODE1 unknown_register_bitset pending count; non-trap zero-progress entry bails",
    ),
];

/// Write the Engine's existing option-26 maps only after the timed end probe.
/// Empty TSV maps remain valid evidence that this fresh scenario made no rows
/// of a given kind.
fn write_execution_profile(
    out: &Path,
    scenario: &str,
    profile: &ExecutionProfile,
    end: &WorkerReport,
    start: &WorkerReport,
) -> Result<Value, String> {
    let stem = out
        .file_stem()
        .and_then(|stem| stem.to_str())
        .ok_or_else(|| format!("--out path has no UTF-8 file stem: {}", out.display()))?;
    let dir = out.with_file_name(format!("{stem}-execution-profile"));
    fs::create_dir_all(&dir).map_err(|e| format!("{}: {e}", dir.display()))?;
    let scenario_name = scenario
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '-' || c == '_' {
                c
            } else {
                '_'
            }
        })
        .collect::<String>();
    let mut maps = Vec::with_capacity(EXECUTION_PROFILE_MAPS.len());
    for (kind, ((name, columns), text)) in
        EXECUTION_PROFILE_MAPS.iter().zip(&profile.maps).enumerate()
    {
        let path = dir.join(format!("{scenario_name}.{name}.tsv"));
        fs::write(&path, text).map_err(|e| format!("{}: {e}", path.display()))?;
        maps.push(json!({
            "kind": kind,
            "name": name,
            "columns": columns,
            "rows": text.lines().count(),
            "path": path,
        }));
    }
    let model_stats = model_stats_delta(end, start)?;
    Ok(json!({
        "metadata": {
            "source": "Engine.set_option(26) and Engine.profile_text(0..4)",
            "measurement": "frequency counts and aggregate model-gate counters only; no CPU or benchmark attribution",
            "start": "enabled by ordered worker acknowledgement after ready and before the start probe",
            "end": "requested by a dedicated worker message after the end timing probe",
            "maps_scope": "fresh Engine for this scenario; maps are cumulative from enable to the post-run request",
            "model_stats_scope": "checked end-minus-start worker-boundary delta; excludes work before the start probe",
            "empty_maps_valid": true,
        },
        "maps": maps,
        "model_stats": model_stats,
    }))
}

fn model_stats_delta(after: &WorkerReport, before: &WorkerReport) -> Result<Value, String> {
    let after = after
        .model_stats
        .ok_or("execution profile requires DSP model stats at end")?;
    let before = before
        .model_stats
        .ok_or("execution profile requires DSP model stats at start")?;
    let delta = |name: &str, end: u64, start: u64| {
        end.checked_sub(start)
            .ok_or_else(|| format!("execution profile model counter moved backwards: {name}"))
    };
    Ok(json!({
        "gated": delta("gated", after.gated, before.gated)?,
        "unsafe_block": delta("unsafe_block", after.unsafe_block, before.unsafe_block)?,
        "irq_deferred": delta("irq_deferred", after.irq_deferred, before.irq_deferred)?,
        "timer_gated": delta("timer_gated", after.timer_gated, before.timer_gated)?,
        "mmr_exits": delta("mmr_exits", after.mmr_exits, before.mmr_exits)?,
        "code_mismatch": delta("code_mismatch", after.code_mismatch, before.code_mismatch)?,
    }))
}

#[cfg(feature = "region-profile")]
fn counter_delta(after: u64, before: u64, name: &str) -> Result<u64, String> {
    after
        .checked_sub(before)
        .ok_or_else(|| format!("region profile counter moved backwards: {name}"))
}

#[cfg(feature = "region-profile")]
fn cost_delta(after: RegionCost, before: RegionCost, name: &str) -> Result<RegionCost, String> {
    Ok(RegionCost {
        calls: counter_delta(after.calls, before.calls, &format!("{name}.calls"))?,
        zero_progress_calls: counter_delta(
            after.zero_progress_calls,
            before.zero_progress_calls,
            &format!("{name}.zero_progress_calls"),
        )?,
        instructions: counter_delta(
            after.instructions,
            before.instructions,
            &format!("{name}.instructions"),
        )?,
        wall_ns: counter_delta(after.wall_ns, before.wall_ns, &format!("{name}.wall_ns"))?,
    })
}

#[cfg(feature = "region-profile")]
fn profile_costs(profile: &RegionProfile) -> BTreeMap<(u8, u32), RegionCost> {
    let mut costs = BTreeMap::new();
    for (pc, entry) in profile.entries() {
        if entry.fast.calls != 0 {
            costs.insert((0, pc), entry.fast);
        }
        if entry.aot.calls != 0 {
            costs.insert((1, pc), entry.aot);
        }
    }
    costs
}

#[cfg(feature = "region-profile")]
fn region_profile_delta(after: &WorkerReport, before: &WorkerReport) -> Result<Value, String> {
    let after_profile = after
        .region_profile
        .as_ref()
        .ok_or("region profile was not enabled in the DSP worker")?;
    let before_profile = before
        .region_profile
        .as_ref()
        .ok_or("region profile was not enabled at scenario start")?;
    let after_costs = profile_costs(after_profile);
    let before_costs = profile_costs(before_profile);
    let mut totals = [RegionCost::default(); 2];
    let mut rows = Vec::new();
    for (&(tier, pc), &after_cost) in &after_costs {
        let before_cost = before_costs.get(&(tier, pc)).copied().unwrap_or_default();
        let cost = cost_delta(after_cost, before_cost, &format!("{tier}:{pc:06x}"))?;
        if cost.calls == 0 {
            continue;
        }
        let total = &mut totals[tier as usize];
        total.calls = total
            .calls
            .checked_add(cost.calls)
            .ok_or("region profile calls overflow")?;
        total.zero_progress_calls = total
            .zero_progress_calls
            .checked_add(cost.zero_progress_calls)
            .ok_or("region profile zero-progress calls overflow")?;
        total.instructions = total
            .instructions
            .checked_add(cost.instructions)
            .ok_or("region profile instructions overflow")?;
        total.wall_ns = total
            .wall_ns
            .checked_add(cost.wall_ns)
            .ok_or("region profile wall time overflow")?;
        rows.push((tier, pc, cost));
    }
    for key in before_costs.keys() {
        if !after_costs.contains_key(key) {
            return Err(format!(
                "region profile entry disappeared: {}:{:06x}",
                key.0, key.1
            ));
        }
    }
    rows.sort_unstable_by(|a, b| b.2.wall_ns.cmp(&a.2.wall_ns));
    let (Some(after_tiers), Some(before_tiers)) = (&after.tiers, &before.tiers) else {
        return Err("region profile requires DSP tier counters".into());
    };
    let blocks = counter_delta(
        after_tiers.block_instructions,
        before_tiers.block_instructions,
        "block_instructions",
    )?;
    let fast = counter_delta(
        after_tiers.fast_insns,
        before_tiers.fast_insns,
        "fast_insns",
    )?;
    let aot = blocks
        .checked_sub(fast)
        .ok_or("fast instruction delta exceeds block instruction delta")?;
    if totals[0].instructions != fast || totals[1].instructions != aot {
        return Err(format!(
            "region profile instruction reconciliation failed: fast {} != {fast}, aot {} != {aot}",
            totals[0].instructions, totals[1].instructions
        ));
    }
    let cost_json = |cost: RegionCost| {
        json!({
            "calls": cost.calls,
            "zero_progress_calls": cost.zero_progress_calls,
            "retired_instructions": cost.instructions,
            "wall_ns": cost.wall_ns,
        })
    };
    Ok(json!({
        "metadata": {
            "clock": "std::time::Instant elapsed nanoseconds",
            "method": "times each fast.run attempt and generated entry.f invocation; instruction counts are icount deltas",
            "fast_wall_ns": "includes cold build, rebuild, and compiler work performed inside fast.run; do not treat it as steady-state execution time",
            "chain_attribution": "an AOT invocation includes any generated chain calls and is attributed to its dispatch entry PC",
            "unmeasured": "dispatcher, model and bank housekeeping, interpreter instructions, and idle visits are not attributed",
            "overhead": "opt-in profiling adds timer reads and sparse-page counter updates at measured dispatches",
        },
        "tiers": { "fast": cost_json(totals[0]), "aot": cost_json(totals[1]) },
        "rows": rows.into_iter().map(|(tier, pc, cost)| json!({
            "tier": if tier == 0 { "fast" } else { "aot" },
            "entry_pc": format!("{pc:06x}"),
            "entry_pc_u32": pc,
            "calls": cost.calls,
            "zero_progress_calls": cost.zero_progress_calls,
            "retired_instructions": cost.instructions,
            "wall_ns": cost.wall_ns,
        })).collect::<Vec<_>>(),
        "reconciliation": { "fast_instructions": fast, "aot_instructions": aot, "ok": true },
    }))
}

struct Probe {
    wall: Instant,
    cf_cpu_ns: u64,
    worker: WorkerReport,
    status: Value,
    link: Value,
    frames: u64,
}

fn probe(rt: &mut CoupledRuntime) -> Result<Probe, String> {
    // snapshot() synchronises the DSP: every frame sent is rendered and drained.
    let snap = rt.snapshot();
    let worker = rt.worker_report().ok_or("DSP worker stopped")?;
    let report = rt.audio_report().ok_or("no native audio session")?;
    Ok(Probe {
        wall: Instant::now(),
        cf_cpu_ns: thread_cpu_ns(),
        worker,
        status: serde_json::to_value(&snap.status).map_err(|e| e.to_string())?,
        link: report["link_timing"].clone(),
        frames: rt.frames_sent(),
    })
}

fn fire(rt: &mut CoupledRuntime, action: Action) -> Result<(), String> {
    match action {
        Action::Button { code, down } => rt.button(code, down),
        Action::Turn { encoder, detents } => rt.turn(encoder, detents),
    }
}

fn run_scenario(args: &Args, syx: &[u8], sc: &Scenario) -> Result<Value, String> {
    let setup = Instant::now();
    let options = AudioOptions {
        image: args.image.clone(),
        state: args.state.clone(),
        snapshot: args.snapshot.clone(),
        buffer_seconds: args.audio_buffer,
        playback: args.playback,
        automatic: false,
        expected_syx: None,
        link_timing: args.link_timing,
    };
    let mut rt = CoupledRuntime::new(syx, None, Some(&options))?;
    if args.pcm_tap {
        rt.observe_pcm(false);
    }
    let setup_s = setup.elapsed().as_secs_f64();
    let initial = rt.snapshot().status;
    if !initial.ready {
        return Err("the restored snapshot is not at ready".into());
    }
    #[cfg(feature = "region-profile")]
    if args.profile_regions && !rt.set_region_profile(true) {
        return Err("DSP worker stopped while enabling region profile".into());
    }
    if args.profile_execution && !rt.set_execution_profile(true) {
        return Err("DSP worker did not enable execution profile".into());
    }
    let mut script = sc.qa_script.then(common::Script::from_env);
    if let Some(script) = &mut script {
        script.poll(&mut rt, initial.icount, true);
    }
    let seconds = args.seconds.or(sc.seconds);
    let target_frames = seconds.map(|s| (s * FRAMES_PER_SECOND).ceil() as u64);
    let until_instr = if args.seconds.is_some() {
        None
    } else {
        sc.instructions
    };

    let start = probe(&mut rt)?;
    let initial_audio_health = rt.audio_report().unwrap_or(Value::Null);
    let start_health = json!({
        "elapsed_wall_seconds": 0.0,
        "source_pcm_frames": initial_audio_health["source_pcm_frames"],
        "health": initial_audio_health,
    });
    eprintln!("coupled-run sample phase started");
    let (mut next, mut icount, mut step_error) = (0usize, initial.icount, None);
    let mut playback_observations = Vec::new();
    let mut next_playback_observation = 0.1_f64;
    let mut events: Vec<&Event> = sc.events.iter().collect();
    events.sort_by_key(|e| match e.when {
        When::Instr(n) => (0, n),
        When::Frames(n) => (1, n),
    });
    let mut chunks = 0u64;
    loop {
        while let Some(e) = events.get(next) {
            let due = match e.when {
                When::Instr(n) => icount - initial.icount >= n,
                When::Frames(n) => rt.frames_sent() >= n,
            };
            if !due {
                break;
            }
            fire(&mut rt, e.action)?;
            next += 1;
        }
        let elapsed = icount - initial.icount;
        let done = until_instr.map_or_else(
            || target_frames.is_some_and(|n| rt.frames_sent() >= n),
            |n| elapsed >= n,
        );
        if done {
            break;
        }
        let budget = match until_instr {
            Some(n) if sc.clip_last_chunk => (n - elapsed).min(u64::from(STEP_CHUNK)) as u32,
            _ => STEP_CHUNK,
        };
        let status = rt.step_chunk(budget).status;
        chunks += 1;
        if args.playback && start.wall.elapsed().as_secs_f64() >= next_playback_observation {
            let elapsed = start.wall.elapsed().as_secs_f64();
            let health = rt.audio_report().unwrap_or(Value::Null);
            playback_observations.push(json!({
                "elapsed_wall_seconds": elapsed,
                "source_pcm_frames": health["source_pcm_frames"],
                "health": health,
            }));
            next_playback_observation = elapsed + 0.1;
        }
        icount = status.icount;
        if let Some(error) = status.error {
            step_error = Some(error);
            break;
        }
        if let Some(script) = &mut script {
            script.poll(&mut rt, icount, status.ready);
        }
    }
    let end = probe(&mut rt)?;
    let execution_profile = if args.profile_execution {
        let profile = rt
            .execution_profile()
            .ok_or("DSP worker did not return execution profile maps")?;
        let out = args
            .out
            .as_deref()
            .ok_or("--profile-execution requires --out")?;
        Some(write_execution_profile(
            out,
            &sc.name,
            &profile,
            &end.worker,
            &start.worker,
        )?)
    } else {
        None
    };
    let wall = end.wall.duration_since(start.wall).as_secs_f64();
    let cf_cpu = (end.cf_cpu_ns - start.cf_cpu_ns) as f64 / 1e9;
    let dsp_cpu = (end.worker.cpu_ns - start.worker.cpu_ns) as f64 / 1e9;
    let tap = rt.pcm_tap();
    let audio_s = match tap {
        Some(t) => t.seconds(),
        None => (end.frames - start.frames) as f64 / FRAMES_PER_SECOND,
    };
    let per = |x: f64| if audio_s > 0.0 { x / audio_s } else { 0.0 };

    let periods = end
        .worker
        .period_ns
        .get(start.worker.period_ns.len()..)
        .unwrap_or(&[]);
    let st = |p: &Probe, k: &str| p.status[k].as_u64().unwrap_or(0);
    let cf_total = st(&end, "icount") - st(&start, "icount");
    let cf_interp = st(&end, "interpreted_instructions") - st(&start, "interpreted_instructions");
    let cf_idle = st(&end, "idle_fast_forwarded_instructions")
        - st(&start, "idle_fast_forwarded_instructions");
    let cf_clear = st(&end, "ram_clear_fast_forwarded_instructions")
        - st(&start, "ram_clear_fast_forwarded_instructions");
    let health = rt.audio_report().unwrap_or(Value::Null);
    let missing = health["missing_blocks"].as_u64().unwrap_or(u64::MAX);
    let halted = health["halted"].clone();
    let pcm = tap.map_or(Value::Null, |t| {
        json!({
            "values": t.count, "seconds": t.seconds(), "sha256": t.sha256_hex(), "rms": t.rms(),
            "peak": t.peak, "nonzero": t.nonzero, "non_finite": t.non_finite,
            "active_windows_100ms": t.active_windows, "windows_100ms": t.windows,
            "window_rms_100ms": t.window_rms.iter().map(|&r| (f64::from(r) * 1e4).round() / 1e4).collect::<Vec<_>>(),
        })
    });
    let hash_match = match (&sc.expect_sha256, tap) {
        (Some(want), Some(t)) => Some(*want == t.sha256_hex()),
        (Some(_), None) => Some(false),
        _ => None,
    };
    #[cfg(feature = "region-profile")]
    let region_profile = if args.profile_regions {
        region_profile_delta(&end.worker, &start.worker)?
    } else {
        Value::Null
    };
    #[cfg(not(feature = "region-profile"))]
    let region_profile = Value::Null;
    let playback_ok = !args.playback
        || (health["sink_connected"].as_bool() == Some(true)
            && health["sink"]["stream_started"].as_bool() == Some(true)
            && health["sink"]["callback_max_frames"].as_u64().unwrap_or(0) > 0
            && health["device_error"].is_null()
            && health["sink"]["error"].is_null());
    let gates = json!({
        "missing_blocks": missing, "halted": halted, "step_error": step_error,
        "pcm_non_finite": tap.map_or(0, |t| t.non_finite),
        "pcm_hash_expected": sc.expect_sha256, "pcm_hash_match": hash_match,
        "ok": missing == 0 && halted.is_null() && step_error.is_none()
            && tap.is_none_or(|t| t.non_finite == 0) && hash_match != Some(false) && playback_ok,
        "playback_health_ok": playback_ok,
    });
    Ok(json!({
        "name": sc.name, "description": sc.description,
        "audio_seconds": audio_s, "wall_seconds": wall, "real_time_factor": per(wall).recip(),
        "setup_wall_seconds": setup_s, "chunks": chunks,
        "audio_config": { "playback": args.playback, "audio_buffer_seconds": args.audio_buffer },
        "audio_health_start": start_health,
        "audio_health_end": { "elapsed_wall_seconds": wall, "source_pcm_frames": health["source_pcm_frames"], "health": health },
        "playback_observations": playback_observations,
        "guest_instructions": cf_total, "frames": end.frames - start.frames,
        "cpu": {
            "dsp_thread_s": dsp_cpu, "cf_thread_s": cf_cpu,
            "dsp_thread_per_audio_s": per(dsp_cpu), "cf_thread_per_audio_s": per(cf_cpu),
            "dsp_thread_busy_of_wall": dsp_cpu / wall.max(1e-9),
            "cf_thread_busy_of_wall": cf_cpu / wall.max(1e-9),
            "clock": "CLOCK_THREAD_CPUTIME_ID",
        },
        "link_timing": json_delta(&end.link, &start.link),
        "dsp_instructions": match (&end.worker.tiers, &start.worker.tiers) {
            (Some(a), Some(b)) => dsp_split(a, b),
            _ => Value::Null,
        },
        "region_profile": region_profile,
        "execution_profile": execution_profile,
        "cf_instructions": {
            "total": cf_total, "interpreted": cf_interp, "idle_fast_forward": cf_idle,
            "ram_clear": cf_clear,
            "interpreted_pct": 100.0 * cf_interp as f64 / cf_total.max(1) as f64,
            "idle_fast_forward_pct": 100.0 * cf_idle as f64 / cf_total.max(1) as f64,
        },
        "dsp_period_wall": period_histogram(periods),
        "pcm": pcm,
        "gates": gates,
    }))
}

fn summary_line(r: &Value) -> String {
    let f = |v: &Value| v.as_f64().unwrap_or(f64::NAN);
    format!(
        "{:<14} audio {:>6.2}s wall {:>7.2}s rtf {:>5.2} dsp_cpu/s {:>5.2} cf_cpu/s {:>5.2} over_deadline {:>5.1}% rms {:.4} ok={}",
        r["name"].as_str().unwrap_or("?"),
        f(&r["audio_seconds"]),
        f(&r["wall_seconds"]),
        f(&r["real_time_factor"]),
        f(&r["cpu"]["dsp_thread_per_audio_s"]),
        f(&r["cpu"]["cf_thread_per_audio_s"]),
        100.0 * f(&r["dsp_period_wall"]["over_deadline_fraction"]),
        f(&r["pcm"]["rms"]),
        r["gates"]["ok"],
    )
}

fn real_main() -> Result<bool, String> {
    let args = parse_args()?;
    if args.profile_regions && !cfg!(feature = "region-profile") {
        return Err(
            "--profile-regions needs a diagnostic rebuild with --features coupled-audio,region-profile"
                .into(),
        );
    }
    if args.profile_execution && args.out.is_none() {
        return Err("--profile-execution requires --out".into());
    }
    let text = match &args.scenarios {
        Some(path) => {
            std::fs::read_to_string(path).map_err(|e| format!("{}: {e}", path.display()))?
        }
        None => DEFAULT_SCENARIOS.to_string(),
    };
    let all = parse_scenarios(&text)?;
    if args.list {
        for s in &all {
            println!("{:<14} {}", s.name, s.description);
        }
        return Ok(true);
    }
    for n in &args.names {
        if !all.iter().any(|s| &s.name == n) {
            return Err(format!("no scenario named {n}"));
        }
    }
    let chosen: Vec<&Scenario> = all
        .iter()
        .filter(|s| args.names.is_empty() || args.names.contains(&s.name))
        .collect();
    let syx = std::fs::read(&args.syx).map_err(|e| format!("{}: {e}", args.syx.display()))?;
    let mut results = Vec::new();
    for sc in chosen {
        eprintln!("scenario {} ...", sc.name);
        let r = run_scenario(&args, &syx, sc)?;
        println!("{}", summary_line(&r));
        results.push(r);
    }
    let ok = results.iter().all(|r| r["gates"]["ok"] == true);
    let report = json!({
        "schema_version": 1,
        "step_chunk": STEP_CHUNK,
        "env": {
            "SHARC_FAST_REGIONS": std::env::var("SHARC_FAST_REGIONS").ok(),
            "SHARC_FAST_AUTO": std::env::var("SHARC_FAST_AUTO").ok(),
            "SHARC_GEN_DIR": std::env::var("SHARC_GEN_DIR").ok(),
            "DSP_IDLE_SKIP": std::env::var("DSP_IDLE_SKIP").ok(),
        },
        "pcm_tap": args.pcm_tap,
        "link_timing": args.link_timing,
        "profile_regions": args.profile_regions,
        "profile_execution": args.profile_execution,
        "scenarios": results,
    });
    if let Some(out) = &args.out {
        if let Some(dir) = out.parent() {
            std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
        }
        std::fs::write(
            out,
            serde_json::to_vec_pretty(&report).map_err(|e| e.to_string())?,
        )
        .map_err(|e| format!("{}: {e}", out.display()))?;
    }
    Ok(ok)
}

fn main() -> ExitCode {
    match real_main() {
        Ok(true) => ExitCode::SUCCESS,
        Ok(false) => {
            eprintln!("coupled-run: a gate failed (see \"gates\" in the report)");
            ExitCode::from(2)
        }
        Err(e) => {
            eprintln!("coupled-run: {e}");
            ExitCode::from(1)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_scenarios_parse_and_have_the_documented_names() {
        let all = parse_scenarios(DEFAULT_SCENARIOS).unwrap();
        let names: Vec<_> = all.iter().map(|s| s.name.as_str()).collect();
        for n in [
            "idle",
            "trig1-hold",
            "kb-chord16",
            "exactness-100m",
            "trig1-noqa-40m",
        ] {
            assert!(names.contains(&n), "{n}");
        }
        let hold = all.iter().find(|s| s.name == "trig1-hold").unwrap();
        assert!(hold.qa_script && hold.seconds == Some(10.0));
    }

    #[test]
    fn scenarios_need_exactly_one_end_and_a_valid_event() {
        let bad = |text: &str| parse_scenarios(text).unwrap_err();
        assert!(bad(r#"{"scenarios":[{"name":"x"}]}"#).contains("exactly one"));
        assert!(
            bad(r#"{"scenarios":[{"name":"x","seconds":1,"instructions":2}]}"#)
                .contains("exactly one")
        );
        assert!(
            bad(r#"{"scenarios":[{"name":"x","seconds":1,"events":[{"button":1}]}]}"#)
                .contains("at_s")
        );
        assert!(
            bad(r#"{"scenarios":[{"name":"x","seconds":1,"events":[{"at_s":0}]}]}"#)
                .contains("button")
        );
        let ok = parse_scenarios(
            r#"{"scenarios":[{"name":"x","seconds":2,"events":[{"at_s":1.5,"button":25},{"at_instr":7,"turn":1,"detents":-2}]}]}"#,
        )
        .unwrap();
        assert!(matches!(ok[0].events[0].when, When::Frames(2250)));
        assert!(matches!(
            ok[0].events[1].action,
            Action::Turn {
                encoder: 1,
                detents: -2
            }
        ));
    }

    #[test]
    fn percentiles_and_deadline_count() {
        let periods: Vec<u32> = (1..=100).map(|n| n * 10_000).collect();
        let h = period_histogram(&periods);
        assert_eq!(h["periods"], 100);
        // Periods above 666,667 ns: 67..=100.
        assert_eq!(h["over_deadline"], 34);
        assert!((h["max_ms"].as_f64().unwrap() - 1.0).abs() < 1e-9);
        assert!((h["p50_ms"].as_f64().unwrap() - 0.51).abs() < 0.011);
        assert_eq!(period_histogram(&[])["over_deadline"], 0);
    }

    #[test]
    fn tier_split_counts_fast_instructions_once() {
        let before = TierCounters::default();
        let after = TierCounters {
            icount: 1000,
            idle_skipped: 400,
            interpreted: 10,
            block_instructions: 590, // includes the 200 the fast tier ran
            fast_insns: 200,
            fast_present: true,
            ..TierCounters::default()
        };
        let split = dsp_split(&after, &before);
        assert_eq!(split["aot_block"], 390);
        assert_eq!(split["fast_tier"], 200);
        assert_eq!(split["other"], 0);
        assert_eq!(split["idle_skip"], 400);
    }
}
