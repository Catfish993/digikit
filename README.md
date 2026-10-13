# Digitakt II / Digitone II firmware research

This repository holds an emulator and analysis tools for the Elektron Digitakt II
and Digitone II. The control CPU is a Freescale ColdFire MCF54415 (user interface,
sequencer, files, MIDI). The audio CPU is an Analog Devices ADSP-21569 SHARC+ whose
program is an ADI boot stream in section 7 of the firmware.

- A native emulator (Rust) runs the real firmware: ColdFire and SHARC+ together, with
  the front panel, the display and audio. It runs on the desktop and, in part, in the
  browser.
- A Python ColdFire reference emulator (Unicorn) was the oracle for the native one. It was removed;
  the last commit that has it is tagged `unicorn-reference-final`.
- Analysis tools (DSP and ColdFire databases, Ghidra tooling, tracers) and a container
  tool that rebuilds and patches a `.syx` file.

The goal is to understand the firmware well enough to add new machines and sounds, and
to test DSP changes without hardware.

**No Elektron firmware is in this repository.** The firmware is copyright Elektron. You
supply your own `.syx` file. `.gitignore` keeps the firmware, the extracted sections,
the snapshots and the databases (`out/`) out of git.

## Firmware

Use Digitakt II OS 1.16 and Digitone II OS 1.11. The older builds are kept because much
of the early analysis used them.

| device | version | file | sha256 |
| --- | --- | --- | --- |
| Digitakt II | 1.16 | `Digitakt_II_OS1.16.syx` | `278541e466edcd77d6b3e018a91fb90185932d3c7de224dd3e68294dddf3a9ec` |
| Digitone II | 1.11 | `Digitone_II_OS1.11.syx` | `2af43e65e3d8390b41c9f66222620f8cce027d73ed87db00c80b440f628472e0` |
| Digitakt II | 1.15C | `Digitakt_II_OS1.15C.syx` | `62d588456e47194bd56dfee9568fb9dd4521c4ff1e8b5427eb461355532e8c6c` |
| Digitone II | 1.10E | `Digitone_II_OS1.10E.syx` | `9881ee5ca4624be2b833e5eb94e294f2f8425371e6729781baccd1c256cac504` |

Put the files in the repository root; the tools look there first. `DIGI_EMU_SYX`
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
uv run python -m emu.extract Digitone_II_OS1.11.syx -o out/sections/dn2-1.11
uv run python -m emu.extract Digitakt_II_OS1.16.syx -o out/sections/dt2-1.16
```

Keep each firmware in its own directory (`out/sections/dt2-1.16`, ...): the analysis
tools find images by these names. A sections directory records the SHA-256 of its
`.syx` in `.source-sha256`, and the tools refuse to mix firmwares. Compare it with
`shasum -a 256 <the .syx>` before you trust a run.

## Ready states

The native emulator starts from a ready state: ColdFire booted to the main screen, DSP
running. Build it from the `.syx` alone:

```sh
mise run emu:ready -- dn2                     # Digitone II 1.11 (about 2 minutes)
mise run emu:ready -- dt2 [--card-image IMG]  # Digitakt II 1.16, optionally with a +Drive image
```

The files go to `snapshots/<device>-audio-ready-cold-*`. Git ignores them.

## Desktop emulator

```sh
mise run emu                      # build once and run (`mise run emu -- --build-only` builds only)
mise run emu -- --cf-only --samples DIR Digitakt_II_OS1.16.syx   # your .wav files on the DT2 +Drive
mise run emu -- --dsp-patch FILE  # overlay a DSP patch (DN2 audio)
mise run emu -- --cf-only         # ColdFire only: no DSP, no audio
```

With the DN2 ready inputs present, `mise run emu` plays DN2 audio. Without them it
starts the ColdFire-only emulator. The DSP needs about 1.6 CPU seconds per audio second
([finding 18](docs/findings/18-dsp-performance.md)), so real time is not reached yet.
`--samples` builds a +Drive image from a folder of `.wav` files and caches it by content.
Track N of the DT2 plays file N (sorted by name; more than 16 files wrap around).

**Choose samples (desktop app, Digitakt II).** With the DT2 running, the toolbar's "Choose
samples" button picks a folder. The app sends its top-level `.wav` files to the controller,
which builds the card in process, cold-boots the DT2 with it (about 12 s the first time at
the default fast tier, `build 1 s + boot 12 s` for three files), presses NO on the FILE SYSTEM
OK window and swaps the runtime; the toolbar shows "Building the card", "Booting the Digitakt
II with the card (n%)" and "Card ready". The card and its ready state are cached by a hash of
the firmware and the samples under `<run dir>/cards/<key>/` (default `out/native/run`), so the
same folder restores in well under a second. It needs the DT2 ready inputs (`mise run ready --
dt2` once: the DSP image and `cold-idle.dsp` come from there). The app remembers the last card
(`<run dir>/cards/last.json`) and mounts it again when a DT2 starts without `--card-image`, in
well under a second; the toolbar shows "Samples: NAME (N files)", or "Choose samples to load a
folder of WAVs" when nothing is cached. A DT2 launch without `SHARC_GEN_DIR` uses the generated
core its PGO profile was trained on (the launcher prints which core and why), so no environment
settings are needed. The browser shows the button
disabled ("desktop only for now"). Headless: the control socket's `card.load {dir}`,
`card.status`; `uv run python tools/card_check.py` checks that trigs 1-3 play a 220 Hz sine, an
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
uv run python tools/digi_client.py inject PATCHED.syx   # hot DSP injection
uv run python tools/dsp_patch.py syx BASE.syx tools/dsp_patches/dn2-1.11-master-half.patch OUT.syx
```

`tools/dsp_patch.py` edits DSP code or data by SHARC+ address and writes a patched
`.syx`. The protocol is in [docs/design/emulator-control.md](docs/design/emulator-control.md).

## Scenarios and tests

A scenario is a list of panel inputs (button presses, encoder turns) that the headless
runner plays against a ready state. It reports the PCM hash and level
([scenarios/README.md](scenarios/README.md)).

```sh
mise run scenarios            # every DN2 scenario; fails when a pinned PCM hash differs
mise run scenario trig1-taps --wav
mise run test:quick           # fast checks
mise run test                 # pytest, cargo test --workspace, web tests, all scenario pins
mise run test:slow            # slow runs and gates that need private fixtures
mise run lint                 # ruff, oxlint, cargo fmt on the clean crates
mise run check                # lint + test:quick, the pre-merge gate
```

Pins are valid only for the firmware and ready state they were recorded with.

## Performance and instrumentation

```sh
mise run perf                 # DSP CPU per audio second, tier split, per-function cost ranking
mise run perf:cov             # fast-tier coverage per configuration
```

Method and results: [finding 18](docs/findings/18-dsp-performance.md). Every run tool
writes `out/runs/<scenario-or-kind>/<UTC stamp>/` (report, audio, logs);
[docs/INSTRUMENTATION.md](docs/INSTRUMENTATION.md) lists the output and the switches.

## Analysis tools

For the DSP, `uv run python tools/sharcdb.py build out/sections/*/section_7_BLOB.bin`
builds `out/sharcdb/<image>.sqlite`. `tools/sharc.py` queries it: `sharc.load("dt2-1.16")`
gives `func`, `callers`, `callees`, `reach`, `last_def`, `refs`, `trace` and `sql`, and
`uv run python tools/sharc.py dt2-1.16 "SQL"` runs one query. For the ColdFire OS, `tools/ghidradump.py` exports a
Ghidra project, `tools/cfdb.py build` makes a database from the dump, and `tools/cf.py`
queries it. Decoding uses only public manuals ([docs/sharc/SOURCES.md](docs/sharc/SOURCES.md)).
All tools are indexed in [docs/TOOLS.md](docs/TOOLS.md).

## Patching

patchkit applies modules (new machines, parameters, master-bus effects) to your own stock `.syx` and writes a new
`.syx`. Modules hold our code only, never firmware bytes.

```sh
mise run toolchains    # once: pinned ColdFire and SHARC+ toolchains for building modules
mise run patch -- --firmware Digitakt_II_OS1.17.syx --modules fx/master-gain -o out/patch/mg.syx
mise run inject -- out/patch/mg.syx     # into a running emulator (mise run emu, or emu:serve)
mise run web                            # the gallery at /patch: patch in the browser, nothing is uploaded
```

Usage, the dev loop, tests, how it works and the safety rules: [docs/PATCHING.md](docs/PATCHING.md). Writing a
module: [modules/README.md](modules/README.md). Facts behind it: [findings 01](docs/findings/01-container-and-patching.md),
[02](docs/findings/02-machines-and-parameters.md) and [21](docs/findings/21-patchkit-core.md).

**Hardware warning**

- No patched image from this repository has been installed on a device. Only Em decides
  to do that.
- Never change section 2 (bootstrap), section 4 (updater) or section 8. patchkit refuses.
- Recovery through the bootstrap STARTUP menu (hold FUNC at power-on, SysEx over MIDI DIN)
  is not tested on hardware.
- Read `docs/findings/01-container-and-patching.md` before you install anything.

## Repository layout

```
dt2/             .syx container: decode, rebuild, decompress
emu/             firmware extraction and the shared helpers (config, symbols, SHARC capture)
native/          Rust workspace: ColdFire core, SHARC+ engine, coupled runner (native/README.md)
packages/web     browser panel (Astro + Solid); packages/desktop is the Tauri app
tools/           analysis, run and measurement tools
tests/           pytest suite (native tests live in the crates)
scenarios/       scripted panel inputs and pinned PCM hashes
devices/*.toml   firmware hashes and panel layout per device
docs/            findings, designs, handovers
```

## Documentation map

- [docs/FINDINGS.md](docs/FINDINGS.md): the results, one file per topic in
  `docs/findings/`, plus an index of the other result documents.
- [docs/handovers/](docs/handovers/): dated handovers with state and next steps. Read
  the newest to continue the work.
- [docs/design/](docs/design/), [docs/plans/](docs/plans/): designs and plans.
  [docs/archive/](docs/archive/): superseded documents, kept for history.
  [native/README.md](native/README.md): crate map and the coupled runner.

Record results in `docs/findings/`, not in handovers. Marks: **[V]** verified here,
**[D]** documented or read once, **[O]** open, **[C]** corrects an earlier claim.

## Licence

digikit ([github.com/m-dwyer/digikit](https://github.com/m-dwyer/digikit)) is licensed under
AGPL-3.0-or-later ([LICENSE](LICENSE)), with additional terms under section 7 of the AGPL
(attribution, origin and names) in [NOTICE](NOTICE). Keep NOTICE and the attribution
"digikit by Em D - https://github.com/m-dwyer/digikit" in copies and modified versions; a
user interface of a covered work shows it in its About, credits or footer area.

The licence covers the code here only; it gives no rights to Elektron's software or
firmware. digikit is not affiliated with or endorsed by Elektron. Digitakt, Digitone and
Syntakt are trademarks of Elektron Music Machines. Container format knowledge comes from
`mischa85/elektron-firmware-tool` (MIT); some architecture facts from
`lalzart/digitakt-ii-firmware-research-public`. The public SHARC+ assembler `js216/selache`
is used only as a read-only cross-check. `tools/ghidra/ColdfireEMAC` contains files from
Ghidra under the Apache License 2.0.
