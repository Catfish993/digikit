| core/digikit-menu: the two assembly shims (finding 28). ColdFire, gas syntax, mcpu=54418.
|
| dk_hook: entered by `jmp` from the SETTINGS rebuild (hook at the last item, before the selection is restored). Live
|   registers there: a2 = the settings menu (`this`), d4 = the saved selection, sp = the rebuild's frame. The Rust
|   function keeps d2-d7/a2-a6 (gcc cdecl); d0/d1/a0/a1 are dead at the site (the displaced code reloads a0). It
|   returns, then the displaced instructions run in the hook tail, which jumps back into the rebuild.
|
| dk_make_sp(out, parent, view): the firmware's shared_ptr<View> constructor takes `out` in a0 and (parent, view) on the
|   stack; this adapts the cdecl call from Rust.
    .text
    .globl dk_hook
dk_hook:
    move.l  %a2,-(%sp)
    jsr     dk_add_item
    addq.l  #4,%sp
    jmp     _dk_tail

    .globl dk_make_sp
dk_make_sp:
    move.l  4(%sp),%a0
    move.l  12(%sp),-(%sp)          | view
    move.l  12(%sp),-(%sp)          | parent (8(%sp) on entry, shifted by the first push)
    jsr     _fw_sp
    addq.l  #8,%sp
    rts

| dk_popup_make(out, textptr): the firmware's make_shared<PopupWindow>(const char *const &) takes `out` in a0 and the
|   reference (a pointer to the message pointer) on the stack.
    .globl dk_popup_make
dk_popup_make:
    move.l  4(%sp),%a0
    move.l  8(%sp),-(%sp)
    jsr     _fw_popup_make
    addq.l  #4,%sp
    rts
