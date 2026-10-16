use device_profile::Registry;
use emmc_card::RandomAccessRead;
use plusdrive_format::{
    Content, EntryKind, Geometry, MmcSlots, Pool, ProjectSeed, Tree, TreeFile, TreeOptions,
    WavLimits, build_tree_image, list_image, parse_native, wav_to_native,
};
use sha2::{Digest, Sha256};
use std::{
    env, fs,
    fs::OpenOptions,
    io::{Read, Seek, SeekFrom, Write},
    path::{Path, PathBuf},
    sync::Mutex,
};

struct FileImage {
    file: Mutex<fs::File>,
    len: u64,
}
impl RandomAccessRead for FileImage {
    fn len(&self) -> u64 {
        self.len
    }
    fn read_at(&self, offset: u64, out: &mut [u8]) -> usize {
        if offset
            .checked_add(out.len() as u64)
            .is_none_or(|end| end > self.len)
        {
            return 0;
        }
        let Ok(mut file) = self.file.lock() else {
            return 0;
        };
        file.seek(SeekFrom::Start(offset))
            .and_then(|_| file.read_exact(out))
            .map(|_| out.len())
            .unwrap_or(0)
    }
}
fn usage() -> &'static str {
    "usage: plusdrive build DIR -o NEW_IMAGE [--device dt2|dn2] [--syx FILE] [--no-project] [--capacity-blocks N] [--max-extent-pages N]\n       plusdrive ls IMAGE\n       plusdrive export IMAGE [--overlay FILE] -o FOLDER [--out DIR] [--prune] [--device dt2|dn2]\n\nDIR is a plus_drive/<device> folder: files and folders map to the path filesystem (a .wav becomes a native\nsample named without the extension; any other file is copied byte for byte), and .mmcfs/{projects,sounds,kits}/NNNN.bin\nare raw MmcFs slots. DN2 has no path filesystem: only .mmcfs applies.\nA boot project is seeded from a verified DT2 1.16 SysEx (--syx) when the folder holds a native sample."
}

const WAV_SOURCE_LIMIT: u64 = 64 * 1024 * 1024;

#[derive(Default)]
struct Scan {
    files: Vec<TreeFile>,
    dirs: Vec<String>,
    slots: MmcSlots,
    skipped: Vec<String>,
}

fn slot_number(name: &str) -> Option<u32> {
    let digits = name.strip_suffix(".bin")?;
    (digits.len() == 4 && digits.bytes().all(|b| b.is_ascii_digit()))
        .then(|| digits.parse().ok())
        .flatten()
}

/// Walk `root`. `path_fs` false (DN2) skips everything outside `.mmcfs`.
fn scan(root: &Path, path_fs: bool) -> Result<Scan, String> {
    let mut out = Scan::default();
    let mut stack = vec![(root.to_path_buf(), String::new())];
    while let Some((dir, prefix)) = stack.pop() {
        let mut entries = fs::read_dir(&dir)
            .map_err(|e| format!("{}: {e}", dir.display()))?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| e.to_string())?;
        entries.sort_by_key(|entry| entry.file_name());
        if !prefix.is_empty() && entries.is_empty() && path_fs {
            out.dirs.push(prefix.clone());
        }
        for entry in entries {
            let name = entry
                .file_name()
                .into_string()
                .map_err(|_| format!("{}: name is not UTF-8", entry.path().display()))?;
            let rel = if prefix.is_empty() {
                name.clone()
            } else {
                format!("{prefix}/{name}")
            };
            let kind = entry.file_type().map_err(|e| e.to_string())?;
            if kind.is_symlink() {
                out.skipped.push(format!("{rel} (symbolic link)"));
                continue;
            }
            if prefix.is_empty() && name == ".mmcfs" && kind.is_dir() {
                scan_mmcfs(&entry.path(), &mut out)?;
                continue;
            }
            if name == ".DS_Store" {
                continue;
            }
            if !path_fs {
                out.skipped
                    .push(format!("{rel} (DN2 has no path filesystem)"));
                continue;
            }
            if kind.is_dir() {
                stack.push((entry.path(), rel));
            } else if kind.is_file() {
                let path = entry.path();
                let len = entry.metadata().map_err(|e| e.to_string())?.len();
                let is_wav = Path::new(&name)
                    .extension()
                    .is_some_and(|x| x.eq_ignore_ascii_case("wav"));
                let content = if is_wav {
                    if len > WAV_SOURCE_LIMIT {
                        return Err(format!("{} exceeds WAV source limit", path.display()));
                    }
                    let wav = fs::read(&path).map_err(|e| e.to_string())?;
                    Content::Native(
                        wav_to_native(&wav, WavLimits::default())
                            .map_err(|e| format!("{}: {e:?}", path.display()))?,
                    )
                } else {
                    let bytes = fs::read(&path).map_err(|e| format!("{}: {e}", path.display()))?;
                    match parse_native(bytes.clone()) {
                        Some(sample) => Content::Native(sample),
                        None => Content::Plain(bytes),
                    }
                };
                out.files.push(TreeFile { path: rel, content });
            } else {
                out.skipped.push(format!("{rel} (not a regular file)"));
            }
        }
    }
    Ok(out)
}

fn scan_mmcfs(dir: &Path, out: &mut Scan) -> Result<(), String> {
    for entry in fs::read_dir(dir).map_err(|e| format!("{}: {e}", dir.display()))? {
        let entry = entry.map_err(|e| e.to_string())?;
        let name = entry.file_name().to_string_lossy().into_owned();
        let pool = Pool::from_dir_name(&name).filter(|_| entry.path().is_dir());
        let Some(pool) = pool else {
            out.skipped
                .push(format!(".mmcfs/{name} (not projects, sounds or kits)"));
            continue;
        };
        for slot_file in fs::read_dir(entry.path()).map_err(|e| e.to_string())? {
            let slot_file = slot_file.map_err(|e| e.to_string())?;
            let file_name = slot_file.file_name().to_string_lossy().into_owned();
            let rel = format!(".mmcfs/{name}/{file_name}");
            match (slot_number(&file_name), slot_file.file_type()) {
                (Some(slot), Ok(kind)) if kind.is_file() => {
                    let bytes = fs::read(slot_file.path()).map_err(|e| format!("{rel}: {e}"))?;
                    out.slots.slots.insert((pool, slot), bytes);
                }
                _ => out.skipped.push(format!("{rel} (not NNNN.bin)")),
            }
        }
    }
    Ok(())
}

fn build(args: Vec<String>) -> Result<(), String> {
    let mut source = None;
    let mut output = None;
    let mut capacity = 0x760000u32;
    let mut syx = None;
    let mut no_project = false;
    let mut device = None;
    let mut max_extent_pages = u32::MAX;
    let mut it = args.into_iter();
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "-o" => output = Some(PathBuf::from(it.next().ok_or("missing -o value")?)),
            "--capacity-blocks" => {
                capacity = it
                    .next()
                    .ok_or("missing --capacity-blocks value")?
                    .parse()
                    .map_err(|_| "invalid --capacity-blocks")?
            }
            "--max-extent-pages" => {
                max_extent_pages = it
                    .next()
                    .ok_or("missing --max-extent-pages value")?
                    .parse()
                    .map_err(|_| "invalid --max-extent-pages")?
            }
            "--device" => device = Some(it.next().ok_or("missing --device value")?),
            "--syx" => syx = Some(PathBuf::from(it.next().ok_or("missing --syx value")?)),
            "--no-project" => no_project = true,
            _ if source.is_none() => source = Some(PathBuf::from(arg)),
            _ => return Err(usage().into()),
        }
    }
    let source = source.ok_or(usage())?;
    let output = output.ok_or(usage())?;
    if no_project && syx.is_some() {
        return Err("--syx and --no-project cannot be combined".into());
    }
    if output.exists() {
        return Err("output already exists".into());
    }
    let main;
    let mut seed = None;
    let mut device_short = device;
    if let Some(syx) = &syx {
        let bytes = fs::read(syx).map_err(|e| format!("{}: {e}", syx.display()))?;
        let digest = Sha256::digest(&bytes)
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>();
        let firmware =
            dt2_firmware_loader::parse(&bytes).map_err(|e| format!("{}: {e}", syx.display()))?;
        main = firmware
            .sections
            .into_iter()
            .find(|section| section.id == 3)
            .ok_or("firmware has no MAIN section")?
            .bytes;
        let registry = Registry::load().map_err(|e| e.to_string())?;
        let (found, profile, _) = registry
            .boot_for_main(&digest, &main)
            .map_err(|e| e.to_string())?;
        if device_short.as_deref().is_some_and(|d| d != found.short) {
            return Err(format!("--device does not match {}", syx.display()));
        }
        device_short = Some(found.short.to_string());
        if found.short == "dt2" {
            seed = Some(ProjectSeed {
                main: &main,
                contract: profile
                    .plusdrive_project_contract
                    .ok_or("firmware has no +Drive project contract")?,
            });
        }
    }
    let geometry = match device_short.as_deref().unwrap_or("dt2") {
        "dt2" => Geometry::Dt2,
        "dn2" => Geometry::Dn2,
        other => return Err(format!("unknown device {other}")),
    };
    let scanned = scan(&source, geometry == Geometry::Dt2)?;
    for line in &scanned.skipped {
        eprintln!("plusdrive: skipped {line}");
    }
    let slot_count = scanned.slots.slots.len();
    let tree = Tree {
        files: scanned.files,
        dirs: scanned.dirs,
        slots: scanned.slots,
    };
    let built = build_tree_image(
        tree,
        TreeOptions {
            capacity_blocks: capacity,
            geometry,
            max_extent_pages,
        },
        if no_project { None } else { seed },
    )
    .map_err(|e| format!("build: {e:?}"))?;
    let (mut dirs, mut plain) = (0, 0);
    for entry in &built.entries {
        match entry.kind {
            EntryKind::Dir => dirs += 1,
            EntryKind::Plain => plain += 1,
            EntryKind::Native => {}
        }
    }
    let result = (|| -> Result<(), String> {
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&output)
            .map_err(|e| e.to_string())?;
        file.set_len(built.image.logical_len())
            .map_err(|e| e.to_string())?;
        let mut bytes = 0usize;
        for (&at, data) in built.image.ranges() {
            file.seek(SeekFrom::Start(at))
                .and_then(|_| file.write_all(data))
                .map_err(|e| e.to_string())?;
            bytes += data.len()
        }
        file.flush().map_err(|e| e.to_string())?;
        println!(
            "logical_bytes={} written_bytes={} sample_entries={} plain_files={} directories={} mmcfs_slots={} project={:?}",
            built.image.logical_len(),
            bytes,
            built.files.len(),
            plain,
            dirs - 1,
            slot_count,
            built.project
        );
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&output);
    };
    result
}
fn ls(path: PathBuf) -> Result<(), String> {
    let meta = fs::metadata(&path).map_err(|e| e.to_string())?;
    if !meta.file_type().is_file() || meta.len() % 512 != 0 {
        return Err("image must be a regular 512-byte-aligned file".into());
    };
    let image = FileImage {
        file: Mutex::new(fs::File::open(path).map_err(|e| e.to_string())?),
        len: meta.len(),
    };
    for entry in list_image(&image).map_err(|e| format!("invalid image: {e:?}"))? {
        println!(
            "{}\t{}\t{}\t{}",
            entry.id,
            String::from_utf8_lossy(&entry.name),
            entry.kind,
            entry.size
        )
    }
    Ok(())
}
fn main() {
    let mut args = env::args().skip(1);
    let command = args.next();
    let rest = args.collect();
    let result = match command.as_deref() {
        Some("build") => build(rest),
        Some("export") => plusdrive_format::export::cli(rest),
        Some("ls") => {
            if rest.len() == 1 {
                ls(PathBuf::from(&rest[0]))
            } else {
                Err(usage().into())
            }
        }
        Some("--help") | Some("-h") => {
            println!("{}", usage());
            Ok(())
        }
        _ => Err(usage().into()),
    };
    if let Err(e) = result {
        eprintln!("plusdrive: {e}");
        std::process::exit(2)
    }
}
