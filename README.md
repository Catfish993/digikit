# digikit

digikit adds new machines and effects to the Elektron **Digitakt II** and **Digitone II**. It patches your own copy of
the stock firmware with a small runtime. The runtime loads modules from the device's +Drive, so you can add or change
a machine without flashing again. An emulator runs the real firmware on your computer, so you can test everything
before it goes near the device.

> **Experimental.** Flashing modified firmware is at your own risk. Only flash images that pass `mise run preflight`.
> digikit never changes the bootloader, so the stock OS can always be restored (see [Recovery](#recovery)).
> **No Elektron firmware is in this repository**: you supply your own `.syx` file.

## Features

- **Runtime on the device:** a SETTINGS > DIGIKIT menu, modules loaded from the +Drive at boot, upload over USB, and a
  debug log you can follow on your computer.
- **Run-time machines:** new machine types next to the stock ones, with their own parameters and DSP code. A safety
  guard mutes a misbehaving machine instead of silencing the whole device.
- **Emulator:** the real firmware (ColdFire and SHARC DSP) runs on your computer, with the front panel, display and audio.
- **Module tests:** every machine is checked against a standard set of tests (parameter limits, modulation, long
  notes), in seconds, before it reaches a device.
- **Browser patcher:** patch your firmware in the browser (`mise run web`, page `/patch`); nothing is uploaded.

## Modules

| Module | What it does | Emulator | On a device |
| --- | --- | --- | --- |
| [digikit runtime](modules/core/runtime/README.md) | Menu, module loading, upload, debug log | Digitakt II, Digitone II | Digitakt II (OS 1.17) |
| [PLUCK](modules/machines/pluck/README.md) | Plucked-string synth: decay, pick position, brightness, colour | Digitakt II, Digitone II | Digitakt II (OS 1.17) |
| [Master gain](modules/fx/master-gain/README.md) | Scales the master output by a set gain | Digitakt II, Digitone II | not yet |
| [Master drive](modules/fx/master-drive/README.md) | Soft-clip saturation on the master output | Digitakt II, Digitone II | not yet |
| [XSLICE](modules/machines/xslice/README.md) | A copy of MANUAL SLICE under its own name (a demonstration) | Digitakt II | not yet |

Supported firmware: **Digitakt II OS 1.17** and **Digitone II OS 1.12**. Download them from Elektron's support site and
put them in `firmware/` (`Digitakt_II_OS1.17.syx`, `Digitone_II_OS1.12.syx`). Device work so far is on the Digitakt II.

## Setup

You need [mise](https://mise.jdx.dev/) and [uv](https://docs.astral.sh/uv/). Then, once:

```sh
mise install
mise run setup        # Python and web dependencies
mise run toolchains   # the compilers for module code
```

## Flash the runtime (Digitakt II)

1. Build and check the image:
   ```sh
   mise run patch -- --firmware firmware/Digitakt_II_OS1.17.syx \
     --modules core/runtime,core/digikit-menu,core/health,core/dynreg,core/dyndsp -o out/patch/digikit-dt2.syx
   mise run preflight -- out/patch/digikit-dt2.syx      # must print PREFLIGHT OK
   ```
2. Open Elektron Transfer, connect the Digitakt over USB, and drop `out/patch/digikit-dt2.syx` on it while the device
   shows its main screen. When the transfer ends, the device asks to upgrade: press **YES**.
3. After the restart, SETTINGS > DIGIKIT shows the runtime (ABOUT shows its version).

## Put a module on the device

With the runtime flashed and the Digitakt connected over USB:

```sh
mise run dk:run -- test/dyn-pluck --port Digitakt --seconds 60
```

This builds the module, uploads it to the +Drive, selects it, restarts the Digitakt and shows the log. Then pick
**PLUCK** as the machine of a track. You can also tick modules by hand in SETTINGS > DIGIKIT > MODULES and choose
APPLY & RESTART.

Useful while testing:

```sh
mise run dk:log -- --port Digitakt follow                       # follow the debug log (saved under out/runs/device-logs/)
mise run dk:screen -- --port Digitakt out/runs/shots/screen.png # a screenshot of the display
mise run dk:store -- --port Digitakt ls                         # the modules on the +Drive
```

## Run the emulator

```sh
mise run emu:ready -- dt2 --syx firmware/Digitakt_II_OS1.17.syx   # once: boot the firmware and save its state (about 2 minutes)
mise run emu -- firmware/Digitakt_II_OS1.17.syx   # the stock firmware
mise run emu -- out/patch/digikit-dt2.syx         # your patched image
```

The emulator's DSP does not run in real time yet, so audio plays slower than on the device. The `dk:*` commands work
with the emulator too: use `--emu` instead of `--port Digitakt`.

## Recovery

If a patched image misbehaves, flash the stock `.syx` the same way (step 2 above). If the device does not start, hold
FUNC while powering on to reach the STARTUP menu, and send the stock OS over MIDI DIN
([PATCHING.md, "Recovery"](docs/PATCHING.md#recovery)).

## More

- [Writing modules](docs/MODULE-AUTHORING.md), and the planned module SDK: [docs/design/module-sdk.md](docs/design/module-sdk.md)
- [Patching, flashing and the device steps in detail](docs/PATCHING.md)
- [Developing digikit](docs/DEVELOPMENT.md): the emulator's options, tests, analysis tools, repository layout
- [Testing](docs/TESTING.md) and [results of the research](docs/FINDINGS.md)
- [Contributing](CONTRIBUTING.md)

## Licence

digikit ([github.com/ElekLabs/digikit](https://github.com/ElekLabs/digikit)) is licensed under
AGPL-3.0-or-later ([LICENSE](LICENSE)), with additional terms under section 7 of the AGPL
(attribution, origin and names) in [NOTICE](NOTICE). Keep NOTICE and the attribution
"digikit by Em D - https://github.com/ElekLabs/digikit" in copies and modified versions; a
user interface of a covered work shows it in its About, credits or footer area.

The licence covers the code here only; it gives no rights to Elektron's software or
firmware. digikit is not affiliated with or endorsed by Elektron. Digitakt, Digitone and
Syntakt are trademarks of Elektron Music Machines. Container format knowledge comes from
`mischa85/elektron-firmware-tool` (MIT); some architecture facts from
`lalzart/digitakt-ii-firmware-research-public`. The public SHARC+ assembler `js216/selache`
is used only as a read-only cross-check. `tools/coldfire/ghidra/ColdfireEMAC` contains files from
Ghidra under the Apache License 2.0.
