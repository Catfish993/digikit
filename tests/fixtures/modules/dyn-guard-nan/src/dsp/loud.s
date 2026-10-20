// Guard fixture (rig only): outputs 100.0 in every sample while the voice is active, silence when idle. Entry as nan.s.

.SECTION/PM seg_pmco;
.GLOBAL _ld_render;

_ld_render:
    I0 = R8;
    I1 = R4;
    MODIFY(I1, 440);
    R9 = DM(I1, M5);
    R0 = 0xff;
    R0 = R9 AND R0;
    R1 = 0x42c80000;                      // 100.0
    IF EQ R1 = PASS R0;
    LCNTR = 32, DO _ld_loop UNTIL LCE;
_ld_loop: DM(I0, M6) = R1;
    JUMP (M13, I13);
