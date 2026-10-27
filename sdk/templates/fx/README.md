# __TITLE__

__ID__: a SHARC+ effect for the Digitakt II and the Digitone II, loaded at run time. It scales the master block by one
GAIN knob. Replace the loop in `src/dsp/__NAME__.s` with your own processing.

- `module.toml`: the module, one `[[fx]]` and its `[[fx.knob]]`.
- `src/dsp/__NAME__.s`: the process routine and the descriptor (`sdk/dk_fx.h`). It reads the knob word and the knob count
  from `build/gen/module.inc`, which `patchkit gen` writes.

```sh
mise run module:check -- __ID__    # build and conformance check on both devices
mise run module:pack -- __ID__ --device dt2     # make the .dkm (or --device dn2)
mise run dk:run -- __ID__ --emu --seconds 10    # run in the running emulator (mise run workbench)
```
