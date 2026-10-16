/* core/runtime: hook trampoline and cache maintenance (docs/design/digikit-runtime.md).
 *
 * dkrt_loop_hook replaces the target of the `jsr` at the top of the UI task's event loop (the call that pops the next event
 * from the UI queue; the queue pointer is on the stack, the stock function returns the event in d0). It runs the runtime's
 * tick, then continues in the stock pop as if it had been called directly: the stack is as the caller left it.
 * d0/d1/a0/a1 are the registers the C ABI lets dkrt_tick clobber; the loop has nothing live in them at this call (it
 * has just pushed the argument, and the result comes back in d0). d2-d7/a2-a6 are preserved by the C function.
 * fw_queue_pop is the stock function (anchor dkrt.fw.queue_pop).
 */
    .text
    .even
    .globl dkrt_loop_hook
dkrt_loop_hook:
    lea     -16(%sp),%sp
    movem.l %d0-%d1/%a0-%a1,(%sp)
    jsr     dkrt_tick
    movem.l (%sp),%d0-%d1/%a0-%a1
    lea     16(%sp),%sp
    jmp     fw_queue_pop

/* void dkrt_flush(u32 start, u32 len): CPUSHL BC for every 16-byte line of [start, start+len): write back dirty data
 * lines and invalidate the line in the data and instruction caches. Needed after code or data that will be fetched as
 * instructions has been written through the data cache (finding 07: the cache is not coherent on hardware). */
    .globl dkrt_flush
dkrt_flush:
    move.l  4(%sp),%a0
    move.l  8(%sp),%d1
    move.l  %a0,%d0
    and.l   #15,%d0
    add.l   %d0,%d1             /* include the bytes before the first line start */
    move.l  %a0,%d0
    and.l   #-16,%d0
    move.l  %d0,%a0
    add.l   #15,%d1
    lsr.l   #4,%d1              /* number of lines */
    beq.s   2f
1:  cpushl  %bc,(%a0)
    lea     16(%a0),%a0
    subq.l  #1,%d1
    bne.s   1b
2:  rts
