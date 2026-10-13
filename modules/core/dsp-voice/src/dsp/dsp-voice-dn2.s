// core/dsp-voice on the Digitone II: render hook for machine types the FM engine does not know.
//
// The Digitone II engine (FUN_1c8ef1) is a stage pipeline over 16 voice slots: slot v has a 32-float voice buffer
// (48 kHz, one DSP block) at `0x804acf90 + 0x80 v` and a frame slot (pitch halfword 1 + v = note << 8, velocity 26 + v
// = velocity << 8, machine type 74 + v). The FM synthesis of each voice writes its buffer; the stages that follow
// (gain, amplitude envelope and its gate, filter, overdrive and the rest of the track chain, then the upsample to the
// 64-float block at 96 kHz, the sends, the master mix) work on the same buffers in place. A slot whose machine type is
// above 4 is skipped by the synthesis like MIDI and its buffer holds nothing, so a routine that fills it makes the
// slot sound through everything after the synthesis (findings 06, "Per-voice hook on DN2").
//
// The hook is FUN_b8251b, a 55-instruction helper the engine calls once per voice buffer after the synthesis, with
// R15 = 16 - v (a count that runs 16, 15, ... 1), R8 = R12 = the voice buffer, R4 = the voice's control block. patchkit
// overwrites its first instruction (`I7 = MODIFY(I7, -18)`, 48 bits) with `JUMP _dv_entry`. This file saves every
// register it touches in `_dv_save`, runs the routine registered for the slot's type, restores the registers, executes
// the displaced instruction and continues in the stock helper at its second instruction. A patched image with no
// registered type renders exactly the stock audio. The helper has a second user on another path (a tail jump from
// FUN_b8258f); that call has R8 != R12 and is passed through.
//
// Routine ABI (entered with JUMP (M13, I12); I13 holds the return address, the routine ends with JUMP (M13, I13);
// ordinary CALL/RTS pairs for its own subroutines are fine):
//   in   R4 = the slot's voice buffer (32 floats, written in full), R0 = type, R2 = slot v (0..15), R12 = 32
//   may clobber  R0-R15, I0-I5, I12, I13, M1-M4 (saved and restored here); must keep I6-I11, I14, I15, M0, M5-M15,
//        the L registers and MODE1 (SIMD off). M5 = 0, M6 = 1, M7 = -1, M13 = 0, M14 = 1, M15 = -1 hold on entry.
//   frame words (`_dv_frame_hw`, halfword index k of the frame copy, big-endian values): pitch 1 + v (note << 8),
//   velocity 26 + v (velocity << 8), type 74 + v. There is no voice record to mark: the routine keeps its own
//   per-slot state. The voice's amplitude envelope, gate and level are the engine's: what the routine writes is the
//   signal before them.
//
// Called once per DSP block and slot. A slot with the same type is called with the same arguments every block.

.SECTION/PM seg_pmco;
.GLOBAL _dv_entry;
.EXTERN _fw_frame;
.EXTERN _fw_render_next;
.EXTERN _dv_table;

_dv_entry:
    DM(_dv_s_r0) = R0;
    R0 = I4;
    DM(_dv_s_i4) = R0;
    I4 = _dv_save;
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
    DM(15, I4) = I0;
    DM(16, I4) = I1;
    DM(17, I4) = I2;
    DM(18, I4) = I3;
    DM(19, I4) = I5;
    DM(20, I4) = I12;
    DM(21, I4) = I13;
    DM(22, I4) = M1;
    DM(23, I4) = M2;
    DM(24, I4) = M3;
    DM(25, I4) = M4;
    // the engine's per-voice call: R8 = R12 = the voice buffer (its low bits 0x10 + 0x80 k), R15 = 16 - v in 1..16
    COMP(R8, R12);
    IF NE JUMP _dv_done;
    R0 = 0x7f;
    R1 = R12 AND R0;
    R0 = 0x10;
    COMP(R1, R0);
    IF NE JUMP _dv_done;
    R0 = 16;
    R1 = R0 - R15;                        // v
    IF LT JUMP _dv_done;
    R0 = 15;
    COMP(R1, R0);
    IF GT JUMP _dv_done;
    DM(_dv_t) = R1;
    DM(_dv_buf) = R12;
    R0 = 74;
    R0 = R1 + R0;                         // halfword 74 + v of the frame copy: the slot's machine type
    R1 = 0xffe;
    R1 = R0 AND R1;
    R1 = LSHIFT R1 BY 1;
    R2 = _fw_frame;
    R1 = R2 + R1;
    I4 = R1;
    R1 = DM(I4, M5);
    R2 = FEXT R0 BY 0:1;
    R2 = LSHIFT R2 BY 4;
    R0 = 0x400;
    R2 = R2 OR R0;
    R1 = FEXT R1 BY R2;                   // the type
    I4 = _dv_table;
_dv_scan:
    R0 = DM(I4, M6);                      // row type; 0 ends the table (the last row is always empty)
    R2 = DM(I4, M6);                      // row pc
    R0 = PASS R0;
    IF EQ JUMP _dv_done;
    COMP(R0, R1);
    IF NE JUMP _dv_scan;
    I12 = R2;                             // the routine's pc
    R0 = R1;                              // type
    R2 = DM(_dv_t);
    R4 = DM(_dv_buf);
    R12 = 32;
    I13 = _dv_done;
    JUMP (M13, I12);                      // the routine returns with JUMP (M13, I13)
_dv_done:
    I4 = _dv_save;
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
    R0 = DM(_dv_s_i4);
    I4 = R0;
    R0 = DM(_dv_s_r0);
    MODIFY(I7, -18) (NW);                 // the displaced first instruction of the stock helper
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

// R0 = the unsigned 16-bit word k of the DSP's frame copy (R0 = k, 0..4095; the frame copy is about 1,350 halfwords); _dv_frame_sw sign-extends.
// Clobbers R0-R2, I4.
.GLOBAL _dv_frame_hw;
.GLOBAL _dv_frame_sw;
_dv_frame_hw:
    R1 = 0xffe;
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
    R1 = 0xffe;
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
.PKDATA _dv_save, 128;                     // R1-R15, I0-I3, I5, I12, I13, M1-M4
.PKDATA _dv_s_r0, 4;
.PKDATA _dv_s_i4, 4;
.PKDATA _dv_t, 4;
.PKDATA _dv_buf, 4;
