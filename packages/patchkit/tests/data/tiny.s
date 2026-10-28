| Test input of packages/patchkit/tests/build.rs, assembled with
|   out/tools/m68k-elf/bin/m68k-elf-as -mcpu=54418 -o tiny.o tiny.s
| One text section with an absolute and a PC-relative reference to an undefined symbol, a section-relative reference,
| a data word that holds an address, and a bss area.
    .text
    .globl  tiny_call
tiny_call:
    jsr     ext_fn
    bsr.l   ext_near
    move.l  #tiny_word,%d0
    rts

    .section .rodata.tiny
    .globl  tiny_word
tiny_word:
    .long   0x12345678
    .long   tiny_call

    .bss
    .balign 4
    .lcomm  tiny_scratch,16
