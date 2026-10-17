//! An in-memory device that follows the state rules of the spec (framing, upload state, offset and retry,
//! length, E_FULL, E_HASH). It does not model the store layout. For tests of the hosts, in Rust and (through
//! `wasm.rs`) in node.

use crate::codec::*;
use crate::proto::Status;
use crate::session::Transport;
use std::collections::VecDeque;
use std::time::Duration;

struct Upload {
    path: String,
    size: usize,
    sha: [u8; 32],
    data: Vec<u8>,
    last: Option<(usize, usize)>,
}

pub struct MockDevice {
    pub port: String,
    pub capacity: u64,
    used: u64,
    /// Sorted by path.
    pub files: Vec<(String, Vec<u8>)>,
    upload: Option<Upload>,
    pub safe_mode: bool,
    pub no_store: bool,
    /// Fault injection: the next n requests are lost before they are handled / the next n replies are lost.
    pub drop_requests: u32,
    pub drop_replies: u32,
    /// Commands handled, in order (cmd without the reply bit), for tests that check the traffic.
    pub log: Vec<u8>,
}

/// Room kept for the table, as the real store keeps it after the staged data.
const TABLE_ROOM: u64 = 4096;

impl MockDevice {
    pub fn new(capacity: u64) -> Self {
        MockDevice {
            port: "mock-0.0".into(),
            capacity,
            used: 0,
            files: Vec::new(),
            upload: None,
            safe_mode: false,
            no_store: false,
            drop_requests: 0,
            drop_replies: 0,
            log: Vec::new(),
        }
    }

    pub fn free_bytes(&self) -> u32 {
        self.capacity
            .saturating_sub(self.used + TABLE_ROOM)
            .min(u32::MAX as u64) as u32
    }

    fn reply(cmd: u8, seq: u8, status: Status, rest: &[u8]) -> Vec<u8> {
        let mut body = vec![status.code() as u8];
        body.extend(rest);
        encode(cmd, true, seq, &body)
    }

    /// Handle one request message; the reply, or `None` when it was dropped (or is not for this protocol).
    pub fn handle(&mut self, raw: &[u8]) -> Option<Vec<u8>> {
        if raw.len() < 8 || raw[..4] != HEAD[..4] || *raw.last()? != 0xF7 {
            return None;
        }
        if self.drop_requests > 0 {
            self.drop_requests -= 1;
            return None;
        }
        let (cmd, seq) = (raw[5], raw[6]);
        let reply = if raw[4] != HEAD[4] {
            Self::reply(cmd, seq, Status::Proto, &[])
        } else {
            match decode(raw) {
                Ok(m) if !m.reply => {
                    self.log.push(m.cmd);
                    self.dispatch(m.cmd, m.seq, &m.body)
                }
                _ => Self::reply(cmd, seq, Status::Msg, &[]),
            }
        };
        if self.drop_replies > 0 {
            self.drop_replies -= 1;
            return None;
        }
        Some(reply)
    }

    fn dispatch(&mut self, cmd: u8, seq: u8, body: &[u8]) -> Vec<u8> {
        let r = |s: Status| Self::reply(cmd, seq, s, &[]);
        match cmd {
            CMD_INFO => {
                let mut b = vec![0, 1, 0, 1];
                b.extend(self.port.as_bytes());
                b.push(0);
                b.extend(self.free_bytes().to_be_bytes());
                b.extend((MAX_CHUNK as u32).to_be_bytes());
                b.push(self.safe_mode as u8 | (self.upload.is_some() as u8) << 1);
                Self::reply(cmd, seq, Status::Ok, &b)
            }
            CMD_CLEAR => {
                if body.len() != 4
                    || u32::from_be_bytes([body[0], body[1], body[2], body[3]]) != CLEAR_MAGIC
                {
                    return r(Status::Msg);
                }
                self.files.clear();
                self.used = 0;
                self.upload = None;
                self.no_store = false;
                r(Status::Ok)
            }
            _ if self.no_store && cmd != CMD_ABORT => r(Status::NoStore),
            CMD_LIST => {
                if body.len() < 4 {
                    return r(Status::Msg);
                }
                let first = u32::from_be_bytes([body[0], body[1], body[2], body[3]]) as usize;
                let mut entries = Vec::new();
                let mut n = 0u8;
                for (p, d) in self.files.iter().skip(first) {
                    let mut e = p.as_bytes().to_vec();
                    e.push(0);
                    e.extend((d.len() as u32).to_be_bytes());
                    e.extend(sha256(d));
                    // 1 KiB on the wire: status + total + n (6 bytes) plus the entries, packed 8 for 7.
                    if encode(cmd, true, 0, &[&[0u8; 6][..], &entries, &e].concat()).len()
                        > MAX_REPLY_WIRE
                        || n == u8::MAX
                    {
                        break;
                    }
                    entries.extend(e);
                    n += 1;
                }
                let mut b = (self.files.len() as u32).to_be_bytes().to_vec();
                b.push(n);
                b.extend(entries);
                Self::reply(cmd, seq, Status::Ok, &b)
            }
            CMD_BEGIN => {
                let Some(nul) = body.iter().position(|&b| b == 0) else {
                    return r(Status::Msg);
                };
                if body.len() != nul + 1 + 4 + 32 {
                    return r(Status::Msg);
                }
                let Ok(path) = std::str::from_utf8(&body[..nul]) else {
                    return r(Status::Path);
                };
                if !path_ok(path) {
                    return r(Status::Path);
                }
                let size = u32::from_be_bytes(body[nul + 1..nul + 5].try_into().unwrap()) as usize;
                let mut sha = [0u8; 32];
                sha.copy_from_slice(&body[nul + 5..]);
                self.upload = None;
                let need = size.div_ceil(512) as u64 * 512;
                if need > self.free_bytes() as u64 {
                    return Self::reply(cmd, seq, Status::Full, &self.free_bytes().to_be_bytes());
                }
                self.upload = Some(Upload {
                    path: path.into(),
                    size,
                    sha,
                    data: Vec::new(),
                    last: None,
                });
                r(Status::Ok)
            }
            CMD_DATA => {
                let Some(up) = self.upload.as_mut() else {
                    return r(Status::State);
                };
                if body.len() < 4 {
                    return r(Status::Msg);
                }
                let offset = u32::from_be_bytes([body[0], body[1], body[2], body[3]]) as usize;
                let bytes = &body[4..];
                let len = bytes.len();
                if len == 0 || len > MAX_CHUNK {
                    return r(Status::Length);
                }
                if up.last == Some((offset, len)) {
                    return r(Status::Ok);
                }
                if offset != up.data.len() {
                    return r(Status::Offset);
                }
                if offset + len > up.size || (!len.is_multiple_of(512) && offset + len != up.size) {
                    return r(Status::Length);
                }
                up.data.extend(bytes);
                up.last = Some((offset, len));
                r(Status::Ok)
            }
            CMD_END => {
                let Some(up) = self.upload.take() else {
                    return r(Status::State);
                };
                if up.data.len() != up.size {
                    self.upload = Some(up);
                    return r(Status::Length);
                }
                if sha256(&up.data) != up.sha {
                    return r(Status::Hash);
                }
                self.used += up.size.div_ceil(512) as u64 * 512;
                self.files.retain(|(p, _)| *p != up.path);
                self.files.push((up.path, up.data));
                self.files.sort_by(|a, b| a.0.cmp(&b.0));
                r(Status::Ok)
            }
            CMD_DELETE => {
                let Some(nul) = body.iter().position(|&b| b == 0) else {
                    return r(Status::Msg);
                };
                let Ok(path) = std::str::from_utf8(&body[..nul]) else {
                    return r(Status::Path);
                };
                if !path_ok(path) {
                    return r(Status::Path);
                }
                let before = self.files.len();
                self.files.retain(|(p, _)| p != path);
                if self.files.len() == before {
                    r(Status::NoFile)
                } else {
                    r(Status::Ok)
                }
            }
            CMD_ABORT => {
                self.upload = None;
                r(Status::Ok)
            }
            _ => r(Status::Proto),
        }
    }
}

/// A `Transport` onto a `MockDevice`: a lost request or reply shows as a timeout at once.
pub struct MockTransport {
    pub dev: MockDevice,
    queue: VecDeque<Vec<u8>>,
}
impl MockTransport {
    pub fn new(dev: MockDevice) -> Self {
        MockTransport {
            dev,
            queue: VecDeque::new(),
        }
    }
}
impl Transport for MockTransport {
    fn send(&mut self, msg: &[u8]) -> Result<(), String> {
        if let Some(r) = self.dev.handle(msg) {
            self.queue.push_back(r);
        }
        Ok(())
    }
    fn recv(&mut self, _timeout: Duration) -> Result<Option<Vec<u8>>, String> {
        Ok(self.queue.pop_front())
    }
}
