| core/ui: the one assembly shim. ColdFire, gas syntax, mcpu=54418.
|
| dk_make_sp(out, parent, view): the firmware's shared_ptr<View> constructor takes `out` in a0 and (parent, view) on the
|   stack; this adapts the cdecl call from C. It makes the control block (use count 1) and registers a detach callback on the
|   view that clears the shared_ptr at parent+0x90. Core/ui passes a parent that is a zeroed heap block, so that clear changes
|   nothing, and keeps the returned shared_ptr for the session: the firmware then never frees the view.
    .text
    .globl dk_make_sp
dk_make_sp:
    move.l  4(%sp),%a0
    move.l  12(%sp),-(%sp)          | view
    move.l  12(%sp),-(%sp)          | parent (8(%sp) on entry, shifted by the first push)
    jsr     cf.string.fn.shared_ptr.make
    addq.l  #8,%sp
    rts
