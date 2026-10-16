//! Export what the firmware wrote to the +Drive back into a `plus_drive/<device>/` folder, byte for byte.
//!
//! Folder layout (plan decision 6, docs/findings/24-plusdrive-export.md): path-filesystem files and folders by their names in
//! the firmware's native format; `.mmcfs/{projects,sounds,kits}/NNNN.bin` for the occupied MmcFs slots;
//! `.mmcfs/boot/SSSSSS.bin` for a 16 MiB region outside the pools (sector in hex: the two boot copies 040000 and
//! 048000, the working project 050000) that the guest wrote.
//!
//! Nothing is lost: the whole tree is first written to `<out>/export/<stamp>/`; then only files that are new or
//! differ are copied into the folder, and each replaced file is moved to `<out>/backup/<stamp>/` first. Files the
//! firmware deleted are listed and kept, unless `prune` is set (then they are moved to the backup too).

use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    io::{Read, Seek, SeekFrom},
    path::{Path, PathBuf},
    sync::Mutex,
    time::{SystemTime, UNIX_EPOCH},
};

use emmc_card::{CardOverlaySector, RandomAccessRead};

use crate::{Fs, Geometry, OverlayImage, WavLimits, parse_overlay, read_mmcfs, wav_to_native};

#[derive(Clone, Debug)]
pub struct Options {
    /// Holds `export/<stamp>/` and `backup/<stamp>/`.
    pub out_root: PathBuf,
    /// Move the files the firmware deleted to the backup.
    pub prune: bool,
    /// Fixed stamp (tests); default: the UTC time now.
    pub stamp: Option<String>,
}

impl Options {
    pub fn new(out_root: impl Into<PathBuf>) -> Self {
        Self {
            out_root: out_root.into(),
            prune: false,
            stamp: None,
        }
    }
}

#[derive(Clone, Debug, Default)]
pub struct Report {
    pub export_dir: PathBuf,
    pub backup_dir: PathBuf,
    /// Relative paths copied into the folder (new files).
    pub added: Vec<String>,
    /// Relative paths whose bytes changed (the old file is in the backup).
    pub replaced: Vec<String>,
    pub unchanged: Vec<String>,
    /// In the folder but not on the card any more.
    pub deleted_by_firmware: Vec<String>,
    /// The subset of `deleted_by_firmware` moved to the backup (with `prune`).
    pub pruned: Vec<String>,
    pub issues: Vec<String>,
}

impl Report {
    pub fn summary(&self) -> String {
        format!(
            "added {} replaced {} unchanged {} deleted_by_firmware {} pruned {} issues {}\nexport {}\nbackup {}",
            self.added.len(),
            self.replaced.len(),
            self.unchanged.len(),
            self.deleted_by_firmware.len(),
            self.pruned.len(),
            self.issues.len(),
            self.export_dir.display(),
            self.backup_dir.display()
        )
    }
    pub fn text(&self) -> String {
        let mut out = self.summary();
        for (title, list) in [
            ("added", &self.added),
            ("replaced", &self.replaced),
            ("unchanged", &self.unchanged),
            ("deleted by the firmware", &self.deleted_by_firmware),
            ("pruned", &self.pruned),
            ("issues", &self.issues),
        ] {
            out.push_str(&format!("\n\n{title} ({})\n", list.len()));
            for line in list {
                out.push_str(&format!("  {line}\n"));
            }
        }
        out
    }
}

// ----- names --------------------------------------------------------------------------------------------------

/// The name the builder gives a sample file: `.wav` dropped, non-ASCII as `?`, at most 255 bytes.
fn builder_name(file_name: &str) -> Vec<u8> {
    let stem = if file_name.len() >= 4
        && file_name.as_bytes()[file_name.len() - 4..].eq_ignore_ascii_case(b".wav")
    {
        &file_name[..file_name.len() - 4]
    } else {
        file_name
    };
    stem.chars()
        .map(|c| if c.is_ascii() { c as u8 } else { b'?' })
        .take(255)
        .collect()
}

fn utc_stamp() -> String {
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_secs());
    let (days, rem) = (secs / 86400, secs % 86400);
    // civil from days (Howard Hinnant)
    let z = days as i64 + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + i64::from(m <= 2);
    format!(
        "{y:04}{m:02}{d:02}-{:02}{:02}{:02}",
        rem / 3600,
        rem % 3600 / 60,
        rem % 60
    )
}

fn unique_dir(root: &Path, kind: &str, stamp: &str) -> PathBuf {
    let base = root.join(kind);
    let mut dir = base.join(stamp);
    let mut n = 1;
    while dir.exists() {
        dir = base.join(format!("{stamp}-{n}"));
        n += 1;
    }
    dir
}

fn same_bytes(a: &Path, b: &Path) -> bool {
    let (Ok(ma), Ok(mb)) = (fs::metadata(a), fs::metadata(b)) else {
        return false;
    };
    if !ma.is_file() || !mb.is_file() || ma.len() != mb.len() {
        return false;
    }
    let (Ok(mut fa), Ok(mut fb)) = (fs::File::open(a), fs::File::open(b)) else {
        return false;
    };
    let (mut x, mut y) = (vec![0u8; 1 << 20], vec![0u8; 1 << 20]);
    loop {
        let Ok(n) = fa.read(&mut x) else { return false };
        if n == 0 {
            return true;
        }
        if fb.read_exact(&mut y[..n]).is_err() || x[..n] != y[..n] {
            return false;
        }
    }
}

fn move_file(from: &Path, to: &Path) -> Result<(), String> {
    if let Some(parent) = to.parent() {
        fs::create_dir_all(parent).map_err(|e| format!("{}: {e}", parent.display()))?;
    }
    if fs::rename(from, to).is_ok() {
        return Ok(());
    }
    fs::copy(from, to).map_err(|e| format!("{} -> {}: {e}", from.display(), to.display()))?;
    fs::remove_file(from).map_err(|e| format!("{}: {e}", from.display()))
}

fn copy_atomic(from: &Path, to: &Path) -> Result<(), String> {
    if let Some(parent) = to.parent() {
        fs::create_dir_all(parent).map_err(|e| format!("{}: {e}", parent.display()))?;
    }
    let mut part = to.as_os_str().to_owned();
    part.push(".part");
    let part = PathBuf::from(part);
    fs::copy(from, &part).map_err(|e| format!("{} -> {}: {e}", from.display(), part.display()))?;
    fs::rename(&part, to).map_err(|e| format!("{}: {e}", to.display()))
}

// ----- the export ---------------------------------------------------------------------------------------------

/// What the firmware's card holds, as relative paths below the folder.
struct Staged {
    /// Relative path (with `/`) of every file written to the export directory.
    files: BTreeSet<String>,
    dirs: BTreeSet<String>,
    /// MmcFs was present on the card (so a missing slot file means a freed slot).
    mmcfs: bool,
}

fn rel_string(parts: &[Vec<u8>]) -> String {
    parts
        .iter()
        .map(|p| String::from_utf8_lossy(p).into_owned())
        .collect::<Vec<_>>()
        .join("/")
}

fn stage_file(dir: &Path, rel: &str, bytes: &[u8]) -> Result<(), String> {
    let path = dir.join(rel);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| format!("{}: {e}", parent.display()))?;
    }
    fs::write(&path, bytes).map_err(|e| format!("{}: {e}", path.display()))
}

/// Write the card's tree and slots to `export_dir`.
fn stage(
    view: &OverlayImage<'_>,
    geometry: Geometry,
    export_dir: &Path,
    report: &mut Report,
) -> Result<Staged, String> {
    let mut staged = Staged {
        files: BTreeSet::new(),
        dirs: BTreeSet::new(),
        mmcfs: false,
    };
    fs::create_dir_all(export_dir).map_err(|e| format!("{}: {e}", export_dir.display()))?;
    match Fs::open(view) {
        Err(e) => report.issues.push(e.to_string()),
        Ok(fs_view) => {
            let walk = fs_view.walk();
            report.issues.extend(walk.issues.iter().cloned());
            // The firmware links its RAM-resident "factory" tree into the root at every mount: expected, not on the card.
            for name in walk
                .ram_entries
                .iter()
                .filter(|n| n.as_slice() != b"factory")
            {
                report.issues.push(format!(
                    "{}: RAM-resident entry (not on the card), skipped",
                    String::from_utf8_lossy(name)
                ));
            }
            // Names that collide on a case-insensitive disk, or with the reserved folders, get the id appended.
            let mut taken: BTreeSet<String> = BTreeSet::new();
            let mut rename: BTreeMap<Vec<u8>, Vec<u8>> = BTreeMap::new();
            for node in &walk.nodes {
                let parts: Vec<&[u8]> = node.path.split(|&b| b == b'/').collect();
                let mut mapped: Vec<Vec<u8>> = Vec::new();
                for i in 0..parts.len() {
                    let prefix = node
                        .path
                        .split(|&b| b == b'/')
                        .take(i + 1)
                        .collect::<Vec<_>>()
                        .join(&b'/');
                    if let Some(known) = rename.get(&prefix) {
                        mapped.push(known.clone());
                        continue;
                    }
                    let last = i + 1 == parts.len();
                    let mut name: Vec<u8> = parts[i]
                        .iter()
                        .map(|&b| {
                            if b == 0 || b == b'/' || b == b'\\' {
                                b'_'
                            } else {
                                b
                            }
                        })
                        .collect();
                    if name.is_empty() {
                        name = b"_".to_vec();
                    }
                    let full = |n: &[u8]| {
                        let mut p = mapped.clone();
                        p.push(n.to_vec());
                        rel_string(&p).to_lowercase()
                    };
                    let reserved = i == 0 && (name.starts_with(b".mmcfs") || name == b".preview");
                    if (reserved || taken.contains(&full(&name))) && last {
                        let id = node.record.id;
                        name.extend_from_slice(format!("~{id}").as_bytes());
                        report.issues.push(format!(
                            "{}: name collides (case or reserved); exported as {}",
                            String::from_utf8_lossy(&node.path),
                            String::from_utf8_lossy(&name)
                        ));
                    }
                    taken.insert(full(&name));
                    rename.insert(prefix, name.clone());
                    mapped.push(name);
                }
                let rel = rel_string(&mapped);
                if node.record.is_dir() {
                    fs::create_dir_all(export_dir.join(&rel)).map_err(|e| format!("{rel}: {e}"))?;
                    staged.dirs.insert(rel);
                    continue;
                }
                let mut issues = Vec::new();
                match fs_view.read_content(&node.record, &mut issues) {
                    Ok(bytes) => {
                        stage_file(export_dir, &rel, &bytes)?;
                        staged.files.insert(rel.clone());
                    }
                    Err(e) => report.issues.push(format!("{rel}: {e}")),
                }
                report
                    .issues
                    .extend(issues.into_iter().map(|i| format!("{rel}: {i}")));
            }
        }
    }
    let mmc = read_mmcfs(view, geometry, Some(view));
    report.issues.extend(mmc.issues.iter().cloned());
    staged.mmcfs = mmc.present;
    for slot in &mmc.slots {
        let rel = format!(".mmcfs/{}/{:04}.bin", slot.pool.dir_name(), slot.index);
        stage_file(export_dir, &rel, &slot.bytes)?;
        staged.files.insert(rel);
    }
    for (sector, bytes) in &mmc.boot {
        let rel = format!(".mmcfs/boot/{sector:06x}.bin");
        stage_file(export_dir, &rel, bytes)?;
        staged.files.insert(rel);
    }
    Ok(staged)
}

fn list_files(folder: &Path, rel: &str, out: &mut Vec<String>) {
    let Ok(entries) = fs::read_dir(folder.join(rel)) else {
        return;
    };
    let mut entries: Vec<_> = entries.flatten().collect();
    entries.sort_by_key(|e| e.file_name());
    for entry in entries {
        let name = entry.file_name().to_string_lossy().into_owned();
        if name.starts_with('.') && name != ".mmcfs" {
            continue;
        }
        let child = if rel.is_empty() {
            name
        } else {
            format!("{rel}/{name}")
        };
        match entry.file_type() {
            Ok(t) if t.is_dir() => list_files(folder, &child, out),
            Ok(t) if t.is_file() => out.push(child),
            _ => {}
        }
    }
}

/// Export the card (`base` plus `overlay`) into `folder`; see the module notes.
pub fn export(
    base: &dyn RandomAccessRead,
    overlay: &[CardOverlaySector],
    folder: &Path,
    geometry: Geometry,
    options: &Options,
) -> Result<Report, String> {
    let stamp = options.stamp.clone().unwrap_or_else(utc_stamp);
    let view = OverlayImage::new(base, overlay);
    let export_dir = unique_dir(&options.out_root, "export", &stamp);
    let backup_dir = unique_dir(&options.out_root, "backup", &stamp);
    let mut report = Report {
        export_dir: export_dir.clone(),
        backup_dir: backup_dir.clone(),
        ..Report::default()
    };
    let staged = stage(&view, geometry, &export_dir, &mut report)?;
    fs::create_dir_all(folder).map_err(|e| format!("{}: {e}", folder.display()))?;

    // Sample sources in the folder: `x.wav` stands for the native file `x` when it converts to the same bytes.
    let mut existing = Vec::new();
    list_files(folder, "", &mut existing);
    let mut wav_for: BTreeMap<String, String> = BTreeMap::new();
    for rel in &existing {
        if rel.to_ascii_lowercase().ends_with(".wav") {
            let (dir, file) = rel.rsplit_once('/').unwrap_or(("", rel));
            let key = String::from_utf8_lossy(&builder_name(file)).into_owned();
            wav_for.insert(
                if dir.is_empty() {
                    key
                } else {
                    format!("{dir}/{key}")
                },
                rel.clone(),
            );
        }
    }
    let mut covered_wavs: BTreeSet<String> = BTreeSet::new();

    for rel in &staged.dirs {
        fs::create_dir_all(folder.join(rel)).map_err(|e| format!("{rel}: {e}"))?;
    }
    for rel in &staged.files {
        let from = export_dir.join(rel);
        let to = folder.join(rel);
        if let Some(wav_rel) = wav_for.get(rel) {
            // The folder holds the source WAV: unchanged when it still builds to these bytes.
            let converted = fs::read(folder.join(wav_rel))
                .ok()
                .and_then(|w| wav_to_native(&w, WavLimits::default()).ok())
                .map(|n| n.bytes);
            covered_wavs.insert(wav_rel.clone());
            if converted.is_some() && converted == fs::read(&from).ok() {
                report.unchanged.push(format!("{rel} (from {wav_rel})"));
                continue;
            }
            // The firmware changed the sample: the WAV would win over the native file at the next build.
            move_file(&folder.join(wav_rel), &backup_dir.join(wav_rel))?;
            report
                .replaced
                .push(format!("{wav_rel} (now native {rel})"));
            copy_atomic(&from, &to)?;
            continue;
        }
        if to.is_dir() {
            report.issues.push(format!(
                "{rel}: a folder is in the way of the file; left alone"
            ));
            continue;
        }
        if to.is_file() {
            if same_bytes(&from, &to) {
                report.unchanged.push(rel.clone());
                continue;
            }
            move_file(&to, &backup_dir.join(rel))?;
            copy_atomic(&from, &to)?;
            report.replaced.push(rel.clone());
        } else {
            // a file where the card has a folder in the path: back it up first
            let mut at = PathBuf::new();
            for part in Path::new(rel)
                .parent()
                .into_iter()
                .flat_map(Path::components)
            {
                at.push(part);
                let p = folder.join(&at);
                if p.is_file() {
                    move_file(&p, &backup_dir.join(&at))?;
                    report.replaced.push(at.to_string_lossy().into_owned());
                }
            }
            copy_atomic(&from, &to)?;
            report.added.push(rel.clone());
        }
    }

    for rel in &existing {
        if staged.files.contains(rel) || covered_wavs.contains(rel) {
            continue;
        }
        if rel.starts_with(".mmcfs/") && !staged.mmcfs {
            continue;
        }
        // a boot project copy the guest did not touch is not re-exported; it is not deleted either
        if rel.starts_with(".mmcfs/boot/") {
            continue;
        }
        // a WAV whose native sample is on the card was handled above; one with no card file was deleted
        report.deleted_by_firmware.push(rel.clone());
        if options.prune {
            move_file(&folder.join(rel), &backup_dir.join(rel))?;
            report.pruned.push(rel.clone());
        }
    }
    fs::write(export_dir.join("REPORT.txt"), report.text())
        .map_err(|e| format!("REPORT.txt: {e}"))?;
    Ok(report)
}

// ----- command line -------------------------------------------------------------------------------------------

/// A card image file read through `RandomAccessRead`.
pub struct FileImage {
    file: Mutex<fs::File>,
    len: u64,
}
impl FileImage {
    pub fn open(path: &Path) -> Result<Self, String> {
        let meta = fs::metadata(path).map_err(|e| format!("{}: {e}", path.display()))?;
        let file = fs::File::open(path).map_err(|e| format!("{}: {e}", path.display()))?;
        Ok(Self {
            file: Mutex::new(file),
            len: meta.len(),
        })
    }
}
impl RandomAccessRead for FileImage {
    fn len(&self) -> u64 {
        self.len
    }
    fn read_at(&self, offset: u64, out: &mut [u8]) -> usize {
        if offset >= self.len {
            return 0;
        }
        let n = usize::try_from((self.len - offset).min(out.len() as u64)).unwrap_or(out.len());
        let Ok(mut file) = self.file.lock() else {
            return 0;
        };
        file.seek(SeekFrom::Start(offset))
            .and_then(|_| file.read_exact(&mut out[..n]))
            .map(|_| n)
            .unwrap_or(0)
    }
}

pub const EXPORT_USAGE: &str = "usage: plusdrive export IMAGE [--overlay FILE] -o FOLDER [--out DIR] [--prune] [--device dt2|dn2]\n  \
     IMAGE   card image (sparse file); --overlay: a `card.overlay` file of the guest's writes\n  \
     -o      the plus_drive/<device> folder to bring up to date\n  \
     --out   holds export/<stamp>/ and backup/<stamp>/ (default out/plusdrive)\n  \
     --prune move files the firmware deleted to the backup (default: list and keep)\n  \
     --device the card's device (default dt2)";

pub fn cli(args: Vec<String>) -> Result<(), String> {
    let (mut image, mut overlay, mut folder, mut out, mut prune) =
        (None, None, None, PathBuf::from("out/plusdrive"), false);
    let mut geometry = Geometry::Dt2;
    let mut it = args.into_iter();
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "--overlay" => {
                overlay = Some(PathBuf::from(it.next().ok_or("missing --overlay value")?))
            }
            "-o" => folder = Some(PathBuf::from(it.next().ok_or("missing -o value")?)),
            "--out" => out = PathBuf::from(it.next().ok_or("missing --out value")?),
            "--prune" => prune = true,
            "--device" => {
                geometry = match it.next().ok_or("missing --device value")?.as_str() {
                    "dt2" => Geometry::Dt2,
                    "dn2" => Geometry::Dn2,
                    other => return Err(format!("unknown device {other}")),
                }
            }
            _ if image.is_none() && !arg.starts_with('-') => image = Some(PathBuf::from(arg)),
            _ => return Err(EXPORT_USAGE.into()),
        }
    }
    let (image, folder) = (image.ok_or(EXPORT_USAGE)?, folder.ok_or(EXPORT_USAGE)?);
    let base = FileImage::open(&image)?;
    let sectors = match overlay {
        Some(path) => {
            parse_overlay(&fs::read(&path).map_err(|e| format!("{}: {e}", path.display()))?)
                .map_err(|e| e.to_string())?
        }
        None => Vec::new(),
    };
    let mut options = Options::new(out);
    options.prune = prune;
    let report = export(&base, &sectors, &folder, geometry, &options)?;
    println!("{}", report.text());
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{Geometry, NamedSample, Pool, SparseImage, build_sample_image};

    fn wav(frames: usize, seed: u8) -> Vec<u8> {
        let data: Vec<u8> = (0..frames)
            .flat_map(|i| (((i as i32 * 37 + seed as i32) % 2000 - 1000) as i16).to_le_bytes())
            .collect();
        let mut out = b"RIFF".to_vec();
        out.extend_from_slice(&(36 + data.len() as u32).to_le_bytes());
        out.extend_from_slice(b"WAVEfmt ");
        for field in [16u32, 0x0001_0001, 48_000, 96_000, 0x0010_0002] {
            out.extend_from_slice(&field.to_le_bytes());
        }
        out.extend_from_slice(b"data");
        out.extend_from_slice(&(data.len() as u32).to_le_bytes());
        out.extend_from_slice(&data);
        out
    }

    fn temp(name: &str) -> PathBuf {
        let dir =
            std::env::temp_dir().join(format!("plusdrive-export-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn card_of(wavs: &[(&str, Vec<u8>)]) -> SparseImage {
        let samples = wavs
            .iter()
            .map(|(n, w)| NamedSample {
                file_name: (*n).into(),
                sample: wav_to_native(w, WavLimits::default()).unwrap(),
            })
            .collect();
        build_sample_image(samples).unwrap().image
    }

    #[test]
    fn export_of_a_built_card_matches_the_input_and_keeps_wavs() {
        let root = temp("roundtrip");
        let folder = root.join("plus_drive/dt2");
        fs::create_dir_all(&folder).unwrap();
        let wavs = [("kick.wav", wav(3000, 1)), ("snare.WAV", wav(40_000, 2))];
        for (n, w) in &wavs {
            fs::write(folder.join(n), w).unwrap();
        }
        let image = card_of(&wavs);
        let mut options = Options::new(root.join("out"));
        options.stamp = Some("t1".into());
        let report = export(&image, &[], &folder, Geometry::Dt2, &options).unwrap();
        assert!(report.issues.is_empty(), "{:?}", report.issues);
        assert_eq!(report.unchanged.len(), 2, "{report:?}");
        assert!(
            report.added.is_empty()
                && report.replaced.is_empty()
                && report.deleted_by_firmware.is_empty()
        );
        // the staged tree holds the native files, byte for byte
        for (n, w) in &wavs {
            let stem = builder_name(n);
            let native = wav_to_native(w, WavLimits::default()).unwrap().bytes;
            assert_eq!(
                fs::read(report.export_dir.join(String::from_utf8(stem).unwrap())).unwrap(),
                native
            );
        }
        // exporting into an empty folder writes the native files
        let empty = root.join("empty");
        let mut options = Options::new(root.join("out"));
        options.stamp = Some("t2".into());
        let report = export(&image, &[], &empty, Geometry::Dt2, &options).unwrap();
        assert_eq!(report.added, ["kick", "snare"]);
        assert_eq!(
            fs::read(empty.join("kick")).unwrap(),
            fs::read(report.export_dir.join("kick")).unwrap()
        );
        // and a second export changes nothing
        options.stamp = Some("t3".into());
        let again = export(&image, &[], &empty, Geometry::Dt2, &options).unwrap();
        assert!(again.added.is_empty() && again.replaced.is_empty());
        assert_eq!(again.unchanged.len(), 2);
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn a_synthetic_overlay_replaces_a_file_and_the_old_one_is_backed_up() {
        let root = temp("overlay");
        let folder = root.join("f");
        fs::create_dir_all(&folder).unwrap();
        let w = wav(2000, 3);
        let image = card_of(&[("a.wav", w.clone())]);
        // first export into the folder: native file `a`
        let mut options = Options::new(root.join("out"));
        options.stamp = Some("s1".into());
        export(&image, &[], &folder, Geometry::Dt2, &options).unwrap();
        let before = fs::read(folder.join("a")).unwrap();
        // the overlay rewrites the first PCM bytes of that file's page
        let fs_view = Fs::open(&image).unwrap();
        let walk = fs_view.walk();
        let rec = &walk.nodes[0].record;
        let physical = fs_view.extents(rec).unwrap()[0].2;
        let sector = crate::page_sector(physical).unwrap();
        let mut written = [0u8; 64];
        let mut data = [0u8; 512];
        written[8] = 0xff; // bytes 0x40..0x48 (PCM start)
        data[0x40..0x48].copy_from_slice(&[9, 9, 9, 9, 9, 9, 9, 9]);
        let overlay = [CardOverlaySector {
            sector,
            written,
            data,
        }];
        options.stamp = Some("s2".into());
        let report = export(&image, &overlay, &folder, Geometry::Dt2, &options).unwrap();
        assert_eq!(report.replaced, ["a"], "{report:?}");
        let after = fs::read(folder.join("a")).unwrap();
        assert_eq!(after.len(), before.len());
        assert_eq!(&after[0x40..0x48], &[9; 8]);
        assert_eq!(fs::read(report.backup_dir.join("a")).unwrap(), before);
        // a file the folder has and the card lacks is listed and kept, then pruned on request
        fs::write(folder.join("gone"), b"x").unwrap();
        options.stamp = Some("s3".into());
        let report = export(&image, &overlay, &folder, Geometry::Dt2, &options).unwrap();
        assert_eq!(report.deleted_by_firmware, ["gone"]);
        assert!(folder.join("gone").exists());
        options.stamp = Some("s4".into());
        options.prune = true;
        let report = export(&image, &overlay, &folder, Geometry::Dt2, &options).unwrap();
        assert_eq!(report.pruned, ["gone"]);
        assert!(!folder.join("gone").exists());
        assert_eq!(fs::read(report.backup_dir.join("gone")).unwrap(), b"x");
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn mmcfs_slots_are_exported_by_their_content() {
        let root = temp("mmcfs");
        let mut image = build_sample_image(Vec::new()).unwrap().image;
        // table sector: project slot 3, sound slot 5, kit slot 9
        let mut table = [0u8; 512];
        table[0] = 1 << 3;
        table[16] = 1 << 5;
        table[272 + 1] = 1 << 1;
        image.replace_at(0x800 * 512, &table).unwrap();
        let mut slot_bytes = vec![0u8; 512 * 3];
        slot_bytes[0] = 0xab;
        slot_bytes[1024] = 0xcd; // last non-zero sector is the third
        image
            .write_at(
                Geometry::Dt2.slot_sector(Pool::Projects, 3) * 512,
                &slot_bytes,
            )
            .unwrap();
        image
            .write_at(Geometry::Dt2.slot_sector(Pool::Sounds, 5) * 512, &[7; 512])
            .unwrap();
        image
            .write_at(Geometry::Dt2.slot_sector(Pool::Kits, 9) * 512, &[8; 512])
            .unwrap();
        let folder = root.join("f");
        let mut options = Options::new(root.join("out"));
        options.stamp = Some("m".into());
        let report = export(&image, &[], &folder, Geometry::Dt2, &options).unwrap();
        assert_eq!(
            report.added,
            [
                ".mmcfs/kits/0009.bin",
                ".mmcfs/projects/0003.bin",
                ".mmcfs/sounds/0005.bin"
            ]
        );
        assert_eq!(
            fs::read(folder.join(".mmcfs/projects/0003.bin")).unwrap(),
            slot_bytes
        );
        // A boot project copy (COKi) is exported only when the guest wrote it.
        options.stamp = Some("m2".into());
        let report = export(&image, &[], &folder, Geometry::Dt2, &options).unwrap();
        assert!(!folder.join(".mmcfs/boot").exists(), "{report:?}");
        let mut written = [0u8; 64];
        written[0] = 0xff;
        let mut data = [0u8; 512];
        data[..4].copy_from_slice(b"COKj");
        let overlay = [CardOverlaySector {
            sector: 0x40000,
            written,
            data,
        }];
        options.stamp = Some("m3".into());
        let report = export(&image, &overlay, &folder, Geometry::Dt2, &options).unwrap();
        assert_eq!(report.added, [".mmcfs/boot/040000.bin"]);
        assert_eq!(
            &fs::read(folder.join(".mmcfs/boot/040000.bin")).unwrap()[..4],
            b"COKj"
        );
        let _ = fs::remove_dir_all(&root);
    }
}
