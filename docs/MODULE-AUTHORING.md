# Writing a patchkit module

A module adds something to a stock Digitakt II or Digitone II firmware: a machine, a parameter, a DSP effect, or a
change to the ColdFire user interface. This page is the short path from an empty module to a patched firmware running
in the emulator. Details: [PATCHING.md](PATCHING.md) (commands and how patchkit works), [modules/README.md](../modules/README.md)
(manifest reference and worked examples).

Nothing here touches hardware. Flashing a patched image is a separate, deliberate step (see "Safety and hardware" in
PATCHING.md).

## Quick usage

Run every command from the repository root. Put your own stock firmware files in the root
(`Digitakt_II_OS1.17.syx`, `Digitone_II_OS1.12.syx`).

### 1. Set up once

```sh
mise install
mise run setup         # Python and web dependencies
mise run toolchains    # pinned ColdFire gcc/binutils and the SHARC+ assembler, into out/tools (about 15 min)
uv run python tools/ready.py dt2 --syx Digitakt_II_OS1.17.syx --out out/ready-pk-dt2-117 --force   # about 2 min
```

The last line builds the emulator's ready state for that firmware (it is derived from the firmware, so it stays under
`out/` and is never committed).

### 2. Create a module

```sh
mise run patch:new -- machines/my-synth --machine --devices dt2,dn2   # a new machine type
mise run patch:new -- fx/my-drive --dsp                                # a master-bus DSP effect
mise run patch:new -- ui/my-tweak --cf                                 # ColdFire code only
```

This writes `modules/<category>/<name>/` with `module.toml`, source stubs (`src/cf/*.c`, `src/dsp/*.s`),
`tests/smoke.toml` and `gallery/README.md`, and builds it so you start from code that compiles. A machine also gets a
permanent type number per device in `modules/core/machines/types.toml` (printed by the command; commit it with the
module: projects store this number).

Edit the sources and `module.toml`, then rebuild:

```sh
mise run patch:build -- modules/machines/my-synth
```

### 3. Patch a firmware

```sh
mise run patch -- --firmware Digitakt_II_OS1.17.syx --modules machines/my-synth -o out/patch/my-synth.syx
```

The frameworks the module `requires` are added for you. The command refuses anything that does not fit (wrong
firmware, a conflict with another module, no room) and verifies the image before it reports success. To see what went
where:

```sh
mise run patch:explain -- --firmware Digitakt_II_OS1.17.syx --modules machines/my-synth
```

### 4. Run the emulator and inject the patched image

In one shell, start the emulator:

```sh
mise run emu                          # the desktop app, or the headless daemon (flags in PATCHING.md, "Run it in the emulator")
```

In another shell, load your image into it:

```sh
mise run inject -- out/patch/my-synth.syx
```

A DSP-only change is applied while the firmware runs (about 1 s). A ColdFire change boots the patched firmware with
DSP audio (about 15 s the first time, under 1 s afterwards from a cache). Then use the faceplate: select your machine
with FUNC + SRC, turn its knobs, play trigs.

### 5. Iterate

```sh
mise run patch:dev -- --modules machines/my-synth --device dt2
```

This watches your sources, rebuilds, patches, injects into the running emulator and runs the module's quick tests on
every save, one summary line per edit (a few seconds for a DSP change).

### 6. Test

```sh
mise run patch:test -- --cell "dt2-1.17:machines/my-synth"     # smoke tour + the module's tests/*.toml, one cell
mise run patch:test                                            # the whole matrix (tests/patchkit_smoke/matrix.toml)
mise run patch:proofs                                          # the quick tier of every emulator proof (about 7 min)
```

A new module set needs its screen goldens recorded once (`--update`). Every run checks that the firmware does not fault,
the DSP does not halt and the main loop keeps running.

## What a module can do today

- **Machines** (`core/machines`): add machine types to the machine list on both devices, with their own names, a
  permanent type number that projects keep, and save/load that survives stock firmware (an unknown type loads as a
  stock machine).
- **Parameters** (`core/params`): give a machine its own parameters (names, ranges, defaults) on its pages; they
  persist with the project and appear as modulation destinations (parameter locks on added parameters are not
  tested yet). On the Digitone II a machine gets its own parameter pages.
- **DSP voices** (`core/dsp-voice`): render a machine's sound with your own SHARC+ routine; the stock amplitude
  envelope, filter, effects and master still apply. `machines/pluck` (a Karplus-Strong string, both devices) is the
  worked example.
- **Master-bus effects**: process the final stereo mix (`fx/master-gain`, `fx/master-drive`); a knob on the master
  page can drive them.
- **ColdFire code and data** in a 1.3 MiB (Digitakt II) or 24.8 MiB (Digitone II) expansion region, hooks at firmware
  sites, tables copied from the user's own image at patch time and extended with rows.
- **One module source for several firmware versions**: modules name firmware sites through anchor rules, never
  addresses; a new firmware version needs only `mise run port:resolve`.
- **The web gallery** (`mise run web`, page `/patch`): pick modules, patch in the browser, boot the result, download
  it. The firmware never leaves the browser.

Limits to know: type numbers 7..23 for added machines on the Digitakt II (17) and 5..23 on the Digitone II (19, a
chosen bound); one master-bus module at a time until a master-effects framework exists; patched DSP code runs slower than real
time in the emulator (correct, but not playable live yet); nothing has been flashed to a device yet.

## Where to look next

- [modules/README.md](../modules/README.md): every manifest key, the test format, `fx/master-gain` and `machines/pluck`
  walked through.
- [PATCHING.md](PATCHING.md): all commands, the emulator control socket, how patching works, safety and recovery.
- `.claude/skills/digikit-module/SKILL.md`: the same workflow for coding agents.
