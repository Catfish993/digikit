# digikit SDK

The SDK is the stable interface between content modules and the digikit runtime.
It does not depend on the device. Device differences live in the `core/*` adapters
and the device profiles (`devices/*.toml`).

## Layout

| Path | What | Edit |
| --- | --- | --- |
| `sdk.toml` | The contract spec: versions, the `dk_api` table, imports, machine and effect layouts, log kinds, protocol numbers, limits, settings. | yes |
| `include/dk/` | Public C headers. Modules include these: `<dk/api.h>`, `<dk/fx.h>`, `<dk/machine.h>`, and `<dk/page.h>` (`DK_PAGE` and `DK_KNOB`, shared by the other two). | yes |
| `include/dk/gen/` | C layouts and constants generated from `sdk.toml`. | no |
| `asm/` | SHARC+ includes generated from `sdk.toml`: `dk_fx.inc`, `dk_machine.inc`. | no |
| `dsp/` | SHARC+ DSP kernels (macros and block routines). See [dsp/README.md](dsp/README.md). | yes |
| `templates/` | Module templates for `mise run module:new`. | yes |
| `vectors/` | Test vectors shared by the Rust, Python and web tests. | no |

`patchkit gen` writes every generated file. `patchkit gen --check` fails when one is stale
(`mise run gen:modules -- --check`).

## Include paths

The build passes these search paths, in this order:

- ColdFire C and assembly: the module's `build/gen/`, the module's `src/`, `modules/core/fw/inc`, then `sdk/include`.
- SHARC+ assembly: the module's `build/gen/`, the module's `src/`, `sdk/asm`, `sdk` (for `dsp/*.inc`), then the directories of the module's sources.

A header in the module's `src/` is shared by C and SHARC+ sources. Use only decimal `#define`s and `//` comments on their own line: the SHARC+ assembler keeps a trailing comment in the macro text.

## Declaring an effect

An effect declares itself, its parameters and its page in a C file of the module, with `DK_FX`, `DK_FX_PARAM` and `DK_PAGE`
from `<dk/fx.h>`. This is `modules/fx/freqshift/src/module.c`:

```c
#include <dk/fx.h>
#include "freqshift.h"

DK_FX(fshift, .process = "_fs_process", .desc = "_fs_desc",
      .mix = 127, .route = DK_ROUTE_SEND, .routes = DK_ROUTE_SEND | DK_ROUTE_MASTER);
DK_FX_PARAM(fshift, FSHIFT_SHIFT_WORD, "Freq Shift", "SHIFT", DK_BIPOLAR, -64, 63, 0);
DK_PAGE(fshift, "FSH", "Freq Shifter", DK_KNOB_AT(FSHIFT_SHIFT_WORD, 0));
```

List the file as an `[[object]]` in `module.toml`. `patchkit pack` reads the records; the device never sees them.
`patchkit desc MODULE_ID` prints them. The fields and rules are in
[docs/MODULE-AUTHORING.md](../docs/MODULE-AUTHORING.md), section 5.4.

## Declaring a machine

A machine is declared the same way, with `DK_MACHINE`, `DK_PARAM` and `DK_PAGE` from `<dk/machine.h>`.
This is `modules/machines/pluck/src/module.c`:

```c
#include <dk/machine.h>

DK_MACHINE(pluck, .name = "Pluck", .short_name = "PLK", .render = "_pk_render",
           .note_on = "_pk_note_on", .desc = "_pk_desc");
DK_PARAM(pluck, 0, pluck_decay, "Decay", "DECAY", 0, 127, 64);
DK_PARAM(pluck, 1, pluck_pick, "Pick position", "PICK", 0, 127, 32);
DK_PARAM(pluck, 2, pluck_bright, "Brightness", "BRIGHT", 0, 127, 80);
DK_PARAM(pluck, 3, pluck_color, "Excitation colour", "COLOR", 0, 127, 40);
DK_PAGE(pluck, "PLUCK", "Pluck", DK_KNOB(0), DK_KNOB(1), DK_KNOB(2), DK_KNOB(3));
```

Parameter values are knob units 0 to 127. The stored word is the value times 256. The DSP symbols are required: a machine has no data-only form.
A machine has at most 4 parameters, in word order, and one page with `DK_KNOB` only. The adapter places word w on a fixed knob.
The rules are in [docs/MODULE-AUTHORING.md](../docs/MODULE-AUTHORING.md), sections 5.2 and 5.3.

## Writing modules

See [docs/MODULE-AUTHORING.md](../docs/MODULE-AUTHORING.md). The plan for the SDK API is
[docs/plans/sdk-api.md](../docs/plans/sdk-api.md).
