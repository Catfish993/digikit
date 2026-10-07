# 17. Emulator control protocol and firmware injection

Design: `docs/design/emulator-control.md`. Code: `native/control`,
`native/boot/src/dsp_stream.rs`, `tools/digi_client.py`, `tools/dsp_patch.py syx`.
Marks as in CLAUDE.md. Nothing here was re-checked by a second agent yet, so
the numbers are **[D]** (measured here, one machine, other lanes running).

## Hot DSP injection (DN2 1.11, ready inputs of 2026-10-06, fast regions on)

Scenario: `trig1-tap-release` events, 3 s of audio, DSP output hashed in the
controller (`pcm.tap`). Patch: `tools/dsp_patches/dn2-1.11-master-half.patch`
(`R2 = 0x1f` -> `0x1e` at 0x1c9d51, master Q31 scale, -6 dB), written into a
.syx with `tools/dsp_patch.py syx` (one changed byte in section 7).

| run | rms | peak | PCM sha256 (prefix) | wall |
|---|---|---|---|---|
| reference (fresh restore) | 0.2096 | 0.6930 | `a0a0f42625dcb87e` | 5.0 to 6.5 s |
| same again after `restart` | 0.2096 | 0.6930 | `a0a0f42625dcb87e` | |
| patched .syx injected before the run | 0.1048 | 0.3465 | `6a7f5cecc41c9385` | 6.4 s |
| patched, then original injected, before the run | 0.2096 | 0.6930 | `a0a0f42625dcb87e` | |
| patched injected at frame 826 (0.55 s), mid-note | | | | window RMS 0.613 -> 0.489 -> 0.303 |

- The patched numbers equal the `--dsp-patch` start-time run of lane A
  (`6a7f5cec..`, rms 0.1048), so the live injection and the start-time overlay
  produce the same PCM. **[D]**
- Injecting the original .syx restores the reference PCM exactly (SHA-256
  equal). **[D]**
- `firmware.load` of the patched .syx takes 0.13 s (parse, compare 7 sections,
  diff the two DSP streams, apply between two frames). **[D]**
- The DSP keeps running at the same speed: the patch bars only the generated
  blocks and fast regions that touch the changed range (lane B), so 3 s of
  audio still render in about 5 s. Before lane B the same patch took 164 s for
  3 s with the fast tier off. **[D]**

## Patched MAIN (one byte of a string in section 3 of DN2 1.11)

- Cold CF boot to `ready` through `Emulator::new_patched`: 4.4 s wall,
  836,986,444 instructions. The cached snapshot loads in 0.13 s and 1M
  instructions later its state digest equals the cold run's (`8644d8a1e51f`).
  Injecting the stock .syx back restores the audio snapshot in 0.22 s. **[D]**
- A patched MAIN runs without DSP audio (no DSP state for it). **[O]**

## Protocol checks (CF-only DN2 1.11, `tests/test_digi_client.py`)

- `step(N)` advances the ColdFire count by exactly N for N = 1, 7, 250000,
  250001 and 600000. An input with `at_instr` is recorded at exactly that
  count. Replaying `inputs.recorded` after `restart` gives the same state
  digest. **[D]**
- A render of `trig1-tap-release` (8.5 s) through the socket gives PCM
  `aa1dbd9e40e53443..`, the pinned `expect_sha256` of the scenario, and a second
  render after `restart` is identical. **[D]**
- `coupled_ready_exactness` fails in this merged tree: CF digest `75b6a52d..`
  (pinned `ab27c5c9..`) and PCM `849157bd..` (pinned `3b600f6d..`). The tree
  carries lane 0's uncommitted DSPI1 model (`board.rs` ch 14 completion, IRQ
  delivery in `runtime.rs`), which changes ColdFire state the gate hashes; the
  controller move touches no machine code and the scenario above still matches
  its pin. Not bisected against a tree without lane 0's change. **[O]**
