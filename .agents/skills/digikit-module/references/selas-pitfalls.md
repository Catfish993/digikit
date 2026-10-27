# Selache (selas) pitfalls for SHARC+ module code

selas is the public SHARC+ assembler that `module:build` runs. Some forms assemble to bytes that decode to a valid-looking
instruction but are wrong on the silicon. The build repairs or refuses the known ones (`tools/patchkit/pk_sharc.py`); this
page says what to write instead. Evidence: docs/findings/05-sharc-isa-and-decoding.md ("Selache shift-immediate encodings"),
docs/findings/45-dsp-voice-context.md, docs/findings/49-dt2-type-sites-and-uninit-model.md (section 10.7).

## Shifts by an immediate

| You write | What happens | Write instead |
| --- | --- | --- |
| `R0 = LSHIFT R1 BY -16;` | repaired by the build (selas leaves the sign bits out; the chip raises ILOPI without them) | fine as written |
| `R0 = LSHIFT R1 BY 16;`, `FEXT R1 BY 4:8;`, `BSET`/`BCLR`/`BTGL`/`BTST R1 BY 5;` | correct | fine as written |
| `R0 = ASHIFT R1 BY -3;` (any count) | refused: selas writes an undefined opcode | put the count in a register: `R2 = -3; R0 = ASHIFT R1 BY R2;` |
| `R0 = R0 OR LSHIFT R1 BY 3;` | refused: selas drops the OR | `R2 = LSHIFT R1 BY 3; R0 = R0 OR R2;` |
| `R0 = R0 OR ASHIFT R1 BY -3;` | refused: undefined opcode | `R2 = -3; R3 = ASHIFT R1 BY R2; R0 = R0 OR R3;` |
| `FDEP R1 BY 4:8;`, `R0 = R0 OR FDEP R1 BY 4:8;` | refused: selas picks the (SE) or the plain FDEP | mask and shift: `R2 = LSHIFT R1 BY 4; R0 = R0 OR R2;` (mask R1 first) |
| `FEXT R1 BY 4:8 (SE);` | refused: assembles to an unrelated word | `R0 = FEXT R1 BY 4:8;` then sign-extend with a shift pair through a register count |
| `ROT R1 BY 3;` | selas refuses it | `R2 = 3; R0 = ROT R1 BY R2;` (check it on the rig) |

The refusal message names the form; comments are not checked. selcc (C) output goes through the same check.

## 16-bit `Rn = Rn op Rx`

A two-register compute whose destination is one of its sources (`R0 = R0 + R1;`) assembles to a 16-bit form that the
emulator may read with another width than the silicon. The build refuses it. Use three distinct registers:
`R0 = R2 + R1;` (finding 45).

## Stores through an index register

`DM(Ia, Mb) = Ureg` cannot store a register of the same DAG (I0-I7, M0-M7, L0-L7, B0-B7 through DM; DAG2 registers
through PM). selas and the emulator accept it; the silicon does not run it as written. This was the rung C halt on the
device (finding 49 section 10.7). Copy the register to an R register first: `R2 = I1; DM(I4, M6) = R2;`. The build reports
these stores.

## Short-word loads

selas ignores `(SW)`/`(SWSE)` on a direct load (`R0 = DM(symbol) (SWSE);`); the build rewrites it to the short-word form.
Write it as you mean it.

## MODIFY

`MODIFY (I7, -26) (NW);` assembles to the firmware's own bytes; without `(NW)` the immediate is in bytes, not words
(finding 20). Always write the scale you mean.

## Rule

"It decodes" is not proof. When you use a form with an immediate that no module uses yet, compare its bytes with the same
form in the stock image (`tools/sharc_dev/sharc.py`, `img.sql("select raw from insn where ...")`) and add what you learn
here and to the cookbook finding.

- `MODIFY(Ix, My)` with a register modifier: our decoder reads `selas`'s encoding as a 48-bit Type 7a and the
  emulator stops. Move a circular pointer with a dummy load `Fx = DM(Ix, My);` instead (open item in the plan).
- Short loads and stores with M0-M3 outside a DO loop: Selache's listing shows them as 4-byte rows and the verify step
  rejects the build. Use M4 for such a modifier.
