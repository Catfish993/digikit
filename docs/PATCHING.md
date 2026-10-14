# Patching the Digitakt II and Digitone II firmware with patchkit

patchkit applies modules (new machines, parameters, master-bus effects) to a stock Elektron `.syx` and writes a new
`.syx` that the device's updater accepts. A module holds our own code and a manifest. It never holds firmware
bytes. The tool runs on the command line (`mise run patch`) and in the browser (the `/patch` page).

**No patched image from this repository has been flashed to a device.** Every result here comes from the emulator.
Read [Safety and hardware](#safety-and-hardware) before you flash anything.

New to modules? Start with [MODULE-AUTHORING.md](MODULE-AUTHORING.md): the short path from an empty module to a
patched firmware running in the emulator.

Supported firmware (a "port" exists for each): Digitakt II 1.16 and 1.17, Digitone II 1.11 and 1.12. Use your own
copy of the `.syx` files; the repository never contains them.

Other documents: [modules/README.md](../modules/README.md) (write a module),
[docs/plans/patchkit.md](plans/patchkit.md) (plan and state),
[findings 21](findings/21-patchkit-core.md) (core), [01](findings/01-container-and-patching.md) (container, recovery,
flash map, expansion region), [20](findings/20-patch-toolchains.md) (toolchains). The notes from before patchkit
(hand-written byte patches on 1.15C and 1.10E) are in [archive/PATCHING-pre-patchkit.md](archive/PATCHING-pre-patchkit.md).

## Quick usage

Run every command from the repository root.

### Prerequisites

```sh
mise install
mise run setup        # Python and web dependencies
mise run toolchains   # pinned m68k-elf gcc/binutils and the public SHARC+ assembler, into out/tools (about 15 min)
```

`mise run toolchains` is needed only to build module sources (`patch:build`, `patch:new`, `patch:dev`). Applying
modules that are already built needs no toolchain. Put your firmware files in the repository root
(`Digitakt_II_OS1.17.syx`, `Digitone_II_OS1.12.syx`, ...). `uv run python tools/pk_toolchains.py status` prints the
installed versions.

### Patch a firmware

```sh
mise run patch -- --firmware Digitakt_II_OS1.17.syx --modules fx/master-gain -o out/patch/mg.syx
```

This writes `out/patch/mg.syx` and `out/patch/mg.plan.json` (every write with its owner, diffable). Without `-o` the
name is `out/patch/<firmware>+<modules>.syx`. The command verifies the result before it reports success. Notes:

- `--modules` is a comma-separated list of module ids (their path under `modules/`). The command line adds the
  modules they `requires`, like the web gallery, and says so (`added by requires: core/machines, ...`). A module that
  is not in the library, a missing requirement and a `requires` cycle are refused.
- Modules apply in a fixed order (dependencies first, then by id), whatever order you write them in.
- The input must be a stock image. A patched image is refused.
- Everything under `out/` is derived from firmware and is never committed.

A machine needs its framework modules, which are added for you:

```sh
mise run patch -- --firmware Digitakt_II_OS1.17.syx --modules machines/pluck -o out/patch/pluck.syx
```

### Inspect, check, explain, verify

```sh
uv run python tools/patchkit.py list                                   # modules and ports
uv run python tools/patchkit.py inspect out/patch/mg.syx               # device, sha256, port, patch record, sections
uv run python tools/patchkit.py check --firmware Digitakt_II_OS1.17.syx           # which modules fit this firmware
uv run python tools/patchkit.py verify out/patch/mg.syx --base Digitakt_II_OS1.17.syx   # re-decode and check
mise run patch:explain -- --firmware Digitakt_II_OS1.17.syx --modules fx/master-gain
```

- `check` prints `ok` or `refused` plus the reason for every module (for example "built for dn2-1.11, dn2-1.12").
- `verify` re-decodes the image and checks the container (packet checksums, content checksum, HMAC trailer), the size
  limits and the patch record. With `--base` it also checks that sections 2, 4 and 8 equal the stock image.
- `explain` lists the allocation map (what went where, per region), each patched site with the bytes before and
  after, and the SHARC+ listing of DSP sites. Its output contains disassembly of firmware code: keep it out of
  commits.

### Run it in the emulator

```sh
mise run emu                  # desktop emulator, or:
mise run emu:serve -- ...     # headless daemon on the control socket (see below)
mise run inject -- out/patch/mg.syx
mise run inject -- out/patch/mg.syx --hot
```

`inject` loads the image into the running emulator through the control socket (`firmware.load`). Without an
argument it takes the newest image under `out/patch/`. The report says how the image started:

| `start_used` | Meaning | Time (DT2 1.17) |
|---|---|---|
| `hot-dsp` | Only the DSP section changed: DSP memory is patched while the firmware runs | about 1 s |
| `snapshot-cache` | MAIN OS changed and this MAIN image was booted before: the saved ColdFire state is restored | under 1 s |
| `cold` | MAIN OS changed: a coupled cold boot with DSP audio, then cached by the MAIN hash | about 15 s (DN2 about 6 s) |

`--hot` passes `--start snapshot`: it never cold boots and fails when no saved state fits.

Headless daemon with DSP audio (the flags `tools/digi_client.py` uses for its own daemons):

```sh
R=out/ready-pk-dt2-117
mise run emu:serve -- --auto-coupled --audio-profile-syx Digitakt_II_OS1.17.syx \
  --dsp-image $R/digi-audio-dn2-image.bin --dsp-state $R/digi-audio-m5.snap.dsp \
  --cf-snapshot $R/digi-audio-m5.snap --no-audio Digitakt_II_OS1.17.syx
mise run emu:ctl status       # from another shell; `mise run emu:ctl quit` stops the daemon
```

The ready inputs come from `uv run python tools/ready.py dt2 --syx Digitakt_II_OS1.17.syx --out out/ready-pk-dt2-117
--force` (about 2 minutes). The test harness looks for them in the table at the top of `tools/pk_harness.py`;
`DIGI_PK_READY_<DEV>_<VER>` (for example `DIGI_PK_READY_DT2_117`) overrides the directory. The harness refuses ready
inputs that are stale or belong to another `.syx`.

### Developer loop

```sh
mise run patch:dev -- --modules fx/master-gain [--device dt2|dn2] [--firmware 1.17]
mise run patch:dev -- --modules fx/master-gain --once      # one iteration, then exit
```

One daemon stays up on the stock firmware. Each time a file under the module's `src/` or its `module.toml` changes,
the loop builds the objects, patches from the stock image, loads the image into the daemon and runs the module's quick
tests. It prints one line per edit:

```
05:35:22 OK   build 0.0 s  patch 0.5 s  load 0.9 s (snapshot)  tests 1/1 2.2 s   edit->running 1.4 s  edit->tested 3.6 s
```

Measured on DT2 1.17: a DSP-only edit is running in about 3 s, a ColdFire edit in about 13 s (cold boot, cached by
MAIN hash). The daemon takes 1 to 2 minutes to start.

### Tests

```sh
mise run patch:test                                                    # the whole matrix (about 1 minute per cell)
mise run patch:test -- --cell "dt2-1.17:fx/master-gain,test/cf-demo"   # one cell; modules joined with a comma
mise run patch:test -- --cell "..." --update                           # record frame goldens for a new module set
uv run python tools/pk_harness.py list                                 # the cells of tests/patchkit_smoke/matrix.toml
cargo test -p patchkit                                                 # the patching library
uv run python -m pytest tests/test_patchkit.py tests/test_pk_prims.py  # codec, linker, primitives (about 2 minutes)
uv run python -m pytest tests/patchkit_smoke --slow                    # the matrix through pytest
```

A matrix cell is device x firmware x module set. For each cell the harness patches, loads the image into a daemon and
runs the smoke tour (boot, track and pattern change, audition, play and stop, machine menu, save and reload a project,
a seeded 40-input stress) plus the `tests/*.toml` files of the modules in the set. After every input it asserts no
fault, no DSP halt and no main-loop stall. Frames are compared by SHA-256 against
`tests/patchkit_smoke/goldens.json`, keyed by device, firmware and module set (hashes only). A module set with no
recorded goldens fails with "no golden"; record them with `--update`. Details: the docstring of `tools/pk_harness.py`
and [finding 17](findings/17-emulator-control.md). The proof tools `tools/pk_proof_*.py` (PCM and behaviour proofs per
module family) are slower and are run before a merge, not in the loop.

### Create a module

```sh
mise run patch:new -- fx/my-effect            # ColdFire and SHARC+ stubs
mise run patch:new -- fx/my-effect --dsp      # SHARC+ master-bus hook that passes audio through
mise run patch:new -- test/my-demo --cf       # ColdFire only: code, data and bss in cf.expansion
mise run patch:build                          # rebuild all objects (`-- modules/fx/my-effect` for one, `-- --check` only checks)
```

The command writes `modules/<category>/<name>/` (manifest, source stubs, `tests/smoke.toml`, `gallery/README.md`) and
builds it, so the template is known to compile. [modules/README.md](../modules/README.md) is the authoring guide and
the manifest reference.

### Resolve a port for a firmware

```sh
mise run port:resolve -- Digitakt_II_OS1.17.syx   # one firmware -> devices/ports/dt2-1.17.toml
mise run port:resolve                             # the four supported firmwares
```

A port records the anchors that modules need (address, length, SHA-256 of the bytes there) and the free regions of
one stock image. It is generated from the `[[anchor_rule]]` entries of the modules and is deterministic: resolving a
supported firmware again leaves `git status` clean. A rule that matches nothing, or more than one place, is an error;
that is how a new firmware shows which rules need work. A firmware without a port file has no compatible modules.

### Web gallery

```sh
mise run web          # builds the browser core and patchkit.wasm, generates the module index, serves http://localhost:4321
```

Open `/patch`. Drop your own `.syx` on the page (it is read in the browser and never uploaded), pick modules, patch,
check the result and download it. Required modules are added automatically; conflicts and incompatible firmware are
explained with patchkit's own error text. "Boot in emulator" starts the patched image in the browser core (screen and
controls, no DSP audio yet). The page reads `public/modules/index.json` and `bundle.json`, which
`packages/web/scripts/gen-modules.mjs` generates from the committed `modules/` and `devices/ports/` (never from
`out/`). `patchkit.wasm` produces the same bytes as the command line (`packages/web/tests/patchkit.test.mjs`). The
first `mise run web` in a fresh tree compiles the browser cores and takes several minutes.

## How it works

### Layers

| Layer | Holds | Examples |
|---|---|---|
| Port | Per-firmware facts: named anchors with SHA-256 of the expected bytes, free regions, container limits. Generated, keyed by the `.syx` SHA-256. | `devices/ports/dt2-1.17.toml` |
| Framework module | Hooks firmware sites once and exports a registry that other modules add entries to. Ordinary assembly plus a manifest. | `core/machines`, `core/params`, `core/dsp-voice`, `core/expansion` |
| Content module | Own code and declarative registry entries. No addresses. | `fx/master-gain`, `machines/pluck` |

patchkit itself (`native/patchkit`, Rust) has no framework-specific code. It provides generic primitives, and the
frameworks are modules built on them. One implementation serves the command line and `patchkit.wasm`.

### Ports and anchors

Modules never contain addresses. A firmware site is a named **anchor** (`dsp.master.q31_call`,
`cf.machines.dispatch`). The module that needs it declares how to find it with an `[[anchor_rule]]`, and
`port:resolve` turns the rules into the lockfile. Rule kinds:

- SHARC+: `sharc_func` (a function found by its relocation-insensitive hash, plus an offset), `sharc_literal`,
  `sharc_mem_addr`, `sharc_table`.
- ColdFire: `cf_func` (a function found by a hash that masks address operands, so one rule serves relinked builds,
  plus an offset), `cf_ptr`, `cf_words` (a masked word signature), `cf_params_t1`, `cf_params_refs` and
  `cf_params_bounds` (many anchors from one rule), `cf_reset_init_calls`, `cf_reset_helper`.

The lockfile stores, per anchor, the address, the length and the SHA-256 of the bytes, never the bytes. At patch time
patchkit checks every anchor a module uses against the user's image; a mismatch refuses the patch. This is why one
module works on 1.16 and 1.17 although the 1.17 ColdFire image is relinked and every address moves. The full syntax of
the rules is the docstring of `tools/port_resolve.py`.

### Module layout and manifest

```
modules/<category>/<name>/
  module.toml          manifest
  src/cf/*.c|*.s       ColdFire sources (m68k-elf gcc, -mcpu=54418, freestanding)
  src/dsp/*.s          SHARC+ sources (algebraic syntax)
  build/*.dko.json     built objects, committed; `patch:build --check` keeps them current
  tests/*.toml         harness tests
  gallery/README.md    text of the gallery card
```

The manifest has `[module]`, `[targets]`, `[[object]]`, `[[op]]`, `[[claim]]`, `[[anchor_rule]]`, `[[section]]`,
`[[registry]]` (with `[[registry.field]]`) and `[[entry]]`. Every key is listed in
[modules/README.md](../modules/README.md).

### Primitives

- **Objects and placement.** An `[[object]]` is a relocatable object built from our sources and placed in a region
  (`dsp.l2`, `cf.expansion`, `cf.flash_cave`). patchkit assigns addresses first-fit and applies the relocations.
- **Writes.** `write` replaces bytes at an anchor plus an offset. It carries `expect_sha256` of the bytes it replaces
  (never the bytes), or a typed `value` with a width of 1, 2 or 4 bytes and a range check. `anchors = "PREFIX.*"`
  repeats one write over many anchors.
- **Hooks.** `hook` overwrites a site with a jump (or, on ColdFire, a call) to a routine of the module. With
  `displaced = "run"` patchkit copies the displaced instructions to a tail (`hook.<anchor>.tail`; PC-relative forms
  are re-encoded or refused) so that the routine can run them and return (`hook.<anchor>.return`). `retarget`
  rewrites the target of an existing call or jump.
- **Sections copied from the image.** `[[section]]` copies bytes of the user's image at patch time (a stock table),
  may append one row per registry entry, and places the result. This is how a framework extends a firmware table
  without storing it.
- **Registries and entries.** A framework declares a `[[registry]]` with typed fields. A content module adds
  `[[entry]]` rows. patchkit validates the fields, assigns indices in canonical module order, builds the row table and
  strings in the registry's region, and defines the symbols `<reg>.first`, `.count`, `.rows`, `.row_size` and
  `<reg>.<id>.index`. Every entry claims `<reg>.<id>`.
- **Expressions.** Wherever a number is accepted: integers, symbols, `+ - * / & | << >>`, parentheses, and
  `image.u8|u16|u32(anchor + offset)`, a checked read from the user's image (for example a stock bound).
- **Device filters.** `devices = ["dt2"]` on objects, sections, ops, entries, claims, rules and registries lets one
  module hold Digitakt II and Digitone II sources side by side.

### Regions

| Region | Where | Size | Use |
|---|---|---|---|
| `cf.flash_cave` | zero padding at the end of the MAIN OS image | about 2 KB | the 54-byte expansion loader only |
| `cf.expansion` | RAM above the DMA arena: DT2 `0x47e8b000-0x47fe0000`, DN2 1.12 `0x4671a000-0x47fe0000` | 1.33 MiB (DT2), 24.8 MiB (DN2) | module ColdFire code, data, bss, registry tables |
| `dsp.l2` | free SHARC+ L2 behind the loaded stream | 922,756 B (DT2), 491,972 B (DN2) | module DSP code and data |

**Expansion segment and loader.** ColdFire code does not fit in the image, so patchkit appends the module payloads
to the MAIN OS image as a chain `[len][dest][bss][payload] ... [0xffffffff]` and adds the built-in `core/expansion`
module whenever a module places something in `cf.expansion`. Its loader (in the flash cave) replaces the reset code's
two helper calls (copy, clear). It copies each payload to its destination, zeroes the module's bss, then calls the two
original helpers. Module code runs in place before the firmware's own initialisation, so it must not assume a libc.
The region is above the bss end, so the reset clear does not touch it, and a canary run found no firmware access
([finding 01](findings/01-container-and-patching.md), "Expansion region").

**DSP boot-stream append.** SHARC+ code is added as new blocks at the end of the boot stream in section 7, placed in
free L2. The ColdFire boot routine sends the whole decompressed section, so no ColdFire change is needed. Limits:
stored size at most `0x100000`, decompressed size at most `0xfffff`; usable appended payload 727,540 B (DT2) and
211,600 B (DN2). patchkit checks every output stream against the boot-ROM rules that the public manual states
(`native/patchkit/src/bootrom.rs`, [finding 06](findings/06-sharc-engine-and-startup.md), "Boot ROM conformance").

### Linking and `.dko` objects

`tools/pk_build.py` compiles a module's sources with the pinned toolchain and writes a `.dko.json` object in
canonical JSON: sections as hex, symbols, relocations with explicit addends (`cf.32`, `cf.pc32`, `cf.pc16`,
`cf.pc8`, `sharc.abs24`, `sharc.abs32`), the source hash and the toolchain version. Our own decoders re-decode every
instruction at build time, so a toolchain encoding error cannot reach an image. At patch time patchkit links the
objects. Undefined symbols resolve to anchors (`[object.externs]`), to symbols of other modules, or to symbols that
patchkit defines (registries, sections, hook tails). Objects are committed, so patching and the browser need no
toolchain.

### Conflicts, claims and order

- A `[[claim]]` names a scarce resource (`dsp.master.hook`, `cf.machines.sites`). Two modules that claim the same one
  are refused. Registry entries claim `<registry>.<id>` automatically.
- `requires` must be selected; `conflicts` must not be. `ports = [...]` in `[module]` pins a module to firmware.
- patchkit refuses overlapping byte ranges after linking, a region over capacity and an anchor hash mismatch.
- Modules apply in canonical order: dependencies first (topological), then by id. The same module set gives the same
  `.syx` bytes whatever order you list it in.

### The patch record

patchkit stores a `DKPR` record in the image: format version, the SHA-256 of the stock `.syx` and the list of
`id@version`. It lives in `cf.expansion` when the patch uses it, otherwise in an appended DSP block. `inspect` and
`verify` read it, and a patched image is never patched again. Nothing on the device reads it.

### Codec and safety rules

- The codec reproduces all four stock images byte for byte (framing, 8-in-7 coding, packet checksums, content
  checksum, HMAC trailer). Sections that a patch does not change keep their stored bytes. Changed sections are packed
  with an aPLib packer that produces the same bytes as `dt2/aplibpack.py` and runs about 25 times faster.
- **Sections 2 (bootstrap), 4 (updater) and 8 never change.** patchkit has no override. The bootstrap is what lets you
  recover from a bad MAIN OS.
- The container is at most `0x300000` (3 MiB). The OS container is written to flash at `0x80000`; the record sectors
  (tagged fields, serial number) start at `0x380000`, and the bootstrap's write loop has no size limit of its own.
  Stock images are about 1.5 MB.
- patchkit re-decodes and verifies every output before it reports success: checksums, HMAC, size limits, the patch
  record, and (with a base) that sections 2, 4 and 8 equal the stock image.
- Never flash the output of `dt2.build.rebuild`: it stores sections without compression and exceeds the cap
  ([finding 01](findings/01-container-and-patching.md)).

### What is never stored in the repository

Firmware (`*.syx`), sections extracted from it, snapshots, ready states, card images, patched images, `plan.json` and
`explain` output of a real patch, databases derived from firmware, and users' sample audio. Ports hold addresses and
SHA-256 values. Modules hold our code, our objects and SHA-256 of the bytes they replace. Frame goldens are SHA-256
only. Modules that need firmware words (oracles) are generated under `out/modules/` and read through `--modules-dir`
or `PATCHKIT_MODULES`. The web page builds its module data from committed files only, and the build guard rejects
`.syx`, `.snap` and `.mstate` files.

## Safety and hardware

### What the emulator has shown

The bootstrap (section 2) runs on the emulated ColdFire core. It tests [FUNC] before MAIN OS is touched, shows the
STARTUP menu over a MAIN OS that faults at reset, accepts patchkit images, rejects corrupt ones, and does not rewrite
itself for a 1.16 or 1.17 image (finding 01, spike 0.2). Patched images boot to the main screen with DSP audio
(DT2 1.16 and 1.17, DN2 1.12), the smoke matrix passes, and the module proofs measure the sound. None of this is a
hardware result.

### Recovery

- Hold **[FUNC]** while powering on. The STARTUP menu comes up from the bootstrap, which patchkit never changes.
- In the STARTUP menu, **[TRIG 4]** selects OS UPGRADE. It receives over **MIDI DIN only**; USB MIDI is not available
  there. Have a MIDI DIN interface ready.
- The bootstrap checks only the content checksum and the HMAC. On failure it prints UPGRADE ABORTED and writes nothing.
  That the menu comes up with a broken MAIN OS is shown in the emulator, not on hardware.
- **Keep the stock `.syx` of the version on the device.** Recovery is installing it again.
- Do not save over a stock project while testing, and back up the card. A project saved with an added machine loads on
  stock firmware with that track as SAMPLE.

### Pre-flash review

The full table of 14 risks, with evidence and mitigation, is in [docs/plans/patchkit.md](plans/patchkit.md)
("Pre-flash review"). The ones that matter most:

1. **The real SHARC boot ROM may reject a patched DSP stream** (it could apply a rule the manual does not state). A
   rejected stream is a boot hang: the ColdFire waits for the DSP with no timeout. Flash in this order: the harmless
   image that changes nothing (`tools/patched_image.py`), then a DSP change without an appended block, then an
   appended block. Keep the stock `.syx` and the recovery steps at hand.
2. **Cycle cost.** The emulator counts instructions, silicon counts cycles. Keep a module small (at most a quarter of
   the idle instructions per block) and listen for clicks at the first run.
3. **Digitone II patches cannot be tested on hardware** (no device available). They are emulator-tested only.
4. **Level.** A gain or drive module can raise the output. Start at the neutral setting with the monitor volume low.
5. The MAIN OS image validator and the real flash writer were not run in the emulator. The bootstrap's own checks were.

### Status

No patched image has been flashed to a device. Whether and when to flash is the maintainer's decision, every time.
The device on the desk is a Digitakt II on 1.16, so a first hardware step would use the DT2 1.16 port.
