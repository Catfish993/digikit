//! The Modules panel's backend: the module upload protocol (`dkupload`, docs/design/digikit-upload.md) to the
//! running emulator through the controller's `midi.usb.in` / `midi.usb.out`. Nothing here talks to a MIDI device.
use std::{
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};

use digi_control::Handle;
use dkupload::{Error, Progress, Session, Transport, codec, proto};
use serde::Serialize;
use serde_json::{Value, json};
use tauri::State;

/// The emulator's USB MIDI path as a transport. Messages are handed over in the high-speed model (384 bytes per 16,500
/// instructions, a microframe), as the kept upload scenarios do; the default (64 bytes per 132,000 instructions, a
/// full-speed frame) makes a 32 KiB chunk take longer to arrive. The reply timeout stays long: END reads the file back.
const USB_CHUNK: u32 = 384;
const USB_PERIOD: u32 = 16_500;

/// The wait of the INFO probe that finds out whether a runtime answers at all.
const QUICK_TIMEOUT: Duration = Duration::from_secs(1);

pub struct EmuTransport {
    handle: Handle,
    rx: Vec<u8>,
}

impl EmuTransport {
    pub fn new(handle: Handle) -> Self {
        EmuTransport { handle, rx: Vec::new() }
    }

    /// Take one whole `F0 .. F7` message out of the received bytes.
    fn take_message(&mut self) -> Option<Vec<u8>> {
        let start = self.rx.iter().position(|&b| b == 0xF0);
        let Some(start) = start else {
            self.rx.clear();
            return None;
        };
        self.rx.drain(..start);
        let end = self.rx.iter().position(|&b| b == 0xF7)?;
        Some(self.rx.drain(..=end).collect())
    }
}

fn from_hex(hex: &str) -> Vec<u8> {
    hex.as_bytes()
        .chunks(2)
        .filter_map(|c| u8::from_str_radix(std::str::from_utf8(c).ok()?, 16).ok())
        .collect()
}

impl Transport for EmuTransport {
    fn send(&mut self, msg: &[u8]) -> Result<(), String> {
        self.handle
            .call("midi.usb.in", json!({ "bytes": proto::hex(msg), "chunk": USB_CHUNK, "period": USB_PERIOD }))
            .map(drop)
            .map_err(|e| e.message)
    }

    fn recv(&mut self, timeout: Duration) -> Result<Option<Vec<u8>>, String> {
        let deadline = Instant::now() + timeout;
        loop {
            if let Some(m) = self.take_message() {
                return Ok(Some(m));
            }
            let reply = self.handle.call("midi.usb.out", json!({})).map_err(|e| e.message)?;
            self.rx.extend(from_hex(reply["hex"].as_str().unwrap_or("")));
            if let Some(m) = self.take_message() {
                return Ok(Some(m));
            }
            if Instant::now() >= deadline {
                return Ok(None);
            }
            std::thread::sleep(Duration::from_millis(10));
        }
    }
}

#[derive(Clone, Default, Serialize)]
pub struct Job {
    running: bool,
    path: String,
    done: u64,
    total: u64,
}

#[derive(Default)]
pub struct Modules {
    /// One device conversation at a time.
    busy: Arc<Mutex<()>>,
    job: Arc<Mutex<Job>>,
    cancel: Arc<AtomicBool>,
}

fn error_json(e: &Error) -> String {
    let (kind, status, free) = match e {
        Error::Device { status, free_bytes } => ("device", Some(*status), *free_bytes),
        Error::Transport(_) => ("transport", None, None),
        Error::Timeout => ("timeout", None, None),
        Error::Protocol(_) => ("protocol", None, None),
        Error::Request(_) => ("request", None, None),
        Error::Cancelled => ("cancelled", None, None),
    };
    json!({
        "error": e.to_string(), "kind": kind, "status": status.map(|s| s.name()), "free_bytes": free,
    })
    .to_string()
}

fn session(handle: &Handle) -> Session<EmuTransport> {
    let mut s = Session::new(EmuTransport::new(handle.clone()));
    s.timeout = Duration::from_secs(6);
    s
}

/// Run `f` after the conversations that hold `busy` before it: requests queue, they are not refused. (The panel
/// asks for INFO and LIST at once; a refused second call showed "another transfer is running".) `mod_cancel` does not
/// take this lock, so it still reaches a running put.
fn one_at_a_time<T>(busy: &Mutex<()>, f: impl FnOnce() -> T) -> T {
    let _guard = busy.lock().unwrap_or_else(|e| e.into_inner());
    f()
}

/// Run `f` on a blocking thread with a session, one at a time.
async fn with_session<T: Send + 'static>(
    handle: Handle,
    modules: &Modules,
    f: impl FnOnce(&mut Session<EmuTransport>) -> Result<T, Error> + Send + 'static,
) -> Result<T, String> {
    let busy = modules.busy.clone();
    tauri::async_runtime::spawn_blocking(move || {
        one_at_a_time(&busy, || {
            let mut s = session(&handle);
            f(&mut s).map_err(|e| error_json(&e))
        })
    })
    .await
    .map_err(|e| e.to_string())?
}

fn entries_json(list: Vec<proto::Entry>) -> Value {
    list.into_iter()
        .map(|e| json!({"path": e.path, "size": e.size, "sha256": proto::hex(&e.sha256)}))
        .collect()
}

#[tauri::command]
pub async fn mod_info(
    host: State<'_, crate::Host>,
    modules: State<'_, Modules>,
    quick: Option<bool>,
) -> Result<Value, String> {
    let quick = quick.unwrap_or(false);
    with_session(host.handle.clone(), &modules, move |s| {
        if quick {
            // A probe for a device without the runtime (stock firmware never answers): one short try.
            (s.timeout, s.retries) = (QUICK_TIMEOUT, 0);
        }
        let i = s.info()?;
        Ok(json!({
            "protocol": i.protocol, "runtime_version": i.runtime_version, "port": i.port,
            "free_bytes": i.free_bytes, "max_chunk": i.max_chunk,
            "safe_mode": i.safe_mode(), "upload_open": i.upload_open(),
        }))
    })
    .await
}

#[tauri::command]
pub async fn mod_list(host: State<'_, crate::Host>, modules: State<'_, Modules>) -> Result<Value, String> {
    with_session(host.handle.clone(), &modules, |s| s.list().map(entries_json)).await
}

#[tauri::command]
pub async fn mod_delete(
    host: State<'_, crate::Host>,
    modules: State<'_, Modules>,
    path: String,
) -> Result<(), String> {
    with_session(host.handle.clone(), &modules, move |s| s.delete(&path)).await
}

#[tauri::command]
pub async fn mod_clear(host: State<'_, crate::Host>, modules: State<'_, Modules>) -> Result<(), String> {
    with_session(host.handle.clone(), &modules, |s| s.clear()).await
}

/// Upload one file: the raw body is the file, `x-path` its store path (percent-encoded). Progress is read with
/// `mod_progress` while this runs; `mod_cancel` stops it.
#[tauri::command]
pub async fn mod_put(
    host: State<'_, crate::Host>,
    modules: State<'_, Modules>,
    request: tauri::ipc::Request<'_>,
) -> Result<(), String> {
    let path = request
        .headers()
        .get("x-path")
        .and_then(|v| v.to_str().ok())
        .and_then(crate::percent_decode)
        .ok_or("missing x-path")?;
    let data = match request.body() {
        tauri::ipc::InvokeBody::Raw(bytes) => bytes.clone(),
        _ => return Err("the file must use a raw byte body".into()),
    };
    let (job, cancel) = (modules.job.clone(), modules.cancel.clone());
    cancel.store(false, Ordering::Relaxed);
    *job.lock().unwrap() = Job { running: true, path: path.clone(), done: 0, total: data.len() as u64 };
    let job_end = job.clone();
    let result = with_session(host.handle.clone(), &modules, move |s| {
        s.put(&path, data, &mut |p: Progress| {
            let mut j = job.lock().unwrap();
            (j.done, j.total) = (p.done, p.total);
            !cancel.load(Ordering::Relaxed)
        })
    })
    .await;
    job_end.lock().unwrap().running = false;
    result
}

#[tauri::command]
pub fn mod_progress(modules: State<'_, Modules>) -> Job {
    modules.job.lock().unwrap().clone()
}

#[tauri::command]
pub fn mod_cancel(modules: State<'_, Modules>) {
    modules.cancel.store(true, Ordering::Relaxed);
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn messages_are_cut_out_of_the_byte_stream() {
        let handle = digi_control::controller::spawn(digi_control::Config::new(None)).0;
        let mut t = EmuTransport::new(handle);
        t.rx = vec![0x90, 0x40, 0xF0, 1, 2];
        assert_eq!(t.take_message(), None);
        t.rx.extend([3, 0xF7, 0xF0, 9, 0xF7, 0x55]);
        assert_eq!(t.take_message(), Some(vec![0xF0, 1, 2, 3, 0xF7]));
        assert_eq!(t.take_message(), Some(vec![0xF0, 9, 0xF7]));
        assert_eq!(t.take_message(), None);
        assert!(t.rx.is_empty());
        assert_eq!(from_hex("f07d"), vec![0xF0, 0x7D]);
    }

    #[test]
    fn overlapping_calls_both_succeed_one_after_the_other() {
        let busy = Arc::new(Mutex::new(()));
        let inside = Arc::new(std::sync::atomic::AtomicU32::new(0));
        let run = || {
            let (busy, inside) = (busy.clone(), inside.clone());
            std::thread::spawn(move || {
                one_at_a_time(&busy, || {
                    assert_eq!(inside.fetch_add(1, Ordering::SeqCst), 0, "two calls at once");
                    std::thread::sleep(Duration::from_millis(50));
                    inside.fetch_sub(1, Ordering::SeqCst);
                    Ok::<_, String>(())
                })
            })
        };
        let (a, b) = (run(), run());
        assert_eq!((a.join().unwrap(), b.join().unwrap()), (Ok(()), Ok(())));
    }

    #[test]
    fn error_json_names_the_status() {
        let e = Error::Device { status: proto::Status::Full, free_bytes: Some(512) };
        let v: Value = serde_json::from_str(&error_json(&e)).unwrap();
        assert_eq!((v["status"].as_str(), v["free_bytes"].as_u64()), (Some("E_FULL"), Some(512)));
        let _ = codec::MAX_CHUNK;
    }
}
