// test/dn2-edge: a Digitone II voice routine that makes the frame's per-slot trig and release pulses audible.
//
// The ColdFire sends four 16-bit slot masks in the TX frame header (frame halfwords 0x11 .. 0x14, bytes 0x22 .. 0x28;
// bit v = voice slot v). Each is set for one frame (one DSP block) and then cleared: 0x11 and 0x13 at a note-on (the
// voice was allocated and its pitch, velocity and type words written), 0x12 and 0x14 at the voice's release. The
// routine keeps the previous bit of every (slot, mask) and, on a rising edge, writes a click of 0.5 into the voice
// buffer at sample 8 k (k = 0 .. 3 for the masks 0x11 .. 0x14); the buffer is zero otherwise. Counting the clicks in
// the PCM against the notes played shows whether the edge is seen once per note, also for a repeated note.
//
// Routine ABI: core/dsp-voice (DN2): R4 = voice buffer, R0 = type, R2 = slot v, R12 = 32.

.SECTION/PM seg_pmco;
.GLOBAL _ed_render;
.EXTERN _dv_frame_hw;

_ed_render:
    R5 = R2;                              // v
    R7 = R4;                              // buffer
    I0 = R7;
    R0 = 0;
    LCNTR = 32, DO _ed_zero UNTIL LCE;
_ed_zero: DM(I0, M6) = R0;
    R9 = 0;                               // k
_ed_loop:
    R0 = 0x11;
    R0 = R9 + R0;
    CALL _dv_frame_hw;                    // R0 = mask word k
    R1 = 0;
    R1 = R1 - R5;
    R0 = LSHIFT R0 BY R1;                 // bit v
    R1 = 1;
    R0 = R0 AND R1;                       // cur
    R10 = LSHIFT R5 BY 2;
    R11 = R10 + R9;
    R12 = _ed_prev;
    R13 = R12 + R11;
    I2 = R13;
    R3 = DM(0, I2);                       // prev
    DM(0, I2) = R0;
    R3 = NOT R3;
    R3 = R0 AND R3;                       // rising edge
    F3 = FLOAT R3;                        // 1.0 on an edge, else 0.0 (no branch: the state starts unknown)
    R1 = 0x3f000000;
    F1 = F3 * F1;
    R10 = LSHIFT R9 BY 3;
    R11 = R10 + R7;
    I0 = R11;
    DM(0, I0) = R1;
    R9 = R9 + 1;
    R0 = 4;
    COMP(R9, R0);
    IF LT JUMP _ed_loop;
    JUMP (M13, I13);

.SECTION/DM seg_dmda;
.PKDATA _ed_prev, 64;
