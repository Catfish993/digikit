// core/dsp-voice: the per-voice render hook of the SHARC engine (docs/findings/06, "Per-voice render hook").
//
// The firmware renders its 32 voices (two per track: 2t and 2t+1) with a CJUMP-called function whose first
// instruction is `MODIFY(I7, -26) (NW)` (the sample renderer, which also zero-fills an idle voice). patchkit
// overwrites that one 48-bit instruction with `JUMP _dv_entry` (op "jump"); this file executes the displaced
// instruction itself and continues in the stock renderer for every type it does not know, so a patched image with
// no registered type renders exactly the stock audio.
//
// At _dv_entry the stock contract holds: R4 = voice record, R8 = the voice's output buffer (32 floats), R12 = 32,
// R14 = the voice loop counter (32 on the first voice, 1 on the last), I6 = the frame the caller's CJUMP built, the
// delay-slot stores are done. The track is (32 - R14) >> 1; its machine type is the short word cache[t] at
// _fw_type_cache (the DSP's copy of frame word 0x94 + 2t), read as one 32-bit word that holds two tracks.
//
// A registered type (a row {type, pc} in _dv_table, filled by `register` ops of the machine modules) is rendered by
// calling its routine once per track, on the even voice; the odd voice receives a copy of the even voice's block
// (a synthesised machine is mono; the engine's later stages pan and mix both voices as for a mono sample).
//
// Routine ABI (entered with JUMP (M13, I12); I13 holds the return address, the routine ends with JUMP (M13, I13).
// The simulator's tracer does not follow an indirect CALL, and the firmware has none; the routine may use ordinary
// CALL/RTS pairs for its own subroutines):
//   in   R4 = voice record (0x1d8 bytes; byte +0x1b8 ACTIVE, +0x1b9 note-on mark, see below), R8 = output block
//        (32 floats, written in full), R12 = 32, R0 = type, R1 = voice index (even), R2 = track t.
//   may clobber  R0-R15, I0-I5, I13, M1-M4, B3 and the ALU/multiplier state; this wrapper saves and restores the
//        registers the stock renderer preserves (and I13). Must keep: I6, I7, I8-I12, I14, I15, M0, M5-M15, L registers,
//        MODE1 (SIMD off).
//        M5 = 0, M6 = 1, M7 = -1, M13 = 0, M14 = 1, M15 = -1 hold on entry.
//   The routine may read the frame copy at _fw_frame (512 short words: pitch word 0x02 + 2t = note << 8; velocity
//   0x34 + 2t = velocity << 8; machine parameters from 0xda + 0x60 t + 2 (mirror index - 25)).
//
// Note-on: the firmware's arm routine stores the short 1 at record +0x1b8, which clears byte +0x1b9. A routine marks
// a running voice by writing a non-zero byte to +0x1b9; `+0x1b8 != 0 and +0x1b9 == 0` is therefore a note-on in this
// block, whatever the machine's selector did to the record in the meantime (docs/findings/06).
//
// Registers: R0-R2, I4 are free on the stock fall-through path (the stock code assigns them before it reads them).

.SECTION/PM seg_pmco;
.GLOBAL _dv_entry;
.EXTERN _fw_type_cache;
.EXTERN _fw_render_next;
.EXTERN _fw_frame;

_dv_entry:
    R0 = 32;
    R0 = R0 - R14;                        // v, the voice index
    R2 = FEXT R0 BY 1:4;                  // t, the track
    R1 = 0xe;
    R1 = R2 AND R1;                       // t & 14
    R1 = LSHIFT R1 BY 1;                  // byte offset of the 32-bit word that holds cache[t] and cache[t^1]
    R2 = _fw_type_cache;
    R1 = R2 + R1;
    I4 = R1;
    R1 = DM(I4, M5);                      // the two types
    R2 = FEXT R0 BY 1:4;                  // t
    R2 = FEXT R2 BY 0:1;                  // t & 1
    R2 = LSHIFT R2 BY 4;                  // 0 or 16: the bit position of the half
    R0 = 0x400;
    R2 = R2 OR R0;
    R1 = FEXT R1 BY R2;                   // cache[t]
    I4 = _dv_table;
_dv_scan:
    R0 = DM(I4, M6);                      // row type; 0 ends the table (the last row is always empty)
    R2 = DM(I4, M6);                      // row pc
    R0 = PASS R0;
    IF EQ JUMP _dv_stock;
    COMP(R0, R1);
    IF NE JUMP _dv_scan;

    // A registered type: the even voice runs the routine, the odd voice copies the even voice's block (the buffers
    // are 0x80 bytes apart).
    I12 = R2;                             // the routine's pc
    MODIFY(I7, -30) (NW);
    DM(-6, I6) = R3;
    DM(-7, I6) = R5;
    DM(-8, I6) = R6;
    DM(-9, I6) = R7;
    DM(-10, I6) = R9;
    DM(-11, I6) = R10;
    DM(-12, I6) = R11;
    DM(-13, I6) = R13;
    DM(-14, I6) = R14;
    DM(-15, I6) = R15;
    DM(-16, I6) = I0;
    DM(-17, I6) = I1;
    DM(-18, I6) = I2;
    DM(-19, I6) = I3;
    DM(-20, I6) = I5;
    DM(-21, I6) = M1;
    DM(-22, I6) = M2;
    DM(-23, I6) = M3;
    DM(-24, I6) = B3;
    DM(-25, I6) = I13;
    R0 = R1;                              // type
    R1 = 32;
    R1 = R1 - R14;                        // v
    R2 = FEXT R1 BY 1:4;                  // t
    R3 = FEXT R1 BY 0:1;
    R3 = PASS R3;
    IF NE JUMP _dv_odd;
    R12 = 32;
    I13 = _dv_back;
    JUMP (M13, I12);                      // the routine returns with JUMP (M13, I13)
_dv_back:
    JUMP _dv_done;
_dv_odd:
    I1 = R8;
    MODIFY(I1, -128);
    I0 = R8;
    LCNTR = 32, DO _dv_copy UNTIL LCE;
        R0 = DM(I1, M6);
_dv_copy: DM(I0, M6) = R0;
_dv_done:
    R5 = DM(-7, I6);
    R6 = DM(-8, I6);
    R7 = DM(-9, I6);
    R9 = DM(-10, I6);
    R10 = DM(-11, I6);
    R11 = DM(-12, I6);
    R13 = DM(-13, I6);
    R14 = DM(-14, I6);
    R15 = DM(-15, I6);
    I0 = DM(-16, I6);
    I1 = DM(-17, I6);
    I2 = DM(-18, I6);
    I3 = DM(-19, I6);
    I5 = DM(-20, I6);
    M1 = DM(-21, I6);
    M2 = DM(-22, I6);
    M3 = DM(-23, I6);
    B3 = DM(-24, I6);
    I13 = DM(-25, I6);
    I12 = DM(M7, I6);                     // the return address, as the stock renderer's epilogue
    JUMP (M14, I12) (DB);
    R3 = DM(-6, I6);
    RFRAME;

_dv_stock:
    MODIFY(I7, -26) (NW);                 // the displaced first instruction of the stock renderer
    JUMP _fw_render_next;

// ---- helpers for machine routines (ordinary subroutines: CALL _dv_..., return with RTS) ----

// F0 = 2 ** F0 for F0 > -64 (relative error 8e-8: a degree-5 minimax polynomial of 2^f on [0, 1) and an exact
// scale by 2^floor). Clobbers R0-R3.
.GLOBAL _dv_exp2;
_dv_exp2:
    R1 = 0x42800000;                      // 64.0
    F0 = F0 + F1;
    R1 = TRUNC F0;
    F2 = FLOAT R1;
    F0 = F0 - F2;                         // f in [0, 1)
    R2 = 0x3af61942;
    F2 = F2 * F0;
    R3 = 0x3c1347f4;
    F2 = F2 + F3;
    F2 = F2 * F0;
    R3 = 0x3d64aa27;
    F2 = F2 + F3;
    F2 = F2 * F0;
    R3 = 0x3e75ead4;
    F2 = F2 + F3;
    F2 = F2 * F0;
    R3 = 0x3f31727b;
    F2 = F2 + F3;
    F2 = F2 * F0;
    R3 = 0x3f7fffff;
    F2 = F2 + F3;
    R3 = -64;
    R1 = R3 + R1;
    F0 = SCALB F2 BY R1;
    RTS;

// R0 = the unsigned 16-bit word k of the DSP's frame copy (R0 = k, 0..511); _dv_frame_sw sign-extends.
// Clobbers R0-R2, I4.
.GLOBAL _dv_frame_hw;
.GLOBAL _dv_frame_sw;
_dv_frame_hw:
    R1 = 0x1fe;
    R1 = R0 AND R1;
    R1 = LSHIFT R1 BY 1;
    R2 = _fw_frame;
    R1 = R2 + R1;
    I4 = R1;
    R1 = DM(I4, M5);
    R2 = FEXT R0 BY 0:1;
    R2 = LSHIFT R2 BY 4;
    R0 = 0x400;                           // len6 = 16 at Ry[11:6], bit6 below it
    R2 = R2 OR R0;
    R0 = FEXT R1 BY R2;
    RTS;
_dv_frame_sw:
    R1 = 0x1fe;
    R1 = R0 AND R1;
    R1 = LSHIFT R1 BY 1;
    R2 = _fw_frame;
    R1 = R2 + R1;
    I4 = R1;
    R1 = DM(I4, M5);
    R2 = FEXT R0 BY 0:1;
    R2 = LSHIFT R2 BY 4;
    R0 = 0x400;                           // len6 = 16 at Ry[11:6], bit6 below it
    R2 = R2 OR R0;
    R0 = FEXT R1 BY R2;
    R0 = LSHIFT R0 BY 16;
    R1 = -16;
    R0 = ASHIFT R0 BY R1;
    RTS;

.SECTION/DM seg_dmda;
.PKDATA _dv_table, 136;                    // 16 rows {type, pc} and one empty row that ends the scan
