# SDK DSP kernel library

Small SHARC+ kernels for module authors. A module includes the file it needs with `#include "dsp/<name>.inc"`; there is
no shared copy at run time, the text is assembled into each module. The module build finds `sdk/` in its include path.

Two forms:

- Step macros (this directory, now): one sample of a kernel, for a module's own block loop.
- Block routines (next): whole-block routines on the stock C convention.

Style: [docs/sharc/style.md](../../docs/sharc/style.md), in particular section 5a (rules from the manuals).

## Register rule for macros

Every macro names all registers it uses as arguments: inputs, outputs, temporaries and constants. The caller allocates
the registers and can see which ones the macro writes. A macro touches no other register, flag-dependent state or pointer.
Constants (0.5, 1.5, 2.0, 1/32) are passed in registers that the caller loads before the loop. The caller writes the
`;` after a macro call. A macro is several instructions separated by `;`, so a loop-end label must never be on the
first line of a macro expansion: put the label on a trailing instruction (usually the store).

Rules that apply to the loop around a macro (style.md 5a):

- A block loop has 4 or more instructions.
- Do not use a DAG register within 4 instructions of loading it.
- A multiplier input that comes from the previous instruction costs stalls (2 if both inputs, 1 if one).
- Keep data in DM.
- The stall costs are not measured in the emulator; it counts instructions.

## Macros

| File | Macro | Instructions per sample | Notes |
| --- | --- | --- | --- |
| `gain.inc` | `DK_GAIN_STEP(OUT, IN1, GN)` | 1 | y = x g |
| `gain.inc` | `DK_MIX_STEP(OUT, IA, IB, MIXV, TMP)` | 3 | y = a + m (b - a) |
| `ramp.inc` | `DK_RAMP_SETUP(STEP, CUR, TGT, INVN)` | 2 per block | step = (target - cur) / 32 |
| `ramp.inc` | `DK_RAMP_STEP(CUR, STEP)` | 1 | cur = cur + step |
| `onepole.inc` | `DK_ONEPOLE_STEP(OUT, IN1, COEF, TMP)` | 3 | y = y + a (x - y) |
| `sat.inc` | `DK_SAT_STEP(OUT, IN1, ONE, HALF, K15, TMP)` | 5 | cubic soft clip, \|y\| <= 1 |
| `recip.inc` | `DK_RECIP(OUT, IN1, TWO, TMP)` | 7 | 1 / x, 2 Newton steps |
| `recip.inc` | `DK_DIV(QUO, NUM, DEN, TWO, TMP, TMP2)` | 8 | n / d |
| `recip.inc` | `DK_RSQRT(OUT, IN1, HALF, K15, TMP, TMP2)` | 16 | 1 / sqrt(x), 3 Newton steps |

Each file has a header with the maths, the arguments, the instruction count and the accuracy. The float32 reference
models are in `tools/patchkit/dsp_ref.py`; the tests are in `tests/patchkit/test_dsp_kernels.py`.

## Speed

Sequential macros do not pack stages into multifunction instructions (a multiply and an add in one instruction, or a
load with a compute). The counts above are the sequential counts. For peak speed a module interleaves the stages by
hand; the block routines (next commit) do this inside.
