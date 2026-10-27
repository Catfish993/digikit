# __TITLE__

__ID__: a SHARC+ voice machine for the Digitakt II and the Digitone II, loaded at run time. It plays a pitched sine with one
level parameter. Replace the render loop in `src/dsp/__NAME__.s` with your own voice.

- `module.toml`: the module, one `[[voice]]`, one `[[machine]]`, its `[[param]]` and the Digitone II page.
- `src/dsp/__NAME__.s`: `render`, `note_on` and the descriptor (`sdk/dk_machine.h`).

```sh
mise run module:check -- __ID__    # build and conformance check on both devices
mise run module:pack -- __ID__     # make the .dkm
mise run dk:run                    # run in the emulator
```
