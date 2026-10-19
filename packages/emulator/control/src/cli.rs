//! Command-line arguments shared by the desktop app and `digi-emud`.

use std::path::PathBuf;

use digi_coupled::{AudioMode, AudioOptions};

pub const USAGE: &str = "usage: digiemu [SYX] [--card-image FILE [--card-folder DIR [--no-card-sync]]] [--control|--no-control] [--paused] [--run-dir DIR] [--coupled|--auto-coupled --dsp-image FILE --dsp-state FILE --cf-snapshot FILE [--dsp-patch FILE] [--audio-buffer SECONDS] [--audio-mode rebuffer|stretch|gap] [--scenario NAME_OR_FILE] [--report PATH] [--no-audio]] [--dsp zero --cf-snapshot FILE]";

#[derive(Clone, Debug, Default)]
pub struct StartupArgs {
    pub syx: Option<PathBuf>,
    pub card_image: Option<PathBuf>,
    /// The +Drive folder `--card-image` was built from (`--card-folder`): the firmware's changes are exported
    /// to it on quit unless `--no-card-sync`.
    pub card_folder: Option<PathBuf>,
    /// False with `--no-card-sync`.
    pub card_sync: bool,
    pub audio: Option<AudioOptions>,
    pub scenario: Option<String>,
    pub report: Option<PathBuf>,
    /// `--control` / `--no-control`; None leaves the host's default.
    pub control: Option<bool>,
    /// Start with the guest paused (control clients step it).
    pub paused: bool,
    /// Directory for sockets and the discovery file (default `out/native/run`).
    pub run_dir: Option<PathBuf>,
}

pub fn parse_values(
    values: impl IntoIterator<Item = std::ffi::OsString>,
    coupled_build: bool,
) -> Result<StartupArgs, String> {
    let mut values = values.into_iter();
    let (mut syx, mut card_image, mut image, mut state, mut snapshot) =
        (None, None, None, None, None);
    let mut audio_profile_syx = None;
    let mut coupled = false;
    let mut zero = false;
    let mut automatic = false;
    let mut buffer = None;
    let mut no_audio = false;
    let mut audio_mode = None;
    let mut dsp_patch = None;
    let mut scenario = None;
    let mut report = None;
    let mut control = None;
    let mut paused = false;
    let mut run_dir = None;
    let mut card_folder = None;
    let mut card_sync = true;
    while let Some(value) = values.next() {
        let mut path = |label: &str| -> Result<PathBuf, String> {
            values
                .next()
                .map(Into::into)
                .ok_or_else(|| format!("{label} requires a file"))
        };
        if value == "--card-image" {
            card_image = Some(path("--card-image")?);
        } else if value == "--card-folder" {
            card_folder = Some(path("--card-folder")?);
        } else if value == "--no-card-sync" {
            card_sync = false;
        } else if value == "--control" {
            control = Some(true);
        } else if value == "--no-control" {
            control = Some(false);
        } else if value == "--paused" {
            paused = true;
        } else if value == "--run-dir" {
            run_dir = Some(path("--run-dir")?);
        } else if value == "--coupled" {
            coupled = true;
        } else if value == "--auto-coupled" {
            coupled = true;
            automatic = true;
        } else if value == "--audio-profile-syx" {
            audio_profile_syx = Some(path("--audio-profile-syx")?);
        } else if value == "--dsp-image" {
            image = Some(path("--dsp-image")?);
        } else if value == "--dsp-state" {
            state = Some(path("--dsp-state")?);
        } else if value == "--cf-snapshot" {
            snapshot = Some(path("--cf-snapshot")?);
        } else if value == "--dsp" {
            let mode = values.next().ok_or("--dsp requires zero or sharc")?;
            zero = match mode.to_str() {
                Some("zero") => true,
                Some("sharc") => false,
                _ => return Err("--dsp is zero or sharc".into()),
            };
        } else if value == "--dsp-patch" {
            dsp_patch = Some(path("--dsp-patch")?);
        } else if value == "--no-audio" {
            no_audio = true;
        } else if value == "--audio-buffer" {
            let seconds = values.next().ok_or("--audio-buffer requires seconds")?;
            let seconds: f64 = seconds
                .to_str()
                .ok_or("invalid --audio-buffer")?
                .parse()
                .map_err(|_| "invalid --audio-buffer")?;
            if !seconds.is_finite() || !(0.0..=10.0).contains(&seconds) {
                return Err("--audio-buffer must be finite and between 0 and 10 seconds".into());
            }
            buffer = Some(seconds);
        } else if value == "--audio-mode" {
            let mode = values.next().ok_or("--audio-mode requires a mode")?;
            audio_mode = Some(AudioMode::parse(
                mode.to_str().ok_or("invalid --audio-mode")?,
            )?);
        } else if value == "--scenario" {
            let spec = values.next().ok_or("--scenario requires a name or file")?;
            scenario = Some(
                spec.into_string()
                    .map_err(|_| "invalid --scenario".to_string())?,
            );
        } else if value == "--report" {
            report = Some(path("--report")?);
        } else if value.to_string_lossy().starts_with("--") {
            return Err(format!("unknown option: {}", value.to_string_lossy()));
        } else if syx.is_none() {
            syx = Some(value.into());
        } else {
            return Err(USAGE.into());
        }
    }
    if card_folder.is_some() && card_image.is_none() {
        return Err("--card-folder needs --card-image".into());
    }
    if !coupled_build && (scenario.is_some() || report.is_some()) {
        return Err("--scenario needs a coupled-audio build".into());
    }
    let audio = if zero {
        if coupled || image.is_some() || state.is_some() || dsp_patch.is_some() {
            return Err(
                "--dsp zero takes only --cf-snapshot (no --coupled, --dsp-image, --dsp-state or --dsp-patch)"
                    .into(),
            );
        }
        Some(AudioOptions {
            snapshot: snapshot.take().ok_or("--dsp zero requires --cf-snapshot")?,
            zero: true,
            ..Default::default()
        })
    } else if coupled {
        if syx.is_none() {
            return Err("--coupled requires a startup SYX".into());
        }
        Some(AudioOptions {
            image: image.ok_or("--coupled requires --dsp-image")?,
            state: state.ok_or("--coupled requires --dsp-state")?,
            snapshot: snapshot.ok_or("--coupled requires --cf-snapshot")?,
            buffer_seconds: buffer.unwrap_or(0.0),
            playback: !no_audio,
            automatic,
            expected_syx: if automatic {
                Some(audio_profile_syx.unwrap_or_else(|| {
                    std::env::var_os("DIGI_EMU_SYX")
                        .map(PathBuf::from)
                        .unwrap_or_else(|| PathBuf::from("firmware/Digitone_II_OS1.12.syx"))
                }))
            } else {
                None
            },
            link_timing: false,
            audio_mode: audio_mode.unwrap_or_default(),
            dsp_patch: dsp_patch.take(),
            zero: false,
        })
    } else {
        if image.is_some()
            || state.is_some()
            || snapshot.is_some()
            || buffer.is_some()
            || audio_mode.is_some()
            || dsp_patch.is_some()
            || scenario.is_some()
            || report.is_some()
        {
            return Err("audio options require --coupled".into());
        }
        None
    };
    Ok(StartupArgs {
        syx,
        card_image,
        card_folder,
        card_sync,
        audio,
        scenario,
        report,
        control,
        paused,
        run_dir,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(args: &[&str]) -> Result<StartupArgs, String> {
        parse_values(args.iter().map(std::ffi::OsString::from), true)
    }

    #[test]
    fn control_flags_and_run_dir_parse() {
        let a = parse(&["x.syx", "--control", "--paused", "--run-dir", "d"]).unwrap();
        assert_eq!(a.control, Some(true));
        assert!(a.paused);
        assert_eq!(a.run_dir, Some("d".into()));
        assert_eq!(parse(&["--no-control"]).unwrap().control, Some(false));
        assert_eq!(parse(&[]).unwrap().control, None);
    }

    #[test]
    fn card_folder_syncs_unless_switched_off() {
        let a = parse(&["x.syx", "--card-image", "c.img", "--card-folder", "pd"]).unwrap();
        assert_eq!(a.card_folder, Some("pd".into()));
        assert!(a.card_sync);
        let a = parse(&["--card-image", "c", "--card-folder", "pd", "--no-card-sync"]).unwrap();
        assert!(!a.card_sync);
        assert!(parse(&["--card-folder", "pd"]).is_err());
        assert_eq!(parse(&[]).unwrap().card_folder, None);
    }

    #[test]
    fn dsp_zero_needs_only_the_cf_snapshot() {
        let a = parse(&["x.syx", "--dsp", "zero", "--cf-snapshot", "s.snap"]).unwrap();
        let audio = a.audio.unwrap();
        assert!(audio.zero);
        assert_eq!(audio.snapshot, PathBuf::from("s.snap"));
        assert!(parse(&["--dsp", "zero"]).is_err());
        assert!(parse(&["--dsp", "zero", "--cf-snapshot", "s", "--dsp-image", "i"]).is_err());
        assert!(parse(&["--dsp", "other"]).is_err());
        assert!(parse(&["--dsp", "sharc"]).unwrap().audio.is_none());
    }
}
