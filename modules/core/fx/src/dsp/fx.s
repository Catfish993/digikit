// core/fx: the effects adapter of the SHARC engine (docs/findings/55, sdk/dk_fx.h).
//
// The hook is the first instruction of the function the block orchestrator calls once per block after the master mix and before the USB
// pack and the DAC converter (H1 of finding 53: Digitakt II FUN_1c14e7, Digitone II FUN_1c244d, which also packs the USB stream). patchkit
// overwrites that 48-bit instruction (`I7 = MODIFY(I7, imm)`) with `JUMP _fx_entry`; the displaced instruction runs in the hook's tail
// (`_fx_back`), which jumps to the second instruction of the stock function. At the entry nothing of the stock frame exists yet, so every
// register the adapter or an effect touches is saved in `_fx_save` (static: the hook is not re-entered) and restored before the tail.
// Argument registers (R4, R8, R12 on the Digitone II) and the callee-saved registers are therefore intact when the stock code starts.
//
// An empty chain (the table's first row has pc 0) costs the save of R0, I4 and L4, one load and the restore: the audio is the stock audio.
//
// The chain: the rows of the registry `fx` (_fx_table, 16 words each, core/fx's module.toml), in module order, up to the zero row. Per row, per block:
//   * disabled by faults (dis != 0): skipped. enabled == was == 0: skipped.
//   * enabled and not was: the effect's state (state_bytes of its dk_fx_desc) is zeroed, then it runs and fades in over the block.
//   * enabled and was: it runs and its output replaces the master block.
//   * not enabled and was: it runs once more and fades out over the block (the process routine is not called while off).
//   The fade is linear per channel: master[k] = dry + (wet - dry) * w_k, w_k = (k + 1) / 32 (in) or 1 - (k + 1) / 32 (out).
//   * process(R4 = state, R8 = dk_fx_in {in = master, out = scratch, block}, R12 = the row's parameter words) is entered with
//     `I13 = return; JUMP (M13, I12)`. It may clobber R0-R15, I0-I5, M1-M4, B3 and must keep the rest (sdk/dk_fx.h).
//   * the output is checked: a negative result, a non-finite sample or a magnitude above 8.0 is a fault: the master block stays as it was (dry,
//     no fade), the effect's state is zeroed and `was` is cleared. The 8th fault within 1500 blocks sets `dis` until the next start.
// (Step 2 of P3d-1a: the hook and the empty adapter. The chain walk follows in step 3.)
// Context: the firmware restores M5-M7, M13-M15, the L registers and MODE1 per DSP context (finding 45), so the adapter saves them and pins
// M5 = 0, M6 = 1, M7 = -1, M13 = 0, M14 = 1, M15 = -1, L0-L3 = L5 = 0, MODE1: PEYEN, TRUNCATE and ALUSAT clear, RND32 set, for the chain
// and restores them at the end. The code before the pin uses immediate offsets and no modify register; L4 is 0 for the whole call.

#include "dk_fx.inc"

#define FX_PC 0
#define FX_DESC 1
#define FX_STATE 2
#define FX_EN 3
#define FX_P0_BYTES 16
#define FX_WAS 8
#define FX_DIS 10
#define FX_WSTART 11
#define FX_WCOUNT 12
#define FX_NFAULT 13
#define FX_ROW_BYTES 64
#define FX_WINDOW 1500
#define FX_MAX_FAULTS 8

.SECTION/PM seg_pmco;
.GLOBAL _fx_entry;
.EXTERN _fx_table;
.EXTERN _fx_master;
.EXTERN _fx_back;

_fx_entry:
    DM(_fx_s_r0) = R0;
    R0 = I4;
    DM(_fx_s_i4) = R0;
    R0 = L4;
    DM(_fx_s_l4) = R0;
    L4 = 0;
    I4 = _fx_table;
    R0 = DM(0, I4);                       // the first row's pc (the chain is not walked yet)
_fx_none:
    R0 = DM(_fx_s_l4);
    L4 = R0;
    R0 = DM(_fx_s_i4);
    I4 = R0;
    R0 = DM(_fx_s_r0);
    JUMP _fx_back;                        // the displaced instruction, then the stock function


.SECTION/DM seg_dmda;
.PKDATA _fx_s_r0, 4;
.PKDATA _fx_s_i4, 4;
.PKDATA _fx_s_l4, 4;
