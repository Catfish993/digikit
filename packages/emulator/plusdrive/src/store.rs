//! The module store: files of `plus_drive/<device>/digikit/` in a reserved range of card sectors that the
//! firmware never writes (format and rationale: docs/design/digikit-store.md).
//!
//! All integers are big-endian. At the first sector of the range:
//!
//! ```text
//! header (one sector): "DKST", u32 version = 1, u32 entry_count, u32 table_sectors, u32 used_sectors,
//!                      u32 crc32 of the table (IEEE, as zlib), zero to 512 bytes
//! table (table_sectors): entry_count entries of 128 bytes, sorted by path:
//!                      path[88] NUL padded (relative to digikit/), u32 first_sector (relative to the
//!                      first sector of the range), u32 size_bytes, u8 sha256[32]
//! data: each file starts on a sector boundary, in table order
//! ```
//!
//! `used_sectors` counts the header, the table and the data.

use emmc_card::RandomAccessRead;
use sha2::{Digest, Sha256};

use crate::SECTOR;

pub const STORE_MAGIC: [u8; 4] = *b"DKST";
pub const STORE_VERSION: u32 = 1;
pub const STORE_ENTRY_BYTES: usize = 128;
pub const STORE_PATH_BYTES: usize = 88;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct StoreFile {
    /// Relative to `digikit/`, `/`-separated, no leading slash.
    pub path: String,
    pub bytes: Vec<u8>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct StoreEntry {
    pub path: String,
    /// Relative to the first sector of the store.
    pub first_sector: u32,
    pub size_bytes: u32,
    pub sha256: [u8; 32],
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Store {
    pub entries: Vec<StoreEntry>,
    pub used_sectors: u32,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum StoreError {
    /// The files need `needed` sectors, the range has `available`.
    DoesNotFit {
        needed: u64,
        available: u64,
    },
    /// A path is empty, has an empty, `.` or `..` component, a NUL, or is not shorter than 88 bytes.
    InvalidPath(String),
    DuplicatePath(String),
    /// A file over 4 GiB - 1 (the size field).
    TooLarge(String),
    /// The reader found a header that is not version 1 or does not match its table.
    Corrupt(String),
}

impl core::fmt::Display for StoreError {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        match self {
            Self::DoesNotFit { needed, available } => write!(
                f,
                "the digikit/ files need {needed} sectors ({} KiB) but the module store has {available} ({} KiB)",
                needed * 512 / 1024,
                available * 512 / 1024
            ),
            Self::InvalidPath(p) => write!(
                f,
                "digikit/{p}: a store path is at most 87 bytes, with no empty, . or .. part"
            ),
            Self::DuplicatePath(p) => write!(f, "digikit/{p}: twice"),
            Self::TooLarge(p) => write!(f, "digikit/{p}: larger than 4 GiB"),
            Self::Corrupt(why) => write!(f, "module store: {why}"),
        }
    }
}
impl std::error::Error for StoreError {}

/// CRC-32 (IEEE 802.3, reflected, as zlib and `crc32` print it).
pub fn crc32(bytes: &[u8]) -> u32 {
    let mut crc = 0xffff_ffffu32;
    for &byte in bytes {
        crc ^= u32::from(byte);
        for _ in 0..8 {
            crc = (crc >> 1) ^ (0xedb8_8320 & (crc & 1).wrapping_neg());
        }
    }
    !crc
}

fn sectors_for(bytes: usize) -> u64 {
    bytes.div_ceil(SECTOR) as u64
}

fn valid_path(path: &str) -> bool {
    path.len() < STORE_PATH_BYTES
        && !path.is_empty()
        && !path.contains('\0')
        && path
            .split('/')
            .all(|part| !part.is_empty() && part != "." && part != "..")
}

/// The bytes of a store holding `files`, a whole number of sectors starting with the header. `available` is the
/// range's size in sectors.
pub fn encode_store(mut files: Vec<StoreFile>, available: u64) -> Result<Vec<u8>, StoreError> {
    files.sort_by(|a, b| a.path.as_bytes().cmp(b.path.as_bytes()));
    for (i, file) in files.iter().enumerate() {
        if !valid_path(&file.path) {
            return Err(StoreError::InvalidPath(file.path.clone()));
        }
        if u32::try_from(file.bytes.len()).is_err() {
            return Err(StoreError::TooLarge(file.path.clone()));
        }
        if i > 0 && files[i - 1].path == file.path {
            return Err(StoreError::DuplicatePath(file.path.clone()));
        }
    }
    let table_sectors = sectors_for(files.len() * STORE_ENTRY_BYTES);
    let mut next = 1 + table_sectors;
    let mut table = vec![0u8; table_sectors as usize * SECTOR];
    let mut starts = Vec::with_capacity(files.len());
    for (i, file) in files.iter().enumerate() {
        let at = i * STORE_ENTRY_BYTES;
        table[at..at + file.path.len()].copy_from_slice(file.path.as_bytes());
        let first = u32::try_from(next).map_err(|_| StoreError::DoesNotFit {
            needed: u64::MAX,
            available,
        })?;
        table[at + 88..at + 92].copy_from_slice(&first.to_be_bytes());
        table[at + 92..at + 96].copy_from_slice(&(file.bytes.len() as u32).to_be_bytes());
        table[at + 96..at + 128].copy_from_slice(&Sha256::digest(&file.bytes));
        starts.push(next);
        next += sectors_for(file.bytes.len());
    }
    if next > available {
        return Err(StoreError::DoesNotFit {
            needed: next,
            available,
        });
    }
    let mut out = vec![0u8; next as usize * SECTOR];
    out[0..4].copy_from_slice(&STORE_MAGIC);
    out[4..8].copy_from_slice(&STORE_VERSION.to_be_bytes());
    out[8..12].copy_from_slice(&(files.len() as u32).to_be_bytes());
    out[12..16].copy_from_slice(&(table_sectors as u32).to_be_bytes());
    out[16..20].copy_from_slice(&(next as u32).to_be_bytes());
    out[20..24].copy_from_slice(&crc32(&table).to_be_bytes());
    out[SECTOR..SECTOR + table.len()].copy_from_slice(&table);
    for (file, start) in files.iter().zip(starts) {
        let at = start as usize * SECTOR;
        out[at..at + file.bytes.len()].copy_from_slice(&file.bytes);
    }
    Ok(out)
}

fn read_vec(img: &dyn RandomAccessRead, offset: u64, len: usize) -> Vec<u8> {
    let mut out = vec![0; len];
    img.read_at(offset, &mut out);
    out
}
fn be32(b: &[u8], at: usize) -> u32 {
    u32::from_be_bytes(b[at..at + 4].try_into().expect("4 bytes"))
}

/// Read the store at `first_sector` (range of `available` sectors): `Ok(None)` when the header magic is absent
/// (nothing was built into the range).
pub fn read_store(
    img: &dyn RandomAccessRead,
    first_sector: u64,
    available: u64,
) -> Result<Option<Store>, StoreError> {
    let base = first_sector * SECTOR as u64;
    let header = read_vec(img, base, SECTOR);
    if header[0..4] != STORE_MAGIC {
        return Ok(None);
    }
    let corrupt = |why: &str| Err(StoreError::Corrupt(why.into()));
    if be32(&header, 4) != STORE_VERSION {
        return corrupt("unknown version");
    }
    let (count, table_sectors, used) = (
        be32(&header, 8) as usize,
        u64::from(be32(&header, 12)),
        u64::from(be32(&header, 16)),
    );
    if table_sectors != sectors_for(count * STORE_ENTRY_BYTES)
        || used < 1 + table_sectors
        || used > available
    {
        return corrupt("header counts are inconsistent");
    }
    let table = read_vec(img, base + SECTOR as u64, table_sectors as usize * SECTOR);
    if crc32(&table) != be32(&header, 20) {
        return corrupt("table checksum");
    }
    let mut entries = Vec::with_capacity(count);
    for i in 0..count {
        let e = &table[i * STORE_ENTRY_BYTES..(i + 1) * STORE_ENTRY_BYTES];
        let name_len = e[..STORE_PATH_BYTES]
            .iter()
            .position(|&b| b == 0)
            .unwrap_or(STORE_PATH_BYTES);
        let first_sector = be32(e, 88);
        let size_bytes = be32(e, 92);
        let end = u64::from(first_sector) + sectors_for(size_bytes as usize);
        if first_sector < 1 + table_sectors as u32 || end > used {
            return corrupt("an entry lies outside the used sectors");
        }
        entries.push(StoreEntry {
            path: String::from_utf8_lossy(&e[..name_len]).into_owned(),
            first_sector,
            size_bytes,
            sha256: e[96..128].try_into().expect("32 bytes"),
        });
    }
    Ok(Some(Store {
        entries,
        used_sectors: used as u32,
    }))
}

/// The bytes of one entry, checked against its SHA-256.
pub fn read_store_file(
    img: &dyn RandomAccessRead,
    first_sector: u64,
    entry: &StoreEntry,
) -> Result<Vec<u8>, StoreError> {
    let at = (first_sector + u64::from(entry.first_sector)) * SECTOR as u64;
    let bytes = read_vec(img, at, entry.size_bytes as usize);
    if Sha256::digest(&bytes)[..] != entry.sha256 {
        return Err(StoreError::Corrupt(format!(
            "digikit/{}: SHA-256 differs",
            entry.path
        )));
    }
    Ok(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::SparseImage;

    fn file(path: &str, len: usize) -> StoreFile {
        StoreFile {
            path: path.into(),
            bytes: (0..len).map(|i| (i * 13 + 5) as u8).collect(),
        }
    }

    #[test]
    fn crc32_matches_the_zlib_check_value() {
        assert_eq!(crc32(b"123456789"), 0xcbf4_3926);
    }

    #[test]
    fn round_trips_sorted_and_sector_aligned() {
        let files = vec![file("b/c.dkm", 1000), file("a.dkm", 512), file("empty", 0)];
        let bytes = encode_store(files.clone(), 0x6000).unwrap();
        assert_eq!(bytes.len() % SECTOR, 0);
        let mut img = SparseImage::new(0x40000 * 512);
        img.write_at(0x38000 * 512, &bytes).unwrap();
        let store = read_store(&img, 0x38000, 0x6000).unwrap().unwrap();
        let names: Vec<_> = store.entries.iter().map(|e| e.path.as_str()).collect();
        assert_eq!(names, ["a.dkm", "b/c.dkm", "empty"]);
        // header + 1 table sector + 1 + 2 + 0 data sectors
        assert_eq!(store.used_sectors, 5);
        assert_eq!(store.entries[0].first_sector, 2);
        assert_eq!(store.entries[1].first_sector, 3);
        for e in &store.entries {
            let want = files.iter().find(|f| f.path == e.path).unwrap();
            assert_eq!(read_store_file(&img, 0x38000, e).unwrap(), want.bytes);
        }
        assert_eq!(&bytes[0..4], b"DKST");
        assert_eq!(&bytes[4..8], &1u32.to_be_bytes());
    }

    #[test]
    fn refuses_what_does_not_fit_or_is_malformed() {
        assert!(matches!(
            encode_store(vec![file("big", 10 * 512)], 5),
            Err(StoreError::DoesNotFit { .. })
        ));
        for bad in ["", "/a", "a//b", "a/../b", "./a", &"x".repeat(88)] {
            assert!(
                matches!(
                    encode_store(vec![file(bad, 1)], 100),
                    Err(StoreError::InvalidPath(_))
                ),
                "{bad}"
            );
        }
        assert!(matches!(
            encode_store(vec![file("a", 1), file("a", 2)], 100),
            Err(StoreError::DuplicatePath(_))
        ));
        encode_store(vec![file(&"x".repeat(87), 1)], 100).unwrap();
    }

    #[test]
    fn an_empty_range_has_no_store_and_damage_is_found() {
        let mut img = SparseImage::new(0x40000 * 512);
        assert_eq!(read_store(&img, 0x38000, 0x6000).unwrap(), None);
        let mut bytes = encode_store(vec![file("a", 700)], 0x6000).unwrap();
        bytes[SECTOR + 3] ^= 1;
        img.write_at(0x38000 * 512, &bytes).unwrap();
        assert!(matches!(
            read_store(&img, 0x38000, 0x6000),
            Err(StoreError::Corrupt(_))
        ));
    }
}
