/* sdk/dk_fx.h -- the FX contract, version DK_FX_V1 (docs/design/module-sdk.md 4.2).
   An effect processes the final stereo block of the device (the master, before the USB pack and the DAC converter).
   The numbers are also in sdk/sdk.toml [fx], from which `patchkit gen` writes sdk/dk_fx.inc for assembly.
   Do not include dk_fx.inc and dk_machine.inc in one assembly file (DK_BLOCK and DK_LEVEL_BOUND_BITS are in both). */
#include <stdint.h>

#define DK_FX_V1         1              /* contract version                                                   */
#define DK_BLOCK         32             /* samples per channel and call, 48 kHz, both devices                 */
#define DK_FX_BLOCK      64             /* floats of a block: planar, 32 left, then 32 right                  */
#define DK_FX_MAX_PARAMS 8              /* hard cap on parameter words: 7 knobs and MIX (v1 rows had 4)       */
#define DK_FX_MAX        4              /* effects in the chain (a device's `[limits] fx_slots` is at most this) */
#define DK_LEVEL_BOUND   8.0f           /* |sample| above this is a fault (+18 dB over full scale)            */
#define DK_FX_MAGIC      0x444b4658u    /* 'DKFX', the first word of dk_fx_desc                               */

typedef enum {                          /* returned by process                                                */
    DK_FX_OK = 0,                       /* out written                                                        */
} dk_fx_status;                         /* a negative number is a fault: the block plays dry                  */

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
