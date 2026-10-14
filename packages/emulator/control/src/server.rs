//! The Unix-socket transport: NDJSON JSON-RPC at `<run_dir>/<pid>.sock`
//! (directory 0700, socket 0600) and the discovery file
//! `<run_dir>/current.json`. Local only; the file permissions are the
//! authentication.

use std::{
    fs,
    io::{self, BufRead, BufReader, Write},
    os::unix::{
        fs::PermissionsExt,
        net::{UnixListener, UnixStream},
    },
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    thread,
    time::{SystemTime, UNIX_EPOCH},
};

use serde_json::{Value, json};

use crate::{
    dispatch::Dispatcher,
    proto::{
        self, Params, RpcError, SCHEMA_VERSION, code, error_response, event_notification,
        ok_response,
    },
};

/// macOS limits a socket path to 104 bytes.
const MAX_SOCKET_PATH: usize = 100;

pub struct ServerOptions {
    pub run_dir: PathBuf,
}

/// Keeps the socket and discovery file alive; removes both on drop.
pub struct Server {
    pub socket: PathBuf,
    pub discovery: PathBuf,
    pid: u32,
}

impl Drop for Server {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.socket);
        if discovery_pid(&self.discovery) == Some(self.pid) {
            let _ = fs::remove_file(&self.discovery);
        }
    }
}

fn discovery_pid(path: &Path) -> Option<u32> {
    let text = fs::read_to_string(path).ok()?;
    let value: Value = serde_json::from_str(&text).ok()?;
    u32::try_from(value["pid"].as_u64()?).ok()
}

fn socket_path(run_dir: &Path, pid: u32) -> PathBuf {
    let preferred = run_dir.join(format!("{pid}.sock"));
    if preferred.as_os_str().len() <= MAX_SOCKET_PATH {
        return preferred;
    }
    let tmp = std::env::var_os("TMPDIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/tmp"));
    tmp.join(format!("digi-{pid}.sock"))
}

/// Remove sockets (and a discovery file) of emulators that are gone: a socket
/// nobody accepts on.
fn prune(run_dir: &Path, own: &Path) {
    let alive = |path: &Path| UnixStream::connect(path).is_ok();
    if let Ok(entries) = fs::read_dir(run_dir) {
        for entry in entries.flatten() {
            let path = entry.path();
            if path != own && path.extension().is_some_and(|e| e == "sock") && !alive(&path) {
                let _ = fs::remove_file(&path);
            }
        }
    }
    let discovery = run_dir.join("current.json");
    if let Ok(text) = fs::read_to_string(&discovery)
        && let Ok(value) = serde_json::from_str::<Value>(&text)
        && let Some(socket) = value["socket"].as_str()
        && Path::new(socket) != own
        && !alive(Path::new(socket))
    {
        let _ = fs::remove_file(&discovery);
    }
}

fn write_discovery(path: &Path, socket: &Path, host: &str, syx_sha256: Value, started: u64) {
    let body = json!({
        "pid": std::process::id(),
        "socket": socket.display().to_string(),
        "schema_version": SCHEMA_VERSION,
        "host": host,
        "syx_sha256": syx_sha256,
        "started": started,
    });
    let partial = path.with_extension("json.part");
    if fs::write(&partial, format!("{body}\n")).is_ok() {
        let _ = fs::rename(&partial, path);
    }
}

/// Start listening. The returned guard must be kept for as long as the
/// server should run; the threads end with the process.
pub fn serve(dispatcher: Dispatcher, options: ServerOptions, host: &str) -> io::Result<Server> {
    let run_dir = options.run_dir;
    fs::create_dir_all(&run_dir)?;
    fs::set_permissions(&run_dir, fs::Permissions::from_mode(0o700))?;
    let pid = std::process::id();
    let socket = socket_path(&run_dir, pid);
    prune(&run_dir, &socket);
    let _ = fs::remove_file(&socket);
    let listener = UnixListener::bind(&socket)?;
    fs::set_permissions(&socket, fs::Permissions::from_mode(0o600))?;
    let discovery = run_dir.join("current.json");
    let started = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_secs());
    write_discovery(&discovery, &socket, host, Value::Null, started);

    // Keep the discovery file's firmware hash current.
    {
        let (dispatcher, discovery, socket, host) = (
            dispatcher.clone(),
            discovery.clone(),
            socket.clone(),
            host.to_string(),
        );
        let events = dispatcher.handle().events().subscribe(Some(vec![
            "firmware.loaded".into(),
            "session.changed".into(),
        ]));
        thread::Builder::new()
            .name("digi-discovery".into())
            .spawn(move || {
                while events.recv().is_ok() {
                    // Let the controller finish what raised the event.
                    if let Ok(status) = dispatcher.handle().call("status", json!({})) {
                        let sha = status["firmware"]["syx_sha256"].clone();
                        write_discovery(&discovery, &socket, &host, sha, started);
                    }
                }
            })?;
    }

    thread::Builder::new()
        .name("digi-accept".into())
        .spawn(move || {
            for stream in listener.incoming() {
                let Ok(stream) = stream else { continue };
                let dispatcher = dispatcher.clone();
                let _ = thread::Builder::new()
                    .name("digi-client".into())
                    .spawn(move || client(stream, dispatcher));
            }
        })?;
    Ok(Server {
        socket,
        discovery,
        pid,
    })
}

fn send(writer: &Arc<Mutex<UnixStream>>, value: &Value) -> io::Result<()> {
    let mut line = value.to_string();
    line.push('\n');
    let mut stream = writer
        .lock()
        .map_err(|_| io::Error::other("client writer poisoned"))?;
    stream.write_all(line.as_bytes())
}

fn client(stream: UnixStream, dispatcher: Dispatcher) {
    let Ok(read_half) = stream.try_clone() else {
        return;
    };
    let writer = Arc::new(Mutex::new(stream));
    for line in BufReader::new(read_half).lines() {
        let Ok(line) = line else { break };
        if line.trim().is_empty() {
            continue;
        }
        let response = match proto::parse_request(&line) {
            Ok(request) if request.method == "events.subscribe" => {
                subscribe(&dispatcher, &writer, request)
            }
            Ok(request) => dispatcher.handle_request(request),
            Err(response) => Some(response),
        };
        if let Some(response) = response
            && send(&writer, &response).is_err()
        {
            break;
        }
    }
}

/// `events.subscribe {kinds?}`: answer, then push every matching event on this
/// connection as a notification until it closes.
fn subscribe(
    dispatcher: &Dispatcher,
    writer: &Arc<Mutex<UnixStream>>,
    request: proto::Request,
) -> Option<Value> {
    let id = request.id.clone().unwrap_or(Value::Null);
    let kinds = match request.params.get("kinds") {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Array(list)) => list
            .iter()
            .map(|k| k.as_str().map(str::to_string))
            .collect::<Option<Vec<_>>>()
            .map(Some)
            .ok_or_else(|| RpcError::bad_params("kinds must be strings")),
        Some(_) => Err(RpcError::bad_params("kinds must be an array")),
    };
    let _ = Params(&request.params);
    let kinds = match kinds {
        Ok(kinds) => kinds,
        Err(error) => return Some(error_response(id, &error)),
    };
    let events = dispatcher.handle().events().subscribe(kinds);
    let writer = Arc::clone(writer);
    let spawned = thread::Builder::new()
        .name("digi-events".into())
        .spawn(move || {
            while let Ok(event) = events.recv() {
                if send(&writer, &event_notification(event)).is_err() {
                    break;
                }
            }
        });
    Some(match spawned {
        Ok(_) => ok_response(id, json!({"subscribed": true})),
        Err(e) => error_response(id, &RpcError::new(code::IO, e.to_string())),
    })
}
