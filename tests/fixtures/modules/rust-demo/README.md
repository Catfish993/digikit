# test/rust-demo

A test fixture, not for players. A `#![no_std]` Rust crate (`src/rs/`) built with rustc_codegen_gcc for the
ColdFire MCF54418 and placed in `cf.expansion`: a counter in bss, a 64-bit FNV hash (the 64-bit multiply helper), a
`match`, a function-pointer table and a 64-bit divide. Nothing in the firmware calls it; the tests call the functions
in place and read the results back. Emulator only.

Build: `mise run module:build -- tests/fixtures/modules/rust-demo` (needs `uv run python -m tools.patchkit.pk_toolchains
codegen-gcc` once). Tests: `tests/patchkit/test_patchkit_rust.py`. Findings: `docs/findings/26-rust-on-coldfire.md`.
