| core/fxpages on the Digitone II (1.12): the FX view pages of run-time effects.
| Design: docs/design/digikit-registry.md ("Effect pages"), docs/findings/61-fx-pages.md. The numbers (device id, page numbers, effect slots, value
| indices) are the device file's, through build/gen/device-dn2.inc (`patchkit gen`).
|
| Four hooks, each on the first whole instructions (6 bytes) of a site; the displaced instructions are not run, each routine redoes them:
|
|   fxp_acc    the page descriptor accessor `FUN_400c25d8(int page) -> row` (rows of 0x2c bytes at a RAM table). A page number FX_PAGE_FIRST + k
|              (k < the effect slots) answers with row k of fxp_rows; everything else runs the stock code (moveq FX_PAGE_FIRST-1,D1; move.l 4(SP),D0; ...).
|   fxp_list   the FX view builder pushes its page count (moveq 3,D1) and then loads the address of its list of page numbers (move.l #list,D0). The
|              count is replaced on the stack and D0 is fxp_list_buf. While fxp_count is 0 (the runtime did not build a list) the stock list stays.
|   fxp_label  the tab label of FxPageView: after the tests for the pages 14 and 15 the stock code tests for 13 (`move.b #0xd,D1b; cmp.l D0,D1; bne.b`).
|              For an effect page the name of its row is pushed and the tail of the label code (`+0x1e`, which pushes the position and draws) runs; else the
|              stock test is redone and its `bne.b` follows.
|   fxp_graph  FxPageView::vfunc_4 skips the filter graph over the knobs 5 and 6 on page 15 (`cmp.l D0,D5; beq.w`); the effect pages skip it too.
|
| Three more hooks keep the effect values with the kit (persistence, finding 61). The firmware's kit loader and saver call one function each for the effect value
| array (the kit's "Fx" object of 101 halfwords) and its storage record of 0xa0 bytes; the stock functions move 46 halfwords by tables and ignore 28 spare
| halfwords at FX_STORE_OFF. The hooks sit on the last instruction of each function (and of the initialiser of the array), so every kit load and save goes through them
| (Load Kit, Save Kit, a pattern's kit, a project): the stock tables stay as they are, and so does the map of the locks that shares them.
|
|   fxp_save   writes the values of the effect rows (the array indices of `fxp_vidx`) and then one 16-bit effect ID for each effect row into the spare halfwords:
|              values first (FX_VALUES_N halfwords), IDs after them (FX_VALUES_N / 8). A row with no effect loaded writes ID 0 and zero values.
|   fxp_load   for each row with an effect (ID in `fxp_ids`): the stored values when the stored ID is the same, else the effect's defaults (`fxp_defs`), into the array.
|              A value above 0x7f00 is not a value a knob makes: it is replaced by the default. A row with no effect is not touched (the stock loader zeroed it).
|
|   fxp_init   the kit's effect value array is initialised (FUN_400e7140, called by the kit initialiser for a new kit, a fresh kit that Load Kit then fills, a project, and by the
|              loader when it refuses a record): each row with an effect gets its defaults (`fxp_defs`) in the array; the rest stays 0.
|
| Both hooks put a mark (FXP_MARK) into the array index FX_MARK_INDEX (an index nothing uses; the init zeroes it, the saver does not save it) when a row has an effect. A kit object that
| has no mark has not been through either hook since the effect was loaded (another pattern's kit, created before the module was): the runtime gives it the defaults when it first sees it.
|
| The runtime fills `fxp_ids` and `fxp_defs` when it registers an effect (the ID is the packer's: patchkit `register::effect_id`). The counters and pointers (`fxp_nload`,
| `fxp_nsave`, `fxp_ninit`, `fxp_last_load`, `fxp_load_src`, `fxp_save_dst`, `fxp_last_save`) are for tests and the debugger.
|
| fxpages_info is the table the runtime reads (all words big endian).

    .include "device-dn2.inc"

    .equ ROW_SIZE, 0x2c         | the firmware's page descriptor row: name, title (char pointers), 9 codes
    .equ LIST_WORDS, 8          | the list buffer: the stock page numbers and the effect pages
    .equ FXP_MARK, 0x46a7       | the value of the mark in the array: the rows hold values (any other value: the array was not given its defaults)
    .equ MARK_OFF, 2 * FX_MARK_INDEX
    .equ ROWS, FX_VALUES_N / 8  | effect rows with values in the kit (8 values each)
    .equ ID_OFF, FX_STORE_OFF + 2 * FX_VALUES_N   | the first effect ID in the storage record

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
    moveq   #FX_PAGE_FIRST-1,%d1         | (the stock bound: the pages below FX_PAGE_FIRST)
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
1:  move.l   _fp_stock,%d1               | the displaced instructions: the first page of the stock list (DEL; D0 is untouched)
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

    .globl fxp_load
fxp_load:
    move.l  0x98(%a0),FX_TAIL_OFF(%a1)  | the displaced instruction (A0 = storage record, A1 = the array)
    lea     -20(%sp),%sp                | ColdFire movem has no predecrement form
    movem.l %d2-%d4/%a4-%a5,(%sp)
    move.l  %a1,fxp_last_load
    move.l  %a0,fxp_load_src
    addq.l  #1,fxp_nload
    lea     ID_OFF(%a0),%a3             | the IDs the kit stored
    lea     FX_STORE_OFF(%a0),%a0       | the values it stored
    lea     fxp_ids,%a2
    lea     fxp_defs,%a4
    lea     fxp_vidx,%a5
    moveq   #ROWS,%d0
1:  mvz.w   (%a2)+,%d1                  | the effect in this row now (0: none)
    mvz.w   (%a3)+,%d2                  | the effect the kit has stored here
    tst.l   %d1
    bne.s   2f
    lea     16(%a0),%a0                 | no effect: leave the row (the stock loader zeroed it)
    lea     16(%a4),%a4
    lea     8(%a5),%a5
    bra.s   7f
2:  move.w  #FXP_MARK,MARK_OFF(%a1)     | an effect is here: the array holds its row (stored values or defaults) from now on
    moveq   #0,%d3                      | d3 = 1: the stored values are this effect's
    cmp.l   %d1,%d2
    bne.s   3f
    moveq   #1,%d3
3:  moveq   #8,%d4
4:  mvz.w   (%a0)+,%d1                  | the stored value
    mvz.w   (%a4)+,%d2                  | the default
    tst.l   %d3
    bne.s   5f
    move.l  %d2,%d1                     | another effect (or none) was here: the default
5:  cmpi.l  #0x7f00,%d1                 | a raw value is 0 .. 0x7f00
    bls.s   6f
    move.l  %d2,%d1
6:  mvz.b   (%a5)+,%d2                  | the array index of this value
    move.w  %d1,(0,%a1,%d2.l*2)
    subq.l  #1,%d4
    bne.s   4b
7:  subq.l  #1,%d0
    bne.s   1b
    movem.l (%sp),%d2-%d4/%a4-%a5
    lea     20(%sp),%sp
    jmp     _fp_load + 6

    .globl fxp_save
fxp_save:
    move.l  FX_TAIL_OFF(%a3),0x98(%a2)  | the displaced instruction (A2 = storage record, A3 = the array)
    lea     -20(%sp),%sp
    movem.l %d2-%d4/%a4-%a5,(%sp)
    move.l  %a3,fxp_last_save
    move.l  %a2,fxp_save_dst
    addq.l  #1,fxp_nsave
    lea     ID_OFF(%a2),%a1             | where the IDs go
    lea     FX_STORE_OFF(%a2),%a0       | where the values go
    lea     fxp_ids,%a4
    lea     fxp_vidx,%a5
    moveq   #ROWS,%d0
1:  mvz.w   (%a4)+,%d1                  | the effect in this row (0: none)
    move.w  %d1,(%a1)+
    moveq   #8,%d2
2:  mvz.b   (%a5)+,%d3                  | the array index of this value
    moveq   #0,%d4
    tst.l   %d1
    beq.s   3f
    move.w  (0,%a3,%d3.l*2),%d4
3:  move.w  %d4,(%a0)+
    subq.l  #1,%d2
    bne.s   2b
    subq.l  #1,%d0
    bne.s   1b
    movem.l (%sp),%d2-%d4/%a4-%a5
    lea     20(%sp),%sp
    jmp     _fp_save + 6

    .globl fxp_init
fxp_init:
    tst.l   %a2                         | the function was given no array: nothing to do
    beq.s   9f
    addq.l  #1,fxp_ninit
    lea     -20(%sp),%sp
    movem.l %d2-%d4/%a3-%a4,(%sp)
    lea     fxp_ids,%a0
    lea     fxp_defs,%a1
    lea     fxp_vidx,%a3
    moveq   #ROWS,%d0
1:  mvz.w   (%a0)+,%d1                  | the effect in this row (0: none)
    bne.s   2f
    lea     16(%a1),%a1                 | no effect: the row stays 0
    lea     8(%a3),%a3
    bra.s   4f
2:  move.w  #FXP_MARK,MARK_OFF(%a2)
    moveq   #8,%d4
3:  mvz.w   (%a1)+,%d2                  | the default
    mvz.b   (%a3)+,%d3                  | the array index of this value
    move.w  %d2,(0,%a2,%d3.l*2)
    subq.l  #1,%d4
    bne.s   3b
4:  subq.l  #1,%d0
    bne.s   1b
    movem.l (%sp),%d2-%d4/%a3-%a4
    lea     20(%sp),%sp
9:  movem.l (%sp),%d2/%a2-%a5          | the displaced instructions
    lea     0x20(%sp),%sp
    jmp     _fp_init + 8

    .section .rodata
    .balign 4
    .globl fxpages_info
fxpages_info:
    .long   0x46585047                  |  0 'FXPG'
    .long   1                           |  4 version
    .long   DEVICE_ID                   |  8 device ([digikit] id: 2 = dn2)
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
fxp_vidx:
    .byte   FX_VALUE_0, FX_VALUE_1, FX_VALUE_2, FX_VALUE_3, FX_VALUE_4, FX_VALUE_5, FX_VALUE_6, FX_VALUE_7           | 80 the value indices (32 bytes)
    .byte   FX_VALUE_8, FX_VALUE_9, FX_VALUE_10, FX_VALUE_11, FX_VALUE_12, FX_VALUE_13, FX_VALUE_14, FX_VALUE_15
    .byte   FX_VALUE_16, FX_VALUE_17, FX_VALUE_18, FX_VALUE_19, FX_VALUE_20, FX_VALUE_21, FX_VALUE_22, FX_VALUE_23
    .byte   FX_VALUE_24, FX_VALUE_25, FX_VALUE_26, FX_VALUE_27, FX_VALUE_28, FX_VALUE_29, FX_VALUE_30, FX_VALUE_31
    .long   fxp_ids                     | 112 the effect ID of each row (halfwords; 0: no effect), written by the runtime
    .long   fxp_defs                    | 116 the default raw value of each of the FX_VALUES_N values (halfwords), written by the runtime
    .long   fxp_last_load               | 120 the array the last kit load wrote (the load hook)
    .long   fxp_nload                   | 124 kit loads that went through the hook
    .long   fxp_nsave                   | 128 kit saves that went through the hook
    .long   fxp_last_save               | 132 the array the last save read
    .long   fxp_load_src                | 136 the storage record the last load read (the bytes the firmware read from the kit file)
    .long   fxp_save_dst                | 140 the storage record the last save wrote
    .long   fxp_ninit                   | 144 kit initialisations that went through the init hook
    .long   FX_MARK_INDEX               | 148 the array index of the mark
    .long   FXP_MARK                    | 152 the mark's value

    .bss
    .balign 4
    .globl fxp_rows, fxp_list_buf, fxp_count, fxp_ids, fxp_defs, fxp_last_load, fxp_nload, fxp_nsave, fxp_last_save, fxp_load_src, fxp_save_dst, fxp_ninit
fxp_rows:     .space ROW_SIZE * LIMIT_FX_SLOTS
fxp_list_buf: .space 4 * LIST_WORDS
fxp_count:    .space 4
fxp_ids:      .space 2 * ROWS
fxp_defs:     .space 2 * FX_VALUES_N
    .balign 4
fxp_last_load: .space 4
fxp_nload:    .space 4
fxp_nsave:    .space 4
fxp_last_save: .space 4
fxp_load_src: .space 4
fxp_save_dst: .space 4
fxp_ninit:    .space 4
