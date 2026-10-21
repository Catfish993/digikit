//! A session over a `Transport`: one request at a time, and uploads through `PutMachine`.

use crate::codec::*;
use crate::proto::*;
use crate::put::{Phase, PutMachine};
use std::time::{Duration, Instant};

pub trait Transport {
    /// Send one whole SysEx message (`F0 .. F7`).
    fn send(&mut self, msg: &[u8]) -> Result<(), String>;
    /// The next whole SysEx message from the device, or `None` after `timeout`.
    fn recv(&mut self, timeout: Duration) -> Result<Option<Vec<u8>>, String>;
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Progress {
    pub done: u64,
    pub total: u64,
}

pub struct Session<T: Transport> {
    pub transport: T,
    seq: u8,
    pub timeout: Duration,
    pub retries: u32,
    pub chunk: usize,
}

impl<T: Transport> Session<T> {
    pub fn new(transport: T) -> Self {
        Session {
            transport,
            seq: 0,
            timeout: Duration::from_secs(2),
            retries: 2,
            chunk: MAX_CHUNK,
        }
    }

    fn take_seq(&mut self) -> u8 {
        let s = self.seq;
        self.seq = (self.seq + 1) & 0x7f;
        s
    }

    fn decode_reply(raw: &[u8]) -> Option<Message> {
        decode(raw).ok().filter(|m| m.reply)
    }

    /// One request and its reply body (after the status). Retries on a timeout with a new `seq`.
    pub fn request(&mut self, cmd: u8, body: &[u8]) -> Result<Vec<u8>, Error> {
        for _ in 0..=self.retries {
            let seq = self.take_seq();
            self.transport
                .send(&encode_request(cmd, seq, body))
                .map_err(Error::Transport)?;
            let deadline = Instant::now() + self.timeout;
            while let Some(left) = deadline.checked_duration_since(Instant::now()) {
                let Some(raw) = self.transport.recv(left).map_err(Error::Transport)? else {
                    break;
                };
                if let Some(m) = Self::decode_reply(&raw)
                    && m.seq == seq
                    && m.cmd == cmd
                {
                    return reply_body(&m).map(<[u8]>::to_vec);
                }
            }
        }
        Err(Error::Timeout)
    }

    pub fn info(&mut self) -> Result<Info, Error> {
        parse_info(&self.request(CMD_INFO, &[])?)
    }

    /// All entries, paged.
    pub fn list(&mut self) -> Result<Vec<Entry>, Error> {
        let mut out: Vec<Entry> = Vec::new();
        loop {
            let page = parse_list(&self.request(CMD_LIST, &(out.len() as u32).to_be_bytes())?)?;
            let got = page.entries.len();
            out.extend(page.entries);
            if got == 0 || out.len() as u32 >= page.total {
                return Ok(out);
            }
        }
    }

    /// READ (runtime 0.2.4, capability `CAP_READ`): (file size, the bytes) of a store file from `offset`. The device returns at
    /// most `READ_MAX` (768) bytes and none past the end, so the bytes may be fewer than `length`. Errors: `NoFile`,
    /// `Offset` (at or past the end), `Length` (0).
    pub fn read(&mut self, path: &str, offset: u32, length: u32) -> Result<(u32, Vec<u8>), Error> {
        parse_read(&self.request(CMD_READ, &read_body(path, offset, length)?)?)
    }

    /// The first 0xa0 bytes of a store file (a .dkm header; fewer for a shorter file).
    pub fn header(&mut self, path: &str) -> Result<Vec<u8>, Error> {
        self.read(path, 0, HEADER_BYTES).map(|(_, b)| b)
    }

    pub fn delete(&mut self, path: &str) -> Result<(), Error> {
        self.request(CMD_DELETE, &path_body(path)?).map(drop)
    }
    pub fn clear(&mut self) -> Result<(), Error> {
        self.request(CMD_CLEAR, &CLEAR_MAGIC.to_be_bytes())
            .map(drop)
    }
    pub fn abort(&mut self) -> Result<(), Error> {
        self.request(CMD_ABORT, &[]).map(drop)
    }

    /// Upload one file. `progress` is called after every reply and returns `false` to cancel (the upload is
    /// aborted on the device).
    pub fn put(
        &mut self,
        path: &str,
        data: Vec<u8>,
        progress: &mut dyn FnMut(Progress) -> bool,
    ) -> Result<(), Error> {
        let mut m = PutMachine::new(path, data, self.chunk, self.seq)?;
        let result = self.drive(&mut m, progress);
        self.seq = m.seq();
        match result {
            Ok(()) => Ok(()),
            // A lost reply to END, then E_STATE on the second END: the first one committed. Look the file up.
            Err(Error::Device {
                status: Status::State,
                ..
            }) if m.phase() == Phase::End && m.end_retried() => {
                let sha = m.sha256();
                if self
                    .list()?
                    .iter()
                    .any(|e| e.path == path && e.sha256 == sha)
                {
                    Ok(())
                } else {
                    Err(Error::Device {
                        status: Status::State,
                        free_bytes: None,
                    })
                }
            }
            Err(e) => {
                // Best effort: drop the open upload. Device errors other than E_STATE leave it open.
                if !matches!(e, Error::Transport(_)) {
                    let _ = self.abort();
                }
                Err(e)
            }
        }
    }

    fn drive(
        &mut self,
        m: &mut PutMachine,
        progress: &mut dyn FnMut(Progress) -> bool,
    ) -> Result<(), Error> {
        let total = m.total();
        loop {
            while let Some(msg) = m.next_send() {
                self.transport.send(&msg).map_err(Error::Transport)?;
            }
            if m.is_done() {
                progress(Progress { done: total, total });
                return Ok(());
            }
            match self
                .transport
                .recv(self.timeout)
                .map_err(Error::Transport)?
            {
                Some(raw) => {
                    if let Some(r) = Self::decode_reply(&raw) {
                        m.on_reply(&r)?;
                    }
                }
                None => m.on_timeout()?,
            }
            if !progress(Progress {
                done: m.acked_bytes(),
                total,
            }) {
                return Err(Error::Cancelled);
            }
        }
    }

    /// Repack: CLEAR, then upload `files` again. The device keeps old file data until CLEAR, so this is how space
    /// comes back. The caller must hold every file the store should keep (`repack_missing`).
    pub fn repack(
        &mut self,
        files: Vec<(String, Vec<u8>)>,
        progress: &mut dyn FnMut(usize, Progress) -> bool,
    ) -> Result<(), Error> {
        self.clear()?;
        for (i, (path, data)) in files.into_iter().enumerate() {
            self.put(&path, data, &mut |p| progress(i, p))?;
        }
        Ok(())
    }
}

/// The store entries that no file in `library` (path, sha256) matches: a repack would lose them.
pub fn repack_missing(listing: &[Entry], library: &[(String, [u8; 32])]) -> Vec<String> {
    listing
        .iter()
        .filter(|e| !library.iter().any(|(p, s)| *p == e.path && *s == e.sha256))
        .map(|e| e.path.clone())
        .collect()
}
