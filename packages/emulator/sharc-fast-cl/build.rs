// The second instance (package sharc-fast-cl-2, same sources, built on sharc-native-2) leaves out the
// C ABI symbols, which the first instance of a binary already defines.
fn main() {
    println!("cargo:rustc-check-cfg=cfg(sharc_second)");
    if std::env::var("CARGO_PKG_NAME").as_deref() == Ok("sharc-fast-cl-2") {
        println!("cargo:rustc-cfg=sharc_second");
    }
}
