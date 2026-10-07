//! Where diagnostic output goes. Each diagnostic has an `*_OUT` variable; when
//! it is unset but `DIGI_DIAG_DIR` is set, the output is `$DIGI_DIAG_DIR/<name>`
//! (the directory is created). tools/digi_env.py `diag_env` sets both.

use std::ffi::OsString;
use std::path::PathBuf;

/// `$DIGI_DIAG_DIR`, created on first use; None when unset or unusable.
pub fn dir() -> Option<PathBuf> {
    let d = PathBuf::from(std::env::var_os("DIGI_DIAG_DIR").filter(|v| !v.is_empty())?);
    std::fs::create_dir_all(&d).ok()?;
    Some(d)
}

/// The explicit `out_env` path, else `$DIGI_DIAG_DIR/name`.
pub fn out_path(out_env: &str, name: &str) -> Option<OsString> {
    std::env::var_os(out_env).or_else(|| dir().map(|d| d.join(name).into_os_string()))
}
