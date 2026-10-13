| Assembly stub: u32 pk_rev(u32 x) = byte-reversed x (ISA_C BYTEREV) with the bits of the low byte reversed.
    .text
    .globl pk_rev
    .type pk_rev,@function
pk_rev:
    move.l 4(%sp),%d0
    byterev.l %d0
    move.l %d0,%d1
    bitrev.l %d1
    ff1.l %d1
    add.l %d1,%d0
    rts
    .size pk_rev,.-pk_rev
