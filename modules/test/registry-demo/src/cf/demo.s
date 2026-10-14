/* test/registry-demo: the hook routine. It runs at the entry of a stock setter with every register as the caller
 * left it: count the call, then continue in the tail patchkit built (the displaced instructions, then a jump back
 * into the setter). addq sets only the condition codes, which the displaced `move.l` recomputes before they are used. */
    .text
    .even
    .globl rd_hook
rd_hook:
    addq.l  #1,rd_hits
    jmp     rd_tail

    .bss
    .even
    .globl rd_hits
rd_hits:
    .space  4
