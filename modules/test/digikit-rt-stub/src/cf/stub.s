| test/digikit-rt-stub: a stand-in for the runtime's C ABI (finding 28). Nothing in the firmware calls it. The emulator proof
| (tools/coldfire/digikit_menu_proof.py) pokes the addresses of these routines into core/digikit-menu's DKRT_API, then
| presses LOAD in the menu: stub_load logs the first 16 bytes of each path it is given and counts the calls.
    .text
    .balign 4
    .globl stub_load
stub_load:                          | int dkrt_load(const char *path): logs the path, returns 0
    move.l  4(%sp),%a0
    move.l  stub_count,%d1
    move.l  %d1,%d0
    andi.l  #7,%d0
    lsl.l   #4,%d0
    lea     stub_log,%a1
    adda.l  %d0,%a1
    move.l  (%a0)+,(%a1)+
    move.l  (%a0)+,(%a1)+
    move.l  (%a0)+,(%a1)+
    move.l  (%a0)+,(%a1)+
    addq.l  #1,%d1
    move.l  %d1,stub_count
    moveq   #0,%d0
    rts

    .globl stub_status
stub_status:                        | void dkrt_status(struct { abi, state, loaded, free_bytes } *)
    move.l  4(%sp),%a0
    moveq   #1,%d0
    move.l  %d0,(%a0)               | abi 1
    move.l  %d0,4(%a0)              | state 1
    move.l  stub_count,%d0
    move.l  %d0,8(%a0)              | loaded = calls so far
    move.l  #0x12000,%d0
    move.l  %d0,12(%a0)
    rts

    .data
    .balign 4
    .globl stub_count
stub_count:
    .long   0
    .globl stub_log
stub_log:
    .space  128
