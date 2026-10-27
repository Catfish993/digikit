// Compiles tests/c/*.c against sdk/include into a static library that the tests link.
// The system C compiler is called directly (the `cc` crate is not vendored).
use std::path::PathBuf;
use std::process::Command;

fn main() {
    let root = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap());
    let out = PathBuf::from(std::env::var("OUT_DIR").unwrap());
    let include = root.join("../../../../sdk/include");
    let src_dir = root.join("tests/c");
    println!("cargo:rerun-if-changed={}", src_dir.display());
    println!("cargo:rerun-if-changed={}", include.display());
    println!("cargo:rerun-if-env-changed=CC");
    let cc = std::env::var("CC").unwrap_or_else(|_| "cc".into());

    let mut sources: Vec<PathBuf> = std::fs::read_dir(&src_dir)
        .expect("tests/c")
        .map(|e| e.unwrap().path())
        .filter(|p| p.extension().is_some_and(|e| e == "c"))
        .collect();
    sources.sort();
    let mut objects = Vec::new();
    for src in &sources {
        let obj = out.join(src.file_stem().unwrap()).with_extension("o");
        let status = Command::new(&cc)
            .args(["-std=c11", "-Wall", "-Wextra", "-Werror", "-O1", "-c"])
            .arg("-I")
            .arg(&include)
            .arg(src)
            .arg("-o")
            .arg(&obj)
            .status()
            .unwrap_or_else(|e| panic!("cannot run {cc}: {e}"));
        assert!(status.success(), "C compile failed: {}", src.display());
        objects.push(obj);
    }
    let lib = out.join("libdkuisample.a");
    let _ = std::fs::remove_file(&lib);
    let ar = std::env::var("AR").unwrap_or_else(|_| "ar".into());
    let status = Command::new(ar)
        .arg("crs")
        .arg(&lib)
        .args(&objects)
        .status()
        .expect("ar");
    assert!(status.success(), "ar failed");
    println!("cargo:rustc-link-search=native={}", out.display());
    println!("cargo:rustc-link-lib=static=dkuisample");
}
