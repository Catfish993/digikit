| PC-relative calls to the firmware: R_68K_PC32 (bsr.l) and R_68K_PC16 (bsr.w, only in reach near the module).
    .text
    .globl pk_far
    .globl pk_near
pk_far:
    move.l 4(%sp),-(%sp)
    bsr.l fw_field_0c
    addq.l #4,%sp
    rts
pk_near:
    bsr.w near_ext
    rts
