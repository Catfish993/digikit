/* sdk/dk_machine.h -- the machine contract, version DK_MACHINE_V1 (docs/design/module-sdk.md 4.1).
   The numbers are in sdk/sdk.toml [machine], from which `patchkit gen` writes sdk/dk_machine_consts.h (included here) and
   sdk/dk_machine.inc for assembly.
   Constants: DK_MACHINE_V1 contract version; DK_BLOCK samples per call, 48 kHz, both devices; DK_MAX_PARAMS hard cap (the device
   profile gives the real maximum); DK_MAX_VOICES voices a machine runs at once on any device; DK_LEVEL_BOUND |sample| above this is a
   fault (+18 dB over full scale); DK_MAGIC 'DKMA', the first word of dk_machine_desc.
   dk_status, returned by render: DK_OK block written, voice continues; DK_DONE block written (may be silence), voice has ended.
   dk_param_kind: DK_P_PERCENT word 0 .. 0x7f00, UI 0 .. 127; DK_P_BIPOLAR 0x4000 at rest, UI -64 .. +63; DK_P_ENUM n steps,
   word = step * 256; DK_P_PITCH semitone offset, 0x4000 at rest, the adapter adds it to `pitch`. */
#include <stdint.h>
#include "dk_machine_consts.h"

typedef struct dk_voice_in {            /* per call, built by the adapter                                     */
    int32_t  pitch;                     /* semitones << 8 (MIDI note 60 = 60 << 8), the device's tune added   */
    int32_t  velocity;                  /* 0 .. 127 << 8                                                      */
    uint32_t block;                     /* blocks since note-on, 0 on the note-on block                       */
    float   *out;                       /* render writes DK_BLOCK floats here                                 */
} dk_voice_in;

/* One per module in read-only data, found by the manifest's symbol. No function pointers: the manifest names the entry symbols. */
typedef struct dk_machine_desc {
    uint32_t magic;                     /* DK_MAGIC                                                           */
    uint32_t contract;                  /* DK_MACHINE_V1                                                      */
    uint32_t n_params;                  /* 0 .. DK_MAX_PARAMS, the manifest's `params` order                  */
    uint32_t voices;                    /* 1 .. DK_MAX_VOICES the module supports                             */
    uint32_t state_bytes;               /* per voice, a multiple of 4                                         */
    uint32_t state_clear_bytes;         /* leading bytes zeroed at note-on, <= state_bytes                    */
    uint32_t max_cycles_block;          /* worst-case instructions of one render call                         */
    uint32_t max_cycles_note_on;        /* the same for the note-on block (note_on + render)                  */
    uint32_t max_call_depth;            /* nested CALLs the module uses                                       */
    uint32_t stack_bytes;               /* data stack the module uses                                         */
} dk_machine_desc;

/* Entry points (symbols named in the manifest). SHARC+ assembly, v1: the adapter enters with `I13 = return address;
   JUMP (M13, I12)` and the module returns with `JUMP (M13, I13)`. Arguments R4 = state, R8 = in, R12 = params; result R0.
   May clobber R0-R15, I0-I5, M1-M4, B3; must keep I6-I15, M0, M5-M15, L0-L7, MODE1. */

/* At note-on, after `state_clear_bytes` are zeroed; no sound. Returns 0, or a negative number (a fault). */
int32_t note_on(void *state, const dk_voice_in *in, const int32_t *params /* DK_MAX_PARAMS words */);

/* Once per block while the voice runs, including the note-on block after note_on. Writes DK_BLOCK floats to in->out. Returns dk_status. */
int32_t render(void *state, const dk_voice_in *in, const int32_t *params /* DK_MAX_PARAMS words */);
