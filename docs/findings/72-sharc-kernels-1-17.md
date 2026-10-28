# SHARC stock kernels on DT2 1.17 and DN2 1.12

Static study (instruction counts from listings, no stalls modelled; nothing [V]). It describes idioms and interfaces
only; no firmware code is reproduced. Style guide: [docs/sharc/style.md](../sharc/style.md). Plan:
[firmware-decls-and-sdk.md](../plans/firmware-decls-and-sdk.md), step 7.

Status: draft for step 7 of `docs/plans/firmware-decls-and-sdk.md`. Read-only study, 2026-10-27.
Marks: **[D]** read or counted once in the listings (sharcdb, DT2 1.17 and DN2 1.12). **[O]** estimate or guess.
No claim here is **[V]**. All addresses are short words (sw). No firmware code is copied. Idioms are described in
my own words. Manual: SHARC+ Core Programming Reference Rev 1.5 (`out/refs/sc58x-2158x-prm/`, or the Rev 1.4
`out/refs/sharc-plus-prm/`). I cite chapter and section names. The page numbers in the bookmark file are PDF pages.

## 0. Common shape of the stock kernels [D]

- Call: delayed `CALL` with a frame (finding 53). Arguments R4, R8, R12; more arguments on the stack
  (the biquad's sample count `n` is the fourth argument, read from the stack frame). Result in R0.
- Most kernels are `reset(R4 = record)` and `process(R4 = record, R8 = in, R12 = out, n)`. One 32-sample block per call.
- The prologue and epilogue (frame, saves of I3, I5, R6, R7, R11, R13-R15) cost about 20 instructions per call. This is
  the call overhead to compare with 32 samples (about 0.6 instruction per sample).
- Every loop is a hardware `DO ... UNTIL LCE`. There is no software loop test in the sample loops.
  Manual: Program Sequencer, "Loop Sequencer", "Counter-Based F1-Active Loop".
- Most kernels have a fast exit for `n` = 0 or 1 and a tail after the loop. The loop is software pipelined, so the
  last results are stored after the loop. Manual: Processing Elements, "Software Pipelining for Multifunction Instructions".
- The kernels hide a lot of state in absolute addresses (tables). That matters for the "call the stock one" option (section 3).

## 1. Building blocks

### 1.1 Block biquad, DT2 `0x1ccbd8` (reset `0x1ccbca`), DN2 `0xb81368` (reset `0xb8135a`) [D]

- Computes: a second-order IIR section over one block of floats, in place or to another buffer.
- Interface: R4 = 8-word record, R8 = in, R12 = out, `n` on the stack. Coefficients in record words 2 to 5
  (four values), state in words 6 and 7 (two floats). Leaf function. Result not used.
- Form: four coefficients and two state words, and five multiplies per sample, so it is not the plain
  five-coefficient direct form. Which form it is (state-variable style, or b0 folded) is **[O]**.
- Idioms:
  - Loop is a software-pipelined `DO` over `n - 2` (R13). Multiplier and ALU run in the same instruction
    (`Fa = Fx * Fy, Fb = Fc - Fd`). A load of the next input sample sits in the same instruction. Store of the
    previous output sits in the loop-end instruction.
  - Coefficients live in registers. State is loaded once and written back once per block.
  - Short blocks (n = 1, 2) take separate tails (`JUMP IF EQ`), so the pipeline never runs with a negative count.
- Cost: loop body is 5 instructions per sample. 59 instructions in total. A block of 32 is about 5 x 30 + 40 = 190
  cycles **[D]** for the count, **[O]** for the cycle figure (stalls not measured).
- Limit: one multiplier gives 5 cycles per sample for 5 multiplies. SIMD (two channels, one per processing element)
  would give 2.5 per sample for a stereo pair **[O]**.
- Manual: Processing Elements, "Multifunction Computations" and "Multifunction Input Operand Constraints"
  (the operand register groups limit which registers can be used in a multifunction instruction);
  "Software Pipelining for Multifunction Instructions"; "SIMD Mode".

### 1.2 Saturator (overdrive), DT2 `0x1cb3d8`, DN2 `0x1cdb56` [D]

- Computes: a ramped drive gain, a soft clip, a ramped output gain. The clip is a short polynomial: clip to the
  range -1..+1 with `CLIP`, then a few steps of "multiply by (1 - |x|)" style shaping, then a subtract. Exact
  curve **[O]**.
- Interface: R4 = record (about 14 words: parameters, previous and new gains), R8 = in, R12 = out, `n` from stack.
  It calls the divide `0x1c06ba` once per block (to scale a gain). It converts an integer parameter to float first.
- Idioms:
  - Per-sample parameter ramps: two accumulators (`F12 = F11 + F12`, `F15 = F8 + F15`) add a fixed step per
    sample. The step is (new - old) / n, computed once before the loop. This is the "ramp" of finding 53.
  - `FCLIP`, `FABS`, `FMIN`, `FMAX` give clipping with no branches. Manual: ALU Floating-Point Computations
    (`Fn = CLIP Fx BY Fy`, `ABS`, `MIN`, `MAX`).
- Cost: loop body about 14 instructions per sample, with two ramps and about 3 shaping steps **[D]**.
  A hand-made version with one ramp should reach 5 to 6 **[O]**.

### 1.3 Envelope generator, DT2 `0x1cbb57`, DN2 `0xb80345` [D]

- Computes: a multi-stage envelope (11-word record) that steps through segments with rates read from a table.
  The DT2 rate table is at `0x26ad30` (finding 53). The DN2 listing loads `0x26a570`. The code is reloc-hash equal,
  not byte-equal, because table addresses differ **[D]**.
- Interface: R4 = record, R8 = in (gate or input block), R12 = out, count in R15. It produces a block of gain values
  and handles a segment change inside a block by splitting the loop (the loop runs to the segment end, then the
  outer code reloads the rate). The inner loop is about 6 instructions per sample, including the exit test **[D]**.
- Idioms: nested control (outer segment loop, inner `DO` per segment), table lookup with a DAG register used as
  a base (`DM(I3, M4)` where M4 is the stage index), exit by a conditional jump out of the loop on threshold crossing.
  Manual: Program Sequencer, "Loop Abort" and "Restrictions on Ending Loops" (a conditional jump out of a loop).
- Cost: about 6 per sample plus a segment change about 30 **[O]**.

### 1.4 Block copy and fill, DT2 `0x1cdd8b`, DN2 `0xb8251b` [D]

- Computes: copy of `n` words. Odd `n` gets one scalar copy. A fill mode exists only if the first word read (a flag or count compared with zero) selects it **[O]**.
- Interface: R4 = pointer to a control word that is read and compared first, R8 = source, R12 = destination, `n` from the frame **[O]**. Check the argument order in the listing before use.
- Idioms: SIMD copy. It sets the SIMD bit (PEYEN, MODE1 bit 21) around a `DO` loop of `n/2` iterations and
  clears it afterwards. Each loop iteration is two instructions (load pair, store pair) with post-modify by 2.
  One instruction moves two words because the S register (complementary data register) takes the second word.
  Manual: Processing Elements, "SIMD Mode"; Data Address Generators, "SIMD Mode", "DAG Transfers in SIMD Mode".
- Cost: 1 instruction per word, 55 instructions in total **[D]**. DM-to-DM copy is bound by one DM access per
  instruction; a dual-bus form (one read on DM, one write on PM) could reach 0.5 per word **[O]**.
- Note: the SIMD bit must be cleared before the kernel returns. The module contract says MODE1 is preserved.

### 1.5 A filter type, DT2 `0x1cdbb2`, DN2 `0xb82342` [D]

- 115 instructions, a record of more than 45 words (offsets up to +42 are read). A block loop of unrolled state
  updates with many `FADD`/`FMUL` pairs. Looks like a ladder or multi-pole filter with coefficient interpolation
  per sample **[O]**. Four other filter types sit at `0x1cd286`, `0x1cc79e`, `0x1cbf07`, `0x1ccd96` (DN2 `0xb81a16`,
  `0xb80f2e`, `0xb806f5`, `0xb81526`).
- The role of each filter type is still open (finding 53). I do not recommend copying them before a reading of
  each one with the parameter table.

### 1.6 Float divide, `0x1c06ba` (same on DN2: 0x1c06ba) [D]

- Computes R4 / R8 -> F0. Not a framed call: plain `CALL`, 47 instructions.
- Idiom: the hardware seed `RECIPS` gives a first estimate, then three Newton-Raphson steps. Each step is a pair:
  `e = 2 - d * r` and `r = r * e`, written as multiplier plus ALU in one instruction (`F2 = F4 * F2, F1 = F8 - F12`).
  The numerator is multiplied in parallel (the quotient is refined in step with the reciprocal). A final correction
  step then rounds. Range guard: if the seed overflows (SV flag), the operands are rescaled with `SCALB`
  and the seed is taken again. The routine turns on RND32 in MODE1 (bit 16) for the last step and restores MODE1.
- Cost: about 25 instructions on the normal path **[D]**.
- Manual: Processing Elements, "Reciprocal/Square Root Instructions" (RECIPS and RSQRTS start the iteration) and
  "Divide Instruction"; ALU floating-point `SCALB`, `COPYSIGN`, `MANT`; "Rounding Mode".

### 1.7 Reciprocal square root, DT2 `0x1c127c`, DN2 `0x1c1226` [D]

- `RSQRTS` seed, then Newton steps: `r = r * (3 - x r^2) / 2`. The "/ 2" is done by `FAVG` (average) with the constant
  3.0: `FAVG(3.0, -x r^2)` is `(3 - x r^2) / 2`. One instruction does the subtract and the halving. 23 instructions
  plus a shared tail (90) **[D]**. Result `x * rsqrt(x)` gives sqrt. The routine saves and restores MODE1.
- Manual: ALU floating-point `Fn = (Fx + Fy) / 2` (FAVG); RSQRTS section as in 1.6.

### 1.8 powf, DT2 `0x1c0d68`, DN2 `0x1c0c76` [D]

- 127 instructions, reads three tables and writes error words (finding 53). Many callers (16). Not pure.
- The SDK already has `_dv_exp2` in `core/dsp-voice` (pluck imports it). For pitch and time constants exp2 is enough.

### 1.9 Delay with interpolated read, DT2 `0x1cbf07` [D]

- Computes: a modulated delay line read with four taps (cubic or 4-point interpolation) and a feedback write.
  Ring of 8192 words (mask `0x1fff`). It calls the divide once (block setup).
- Idioms:
  - Index arithmetic with bit-field extract: `FEXT` takes the low 13 bits, so the ring wraps with no `B`/`L`
    registers. Finding 53 notes the same for the FX units (the stock units never write B or L).
  - Four reads from one base register, with the four offsets placed in M registers (M1 to M4) before the loads.
  - Per-sample ramps for the delay time (`F14 = F14 + F6`) and gain.
- Cost: about 41 instructions per sample in the loop body **[D]**. The address work (mask, four M loads) is more than
  half of it. With hardware circular buffers (L and B set, post-modify) it would be about 15 **[O]**.
- Manual: Data Address Generators, "Circular Buffering Mode" and "Circular Buffer Programming Model";
  Processing Elements, "Bit Field Manipulation Instructions" (FEXT).

### 1.10 2x oversampler decimator, DN2 `0xb826c8` (FM TONE path; DT2 sample machines use `0xb80000`) [D]

- Computes: 64 input samples at 96 kHz -> 32 output samples at 48 kHz through a half-band FIR.
  The loop runs 32 times and takes two input samples per iteration.
- Idioms: coefficients read once from four constants (`0x26edd0` to `0x26eddc`) into registers. The delay line is
  12 words in the record and shifts by explicit moves: a read and a write of each word every iteration. A 2-bit
  `FEXT` index selects which of 4 words to overwrite (small ring).
- Cost: about 25 instructions per output sample, 111 in total **[D]**. Much of the loop is the move of the delay
  line (many loads and stores of the 12-word history). A circular buffer with post-modify cuts the moves away and
  the loop to about 10 instructions **[O]**. A symmetric half-band needs only half the multiplies (the odd
  coefficients are zero).
- Manual: DAG chapter, "Circular Buffering Mode"; Processing Elements, "Multifunction Computations".

### 1.11 Other shared items seen

- `0xb82a5b` (DN2): a bank of 8 lines with per-line index masks and nested loops, about 25 instructions per line-sample
  (8 lines x block). The old label says "master dynamics"; my reading is a feedback delay network or comb bank **[O]**.
  Do not treat it as a compressor without a second check.
- `0xb82bd6` (DN2): a delay-line step with masked index, about 10 instructions per sample **[D]**.
- DT2 `0x1c74a1`: master float buffer to q31 converter (anchor `dsp.master.q31_converter`), 19 instructions with one DO.

## 2. Idioms in one place (for the style guide and for reviews)

| Idiom | Where seen | Manual |
| --- | --- | --- |
| Hardware `DO ... UNTIL LCE` with count in a register | all sample loops | Program Sequencer, Loop Sequencer |
| Multiplier + ALU in one instruction | biquad, divide, saturator | Processing Elements, Multifunction Computations |
| Compute + data move in one instruction | biquad (load next), envelope | Instruction Set Reference, Group I |
| Software pipelining (stores after the loop) | biquad, saturator | Multifunction: Software Pipelining |
| Post-modify DAG access `DM(Ia, Mb)` with constant M6 = 1, M5 = 0 | everywhere | DAG chapter, Post-Modify |
| RECIPS / RSQRTS seed + Newton steps | divide, rsqrt | Processing Elements, Reciprocal/Square Root |
| FAVG to halve in the Newton step | rsqrt | ALU Floating-Point Computations |
| CLIP / ABS / MIN / MAX, no branch | saturator, delay | ALU Floating-Point Computations |
| FEXT masks and wraps an index | delay, decimator | Bit Field Manipulation |
| SIMD (PEYEN) around a copy loop | copy/fill | Processing Elements, SIMD Mode |
| Parameter ramp: accumulator += step per sample | saturator, delay | n/a (technique) |
| MODE1 saved and restored around RND32 or SIMD | divide, rsqrt, copy | Processing Elements, Rounding Mode |

## 3. SDK kernel candidates

Cycle targets are instruction counts for one block of 32 samples with the call overhead included, **[O]**.
Interface proposal: C-callable view with the stock register contract (R4, R8, R12, optional stack argument; result R0).
All functions are leaf and keep the module register contract (see `style.md`, section 5). Names use a `dk_` prefix.

| Kernel | C proposal | Register contract | Cycles target (32 samples) | Own or stock |
| --- | --- | --- | --- | --- |
| Gain / ramp / mix | `void dk_ramp_gain(const float *in, float *out, float g0, float g1)` | R4 = in, R8 = out, F12 = g0, stack = g1 (or R12 = pointer to {g0, g1}) | 40 (1 per sample + 8) | own |
| Biquad (5 coefficient) | `void dk_biquad(dk_biquad_state *s, const float *in, float *out)` | R4 = state (coef 5 + z 2), R8 = in, R12 = out, n fixed 32 | 5 x 32 + 25 = 185; stereo SIMD pair 110 | own (stock record form differs and n is variable) |
| Delay line, interpolated read | `void dk_delay(dk_delay_state *s, const float *in, float *out, const float *time)` | R4 = state with B/L base and write index; R8 = in; R12 = out; time ramp in the state | 15 x 32 + 40 = 520 | own (stock is 41 per sample and fixed to 8192) |
| Soft saturator | `void dk_sat(dk_sat_state *s, const float *in, float *out)` | R4 = state (drive, level, ramps), R8, R12 | 6 x 32 + 30 = 220 | own; stock only if the exact stock curve is wanted |
| Reciprocal | `float dk_recip(float x)` | F4 = x -> F0 (2 Newton steps, about 10 instructions) | 10 per call | own (inline macro) |
| Divide | `float dk_div(float a, float b)` | R4 = a, R8 = b -> F0 | 25 per call | stock (plain `CALL` to 0x1c06ba; same address on both) or own macro |
| Reciprocal square root | `float dk_rsqrt(float x)` | F4 -> F0 | 12 per call | own macro; stock 0x1c127c / 0x1c1226 differ per device |
| exp2 / pitch | `float dk_exp2(float x)` | F0 -> F0 | exists as `_dv_exp2` in core/dsp-voice | stock (core) |
| Envelope (AD/ADSR) | `void dk_env(dk_env_state *s, float *out)` | R4 = state, R8 = out | 6 x 32 + 20 = 190 | own (stock needs the rate table; addresses differ) |
| Half-band 2x up and down | `void dk_halfband_down(state *s, const float *in64, float *out32)` | R4, R8 (64 floats), R12 (32 floats) | 10 x 32 + 30 = 350 | own (stock is about 25 per sample) |
| Block copy / fill | `void dk_copy(float *dst, const float *src, int n)` | R4, R8, R12 | 16 + 32 + 20 = 70 for 32 | stock `0x1cdd8b` / `0xb8251b` (not the same address: needs a device row) or own |

### Own against stock: the trade-off

- Own:
  - Costs DSP program memory per module (each module carries its copy; a shared SDK object in the `.dkm` could
    share it between modules, **[O]** whether the loader supports it).
  - Needs measurement, but is stable across firmware updates and identical on both devices by construction.
  - Can be faster than the stock code (delay 41 -> 15, halfband 25 -> 10, **[O]**). Can use the contract
    (M5, M6 constants) and 32-sample blocks.
- Stock through an import:
  - Saves code, but the address comes from the anchor rules (hash resolve) and the record layout is the firmware's.
  - Shared kernels: the DT2 and DN2 addresses differ (for example biquad `0x1ccbd8` / `0xb81368`). The code is
    byte-identical or reloc-hash equal, but the address is per device, so each import needs a device row. Only the
    divide (`0x1c06ba`) has one address on both **[D]**.
  - Hidden dependencies: the envelope reads a rate table at a device-specific absolute address (DT2 `0x26ad30`,
    DN2 listing `0x26a570`). powf reads three tables and writes error words. The divide and rsqrt change MODE1 bit
    16 and restore it **[D]**. A module must not call them from code that runs with SIMD or a loop active **[O]**.
  - Firmware updates can move or change them. The DSP image was byte-identical from 1.16 to 1.17 and 1.11 to
    1.12 (finding 53), so the risk is small today.
- Recommendation: write our own for everything on the hot path. Use stock only for the divide and `dv_exp2`
  (already available), where a device-independent call exists.

## 4. Top kernels to build first

1. Ramp + gain (and mix): every module needs it; one instruction per sample; it fixes the ramp idiom.
2. Biquad (5-coefficient, 32 samples, stereo SIMD variant): the most used block in FX and machines.
3. Circular delay with interpolated read: Karplus-Strong (pluck's 23-instruction loop becomes about 10), chorus, delay.
4. Soft saturator with ramped drive: cheap, and protects `DK_LEVEL_BOUND`.
5. Reciprocal, rsqrt and divide macros: needed by filters, envelopes and normalisation; keep out of loops.
After these: half-band 2x oversampler (DT2 sample machines, FM), envelope.

## 5. Open questions

- Biquad topology of the stock record (4 coefficients, 2 state words): decide by reading the arithmetic in the loop. **[O]**
- Is a `DO` loop of 1 or 2 instructions legal and free on this core? Manual: "Restrictions on Ending Loops",
  "VISA-Related Restrictions on Hardware Loops". The stock loops are 4 or more. Needs one emulator check.
- Stalls: the manual lists stall causes (Pipeline Flushes and Stalls, "Stalls Related to DAG Operations" and
  "Stalls Related to Memory Access"). The emulator counts instructions, not stalls, so all cycle numbers are
  instruction counts. Hardware timing is **[O]**.
- Can a module use `B`/`L` registers (circular buffers)? The contract keeps L0-L7 and CBUFEN is set (finding 45).
  A module must set B and L, then restore L to 0 before return. Check that no interrupt in the voice context needs them.
- Does the `.dkm` loader allow one shared copy of an SDK kernel for several modules?
