# fx/master-drive

A master-bus soft-clip saturator with one knob, for the Digitakt II (1.17) and the Digitone II (1.12) from one source.
It changes both CPUs.

## What it does

- **DSP.** The block handler's one call of the Q31 converter is retargeted to a routine in free L2
  (`src/dsp/master_drive.s`). The routine processes the final 64 planar floats of every block (32 left, then 32
  right) and tail-jumps to the converter. Per sample: pre-gain `G = 1 + 7 d` (up to +18 dB), a soft clip
  `s(t) = t - t^3/3` with `|t|` limited to 1 (zero slope at the knee), a makeup factor `m = 1 - 0.3 d`, and a
  dry/wet crossfade `out = x (1 - d) + m s(G x) d`. The crossfade makes the effect continuous with bypass at
  `d -> 0`. At `d = 0` the block is not touched, so the audio is **bit-identical to stock**.
- **Click-free.** `d` follows the knob by at most 1/128 per block (85 ms for the full range) and the three
  coefficients ramp linearly across each 32-sample channel.
- **Cost.** 20 DSP instructions per block with no drive and 968 at full drive, measured on the concrete core
  (tests/test_master_drive.py). The block budget is about 330,000 (DN2) to 450,000 (DT2) instructions at the assumed
  clock (finding 06, "Headroom"): 0.3 % at most.
- **ColdFire.** The module renames the knob on the master page. It writes the addresses of two strings (placed in
  `cf.expansion` by `core/expansion`) into the knob's parameter descriptor row: the page label `DRV` and the
  long name `Master Drive` (shown while the knob turns). The default and the range stay as they are.

## The knob and its trade-off

The knob is **encoder 7 of the FUNC+MOD master page** (the 7th of the 8 encoders): the master compressor's
**Sidechain Filter** word (fx[66]). No new parameter exists, so it is stored in the kit, saved with the project and
can be p-locked like any other.

- **Neutral is the stock default, UI 80 (the page shows 16).** Factory kits and every kit saved by stock firmware hold
  `0x5000` there; the descriptor default is not what a new project starts from (the kit is copied from a stored
  preset). So drive is 0 at that value and below, and rises from there to the top (`0x7f00`, shown as 63).
- **When the master compressor is mixed in (its MIX knob above 0), the same knob also moves the compressor's
  sidechain filter.** The two effects are not separable. With the compressor unmixed (the default) the word
  reaches nothing else in the stock firmware (finding 04).
- The knob icon is still the stock filter curve: the icon is chosen by the page layout, not by the descriptor row.

## Claims and anchors

- Claims: `dsp.master.hook` (also claimed by `fx/master-gain`, so the two modules are refused together),
  `frame.fx66`, `cf.param.master_sidechain_filter`.
- Anchors, all resolved by rules in `module.toml` (`tools/port_resolve.py`), none typed: the call and the entry of
  the converter and the master buffer (as `fx/master-gain`), `dsp.fx.sidechain_filter_word` (the address inside the
  stock reader's short-word load: DT2 0x256008, DN2 0x25ced8) and `cf.param.master_sidechain_filter` (the 60-byte
  descriptor row, found by its masked words: mirror index 66, range 0 to 0x7f00, default 0x5000).

## Test

- `tests/test_master_drive.py`: the routine on the concrete SHARC+ core against a float32 reference (140 blocks of
  ramp, release to exact bypass, saved registers, instruction counts).
- `tools/pk_proof_drive.py dt2|dn2`: the whole chain in the emulator (patch, coupled cold boot, knob, PCM).
  Numbers: finding 22.
