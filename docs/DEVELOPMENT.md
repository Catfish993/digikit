# Developing digikit

The details behind the [README](../README.md): firmware files, the emulator and its options, tests, analysis tools and the repository
layout. For modules see [MODULE-AUTHORING.md](MODULE-AUTHORING.md) and [PATCHING.md](PATCHING.md); for tests [TESTING.md](TESTING.md).

## Firmware files

Modules and patchkit target Digitakt II 1.17 and Digitone II 1.12. The emulator runs
these two by default. It also runs 1.16 and 1.11, which most earlier analysis used.
1.15C and 1.10E are kept for old analysis. Get the `.syx` files from Elektron's support
site for your device; the repository never includes them.

| device | version | file | sha256 |
| --- | --- | --- | --- |
| Digitakt II | 1.17 | `Digitakt_II_OS1.17.syx` | `26c22f6652625ac2cfd47f7ee970d388ed8b6427dae3563c0a6a2f2d334350d5` |
| Digitone II | 1.12 | `Digitone_II_OS1.12.syx` | `7e5b8a733d4ab1fdf7e5d22757f7d8eddaa3cbf71d0858874a791844c02ceb35` |
| Digitakt II | 1.16 | `Digitakt_II_OS1.16.syx` | `278541e466edcd77d6b3e018a91fb90185932d3c7de224dd3e68294dddf3a9ec` |
| Digitone II | 1.11 | `Digitone_II_OS1.11.syx` | `2af43e65e3d8390b41c9f66222620f8cce027d73ed87db00c80b440f628472e0` |
| Digitakt II | 1.15C | `Digitakt_II_OS1.15C.syx` | `62d588456e47194bd56dfee9568fb9dd4521c4ff1e8b5427eb461355532e8c6c` |
| Digitone II | 1.10E | `Digitone_II_OS1.10E.syx` | `9881ee5ca4624be2b833e5eb94e294f2f8425371e6729781baccd1c256cac504` |

Put the files in `firmware/`; the tools look there first (`tools/firmware/config.py`).
`firmware/README.md` lists the images and their SHA-256 values. `DIGI_EMU_SYX`
(Digitone II) and `DIGI_EMU_SYX_DT2` (Digitakt II) override the paths.

## Setup

You need [mise](https://mise.jdx.dev/) (it installs the pinned Rust and pnpm) and
[uv](https://docs.astral.sh/uv/) (Python 3.12).

```sh
mise install
mise run setup      # uv sync, pnpm install
```

`mise tasks` lists every task.

## Extract the firmware

```sh
uv run python -m tools.firmware.extract firmware/Digitone_II_OS1.12.syx -o out/sections/dn2-1.12
uv run python -m tools.firmware.extract firmware/Digitakt_II_OS1.17.syx -o out/sections/dt2-1.17
```

Keep each firmware in its own directory (`out/sections/dt2-1.17`, ...): the analysis
tools find images by these names. A sections directory records the SHA-256 of its
`.syx` in `out/sections/<image>/.source-sha256`, and the tools refuse to mix firmwares. Compare it with
`shasum -a 256 <the .syx>` before you trust a run.

## Ready states

The native emulator starts from a ready state: ColdFire booted to the main screen, DSP
running. Build it from the `.syx` alone:

```sh
mise run emu:ready -- dn2                     # Digitone II 1.12 (about 2 minutes)
mise run emu:ready -- dt2 [--card-image IMG]  # Digitakt II 1.17, optionally with a +Drive image
```

The files go to `out/snapshots/<device>-audio-ready-cold-*`. Git ignores them.

## Desktop emulator

```sh
mise run workbench                # build once and run (`mise run workbench -- --build-only` builds only)
mise run workbench -- --cf-only --plus-drive DIR firmware/Digitakt_II_OS1.17.syx   # a +Drive folder (default: plus_drive/<device>/; --samples is the same option)
mise run workbench -- --dsp-patch FILE  # overlay a DSP patch (DN2 audio)
mise run workbench -- --cf-only   # ColdFire only: no DSP, no audio
```

With the DN2 ready inputs present, `mise run workbench` plays DN2 audio. Without them it
starts the ColdFire-only emulator. The DSP needs about 1.6 CPU seconds per audio second
([finding 18](findings/18-dsp-performance.md)), so real time is not reached yet.
The +Drive comes from `plus_drive/dt2/` or `plus_drive/dn2/` (git ignores both) when it exists, or from `--plus-drive DIR` (`--samples DIR` is the same option): the launcher builds a card image from the folder and caches it by content ([docs/SAMPLES.md](SAMPLES.md)).
Track N of the DT2 plays file N (sorted by name; more than 16 files wrap around).

**Choose samples (desktop app, Digitakt II).** With the DT2 running, the toolbar's "Choose
samples" button picks a folder. The app sends its top-level `.wav` files to the controller,
which builds the card in process, cold-boots the DT2 with it (about 12 s the first time at
the default fast tier, `build 1 s + boot 12 s` for three files), presses NO on the FILE SYSTEM
OK window and swaps the runtime; the toolbar shows "Building the card", "Booting the Digitakt
II with the card (n%)" and "Card ready". The card and its ready state are cached by a hash of
the firmware and the samples under `<run dir>/cards/<key>/` (default `out/native/run`), so the
same folder restores in well under a second. It needs the DT2 ready inputs (`mise run emu:ready --
dt2` once: the DSP image and `cold-idle.dsp` come from there). The app remembers the last card
(`<run dir>/cards/last.json`) and mounts it again when a DT2 starts without `--card-image`, in
well under a second; the toolbar shows "Samples: NAME (N files)", or "Choose samples to load a
folder of WAVs" when nothing is cached. A DT2 launch without `SHARC_GEN_DIR` uses the generated
core its PGO profile was trained on (the launcher prints which core and why), so no environment
settings are needed. The browser shows the button
disabled ("desktop only for now"). Headless: the control socket's `card.load {dir}`,
`card.status`; `uv run python -m tools.emulator.card_check` checks that trigs 1-3 play a 220 Hz sine, an
880 Hz sine and noise from three generated files.

## Web panel

```sh
mise run web                       # build the browser core (WASM) and serve the panel
mise run web:build                 # static build; it fails if firmware-derived files would ship
mise run web:wasm                  # only the browser core
mise run web:parity trig1-tap-release   # browser PCM equals native PCM
```
The browser DSP is slow; the browser host is parked on the branch `wip/browser-host`.

## Control socket and the patch loop

```sh
mise run emu:serve                      # headless, paused; serves the control socket
mise run emu:ctl status                 # status, step N, tap CODE, frame out.png, events
uv run python -m tools.control.digi_client inject PATCHED.syx   # hot DSP injection
uv run python -m tools.patchkit.dsp_patch syx firmware/BASE.syx tools/patchkit/dsp_patches/dn2-1.12-master-half.patch OUT.syx
```

`tools/patchkit/dsp_patch.py` edits DSP code or data by SHARC+ address and writes a patched
`.syx`. Control socket reference: [docs/CONTROL.md](CONTROL.md). The protocol design is in [docs/design/emulator-control.md](design/emulator-control.md).

## Scenarios and tests

A scenario is a list of panel inputs (button presses, encoder turns) that the headless
runner plays against a ready state. It reports the PCM hash and level
([tests/scenarios/README.md](../tests/scenarios/README.md)).

```sh
mise run scenario:all         # every DN2 scenario; fails when a pinned PCM hash differs
mise run scenario trig1-taps --wav
mise run test:quick           # fast checks
mise run test                 # pytest, cargo test --workspace, web tests, all scenario pins
mise run test:slow            # slow runs and gates that need private fixtures
mise run lint                 # ruff, oxlint, cargo fmt on the clean crates
mise run verify:pre           # quick lane check, not the merge gate
mise run verify               # the gate: the steps your changed files need
mise run verify:full          # every step; run before merging to main
```

`mise run verify` is the gate, not `mise run verify:pre`. Test layout and commands:
[docs/TESTING.md](TESTING.md).

Pins are valid only for the firmware and ready state they were recorded with.

## Performance and instrumentation

```sh
mise run perf                 # DSP CPU per audio second, tier split, per-function cost ranking
mise run perf:cov             # fast-tier coverage per configuration
```

Method and results: [finding 18](findings/18-dsp-performance.md). Every run tool
writes `out/runs/<scenario-or-kind>/<UTC stamp>/` (report, audio, logs);
[docs/INSTRUMENTATION.md](INSTRUMENTATION.md) lists the output and the switches.

## Analysis tools

For the DSP, `uv run python -m tools.sharc_dev.sharcdb build out/sections/*/section_7_BLOB.bin`
builds `out/sharcdb/<image>.sqlite`. `tools/sharc_dev/sharc.py` queries it: `sharc.load("dt2-1.17")`
gives `func`, `callers`, `callees`, `reach`, `last_def`, `refs`, `trace` and `sql`, and
`uv run python -m tools.sharc_dev.sharc dt2-1.17 "SQL"` runs one query. For the ColdFire OS, `tools/coldfire/ghidradump.py` exports a
Ghidra project, `tools/coldfire/cfdb.py build` makes a database from the dump, and `tools/coldfire/cf.py`
queries it. Decoding uses only public manuals ([docs/sharc/SOURCES.md](sharc/SOURCES.md)).
All tools are indexed in [docs/TOOLS.md](TOOLS.md).

## Patching

patchkit applies modules (new machines, parameters, master-bus effects) to your own stock `.syx` and writes a new
`.syx`. Modules hold our code only, never firmware bytes.

```sh
mise run patch:toolchains   # once: pinned ColdFire and SHARC+ toolchains for building modules
mise run patch -- --firmware firmware/Digitakt_II_OS1.17.syx --modules fx/master-gain -o out/patch/mg.syx
mise run patch:inject -- out/patch/mg.syx   # into a running emulator (mise run workbench, or emu:serve)
mise run web                            # the gallery at /patch: patch in the browser, nothing is uploaded
```

**Writing a module** (create, build, patch, inject into the running emulator, test, and what modules can do):
[docs/MODULE-AUTHORING.md](MODULE-AUTHORING.md). Usage, the dev loop, tests, how it works and the safety rules:
[docs/PATCHING.md](PATCHING.md). Manifest reference: [modules/README.md](../modules/README.md). Facts behind it: [findings 01](findings/01-container-and-patching.md),
[02](findings/02-machines-and-parameters.md) and [21](findings/21-patchkit-core.md).

## Module loop

1. Create the module: `mise run module:new -- fx/NAME` (`--cf` for ColdFire code only, `--dsp` for a SHARC+ stub only).
2. Check it without an emulator: `mise run module:check -- NAME [--device dt2|dn2]` (NAME is `fx/NAME` or `NAME`; machine modules run the conformance suite, then the module's own tests).
3. Run it in the emulator: `mise run dk:run -- fx/NAME --emu`. An emulator must already run (`mise run workbench` or `mise run emu:serve`). It builds, packs, uploads, selects the module, restarts the guest and follows the log.
4. Run it on the device: `mise run dk:run -- fx/NAME --port Digitakt`. The device needs the digikit runtime (version 4 or newer): `mise run patch`, then `mise run patch:preflight` on the image before you flash it.

Each task takes `-- --help`.

**Hardware warning**

- Patched images have run on a Digitakt II (OS 1.17) since 2026-10-18 (docs/findings/47-dt2-device-rungs.md). Flash
  only images that pass `mise run patch:preflight`.
- Never change section 2 (bootstrap), section 4 (updater) or section 8. patchkit refuses.
- Recovery is the bootstrap's STARTUP menu (hold FUNC at power-on, then the stock OS over MIDI DIN); it has not been rehearsed.
- Read `docs/findings/01-container-and-patching.md` before you install anything.

## Repository layout

```
firmware/          stock firmware images you supply (ignored; see firmware/README.md)
apps/              desktop (Tauri) and web shells (browser panel: Astro + Solid)
packages/emulator/ Rust workspace: runtime, ColdFire and SHARC execution, runners
packages/patchkit/ patch toolchain: port lockfiles, linker, apply, verify (CLI and wasm)
packages/*         TypeScript control client and test support
tools/             Python tools, one package per owner (tools/<owner>/, docs/LAYOUT.md)
tests/             pytest suite (native tests live in the crates); see docs/TESTING.md
tests/scenarios/   scripted panel inputs and pinned PCM hashes
modules/           patchkit modules
devices/           device profiles (firmware hashes, panel layout), LED maps, and ports/
docs/              findings, designs, handovers
out/               everything generated locally (ignored): builds, runs, caches, out/sections/<image>, out/snapshots
```

Who owns what, and planned moves: [docs/LAYOUT.md](LAYOUT.md).

## Documentation map

- [docs/FINDINGS.md](FINDINGS.md): the results, one file per topic in
  `docs/findings/`, plus an index of the other result documents.
- [docs/handovers/](handovers/): dated handovers with state and next steps. Read
  the newest to continue the work.
- [docs/design/](design/), [docs/plans/](plans/): designs and plans.
  [docs/archive/](archive/): superseded documents, kept for history.
  [packages/emulator/README.md](../packages/emulator/README.md): crate map and the coupled runner.

Record results in `docs/findings/`, not in handovers. Marks: **[V]** verified here,
**[D]** documented or read once, **[O]** open, **[C]** corrects an earlier claim.

## Limitations

- The DSP does not run in real time yet ([finding 18](findings/18-dsp-performance.md)).
- The browser DSP is slow; the browser host is parked on the branch `wip/browser-host`.
- The control socket is a Unix socket ([docs/design/emulator-control.md](design/emulator-control.md)).
- Some scenario PCM pins need the private ready inputs ([tests/scenarios/README.md](../tests/scenarios/README.md)).

Contributing: [CONTRIBUTING.md](../CONTRIBUTING.md).
