// core/fx: the effects adapter of the SHARC engine (docs/findings/55, sdk/dk_fx.h).
//
// The hook is the first instruction of a function that the block orchestrator calls once per block after the master mix and before the USB
// pack: Digitakt II FUN_1c80f2 (called at 0x1c311f, after the pack's earlier siblings and before the pack at 0x1c3160), Digitone II FUN_1c244d (H1 of
// finding 53, which also holds the pack). The site is chosen for the emulator's AOT core, not for the firmware: a patched code range bars the generated
// regions of that range (finding 55). patchkit overwrites the 48-bit first instruction (`I7 = MODIFY(I7, imm)`) with `JUMP _fx_entry`; the displaced
// instruction runs in the hook's tail (`_fx_back`), which jumps to the second instruction of the stock function. At the entry nothing of the stock frame
// exists yet, so every register the adapter or an effect touches is saved in `_fx_save` (static: the hook is not re-entered) and restored before the
// tail. Argument registers (R4, R8, R12) and the callee-saved registers are therefore intact when the stock code starts.
//
// An empty chain (every row up to the zero row is free, or there is none) costs the save of R0, I4 and L4, a quick scan of at most DK_FX_MAX rows
// (about 5 instructions per free row) and the restore: the audio is the stock audio.
//
// The chain: the rows of the registry `fx` (_fx_table, 24 words each, core/fx's module.toml), in module order, up to the zero row. A free row (pc
// 0xffffffff, a place that core/dynfx reserves for a run-time effect) is skipped; a run-time effect is published by writing its words and its pc last
// (one word per transfer, docs/design/digikit-dkm.md), so a scan never runs a half-written row. Per row, per block:
//   * free (pc 0xffffffff): skipped. disabled by faults (dis != 0): skipped. enabled == was == 0: skipped.
//   * enabled and not was: the effect's state (state_bytes of its dk_fx_desc) is zeroed, then it runs and fades in over the block.
//   * enabled and was: it runs and its output replaces the master block.
//   * not enabled and was: it runs once more and fades out over the block (the process routine is not called while off).
//   The fade is linear per channel: master[k] = dry + (wet - dry) * w_k, w_k = (k + 1) / 32 (in) or 1 - (k + 1) / 32 (out).
//   MIX (row word 13, a float): the dry/wet blend of the row, owned by the adapter, not by the effect. The effect writes only the wet signal; the
//   master becomes dry + mix * (wet - dry) (linear), in the fades the weight w_k is multiplied by the mix. mix == 1.0 (the default of a row) is the
//   plain copy (the wet signal bit for bit); mix == 0 leaves the master as it was (the dry signal bit for bit; the effect still runs and is checked).
//   A run-time effect gets its mix from the knob on its page (docs/design/digikit-registry.md).
//   * process(R4 = state, R8 = dk_fx_in {in = master, out = scratch, block}, R12 = the row's parameter words) is entered with
//     `I13 = return; JUMP (M13, I12)`. It may clobber R0-R15, I0-I5, M1-M4, B3 and must keep the rest (sdk/dk_fx.h).
//   * the output is checked: a negative result, a non-finite sample or a magnitude above 8.0 is a fault: the master block stays as it was (dry,
//     no fade), the effect's state is zeroed and `was` is cleared. The 8th fault within 1500 blocks sets `dis` until the next start.
// Context: the firmware restores M5-M7, M13-M15, the L registers and MODE1 per DSP context (finding 45), so the adapter saves them and pins
// M5 = 0, M6 = 1, M7 = -1, M13 = 0, M14 = 1, M15 = -1, L0-L3 = L5 = 0, MODE1: PEYEN, TRUNCATE and ALUSAT clear, RND32 set, for the chain
// and restores them at the end. The code before the pin uses immediate offsets and no modify register; L4 is 0 for the whole call.

#include "dk_fx.inc"

#define FX_PC 0
#define FX_DESC 1
#define FX_STATE 2
#define FX_EN 3
#define FX_P0_BYTES 16
#define FX_WAS 12
#define FX_MIX 13
#define FX_DIS 14
#define FX_WSTART 15
#define FX_WCOUNT 16
#define FX_NFAULT 17
#define FX_ROW_BYTES 96
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
    // the quick scan: rows 0 to 3 (DK_FX_MAX; a row is 24 words). pc 0 ends the table (nothing to run), pc 0xffffffff is a free row (next row),
    // anything else is an effect: run the chain.
    R0 = DM(0, I4);
    R0 = PASS R0;
    IF EQ JUMP _fx_none;
    R0 = R0 + 1;
    IF NE JUMP _fx_run;
    R0 = DM(24, I4);
    R0 = PASS R0;
    IF EQ JUMP _fx_none;
    R0 = R0 + 1;
    IF NE JUMP _fx_run;
    R0 = DM(48, I4);
    R0 = PASS R0;
    IF EQ JUMP _fx_none;
    R0 = R0 + 1;
    IF NE JUMP _fx_run;
    R0 = DM(72, I4);
    R0 = PASS R0;
    IF EQ JUMP _fx_none;
    R0 = R0 + 1;
    IF NE JUMP _fx_run;
_fx_none:
    R0 = DM(_fx_s_l4);
    L4 = R0;
    R0 = DM(_fx_s_i4);
    I4 = R0;
    R0 = DM(_fx_s_r0);
    JUMP _fx_back;                        // the displaced instruction, then the stock function

_fx_run:
    I4 = _fx_save;
    DM(0, I4) = R1;
    DM(1, I4) = R2;
    DM(2, I4) = R3;
    DM(3, I4) = R4;
    DM(4, I4) = R5;
    DM(5, I4) = R6;
    DM(6, I4) = R7;
    DM(7, I4) = R8;
    DM(8, I4) = R9;
    DM(9, I4) = R10;
    DM(10, I4) = R11;
    DM(11, I4) = R12;
    DM(12, I4) = R13;
    DM(13, I4) = R14;
    DM(14, I4) = R15;
    R0 = I0;                              // a DM store cannot take a DAG1 register (UREGXDAG1, PRM p.15-7): through R0 (saved in _fx_s_r0)
    DM(15, I4) = R0;
    R0 = I1;
    DM(16, I4) = R0;
    R0 = I2;
    DM(17, I4) = R0;
    R0 = I3;
    DM(18, I4) = R0;
    R0 = I5;
    DM(19, I4) = R0;
    DM(20, I4) = I12;
    DM(21, I4) = I13;
    R0 = M1;
    DM(22, I4) = R0;
    R0 = M2;
    DM(23, I4) = R0;
    R0 = M3;
    DM(24, I4) = R0;
    R0 = M4;
    DM(25, I4) = R0;
    R0 = B3;
    DM(26, I4) = R0;
    // save the context registers, then pin them
    I4 = _fx_ctx;
    R3 = M5;
    DM(0, I4) = R3;
    R3 = M6;
    DM(1, I4) = R3;
    R3 = M7;
    DM(2, I4) = R3;
    DM(3, I4) = M13;
    DM(4, I4) = M14;
    DM(5, I4) = M15;
    R3 = L0;
    DM(6, I4) = R3;
    R3 = L1;
    DM(7, I4) = R3;
    R3 = L2;
    DM(8, I4) = R3;
    R3 = L3;
    DM(9, I4) = R3;
    R3 = L5;
    DM(10, I4) = R3;
    R3 = MODE1;
    DM(11, I4) = R3;
    M5 = 0;
    M6 = 1;
    M7 = -1;
    M13 = 0;
    M14 = 1;
    M15 = -1;
    L0 = 0;
    L1 = 0;
    L2 = 0;
    L3 = 0;
    L5 = 0;
    BIT CLR MODE1 0x20A000;               // PEYEN, TRUNCATE, ALUSAT
    BIT SET MODE1 0x10000;                // RND32
    NOP;

    // the block counter and the dk_fx_in
    R0 = DM(_fx_blk);
    R0 = R0 + 1;
    DM(_fx_blk) = R0;
    I4 = _fx_in;
    R1 = _fx_master;
    DM(DK_FX_IN_W_IN, I4) = R1;
    R1 = _fx_scratch;
    DM(DK_FX_IN_W_OUT, I4) = R1;
    DM(DK_FX_IN_W_BLOCK, I4) = R0;
    R0 = _fx_table;
    DM(_fx_c + 0) = R0;                   // the row

_fx_row:
    R0 = DM(_fx_c + 0);
    I4 = R0;
    R0 = DM(FX_PC, I4);
    R0 = PASS R0;
    IF EQ JUMP _fx_rows_done;
    R0 = R0 + 1;
    IF EQ JUMP _fx_skip;                  // a free row (pc 0xffffffff)
    R0 = DM(FX_DIS, I4);
    R0 = PASS R0;
    IF NE JUMP _fx_skip;
    R2 = DM(FX_EN, I4);
    R3 = DM(FX_WAS, I4);
    R4 = 1;
    R1 = 0;
    R2 = PASS R2;
    IF NE R1 = PASS R4;                   // enabled, 0 or 1
    DM(_fx_c + 8) = R1;
    COMP(R1, R3);
    IF NE JUMP _fx_chg;
    R1 = PASS R1;
    IF EQ JUMP _fx_skip;                  // off and was off
    R5 = 0;                               // mode: steady
    JUMP _fx_go;
_fx_chg:
    R1 = PASS R1;
    IF EQ JUMP _fx_off;
    CALL _fx_zero;                        // switching on: a fresh state
    R5 = 1;                               // mode: fade in
    JUMP _fx_go;
_fx_off:
    R5 = 2;                               // mode: fade out
_fx_go:
    DM(_fx_c + 4) = R5;
    R4 = DM(FX_STATE, I4);
    R8 = _fx_in;
    R12 = I4;
    R0 = FX_P0_BYTES;
    R12 = R12 + R0;
    I12 = DM(FX_PC, I4);
    I13 = _fx_ret;
    JUMP (M13, I12);                      // process returns with JUMP (M13, I13)
_fx_ret:
    DM(_fx_c + 12) = R0;
    R0 = DM(_fx_c + 0);
    I4 = R0;
    R0 = DM(_fx_c + 12);
    R0 = PASS R0;
    IF LT JUMP _fx_fault;                 // a negative result
    // the output block must hold finite samples of at most 8.0 in magnitude (the same integer test as the voice guard of core/dsp-voice)
    I0 = _fx_scratch;
    R13 = 0;
    R9 = 0x7fffffff;
    LCNTR = DK_FX_BLOCK, DO _fx_chk UNTIL LCE;
        R0 = DM(I0, M6);
        R0 = R0 AND R9;
_fx_chk: R13 = MAX(R13, R0);
    R0 = 0x41000000;                      // 8.0
    COMP(R13, R0);
    IF GT JUMP _fx_fault;
    R9 = DM(FX_MIX, I4);                  // I4 is still the row
    R0 = 0x7fffffff;
    R0 = R9 AND R0;
    IF EQ JUMP _fx_ok;                    // mix 0: the master stays dry, bit for bit
    R5 = DM(_fx_c + 4);
    R0 = 0x3f800000;
    COMP(R9, R0);
    IF NE JUMP _fx_xfade;                 // mix not 1.0: the blend (also in the steady mode)
    R5 = PASS R5;
    IF NE JUMP _fx_xfade;
    I1 = _fx_scratch;                     // steady, mix 1.0: the output replaces the master block
    I0 = _fx_master;
    LCNTR = DK_FX_BLOCK, DO _fx_cp UNTIL LCE;
        R0 = DM(I1, M6);
_fx_cp: DM(I0, M6) = R0;
    JUMP _fx_ok;
_fx_xfade:                                // R9 = the mix (float), R5 = the mode: 0 steady (mix not 1.0), 1 fade in, 2 fade out
    R6 = 0x3d000000;                      // +1/32
    R7 = 0;                               // the weight before the first sample
    R0 = 1;
    COMP(R5, R0);
    IF EQ JUMP _fx_xf;
    R6 = 0xbd000000;                      // -1/32
    R7 = 0x3f800000;                      // 1.0
    R5 = PASS R5;
    IF NE JUMP _fx_xf;
    R6 = 0;                               // steady: the weight stays 1.0, so the blend is the mix
_fx_xf:
    I1 = _fx_scratch;
    I0 = _fx_master;
    I2 = _fx_master;
    CALL _fx_xchan;                       // left
    CALL _fx_xchan;                       // right
_fx_ok:
    R0 = DM(_fx_c + 0);
    I4 = R0;
    R1 = DM(_fx_c + 8);
    DM(FX_WAS, I4) = R1;
_fx_skip:
    R0 = DM(_fx_c + 0);
    R1 = FX_ROW_BYTES;
    R0 = R0 + R1;
    DM(_fx_c + 0) = R0;
    JUMP _fx_row;

// A fault (I4 = the row): the master block is untouched, the state is zeroed, the effect counts as off. The 8th fault within the window sets `dis`.
_fx_fault:
    CALL _fx_zero;
    R0 = 0;
    DM(FX_WAS, I4) = R0;
    R1 = DM(_fx_diag + 4);
    R1 = R1 + 1;
    DM(_fx_diag + 4) = R1;                // faults of all effects
    R5 = DM(_fx_blk);
    R6 = DM(FX_WSTART, I4);
    R7 = R5 - R6;
    R0 = FX_WINDOW;
    COMP(R7, R0);
    IF LT JUMP _fx_fwin;
    DM(FX_WSTART, I4) = R5;
    R7 = 0;
    DM(FX_WCOUNT, I4) = R7;
_fx_fwin:
    R7 = DM(FX_WCOUNT, I4);
    R7 = R7 + 1;
    DM(FX_WCOUNT, I4) = R7;
    R9 = DM(FX_NFAULT, I4);
    R9 = R9 + 1;
    DM(FX_NFAULT, I4) = R9;
    R0 = FX_MAX_FAULTS;
    COMP(R7, R0);
    IF LT JUMP _fx_skip;
    R0 = 1;
    DM(FX_DIS, I4) = R0;
    JUMP _fx_skip;

_fx_rows_done:
    I4 = _fx_ctx;
    R0 = DM(11, I4);
    MODE1 = R0;
    NOP;
    M5 = DM(0, I4);
    M6 = DM(1, I4);
    M7 = DM(2, I4);
    M13 = DM(3, I4);
    M14 = DM(4, I4);
    M15 = DM(5, I4);
    L0 = DM(6, I4);
    L1 = DM(7, I4);
    L2 = DM(8, I4);
    L3 = DM(9, I4);
    L5 = DM(10, I4);
    I4 = _fx_save;
    B3 = DM(26, I4);                      // a B register load also loads its I register (PRM p.6-24): B3 before I3
    R1 = DM(0, I4);
    R2 = DM(1, I4);
    R3 = DM(2, I4);
    R4 = DM(3, I4);
    R5 = DM(4, I4);
    R6 = DM(5, I4);
    R7 = DM(6, I4);
    R8 = DM(7, I4);
    R9 = DM(8, I4);
    R10 = DM(9, I4);
    R11 = DM(10, I4);
    R12 = DM(11, I4);
    R13 = DM(12, I4);
    R14 = DM(13, I4);
    R15 = DM(14, I4);
    I0 = DM(15, I4);
    I1 = DM(16, I4);
    I2 = DM(17, I4);
    I3 = DM(18, I4);
    I5 = DM(19, I4);
    I12 = DM(20, I4);
    I13 = DM(21, I4);
    M1 = DM(22, I4);
    M2 = DM(23, I4);
    M3 = DM(24, I4);
    M4 = DM(25, I4);
    JUMP _fx_none;                        // L4, I4, R0 and the tail

// In: I4 = the row. Zeroes the effect's state (state_bytes of its desc). Clobbers R0, R5, I1, I2.
_fx_zero:
    I1 = DM(FX_DESC, I4);
    R5 = DM(DK_FX_DESC_W_STATE_BYTES, I1);
    R0 = -2;
    R5 = LSHIFT R5 BY R0;                 // words
    R5 = PASS R5;
    IF EQ RTS;
    I2 = DM(FX_STATE, I4);
    R0 = 0;
    LCNTR = R5, DO _fx_zl UNTIL LCE;
_fx_zl: DM(I2, M6) = R0;
    RTS;

// One channel of a fade or of a blend. In: R6 = the weight step, R7 = the weight before the first sample, R9 = the mix (float), I1 = wet, I2 = dry
// (read), I0 = master (write). The weight of a sample is the fade weight times the mix. Each pointer advances by DK_BLOCK words. Clobbers R0, R1,
// R10, R11.
_fx_xchan:
    R10 = R7;
    LCNTR = DK_BLOCK, DO _fx_xl UNTIL LCE;
        F10 = F10 + F6;
        F11 = F10 * F9;
        R0 = DM(I2, M6);
        R1 = DM(I1, M6);
        F1 = F1 - F0;
        F1 = F1 * F11;
        F1 = F1 + F0;
_fx_xl: DM(I0, M6) = R1;
    RTS;

.SECTION/DM seg_dmda;
.PKDATA _fx_save, 128;                    // R1-R15, I0-I3, I5, I12, I13, M1-M4, B3
.PKDATA _fx_s_r0, 4;
.PKDATA _fx_s_i4, 4;
.PKDATA _fx_s_l4, 4;
.PKDATA _fx_ctx, 48;                      // M5, M6, M7, M13, M14, M15, L0-L3, L5, MODE1 of the context
.PKDATA _fx_blk, 4;                       // blocks that ran a chain
.PKDATA _fx_in, 12;                       // the dk_fx_in
.PKDATA _fx_scratch, 256;                 // the effect's output block
// The adapter's scalars: 0 row, 4 mode, 8 enabled, 12 result.
.PKDATA _fx_c, 16;
// Diagnostics: word 0 spare, word 1 faults of all effects.
.PKDATA _fx_diag, 32;
