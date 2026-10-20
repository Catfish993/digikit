/* test/dyn-fault: a module whose `init` never returns because the machine resets: it writes SOFTRST (0x80) into RCM.RCR
 * (0xEC090000), which is what the firmware's own exception path does when a module faults (finding 36, section 3: the inline
 * reset at 0x40001ab8; the emulator vectors an exception but flags it as a fault instead of running the handler, so this module
 * takes the same step itself). Used by the safe-mode scenario of tools/patchkit/pk_dkrt_boot.py. */
int dkm_init(const void *api)
{
    (void)api;
    *(volatile unsigned char *)0xEC090000 = 0x80;
    for (;;) {
    }
    return 0;
}
