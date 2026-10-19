//! Status codes, request bodies and reply parsers.

use crate::codec::*;
use std::fmt;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Status {
    Ok,
    Msg,
    Path,
    Full,
    State,
    Offset,
    Length,
    Hash,
    Write,
    Read,
    NoFile,
    Proto,
    NoStore,
    Unknown(i8),
}

impl Status {
    pub fn from_code(c: i8) -> Status {
        match c {
            0 => Status::Ok,
            -50 => Status::Msg,
            -51 => Status::Path,
            -52 => Status::Full,
            -53 => Status::State,
            -54 => Status::Offset,
            -55 => Status::Length,
            -56 => Status::Hash,
            -57 => Status::Write,
            -58 => Status::Read,
            -59 => Status::NoFile,
            -60 => Status::Proto,
            -61 => Status::NoStore,
            other => Status::Unknown(other),
        }
    }
    pub fn code(self) -> i8 {
        match self {
            Status::Ok => 0,
            Status::Msg => -50,
            Status::Path => -51,
            Status::Full => -52,
            Status::State => -53,
            Status::Offset => -54,
            Status::Length => -55,
            Status::Hash => -56,
            Status::Write => -57,
            Status::Read => -58,
            Status::NoFile => -59,
            Status::Proto => -60,
            Status::NoStore => -61,
            Status::Unknown(c) => c,
        }
    }
    pub fn name(self) -> &'static str {
        match self {
            Status::Ok => "OK",
            Status::Msg => "E_MSG",
            Status::Path => "E_PATH",
            Status::Full => "E_FULL",
            Status::State => "E_STATE",
            Status::Offset => "E_OFFSET",
            Status::Length => "E_LENGTH",
            Status::Hash => "E_HASH",
            Status::Write => "E_WRITE",
            Status::Read => "E_READ",
            Status::NoFile => "E_NOFILE",
            Status::Proto => "E_PROTO",
            Status::NoStore => "E_NOSTORE",
            Status::Unknown(_) => "E_UNKNOWN",
        }
    }
    pub fn meaning(self) -> &'static str {
        match self {
            Status::Ok => "ok",
            Status::Msg => "body too short or malformed",
            Status::Path => "path breaks the store rules",
            Status::Full => "not enough free sectors in the store (repack, or remove files)",
            Status::State => "no upload open",
            Status::Offset => "offset is neither the next byte nor a retry of the previous chunk",
            Status::Length => "chunk length breaks the rule, or the file would pass its size",
            Status::Hash => "the staged file does not match the sha256 sent with BEGIN",
            Status::Write => "the firmware's block write failed",
            Status::Read => "the firmware's block read failed",
            Status::NoFile => "that path is not in the store",
            Status::Proto => "unknown protocol version or command",
            Status::NoStore => {
                "the device has no module store, or its header is unreadable (CLEAR still works)"
            }
            Status::Unknown(_) => "unknown status code",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Error {
    /// The device answered with a non-zero status. `free_bytes` is set for `E_FULL`.
    Device {
        status: Status,
        free_bytes: Option<u32>,
    },
    /// The transport failed (port closed, write error).
    Transport(String),
    /// No reply after the retries.
    Timeout,
    /// The reply did not parse, or came for another command.
    Protocol(String),
    /// The host refused before sending (bad path, file too large, missing files for a repack).
    Request(String),
    Cancelled,
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Device { status, free_bytes } => {
                write!(f, "{} ({})", status.name(), status.meaning())?;
                if let Some(b) = free_bytes {
                    write!(f, ", {b} bytes free")?;
                }
                Ok(())
            }
            Error::Transport(m) => write!(f, "transport: {m}"),
            Error::Timeout => write!(f, "no reply from the device"),
            Error::Protocol(m) => write!(f, "protocol: {m}"),
            Error::Request(m) => write!(f, "{m}"),
            Error::Cancelled => write!(f, "cancelled"),
        }
    }
}
impl std::error::Error for Error {}

/// A reply split into its status and the body after it. A non-zero status is an `Err`.
pub fn reply_body(m: &Message) -> Result<&[u8], Error> {
    let Some((&s, rest)) = m.body.split_first() else {
        return Err(Error::Protocol("reply without a status".into()));
    };
    let status = Status::from_code(s as i8);
    if status == Status::Ok {
        return Ok(rest);
    }
    let free_bytes = (status == Status::Full && rest.len() >= 4)
        .then(|| u32::from_be_bytes([rest[0], rest[1], rest[2], rest[3]]));
    Err(Error::Device { status, free_bytes })
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Info {
    pub protocol: u16,
    pub runtime_version: u16,
    pub port: String,
    pub free_bytes: u32,
    pub max_chunk: u32,
    pub flags: u8,
}
impl Info {
    pub fn safe_mode(&self) -> bool {
        self.flags & 1 != 0
    }
    pub fn upload_open(&self) -> bool {
        self.flags & 2 != 0
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Entry {
    pub path: String,
    pub size: u32,
    pub sha256: [u8; 32],
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ListPage {
    pub total: u32,
    pub entries: Vec<Entry>,
}

struct Cur<'a>(&'a [u8]);
impl<'a> Cur<'a> {
    fn take(&mut self, n: usize) -> Result<&'a [u8], Error> {
        if self.0.len() < n {
            return Err(Error::Protocol("reply too short".into()));
        }
        let (a, b) = self.0.split_at(n);
        self.0 = b;
        Ok(a)
    }
    fn u8(&mut self) -> Result<u8, Error> {
        Ok(self.take(1)?[0])
    }
    fn u16(&mut self) -> Result<u16, Error> {
        let b = self.take(2)?;
        Ok(u16::from_be_bytes([b[0], b[1]]))
    }
    fn u32(&mut self) -> Result<u32, Error> {
        let b = self.take(4)?;
        Ok(u32::from_be_bytes([b[0], b[1], b[2], b[3]]))
    }
    fn cstr(&mut self) -> Result<String, Error> {
        let n = self
            .0
            .iter()
            .position(|&b| b == 0)
            .ok_or_else(|| Error::Protocol("string without NUL".into()))?;
        let s = String::from_utf8_lossy(self.take(n)?).into_owned();
        self.take(1)?;
        Ok(s)
    }
}

pub fn parse_info(body: &[u8]) -> Result<Info, Error> {
    let mut c = Cur(body);
    Ok(Info {
        protocol: c.u16()?,
        runtime_version: c.u16()?,
        port: c.cstr()?,
        free_bytes: c.u32()?,
        max_chunk: c.u32()?,
        flags: c.u8()?,
    })
}

pub fn parse_list(body: &[u8]) -> Result<ListPage, Error> {
    let mut c = Cur(body);
    let total = c.u32()?;
    let n = c.u8()?;
    let mut entries = Vec::new();
    for _ in 0..n {
        let path = c.cstr()?;
        let size = c.u32()?;
        let mut sha256 = [0u8; 32];
        sha256.copy_from_slice(c.take(32)?);
        entries.push(Entry { path, size, sha256 });
    }
    Ok(ListPage { total, entries })
}

pub fn path_body(path: &str) -> Result<Vec<u8>, Error> {
    if !path_ok(path) {
        return Err(Error::Request(format!("not a store path: {path:?}")));
    }
    let mut b = path.as_bytes().to_vec();
    b.push(0);
    Ok(b)
}

pub fn begin_body(path: &str, size: u32, sha: &[u8; 32]) -> Result<Vec<u8>, Error> {
    let mut b = path_body(path)?;
    b.extend(size.to_be_bytes());
    b.extend(sha);
    Ok(b)
}

pub fn data_body(offset: u32, bytes: &[u8]) -> Vec<u8> {
    let mut b = offset.to_be_bytes().to_vec();
    b.extend(bytes);
    b
}

pub fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn status_round_trip() {
        for c in -61..=0i8 {
            assert_eq!(Status::from_code(c).code(), c);
        }
        assert_eq!(Status::from_code(-52).name(), "E_FULL");
    }

    #[test]
    fn full_reply_carries_free_bytes() {
        let m = Message {
            cmd: CMD_BEGIN,
            reply: true,
            seq: 0,
            body: vec![(-52i8) as u8, 0, 0, 1, 0],
        };
        assert_eq!(
            reply_body(&m),
            Err(Error::Device {
                status: Status::Full,
                free_bytes: Some(256)
            })
        );
    }

    #[test]
    fn info_and_list_parse() {
        let mut b = vec![0, 1, 0, 3];
        b.extend(b"dt2-1.17\0");
        b.extend(1024u32.to_be_bytes());
        b.extend(32768u32.to_be_bytes());
        b.push(1);
        let i = parse_info(&b).unwrap();
        assert_eq!(
            (i.port.as_str(), i.free_bytes, i.max_chunk),
            ("dt2-1.17", 1024, 32768)
        );
        assert!(i.safe_mode() && !i.upload_open());
        assert!(parse_info(&b[..b.len() - 1]).is_err());
    }
}
