//! A real MIDI port for the digikit SysEx protocol (the desktop app's second target next to the emulator). The page speaks the protocol itself
//! in TypeScript (`apps/web/src/modules/dkupload-core.ts`); this file only moves whole SysEx messages: it lists the ports, opens one by name
//! (input and output of the same name), sends a message and waits for the next one. No process is started. Nothing here knows a device: the
//! port is whatever the user picks.
use std::sync::{
    Arc, Mutex,
    mpsc::{Receiver, RecvTimeoutError, Sender, channel},
};
use std::time::Duration;

use midir::{Ignore, MidiInput, MidiInputConnection, MidiOutput, MidiOutputConnection};
use tauri::State;

/// A device's SysEx can reach the callback in pieces (CoreMIDI splits a long message); this puts the pieces together. Bytes outside a SysEx
/// message (clock, active sensing, notes) are dropped. A message that starts inside another one drops the unfinished one.
#[derive(Default)]
pub struct Sysex {
    open: Vec<u8>,
}

impl Sysex {
    /// Feed the bytes of one callback; returns the messages (`F0 ... F7`) it completed.
    pub fn feed(&mut self, bytes: &[u8]) -> Vec<Vec<u8>> {
        let mut done = Vec::new();
        for &b in bytes {
            if b == 0xF0 {
                self.open.clear();
                self.open.push(b);
            } else if self.open.is_empty() {
                continue;
            } else if b == 0xF7 {
                self.open.push(b);
                done.push(std::mem::take(&mut self.open));
            } else if b < 0x80 {
                self.open.push(b);
            } else if b < 0xF8 {
                // a status byte inside a SysEx ends it without F7: drop it
                self.open.clear();
            }
            // real-time bytes (0xF8 and up) may sit anywhere: ignored
        }
        done
    }
}

/// The names that are both an input and an output port, in the input's order.
pub fn common_ports(inputs: &[String], outputs: &[String]) -> Vec<String> {
    inputs
        .iter()
        .filter(|n| outputs.contains(n))
        .cloned()
        .collect()
}

struct Open {
    out: MidiOutputConnection,
    /// Keeps the input connection alive; dropping it closes the port.
    _input: MidiInputConnection<()>,
    rx: Arc<Mutex<Receiver<Vec<u8>>>>,
}

#[derive(Default)]
pub struct MidiPort(Mutex<Option<Open>>);

fn names_in(input: &MidiInput) -> Vec<String> {
    input
        .ports()
        .iter()
        .filter_map(|p| input.port_name(p).ok())
        .collect()
}

fn names_out(output: &MidiOutput) -> Vec<String> {
    output
        .ports()
        .iter()
        .filter_map(|p| output.port_name(p).ok())
        .collect()
}

/// The MIDI ports a device could be on: names that exist as input and as output.
#[tauri::command]
pub async fn midi_ports() -> Result<Vec<String>, String> {
    tauri::async_runtime::spawn_blocking(|| {
        let input = MidiInput::new("digikit").map_err(|e| e.to_string())?;
        let output = MidiOutput::new("digikit").map_err(|e| e.to_string())?;
        Ok(common_ports(&names_in(&input), &names_out(&output)))
    })
    .await
    .map_err(|e| e.to_string())?
}

/// Open the port called `name` (closing the open one first).
#[tauri::command]
pub async fn midi_open(state: State<'_, MidiPort>, name: String) -> Result<(), String> {
    let opened = tauri::async_runtime::spawn_blocking(move || -> Result<Open, String> {
        let mut input = MidiInput::new("digikit").map_err(|e| e.to_string())?;
        input.ignore(Ignore::None); // SysEx is what this is for
        let output = MidiOutput::new("digikit").map_err(|e| e.to_string())?;
        let in_port = input
            .ports()
            .into_iter()
            .find(|p| input.port_name(p).is_ok_and(|n| n == name))
            .ok_or_else(|| format!("no MIDI input port {name:?}"))?;
        let out_port = output
            .ports()
            .into_iter()
            .find(|p| output.port_name(p).is_ok_and(|n| n == name))
            .ok_or_else(|| format!("no MIDI output port {name:?}"))?;
        let (tx, rx): (Sender<Vec<u8>>, Receiver<Vec<u8>>) = channel();
        let mut assembler = Sysex::default();
        let conn = input
            .connect(
                &in_port,
                "digikit in",
                move |_, bytes, _| {
                    for m in assembler.feed(bytes) {
                        let _ = tx.send(m);
                    }
                },
                (),
            )
            .map_err(|e| e.to_string())?;
        let out = output
            .connect(&out_port, "digikit out")
            .map_err(|e| e.to_string())?;
        Ok(Open {
            out,
            _input: conn,
            rx: Arc::new(Mutex::new(rx)),
        })
    })
    .await
    .map_err(|e| e.to_string())??;
    *state.0.lock().map_err(|e| e.to_string())? = Some(opened);
    Ok(())
}

#[tauri::command]
pub fn midi_close(state: State<'_, MidiPort>) -> Result<(), String> {
    *state.0.lock().map_err(|e| e.to_string())? = None;
    Ok(())
}

/// Send one whole message (the raw body of the request).
#[tauri::command]
pub async fn midi_send(
    state: State<'_, MidiPort>,
    request: tauri::ipc::Request<'_>,
) -> Result<(), String> {
    let bytes = match request.body() {
        tauri::ipc::InvokeBody::Raw(bytes) => bytes.clone(),
        _ => return Err("a MIDI message must use a raw byte body".into()),
    };
    let mut guard = state.0.lock().map_err(|e| e.to_string())?;
    let open = guard.as_mut().ok_or("no MIDI port is open")?;
    open.out.send(&bytes).map_err(|e| e.to_string())
}

/// The next complete SysEx message, or an empty reply after `timeout_ms`.
#[tauri::command]
pub async fn midi_recv(
    state: State<'_, MidiPort>,
    timeout_ms: u64,
) -> Result<tauri::ipc::Response, String> {
    let rx = {
        let guard = state.0.lock().map_err(|e| e.to_string())?;
        guard.as_ref().ok_or("no MIDI port is open")?.rx.clone()
    };
    tauri::async_runtime::spawn_blocking(move || {
        let rx = rx.lock().map_err(|e| e.to_string())?;
        match rx.recv_timeout(Duration::from_millis(timeout_ms)) {
            Ok(m) => Ok(tauri::ipc::Response::new(m)),
            Err(RecvTimeoutError::Timeout) => Ok(tauri::ipc::Response::new(Vec::new())),
            Err(RecvTimeoutError::Disconnected) => Err("the MIDI port closed".to_string()),
        }
    })
    .await
    .map_err(|e| e.to_string())?
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_message_in_pieces_is_put_together_and_realtime_bytes_are_ignored() {
        let mut s = Sysex::default();
        assert!(s.feed(&[0xFE, 0xF8]).is_empty());
        assert!(s.feed(&[0xF0, 0x7D, 0x64]).is_empty());
        assert!(s.feed(&[0xF8, 0x6B, 0x01]).is_empty());
        assert_eq!(
            s.feed(&[0x02, 0xF7, 0xFE]),
            vec![vec![0xF0, 0x7D, 0x64, 0x6B, 0x01, 0x02, 0xF7]]
        );
        // two messages in one callback
        assert_eq!(
            s.feed(&[0xF0, 1, 0xF7, 0xF0, 2, 0xF7]),
            vec![vec![0xF0, 1, 0xF7], vec![0xF0, 2, 0xF7]]
        );
    }

    #[test]
    fn an_unfinished_message_is_dropped_when_another_starts_or_a_status_byte_comes() {
        let mut s = Sysex::default();
        assert_eq!(
            s.feed(&[0xF0, 1, 2, 0xF0, 3, 0xF7]),
            vec![vec![0xF0, 3, 0xF7]]
        );
        assert!(s.feed(&[0xF0, 1, 0x90, 2, 0xF7]).is_empty());
        assert_eq!(s.feed(&[0xF0, 9, 0xF7]), vec![vec![0xF0, 9, 0xF7]]);
    }

    #[test]
    fn only_names_that_are_both_input_and_output_are_offered() {
        let i = ["A".to_string(), "B".to_string(), "C".to_string()];
        let o = ["C".to_string(), "A".to_string()];
        assert_eq!(common_ports(&i, &o), vec!["A".to_string(), "C".to_string()]);
    }
}
