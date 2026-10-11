//! In-process +Drive cards for the DT2: a folder of WAV files becomes a card
//! image (no Python, no subprocess), cached by a hash of the firmware and the
//! samples.
//!
//! Layout under the run directory:
//!
//! ```text
//! <run_dir>/cards/<key12>/dt2.img        sparse card image (4 GB logical)
//! <run_dir>/cards/<key12>/ready/         digi-audio-m5.snap(.dsp), the ready state
//! <run_dir>/cards/<key12>/SOURCE.json    what the card was built from
//! ```
//!
//! Everything under `cards/` is derived from the firmware and the user's
//! samples: it stays in the git-ignored `out/` tree and is never committed.

use std::{
    fs,
    io::{Seek, SeekFrom, Write},
    path::{Path, PathBuf},
};

use plusdrive_format::{
    Dt2Seed, MAX_SAMPLES, NamedSample, WavLimits, build_sample_image_with_project, wav_to_native,
};
use serde_json::json;
use sha2::{Digest, Sha256};

/// Bump when the card layout or the track mapping changes, so old caches are
/// not reused: it is part of the key.
pub const FORMAT_TAG: &str = "dt2-card-v2-track-n-file-n";
const MAX_WAV_BYTES: u64 = 64 * 1024 * 1024;
const MAX_NATIVE_BYTES: usize = 256 * 1024 * 1024;

#[derive(Clone, Debug)]
pub struct SampleFile {
    pub name: String,
    pub path: PathBuf,
}

/// The top-level `.wav` files of `dir`, sorted by name.
pub fn list_wavs(dir: &Path) -> Result<Vec<SampleFile>, String> {
    if !dir.is_dir() {
        return Err(format!("{} is not a directory", dir.display()));
    }
    let mut files = Vec::new();
    for entry in fs::read_dir(dir).map_err(|e| format!("{}: {e}", dir.display()))? {
        let entry = entry.map_err(|e| e.to_string())?;
        let path = entry.path();
        let is_wav = path
            .extension()
            .is_some_and(|extension| extension.eq_ignore_ascii_case("wav"));
        if is_wav && entry.file_type().map_err(|e| e.to_string())?.is_file() {
            let name = entry
                .file_name()
                .into_string()
                .map_err(|_| "a sample file name is not UTF-8".to_string())?;
            files.push(SampleFile { name, path });
        }
    }
    files.sort_by(|a, b| a.name.cmp(&b.name));
    if files.is_empty() {
        return Err(format!("no .wav files directly in {}", dir.display()));
    }
    if files.len() > MAX_SAMPLES {
        return Err(format!("{} samples, at most {MAX_SAMPLES}", files.len()));
    }
    Ok(files)
}

/// Where a card lives and whether its parts exist.
#[derive(Clone, Debug)]
pub struct CardPaths {
    pub key: String,
    pub dir: PathBuf,
    pub image: PathBuf,
    pub ready: PathBuf,
}

impl CardPaths {
    pub fn new(run_dir: &Path, key: &str) -> Self {
        let dir = run_dir.join("cards").join(&key[..12.min(key.len())]);
        Self {
            key: key.to_string(),
            image: dir.join("dt2.img"),
            ready: dir.join("ready"),
            dir,
        }
    }
    pub fn snapshot(&self) -> PathBuf {
        self.ready.join("digi-audio-m5.snap")
    }
    pub fn dsp_state(&self) -> PathBuf {
        self.ready.join("digi-audio-m5.snap.dsp")
    }
    /// Both ready files exist (the cold boot writes the snapshot last).
    pub fn ready_cached(&self) -> bool {
        self.snapshot().is_file() && self.dsp_state().is_file()
    }
}

/// The cache key: firmware, format tag, and every sample's name and bytes.
pub fn card_key(syx: &[u8], files: &[SampleFile]) -> Result<String, String> {
    let mut hash = Sha256::new();
    hash.update(b"firmware\0");
    hash.update(Sha256::digest(syx));
    hash.update(b"\0format\0");
    hash.update(FORMAT_TAG.as_bytes());
    for file in files {
        let bytes = fs::read(&file.path).map_err(|e| format!("{}: {e}", file.path.display()))?;
        hash.update(b"\0sample\0");
        hash.update(file.name.as_bytes());
        hash.update(b"\0");
        hash.update(Sha256::digest(&bytes));
    }
    Ok(hash
        .finalize()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect())
}

/// What a card build produced.
#[derive(Clone, Debug)]
pub struct CardInfo {
    pub samples: Vec<String>,
    pub image_bytes_written: u64,
}

/// Convert `files` and write the seeded image to `image` (through a `.part`
/// file, so a crash never leaves a half image under the final name).
/// `progress(done, total)` is called after each file is converted.
pub fn build_card(
    syx: &[u8],
    files: &[SampleFile],
    image: &Path,
    mut progress: impl FnMut(usize, usize),
) -> Result<CardInfo, String> {
    let seed = Dt2Seed::from_syx(syx)?;
    let mut samples = Vec::with_capacity(files.len());
    let mut total = 0usize;
    for (index, file) in files.iter().enumerate() {
        let size = fs::metadata(&file.path)
            .map_err(|e| format!("{}: {e}", file.path.display()))?
            .len();
        if size > MAX_WAV_BYTES {
            return Err(format!("{} exceeds the 64 MiB WAV limit", file.name));
        }
        let wav = fs::read(&file.path).map_err(|e| format!("{}: {e}", file.path.display()))?;
        let sample = wav_to_native(&wav, WavLimits::default())
            .map_err(|e| format!("{}: {e:?}", file.name))?;
        total = total
            .checked_add(sample.bytes.len())
            .filter(|total| *total <= MAX_NATIVE_BYTES)
            .ok_or("the converted samples exceed 256 MiB")?;
        samples.push(NamedSample {
            file_name: file.name.clone(),
            sample,
        });
        progress(index + 1, files.len());
    }
    let built = build_sample_image_with_project(samples, seed.seed())
        .map_err(|e| format!("card image: {e:?}"))?;
    if let Some(parent) = image.parent() {
        fs::create_dir_all(parent).map_err(|e| format!("{}: {e}", parent.display()))?;
    }
    let mut partial = image.as_os_str().to_owned();
    partial.push(".part");
    let partial = PathBuf::from(partial);
    let _ = fs::remove_file(&partial);
    let written = (|| -> Result<u64, String> {
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&partial)
            .map_err(|e| format!("{}: {e}", partial.display()))?;
        file.set_len(built.image.logical_len())
            .map_err(|e| e.to_string())?;
        let mut bytes = 0u64;
        for (&at, data) in built.image.ranges() {
            file.seek(SeekFrom::Start(at))
                .and_then(|_| file.write_all(data))
                .map_err(|e| e.to_string())?;
            bytes += data.len() as u64;
        }
        file.flush().map_err(|e| e.to_string())?;
        Ok(bytes)
    })();
    let written = match written {
        Ok(bytes) => bytes,
        Err(error) => {
            let _ = fs::remove_file(&partial);
            return Err(error);
        }
    };
    fs::rename(&partial, image).map_err(|e| format!("{}: {e}", image.display()))?;
    Ok(CardInfo {
        samples: built
            .files
            .iter()
            .map(|file| file.file_name.clone())
            .collect(),
        image_bytes_written: written,
    })
}

/// Build the card of `files` unless it is cached; the paths and whether the
/// image was already there.
pub fn ensure_card(
    run_dir: &Path,
    syx: &[u8],
    files: &[SampleFile],
    progress: impl FnMut(usize, usize),
) -> Result<(CardPaths, bool), String> {
    let key = card_key(syx, files)?;
    let paths = CardPaths::new(run_dir, &key);
    if paths.image.is_file() {
        return Ok((paths, true));
    }
    let info = build_card(syx, files, &paths.image, progress)?;
    let source = json!({
        "key": paths.key,
        "format": FORMAT_TAG,
        "firmware_sha256": Sha256::digest(syx).iter().map(|b| format!("{b:02x}")).collect::<String>(),
        "samples": info.samples,
    });
    let _ = fs::write(
        paths.dir.join("SOURCE.json"),
        serde_json::to_string_pretty(&source).unwrap_or_default(),
    );
    Ok((paths, false))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn wav(frequency: f64, frames: usize) -> Vec<u8> {
        let mut data = Vec::new();
        for i in 0..frames {
            let value = (0.5
                * (std::f64::consts::TAU * frequency * i as f64 / 48_000.0).sin()
                * 32767.0) as i16;
            data.extend_from_slice(&value.to_le_bytes());
        }
        let mut out = b"RIFF".to_vec();
        out.extend_from_slice(&(36 + data.len() as u32).to_le_bytes());
        out.extend_from_slice(b"WAVEfmt ");
        out.extend_from_slice(&16u32.to_le_bytes());
        out.extend_from_slice(&1u16.to_le_bytes());
        out.extend_from_slice(&1u16.to_le_bytes());
        out.extend_from_slice(&48_000u32.to_le_bytes());
        out.extend_from_slice(&96_000u32.to_le_bytes());
        out.extend_from_slice(&2u16.to_le_bytes());
        out.extend_from_slice(&16u16.to_le_bytes());
        out.extend_from_slice(b"data");
        out.extend_from_slice(&(data.len() as u32).to_le_bytes());
        out.extend_from_slice(&data);
        out
    }

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("digi-cards-{name}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[test]
    fn lists_only_top_level_wavs_sorted() {
        let dir = scratch("list");
        fs::write(dir.join("b.wav"), wav(440.0, 100)).unwrap();
        fs::write(dir.join("A.WAV"), wav(440.0, 100)).unwrap();
        fs::write(dir.join("note.txt"), b"x").unwrap();
        fs::create_dir_all(dir.join("sub")).unwrap();
        fs::write(dir.join("sub/c.wav"), wav(440.0, 100)).unwrap();
        let names: Vec<_> = list_wavs(&dir)
            .unwrap()
            .into_iter()
            .map(|f| f.name)
            .collect();
        assert_eq!(names, ["A.WAV", "b.wav"]);
        assert!(list_wavs(&dir.join("sub/none")).is_err());
        let empty = scratch("empty");
        assert!(list_wavs(&empty).unwrap_err().contains("no .wav"));
    }

    #[test]
    fn key_follows_firmware_names_and_bytes() {
        let dir = scratch("key");
        fs::write(dir.join("a.wav"), wav(220.0, 480)).unwrap();
        fs::write(dir.join("b.wav"), wav(880.0, 480)).unwrap();
        let files = list_wavs(&dir).unwrap();
        let key = card_key(b"fw1", &files).unwrap();
        assert_eq!(key, card_key(b"fw1", &files).unwrap());
        assert_ne!(key, card_key(b"fw2", &files).unwrap());
        fs::write(dir.join("b.wav"), wav(881.0, 480)).unwrap();
        assert_ne!(key, card_key(b"fw1", &files).unwrap());
        let paths = CardPaths::new(Path::new("run"), &key);
        assert!(
            paths
                .image
                .ends_with(format!("cards/{}/dt2.img", &key[..12]))
        );
        assert!(!paths.ready_cached());
    }

    /// With the DT2 1.16 .syx (`DT2_SYX`, or the repository root's): three
    /// WAV files build a card whose track N sample is file N, and a second
    /// call reuses the cache.
    #[test]
    fn builds_a_seeded_card_from_three_wavs_and_caches_it() {
        let Some(syx) = std::env::var_os("DT2_SYX")
            .map(PathBuf::from)
            .or_else(|| {
                Path::new(env!("CARGO_MANIFEST_DIR"))
                    .ancestors()
                    .map(|dir| dir.join("Digitakt_II_OS1.16.syx"))
                    .find(|path| path.exists())
            })
            .and_then(|path| fs::read(path).ok())
        else {
            return;
        };
        let dir = scratch("build");
        let samples = dir.join("samples");
        fs::create_dir_all(&samples).unwrap();
        for (name, frequency) in [("1-a220.wav", 220.0), ("2-b880.wav", 880.0)] {
            fs::write(samples.join(name), wav(frequency, 4800)).unwrap();
        }
        fs::write(samples.join("3-c3001.wav"), wav(3001.0, 4800)).unwrap();
        let files = list_wavs(&samples).unwrap();
        let mut seen = Vec::new();
        let (paths, cached) = ensure_card(&dir.join("run"), &syx, &files, |done, total| {
            seen.push((done, total))
        })
        .unwrap();
        assert!(!cached);
        assert_eq!(seen, [(1, 3), (2, 3), (3, 3)]);
        assert_eq!(fs::metadata(&paths.image).unwrap().len() % 512, 0);
        let listing = {
            struct Image(fs::File, u64);
            impl emmc_card::RandomAccessRead for Image {
                fn len(&self) -> u64 {
                    self.1
                }
                fn read_at(&self, offset: u64, out: &mut [u8]) -> usize {
                    use std::os::unix::fs::FileExt;
                    self.0.read_at(out, offset).unwrap_or(0)
                }
            }
            let file = fs::File::open(&paths.image).unwrap();
            let len = file.metadata().unwrap().len();
            plusdrive_format::list_image(&Image(file, len)).unwrap()
        };
        let names: Vec<_> = listing
            .iter()
            .map(|entry| String::from_utf8_lossy(&entry.name).into_owned())
            .collect();
        for name in ["1-a220", "2-b880", "3-c3001"] {
            assert!(names.iter().any(|n| n == name), "{names:?}");
        }
        let (again, cached) = ensure_card(&dir.join("run"), &syx, &files, |_, _| {}).unwrap();
        assert!(cached);
        assert_eq!(again.image, paths.image);
        let _ = fs::remove_dir_all(&dir);
    }
}
