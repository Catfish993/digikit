//! The module library: the built `.dkm` files the Transfer page offers. The app does not build or pack; a dev task (`mise run
//! module:pack-all`, run by `mise run workbench`) writes one folder per device, `out/modules/<device>/<id>.dkm`, and this file lets
//! the page read it (the one thing the page cannot do itself: file access). The folder is the single constant below; a packaged app
//! would bundle a folder and point it here.
use std::path::{Path, PathBuf};

use serde::Serialize;

/// The library folder, relative to the repository root (one subfolder per device: `dt2`, `dn2`).
const LIBRARY_DIR: &str = "out/modules";

#[derive(Serialize, Debug, PartialEq)]
pub struct LibFile {
    /// Relative to the device's folder, `/` separated (`digikit/pluck.dkm`).
    pub path: String,
    pub size: u64,
}

fn device_dir(root: &Path, device: &str) -> Result<PathBuf, String> {
    if device.is_empty() || !device.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit()) {
        return Err(format!("not a device id: {device:?}"));
    }
    Ok(root.join(LIBRARY_DIR).join(device))
}

fn walk(dir: &Path, base: &Path, out: &mut Vec<LibFile>) -> std::io::Result<()> {
    for entry in std::fs::read_dir(dir)? {
        let entry = entry?;
        let path = entry.path();
        let meta = entry.metadata()?;
        if meta.is_dir() {
            walk(&path, base, out)?;
        } else if path.extension().is_some_and(|e| e == "dkm") {
            let rel = path.strip_prefix(base).unwrap_or(&path);
            let rel = rel.components().map(|c| c.as_os_str().to_string_lossy()).collect::<Vec<_>>().join("/");
            out.push(LibFile { path: rel, size: meta.len() });
        }
    }
    Ok(())
}

/// The `.dkm` files under the device's folder, by path. A missing folder is an empty library.
pub fn scan(root: &Path, device: &str) -> Result<Vec<LibFile>, String> {
    let dir = device_dir(root, device)?;
    let mut out = vec![];
    match walk(&dir, &dir, &mut out) {
        Ok(()) => {}
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(e) => return Err(format!("{}: {e}", dir.display())),
    }
    out.sort_by(|a, b| a.path.cmp(&b.path));
    Ok(out)
}

/// The bytes of one library file; a path that leaves the device's folder is refused.
pub fn read(root: &Path, device: &str, rel: &str) -> Result<Vec<u8>, String> {
    let dir = device_dir(root, device)?;
    if rel.split('/').any(|p| p.is_empty() || p == "." || p == "..") || !rel.ends_with(".dkm") {
        return Err(format!("not a library file: {rel:?}"));
    }
    let path = dir.join(rel);
    std::fs::read(&path).map_err(|e| format!("{}: {e}", path.display()))
}

fn root() -> Result<PathBuf, String> {
    digi_control::runtime_patch::find_root(None)
}

/// The built modules of a device (`dt2`, `dn2`): path and size of each.
#[tauri::command]
pub async fn lib_scan(device: String) -> Result<Vec<LibFile>, String> {
    tauri::async_runtime::spawn_blocking(move || scan(&root()?, &device))
        .await
        .map_err(|e| e.to_string())?
}

/// One library file as raw bytes.
#[tauri::command]
pub async fn lib_read(device: String, path: String) -> Result<tauri::ipc::Response, String> {
    tauri::async_runtime::spawn_blocking(move || read(&root()?, &device, &path).map(tauri::ipc::Response::new))
        .await
        .map_err(|e| e.to_string())?
}

#[cfg(test)]
mod tests {
    use super::*;

    fn library() -> PathBuf {
        let root = std::env::temp_dir().join(format!("digi-library-test-{}", std::process::id()));
        let dir = root.join(LIBRARY_DIR).join("dt2");
        std::fs::create_dir_all(dir.join("digikit")).unwrap();
        std::fs::create_dir_all(dir.join("test")).unwrap();
        std::fs::write(dir.join("digikit/pluck.dkm"), b"PLUCK").unwrap();
        std::fs::write(dir.join("test/sine.dkm"), b"SINE!!").unwrap();
        std::fs::write(dir.join("notes.txt"), b"not a module").unwrap();
        root
    }

    #[test]
    fn the_scan_lists_dkm_files_by_path_and_a_missing_folder_is_empty() {
        let root = library();
        assert_eq!(
            scan(&root, "dt2").unwrap(),
            vec![
                LibFile { path: "digikit/pluck.dkm".into(), size: 5 },
                LibFile { path: "test/sine.dkm".into(), size: 6 }
            ]
        );
        assert_eq!(scan(&root, "dn2").unwrap(), vec![]);
        assert!(scan(&root, "../x").is_err());
        assert_eq!(read(&root, "dt2", "digikit/pluck.dkm").unwrap(), b"PLUCK");
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn a_read_stays_inside_the_device_folder() {
        let root = library();
        for bad in ["../dn2/x.dkm", "digikit/../../x.dkm", "/etc/passwd", "notes.txt", "", "a//b.dkm"] {
            assert!(read(&root, "dt2", bad).is_err(), "{bad}");
        }
        assert!(read(&root, "dt2", "nothing.dkm").is_err());
        let _ = std::fs::remove_dir_all(&root);
    }
}
