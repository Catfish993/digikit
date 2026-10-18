/* core/runtime: the device object. The id and the name are the device file's (digitone-ii.toml [digikit] id and name) through
 * build/gen/device-dn2.inc, which `patchkit gen` writes. */
    .include "device-dn2.inc"
    .section .rodata
    .even
    .globl dkrt_device_id
dkrt_device_id:
    .long DEVICE_ID
    .globl dkrt_device_name
dkrt_device_name:
    .long DEVICE_NAME_WORD
