//! A coupled cold boot to the ready state, in process: the Rust logic of
//! `native/boot/examples/sharc_live.rs` that `tools/ready.py` runs for a card.
//!
//! The ColdFire boots from the .syx with the card attached and the DSP
//! (restored from the cold-idle state) on the DSPI2 link. When the panel is
//! ready and, for devices whose sample loader keeps sending (DT2), link
//! port 0 has been quiet for a while, the ColdFire snapshot and the DSP state
//! are written in the files `CoupledRuntime::new` restores from
//! (`digi-audio-m5.snap` and `digi-audio-m5.snap.dsp`).
//!
//! The ColdFire machine is not `Send`, so call [`boot_to_ready`] on the thread
//! that should own it for the duration of the boot; it returns plain files.

use std::{
    path::{Path, PathBuf},
    sync::atomic::{AtomicBool, Ordering},
    time::Instant,
};

use elektron_native_boot::{
    Emulator,
    sharc_peer::{IdleLoop, NativeDsp, ThreadedPeer, open_engine_patched, period_from_env},
};
use emmc_card::Card;

const DSP_MAGIC: &[u8; 8] = b"DT2DSP01";
/// ColdFire instructions after which a boot is called stuck (sharc_live's limit).
pub const BOOT_LIMIT: u64 = 3_000_000_000;
/// About where a DT2 boot with a card becomes ready, for progress only.
const READY_ESTIMATE: u64 = 1_000_000_000;
/// Frames link port 0 stays quiet before the DT2 snapshot is taken.
const QUIET_FRAMES: usize = 400;
const CHUNK: u32 = 250_000;

/// What the cold boot needs besides the firmware and the card.
#[derive(Clone, Debug)]
pub struct ColdInputs {
    /// The packed DSP loader image (`digi-audio-dn2-image.bin`).
    pub image: PathBuf,
    /// The DSP's cold-start state at its first idle rest (`cold-idle.dsp`, the
    /// `dsp_cold` example's output; the same `DT2DSP01` layout as the sidecar).
    pub cold_idle: PathBuf,
}

#[derive(Clone, Debug)]
pub struct ColdProgress {
    pub icount: u64,
    pub frames: usize,
    /// 0..1, a guess from the usual boot length.
    pub fraction: f64,
    pub wall_seconds: f64,
}

#[derive(Clone, Debug)]
pub struct ColdReport {
    pub snapshot: PathBuf,
    pub dsp: PathBuf,
    pub icount: u64,
    pub frames: usize,
    pub wall_seconds: f64,
    /// Of `wall_seconds`: reading the DSP files and starting the DSP engine.
    pub dsp_start_seconds: f64,
}

fn write_atomic(path: &Path, data: &[u8]) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| format!("{}: {e}", parent.display()))?;
    }
    let mut partial = path.as_os_str().to_owned();
    partial.push(".part");
    let partial = PathBuf::from(partial);
    std::fs::write(&partial, data).map_err(|e| format!("{}: {e}", partial.display()))?;
    std::fs::rename(&partial, path).map_err(|e| format!("{}: {e}", path.display()))
}

/// Cold boot `syx` with `card` to the ready state and write
/// `<out_dir>/digi-audio-m5.snap` and `<out_dir>/digi-audio-m5.snap.dsp`.
///
/// `progress` is called about every 20 chunks; `cancel` is polled each chunk.
pub fn boot_to_ready(
    syx: &[u8],
    card: Card,
    inputs: &ColdInputs,
    out_dir: &Path,
    cancel: &AtomicBool,
    progress: impl FnMut(&ColdProgress),
) -> Result<ColdReport, String> {
    let emulator = Emulator::new(syx, Some(card))?;
    boot_emulator_to_ready(emulator, inputs, out_dir, cancel, progress)
}

/// [`boot_to_ready`] for a patched build of the firmware whose SHA-256 is
/// `base_sha256` (`Emulator::new_patched`: the base names the device profile;
/// the MAIN bytes are not checked). The DSP side is the base's, so `inputs`
/// are the base's cold-start files.
pub fn boot_patched_to_ready(
    syx: &[u8],
    card: Option<Card>,
    base_sha256: &str,
    inputs: &ColdInputs,
    out_dir: &Path,
    cancel: &AtomicBool,
    progress: impl FnMut(&ColdProgress),
) -> Result<ColdReport, String> {
    let emulator = Emulator::new_patched(syx, card, base_sha256)?;
    boot_emulator_to_ready(emulator, inputs, out_dir, cancel, progress)
}

/// The cold boot of a fresh `emulator` (nothing stepped yet) with the DSP of
/// `inputs` on the DSPI2 link, saved at the ready state into `out_dir`.
pub fn boot_emulator_to_ready(
    mut emulator: Emulator,
    inputs: &ColdInputs,
    out_dir: &Path,
    cancel: &AtomicBool,
    mut progress: impl FnMut(&ColdProgress),
) -> Result<ColdReport, String> {
    let started = Instant::now();
    let image = std::fs::read(&inputs.image).map_err(|e| format!("DSP image: {e}"))?;
    let sidecar =
        std::fs::read(&inputs.cold_idle).map_err(|e| format!("DSP cold-idle state: {e}"))?;
    if sidecar.len() <= 24 || &sidecar[..8] != DSP_MAGIC {
        return Err("cold-idle state is not a DT2DSP01 file".into());
    }
    let base = u64::from_le_bytes(sidecar[8..16].try_into().unwrap());
    let state = sidecar[24..].to_vec();

    let profile = emulator.diagnostics();
    let registry = device_profile::Registry::load().map_err(|e| e.to_string())?;
    let dsp = registry
        .dsp_for(&profile.device, &profile.version)
        .cloned()
        .ok_or_else(|| {
            format!(
                "no DSP profile for {} OS {}",
                profile.device, profile.version
            )
        })?;
    let idle = IdleLoop {
        range: (dsp.idle_start, dsp.idle_end),
        head: dsp.idle_head,
    };
    let sample_link = dsp.sample_link == Some(device_profile::SampleLink::Lp0);
    emulator.enable_ssi_diagnostic(96_000)?;
    let (peer, handle, shared) = ThreadedPeer::spawn(
        move || open_engine_patched(&image, &state, base, None, idle).map(NativeDsp),
        period_from_env(),
        idle.range,
    )?;
    if sample_link {
        peer.enable_sample_link();
    }
    {
        let mut shared = shared.borrow_mut();
        shared.attached = true;
        shared.dsp_instructions = 0;
    }
    emulator.set_dspi2_peer(peer.boxed());
    let dsp_start_seconds = started.elapsed().as_secs_f64();

    // DT2's sample loader keeps sending after the panel is ready: save at the
    // first ready point where link port 0 has been quiet for a while.
    let quiet_frames = sample_link.then_some(QUIET_FRAMES);
    let (mut last_lp0, mut last_change) = ((0u64, 0u32), 0usize);
    let mut chunks = 0u64;
    loop {
        if cancel.load(Ordering::Relaxed) {
            return Err("cancelled".into());
        }
        let snapshot = emulator.step_chunk(CHUNK);
        let status = &snapshot.status;
        if let Some(error) = &status.error {
            return Err(format!("guest fault during boot: {error}"));
        }
        // Commit every frame sent so far, so the quiet test does not depend on
        // how far the worker thread got.
        handle.sync();
        let (frames, quiet) = {
            let shared = shared.borrow();
            if let Some(halt) = &shared.halted {
                return Err(format!("DSP halted during boot: {halt}"));
            }
            let now = (shared.lp0_units, shared.lp0_pending);
            if now != last_lp0 {
                last_lp0 = now;
                last_change = shared.frames.len();
            }
            let quiet = quiet_frames
                .is_none_or(|n| shared.lp0_pending == 0 && shared.frames.len() >= last_change + n);
            (shared.frames.len(), quiet)
        };
        chunks += 1;
        if chunks % 20 == 0 {
            progress(&ColdProgress {
                icount: status.icount,
                frames,
                fraction: (status.icount as f64 / READY_ESTIMATE as f64).min(0.99),
                wall_seconds: started.elapsed().as_secs_f64(),
            });
        }
        if status.ready && quiet {
            let icount = status.icount;
            let cf = emulator
                .save_state()
                .map_err(|e| format!("snapshot: {e}"))?;
            let (exported, dsp_icount) = handle.export();
            let mut file = DSP_MAGIC.to_vec();
            file.extend(base.wrapping_add(dsp_icount).to_le_bytes());
            file.extend(shared.borrow().dsp_instructions.to_le_bytes());
            file.extend(exported);
            let snapshot_path = out_dir.join("digi-audio-m5.snap");
            let dsp_path = out_dir.join("digi-audio-m5.snap.dsp");
            // The DSP file first: a snapshot without its sidecar is unusable,
            // and the snapshot's presence is what marks the pair complete.
            write_atomic(&dsp_path, &file)?;
            write_atomic(&snapshot_path, &cf)?;
            return Ok(ColdReport {
                snapshot: snapshot_path,
                dsp: dsp_path,
                icount,
                frames,
                wall_seconds: started.elapsed().as_secs_f64(),
                dsp_start_seconds,
            });
        }
        if status.icount >= BOOT_LIMIT {
            return Err(format!("not ready after {BOOT_LIMIT} instructions"));
        }
    }
}
