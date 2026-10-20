# 52. Sticky machine ids: a run-time machine's type number is assigned at load and remembered on the card

P3b-2 of the module SDK (docs/design/module-sdk.md 11.5 D7). Marks as in AGENTS.md: **[D]** measured or read once here, **[O]** open; nothing is **[V]**. Emulator only
(Digitakt II 1.17 and Digitone II 1.12 images); **the device has not run it.**

## What changed

- A run-time machine has no number in `types.toml` any more. Its global id is `<module id>:<machine id>`; its 24-bit hash is the low 24 bits of FNV-1a 32 of that string.
  The `.dkm` carries `hash << 8` where a stock machine has its type (machine record, page record, DSP voice entry); a low byte of 0 means "assign at load" **[D]**
  (`packages/patchkit/src/register.rs`, `docs/design/digikit-registry.md`).
- The runtime assigns the numbers for the whole selection before the first module loads (`assign.rs`), from a map of 8 entries (`hash << 8 | type`) in the selection
  record's reserved bytes (28 to 31 and 480 to 507); it writes the record once after the loads, only when the map changed. SELECT and the other writers keep the map bytes **[D]**
  (scenario `sticky-types`: six boots on the cards each boot left; SELECT checked in each).
- `MODULES` (protocol command 3, `Uploader.modules()`, `dkupload modules`) lists the loaded modules with arena and DSP ranges, contract bytes, assigned types, voice rows and the guard's
  fault count **[D]** (`debug-log`).
- Store edge case: only a valid selection record plus foreign bytes in both header sectors no longer reads as an empty store; BEGIN, DELETE and LIST answer `E_NOSTORE` and nothing is
  written **[D]** (`card-safety`).
- `debug-guard` runs on both images: the NaN fixture is an `own_dsp` machine with a one-knob page on the Digitone II; both images log `MACHINE_FAULT` with count 8 and one `MACHINE_DISABLED` **[D]**.

## Measured

| What | Result |
|---|---|
| fresh card, pluck then xslice | 16 and 17, in load order; both entries written |
| only xslice on that card | 17 again (sticky), map not rewritten |
| xslice and params | params 18, not 16: pluck's number stays reserved while it is in the map and a free number exists |
| the same three machines in another order | 18, 16, 17 as remembered, nothing rewritten |
| a full map, new machine | the lowest entry of a machine that is not in this boot is dropped (`MACHINE_TYPE_EVICTED`, an error event); an entry of a machine that is in the boot is never the one |
| PCM of `equiv-dyn-pluck` (DT2, DN2), `menu-dkrun-pluck`, `rung-c`, `debug-guard` (DT2) | equal to the P3a oracle (`out/sdk-oracle/2026-10-20/table.txt`); the DN2 `run.wav` equals `ref.wav`; the `guard.log` differs by the new events and the numbers |

## Open

- **[O]** The guard's words name only the type of the last fault and of the last type switched off, so the per-module fault count of MODULES is exact for one faulting type and goes to the last one when
  two types fault inside one poll. A per-row count needs the DSP rows to be readable from the ColdFire (they are write-only through the link now).
- **[O]** A card rebuilt from a +Drive folder (builder) has a zero map: the exporter does not carry it, so types may be assigned again after such a round trip. A project saved before that keeps the old
  numbers and would load another machine, or an empty row.
- **[O]** A hash collision between two machines of one boot gives the second a fresh number that is not remembered (the rule is implemented and has no test: no two ids collide in the repository).
