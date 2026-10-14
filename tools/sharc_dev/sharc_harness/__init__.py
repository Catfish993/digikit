"""Render one Digitakt II SHARC+ voice offline with tools/sharc_dev/sharc_run.py and
write it to a WAV, for correctness- and speed-checking the emulator against
a real firmware code path rather than a hand-written test vector.

    uv run python -m tools.sharc_dev.sharc_harness dt2-1.16 --blocks 8 \
        --out /tmp/voice0.wav

Library use:

    import sharc_harness as h
    memory = h.load_image_memory("dt2-1.16")
    blocks = h.render_blocks(memory, voice=0, n_blocks=8)
    h.write_wav("/tmp/voice0.wav", h.flatten(blocks), sample_rate=48000)

Per docs/findings/06-sharc-engine-and-startup.md's "The voice record
contract" and "Voices" sections (two agents checked the contract table
against the 1.16 bytes): one voice occupies a 0x1d8-byte record at
``profile.voice_records + voice*0x1d8``. FUN_1c4ecf (``profile.voice_render``,
called the way FUN_1c642a's dispatch loop calls it: **R4 = the record
address itself**, not record+4 -- see "Corrected voice call convention"
below) is one voice's render, producing 64 interpolated floats at
record+4..+0x103 and, through its own call to the decimator at 0xb80000, a
2:1-decimated 32-sample block.

**Corrected voice call convention.** An earlier version of this harness
passed R4 = record+4, compensating with two empirical pokes (record+0x1bc
set to 1, record+4's first float set to 1.0) to reach the render body at
all. Both were papering over the same off-by-4: FUN_1c642a's real call
site (sw 0x1c6ad3-0x1c6b00) loads ``I4 = DM(I6-4)`` (frame_workspace,
``profile.frame_workspace``), computes ``R13 = DM(I6-4) + 4`` once before
the 32-voice loop and passes ``R4 = R13`` unmodified to FUN_1c4ecf/
FUN_1c5576 -- i.e. R4 = frame_workspace + 4 = ``profile.voice_records``,
the record base, confirmed directly against ``out/sharcdb``'s decode at
those sw addresses. Inside FUN_1c4ecf/FUN_1c4f81, I4 is set to R4 once
(sw 0x1c4f0f) and never rebased before the fields the contract table
documents at record+4.., record+0x1b8 etc are read through it -- so those
offsets are relative to the *record base*, matching the contract table
exactly once R4 is corrected. Both empirical pokes are gone.

**The record+0 open problem, resolved: it is the raw sample-data
pointer.** FUN_1c4ecf's own entry gate (sw 0x1c4f15: ``R4 = DM(I4, M5)``,
i.e. word 0 of the record) was previously undocumented ("[O]" in the
contract table's context) and empirically zero-filled unless non-zero,
alongside the ACTIVE byte at +0x1b8. Tracing FUN_1c4f81 (out/sharcdb
decode, sw 0x1c504a): **before I4 is rebased to +0x1b8** (sw 0x1c504c),
``I0 = DM(I4, M5)`` reads that same word-0 field, and the DO 64
interpolation loop (sw 0x1c5096-0x1c5101) reloads ``I4 = I0`` every
iteration (sw 0x1c50a4) before indexing the raw samples it multiplies
against the polyphase taps. So record+0 is a pointer: null means "no
sample assigned" (the gate FUN_1c4ecf checks), non-null is the address the
DO 64 loop actually reads PCM through. ``setup_voice()`` now writes
``sample_base`` there instead of setting a dead I0 register (I0 was always
overwritten before the loop used it -- this is why seeding the register
directly, as the previous version did, had no effect).

Symbols (function/data addresses) come from ``tools/sharc_dev/sharc_symbols.py``'s
``resolve()`` against ``out/sharcdb``, not a hardcoded dict -- see
``profile()`` below. Only record layout (byte offsets within one voice
record, and the coefficient table's phase stride) are constants here, since
those are not independently-addressed symbols.

**--run-init now actually feeds the render (2026-09-25).** Previously,
``run_init()``'s Runner and its State were thrown away: ``render_blocks()``/
``check_correctness()`` always called ``new_runner()``, which builds a
*bare* State with an empty overlay, so FUN_1c15e3's writes (finding 06's
"Init writes") never reached a render even with ``--run-init`` on the CLI.
``new_runner(..., init=...)`` (an ``InitResult`` from ``run_init()``) now
starts from a copy of init's own post-return State instead (same
overlay/uregs/mmrs FUN_1c15e3 itself left -- see ``_clone_state`` for why a
copy, and why not ``sharc_core.state._copy()``). The bare-state path stays
the default (``init`` omitted) for comparison. ``setup_voice()`` was also
still poking every field it wrote by hand regardless of what a real trigger
sets; it now writes the documented net effect of a trigger (arm +
position/step setter -- see its own docstring) instead, and reports which
fields still have no documented trigger-path writer.

**CLI semantics corrected; THD+N re-measured on a coherent-sampling render
(lane L17, task 1/2, 2026-09-25).** See ``SOURCE_SAMPLE_RATE``'s own
docstring and ``main()``'s own comment for task 1's CLI fix (the source is
now a --freq Hz tone at the firmware's own 96 kHz sample-playback rate,
--sample-len auto-sizes from --blocks/--pitch-step via
``_default_sample_len()``, and the audible output is documented as
freq * pitch_step Hz).

Task 2 re-measured THD+N at pitch_step in {0.5, 1.0, 2.0} on a long,
*coherently-sampled* render (128 blocks = 4096 decimated output samples per
case, one block discarded up front for the startup transient, --freq chosen
per case so the *output* lands on an exact DFT bin -- 996.09375 Hz, bin 85
of 4096 at the 48 kHz decimated rate -- so a plain time-domain Parseval
split (total mean-square power minus the exact-bin fundamental's own power,
via direct sine/cosine correlation, no FFT/numpy needed) has zero spectral
leakage) instead of the previous short, truncated-prefix measurement
(-35.5 dBc at step 1.0 vs -65 dBc at 0.5 and -74 dBc at 2.0). Result: **all
three steps measure within about 0.5 dB of each other**, at -92.2 dBc
(0.5), -92.6 dBc (1.0) and -92.8 dBc (2.0) -- consistent with this
harness's own 16-bit (Q15) ``sample_format="int16"`` quantization floor at
the rendered amplitude (~0.405 out of a possible 1.0: 16-bit full-scale SNR
is ~98 dB, less ``20*log10(0.405)`` ~= 8 dB of headroom gives ~90 dB, which
is what was measured), not a real interpolation-accuracy difference at
unity step. **Step 1.0 is not "genuinely worse"; the previous -35.5 dBc
reading was a measurement artifact** -- almost certainly scalloping loss
from correlating a short, non-coherently-sampled window against an
off-bin frequency (this lane's own Parseval-split fundamental-power
estimate underestimates the true fundamental whenever the analysis window
does not hold an exact integer number of cycles, inflating the apparent
residual/noise term with no change to the actual signal), not a property
of the render itself.

This lane also checked the other half of the task 2 brief -- the
coefficient row at frac=0 (``read_coeff_table()``'s phase 0, the only row
pitch_step=1.0 with an integer ``start`` ever selects) -- against both the
static image and a post-``run_init()`` runtime read of the same address
(``profile(image).coeff_table`` = 0x25d940, confirmed identical between
the two: static six taps
``[-0.0452, 0.0478, 0.3902, 0.0629, -0.0543, 0.0033]``, so "0x25d940 has no
bytes in the static image" did not hold for this image/loader -- the
static ``LoadedMemory`` already resolves it). **Row 0 is not a pure
delay/unit impulse** (that would read as one tap at 1.0 and the rest 0); it
is an ordinary 6-tap lowpass kernel with real energy on every tap, the same
shape every other phase has -- so unity pitch_step still runs the full
polyphase filter (not an identity passthrough), but that filtering is
present at every pitch_step via its own coefficient row, not something
that singles out step 1.0 -- consistent with the near-identical THD+N
measured across all three steps above, not a competing explanation for a
difference that this lane's own re-measurement did not find.
"""

from __future__ import annotations

from tools.sharc_dev import sharc as sharcmod
from tools.sharc_dev import sharc_dac

from .audio import decimate, reference_render, write_wav
from .capture import (
    CAPTURE_FRAME_BASE,
    CAPTURE_FRAME_LEN,
    DEFAULT_CAPTURE_FRAME_INDEX,
    DEFAULT_CAPTURE_PATH,
    DEFAULT_CAPTURE_TRACK,
    MASTER_BUS_TABLE_RANGE,
    SETTLED_FRAME_RANGES,
    capture_frame_bytes,
    capture_frame_bytes_for_render,
    find_settled_capture_frame,
    load_capture,
    write_capture_frame,
)
from .check import _default_sample_len, _make_test_input, check_correctness
from .cli import main
from .dma import (
    COPY_GATE_ADDRESS,
    DMA_SHIFT_CALLBACK,
    DMA_SHIFT_CALLBACK_COMPLETE_EVENT,
    RING_SIZE_BYTES,
    _swap16,
    dma_landing_address,
    drive_dma_completion,
    setup_frame_dma,
    write_dma_transfer,
)
from .frame import (
    FRAME_MILESTONE,
    FRAME_PATCH_TABLE,
    call_frame,
    call_frame_collect_all,
    companding_record_address,
    companding_record_fields,
    render_frames,
    setup_frame,
)
from .layout import (
    COEFF_TABLE_TAPS,
    FIELD_ACTIVE,
    FIELD_END,
    FIELD_FADE_IN,
    FIELD_LOOP,
    FIELD_PHASE,
    FIELD_RESEED,
    FIELD_REVERSE,
    FIELD_SAMPLE_LENGTH,
    FIELD_SAMPLE_LENGTH_HI,
    FIELD_SAMPLE_PTR,
    FIELD_SEED_PENDING,
    FIELD_STEP,
    FIELD_WORK_BUFFER,
    FIELD_ZERO_CROSS_MUTE,
    Q31,
    SESSION_TICK_ADDR,
    SOURCE_SAMPLE_RATE,
    VOICE_RECORD_COUNT,
    VOICE_RECORD_STRIDE,
    profile,
    voice_record_address,
)
from .mix import (
    MASTER_STAGE_CALL_PC,
    TRACK_MIX_BASE,
    TRACK_MIX_CHANNEL_WORDS,
    TRACK_MIX_STRIDE,
    call_frame_with_track_injection,
    read_master_mix,
    read_ring_a,
    read_voice_work_buffer_decimated,
    render_frames_to_ring_a,
    write_ring_a_wav,
)
from .runner import (
    InitResult,
    _clone_state,
    _dm_word,
    _poke,
    _read_byte,
    _read_q31_pair,
    _write_q31_pair,
    load_image_memory,
    new_runner,
    run_init,
)
from .voice import (
    _write_samples,
    call_render,
    read_coeff_table,
    render_blocks,
    scan_voice_active,
    setup_voice,
)

__all__ = [
    "CAPTURE_FRAME_BASE",
    "CAPTURE_FRAME_LEN",
    "COEFF_TABLE_TAPS",
    "COPY_GATE_ADDRESS",
    "DEFAULT_CAPTURE_FRAME_INDEX",
    "DEFAULT_CAPTURE_PATH",
    "DEFAULT_CAPTURE_TRACK",
    "DMA_SHIFT_CALLBACK",
    "DMA_SHIFT_CALLBACK_COMPLETE_EVENT",
    "FIELD_ACTIVE",
    "FIELD_END",
    "FIELD_FADE_IN",
    "FIELD_LOOP",
    "FIELD_PHASE",
    "FIELD_RESEED",
    "FIELD_REVERSE",
    "FIELD_SAMPLE_LENGTH",
    "FIELD_SAMPLE_LENGTH_HI",
    "FIELD_SAMPLE_PTR",
    "FIELD_SEED_PENDING",
    "FIELD_STEP",
    "FIELD_WORK_BUFFER",
    "FIELD_ZERO_CROSS_MUTE",
    "FRAME_MILESTONE",
    "FRAME_PATCH_TABLE",
    "InitResult",
    "MASTER_BUS_TABLE_RANGE",
    "MASTER_STAGE_CALL_PC",
    "Q31",
    "RING_SIZE_BYTES",
    "SESSION_TICK_ADDR",
    "SETTLED_FRAME_RANGES",
    "SOURCE_SAMPLE_RATE",
    "TRACK_MIX_BASE",
    "TRACK_MIX_CHANNEL_WORDS",
    "TRACK_MIX_STRIDE",
    "VOICE_RECORD_COUNT",
    "VOICE_RECORD_STRIDE",
    "_clone_state",
    "_default_sample_len",
    "_dm_word",
    "_make_test_input",
    "_poke",
    "_read_byte",
    "_read_q31_pair",
    "_swap16",
    "_write_q31_pair",
    "_write_samples",
    "call_frame",
    "call_frame_collect_all",
    "call_frame_with_track_injection",
    "call_render",
    "capture_frame_bytes",
    "capture_frame_bytes_for_render",
    "check_correctness",
    "companding_record_address",
    "companding_record_fields",
    "decimate",
    "dma_landing_address",
    "drive_dma_completion",
    "find_settled_capture_frame",
    "load_capture",
    "load_image_memory",
    "main",
    "new_runner",
    "profile",
    "read_coeff_table",
    "read_master_mix",
    "read_ring_a",
    "read_voice_work_buffer_decimated",
    "reference_render",
    "render_blocks",
    "render_frames",
    "render_frames_to_ring_a",
    "run_init",
    "scan_voice_active",
    "setup_frame",
    "setup_frame_dma",
    "setup_voice",
    "sharc_dac",
    "sharcmod",
    "voice_record_address",
    "write_capture_frame",
    "write_dma_transfer",
    "write_ring_a_wav",
    "write_wav",
]
