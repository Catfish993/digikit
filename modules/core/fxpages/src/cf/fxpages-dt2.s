| core/fxpages on the Digitakt II (1.16, 1.17): the FX view pages of run-time effects.
| Design: docs/design/digikit-registry.md ("Effect pages"), docs/findings/61-fx-pages.md. The numbers (device id, page numbers, effect slots, value
| indices) are the device file's, through build/gen/device-dt2.inc (`patchkit gen`).
|
| Four hooks, each on the first whole instructions (6 bytes) of a site; the displaced instructions are not run, each routine redoes them:
|
|   fxp_acc    the page descriptor accessor `FUN_400c898a(int page) -> row` (rows of 0x2c bytes at a RAM table). A page number FX_PAGE_FIRST + k
|              (k < the effect slots) answers with row k of fxp_rows; everything else runs the stock code (moveq 0x20,D1; move.l 4(SP),D0; ...).
|   fxp_list   the FX view builder pushes its page count (moveq 3,D1) and then loads the address of its list of page numbers (move.l #list,D0). The
|              count is replaced on the stack and D0 is fxp_list_buf. While fxp_count is 0 (the runtime did not build a list) the stock list stays.
|   fxp_label  the tab label of FxPageView: after the tests for the pages 14 and 15 the stock code tests for 13 (`move.b #0xd,D1b; cmp.l D0,D1; bne.b`).
|              For an effect page the name of its row is pushed and the tail of the label code (`+0x1e`, which pushes the position and draws) runs; else the
|              stock test is redone and its `bne.b` follows.
|   fxp_graph  FxPageView::vfunc_4 skips the filter graph over the knobs 5 and 6 on page 15 (`cmp.l D0,D5; beq.w`); the effect pages skip it too.
|
| fxpages_info is the table the runtime reads (all words big endian).

    .include "device-dt2.inc"

    .equ ROW_SIZE, 0x2c         | the firmware's page descriptor row: name, title (char pointers), 9 codes
    .equ LIST_WORDS, 8          | the list buffer: the stock page numbers and the effect pages

    .text

    .globl fxp_acc
fxp_acc:
    move.l  4(%sp),%d0
    subi.l  #FX_PAGE_FIRST,%d0
    cmpi.l  #LIMIT_FX_SLOTS,%d0
    bcc.s   1f                          | not an effect page (also a number below the first)
    moveq   #ROW_SIZE,%d1
    muls.l  %d1,%d0
    addi.l  #fxp_rows,%d0
    rts
1:  move.l  4(%sp),%d0                  | the displaced instructions
    moveq   #0x20,%d1
    jmp     _fp_acc + 6

    .globl fxp_list
fxp_list:
    move.l  fxp_count,%d0
    beq.s   1f
    move.l  %d0,(%sp)                   | the count the builder pushed (the stock 3)
    move.l  #fxp_list_buf,%d0
    jmp     _fp_list + 6
1:  move.l  #_fp_stock,%d0              | the displaced instruction
    jmp     _fp_list + 6

    .globl fxp_label
fxp_label:
    move.l  %d0,%d1
    subi.l  #FX_PAGE_FIRST,%d1
    cmpi.l  #LIMIT_FX_SLOTS,%d1
    bcc.s   1f
    moveq   #ROW_SIZE,%d0
    muls.l  %d0,%d1
    addi.l  #fxp_rows,%d1
    movea.l %d1,%a0
    move.l  (%a0),-(%sp)                | the tab label, as the stock code pushes its string
    jmp     _fp_label + 0x1e
1:  moveq   #0xd,%d1                    | the displaced instructions (D0 is untouched)
    cmp.l   %d0,%d1
    jmp     _fp_label + 6

    .globl fxp_graph
fxp_graph:
    cmp.l   %d0,%d5                     | the stock compare: page 15
    beq.s   1f
    cmpi.l  #FX_PAGE_FIRST,%d0
    bcs.s   2f
1:  jmp     _fp_graph + 0x1c4           | no graph
2:  jmp     _fp_graph + 6

    .section .rodata
    .balign 4
    .globl fxpages_info
fxpages_info:
    .long   0x46585047                  |  0 'FXPG'
    .long   1                           |  4 version
    .long   DEVICE_ID                   |  8 device ([digikit] id: 1 = dt2)
    .long   fxp_rows                    | 12 the page rows (ROW_SIZE bytes each: name, title, 9 codes)
    .long   ROW_SIZE                    | 16 row size
    .long   FX_PAGE_FIRST               | 20 the page number of row 0
    .long   LIMIT_FX_SLOTS              | 24 rows (one for each effect slot)
    .long   fxp_list_buf                | 28 the list of page numbers the FX view gets (words)
    .long   LIST_WORDS                  | 32 its capacity
    .long   fxp_count                   | 36 the count cell (0: the stock list)
    .long   _fp_stock                   | 40 the stock list (page numbers, words)
    .long   _fp_stock_n                 | 44 the address of `moveq N,D1` in the builder: N is the byte at +1, the stock count
    .long   _fp_p_first                 | 48 the parameter code of knob 0 of effect row 0 (`params.fp0`)
    .long   8                           | 52 parameter rows for each effect row: 7 knobs and MIX
    .long   FX_ARRAY_OFF                | 56 byte offset of the kit's effect value array in the kit object
    .long   FX_VALUES_N                 | 60 number of value indices
    .long   FX_TPL_PERCENT              | 64 stock parameter codes whose rows the knobs copy
    .long   FX_TPL_BIPOLAR              | 68
    .long   FX_TPL_MIX                  | 72
    .long   FX_KIT_PTR                  | 76 the address of the firmware's pointer to the live kit object
    .byte   FX_VALUE_0, FX_VALUE_1, FX_VALUE_2, FX_VALUE_3, FX_VALUE_4, FX_VALUE_5, FX_VALUE_6, FX_VALUE_7           | 80 the value indices (32 bytes)
    .byte   FX_VALUE_8, FX_VALUE_9, FX_VALUE_10, FX_VALUE_11, FX_VALUE_12, FX_VALUE_13, FX_VALUE_14, FX_VALUE_15
    .byte   FX_VALUE_16, FX_VALUE_17, FX_VALUE_18, FX_VALUE_19, FX_VALUE_20, FX_VALUE_21, FX_VALUE_22, FX_VALUE_23
    .byte   FX_VALUE_24, FX_VALUE_25, FX_VALUE_26, FX_VALUE_27, FX_VALUE_28, FX_VALUE_29, FX_VALUE_30, FX_VALUE_31

    .bss
    .balign 4
    .globl fxp_rows, fxp_list_buf, fxp_count
fxp_rows:     .space ROW_SIZE * LIMIT_FX_SLOTS
fxp_list_buf: .space 4 * LIST_WORDS
fxp_count:    .space 4
