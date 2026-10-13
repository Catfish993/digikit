# patchkit: firmware modules for Digitakt II and Digitone II

Status: design agreed 2026-10-13; Phase 0 and Phase 1 done (lane `a`, 2026-10-13); Phase 2 done (lane `a`, 2026-10-13). Branch `work/patchkit`.

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

Spec (decided 2026-10-13):

- **Split.** Rust `native/patchkit` (lib + `patchkit` binary + wasm32 cdylib, the native/boot pattern; deps:
  `loader`, `coldfire` (std-only), sha2, hmac, serde, serde_json, toml) does everything at patch time: `.syx`
  decode (via `loader`) and encode, aPLib pack, HMAC seal, checksums, 8-in-7 framing with the per-device packet
  constant, port load and verify, link, apply, conflicts, record, plan, explain. Python does everything at
  module-build and port-resolve time (toolchains, cfdb/sharcdb queries): `tools/pk_build.py` (src -> `.dko`),
  `tools/port_resolve.py` (rules -> port lockfile). `tools/patchkit.py` wraps the Rust CLI for mise.
- **Codec.** Untouched sections keep their stored bytes verbatim; changed sections are packed with an aPLib packer
  ported from `dt2/aplibpack.py` that produces the **same bytes** as the Python packer (test on all 16 stock
  sections). Hard limits from the port (`container_max` 0x300000, section 7 stored <= 0x100000 and depacked <=
  0xfffff, MAIN staged image <= 0x3ffc00); sections 2, 4, 8 can never change. Every output is re-decoded and
  checked (checksum, HMAC, packet checksums, limits, record).
- **Port lockfile** `devices/ports/<short>-<version>.toml`:
  - `[port]`: device, version, `syx_sha256`, `main_sha256`, `dsp_sha256`, `resolved_by` (tool + version).
  - `[limits]`: container and section limits above.
  - `[[region]]`: name (`cf.flash_cave`, `cf.expansion`, `dsp.l2`), cpu, start, end, excludes, mark.
  - `[[anchor]]`: name (`cf.reset.init_calls`, `dsp.master.q31_call`, ...), cpu, addr (byte address; SHARC also
    gives the PM word address), len, `sha256` of the bytes, mark, source rule.
  Anchors are declared with resolution rules by the modules and frameworks that need them
  (`[[anchor_rule]]` in module.toml: masked signature, `cfmatch` from a reference port plus offset, or SHARC
  `reloc_hash` function match plus offset). `port:resolve` resolves every declared rule for one firmware and
  rewrites its lockfile. patchkit only reads lockfiles and verifies anchor hashes; a firmware without a lockfile
  has no compatible modules.
- **Objects** `.dko.json`: canonical JSON (sorted keys, no floats) from the `tools/pk_link.py` prototype: header
  (arch `cf` | `sharc`, format version, source sha256, toolchain), sections (name, kind text|data|bss, align,
  hex), symbols (native units: bytes, or 16-bit parcels for SHARC), relocs `{section, offset, type, symbol,
  addend}` with explicit addends; types `cf.32`, `cf.pc32`, `cf.pc16`, `cf.pc8`, `sharc.abs24`, `sharc.abs32`.
  The ISA check (our decoders) runs in `pk_build.py`, not at patch time.
- **Module manifest** `module.toml`: `[module]` id, version, name, category, status, summary, `requires`,
  `conflicts`; `[targets]` (`dt2-1.17 = "tested"` ...); `[[object]]` paths; `[[anchor_rule]]`; ops; `[[claim]]`
  resources. v1 ops: `place` (objects into a named region, implied by `[[object]]`), `write` (bytes at an anchor
  plus offset, may carry relocs to module symbols), `retarget` (rewrite the target of a CF `jsr/bsr/jmp` or SHARC
  `CALL/JUMP` at an anchor to a module symbol). Displaced-instruction trampolines wait for Phase 3.
- **Built-in `core.expansion`**: whenever a module places into `cf.expansion`, patchkit appends
  `[len][dest][payload]` to MAIN OS, writes the loader (its own `.dko`, `modules/core/expansion/`) into
  `cf.flash_cave`, and redirects `cf.reset.init_calls`; the loader also zeroes module bss. `dsp.l2` placement uses
  boot-stream `APPEND` blocks.
- **Apply order and conflicts**: dependency topology then id; refuse overlapping bytes after linking, colliding
  claims, unmet requires/conflicts, a region over capacity, or an anchor hash mismatch.
- **Patch record** `DKPR` (format version, base `syx_sha256`, `id@version` list) in `cf.expansion` when the patch
  uses it, else in an appended `dsp.l2` block. patchkit refuses inputs that already carry a record.
- **CLI**: `patchkit inspect X.syx` (device, version, port, record), `check` and `apply --firmware X.syx
  --modules a,b [-o OUT] [--plan plan.json]`, `explain --plan`, `verify OUT.syx`. Modules are found under
  `modules/` by id.

Slice first (one agent), then coverage:

- [x] Slice: codec + port lockfile + `write`/`retarget` + `dsp.l2` placement + record + verify, proven by the
      master-gain module (spike 0.7 routine as SHARC source, one module for both devices) on DT2 1.17 and DN2 1.12:
      -6.02 dB at gain 0.5, bit-identical at 1.0, through `firmware.load`.
- [x] Oracle: DN2 master-half as a one-`write` module: decoded section 7 equals `dsp_patch.py syx`.
- [x] Oracle: XSLICE as a pinned raw-writes module (converted from `machinebuild` plan, dt2-1.16 only): section 3
      sha `cdbd7726...`.
- [x] `core.expansion` and a CF module in `cf.expansion`: boots to ready, function callable (pk_cfcall), region
      content intact.
- [x] Conflicts, canonical order, `plan.json`, `explain`; aPLib byte-equality with Python on all 16 sections.
- [x] wasm build in `tools/native_wasm.py` (`public/patchkit.wasm`); CLI and wasm outputs byte-identical.

### Phase 1 results (2026-10-13, lane `a`)

Details and numbers: `docs/findings/21-patchkit-core.md`. Built: `native/patchkit` (lib, `patchkit` binary, wasm
C ABI), `tools/pk_build.py`, `tools/port_resolve.py`, `tools/patchkit.py`, ports for dt2-1.16, dt2-1.17,
dn2-1.11 and dn2-1.12, modules `core/expansion`, `fx/master-gain`, `test/cf-demo`; the generated (uncommitted, `out/modules/`) oracle modules
`oracle/dn2-master-half` and `oracle/xslice-dt2-1-16`; mise tasks `patch`, `patch:build`, `patch:explain`, `port:resolve`,
`inject`.

- The codec rebuilds all four stock images byte for byte; the Rust aPLib packer equals the Python one on all 16
  packed sections (and is 25 times faster). Stock payloads start at container offset 0x90, not 0x80 as
  `dt2/build.py` writes [C].
- `fx/master-gain` on DT2 1.17 and DN2 1.12, injected through `firmware.load`: bit-identical at gain 1.0,
  -6.0206 dB at 0.5, from the same ready state.
- Oracles: DN2 master half (all decoded sections equal `dsp_patch.py syx`), XSLICE (section 3 sha256
  `cdbd7726...ecb86a`).
- `core/expansion`: the loader (54 bytes) boots to ready on both devices with DSP audio (DN2 6.4 s, DT2 13.6 s,
  PCM equal to stock); a C module with code, data and bss runs in place.
- Deviations from the spec, all small: `hmac` is a 20-line function over `sha2` instead of a crate; module ids
  are paths (`core/expansion`, `fx/master-gain`), not dotted; the segment header is `[len][dest][bss]` (the loader
  zeroes bss) with a `0xffffffff` terminator; `anchor` kinds `code`, `data` and `value` (a number read from an
  instruction, for the converter's buffer address); a manifest `ports = [...]` field pins a module to firmware
  (XSLICE); an `--no-record` switch exists for the oracles only; stock containers keep their own first payload
  offset; SHARC `retarget` supports the direct CALL only, and the SHARC listing of `explain` lives in the Python
  front end.

### Phase 4.1 results (2026-10-13, lane `fastweb`)

Details: `docs/findings/22-patchkit-master-drive.md`. `modules/fx/master-drive` patches DT2 1.17 and DN2 1.12 from one
source: a soft-clip saturator at the Q31 converter call and the master page's encoder 7 (the compressor's Sidechain
Filter, fx[66]) relabelled `DRV`, by two strings in `cf.expansion` and the descriptor row's name pointers. Both devices
pass `tools/pk_proof_drive.py`: bit-identical to stock at the neutral value, harmonics and level rise with drive, no
clicks while the knob moves, no fault. 20 to 968 DSP instructions per block.

- **Neutral is 0x5000 (UI 80), not 0:** kits do not start from the descriptor default [C]; the module keeps the default.
- New: anchor rule kinds `sharc_mem_addr` and `cf_words` (port_resolve), `(SW)`/`(SWSE)` direct loads in `pk_sharc`
  (Selache ignores the suffix), `run_call(approx_recips=...)`, data-only ColdFire objects.
- Open: hardware; whether R5-R13 are dead at the hook (saved anyway); DT2 1.16 and DN2 1.11 not run; the module's
  emulator test is slow and skips without inputs.

### Phase 2: developer loop and harness (about 3 days, high)

- [x] mise tasks above (`patch:new`, `patch:dev`, `patch:test`); module template; agent skill
      `.claude/skills/digikit-module/`.
- [x] Emulator: emit `fault` and `halt` events; main-loop heartbeat (+ `stall` event); check whether the MCF5441x
      watchdog is modelled (no: and the firmware never configures it, finding 17).
- [x] Harness: smoke tour, goldens, stress, matrix; `patch:dev` watch loop.

### Phase 2 results (2026-10-13, lane `a`)

Details: finding 17 ("Health events, heartbeat and the watchdog") and `tools/pk_harness.py` (module docstring).

- Emulator: `fault`, `halt`, `stall`/`stall.end` events, `status.heartbeat`, `mem.cf.peek/poke`, `watchdog.config`
  (`native/control/src/health.rs`, 5 unit tests; end to end on DT2 1.17 and DN2 1.12 through `tests/patchkit_smoke`).
  The first real run already caught a DSP halt in stock DT2 1.17 (pattern 3 or 4 plus PLAY, a conditional Type 7a in
  the generated core, finding 17), now a test and a documented limit.
- Harness: `tools/pk_harness.py`, `tests/patchkit_smoke/` (tour `tours/smoke.toml`, `matrix.toml`, `goldens.json` with
  SHA-256 only, 9 pytest tests plus 4 slow matrix cells), module tests from `modules/*/*/tests/*.toml`. Tour: boot to
  the readiness contract, track change, pattern change, audition (PCM), play/stop with the sequencer running,
  machine menu open and close, save project and reload, a seeded 40-input stress; every segment starts from `restart`.
  Matrix (device x firmware x module set), one PASS/FAIL line per cell, logs under `out/runs/patchkit/`.
- Matrix result: DT2 1.17 and DN2 1.12, stock and `fx/master-gain` + `test/cf-demo`: 4 of 4 PASS against the
  recorded goldens, 28 to 35 checks and 28 to 52 s per cell. Frames are deterministic across runs, and the patched
  cells produce the same frame hashes as stock.
- Dev loop: `patch:new` (template builds), `patch:dev`. Edit to running on DT2 1.17: DSP change 2.9 s (cached MAIN),
  ColdFire change 12.6 s (cold boot); edit to quick tests done 4.8 s and 14.5 s.
- Open: ready inputs of 1.16 and 1.11 are not in the matrix (`snapshots/*-ready` built before the INTC-force fix; the
  harness refuses stale ready inputs); `underrun` is not emitted; the conditional Type 7a halt.

### Phase 3: frameworks

- [x] `core.expansion` (loader, region): done in Phase 1 (finding 21).
- [x] `core.machines` DT2 1.17 (lane `b`, 2026-10-13): generalises machinepatch's nine parts to N types, plus the
      persistence immediate, the list allocation, a seventh clone site, the type argument of the parameter mapping and
      the DSP selector table; XSLICE and a second clone as content modules. See "Phase 3 results".
- [ ] `core.master-fx` (with 0.7 master hook).
- [ ] `core.machines` DN2 (3-5 days): parameter-page layer. Assessment in "Phase 3 results"; not started.
- [x] Persistence check: project saved on a patched image, loaded on stock (emulator): the track loads as SAMPLE.

### Phase 3 results (2026-10-13, lane `b`)

Details, evidence and the DSP binding contract: `docs/findings/02-machines-and-parameters.md` ("core.machines").
Built: `native/patchkit/src/machines.rs` (the generator, 4 unit tests, `tests/machines.rs` with 6 integration tests on the
real ports and images), `[[machine]]` in the manifest, `modules/core/machines` (24 ColdFire anchor rules of the new kind
`cf_func`, one DSP rule), `modules/machines/xslice`, `modules/test/machine-clone`, `tools/pk_machine_rules.py`,
`tools/pk_proof_machines.py`, `tools/card_overlay.py` and the daemon method `card.overlay`; harness steps `expect_mem`
and `expect_dsp`; two new matrix cells.

- DT2 1.17 and 1.16 (same proof, same results): XSLICE is selectable (type 7), its SRC page shows the CFADE knob, the type reaches the DSP (TX frame word,
  DSP cache), the sound is bit-identical to stock MANUAL SLICE (audition and sequencer), the MOD destination picker lists
  its parameters, a saved project keeps type 7 (control: without the persistence immediate it falls to SAMPLE) and
  loads as SAMPLE on stock firmware without a fault. XSLICE and a second clone together get types 7 and 8 in any
  selection order (same `.syx` bytes) and both sound like MANUAL SLICE.
- Corrections to the old XSLICE: an illegal 8-byte ColdFire instruction in `machinepatch.build_rank_shim` (images from
  `tools/machinebuild.py` fault on a real ColdFire), the list builder's fixed seven-entry allocation, a missing
  `type == 6` site in the frame builder.
- Constraint found: the emulator fetches ColdFire code only from the MAIN OS image, so framework code lives in the
  2,108-byte flash cave (about 80 B per added type, 452 B for two). Open: execute from RAM in the model.
- DSP binding: extern symbol `machines.<id>.type`; the DSP's selector table is relocated (word per added type, `selector`
  in the manifest picks the render path).
- Not done: DN2, hardware, p-lock and parameter-effect checks of a new machine, types above 8.

**DN2 1.12 assessment (not built).** The dispatch, list, name table, permission check, persistence immediate and the group
helper exist (finding 02, "Digitone II 1.11 has the same machine machinery"): five types, type byte at `+0xde`, name
rows ordered `{hint, name, abbrev}`, no sort comparator and no per-type table, list builder allocation of five. Missing
for a framework port: anchors for the permit bound, the group bound and the clone sites (`type == k` tests of the
machine cloned: to be searched), the DN2 DSP selector table (a different function, same idea) and, the large unknown,
the parameter-page layer (`MachineParameterPageView`, `SrcMachineParamPageCopy`) that gives each synthesis engine its
own pages. Steps: (1) per-device layouts in `machines.rs` and anchors on 1.11 and 1.12 for a clone of FM Tone (1 day);
(2) read how a type picks its page layout and whether a descriptor row is enough (1 to 1.5 days, the risk); (3) DSP selector
and proof tool on the ready inputs (1 day). Estimate 3 to 4 days of agent time, confidence about 50 % for a clone that
shows its own name and sounds like its original, lower for a new parameter page. Payoff is moderate: the framework is
the same, but DN2's machines are FM engines, so new DN2 machines need the DSP voice lane first.

### Phase 4.2 results (2026-10-14, lane `fastweb`)

- Hook: the first instruction of the sample renderer `0x1c4ecf` (DT2 1.16/1.17, one DSP blob) is overwritten with a
  `JUMP` (new op `jump`); a call-site retarget of the voice loop would bar the AOT region of the engine's busiest
  function (4 frames/s). Types register with the new op `register` (a row `{type, pc}` in a table another module
  declares with `[[table]]`); an unregistered type falls through, PCM bit-identical to stock, +1,120 DSP
  instructions per block. Spec and evidence: findings 06, "Per-voice render hook: built". Assembler facts: findings 20.
- `test/sine-voice` (type 8 forced on the wire): 261.626 / 523.252 / 277.184 Hz for note 60 / 72 / tune +1, the
  level, reverb-send and filter stages act on it. `machines/pluck` (type 9): Karplus-Strong, one string per track,
  fundamental within 0.0003 cents at notes 60 and 72, T60 1.02 s for a requested 1.02 s; analytic tuning error
  under 0.01 cents from note 7 to 95, 0.13 at 104, 0.42 at 108.
- **Contract with `core.machines` (lane b):** a machine module gives `core.dsp-voice` its type number and routine
  with `[[op]] op = "register", table = "_dv_table", row = ["<type>", "<routine symbol>"]`; today the type is a literal in the manifest
  and should become the symbol `machines.<id>.type` that `core.machines` assigns (patchkit resolves a row word that
  is not a number as a global symbol). The ColdFire side must put the type in frame word `0x94 + 2t` (finding 04),
  keep types below 24 (the selector table was read to 23, findings 06), and map the descriptor fields to the
  engine slots listed there; Pluck's table: slot 0 TUNE (25), 1 DECAY (26), 2 PICK (27), 3 unused (28 stays 0),
  4 BRIGHT (31), 5 COLOR (32), 6 unused (33), 7 LEV (34).
- Open: DN2 (a different, FM voice engine; see the assessment in findings 06), hardware, DT2 1.16 not run.

### Phase 4: proof-of-concept modules

- [x] 4.1 `fx/master-drive` (or compressor), DT2 and DN2: a new parameter carried in a frame word the DSP
      discards today, DSP code at the master hook. First module that changes both CPUs. Done 2026-10-13 (lane
      fastweb), see "Phase 4.1 results".
- [x] 4.2 Simple voice (sine or noise) on DT2 through `core.dsp-voice`. Done 2026-10-14 (lane fastweb), see
      "Phase 4.2 results". The Pluck DSP side (`machines/pluck`) is done too; its ColdFire entry waits for `core.machines`.
- [ ] 4.3 `machines/granular` DT2 (grains from sample memory), 3-5 days after 4.2.
- [ ] 4.4 `machines/granular` DN2. **Open decision:** grain source (track oscillator or wavetable, audio input, or
      a resample buffer).

### Phase 5: web (about 4-5 days, can start after Phase 2)

- [x] `patchkit.wasm` worker; `modules/index.json` build step.
- [ ] Browser cold boot from `.syx` and in-page extraction (open: the patched image boots in the plain core, with
      screen and controls but no DSP audio).
- [x] Load the COI service worker (both pages, through the shared layout).
- [x] `/patch` gallery (categories, status and compatibility badges, dependency and conflict explanations),
      patch, verify, boot, download.
- [x] UI cleanup: dev-only controls behind a flag, device follows the uploaded firmware.

### Phase 5 results (2026-10-13, lane `b`)

- Build step `packages/web/scripts/gen-modules.mjs` (run by `predev` and `prebuild`) writes
  `public/modules/index.json` (cards: id, name, category, status, summary, targets, requires, conflicts, claims,
  `gallery/README.md` as data blocks, never HTML) and `public/modules/bundle.json` (ports and modules as texts, equal
  to `tools/patchkit.py bundle`). Committed `modules/` and `devices/ports/` only; both outputs are gitignored. The
  build guard now also rejects `.syx`, `.snap` and `.mstate` files.
- `patchkit.wasm` gained one export, `pk_check` (per-module compatibility with an image, `wasm.rs`). The worker
  (`src/patch/patch-worker.ts`, wrapper `patchkit-core.ts` shared with the node tests) checks, applies in
  canonical order and verifies against the stock image; it fetches only the site's own wasm and bundle.
- `/patch` (`src/components/PatchGallery.tsx`): drop zone, identification against the device profiles and ports,
  category filter, status and per-firmware badges, automatic `requires`, conflicts and shared claims explained,
  patchkit's own error text for incompatibility, result panel, download (`Digitakt_II_OS1.17+fx-master-gain.syx`),
  "Boot in emulator" through IndexedDB (`src/patch/handoff.ts`, `/?handoff=1`), safety notes.
- Emulator page: shared layout with a top nav and theme tokens (`styles/site.css`, light and dark); dev-only
  controls (device select, export diagnostics, DSP image/state/snapshot pickers, bench, `?leds=`) show on the dev
  server or with `?dev=1` (`src/dev.ts`); the device follows the firmware's hash. The one pre-existing oxlint
  error (early return in `Faceplate`) is fixed.
- Checked with `tools/web_shots.mjs` (headless Chrome): on the dev server and on `astro preview` (no headers), DT2
  1.17 and DN2 1.12 patched with `fx/master-gain` in the page are byte-identical to the command line, and "Boot in
  emulator" boots the patched image in the plain core to the startup display.
- Open: no DSP audio and no cold boot in the browser; the gallery has one real module; hardware untested; the
  emulator page's own `fetch('/emulator-core.wasm')` is root-absolute (breaks under a Pages sub-path, as before).

### Phase 6: agent tooling (about 2-3 days, parallel with Phases 1-3)

- [x] `tools/fw.py`: compact query CLI with `batch` mode and cached graphs; skill file.
- [x] Atlas `out/atlas/<image>.sqlite`: machines, parameters, FX, page views, strings, sequencer structures; FTS5
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

## Emulator confidence, wave 3 (2026-10-13, lane `a`)

- Conditional Type 7a is modelled (finding 05): DT2 1.17 and 1.16 PLAY in patterns 3 and 4 runs clean; DN2 pins and the
  61 oracles, DT2 tone, oracles and tier-quick pass against regenerated cores (`gen-next` dirs, coordinator swaps them
  in at merge).
- Boot ROM rules (finding 06, "Boot ROM conformance"): `tools/sharc_bootcheck.py`, `native/patchkit/src/bootrom.rs`;
  stock streams, `fx/master-gain`, `fx/master-drive` and the L2 canary pass. Two defects fixed: the FIRST block's
  next-application pointer was not grown by `append_block`, and the append window included the ROM's 8 KB workspace.
  The emulator now refuses such streams (`tools/ready.py`, `firmware.load`, `patchkit verify`).
- selcc pointers: a normal-word access through a byte-address pointer matches the manual (finding 05).

## Pre-flash review (what no emulator run or public manual closes)

Em flashes nothing until everything works in the emulator; these are the risks that remain after that, each with the
best mitigation available. Ranked by what a failure costs.

| # | Risk | Evidence now | Mitigation |
|---|---|---|---|
| 1 | The boot ROM rejects the patched DSP stream (it applies a rule the manual does not state) | All stated rules checked on stock, gain, drive and canary streams (finding 06). A rejected stream is a **boot hang**: the ColdFire polls the SHARC handshake with no timeout | First hardware step is the image that changes nothing (`tools/patched_image.py`), then a DSP change with no appended block (`fx/master-gain` is a 3-byte in-place edit plus a 148-byte record block: take the smallest first), then the append. Keep the stock `.syx` and the STARTUP-menu recovery (0.2) at hand |
| 2 | Cycle cost: the emulator counts instructions, silicon counts cycles (L2 fetch and data wait states, cache misses on first execution, bank conflicts, SIMD memory pairs) | `fx/master-drive`: 20 to 968 DSP instructions per block; idle share 50 % (DN2), 68 % (DT2) measured in instructions (finding 22, 0.7) | Budget a module at no more than a quarter of the measured idle instructions per block; keep the hot loop short enough to stay in the cache; the neutral setting bypasses the heavy path where the module allows it. Listen for clicks at the first run; there is no `underrun` event in the emulator |
| 3 | Real DDR contents and timing: the emulator fills DDR from the stream's FILL blocks and skips the initcode's clock and DMC calibration | No patch writes DDR; sample RAM use is stock | None needed while modules stay out of DDR; keep it that way in v1 |
| 4 | Boot time grows with the stream: the ColdFire sends one byte per DSPI frame and handshake poll; the canary doubles the section | Gain +148 B, drive +620 B on 321 KB; the transfer time per byte was not measured against hardware | Ship modules small; use the canary only on the emulator |
| 5 | SPI slave boot expects multiples of 1024 bytes (HWR 40-21); the stock section is not | Stock boots with a short tail; patched tails differ (finding 06 (d)) | If the first appended image hangs while the in-place-only image boots, pad the appended block so that the section length keeps the stock value mod 1024 (505 including the command byte); a one-line change in `append_block` |
| 6 | L1 targets and L1 PM room (appends refuse L1); the datasheet map is not in `out/refs` | No L1 appends | None; L2 only |
| 7 | Rules and state of the ROM-prepared environment: L2 ECC initialised, caches off at handover, `RCU_BCODE` bits | Stock code already runs from L2 loaded this way (DT2: the 109 KB block at `0x20000000` holds functions at pc `0xb80000...`, 24,070 instructions in sharcdb) | Same path as stock; nothing to do |
| 8 | Float behaviour on silicon against the core: denormal flush, saturation mode, reciprocal approximation (`approx_recips`) in module code | Seven DN2 PCM pins and 61 oracles exact against the core; module code unit-run and PCM-proved in the emulator only | Prefer plain add, multiply and compare in module code; check a module's output against its emulator render by ear and by the harness PCM test before trusting a new operation |
| 9 | Interrupts, DMA and ColdFire timing under real load (the DSPI frame cadence, SSI underruns, hot sample loading): the emulator is deterministic | Coupled runs and 35 s tours pass; 40-input stress in the harness | Hardware soak after the first success |
| 10 | The MAIN OS image validator (`0x400d9e4c`) and the real flash writer were not run; flash cap 3 MiB and the HMAC trailer are checked by the emulated bootstrap | 0.2 [V] for the bootstrap; patched containers rebuild stock byte for byte | The harmless image first; the updater's own checks reject a bad container before it writes (the bootstrap runs from section 2, which patchkit never touches) |
| 11 | Firmware version: the device runs 1.16; most proofs ran on 1.17 (and DN2 1.12). 1.16 has the same DSP section; its MAIN differs, `fx/master-drive` was not run on 1.16, and 36 of 135 DT2 FX oracles differ on 1.17 | Port files for 1.16 exist; 1.16 PLAY in patterns 3 and 4 passes | Run the module test tour on 1.16 in the emulator before the first flash (`DIGI_PK_READY_DT2_116`, `tests/patchkit_smoke`), and flash the 1.16 port |
| 12 | DN2 patches cannot be tested on hardware at all (no device on the desk) | Emulator only | Say so on the gallery page: DN2 modules are emulator-tested only |
| 13 | Saved projects and kits made on patched firmware read back on stock firmware (Phase 3 not done) | Not tested | Do not save over a stock project while testing; back up the card |
| 14 | Analog level: a drive or gain module can raise the output above the converter's range | Q31 clip in the emulator, neutral default is bit-exact | Start at the neutral setting with the monitor volume low |

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
