/* core/health: the two hooks (docs/findings/41-digikit-health.md). ColdFire, gas syntax, mcpu=54418.
 *
 * health_idle_hook replaces the last call of the boot thread, `jsr <create the main-OS task>` (a `hook` op, nothing displaced). It
 * makes that call itself, then counts forever (`health_idle_line`, one cache line of its own): the thread's old end was an empty loop, and it runs only when no task is ready.
 * health_create_main is the called function (anchor health.fw.create_main, resolved at patch time).
 *
 * health_block_hook is a `hook` op at the entry of the block interrupt handler (the displaced `link.w; movem.l` run in the tail
 * health_block_next). The interrupt frame is on the stack; d0/d1/a0/a1 are saved, d2-d7/a2-a6 are the C function's to keep. */
    .text
    .even
    .globl health_idle_hook
health_idle_hook:
    jsr     health_create_main
1:  addq.l  #1,health_idle_line
    bra.s   1b

    .globl health_block_hook
health_block_hook:
    lea     -16(%sp),%sp
    movem.l %d0-%d1/%a0-%a1,(%sp)
    jsr     health_block
    movem.l (%sp),%d0-%d1/%a0-%a1
    lea     16(%sp),%sp
    jmp     health_block_next
