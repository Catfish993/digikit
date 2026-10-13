# patchkit: firmware modules for Digitakt II and Digitone II

Status: design agreed 2026-10-13, not started. Branch `work/patchkit`.

## Goal

Patch a stock Elektron `.syx` with composable modules (new machines, parameters, menu items, FX) that target the
ColdFire MAIN OS, the SHARC+ DSP, or both, for every device and firmware the module supports. Two front ends share
one implementation:

- **CLI** (mise tasks): create a module, build it, patch a firmware, inject it into a running emulator, run the
  test matrix. The loop from edit to a running patched emulator should take seconds.
- **Web gallery** (GitHub Pages): the user uploads their own firmware, picks compatible modules, patches in the
  browser, sees the patched firmware boot, and downloads the patched `.syx`. Firmware never leaves the browser and
  the site never hosts firmware.

## Facts this design rests on

- Container: checksum, HMAC trailer and per-packet checksums are reproduced; `dt2.build.rebuild` repacks and
  `roundtrip.check_authenticity` verifies (finding 01). No patched image has been flashed to a device.
- 1.17 (DT2) and 1.12 (DN2) change only MAIN OS (section 3) and META (section 5). Sections 2 (bootstrap), 4
  (updater), 7 (SHARC blob) and 8 are byte-identical to 1.16 / 1.11. DT2 1.17 MAIN is the same size as 1.16 but
  relinked (2.3 M differing bytes in 9,331 runs), so every ColdFire address moves. Modules must not hold addresses.
- The bootstrap upgrades itself only when the incoming bootstrap version is strictly greater (finding 01). The
  device runs 1.16, whose bootstrap (0x0201) equals the one in 1.17, so installing a patched 1.17 does not rewrite
  the bootstrap.
- Recovery: hold [FUNC] at power-on for the STARTUP menu (bootstrap-owned), [TRIG 4] for OS UPGRADE over **MIDI
  DIN only** (USB MIDI is not available there). The bootstrap checks only the content checksum and HMAC; on
  failure it prints UPGRADE ABORTED and writes nothing [V]. That the menu comes up with a broken MAIN OS is [O].
- The bootstrap's receive and flash-write path has no size limit. NOR flash is 16 MiB; the container is written at
  flash offset 0x80000 and record sectors (tagged fields, serial number) start at 0x380000, so the container must
  stay at or under 0x300000 bytes (finding 01, spike 0.2). DDR is 128 MiB [C] (finding 08, spike 0.3).
- ColdFire space today: `flash_cave` only (DT2 1.16 2,108 B, DN2 1.11 2,136 B). `cave_b` is cleared at reset.
  `tools/cavefind.py` is lost (only its .pyc remains).
- SHARC space: L2 past the loader looks free (about 900 KB DT2, 490 KB DN2), weak evidence. `dsp_patch.py` can only
  overwrite bytes the boot stream already stores.
- Emulator: `firmware.load` (inject) applies a DSP-only change hot in 0.13 s; a MAIN change cold-boots the
  ColdFire in about 4.4 s, cached per MAIN sha, **without DSP audio** (finding 17, [O]). No `fault`/`halt` events
  are emitted and there is no hang watchdog.
- No new DSP code has run and no added machine has made sound. The per-voice hook into the SHARC render path is
  open research (type 7 selector slot collides; render choice is binary).
- Browser: no cold boot from a `.syx`, no in-page extraction, generic core at about 0.17x real time, COI service
  worker not loaded.

## Architecture

### Layers

| Layer | Holds | Example |
|---|---|---|
| Port | Per-firmware facts: named anchors with `expect` hashes, allocatable regions. Generated, keyed by `.syx` sha256. | `devices/ports/dt2-1.17.toml` |
| Framework module | Hooks firmware sites once, exports a registry API, carries per-device differences. | `core.expansion`, `core.machines`, `core.master-fx`, `core.dsp-voice` |
| Content module | Own code plus declarative registry entries. No addresses. | `fx/master-drive`, `machines/granular` |

A module is compatible with a firmware when its framework dependencies resolve on that firmware's port and its
resources fit. `targets` in the manifest records only test status (`stable`, `experimental`, `untested`).

### Ports

- Live in `devices/ports/<short>-<version>.toml`, next to the device files, referenced by firmware sha256.
- Built by `mise run port:resolve -- FILE.syx` from resolution rules (masked signatures, xref shapes, SHARC
  `reloc_hash`), the same approach as `emu/symbols.py` and `tools/sharc_symbols.py`. Never hand-typed.
- Each anchor stores address, length and the **sha256 of the expected bytes**, not the bytes, so no firmware bytes
  are committed.
- Regions: `flash_cave` (loader only), the ColdFire expansion region, free SHARC L2, each with capacity and
  evidence mark.

### Module layout

```
modules/<category>/<id>/
  module.toml      id, version, category, status, summary, requires/conflicts, resources claimed,
                   registry entries (machines, params, menu items), hooks, per-target test status
  src/cf/*.c|*.s   ColdFire: gcc/binutils m68k-elf, -mcpu=54418, freestanding, -Os, relocatable
  src/dsp/*.s      SHARC+: selache selas; parcel order and relocations handled by patchkit
  tests/*.toml     module scenarios for the harness
  gallery/         card image, long description
  build/*.dko      compiled relocatable objects (committed; a check keeps them current with src/)
```

Source language: C for ColdFire logic, assembly for SHARC kernels (its algebraic syntax reads well), both
commented. Hook stubs and trampolines are generated by patchkit from the manifest, so authors rarely write raw
hook assembly. There is no custom cross-ISA IL: no mature compiler targets both ColdFire EMAC and SHARC+, and
little code is shared between the two CPUs. The readable layer is the manifest plus `patch:explain`.

`.dko`: JSON with sections as hex, symbols, relocations, source hash, toolchain version. Builds are pinned and
reproducible.

### patchkit (Rust, `native/patchkit`)

One implementation, built as a CLI binary and as `patchkit.wasm` for the gallery. A test asserts the CLI and wasm
produce byte-identical output. It does not depend on the generated SHARC core, so it builds in seconds, and
patching never rebuilds the emulator (patched images boot through `boot_for_base`).

- `.syx` codec: reuse `native/loader`; untouched sections keep their original packed bytes verbatim; aPLib packer
  for changed sections; HMAC and per-device packet constant.
- Safety rules, not overridable: refuse any change to sections 2, 4 and 8; cap the packed container size at the
  stock size plus a budget set from the flash map (Phase 0.3); re-decode and verify every output.
- Linker: allocate from port regions first-fit after earlier claims; ColdFire relocations (R_68K_32, PC16, PC32);
  SHARC relocations; symbols from ports, frameworks and the module itself.
- Operations: `write` (with expect), `hook` (trampoline with displaced-instruction relocation, using the native
  ColdFire decoder), `relocate_table`, `repoint_imm`, `dsp_append` (new loader blocks), registry entries.
- Conflicts: refuse overlapping byte ranges after linking; refuse colliding resource claims (machine type ids,
  parameter ids, mirror slots, frame words, DSP selector slots, menu positions); requires/conflicts and framework
  versions. Modules apply in canonical order (dependency topology, then id).
- Patch record: base sha plus module@version list, stored in the image. Patching an already patched image is
  refused; always patch from stock.
- Outputs: patched `.syx`, `plan.json` (every write with provenance, diffable), and `explain` (before/after
  disassembly of each site, allocation map, registry entries).
- `tools/machinepatch.py` / `machinebuild.py` become the reference: their XSLICE output and the DN2 master-half DSP
  patch must be reproduced byte-identically before the Python versions are retired.

### Space

- ColdFire expansion segment: append module code and data to MAIN OS; a 34-byte loader in `flash_cave`, called
  from the reset path at 0x4000053e (before the .data copy and RAM clear, before caches are on), copies
  `[len][dest][payload]` from the image end into the region above the DMA arena: DT2 0x47e8b000-0x47fe0000
  (1.33 MiB), DN2 about 24.8 MiB. Canary-checked on all four images (finding 01, spike 0.3). The loader must zero
  module bss itself.
- SHARC: new loader blocks in free L2, verified by canary.
- eMMC/+Drive: not used. The file system starts late in boot, the file can be missing or deleted, and the browser
  flow would need a card file. Revisit only if flash or memory capacity binds.

## Developer loop

| Task | Does |
|---|---|
| `mise run patch:new -- category/name` | Module from template |
| `mise run patch:build [-- MODULE]` | Compile `src/` to `build/*.dko` |
| `mise run patch -- --firmware X.syx --modules a,b [-o OUT]` | Link, apply, verify; writes `out/patch/` |
| `mise run patch:explain -- ...` | Human-readable listing of a patch |
| `mise run inject [-- OUT.syx] [--hot]` | Load into the running emulator (`emu:ctl inject`) |
| `mise run patch:dev -- --modules a,b` | Watch, rebuild, patch, inject |
| `mise run patch:test [-- --matrix]` | Harness over the control socket |
| `mise run port:resolve -- X.syx` | Generate or check a port file |
| `mise run fw:onboard -- X.syx` | Extract, databases, names, port for a new firmware |

Inject modes: DSP-only changes hot (exists); ColdFire changes through a coupled cold boot to ready cached by
(MAIN sha, DSP sha) (Phase 0.6); `--hot` for code-only ColdFire edits that a module marks hot-safe.

Harness (pytest over the control socket):
- Smoke tour for every module set: boot to the readiness contract, track and pattern change, play/stop, machine
  select, save and reload a project, seeded random-input stress.
- Module tests from `tests/*.toml`.
- Assertions: no `diagnostics.fault`, no DSP `halted`, main-loop heartbeat advancing, framebuffer goldens (exact
  hashes), PCM oracles where relevant, DSP headroom from idle-loop share within budget.
- Matrix: device x firmware x module set.

## Web gallery

Upload, identify (device-profiles), list `modules/index.json` (generated at build time from manifests and
`.dko`), compatibility and conflicts from `patchkit.wasm` in a worker, patch and verify, boot in the emulator,
download. Routes: emulator and `/patch`. Dev-only pickers move behind a dev flag. Until the browser DSP is fast
enough, audio is an offline render.

## Phase 0 results (wave 1, 2026-10-13)

- 0.1: DT2 1.17 and DN2 1.12 boot to ready; device profiles load at run time; `tools/cfmatch.py` matches 97.5-98%
  of functions in about 8 s (99.8-99.9% precision against Version Tracking). Open: 36 of 135 DT2 FX oracles fail
  on 1.17 (encoder read-backs differ); DN2 1.12 keeps 3 of 4 PCM pins.
- 0.2: the real bootstrap runs on the ColdFire core (`native/bootstrap`): [FUNC] is tested before MAIN OS is
  touched, the STARTUP menu comes up over a faulting MAIN OS, the bootstrap's checksum/HMAC accepts rebuilt images
  and rejects corrupt ones, recovery to stock works, and a 1.16/1.17 image does not rewrite the bootstrap [V].
  `tools/patched_image.py` builds the harmless stock-size test image. Open: MAIN OS validator (0x400d9e4c) not run.
- 0.3: RAM map, expansion region and loader hook as above; `tools/regions.py` replaces cavefind; `dt2/aplibpack.py`
  passes the bootstrap's own depacker on all 16 sections; `rebuild` keeps untouched sections verbatim and has a
  `max_container` guard.

- 0.7 (master hook, lane fastweb): the Q31 converter's single CALL (DT2 `0x1c7734`, DN2 `0x1c9fd2`) retargeted to a
  routine in free L2 scales the final master block; gain 1.0 is bit-identical, gain 0.5 is -6.0206 dB on both devices
  (finding 06). The CFADE word (`+0xde`) is read by the DSP only as a flag with no effect on a Sample-machine track,
  so a hook can use it as a parameter channel ([C]: the earlier "discarded" reading missed a flag). Open: the
  UI-knob leg (needs 0.6), DN2's parameter word, the flag's reader; voice hook only proposed.

## Phase 0 results (wave 2, 2026-10-13)

- 0.4: `Stream.append_block` and an `APPEND` patch op (Python and Rust); the ColdFire boot routine sends the whole
  depacked section 7 (limits: stored <= 0x100000, depacked <= 0xfffff). Usable appended payload: 727,540 B (DT2),
  211,600 B (DN2), limited by the stream, not by L2. Free L2 by canary over the scenario suites: 922,756 B (DT2,
  keep 0x200fa000-0x200fe000 out), 491,972 B (DN2). Open: the real SHARC boot ROM with an extra block (hardware);
  L1 PM room.
- 0.5: pinned binutils 2.47 + GCC 16.2.0 for `-mcpu=54418` and selache 2b26d3b7 under out/tools
  (`mise run toolchains`); `tools/pk_link.py` (JSON object prototype, R_68K_32/PC32/PC16/PC8); call harness on the
  CF core (`Emulator::call_function`, `tools/pk_cfcall.py`); `tools/pk_sharc.py` assembles, links and re-decodes
  SHARC code and runs it on the core. selcc: glue only. Open: libgcc helpers for 54418 (float, 64-bit).
- 0.6: `firmware.load` of a patched MAIN cold-boots coupled with DSP audio: DN2 about 6-7 s, DT2 about 13 s
  uncached, under 0.5 s cached. The ColdFire expansion canary also passes with the DSP running.
- 0.7: master hook = retarget the single CALL into the Q31 converter (DT2 `PM 1c7734`, DN2 `PM 1c9fd2`); a
  7-instruction routine in L2 gives exactly -6.02 dB, bit-identical at gain 1.0, on both devices. The +0xde
  (CFADE) word is not discarded [C]: it sets a flag; poking it does not change Sample-machine PCM. Headroom: idle
  share about 50% (DN2) and 68% (DT2). Per-voice hook proposal at 0x1c6b00, about 40%. Open: a UI knob that
  reaches a free frame word; DN2's parameter word.
- Hardware smoke test (0.2): ready (`tools/patched_image.py`), waiting for Em's decision.

## Phases

Estimates are agent time. Phase 0 spikes run in parallel lanes (`tools/lane.sh`); each ends with a go/no-go.

### Phase 0: de-risk

- [x] **0.1 Onboard 1.17 and 1.12** (about 1 day, high). Device profile entries (make `devices/*.toml` loadable at
      run time, compiled-in fallback); emulator boots both to ready; sharcdb reuse (blobs identical); Ghidra import
      with seeds and RTTI; cfdb; in-house ColdFire function matcher (address-masked hash plus call-graph
      propagation) to port 1.16/1.11 names; ready snapshots; AOT cores unchanged (same DSP image).
- [x] **0.2 Bricking and recovery** (about 2 days, medium-high). Run the bootstrap (section 2) in the emulator:
      [FUNC] held reaches the STARTUP menu with a MAIN OS that faults at reset; the bootstrap's own checksum and HMAC
      code accepts a patchkit image (DIN MIDI receive or a direct call of its verifier); confirm a patched 1.17 does
      not trigger a bootstrap upgrade on a 1.16 device. Then Em decides on a hardware test: a harmless patch (one
      string), flashed, then recovery to stock through the STARTUP menu. Prerequisite: a MIDI DIN interface.
- [x] **0.3 Space** (about 2 days, medium). Resolve the DDR map against the reset clear range; choose and canary
      the ColdFire expansion region through long runs; aPLib packer (round trip through our depacker and the
      firmware's); flash layout past the OS container, and the size cap patchkit enforces; rewrite cavefind as a
      port region resolver.
- [x] **0.4 SHARC append** (about 1 day, medium). Append a loader block into free L2; confirm the ColdFire boot
      routine sends the whole section; canary through the scenario suite.
- [x] **0.5 Toolchains** (about 1-2 days, high for ColdFire, medium for SHARC). Assemble or compile, link and run
      one function on each emulated core as a unit test. Every assembled SHARC word is re-decoded by our decoder.
      Pin selache (fetched by a mise task into `out/tools`, not vendored) and binutils/gcc versions.
- [x] **0.6 Coupled cold boot** (about 2 days, medium-high). Patched MAIN with DSP audio, in-process, cached by
      (MAIN sha, DSP sha). Target under 30 s uncached. Also the base for the browser cold boot.
- [x] **0.7 DSP hook point** (3-10 days; master hook about 70%, voice path about 40%). Master-chain hook first
      (`0x1c207b`), then a per-type voice render path. Gate for Phase 4.2 onward.

### Phase 1: patchkit core (about 3 days, high)

- [ ] Crate, CLI, wasm target; `.syx` codec with verbatim untouched sections and safety rules.
- [ ] Port format and resolver; `devices/ports/` for dt2-1.16, dt2-1.17, dn2-1.11, dn2-1.12.
- [ ] `.dko` format, linker, operations, trampoline generation.
- [ ] Conflicts, canonical order, patch record, `plan.json`, `explain`.
- [ ] Oracles: DN2 master-half and XSLICE byte-identical; CLI and wasm byte-identical.

### Phase 2: developer loop and harness (about 3 days, high)

- [ ] mise tasks above; module template; agent skill `.claude/skills/digikit-module/`.
- [ ] Emulator: emit `fault` and `halt` events; main-loop heartbeat; check whether the MCF5441x watchdog is
      modelled.
- [ ] Harness: smoke tour, goldens, stress, matrix; `patch:dev` watch loop.

### Phase 3: frameworks

- [ ] `core.expansion` (loader, region) (about 1 day).
- [ ] `core.machines` DT2 (about 2 days): generalises machinepatch's nine parts with room for N machines.
- [ ] `core.master-fx` (with 0.7 master hook).
- [ ] `core.machines` DN2 (3-5 days): parameter-page layer.
- [ ] Persistence check: project saved on a patched image, loaded on stock (emulator).

### Phase 4: proof-of-concept modules

- [ ] 4.1 `fx/master-drive` (or compressor), DT2 and DN2: a new parameter carried in a frame word the DSP
      discards today, DSP code at the master hook. First module that changes both CPUs.
- [ ] 4.2 Simple voice (sine or noise) on DT2 through `core.dsp-voice`.
- [ ] 4.3 `machines/granular` DT2 (grains from sample memory), 3-5 days after 4.2.
- [ ] 4.4 `machines/granular` DN2. **Open decision:** grain source (track oscillator or wavetable, audio input, or
      a resample buffer).

### Phase 5: web (about 4-5 days, can start after Phase 2)

- [ ] `patchkit.wasm` worker; `modules/index.json` build step.
- [ ] Browser cold boot from `.syx` and in-page extraction; load the COI service worker.
- [ ] `/patch` gallery (categories, status and compatibility badges, dependency and conflict explanations),
      patch, verify, boot, download.
- [ ] UI cleanup: dev-only controls behind a flag, device follows the uploaded firmware.

### Phase 6: agent tooling (about 2-3 days, parallel with Phases 1-3)

- [ ] `tools/fw.py`: compact query CLI with `batch` mode and cached graphs; skill file.
- [ ] Atlas `out/atlas/<image>.sqlite`: machines, parameters, FX, page views, strings, sequencer structures; FTS5
      over strings and decompiled C; glossary from the manuals in `out/refs/`. Extractor rules committed, data
      derived.

### Phase 7: documentation

- [ ] Rewrite `docs/PATCHING.md`: quick usage first (create, build, patch, inject, test, flash and recover), then
      how it works (ports, frameworks, linker, regions, conflicts, safety rules).
- [ ] `modules/README.md`: authoring guide and manifest reference.
- [ ] Findings for every [V]/[D] result from Phase 0.

### Later

- Cross-porting between devices (for example DN2 FM Drum onto DT2): a module section sourced from the user's second
  firmware at patch time, so no vendor bytes are stored. Research only; the `.dko` format leaves room for it.

## Rules

- Never change sections 2, 4 or 8. patchkit enforces this with no override.
- Flashing hardware is Em's decision, every time.
- No firmware bytes in the repository: ports store hashes, modules store our own code.
- Patched images show a version marker so issues are not reported to Elektron.
- No user-provided firmware is ever uploaded or hosted by the site.

## Risk register

| Risk | Mitigation |
|---|---|
| Device rejects image or fails to boot | 0.2 emulated bootstrap acceptance and STARTUP-menu recovery; harmless first hardware test; DIN MIDI interface ready |
| Flash overwrite past the OS container | 0.3 flash layout; aPLib packer; packed size cap |
| Expansion region in use | 0.3 memory map, canary over long runs |
| Free SHARC L2 not free | 0.4 canary over scenarios |
| No DSP voice hook | 0.7 gate; master hook first; Phase 4 order |
| Toolchain encoding bugs | Re-decode every SHARC word; unit-run functions on the emulated cores |
| Projects saved on patched firmware break stock firmware | Phase 3 persistence check; machines reuse the 8 engine slots |
| New parameters not storable or p-lockable | Prefer the 8 positional engine slots and discarded frame words in v1 |
| DSP overrun on hardware | Idle-loop headroom budget in the harness |
| New Elektron OS | Signature-based ports; `fw:onboard`; 1.17 is the first test |
