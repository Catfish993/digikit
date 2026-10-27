# digikit SDK

The SDK is the stable interface between content modules and the digikit runtime.
It does not depend on the device. Device differences live in the `core/*` adapters
and the device profiles (`devices/*.toml`).

## Layout

| Path | What | Edit |
| --- | --- | --- |
| `sdk.toml` | The contract spec: versions, the `dk_api` table, imports, machine and effect layouts, log kinds, protocol numbers, limits, settings. | yes |
| `include/dk/` | Public C headers. Modules include these: `<dk/api.h>`, `<dk/fx.h>`, `<dk/machine.h>`. | yes |
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

An effect declares its page and knobs in a C file of the module, with `DK_FX` and `DK_KNOB` from `<dk/fx.h>`.
This is `modules/fx/freqshift/src/module.c`:

```c
#include <dk/fx.h>
#include "freqshift.h"

DK_FX(fshift, .process = "_fs_process", .desc = "_fs_desc",
      .tab = "FSH", .title = "Freq Shifter", .mix = 127,
      .route = DK_ROUTE_SEND, .routes = DK_ROUTE_SEND | DK_ROUTE_MASTER,
      .knob = {DK_KNOB(FSHIFT_SHIFT_WORD, "Freq Shift", "SHIFT", DK_BIPOLAR, -64, 63, 0)});
```

List the file as an `[[object]]` in `module.toml`. `patchkit pack` reads the record; the device never sees it.
`patchkit desc MODULE_ID` prints it. The fields and rules are in
[docs/MODULE-AUTHORING.md](../docs/MODULE-AUTHORING.md), section 5.4.

## Writing modules

See [docs/MODULE-AUTHORING.md](../docs/MODULE-AUTHORING.md). The plan for the SDK API is
[docs/plans/sdk-api.md](../docs/plans/sdk-api.md).
