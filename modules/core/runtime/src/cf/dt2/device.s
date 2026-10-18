/* core/runtime: the device object. The id, the name and the PEEK ranges are the device file's (digitakt-ii.toml [digikit] id, name and peek) through
 * build/gen/device-dt2.inc, which `patchkit gen` writes. */
    .include "device-dt2.inc"
    .section .rodata
    .even
    .globl dkrt_device_id
dkrt_device_id:
    .long DEVICE_ID
    .globl dkrt_device_name
dkrt_device_name:
    .long DEVICE_NAME_WORD
    .globl dkrt_peek_n
dkrt_peek_n:
    .long PEEK_N
    .globl dkrt_peek_ranges
dkrt_peek_ranges:
    .long PEEK_LO_0, PEEK_HI_0, PEEK_LO_1, PEEK_HI_1
    .long PEEK_LO_2, PEEK_HI_2, PEEK_LO_3, PEEK_HI_3
