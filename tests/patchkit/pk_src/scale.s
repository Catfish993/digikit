.SECTION/PM seg_pmco;
.GLOBAL _scale_buf;
.EXTERN _fw_hook;
_scale_buf:
    I0 = R4;
    M0 = 1;
    L0 = 0;
    F1 = 2.5;
    LCNTR = R8, DO scale_end UNTIL LCE;
        F2 = DM(0, I0);
        F2 = F2*F1;
scale_end: DM(I0, M0) = F2;
    CALL _bias;
    JUMP _fw_hook;
_bias:
    R0 = 7;
    RTS;
