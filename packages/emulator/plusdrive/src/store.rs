//! The module store: files of `plus_drive/<device>/digikit/` in a reserved range of card sectors that the
//! firmware never writes (format and rationale: docs/design/digikit-store.md).
//!
//! All integers are big-endian. At the first sector of the range:
//!
//! ```text
//! header (one sector): "DKST", u32 version (1 or 2), u32 entry_count, u32 table_sectors, u32 used_sectors,
//!                      u32 crc32 of the table (IEEE, as zlib); version 2 adds u32 table_first_sector at 24,
//!                      u32 seq at 28 and a crc32 of bytes 0..508 at 508. Version 2 has two copies: A at sector 0 and B
//!                      at `end - selection_sectors - 1` (devices/*.toml [card.store]); the valid copy with the higher seq wins
//! table (table_sectors, from table_first_sector; 1 in version 1): entry_count entries of 128 bytes, sorted by path:
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
/// The version the builder writes. Readers take 1 and 2.
pub const STORE_VERSION: u32 = 2;
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
    /// 1 or 2.
    pub version: u32,
    /// Sequence number of the header read (1 for a version 1 store).
    pub seq: u32,
    /// Which copy was used: false = A (sector 0), true = B.
    pub from_b: bool,
    /// Where the table starts, relative to the store.
    pub table_first_sector: u32,
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

/// A store path: 1 to 87 bytes, no NUL, no empty, `.` or `..` part.
pub fn valid_store_path(path: &str) -> bool {
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
        if !valid_store_path(&file.path) {
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
    out[24..28].copy_from_slice(&1u32.to_be_bytes());
    out[28..32].copy_from_slice(&1u32.to_be_bytes());
    let hcrc = crc32(&out[..508]);
    out[508..512].copy_from_slice(&hcrc.to_be_bytes());
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

/// One header copy as the sector holds it.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Header {
    pub version: u32,
    pub count: u32,
    pub table_sectors: u32,
    pub used: u32,
    pub table_crc: u32,
    pub table_first: u32,
    pub seq: u32,
}

/// Parse a header sector: the magic, a version of 1 or 2 and (version 2) the header CRC. `None` otherwise.
pub fn parse_header(sector: &[u8]) -> Option<Header> {
    if sector.len() < SECTOR || sector[0..4] != STORE_MAGIC {
        return None;
    }
    let version = be32(sector, 4);
    let mut h = Header {
        version,
        count: be32(sector, 8),
        table_sectors: be32(sector, 12),
        used: be32(sector, 16),
        table_crc: be32(sector, 20),
        table_first: 1,
        seq: 1,
    };
    match version {
        1 => {}
        2 => {
            if be32(sector, 508) != crc32(&sector[..508]) {
                return None;
            }
            h.table_first = be32(sector, 24);
            h.seq = be32(sector, 28);
        }
        _ => return None,
    }
    Some(h)
}

/// The 512 bytes of a version 2 header copy.
pub fn encode_header(h: &Header) -> [u8; SECTOR] {
    let mut out = [0u8; SECTOR];
    out[0..4].copy_from_slice(&STORE_MAGIC);
    out[4..8].copy_from_slice(&2u32.to_be_bytes());
    out[8..12].copy_from_slice(&h.count.to_be_bytes());
    out[12..16].copy_from_slice(&h.table_sectors.to_be_bytes());
    out[16..20].copy_from_slice(&h.used.to_be_bytes());
    out[20..24].copy_from_slice(&h.table_crc.to_be_bytes());
    out[24..28].copy_from_slice(&h.table_first.to_be_bytes());
    out[28..32].copy_from_slice(&h.seq.to_be_bytes());
    let c = crc32(&out[..508]);
    out[508..512].copy_from_slice(&c.to_be_bytes());
    out
}

/// Load the table `h` points at and check it; `None` when it is not a valid store.
fn load_store(
    img: &dyn RandomAccessRead,
    base: u64,
    available: u64,
    h: &Header,
    from_b: bool,
) -> Result<Store, StoreError> {
    let corrupt = |why: &str| Err(StoreError::Corrupt(why.into()));
    let (count, table_sectors, used) = (
        h.count as usize,
        u64::from(h.table_sectors),
        u64::from(h.used),
    );
    let tfirst = u64::from(h.table_first);
    if table_sectors != sectors_for(count * STORE_ENTRY_BYTES)
        || tfirst < 1
        || used < tfirst + table_sectors
        || used > available
    {
        return corrupt("header counts are inconsistent");
    }
    let table = read_vec(img, base + tfirst * SECTOR as u64, table_sectors as usize * SECTOR);
    if crc32(&table) != h.table_crc {
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
        if first_sector < 1 || end > used {
            return corrupt("an entry lies outside the used sectors");
        }
        entries.push(StoreEntry {
            path: String::from_utf8_lossy(&e[..name_len]).into_owned(),
            first_sector,
            size_bytes,
            sha256: e[96..128].try_into().expect("32 bytes"),
        });
    }
    Ok(Store {
        entries,
        used_sectors: h.used,
        version: h.version,
        seq: h.seq,
        from_b,
        table_first_sector: h.table_first,
    })
}

/// Read the store at `first_sector` (range of `available` sectors; `header_b_sector` is the absolute sector of header copy B,
/// `StoreLayout::header_b`): `Ok(None)` when neither header has the magic (nothing was built into the range). A copy is
/// valid when its magic, version, header CRC (version 2) and table CRC hold; the valid version 2 copy with the higher `seq`
/// wins (equal: A); a version 1 copy at A counts only when B is not valid.
pub fn read_store(
    img: &dyn RandomAccessRead,
    first_sector: u64,
    available: u64,
    header_b_sector: u64,
) -> Result<Option<Store>, StoreError> {
    let base = first_sector * SECTOR as u64;
    let a_sec = read_vec(img, base, SECTOR);
    let b_sec = read_vec(img, header_b_sector * SECTOR as u64, SECTOR);
    let a = parse_header(&a_sec);
    let b = parse_header(&b_sec).filter(|h| h.version == 2);
    if a.is_none() && b.is_none() && a_sec[0..4] != STORE_MAGIC {
        return Ok(None);
    }
    let a_store = a.map(|h| load_store(img, base, available, &h, false));
    let b_store = b.map(|h| load_store(img, base, available, &h, true));
    let a_ok = a_store.as_ref().and_then(|r| r.as_ref().ok());
    let b_ok = b_store.as_ref().and_then(|r| r.as_ref().ok());
    let pick_b = match (a_ok, b_ok) {
        (Some(a), Some(b)) => a.version == 1 || b.seq > a.seq,
        (None, Some(_)) => true,
        _ => false,
    };
    if pick_b {
        return b_store.unwrap().map(Some);
    }
    match (a_store, b_store) {
        (Some(Ok(s)), _) => Ok(Some(s)),
        (Some(Err(e)), _) => Err(e),
        (None, Some(Err(e))) => Err(e),
        _ => Err(StoreError::Corrupt("unknown version or header checksum".into())),
    }
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
        let store = read_store(&img, 0x38000, 0x6000, 0x3dffd).unwrap().unwrap();
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
        assert_eq!(&bytes[4..8], &2u32.to_be_bytes());
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
        assert_eq!(read_store(&img, 0x38000, 0x6000, 0x3dffd).unwrap(), None);
        let mut bytes = encode_store(vec![file("a", 700)], 0x6000).unwrap();
        bytes[SECTOR + 3] ^= 1;
        img.write_at(0x38000 * 512, &bytes).unwrap();
        assert!(matches!(
            read_store(&img, 0x38000, 0x6000, 0x3dffd),
            Err(StoreError::Corrupt(_))
        ));
    }

    /// An overwritable card for the torn-write cases (`SparseImage` refuses a second write).
    struct Mem(Vec<u8>);
    impl Mem {
        fn write_at(&mut self, at: u64, b: &[u8]) -> Result<(), ()> {
            self.0[at as usize..at as usize + b.len()].copy_from_slice(b);
            Ok(())
        }
    }
    impl RandomAccessRead for Mem {
        fn len(&self) -> u64 {
            self.0.len() as u64
        }
        fn read_at(&self, off: u64, dst: &mut [u8]) -> usize {
            dst.copy_from_slice(&self.0[off as usize..off as usize + dst.len()]);
            dst.len()
        }
    }

    const B: u64 = 0x3dffd;

    /// A card with `files` built as version 2 (both header copies).
    fn card(files: Vec<StoreFile>) -> (Mem, Vec<u8>) {
        let bytes = encode_store(files, B - 0x38000).unwrap();
        let mut img = Mem(vec![0; 0x40000 * 512]);
        img.write_at(0x38000 * 512, &bytes).unwrap();
        img.write_at(B * 512, &bytes[..SECTOR]).unwrap();
        (img, bytes)
    }

    /// Header copy with a higher seq that points at the table at sector `tfirst` (a table copy written there).
    fn commit_b(img: &mut Mem, bytes: &[u8], seq: u32, at: u64, files: usize) {
        let mut h = parse_header(&bytes[..SECTOR]).unwrap();
        // a second table: the same table at `at`
        let table = &bytes[SECTOR..SECTOR + h.table_sectors as usize * SECTOR];
        img.write_at((0x38000 + at) * 512, table).unwrap();
        h.seq = seq;
        h.table_first = at as u32;
        h.used = at as u32 + h.table_sectors;
        assert_eq!(h.count as usize, files);
        img.write_at(B * 512, &encode_header(&h)).unwrap();
    }

    #[test]
    fn equal_seq_takes_a_and_the_higher_seq_takes_b() {
        let (mut img, bytes) = card(vec![file("a", 700)]);
        let s = read_store(&img, 0x38000, 0x6000, B).unwrap().unwrap();
        assert_eq!((s.version, s.seq, s.from_b), (2, 1, false));
        commit_b(&mut img, &bytes, 2, 0x100, 1);
        let s = read_store(&img, 0x38000, 0x6000, B).unwrap().unwrap();
        assert_eq!((s.seq, s.from_b, s.table_first_sector), (2, true, 0x100));
        assert_eq!(s.entries[0].path, "a");
    }

    #[test]
    fn a_torn_b_leaves_the_old_store_and_a_torn_a_leaves_b() {
        let (mut img, bytes) = card(vec![file("a", 700)]);
        commit_b(&mut img, &bytes, 2, 0x100, 1);
        // B torn (bit flip): header CRC fails, A (seq 1) is used
        let mut b = img_sector(&img, B);
        b[40] ^= 1;
        img.write_at(B * 512, &b).unwrap();
        let s = read_store(&img, 0x38000, 0x6000, B).unwrap().unwrap();
        assert_eq!((s.seq, s.from_b), (1, false));
        // B good, A torn: B is used
        commit_b(&mut img, &bytes, 2, 0x100, 1);
        let mut a = img_sector(&img, 0x38000);
        a[28] ^= 1;
        img.write_at(0x38000 * 512, &a).unwrap();
        let s = read_store(&img, 0x38000, 0x6000, B).unwrap().unwrap();
        assert_eq!((s.seq, s.from_b), (2, true));
        // B points at a table that was not written (power cut inside the table write): the table CRC fails, A is used
        let (mut img, bytes) = card(vec![file("a", 700)]);
        commit_b(&mut img, &bytes, 2, 0x100, 1);
        img.write_at((0x38000 + 0x100) * 512, &[0u8; 512]).unwrap();
        let s = read_store(&img, 0x38000, 0x6000, B).unwrap().unwrap();
        assert_eq!((s.seq, s.from_b), (1, false));
    }

    fn img_sector(img: &Mem, sector: u64) -> Vec<u8> {
        read_vec(img, sector * 512, 512)
    }

    #[test]
    fn a_version_1_store_reads_and_loses_to_a_valid_b() {
        let (mut img, bytes) = card(vec![file("a", 700)]);
        // turn A into version 1: no seq, no header CRC; B is absent
        let mut v1 = bytes[..SECTOR].to_vec();
        v1[4..8].copy_from_slice(&1u32.to_be_bytes());
        v1[24..32].fill(0);
        v1[508..512].fill(0);
        img.write_at(0x38000 * 512, &v1).unwrap();
        img.write_at(B * 512, &[0u8; 512]).unwrap();
        let s = read_store(&img, 0x38000, 0x6000, B).unwrap().unwrap();
        assert_eq!((s.version, s.seq, s.table_first_sector), (1, 1, 1));
        assert_eq!(s.entries.len(), 1);
        // a valid B (seq 2) wins over a version 1 A
        commit_b(&mut img, &bytes, 2, 0x100, 1);
        let s = read_store(&img, 0x38000, 0x6000, B).unwrap().unwrap();
        assert!(s.from_b && s.version == 2);
        // a bad version is corrupt, no magic is no store
        v1[4..8].copy_from_slice(&9u32.to_be_bytes());
        img.write_at(0x38000 * 512, &v1).unwrap();
        img.write_at(B * 512, &[0u8; 512]).unwrap();
        assert!(read_store(&img, 0x38000, 0x6000, B).is_err());
    }
}
