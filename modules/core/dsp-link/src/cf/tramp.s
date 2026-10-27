/* core/dsp-link: the hook on the block frame sender (docs/findings/31-dsp-link.md).
 *
 * dl_frame_hook replaces the target of the `jsr` that calls the stock frame driver from the block interrupt handler. The
 * four arguments are on the stack as the handler pushed them (data bytes, data, frame bytes, record copy). It runs dl_tick
 * with the same arguments, then continues in the stock driver as if it had been called directly: the stack is as the handler
 * left it. d0/d1/a0/a1 are the registers the C ABI lets dl_tick clobber; the handler has nothing live in them at this call
 * (it has just pushed the arguments, and the driver returns nothing). dl_stock_send is the stock driver (anchor cf.link.fn.frame). */
    .text
    .even
    .globl dl_frame_hook
dl_frame_hook:
    lea     -16(%sp),%sp
    movem.l %d0-%d1/%a0-%a1,(%sp)
    move.l  32(%sp),-(%sp)      /* record copy */
    move.l  32(%sp),-(%sp)      /* frame bytes */
    move.l  32(%sp),-(%sp)      /* data */
    move.l  32(%sp),-(%sp)      /* data bytes */
    jsr     dl_tick
    lea     16(%sp),%sp
    movem.l (%sp),%d0-%d1/%a0-%a1
    lea     16(%sp),%sp
    jmp     dl_stock_send
