| core/digikit-menu: the hook at the end of the SETTINGS rebuild (finding 28). ColdFire, gas syntax, mcpu=54418.
|
| dk_hook: entered by `jmp` from the SETTINGS rebuild (hook at the last item, before the selection is restored). Live
|   registers there: a2 = the settings menu (`this`), d4 = the saved selection, sp = the rebuild's frame. dk_add_item (menu.c)
|   keeps d2-d7/a2-a6 (gcc cdecl); d0/d1/a0/a1 are dead at the site (the displaced code reloads a0). It returns, then the displaced
|   instructions run in the hook tail, which jumps back into the rebuild.
    .include "dk_fw_menu.inc"
    .text
    .globl dk_hook
dk_hook:
    dk_fw_menu_settings_list_tail_hook dk_add_item, _dk_tail
