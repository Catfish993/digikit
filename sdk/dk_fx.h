/* sdk/dk_fx.h -- the FX contract, version DK_FX_V1 (docs/design/module-sdk.md 4.2).
   An effect processes the final stereo block of the device (the master, before the USB pack and the DAC converter).
   The numbers are in sdk/sdk.toml [fx], from which `patchkit gen` writes sdk/dk_fx_consts.h (included here) and sdk/dk_fx.inc for assembly.
   Do not include dk_fx.inc and dk_machine.inc in one assembly file (DK_BLOCK and DK_LEVEL_BOUND_BITS are in both).
   Constants: DK_FX_V1 contract version; DK_BLOCK samples per channel and call, 48 kHz, both devices; DK_FX_BLOCK floats of a block,
   planar, 32 left, then 32 right; DK_FX_MAX_PARAMS hard cap on parameter words (the 7 knobs of a page are p0 to p6, v1 rows had 4;
   MIX is not one: the adapter owns it); DK_FX_MAX effects in the chain (a device's `[limits] fx_slots` is at most this);
   DK_LEVEL_BOUND |sample| above this is a fault (+18 dB over full scale); DK_FX_MAGIC 'DKFX', the first word of dk_fx_desc.
   dk_fx_status, returned by process: DK_FX_OK out written; a negative number is a fault: the block plays dry. */
#include <stdint.h>
#include "dk_fx_consts.h"

typedef struct dk_fx_in {               /* per call, built by the adapter                                     */
    const float *in;                    /* the block before this effect, DK_FX_BLOCK floats, read only        */
    float       *out;                   /* process writes DK_FX_BLOCK floats here (adapter scratch)           */
    uint32_t     block;                 /* blocks since the start of the DSP, wraps at 2^32                   */
} dk_fx_in;

/* One per module in read-only data, found by the module's registry row (patched in) or its `[[fx]]` record (run-time, DSP section version 4
   of the .dkm). The row holds DK_FX_MAX_PARAMS parameter words, p0 to p7: word i of `params` is p_i, all 0 unless the row says otherwise (a run-time
   effect's record gives defaults). The adapter passes the words as they are; n_params says how many the effect reads. No function pointers. */
typedef struct dk_fx_desc {
    uint32_t magic;                     /* DK_FX_MAGIC                                                        */
    uint32_t contract;                  /* DK_FX_V1                                                           */
    uint32_t n_params;                  /* 0 .. DK_FX_MAX_PARAMS                                              */
    uint32_t state_bytes;               /* one instance, a multiple of 4; zeroed when the effect switches on  */
    uint32_t max_cycles_block;          /* worst-case instructions of one process call                        */
    uint32_t max_call_depth;            /* nested CALLs the module uses                                       */
    uint32_t stack_bytes;               /* data stack the module uses                                         */
} dk_fx_desc;

/* Entry point. SHARC+ assembly, v1: the adapter enters with `I13 = return address; JUMP (M13, I12)` and the module returns with
   `JUMP (M13, I13)`. Arguments R4 = state, R8 = in (dk_fx_in), R12 = params; result R0. Register rules as dk_machine.h:
   may clobber R0-R15, I0-I5, M1-M4, B3; must keep I6-I15, M0, M5-M15, L0-L7, MODE1. */
int32_t process(void *state, const dk_fx_in *in, const int32_t *params /* DK_FX_MAX_PARAMS words */);
