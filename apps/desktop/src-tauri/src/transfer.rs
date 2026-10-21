//! The Transfer panel's backend: two thin commands that run the Python tool `tools/device/dktransfer.py` against this workbench's own
//! control socket (`list --emu --json`, `install ... --emu --json`). The tool owns the logic (which modules pack for the target, build,
//! pack, upload, SELECT, RESTART, wait); this file only starts it and hands its output lines to the panel. An install runs in the
//! background; its JSON lines are polled with a cursor, like `mod_progress`.
use std::{
    io::{BufRead, BufReader},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::{Arc, Mutex},
};

use serde_json::{Value, json};
use tauri::State;

#[derive(Default)]
struct Job {
    running: bool,
    events: Vec<Value>,
}

#[derive(Default)]
pub struct Transfer {
    job: Arc<Mutex<Job>>,
}

/// The arguments after `-m tools.device.dktransfer` for this workbench: its socket, JSON output, then the subcommand.
fn tool_args(socket: &Path, sub: &[String]) -> Vec<String> {
    let mut a: Vec<String> = ["--emu", "--socket"].map(String::from).into();
    a.push(socket.display().to_string());
    a.push("--json".into());
    a.extend(sub.iter().cloned());
    a
}

/// `python -m tools.device.dktransfer ARGS` from the repository root: the checkout's virtualenv when there is one, else `uv run`.
fn tool_command(root: &Path, args: &[String]) -> Command {
    let venv = root.join(".venv/bin/python");
    let mut c = if venv.is_file() {
        Command::new(venv)
    } else {
        let mut c = Command::new("uv");
        c.args(["run", "python"]);
        c
    };
    c.args(["-m", "tools.device.dktransfer"]).args(args).current_dir(root);
    let py = std::env::var_os("PYTHONPATH").unwrap_or_default();
    let mut paths = vec![root.to_path_buf()];
    paths.extend(std::env::split_paths(&py).filter(|p| p != root));
    if let Ok(joined) = std::env::join_paths(paths) {
        c.env("PYTHONPATH", joined);
    }
    c
}

fn socket_and_root(host: &crate::Host) -> Result<(PathBuf, PathBuf), String> {
    let socket = host
        .socket
        .clone()
        .ok_or("the control socket is off (the app was started with --no-control): Transfer needs it")?;
    let socket = std::fs::canonicalize(&socket).unwrap_or(socket);
    let root = digi_control::runtime_patch::find_root(None)?;
    Ok((socket, root))
}

/// Run `cmd`, keep each JSON line of its stdout in `job` and flag the end. A non-zero exit without an `error` event adds one with
/// the tail of stderr.
fn run_job(mut cmd: Command, job: Arc<Mutex<Job>>) {
    cmd.stdout(Stdio::piped()).stderr(Stdio::piped());
    let push = |v: Value| job.lock().unwrap_or_else(|e| e.into_inner()).events.push(v);
    match cmd.spawn() {
        Err(e) => push(json!({"event": "error", "message": format!("cannot start the tool: {e}")})),
        Ok(mut child) => {
            let err = child.stderr.take().map(|s| {
                std::thread::spawn(move || BufReader::new(s).lines().map_while(Result::ok).collect::<Vec<_>>())
            });
            let mut failed = false;
            for line in BufReader::new(child.stdout.take().unwrap()).lines().map_while(Result::ok) {
                if let Ok(v) = serde_json::from_str::<Value>(&line) {
                    failed |= v["event"] == "error";
                    push(v);
                }
            }
            let status = child.wait();
            let tail = err.and_then(|h| h.join().ok()).unwrap_or_default();
            if !failed && !status.map(|s| s.success()).unwrap_or(false) {
                let why = tail.iter().rev().find(|l| !l.trim().is_empty()).cloned().unwrap_or_else(|| "the tool failed".into());
                push(json!({"event": "error", "message": why}));
            }
        }
    }
    job.lock().unwrap_or_else(|e| e.into_inner()).running = false;
}

/// The merged module list (`dktransfer list`): the JSON document, or the tool's error message.
#[tauri::command]
pub async fn xfer_list(host: State<'_, crate::Host>, xfer: State<'_, Transfer>) -> Result<Value, String> {
    let (socket, root) = socket_and_root(&host)?;
    if xfer.job.lock().unwrap_or_else(|e| e.into_inner()).running {
        return Err("an install is running".into());
    }
    tauri::async_runtime::spawn_blocking(move || {
        let job = Arc::new(Mutex::new(Job { running: true, events: vec![] }));
        run_job(tool_command(&root, &tool_args(&socket, &["list".into()])), job.clone());
        let events = std::mem::take(&mut job.lock().unwrap().events);
        for v in &events {
            if v["event"] == "error" {
                return Err(v["message"].as_str().unwrap_or("the tool failed").to_string());
            }
        }
        events.into_iter().next().ok_or_else(|| "the tool printed nothing".to_string())
    })
    .await
    .map_err(|e| e.to_string())?
}

/// Start a background `dktransfer` job (`install ...`, `restart ...`); its progress is read with `xfer_events`.
fn start_job(host: &crate::Host, xfer: &Transfer, sub: Vec<String>) -> Result<(), String> {
    let (socket, root) = socket_and_root(host)?;
    {
        let mut j = xfer.job.lock().unwrap_or_else(|e| e.into_inner());
        if j.running {
            return Err("a transfer is already running".into());
        }
        *j = Job { running: true, events: vec![] };
    }
    let cmd = tool_command(&root, &tool_args(&socket, &sub));
    let job = xfer.job.clone();
    std::thread::spawn(move || run_job(cmd, job));
    Ok(())
}

/// Start `dktransfer install MODULES --keep KEEP`: build, upload, verify and select (no restart).
#[tauri::command]
pub fn xfer_install(
    host: State<'_, crate::Host>,
    xfer: State<'_, Transfer>,
    modules: Vec<String>,
    keep: Vec<String>,
) -> Result<(), String> {
    if modules.is_empty() {
        return Err("tick at least one module".into());
    }
    let mut sub: Vec<String> = vec!["install".into()];
    sub.extend(modules);
    if !keep.is_empty() {
        sub.push("--keep".into());
        sub.extend(keep);
    }
    start_job(&host, &xfer, sub)
}

/// Start `dktransfer restart MODULES`: RESTART, wait for the boot, report which modules loaded.
#[tauri::command]
pub fn xfer_restart(host: State<'_, crate::Host>, xfer: State<'_, Transfer>, modules: Vec<String>) -> Result<(), String> {
    let mut sub: Vec<String> = vec!["restart".into()];
    sub.extend(modules);
    start_job(&host, &xfer, sub)
}

/// The install's events from index `since` on, the next index and whether it still runs.
#[tauri::command]
pub fn xfer_events(xfer: State<'_, Transfer>, since: usize) -> Value {
    let j = xfer.job.lock().unwrap_or_else(|e| e.into_inner());
    let from = since.min(j.events.len());
    json!({"events": j.events[from..], "next": j.events.len(), "running": j.running})
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_tool_is_told_this_workbenchs_socket_and_json() {
        let a = tool_args(Path::new("/r/out/native/run/7.sock"), &["install".into(), "digikit/pluck".into(), "--keep".into(), "modules/a.dkm".into()]);
        assert_eq!(
            a,
            ["--emu", "--socket", "/r/out/native/run/7.sock", "--json", "install", "digikit/pluck", "--keep", "modules/a.dkm"]
        );
    }

    fn job_of(script: &str) -> Vec<Value> {
        let job = Arc::new(Mutex::new(Job { running: true, events: vec![] }));
        let mut c = Command::new("/bin/sh");
        c.args(["-c", script]);
        run_job(c, job.clone());
        let j = job.lock().unwrap();
        assert!(!j.running);
        j.events.clone()
    }

    #[test]
    fn json_lines_are_kept_and_other_lines_dropped() {
        let ev = job_of(r#"echo '{"event":"start"}'; echo noise; echo '{"event":"done","loaded":[]}'"#);
        assert_eq!(ev.iter().map(|e| e["event"].as_str().unwrap()).collect::<Vec<_>>(), ["start", "done"]);
    }

    #[test]
    fn a_failing_tool_ends_with_an_error_event() {
        let ev = job_of(r#"echo '{"event":"start"}'; echo 'error: boom' >&2; exit 1"#);
        assert_eq!(ev.last().unwrap(), &json!({"event": "error", "message": "error: boom"}));
        // a tool that reported its own error is not reported twice
        let ev = job_of(r#"echo '{"event":"error","message":"paused"}'; exit 1"#);
        assert_eq!(ev.len(), 1);
    }
}
