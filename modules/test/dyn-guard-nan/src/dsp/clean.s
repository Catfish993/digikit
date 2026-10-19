// Guard fixture (rig only): a clean voice. While active it outputs the ramp 0.125 k (k = 0 .. 31, up to 3.875: inside the guard's bound of 8.0) and
// marks the voice running (byte +0x1b9); idle it outputs silence. It also copies the machine knobs it sees (the frame words of knob 0 .. 7 of its
// track) to _cl_seen, to show what the guard passes on. Entry as nan.s (R2 = track).

.SECTION/PM seg_pmco;
.GLOBAL _cl_render;
.EXTERN _dv_frame_hw;

_cl_render:
    R5 = R2;
    I0 = R8;
    I1 = R4;
    MODIFY(I1, 440);
    R9 = DM(I1, M5);
    R0 = 0xff;
    R0 = R9 AND R0;
    IF EQ JUMP _cl_idle;
    R0 = 0x5a00;
    R9 = R9 OR R0;
    DM(I1, M5) = R9;                      // the voice runs
    // the knobs: halfwords 0x6d + 48 t + 0, 1, 2, 3, 6, 7, 8, 9
    R0 = LSHIFT R5 BY 5;
    R1 = LSHIFT R5 BY 4;
    R0 = R1 + R0;
    R1 = 0x6d;
    R13 = R1 + R0;
    I2 = _cl_seen;
    R0 = PASS R13;
    CALL _dv_frame_hw;
    DM(0, I2) = R0;
    R0 = R13 + 1;
    CALL _dv_frame_hw;
    DM(1, I2) = R0;
    R1 = 2;
    R0 = R13 + R1;
    CALL _dv_frame_hw;
    DM(2, I2) = R0;
    R1 = 3;
    R0 = R13 + R1;
    CALL _dv_frame_hw;
    DM(3, I2) = R0;
    R1 = 6;
    R0 = R13 + R1;
    CALL _dv_frame_hw;
    DM(4, I2) = R0;
    R1 = 7;
    R0 = R13 + R1;
    CALL _dv_frame_hw;
    DM(5, I2) = R0;
    R1 = 8;
    R0 = R13 + R1;
    CALL _dv_frame_hw;
    DM(6, I2) = R0;
    R1 = 9;
    R0 = R13 + R1;
    CALL _dv_frame_hw;
    DM(7, I2) = R0;
    R1 = 0;                               // k = 0, 0.125 k
    R2 = 0x3e000000;                      // 0.125
    F3 = PASS F1;
    LCNTR = 32, DO _cl_loop UNTIL LCE;
        DM(I0, M6) = R3;
_cl_loop: F3 = F3 + F2;
    JUMP (M13, I13);
_cl_idle:
    R0 = 0;
    LCNTR = 32, DO _cl_zloop UNTIL LCE;
_cl_zloop: DM(I0, M6) = R0;
    JUMP (M13, I13);

.SECTION/DM seg_dmda;
.PKDATA _cl_seen, 32;
