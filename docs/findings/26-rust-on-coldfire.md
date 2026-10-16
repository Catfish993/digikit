# 26. Rust on the ColdFire (digikit runtime, Phase 3 toolchain)

Plan: `docs/plans/digikit-runtime.md`. Builds on S1/S1b in finding 23. One lane, 2026-10-16. Nothing here ran on a
device. Marks: **[D]** measured or read once, **[O]** open.

## What ran **[D]**

- `modules/test/rust-demo` is a `#![no_std]`, `panic = "abort"` crate (`src/rs/`, 6 exported symbols) built with
  rustc_codegen_gcc for `-mcpu=54418`, converted by `tools/patchkit/pk_build` into a committed
  `build/rust-demo.dko.json` and placed in `cf.expansion` by the stock patchkit linker (no linker change).
  Functions: a bss counter that stores a magic and a count, a 64-bit FNV-1a hash (u64 multiply), a `match` with a
  function-pointer table, a u64 multiply and a u64 divide, and a self test that runs a slice iterator sum, the hash and
  all ops.
- Patched into DT2 1.16 (also DN2 1.11, DT2 1.17, DN2 1.12) with `patchkit apply`; booted; the functions were called
  in place (`pk_cfcall`) and agree with a Python reference: counter 1, 2, 3 and the state words
  `(0x52555354, n, checksum, 0)`, the hash, every op, the self test. Callee-saved registers and the stack pointer
  are intact after each call. `tests/patchkit/test_patchkit_rust.py`: 6 tests, 35 s.
- Image checks: base sha256 of `out/sections/dt2-1.16/.source-sha256` equals the `.syx` and the plan's `base`
  (`278541e4...`); patched image `263045a0...`.
- Instruments (DT2 1.16, boot to ready plus two calls, 9.4 s wall): `DIGI_FLUSH_CHECK=1` and `DIGI_WRITE_WATCH` on
  `0x47e8b000-0x47fe0000` after the CACR write with `ALLOW_PC` = the module's code (`0x47e8b000-0x47e8b228`): 0 write
  events, 0 flush hits; the check saw 105.3M stores, 138K fetches, 1.03M CPUSHL. Control with `ALLOW_PC` elsewhere:
  642 events, all from the module's code (stack pushes of the call harness at `0x47fe0000` downwards and the bss
  writes), so the watch sees the module. The reset-path loader copies the code before the caches are on.
- Harness: matrix cell `dt2-1.16:test/rust-demo` with the quick test `boots` (frame equals the stock main frame, no
  fault) passes cold in 35 s, 9 s from the snapshot cache.

## Toolchain, in the repository **[D]**

`uv run python -m tools.patchkit.pk_toolchains codegen-gcc` (after `m68k`) does, each stage skipped when its output
exists: `rustup` install of `nightly-2026-10-11` (rust-src, rustc-dev, llvm-tools); rust-lang/gcc at
`37ea7cd851f04ee6a313c5067c9fde9328f876aa` configured `--target=m68k-elf --enable-host-shared
--enable-languages=jit,c` and patched with `tools/patchkit/toolchain/libgccjit-darwin.patch` (libc++ header clash,
`std::_Hash_bytes`, a static initializer that read `targetm` before it was set, a stale Darwin export list); rustc_codegen_gcc at
`a7d8a5227b520187e3d93c1f7fc01dd1ba8659cf` with `rustc_codegen_gcc-driver-mcpu.patch` (3 lines: `-mcpu=` also goes to the GCC
driver so `m68k-elf-as` accepts ColdFire forms). The target is `tools/patchkit/toolchain/m68k-cf-none-elf-a2.json`
(`i64:16:64`). Both patches apply cleanly to pristine copies of the pinned files (`git apply --check`).

- The installer was **not** run from scratch (the S1b copy under `out/tools/codegen-gcc` has the same pins and was
  reused). S1b needed about 75 min including debugging; a scripted run is untimed **[O]**. Its macOS-specific part
  (the `make -C gcc all` workaround after `all-gcc` stops in `jit/`) is transcribed from the S1b notes and untested
  as code **[O]**. The libgccjit stage needs Homebrew gmp, mpfr, libmpc, isl, zstd and Apple clang.
- Module build: a Cargo.toml as the only entry of `sources` selects `tools/patchkit/pk_rust.py`:
  `cargo +nightly rustc --release -Zbuild-std=core,compiler_builtins -Zbuild-std-features=compiler-builtins-mem`,
  then `ld -r --gc-sections -u <the crate's global symbols>` over the staticlib, `objcopy -L '_R*'` and
  `strip --strip-unneeded`. `-Zlocation-detail=none` removes source paths from panic data. 6 s cold, 0.9 s for
  `--check`. The object is byte-identical when the crate is built from another directory (compared).
- Refusals with clear messages: ELF flags other than ISA_C + EMAC (checked on every archive member and the result),
  a compiler helper left undefined, a relocation other than `R_68K_32` (a Rust object has only that type), and
  patchkit's own decoder pass over every instruction (535 here).

## Helpers **[D]**

- `core`, `compiler_builtins` (with `mem`) and the crate are compiled for the ColdFire by the same backend. The final
  object has no libgcc: `__muldi3` (28 B plus 154 B of compiler_builtins code), `__udivdi3` (28 B plus 44 B plus
  818 B of the shared 64-bit divide) and `memcpy`, `memset`, `memcmp` come from Rust and are ColdFire code.
- Probe crate (not committed): u64 and i64 divide and remainder, u64 multiply and shifts, u128 multiply, `clz`,
  slice copy, fill and compare, f32 and f64 multiply, f32 to i32, a bounds-checked index. It links with no undefined
  symbol, 9192 B of code, 2856 instructions, all accepted by the ColdFire decoder, only `R_68K_32`.
- The u64 multiply and divide helpers computed correct results in the emulator (hash and op 6 of the demo).
- C modules: the repo's m68k-elf libgcc is 68020 code (finding 23). A C module that multiplies or divides 64-bit
  values would link against it at patch time and fail the decoder. Recommendation: build libgcc for `-mcpu=54418`
  in `pk_toolchains m68k` (`--with-multilib-list=` with a ColdFire `mcpu`, or a second libgcc build with
  `CFLAGS_FOR_TARGET=-mcpu=54418`) and give C objects the same `ld -r` against it; or let C modules call
  compiler_builtins from a shared Rust runtime object. Not done here.

## Code size against C **[D]**

The 64-bit FNV-1a loop (`pk_rs_hash` against `src/rs/equiv.c`, gcc -mcpu=54418 -Os): C 78 B; Rust `opt-level="s"`
96 B plus a 12 B non-inlined `slice::iter::into_iter` (108 B, +38%); `opt-level=2` 100 B (+28%). Both call
`__muldi3` for the multiply; Rust has to carry its own copy (182 B, shared by all functions of the object). Whole demo:
1494 B of code including 1072 B of helpers (`__udivdi3` and its shared routine), 44 B of data, 16 B of bss.
One function, one compiler version: not a general result.

## Open **[O]**

- The installer from scratch; the backend on larger crates (`alloc`, `core::fmt`); `#[inline]` at `-Os` (the backend did
  not inline `into_iter`).
- The 8-byte struct result differs between C and Rust (finding 23): keep scalar results at the module boundary.
- Hardware has not seen it. The Rust `memcpy` is byte-wise C-like code from compiler_builtins; no word-wise tuning.
- Calls into Rust from firmware hooks (a `hook`/`retarget` op to a Rust function) are not exercised here; the tests
  call the functions with the gcc m68k ABI, which Rust's `extern "C"` matches for integers and pointers.
