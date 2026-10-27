| test/ui-demo: the hook at the end of the SETTINGS rebuild (as core/digikit-menu). ColdFire, gas syntax, mcpu=54418.
    .include "dk_fw_menu.inc"
    .text
    .globl dk_hook
dk_hook:
    dk_fw_menu_settings_list_tail_hook dk_add_item, _dk_tail
