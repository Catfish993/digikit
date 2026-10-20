# Writing a patchkit module

A module adds something to a stock Digitakt II or Digitone II firmware: a machine, a parameter, a DSP effect, or a
change to the ColdFire user interface. This page is the short path from an empty module to a patched firmware running
in the emulator. Details: [PATCHING.md](PATCHING.md) (commands and how patchkit works), [modules/README.md](../modules/README.md)
(manifest reference and worked examples).

Nothing here touches hardware. Flashing a patched image is a separate, deliberate step (see "Safety and hardware" in
PATCHING.md).

## Try it: the PLUCK machine in the emulator

The fastest way to see a module work: patch the existing PLUCK machine (a plucked string) into Digitakt II 1.17 and
inject it into the running desktop emulator. Firmware files go in `firmware/`; the ready state is built once
(about 2 minutes) and stays under `out/`.

```sh
uv run python -m tools.emulator.ready dt2 --syx firmware/Digitakt_II_OS1.17.syx --out out/ready-pk-dt2-117 --force   # once
```

Shell 1, the emulator on Digitakt II 1.17 (`DIGI_EMU_READY_DIR_DT2` points the app at that ready state; the default
one is for 1.16). The first launch builds the app, which takes several minutes:

```sh
DIGI_EMU_READY_DIR_DT2=out/ready-pk-dt2-117 mise run workbench -- firmware/Digitakt_II_OS1.17.syx
```

Shell 2, patch and inject:

```sh
mise run patch -- --firmware firmware/Digitakt_II_OS1.17.syx --modules digikit/pluck -o out/patch/pluck.syx
mise run patch:inject -- out/patch/pluck.syx
```

`patch` adds the frameworks PLUCK needs (`core/machines`, `core/params`, `core/dsp-voice`) and verifies the image.
`patch:inject` boots the patched firmware inside the running app with DSP audio (about 15 s the first time, under 1 s after).

On the faceplate:

1. Hold **FUNC** and press **SRC** for machine select; **PLUCK** is at the end of the list, after MIDI. Confirm with
   **YES**.
2. The SRC page shows PLUCK's parameters: TUNE, DECAY, PICK, BRIGHT, COLOR, LEV.
3. Press trig keys to play. Turn knobs with quick turns: slow single detents barely move a value (encoder
   acceleration).
4. Save the project and reload it: the track stays PLUCK (machine type 9). Stock firmware loads the same project with
   the track as SAMPLE.

Audio caveat: the emulator runs patched DSP code slower than real time (about 4.5x for a ringing PLUCK on a build
without PGO), so in the desktop app the screens and knobs behave normally but PLUCK's audio stutters. To hear it
clean, record the app's output, which is generated in emulated time:

```sh
DIGI_EMU_SINK_WAV=out/pluck.wav DIGI_EMU_READY_DIR_DT2=out/ready-pk-dt2-117 mise run workbench -- firmware/Digitakt_II_OS1.17.syx
```

Other modules to try the same way:

```sh
# a clone machine (XSLICE, a MANUAL SLICE clone with its own name; no DSP code of its own)
mise run patch -- --firmware firmware/Digitakt_II_OS1.17.syx --modules test/xslice -o out/patch/xslice.syx
# a master-bus drive: the DRV knob on the FUNC + MOD master page (encoder 7; above 80 adds drive)
mise run patch -- --firmware firmware/Digitakt_II_OS1.17.syx --modules digikit/master-drive -o out/patch/drive.syx
# Digitone II 1.12 (start the emulator with firmware/Digitone_II_OS1.12.syx and a 1.12 ready state)
mise run patch -- --firmware firmware/Digitone_II_OS1.12.syx --modules digikit/pluck -o out/patch/pluck-dn2.syx
```

Inject each with `mise run patch:inject -- <file>`. To see what a patch changed, and to go back to stock:

```sh
mise run patch:explain -- --firmware firmware/Digitakt_II_OS1.17.syx --modules digikit/pluck
mise run patch:inject -- firmware/Digitakt_II_OS1.17.syx
```

If the app starts without audio it prints which ready inputs are missing; rebuild them with the `tools/emulator/ready.py` line
above.

## Quick usage

Run every command from the repository root. Put your own stock firmware files in `firmware/`
(`Digitakt_II_OS1.17.syx`, `Digitone_II_OS1.12.syx`).

### 1. Set up once

```sh
mise install
mise run setup         # Python and web dependencies
mise run patch:toolchains    # pinned ColdFire gcc/binutils and the SHARC+ assembler, into out/tools (about 15 min)
uv run python -m tools.emulator.ready dt2 --syx firmware/Digitakt_II_OS1.17.syx --out out/ready-pk-dt2-117 --force   # about 2 min
```

The last line builds the emulator's ready state for that firmware (it is derived from the firmware, so it stays under
`out/` and is never committed).

### 2. Create a module

```sh
mise run module:new -- machines/my-synth --machine --devices dt2,dn2   # a new machine type
mise run module:new -- fx/my-drive --dsp                                # a master-bus DSP effect
mise run module:new -- ui/my-tweak --cf                                 # ColdFire code only
```

This writes `modules/<category>/<name>/` with `module.toml`, source stubs (`src/cf/*.c`, `src/dsp/*.s`),
`tests/smoke.toml`, `README.md` and `gallery/icon.svg`, and builds it so you start from code that compiles. A machine also gets a
permanent type number per device in `modules/core/machines/types.toml` (printed by the command; commit it with the
module: projects store this number).

Edit the sources and `module.toml`, then rebuild:

```sh
mise run module:build -- modules/machines/my-synth
```

The build decodes every ColdFire instruction with the repository's own decoder and refuses a form the MCF54418 does not have (a 68020 or FPU
instruction would trap on the device). It then records the passing check in the object (`toolchain.isa_check` of the `.dko.json`, a digest of
the object's sections, symbols and relocations), and `patchkit pack` refuses a ColdFire object whose record is missing or does not match its
bytes: an object made by hand or edited after the build cannot become a run-time `.dkm`. It guards against accident; it is not a signature.

### 3. Patch a firmware

```sh
mise run patch -- --firmware firmware/Digitakt_II_OS1.17.syx --modules machines/my-synth -o out/patch/my-synth.syx
```

The frameworks the module `requires` are added for you. The command refuses anything that does not fit (wrong
firmware, a conflict with another module, no room) and verifies the image before it reports success. To see what went
where:

```sh
mise run patch:explain -- --firmware firmware/Digitakt_II_OS1.17.syx --modules machines/my-synth
```

### 4. Run the emulator and inject the patched image

In one shell, start the emulator:

```sh
mise run workbench                    # the desktop app, or the headless daemon (flags in PATCHING.md, "Run it in the emulator")
```

In another shell, load your image into it:

```sh
mise run patch:inject -- out/patch/my-synth.syx
```

A DSP-only change is applied while the firmware runs (about 1 s). A ColdFire change boots the patched firmware with
DSP audio (about 15 s the first time, under 1 s afterwards from a cache). Then use the faceplate: select your machine
with FUNC + SRC, turn its knobs, play trigs.

### 5. Iterate

```sh
mise run module:dev -- --modules machines/my-synth --device dt2
```

This watches your sources, rebuilds, patches, injects into the running emulator and runs the module's quick tests on
every save, one summary line per edit (a few seconds for a DSP change).

### 6. Test

```sh
mise run patch:test -- --cell "dt2-1.17:machines/my-synth"     # smoke tour + the module's tests/*.toml, one cell
mise run patch:test                                            # the whole matrix (tests/patchkit/smoke/matrix.toml)
mise run patch:proofs                                          # the quick tier of every emulator proof (about 7 min)
```

A new module set needs its screen goldens recorded once (`--update`). Every run checks that the firmware does not fault,
the DSP does not halt and the main loop keeps running.

## What a module can do today

- **Machines** (`core/machines`): add machine types to the machine list on both devices, with their own names, a
  permanent type number that projects keep, and save/load that survives stock firmware (an unknown type loads as a
  stock machine).
  A machine of a run-time module (`dynamic = true`) has no entry in `types.toml`: its global id is `<module id>:<machine id>`, the runtime gives it a
  type number in 16 to 23 when it loads the module, and the card remembers the number (by a hash of that id) so the machine keeps it while it stays in the
  card's map of 8 machines. Read the number a boot assigned from `dkupload modules` (or the boot report), never write it into code or a test.
  `type_hint` (optional, per machine and device, within 16 to 23) is the number the machine takes when the card has no entry for it and the number is free: it exists for the machines that
  had a fixed number before sticky ids, so old projects still find them. **A new module leaves it out.**
- **Parameters** (`core/params`): give a machine its own parameters (names, ranges, defaults) on its pages; they
  persist with the project and appear as modulation destinations (parameter locks on added parameters are not
  tested yet). On the Digitone II a machine gets its own parameter pages.
- **DSP voices** (`core/dsp-voice`): render a machine's sound with your own SHARC+ routine; the stock amplitude
  envelope, filter, effects and master still apply. `digikit/pluck` (a Karplus-Strong string, both devices) is the
  worked example.
- **Master-bus effects**: process the final stereo mix (`digikit/master-gain`, `digikit/master-drive`); a knob on the master
  page can drive them.
- **ColdFire code and data** in a 1.3 MiB (Digitakt II) or 24.8 MiB (Digitone II) expansion region, hooks at firmware
  sites, tables copied from the user's own image at patch time and extended with rows.
- **One module source for several firmware versions**: modules name firmware sites through anchor rules, never
  addresses; a new firmware version needs only `mise run patch:port`.
- **The web gallery** (`mise run web`, page `/patch`): pick modules, patch in the browser, boot the result, download
  it. The firmware never leaves the browser.

### The register contract of a DSP voice routine

The firmware restores M5, M6, M7, M13, M14, M15, the L registers and MODE1 per DSP context, so a routine that is entered from
`core/dsp-voice` does not get them from the context: the wrapper saves them, **sets** them and restores them after the
routine (finding 45). While your routine runs: M5 = 0, M6 = 1, M7 = -1, M13 = 0, M14 = 1, M15 = -1 (so `DM(I0, M6)`
steps one word and `JUMP (M13, I13)` returns), L0-L5 = 0 (linear addressing), SIMD off, ALUSAT and TRUNCATE clear, RND32 set.
You may use R0-R15, I0-I5, I13, M1-M4 and B3 freely; keep I6-I12, I14, I15, M0 and M5-M15 as they are, and do not change L or
MODE1. The same holds on both devices (on the Digitone II the routine gets the slot's buffer in R4, on the Digitakt II the voice
record and the output block; the headers of `core/dsp-voice/src/dsp/*.s` list the entry registers). Code that
runs anywhere else on the DSP (a master-bus effect, a link hook) is not entered through the wrapper: it must not assume these
values either, unless it saves, sets and restores them itself as `core/dsp-link` does (the two master-bus modules still use M5 = 0
and M6 = 1 at the converter call they replace, where the stock code uses the same registers; finding 45). A 16-bit `Rn = Rn op Rx` followed by
some instructions can make the emulator read a different instruction than the silicon; the build refuses such code and says
so (use distinct registers, finding 45).

Memory a routine may write: the output block (`in->out`), its own voice's state block (`state_bytes` from `base + stride * track`), its
data stack (`stack_bytes`, below the adapter's frame) and the data and bss of its own object (shared by all voices: state that must differ per voice goes in `state`). Nothing else: not another voice's
state, not the adapter's data, not the frame copy. `module:check` runs the conformance suite on the concrete SHARC+ core and records
every store the module's code makes during `note_on` and `render`; a store outside these ranges fails with the module, device, call,
PC, address, size and the nearest known region. The sweep covers notes from low to high, parameter changes between blocks, and two
voices active at once (group `writes`).

Limits to know: type numbers 7..23 for added machines on the Digitakt II (17) and 5..23 on the Digitone II (19, a
chosen bound); one master-bus module at a time until a master-effects framework exists; patched DSP code runs slower than real
time in the emulator (correct, but not playable live yet); nothing has been flashed to a device yet.

## Run-time modules (.dkm)

A module can also be packed as a `.dkm` and loaded by the digikit runtime at boot, without patching a new image. The loop is
`module:new` -> `module:check` -> `dk:run --emu` -> `dk:run --port`, described in
[DEVELOPMENT.md, "Module loop"](DEVELOPMENT.md#module-loop).

- `mise run module:check -- NAME` prints the packed size by section per device, or "not packed" with the reason (for example a
  module that is patched in). `patchkit pack` and `patchkit dkm-info` print the same `size` line; `mise run patch:mem` prints the
  runtime's own bytes.
- Knob slots on a Digitakt II SAMPLE clone: slot 6 is usable. Slot 3 passes only the sample bits (`value & 0x3ff`): do not put a
  parameter there: the build refuses it (docs/findings/02-machines-and-parameters.md, last section).

### Versions and compatibility

- The runtime has a semantic version, now 0.1.0, packed as `major<<16|minor<<8|patch`. Runtimes 1 to 5 read as 0.0.1 to 0.0.5.
  `sdk/sdk.toml` is the one source; `mise run gen:modules` writes the generated files from it (`-- --check` lists stale ones).
- The contract versions are also in `sdk/sdk.toml`: api (1,1), machine (1,0), fx (0,0), service (1,0). `pack` writes the contracts a
  module uses into the `.dkm` header: api always, machine when the module has a DSP section, service when it has ColdFire slot
  functions or init/fini.
- Rebuild the module when a contract it declares (major not 0) changes its major version, or when the runtime's minor is below the
  module's. Then the loader and the menu refuse it with `E_CONTRACT` (-48, "built for another SDK contract: rebuild the module").
  The runtime version the module was built for (header byte 0x30) is informational and is not checked.

### Logging

- `Api.log(msg, value)` logs at level info. `Api.log_lv(level, msg, value)` takes a level: 0 error, 1 warn, 2 info, 3 debug, 4 trace.
- Each event carries its level and its source (the runtime, or the module). The default level is info; with DEBUG off in the menu only
  errors are kept.
- `mise run dk:log -- --port Digitakt level` prints the levels. `level debug` sets the default. `level --module NAME=trace` sets one
  module (NAME or record index; `NAME=default` clears it). `follow --level L --module NAME` filters the display. The levels are not
  stored on the card and reset at boot. Setting them needs runtime 0.1.0.

## Where to look next

- [modules/README.md](../modules/README.md): every manifest key, the test format, `digikit/master-gain` and `digikit/pluck`
  walked through.
- [PATCHING.md](PATCHING.md): all commands, the emulator control socket, how patching works, safety and recovery.
- `.claude/skills/digikit-module/SKILL.md`: the same workflow for coding agents.
