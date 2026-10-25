// core/fx, the send route (Digitakt II; docs/findings/62-fx-send-route.md): an effect row with route 1 does not process the master. Its input is a send bus: the sum
// over the 16 tracks of (the row's amount for the track) squared times the track's bus. Its wet output times the row's MIX is added to the stock FX return bus,
// which the master function mixes like the return of the stock send effects. The effect's process routine and the dk_fx contract are the same as in the master
// route (64 planar floats in: 32 left, 32 right; wet out).
//
// The hook is the first instruction of the master function (FUN_1c207b, called at 0x1c3099; 48 bits, `I7 = MODIFY(I7, -0x14)`, one caller). The sends driver
// (FUN_1c18a6) has finished: the track buses 0x252df8 + 0x100 * t (16 tracks, 32 left then 32 right floats) are intact, the FX return bus 0x254778 is written (by
// the driver at 0x1c1ef2) and not yet read (by the master at 0x1c21ab). The entry is the same kind of hook as fx.s's: the displaced instruction runs in the tail
// (`_fxs_back`), every register is saved by `_fx_enter` of fx.s, which also pins the context, and `_fx_exec` of fx.s runs one row with the output check, the
// fault accounting and the fades. So the guard exists once.
//
// The block counter `_fx_blk` is advanced by the master hook only (once a block, also when the chain has no master row); this hook runs earlier in the block and
// passes the current value on, so a send effect sees the number of the block before.
//
// Row words (fx.s, module.toml): 18 route (0: master chain, 1: send; other values: the row is run by neither), 19 to 22 the amounts of the 16 tracks, one byte
// for each, track t in word 19 + t / 4, byte t % 4 (little endian; track 0 is the least significant byte of word 19). A byte is 0 to 127 (a larger value counts as 127).
// The gain of a track is (byte / 127) squared, as the stock sends square their knob value (FUN_1c18a6: `F2 = F2 * F2`). Each gain is ramped in a block, linear from
// the gain of the last block that ran this row to the new gain: the gain before sample k is old + (k + 1) * step, step = (new - old) / 32, by adding the step
// per sample (the stock sends ramp the same way). The old gains are kept per row in `_fxs_prev`. A track with the old and the new gain 0 costs a few instructions.
//
// Cost (concrete core, tests/patchkit/test_fx_send.py, the half test effect of about 210 instructions): a block with no send row costs the quick scan, 16 instructions
// (no row) to 31 (three free rows), about 8 for each effect row. A send row, steady: 1264 instructions with all amounts 0 (the context save and restore, the output
// check of fx.s, clearing the bus, the 16 gains, the return add: 256), 1414 with one track, each track with a gain adds 150 (4 instructions a sample), 3664 with 16 tracks.
// A track whose old and new gain are 0 costs 10.
//
// Register use: R0 to R15, I0 to I5, I12, I13, M1 to M4, B3 as fx.s (the hook saves them), M6 = 1 and M14 = 1 pinned by `_fx_enter`.

#include "dk_fx.inc"

#define FX_PC 0
#define FX_EN 3
#define FX_WAS 12
#define FX_MIX 13
#define FX_DIS 14
#define FX_ROUTE 18
#define FX_AMT0_BYTES 76                  // word 19
#define FX_ROW_BYTES 96
#define FXS_TRACKS 16
#define FXS_TRACK_BYTES 256               // a track bus: 64 floats
#define FXS_RIGHT_BYTES 128               // the right channel of a bus of 32 + 32 floats

.SECTION/PM seg_pmco;
.GLOBAL _fxs_entry;
.EXTERN _fx_table;
.EXTERN _fx_enter;
.EXTERN _fx_leave;
.EXTERN _fx_exec;
.EXTERN _fx_c;
.EXTERN _fx_in;
.EXTERN _fx_scratch;
.EXTERN _fx_s_r0;
.EXTERN _fx_s_i4;
.EXTERN _fx_s_l4;
.EXTERN _fxs_trackbus;
.EXTERN _fxs_return;
.EXTERN _fxs_back;

_fxs_entry:
    DM(_fx_s_r0) = R0;
    R0 = I4;
    DM(_fx_s_i4) = R0;
    R0 = L4;
    DM(_fx_s_l4) = R0;
    L4 = 0;
    I4 = _fx_table;
    // the quick scan: rows 0 to 3. pc 0 ends the table, pc 0xffffffff is a free row (next row), route 1 is a send row: run, anything else: next row.
    R0 = DM(0, I4);
    R0 = PASS R0;
    IF EQ JUMP _fxs_none;
    R0 = R0 + 1;
    IF EQ JUMP _fxs_q1;
    R0 = DM(18, I4);
    R0 = R0 - 1;
    IF EQ JUMP _fxs_run;
_fxs_q1:
    R0 = DM(24, I4);
    R0 = PASS R0;
    IF EQ JUMP _fxs_none;
    R0 = R0 + 1;
    IF EQ JUMP _fxs_q2;
    R0 = DM(42, I4);
    R0 = R0 - 1;
    IF EQ JUMP _fxs_run;
_fxs_q2:
    R0 = DM(48, I4);
    R0 = PASS R0;
    IF EQ JUMP _fxs_none;
    R0 = R0 + 1;
    IF EQ JUMP _fxs_q3;
    R0 = DM(66, I4);
    R0 = R0 - 1;
    IF EQ JUMP _fxs_run;
_fxs_q3:
    R0 = DM(72, I4);
    R0 = PASS R0;
    IF EQ JUMP _fxs_none;
    R0 = R0 + 1;
    IF EQ JUMP _fxs_none;
    R0 = DM(90, I4);
    R0 = R0 - 1;
    IF EQ JUMP _fxs_run;
_fxs_none:
    R0 = DM(_fx_s_l4);
    L4 = R0;
    R0 = DM(_fx_s_i4);
    I4 = R0;
    R0 = DM(_fx_s_r0);
    JUMP _fxs_back;                       // the displaced instruction, then the master function

_fxs_run:
    CALL _fx_enter;
    I4 = _fx_in;
    R0 = _fxs_bus;
    R1 = 4;
    R0 = R0 + R1;                         // the bus: one word behind the pad
    DM(DK_FX_IN_W_IN, I4) = R0;
    R0 = _fx_table;
    DM(_fx_c + 0) = R0;                   // the row
    R0 = 0;
    DM(_fxs_c + 0) = R0;                  // the row's index

_fxs_row:
    R0 = DM(_fx_c + 0);
    I4 = R0;
    R0 = DM(FX_PC, I4);
    R0 = PASS R0;
    IF EQ JUMP _fxs_done;
    R0 = R0 + 1;
    IF EQ JUMP _fxs_skip;                 // a free row
    R0 = DM(FX_ROUTE, I4);
    R0 = R0 - 1;
    IF NE JUMP _fxs_skip;                 // not a send row
    R0 = DM(FX_DIS, I4);
    R0 = PASS R0;
    IF NE JUMP _fxs_skip;                 // switched off by its faults
    R0 = DM(FX_EN, I4);
    R1 = DM(FX_WAS, I4);
    R0 = R0 OR R1;
    IF EQ JUMP _fxs_skip;                 // off and was off: no bus to build
    CALL _fxs_mixbus;
    R0 = DM(_fx_c + 0);
    I4 = R0;
    CALL _fx_exec;
    R0 = PASS R0;
    IF EQ JUMP _fxs_skip;                 // nothing to add: off, switched off or a fault
    CALL _fxs_ret;
    R0 = DM(_fx_c + 0);
    I4 = R0;
    R1 = DM(_fx_c + 8);
    DM(FX_WAS, I4) = R1;
_fxs_skip:
    R0 = DM(_fx_c + 0);
    R1 = FX_ROW_BYTES;
    R0 = R0 + R1;
    DM(_fx_c + 0) = R0;
    R0 = DM(_fxs_c + 0);
    R0 = R0 + 1;
    DM(_fxs_c + 0) = R0;
    JUMP _fxs_row;

_fxs_done:
    CALL _fx_leave;
    JUMP _fxs_none;

// The send bus of the row at `_fx_c + 0` (row number `_fxs_c + 0`): the sum over the tracks of gain * the track's bus. Clobbers what an effect may.
_fxs_mixbus:
    R0 = _fxs_bus;
    R1 = 4;
    R0 = R0 + R1;
    I0 = R0;
    R0 = 0;
    LCNTR = DK_FX_BLOCK, DO _fxs_z UNTIL LCE;
_fxs_z: DM(I0, M6) = R0;
    // the 16 new gains: (byte / 127) squared, from the amount words 19 to 22
    R0 = DM(_fx_c + 0);
    R1 = FX_AMT0_BYTES;
    R0 = R0 + R1;
    I1 = R0;
    I0 = _fxs_gnew;
    R4 = 127;
    R5 = -8;
    R6 = 0xff;
    R7 = 0x3c010204;                      // 1 / 127
    LCNTR = 4, DO _fxs_cw UNTIL LCE;
        R0 = DM(I1, M6);
        LCNTR = 4, DO _fxs_cb UNTIL LCE;
            R1 = R0 AND R6;
            R0 = LSHIFT R0 BY R5;
            R1 = MIN(R1, R4);
            F1 = FLOAT R1;
            F1 = F1 * F7;
            F1 = F1 * F1;
_fxs_cb:    DM(I0, M6) = R1;
_fxs_cw: NOP;
    // the tracks: I4 = the new gains, I5 = this row's old gains
    R0 = DM(_fxs_c + 0);
    R1 = 6;
    R0 = LSHIFT R0 BY R1;                 // 16 floats a row
    R1 = _fxs_prev;
    R0 = R0 + R1;
    I5 = R0;
    I4 = _fxs_gnew;
    R0 = _fxs_trackbus;
    DM(_fxs_c + 8) = R0;                  // the track's bus
    R3 = 0x3d000000;                      // 1 / 32 (kept: _fxs_mac does not use R3)
    LCNTR = FXS_TRACKS, DO _fxs_tl UNTIL LCE;
        R1 = DM(I4, M6);                  // the new gain
        R2 = DM(I5, M5);                  // the old gain
        R0 = R1 OR R2;
        IF EQ JUMP _fxs_ts;               // both 0: the track adds nothing
        DM(I5, M6) = R1;
        F0 = F1 - F2;
        F5 = F0 * F3;                     // the step
        R4 = PASS R2;                     // the gain before the first step
        CALL _fxs_mac;
        JUMP _fxs_te;
_fxs_ts: R0 = DM(I5, M6);                 // a dummy read: its post-modify steps over the track
_fxs_te: R0 = DM(_fxs_c + 8);
        R1 = FXS_TRACK_BYTES;
        R0 = R0 + R1;
_fxs_tl: DM(_fxs_c + 8) = R0;
    RTS;

// bus += g_k * the track's bus, g_k = F4 + (k + 1) * F5, k = 0 to 31, both channels. The track is at `_fxs_c + 8`. Keeps I4, I5, R3. The loop is software
// pipelined in four instructions a sample: the right channel is added in the same iteration, the store of the left sum goes one iteration later (the first one
// lands on the pad word before the bus) and the last one after the loop.
_fxs_mac:
    R0 = DM(_fxs_c + 8);
    I1 = R0;                              // the track, left
    R1 = FXS_RIGHT_BYTES;
    R0 = R0 + R1;
    I0 = R0;                              // the track, right
    R0 = _fxs_bus;
    R1 = 4;
    R0 = R0 + R1;
    I13 = R0;                             // the bus, left (read)
    R1 = FXS_RIGHT_BYTES;
    R2 = R0 + R1;
    I12 = R2;                             // the bus, right (read)
    I2 = R2;                              // the bus, right (write)
    I3 = _fxs_bus;                        // the pad, then the left channel (write)
    LCNTR = 32, DO _fxs_ml UNTIL LCE;
        F4 = F4 + F5, R1 = DM(I0, M6), R8 = PM(I12, M14);
        F12 = F1 * F4, R2 = DM(I1, M6), R10 = PM(I13, M14);
        F13 = F2 * F4, F8 = F8 + F12, DM(I3, M6) = R9;
_fxs_ml: F9 = F10 + F13, DM(I2, M6) = R8;
    DM(I3, M6) = R9;
    RTS;

// Adds the effect's output times its MIX to the FX return bus: return[k] += wet[k] * w_k * mix, w_k = 1 (steady), (k + 1) / 32 (fade in) or 1 - (k + 1) / 32
// (fade out), the same weights as the master blend of fx.s. Mix 0 adds nothing. The row is at `_fx_c + 0`, the mode at `_fx_c + 4`.
_fxs_ret:
    R0 = DM(_fx_c + 0);
    I4 = R0;
    R9 = DM(FX_MIX, I4);
    R0 = 0x7fffffff;
    R0 = R9 AND R0;
    IF EQ RTS;
    R5 = DM(_fx_c + 4);
    R5 = PASS R5;
    IF EQ JUMP _fxs_steady;
    R6 = 0x3d000000;                      // +1/32
    R7 = 0;                               // the weight before the first sample
    R0 = 1;
    COMP(R5, R0);
    IF EQ JUMP _fxs_rw;
    R6 = 0xbd000000;                      // -1/32
    R7 = 0x3f800000;                      // 1.0
_fxs_rw:
    I1 = _fx_scratch;
    I0 = _fxs_return;
    CALL _fxs_rchan;                      // left
    CALL _fxs_rchan;                      // right
    RTS;
_fxs_steady:                              // the weight is 1.0: return[k] += wet[k] * mix, both channels in one loop (the same products as the weighted loop)
    I1 = _fx_scratch;
    I2 = _fxs_return;
    I12 = _fxs_return;
    R4 = PASS R9;
    LCNTR = DK_FX_BLOCK, DO _fxs_sl UNTIL LCE;
        R0 = DM(I1, M6), R10 = PM(I12, M14);
        F12 = F0 * F4;
        F10 = F10 + F12;
_fxs_sl: DM(I2, M6) = R10;
    RTS;

// One channel: R6 = the weight step, R7 = the weight before the first sample, R9 = the mix, I1 = the wet block (read), I0 = the return (read and write). Each
// pointer advances by 32 words. Clobbers R0, R1, R2, R10, R11, I2.
_fxs_rchan:
    R10 = R7;
    I2 = I0;
    LCNTR = DK_BLOCK, DO _fxs_rl UNTIL LCE;
        F10 = F10 + F6;
        F11 = F10 * F9;
        R0 = DM(I1, M6);
        F1 = F0 * F11;
        R2 = DM(I2, M5);
        F2 = F2 + F1;
_fxs_rl: DM(I2, M6) = R2;
    I0 = I2;
    RTS;

.SECTION/DM seg_dmda;
.PKDATA _fxs_bus, 260;                    // the pad word, then the send bus (32 left, 32 right floats)
.PKDATA _fxs_gnew, 64;                    // the 16 new gains
.PKDATA _fxs_prev, 256;                   // the old gains: 16 floats for each of up to 4 rows
// The scalars: 0 the row's index, 8 the track's bus.
.PKDATA _fxs_c, 16;
