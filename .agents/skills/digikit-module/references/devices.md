# Device facts for module authors (Digitakt II 1.17, Digitone II 1.12)

What differs between the two devices when you write a machine or an effect. Sources: docs/findings/53-stock-dsp-shape.md,
docs/findings/06-sharc-engine-and-startup.md, docs/findings/32-runtime-dsp-machines.md, devices/digitakt-ii.toml,
devices/digitone-ii.toml. One machine source serves both devices; the adapter of `core/dsp-voice` hides the differences
listed under "Voices".

## Time and budget

| | Digitakt II | Digitone II |
| --- | --- | --- |
| Block | 32 samples at 48 kHz, 1500 blocks per second | same |
| Instructions per block (1 GHz assumed) | about 666,000 | same |
| Stock DSP busy per block (measured) | about 211,000 | about 335,000 |
| Free per block | about 450,000 | about 330,000 |
| Reference cost | pluck: about 900 per ringing voice per block; adapter and guard about 700 of that | similar |

Plan a machine for 16 voices at once: 16 x your `max_cycles_block` must fit with room for the other modules.

## Voices

- **Digitakt II:** 16 tracks; the stock engine runs 32 voice records (L = even voice, R = odd voice of a track). Your
  `render` is called per playing voice; the stock filter, amp, track FX, sends and master run after it.
- **Digitone II:** 16 voice slots; the ColdFire maps slots to tracks per note, so a voice is not tied to a track. The
  stock chain after the voice is the same code as on the Digitakt II.
- Both: one `render` call per voice per block; the voice ends when `render` returns `DK_DONE`.

## Parameters

- The DSP receives up to 4 parameter words per voice today: the knobs in `param_slots` (`[1, 2, 4, 5]` on the Digitakt
  II, `[0, 1, 2, 3]` on the Digitone II, devices/*.toml). More knobs can be shown and stored, but only these reach
  `params`. See references/params.md.
- Words are 0 .. 0x7f00 (UI 0 .. 127), smoothed on the ColdFire before they are sent; the DSP sees them once per block.

## Memory

| | Digitakt II | Digitone II |
| --- | --- | --- |
| Run-time DSP area (all loaded modules share it) | 512 KiB | 320 KiB |
| Example | pluck reserves 256 KiB of delay lines (16 x 4096 words) | the same fits; little room is left |

Large buffers (delay lines, resonator banks) are the first limit on the Digitone II. Size them per voice and say the total
in the module README.

## Limits of the loader

The runtime refuses a module that does not fit, with its own code (docs/findings/57-capacity-isolation.md). Limits come from `[limits]` of `devices/*.toml`.

| | Digitakt II | Digitone II | Refusal |
| --- | --- | --- | --- |
| modules at once (selection) | 8 | 8 | SELECT of a ninth: `E_SEL_COUNT` |
| module file | 64 KiB | 64 KiB | `E_TOO_BIG` |
| ColdFire memory (image + bss, all modules) | 512 KiB | 512 KiB | `E_NOMEM` |
| DSP area (all modules; 64-byte granules, 60 bytes unusable) | 512 KiB | 320 KiB | `E_DSP_NOSPACE` |
| machine types, voice rows | 8 | 8 | a ninth machine `E_TYPE_RANGE`, a ninth voice `E_DSP_ROWS` |
| parameter rows | 16 | 16 | `E_PARAM_FULL` |
| page rows | none | 8 | `E_PAGE_FULL` |
| effects in the chain (patched in) | 4 | 4 | patchkit refuses at build time |

`bss` is not stored in the file: a large work buffer costs arena or DSP area, not file size. Size per-voice state for the lowest musical note, not for the arithmetic limit.

What is used and what is left: `mise run dk:store -- --emu info` (or `--port NAME`) prints one `capacity` line per resource (runtime 0.2.2 and later): modules, arena and DSP area bytes with the largest free piece, machine, voice, parameter and page rows. `modules` lists each module's share.


## Stock patterns worth copying

- Many stock machines render 64 samples at 96 kHz and decimate to 32 (DT2 sample machines, DN2 FM TONE): it lowers
  aliasing for bright oscillators. Not needed for filtered sources such as pluck.
- Parameters are converted to floats once per block (scale 1/32512, `0x38010204`) and ramped per sample by
  (new - old) / 32 inside a block.
- Shared stages use `reset(state)` and `process(state, in, out, n)` on whole blocks.
