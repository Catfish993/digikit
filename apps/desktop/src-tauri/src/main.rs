//! The desktop app: the webview's commands are thin messages to the shared
//! emulator controller (`packages/emulator/control`), which also serves the control
//! socket when the app runs with `--control` (the launcher adds it).
use std::{path::PathBuf, process};

use digi_control::{
    Command, Config, Handle, Mode, ScenarioSetup,
    cli::{self, StartupArgs},
    controller::spawn,
    dispatch::Dispatcher,
    load_scenario,
};
use elektron_native_boot::Snapshot;
mod desktop_runtime;
use serde_json::Value;
use std::sync::mpsc;
use tauri::State;

struct Host {
    handle: Handle,
    /// Where the webview's chosen sample files are staged before the card is built.
    staging: PathBuf,
}

fn parse_args() -> Result<StartupArgs, String> {
    cli::parse_values(std::env::args_os().skip(1), cfg!(feature = "coupled-audio"))
}

async fn send(
    host: State<'_, Host>,
    command: impl FnOnce(digi_control::Reply) -> Command + Send + 'static,
) -> Result<Snapshot, String> {
    let sender = host.handle.sender().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let (reply, response) = mpsc::channel();
        sender
            .send(command(reply))
            .map_err(|_| "emulator actor stopped")?;
        response
            .recv()
            .map_err(|_| "emulator actor dropped reply")?
    })
    .await
    .map_err(|e| e.to_string())?
}

async fn send_json(
    host: State<'_, Host>,
    command: impl FnOnce(digi_control::JsonReply) -> Command + Send + 'static,
) -> Result<Value, String> {
    let sender = host.handle.sender().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let (reply, response) = mpsc::channel();
        sender
            .send(command(reply))
            .map_err(|_| "emulator actor stopped")?;
        response
            .recv()
            .map_err(|_| "emulator actor dropped reply")?
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn emu_diagnostics(host: State<'_, Host>, session_id: u64) -> Result<Value, String> {
    send_json(host, move |reply| Command::Diagnostics {
        session: session_id,
        reply,
    })
    .await
}

/// Writes an exported diagnostics report to `out/native/diagnostics/` under
/// the working directory (the repository root under `mise run emu`); the
/// webview cannot save `<a download>` links.
#[tauri::command]
fn emu_save_diagnostics(name: String, json: String) -> Result<String, String> {
    let dir = std::env::current_dir()
        .map_err(|e| e.to_string())?
        .join("out/native/diagnostics");
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let safe: String = name
        .chars()
        .filter(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.'))
        .collect();
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|e| e.to_string())?
        .as_secs();
    let path = dir.join(format!("{stamp}-{safe}"));
    std::fs::write(&path, json).map_err(|e| e.to_string())?;
    Ok(path.display().to_string())
}

#[tauri::command]
async fn emu_audio_status(host: State<'_, Host>, session_id: u64) -> Result<Value, String> {
    send_json(host, move |reply| Command::AudioStatus {
        session: session_id,
        reply,
    })
    .await
}

#[tauri::command]
async fn emu_load(
    host: State<'_, Host>,
    request: tauri::ipc::Request<'_>,
) -> Result<Snapshot, String> {
    let session_id = request
        .headers()
        .get("x-session-id")
        .and_then(|value| value.to_str().ok())
        .ok_or("missing x-session-id")?
        .parse::<u64>()
        .map_err(|_| "invalid x-session-id")?;
    let bytes = match request.body() {
        tauri::ipc::InvokeBody::Raw(bytes) => bytes.clone(),
        _ => return Err("firmware must use a raw byte body".into()),
    };
    send(host, move |reply| Command::Load {
        session: session_id,
        bytes,
        card: None,
        reply,
    })
    .await
}
#[tauri::command]
async fn emu_startup(host: State<'_, Host>) -> Result<Snapshot, String> {
    send(host, |reply| Command::Startup { reply }).await
}
#[tauri::command]
async fn emu_restart(
    host: State<'_, Host>,
    session_id: u64,
    next_session_id: u64,
) -> Result<Snapshot, String> {
    send(host, move |reply| Command::Restart {
        session: session_id,
        next_session: next_session_id,
        reply,
    })
    .await
}
#[tauri::command]
async fn emu_step(host: State<'_, Host>, session_id: u64, budget: u32) -> Result<Snapshot, String> {
    send(host, move |reply| Command::Step {
        session: session_id,
        budget,
        reply,
    })
    .await
}
#[tauri::command]
async fn emu_button(
    host: State<'_, Host>,
    session_id: u64,
    code: u8,
    down: bool,
) -> Result<Snapshot, String> {
    send(host, move |reply| Command::Button {
        session: session_id,
        code,
        down,
        reply,
    })
    .await
}
#[tauri::command]
async fn emu_turn(
    host: State<'_, Host>,
    session_id: u64,
    encoder: u8,
    detents: i32,
) -> Result<Snapshot, String> {
    send(host, move |reply| Command::Turn {
        session: session_id,
        encoder,
        detents,
        reply,
    })
    .await
}
#[tauri::command]
async fn emu_tap(
    host: State<'_, Host>,
    session_id: u64,
    code: u8,
    hold: Option<u32>,
) -> Result<Snapshot, String> {
    send(host, move |reply| Command::Tap {
        session: session_id,
        code,
        hold: hold.unwrap_or(40_000_000),
        reply,
    })
    .await
}
#[tauri::command]
async fn emu_stop(host: State<'_, Host>, session_id: u64) -> Result<(), String> {
    match send(host, move |reply| Command::Stop {
        session: session_id,
        reply,
    })
    .await
    {
        Ok(_) => Ok(()),
        Err(error) if error == "No firmware selected" => Ok(()),
        Err(error) => Err(error),
    }
}

/// A header value the webview percent-encoded (`encodeURIComponent`).
fn percent_decode(text: &str) -> Option<String> {
    let bytes = text.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' {
            let hex = text.get(i + 1..i + 3)?;
            out.push(u8::from_str_radix(hex, 16).ok()?);
            i += 3;
        } else {
            out.push(bytes[i]);
            i += 1;
        }
    }
    String::from_utf8(out).ok()
}

/// Forget the staged sample files (the start of a new folder choice).
#[tauri::command]
async fn emu_card_reset(host: State<'_, Host>) -> Result<(), String> {
    let dir = host.staging.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).map_err(|e| e.to_string())
    })
    .await
    .map_err(|e| e.to_string())?
}

/// Stage one chosen `.wav` file: the raw body, its name in `x-name`.
#[tauri::command]
async fn emu_card_stage(
    host: State<'_, Host>,
    request: tauri::ipc::Request<'_>,
) -> Result<(), String> {
    let name = request
        .headers()
        .get("x-name")
        .and_then(|value| value.to_str().ok())
        .and_then(percent_decode)
        .ok_or("missing x-name")?;
    // A bare file name: the folder choice is flat, and nothing may escape the staging directory.
    if name.is_empty()
        || name.starts_with('.')
        || name.contains(['/', '\\', '\0'])
        || !name.to_ascii_lowercase().ends_with(".wav")
    {
        return Err(format!("not a sample file name: {name}"));
    }
    let bytes = match request.body() {
        tauri::ipc::InvokeBody::Raw(bytes) => bytes.clone(),
        _ => return Err("sample files must use a raw byte body".into()),
    };
    let dir = host.staging.clone();
    tauri::async_runtime::spawn_blocking(move || {
        std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
        std::fs::write(dir.join(name), bytes).map_err(|e| e.to_string())
    })
    .await
    .map_err(|e| e.to_string())?
}

/// Build the card of the staged files and restart the DT2 on it; progress is
/// read with `emu_card_status`.
#[tauri::command]
async fn emu_card_load(host: State<'_, Host>, name: Option<String>) -> Result<Value, String> {
    let (handle, dir) = (host.handle.clone(), host.staging.clone());
    tauri::async_runtime::spawn_blocking(move || {
        handle
            .call(
                "card.load",
                serde_json::json!({
                    "dir": dir.display().to_string(), "wait": false, "name": name,
                }),
            )
            .map_err(|e| e.message)
    })
    .await
    .map_err(|e| e.to_string())?
}

#[tauri::command]
async fn emu_card_status(host: State<'_, Host>) -> Result<Value, String> {
    let handle = host.handle.clone();
    tauri::async_runtime::spawn_blocking(move || {
        handle
            .call("card.status", serde_json::json!({}))
            .map_err(|e| e.message)
    })
    .await
    .map_err(|e| e.to_string())?
}

/// The desktop `--scenario` mode ends the app when the controller reports the
/// scenario done: write the report (to `--report` or stdout) and exit.
fn exit_on_scenario_done(handle: &Handle, report: Option<PathBuf>) {
    let events = handle
        .events()
        .subscribe(Some(vec!["scenario.done".into()]));
    std::thread::spawn(move || {
        let Ok(event) = events.recv() else { return };
        let text = match serde_json::to_string_pretty(&event["report"]) {
            Ok(text) => text,
            Err(error) => {
                eprintln!("report: {error}");
                process::exit(1)
            }
        };
        match report {
            Some(path) => {
                let written = path
                    .parent()
                    .map_or(Ok(()), std::fs::create_dir_all)
                    .and_then(|()| std::fs::write(&path, text));
                if let Err(error) = written {
                    eprintln!("report: {error}");
                    process::exit(1)
                }
            }
            None => println!("{text}"),
        }
        process::exit(0)
    });
}

fn main() {
    #[cfg(feature = "coupled-audio")]
    if std::env::args_os()
        .nth(1)
        .is_some_and(|a| a == "--pgo-train")
    {
        match desktop_runtime::pgo_training_workload() {
            Ok(()) => {
                println!("pgo_train=ok");
                process::exit(0)
            }
            Err(error) => {
                println!("pgo_train=failed: {error}");
                process::exit(1)
            }
        }
    }
    let startup = parse_args().unwrap_or_else(|error| {
        eprintln!("{error}");
        process::exit(2)
    });
    let run_dir = startup
        .run_dir
        .clone()
        .unwrap_or_else(|| PathBuf::from("out/native/run"));
    let mut config = Config::new(startup.audio.clone());
    config.mode = if startup.paused {
        Mode::Paused
    } else if startup.scenario.is_some() {
        // Scenario ticks run in the webview's `emu_step`.
        Mode::Ui
    } else {
        Mode::Free
    };
    config.run_dir = run_dir.clone();
    // Mount the last card again at start (not for scenarios, which must not depend on earlier runs).
    config.remember_card = startup.scenario.is_none();
    let run_dir_for_staging = run_dir.clone();
    config.on_quit = Some(Box::new(|| {
        // Let the socket thread write the reply first.
        std::thread::sleep(std::time::Duration::from_millis(200));
        process::exit(0)
    }));
    config.scenario = startup.scenario.as_deref().map(|spec| ScenarioSetup {
        scenario: load_scenario(spec).unwrap_or_else(|error| {
            eprintln!("{error}");
            process::exit(2)
        }),
    });
    let has_scenario = config.scenario.is_some();
    let (handle, _controller) = spawn(config);
    if has_scenario {
        exit_on_scenario_done(&handle, startup.report.clone());
    }
    if let Some(path) = startup.syx.clone() {
        let bytes = std::fs::read(&path).unwrap_or_else(|error| {
            eprintln!("firmware input: {error}");
            process::exit(2)
        });
        let (reply, response) = mpsc::channel();
        handle
            .sender()
            .send(Command::Load {
                session: 1,
                bytes,
                card: startup.card_image.clone(),
                reply,
            })
            .unwrap_or_else(|error| {
                eprintln!("emulator startup: {error}");
                process::exit(2)
            });
        if let Err(error) = response
            .recv()
            .unwrap_or_else(|error| Err(error.to_string()))
        {
            eprintln!("emulator startup: {error}");
            process::exit(2);
        }
    }
    #[cfg(unix)]
    // The control socket is on unless `--no-control` (design question 3).
    let _server = if startup.control != Some(false) {
        match digi_control::server::serve(
            Dispatcher::new(handle.clone(), "digiemu-desktop"),
            digi_control::server::ServerOptions { run_dir },
            "digiemu-desktop",
        ) {
            Ok(server) => {
                eprintln!("control socket: {}", server.socket.display());
                Some(server)
            }
            Err(error) => {
                // The emulator still runs; only scripted control is lost.
                eprintln!("control socket: {error}");
                None
            }
        }
    } else {
        None
    };
    #[cfg(not(unix))]
    let _ = (&run_dir, Dispatcher::new);
    let host = Host {
        handle,
        staging: run_dir_for_staging.join("cards").join("staging"),
    };
    tauri::Builder::default()
        .manage(host)
        .invoke_handler(tauri::generate_handler![
            emu_diagnostics,
            emu_save_diagnostics,
            emu_audio_status,
            emu_startup,
            emu_load,
            emu_restart,
            emu_step,
            emu_button,
            emu_turn,
            emu_tap,
            emu_stop,
            emu_card_reset,
            emu_card_stage,
            emu_card_load,
            emu_card_status
        ])
        .run(tauri::generate_context!())
        .expect("tauri runtime error");
}

#[cfg(test)]
mod cli_tests {
    use super::*;
    use desktop_runtime::{AudioMode, AudioOptions};

    fn parse(args: &[&str]) -> Result<StartupArgs, String> {
        cli::parse_values(
            args.iter().map(std::ffi::OsString::from),
            cfg!(feature = "coupled-audio"),
        )
    }

    #[test]
    fn default_is_cf_only_and_card_path_is_preserved() {
        assert!(parse(&[]).unwrap().audio.is_none());
        let args = parse(&["file.syx", "--card-image", "card.img"]).unwrap();
        assert_eq!(args.syx, Some("file.syx".into()));
        assert_eq!(args.card_image, Some("card.img".into()));
        assert!(args.audio.is_none());
    }
    #[test]
    fn coupled_requires_all_private_inputs_and_valid_buffer() {
        for args in [
            &["--coupled"][..],
            &["x", "--coupled"],
            &["x", "--dsp-state", "state"],
            &["--unknown"],
            &["x", "--audio-buffer", "NaN"],
            &["x", "--audio-buffer", "-1"],
            &["x", "--audio-buffer", "11"],
        ] {
            assert!(parse(args).is_err(), "{args:?}");
        }
        let parsed = parse(&[
            "x",
            "--coupled",
            "--dsp-image",
            "image",
            "--dsp-state",
            "state",
            "--cf-snapshot",
            "snapshot",
            "--no-audio",
            "--audio-buffer",
            "0.1",
        ])
        .unwrap();
        let audio = parsed.audio.unwrap();
        assert_eq!(audio.image, PathBuf::from("image"));
        assert_eq!(audio.buffer_seconds, 0.1);
        assert!(!audio.playback);
    }

    #[test]
    fn audio_mode_parses_and_requires_coupled() {
        let base = [
            "x",
            "--auto-coupled",
            "--dsp-image",
            "image",
            "--dsp-state",
            "state",
            "--cf-snapshot",
            "snapshot",
        ];
        let with = |mode: &'static str| {
            let mut args = base.to_vec();
            args.extend(["--audio-mode", mode]);
            parse(&args)
        };
        assert_eq!(
            with("stretch").unwrap().audio.unwrap().audio_mode,
            AudioMode::Stretch
        );
        assert!(with("loud").is_err());
        assert!(parse(&["x", "--audio-mode", "gap"]).is_err());
    }

    #[test]
    fn tap_holds_for_the_requested_guest_instructions_across_chunks() {
        let syx = std::env::var_os("DIGI_COUPLED_SYX")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("../../../firmware/Digitone_II_OS1.11.syx"));
        let Ok(bytes) = std::fs::read(syx) else {
            return;
        };
        let (handle, _join) = spawn(Config::new(None));
        let sender = handle.sender().clone();
        let (load_reply, load_response) = mpsc::channel();
        sender
            .send(Command::Load {
                session: 1,
                bytes,
                card: None,
                reply: load_reply,
            })
            .unwrap();
        let before = load_response.recv().unwrap().unwrap().status.icount;
        let (tap_reply, tap_response) = mpsc::channel();
        sender
            .send(Command::Tap {
                session: 1,
                code: 25,
                hold: 250_001,
                reply: tap_reply,
            })
            .unwrap();
        let after = tap_response.recv().unwrap().unwrap().status.icount;
        assert!(after - before >= 250_001);
    }

    #[test]
    fn audio_status_validates_session_and_reports_no_coupled_audio_as_null() {
        let (handle, controller) = spawn(Config::new(None));
        let sender = handle.sender().clone();
        let (missing_reply, missing_response) = mpsc::channel();
        sender
            .send(Command::AudioStatus {
                session: 0,
                reply: missing_reply,
            })
            .unwrap();
        assert_eq!(
            missing_response.recv().unwrap().unwrap_err(),
            "No firmware selected"
        );
        let (stale_reply, stale_response) = mpsc::channel();
        sender
            .send(Command::AudioStatus {
                session: 1,
                reply: stale_reply,
            })
            .unwrap();
        assert_eq!(
            stale_response.recv().unwrap().unwrap_err(),
            "stale emulator session"
        );

        let syx = std::env::var_os("DIGI_COUPLED_SYX")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("../../../firmware/Digitone_II_OS1.11.syx"));
        let Ok(bytes) = std::fs::read(syx) else {
            drop((sender, handle));
            controller.join().unwrap();
            return;
        };
        let (load_reply, load_response) = mpsc::channel();
        sender
            .send(Command::Load {
                session: 1,
                bytes,
                card: None,
                reply: load_reply,
            })
            .unwrap();
        load_response.recv().unwrap().unwrap();
        let (status_reply, status_response) = mpsc::channel();
        sender
            .send(Command::AudioStatus {
                session: 1,
                reply: status_reply,
            })
            .unwrap();
        assert_eq!(status_response.recv().unwrap().unwrap(), Value::Null);
        drop((sender, handle));
        controller.join().unwrap();
    }

    #[test]
    #[ignore = "private DN2 ready fixture and generated DSP"]
    fn startup_returns_the_coupled_restore_frame_once() {
        let (Some(fixture_dir), Some(syx)) = (
            std::env::var_os("DIGI_COUPLED_FIXTURES").map(PathBuf::from),
            std::env::var_os("DIGI_COUPLED_SYX").map(PathBuf::from),
        ) else {
            return;
        };
        let Ok(bytes) = std::fs::read(syx) else {
            return;
        };
        let audio = AudioOptions {
            image: fixture_dir.join("digi-audio-dn2-image.bin"),
            state: fixture_dir.join("digi-audio-m5.snap.dsp"),
            snapshot: fixture_dir.join("digi-audio-m5.snap"),
            buffer_seconds: 0.0,
            playback: false,
            automatic: false,
            expected_syx: None,
            link_timing: false,
            audio_mode: AudioMode::default(),
            dsp_patch: None,
            zero: false,
        };
        let (handle, controller) = spawn(Config::new(Some(audio)));
        let sender = handle.sender().clone();
        let (load_reply, load_response) = mpsc::channel();
        sender
            .send(Command::Load {
                session: 1,
                bytes,
                card: None,
                reply: load_reply,
            })
            .unwrap();
        let loaded = load_response.recv().unwrap().unwrap();
        let frame = loaded
            .frame
            .clone()
            .expect("load should observe restored LCD");
        assert!(frame.iter().any(|byte| *byte != 0));
        let revision = loaded.status.frame_revision;
        let (startup_reply, startup_response) = mpsc::channel();
        sender
            .send(Command::Startup {
                reply: startup_reply,
            })
            .unwrap();
        let startup = startup_response.recv().unwrap().unwrap();
        assert_eq!(startup.frame, Some(frame));
        assert_eq!(startup.status.frame_revision, revision);
        let (second_reply, second_response) = mpsc::channel();
        sender
            .send(Command::Startup {
                reply: second_reply,
            })
            .unwrap();
        assert!(second_response.recv().unwrap().unwrap().frame.is_none());
        drop((sender, handle));
        controller.join().unwrap();
    }
}
