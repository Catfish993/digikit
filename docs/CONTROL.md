# Control socket

Drive a running emulator from scripts: load a patched image, step the guest, press keys, read the screen. This page
lists what is implemented. The design, including parts that are not built, is in
[design/emulator-control.md](design/emulator-control.md).

## Transport

| Item | Value |
|---|---|
| Protocol | JSON-RPC 2.0, one JSON object per line (newline-delimited) |
| Socket | Unix socket `<run_dir>/<pid>.sock` (Unix only) |
| Discovery | `<run_dir>/current.json`, key `socket` |
| Default run dir | `out/native/run` (mode 0700); `--run-dir DIR` overrides it |
| Events | Pushed on a connection after `events.subscribe`, as notifications `{"method": "event", "params": ...}` |

## Who serves it

| Server | Start | Notes |
|---|---|---|
| Desktop app | `mise run emu` | On by default. `--no-control` turns it off. Prints `control socket: <path>` to stderr. |
| Daemon `digi-emud` | `mise run emu:serve` | No window. Starts paused: a client steps it or sets `run.mode free`. `--free` runs it live. |

Coupled DSP audio in the daemon needs `--auto-coupled` and the ready inputs; see [PATCHING.md](PATCHING.md).
Startup flags are shared by both servers (`packages/emulator/control/src/cli.rs`): `--control`, `--no-control`, `--paused`,
`--run-dir DIR`, `--card-image FILE`, `--scenario NAME_OR_FILE`, `--report PATH`, `--no-audio` and the coupled-audio
flags. `--scenario` needs a coupled-audio build.

## Command line

`mise run emu:ctl -- <subcommand>` runs `uv run python -m tools.control.digi_client <subcommand>`.

| Subcommand | Does |
|---|---|
| `status` | Print the `status` result |
| `load SYX` / `inject SYX` | `firmware.load`; options `--base-sha256`, `--start auto\|snapshot\|cold`, `--dsp-patch` (`load` also `--card`) |
| `restart` | Back to the ready state of the running firmware |
| `step N` | Run N guest instructions |
| `tap CODE [--hold N] [--at-instr N]` | Press and release a key (default hold 40000000 instructions) |
| `frame out.png` | Save the OLED as a PNG |
| `call METHOD k=v ...` | Any method; each value is parsed as JSON, else kept as text |
| `events [kinds...]` | Print events as JSON lines until interrupted |
| `quit` | Stop the server |

Global options: `--socket PATH` (default: discover), `--run-dir DIR` (where to look for `current.json`).

`mise run inject -- OUT.syx [--hot]` loads a patched image through `tools/patchkit/patchkit.py inject`.

## Methods

Wrapper is the `Emu` method in `tools/control/digi_client.py`. `hello` reports an incomplete method list (it omits
`mem.cf.peek`, `mem.cf.poke`, `card.*` and `watchdog.config`); use this table.

| Method | Wrapper | CLI |
|---|---|---|
| `hello` | `connect` | (automatic) |
| `status` | `status` | `status` |
| `run.mode` (`ui`, `paused`, `free`) | | `call` |
| `step` | `step` | `step` |
| `run_until` | `run_until` | `call` |
| `firmware.load` | `load`, `inject` | `load`, `inject` |
| `restart` | `restart` | `restart` |
| `input.button`, `input.turn` | `button`, `turn` | `call` |
| `input.tap` | `tap` | `tap` |
| `input.schedule` | `schedule` | `call` |
| `inputs.recorded` | `recorded` | `call` |
| `framebuffer.get` | `frame`, `frame_png`, `frame_hash` | `frame` |
| `leds.get` | `leds` | `call` |
| `mem.cf.peek`, `mem.cf.poke` | `peek32`, `poke32` | `call` |
| `card.overlay`, `card.load`, `card.status`, `card.cancel` | | `call` |
| `watchdog.config` | `watchdog` | `call` |
| `diagnostics` | | `call` |
| `audio.report` | `audio` | `call` |
| `state.digest` | `state_digest` | `call` |
| `pcm.tap` (`start`, `report`, `capture`, `capture_stop`) | `pcm_start`, `pcm_report` | `call` |
| `cf.profile` | | `call`; only with the `cf-profile` feature |
| `dsp.peek` | `dsp_peek` | `call` |
| `dsp.patch` | `patch` | `call` |
| `guard.set`, `guard.report`, `guard.clear` | | `call` |
| `events.subscribe` | `events`, `subscribe`, `drain_events` | `events` |
| `quit` | `quit` | `quit` |

`framebuffer.get {hash?, data?}`: `hash: true` adds `sha256`, the hex SHA-256 of the 1024 decoded OLED bytes
(the bytes `data_b64` carries; the hash the patchkit smoke goldens use); `data: false` omits `data_b64`.

`leds.get` returns `{led_revision, leds, rgb}`: `leds` is 128 hex digits (64 LED values), `rgb` 384 hex digits
(64 x r, g, b) or null until any is set. It reads the emulator's current state and does not consume it from the UI.

`--dsp zero --cf-snapshot FILE` (on `digi-emud` and the app) starts without a DSP: SSI paced, the DSPI2 peer
answers zeros, the ColdFire state restored from the snapshot (about 2.2x real time). `status.dsp` is `"zero"`,
`"sharc"` or `"none"`. `run_until {frames|audio_s}` is refused (no DSP session); `elapsed`, `icount` and `step`
work. No PCM is produced.

`guard.set {protect: [{lo, hi, label}], allow_pc: [{lo, hi}]}` arms write-range guards (addresses are integers or
`"0x..."` strings, `hi` exclusive; an empty `protect` disarms). A ColdFire store (8, 16 or 32 bit) that overlaps a
protected range while the PC is outside every `allow_pc` range is a hit. `guard.report` returns `{total, hits}`
(the first 64 hits: `pc`, `addr`, `size`, `label`, `icount`); `guard.clear` forgets the hits and keeps the guards.
`status.cf.guard_hits` is the total. The `guard` event is sent once per protected range on its first hit (`label`,
`pc`, `addr`, `size`, `hit_icount`, `total`). The run is never stopped. Armed guards turn off the fused-loop and
reset-clear shortcuts. Not checked: eDMA and other peripheral writes, and host writes (`mem.cf.poke`, card load,
`dsp.patch`). `coupled-run --guard FILE.json` takes the same object and adds `guard: {total, hits}` and the gate
`guard_ok` to the scenario report.

`pcm.tap` needs the coupled-audio build and a DSP session. `capture` and `capture_stop` have no wrapper.

## Events

`halt`, `stall.end`, `underrun`, `session.changed`, `scenario.done`, `firmware.loaded` (`ok`, `seconds`), `mode`,
`guard`, `card.started`, `card.ready`, `card.error`.

## Error codes

| Code | Name | Code | Name |
|---|---|---|---|
| -32700 | parse error | 4 | firmware rejected |
| -32600 | invalid request | 5 | audio unavailable |
| -32601 | method not found | 6 | guest fault |
| -32602 | bad params | 7 | DSP halted |
| 1 | no firmware | 8 | I/O |
| 2 | stale session | 9 | timeout |
| 3 | busy | | |

## Examples

```sh
mise run emu:serve                            # headless, paused
mise run emu:ctl status
mise run inject -- out/patch/mg.syx           # cold or snapshot start, as needed
mise run inject -- out/patch/mg.syx --hot     # never cold boots; fails if no saved state fits
mise run emu:ctl step 1000000
mise run emu:ctl frame out.png
mise run emu:ctl tap 25 --hold 1000000
mise run emu:ctl events halt underrun
mise run emu:ctl quit
```

Python (`tools/` on `sys.path`):

```python
from digi_client import Emu

emu = Emu.connect()              # or Emu.connect(run_dir="...")
print(emu.status())
emu.step(1_000_000)
print(emu.state_digest())
emu.close()
```

`Emu.spawn(*args)` starts a private daemon (binary from `DIGI_EMUD`) and quits it on exit;
`Emu.spawn_coupled(syx, ready_dir)` adds the coupled-audio flags.

## Not implemented

The design document lists these; no server method or client wrapper exists.

| Design item | Use instead |
|---|---|
| `scenario.run` | Startup flag `--scenario NAME_OR_FILE [--report PATH]`; the `scenario.done` event carries the report |
| replay | `inputs.recorded` then `input.schedule` |
| save / restore, counters | none |
| `cf_read` | `mem.cf.peek` |
| `capture.*` wrappers | `pcm.tap` with `action=capture` through `call` |
