# Native crates

One cargo workspace (root `Cargo.toml`, one `Cargo.lock`, profiles defined at
the root) holds the crates that build together: `sharc-rt`, `sharc-gen`,
`sharc`, `sharc-fast-cl`, `boot` and its path dependencies (`coldfire`,
`coldfire/cfabi`, `periph`, `card`, `machine`, `loader`, `plusdrive`,
`device-profile`), `live`, `host` and `packages/desktop/src-tauri`.

Left out: `sharc-jit` (wasmtime and wasm32-only runtime crates, its own
lockfile, nothing in the workspace depends on it) and `.claude/` (agent
worktrees live inside the tree and must not become members). A crate is never
built with its own manifest's profile: cargo reads profiles only from the root.

## Profiles

- `release`: timing and shipping profile (opt-level 3, 16 codegen units,
  unwinding panics: what the desktop app has always used; PGO is trained on it).

There is no cheaper profile for correctness-only runs. Measured on `sharc-gen`
(80 MB generated core): an opt-level 1 override costs about 2.5x the CPU of
release (5.5 min wall vs 2.9 min), and opt-level 0 compiles in about 1.4 min but
runs `coupled_ready_exactness` in 150 s instead of a few seconds (and overflows
the default thread stack). Use `release`.

## Target dirs

`sharc-gen` (the generated SHARC+ core, 80 MB of Rust) takes minutes to compile
and cargo recompiles it for each target dir and each `SHARC_GEN_DIR`. So tools
build into `out/native/target/<gen-key>` (`tools/native_target.py`):

| key        | generated core                                         |
|------------|--------------------------------------------------------|
| `prod`     | `out/native/dn2-audio/gen` (desktop app, PGO)          |
| `nogen`    | none (`SHARC_GEN_DIR` unset): CF-only builds, wasm CF  |
| `generic`  | any other transpile-only gen (no `image.rs`)           |
| `g-<hash>` | any other gen with an image: hash of its resolved path |

Everything for one gen goes into its key dir, built with plain `--release`
(the desktop app, `-p sharc-fast-cl --examples`, `native/sharc` tests, the PGO
control build), so `sharc-gen` compiles once. Builds with different RUSTFLAGS
(PGO) get a subdir of the same key: `pgo-instrument/`, `pgo-use/`. Switching gen
never touches another gen's build.

```
export SHARC_GEN_DIR=$PWD/out/native/dn2-audio/gen
T=$(uv run python tools/native_target.py dir)
cargo build --release --locked --target-dir "$T" -p sharc-fast-cl --examples
```

In a git worktree the root is `out/native/target-wt/<worktree-name>/` instead
(`out/` is linked to the main tree's, and two checkouts in one target dir would
invalidate each other). A worktree cannot reuse the main tree's compiled
`sharc-gen` -- different package paths and fresh file mtimes make cargo rebuild
it -- so it pays one compile per gen, then reuses its own dir.

A plain `cargo build` without `--target-dir` uses `./target` (ignored by git).
Wrap heavy builds in `uv run python tools/benchlock.py run --shared -- ...`.
