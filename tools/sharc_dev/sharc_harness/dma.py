"""DMA transfer setup and completion driving."""

from __future__ import annotations

from tools.sharc_dev import sharc_run as sr

from .layout import profile
from .runner import _dm_word, _poke

# --- Delivering the DSPI2 transfer at its real hardware address (lane G1,
# 2026-09-26) --------------------------------------------------------------
#
# Every earlier replay (tools/sharc_dev/sharc_replay.py) wrote a captured frame's
# bytes straight into `0x2558dc` (the CONFIRMED-by-execution buffer
# render_frame's own per-track decode reads, docs/findings/06's "The
# ColdFire frame is mapped into SHARC DM at 0x2558dc") and forced
# `command_word` to that frame's own header value directly -- bypassing the
# real SPI-slave receive DMA and command_dispatch_fn entirely. This lane
# traces where those two cells actually come from on real hardware, so a
# replay can write the transfer at THAT address instead and let render_frame
# (FUN_1c2b24) do its own copy, the way a real DMA-then-firmware sequence
# would.
#
# **The receive DMA's ADDRSTART is `command_word` itself -- confirmed from
# both ends.** `FUN_1c7bd4` (the SPI-slave driver's own setup routine,
# docs/findings/04's Lane D2 -- it also calls `FUN_1c9fd5(R12=0x261a10)`,
# the control-block zero-fill lane D2 already traced) builds a 2-node
# descriptor-list ring at DM `0x2641b0`/`0x2641cc` (`sw` 0x1c7c60-0x1c7cc8):
# each node's own DSCPTR_NXT/ADDRSTART/CFG/XCNT/XMOD fields match the
# SC58x/2158x Hardware Reference's own Descriptor-List Mode layout
# (out/refs/adsp-2156x-hwr, Table 27-10, offsets +0x00/+0x04/+0x08/+0x0c/
# +0x10) closely enough to identify the fields (this lane did not chase the
# exact byte width of CFG/XCNT further); node 1's own ADDRSTART
# (`sw 0x1c7c6f`, `I5 = 0x265220`) and node 2's (`sw 0x1c7c89`,
# `I1 = 0x264220`) are EXACTLY `profile(image).command_word`'s own two
# ping-pong instances (`command_word + (shift<<12)`, shift 0/1) -- the SAME
# formula and the SAME addresses `command_dispatch_fn`/block_handler already
# use to find the pending command. A second, identically-shaped ring at
# `0x2641e8`/(the same base again) targets `command_record_table`'s own two
# instances (`0x266220`/`0x267220`, `sw` 0x1c7cb9-0x1c7cbc) -- provisioned
# in hardware exactly like the first, but a captured DSPI2 transfer (2,748
# bytes, `dspiframe.FRAME_BYTES`) is smaller than one 0x1000-byte ring, so
# it never reaches this second ring: consistent with lane F2's own finding
# that `command_record_table` is unwritten everywhere this project has run.
#
# From the CONSUMER side, independently: `FUN_1c2b24`'s own prologue
# (`sw 0x1c2b79`/`0x1c2b83`) saves its incoming `R12` argument (`command_
# word + (shift<<12)`, command_dispatch_fn's first by-reference output) into
# `M10`, and `sw 0x1c2c71`-`0x1c2c8b` -- gated by the flag below -- copies
# 512 long-word (2,048-byte, `0x800`) reads starting AT `I4 = M10` into
# `I5 = 0x2558dc`. So `0x2558dc` is a WORKING COPY render_frame makes of the
# real landing zone every frame, not the landing zone itself: TX byte 0 (the
# command header docs/findings/04 already reads at offset 0) is copied
# to 0x2558dc's own byte 0 unchanged, matching `sharc_replay.py`'s existing
# `COMMAND_HEADER_OFFSET` convention exactly, because `command_word` (word 0
# of the ring) and 0x2558dc's own byte 0 are now the SAME transferred byte,
# not two independently-poked cells.
#
# **`FUN_1c77b4` (docs/findings/04's Lane D2 driver callback) is exactly 9
# instructions and does exactly one thing:** `R2 = btgl(R8, bit=5)` (toggle
# bit 5 of its own event-code argument R8, also testing that bit's ORIGINAL
# value into SZ), `JUMP IF NOT SZ` skips the rest -- so ONLY when bit 5 of
# R8 is set does it `R1 = DM(command_word_shift_src); R2 = btgl(R1, R2);
# DM(command_word_shift_src) = R2` -- an XOR toggle of the ping-pong shift.
# No other memory is touched: this lane reads that as "toggle the ring
# select on a transfer-complete event" (bit 5's own ADI SSL event-code
# meaning was not decoded -- `DMA_SHIFT_CALLBACK_COMPLETE_EVENT` below is
# simply a value with bit 5 set, not a cited constant), with the underlying
# SPI DMA channel's own descriptor-list auto-advance (SC58x/2158x HWR
# "Descriptor-List Mode": the channel loads DMA_DSCPTR_NXT into DMA_DSCPTR_
# CUR on its own at the end of a work unit) doing the actual double-buffer
# re-arming in hardware, invisibly to SHARC code -- consistent with
# `has_static_caller: 0` (docs/findings/04: reached only through the ADI
# SSL library's own generic ISR dispatch, never called by name).
#
# **A second, still-open gate this lane found and had to work around:**
# the whole companding-fields-plus-2,048-byte-copy block above is itself
# gated (`sw 0x1c2c41`-`0x1c2c6e`: `R2 = DM(0x2567dc) (bw); R2 =
# leftz(R2, R0)`, PRM p.521 "SV = result == 32" i.e. SV set iff the byte
# was 0; `JUMP IF SV` skips the copy) by a byte this lane did NOT expect
# from any earlier finding. Empirically, `DM(0x2567dc)` reads back `1`
# right after `run_init()` (so the FIRST frame call after boot does run the
# real copy), but the SAME block's own tail (`sw 0x1c2c88`-`0x1c2c8b`:
# `I4 = 0x2567dc; DM(I4, M5) = M13`) writes it back to `M13`'s OWN value at
# that point, which this lane's concrete runs always find to be `0` -- so
# the gate SELF-CLEARS after one successful copy, and `img.writers()` over
# `[0x2567dc, 0x2567dd)` finds no OTHER resolved writer (several `base_only`
# candidates sit inside `unpack_track`/`FUN_1c24e9`'s own per-track decode
# body, `sw` 0x1c255e-0x1c25cd, close enough to be plausible but not
# resolved to this exact byte within this lane's budget). Real hardware
# must re-arm this every completed transfer, the same way it re-arms the
# receive descriptor itself -- most likely the same opaque ADI SSL ISR
# dispatch `FUN_1c77b4` is reached through, not visible SHARC code. Per
# this project's own rule against guessing an unresolved trigger (docs/
# findings/06's Lane E2 made the same call for the mix gate and the
# companding record's own writer), `drive_dma_completion()` below pokes
# this byte back to nonzero explicitly, alongside the ping-pong toggle, as
# the most faithful stand-in this lane could build for "the DMA-completion
# event this project cannot single-step into" -- documented here as an
# **[O]** substitute, not claimed as a traced firmware write.

DMA_SHIFT_CALLBACK = 0x1C77B4  # FUN_1c77b4 -- see this section's own note.
DMA_SHIFT_CALLBACK_COMPLETE_EVENT = 0x20  # bit 5 set; the exact ADI SSL
# event code was not decoded (see this section's own note) -- only bit 5's
# own effect on this one function was confirmed by reading its 9 instructions.

COPY_GATE_ADDRESS = 0x2567DC  # see this section's own note; [O] real setter.

TRANSFER_COPY_BYTES = 0x800  # 2,048 bytes -- FUN_1c2b24's own DO 512 loop
# at sw 0x1c2c79-0x1c2c7f, confirmed by this section's own trace.

RING_SIZE_BYTES = 0x1000  # the shift<<12 stride both rings use.


def dma_landing_address(state, image: str) -> int:
    """Where the SPI-slave receive DMA's own ADDRSTART currently points for
    the transfer THIS replay is about to deliver -- see this module's own
    section note above. `FUN_1c77b4` toggles `command_word_shift_src` to
    point AT the buffer a transfer just completed into, so the buffer a NEW
    transfer lands in, before that toggle, is the OTHER one: `profile(image
    ).command_word + ((1 - (shift & 1)) << 12)`, `shift` being
    `command_word_shift_src`'s CURRENT (pre-toggle) value."""
    p = profile(image)
    shift = _dm_word(state, p.command_word_shift_src) & 1
    return (p.command_word + ((1 - shift) * RING_SIZE_BYTES)) & 0xFFFFFFFF


def _swap16(data: bytes) -> bytes:
    """Swap the two bytes of every 16-bit unit of DATA (an odd trailing
    byte, if any, is left in place) -- see `write_dma_transfer()`'s own
    docstring for why this is needed at all."""
    out = bytearray(data)
    for i in range(0, len(out) - 1, 2):
        out[i], out[i + 1] = out[i + 1], out[i]
    return bytes(out)


def write_dma_transfer(
    state, image: str, payload: bytes, *, swap16: bool = True
) -> int:
    """Write PAYLOAD (a captured frame's own raw TX bytes, `emu/sharc_
    capture.py`'s `Dspi2Frame.tx`, the SAME big-endian wire order
    `describe_frame()`/`frame_command()` already parse with `_u16be()`) at
    `dma_landing_address()` -- the real hardware receive-DMA landing zone
    this module's own section note traces, not `0x2558dc`/`command_word`
    directly.

    **Byte order, found empirically, not yet cited to a specific hardware
    bit.** A naive byte-for-byte copy (PAYLOAD's own wire order, address
    `base+i` for byte `i`) makes a plain 32-bit `DM` word read at `base`
    recover the WRONG value: this module's own dev checks found a header
    of `0x00, 0x03` (command 3, big-endian) reading back as `0x300`, not
    `3` -- `block_handler`'s own range check (`sw 0x1c750a`-`0x1c7511`:
    `R2 = DM(I3, M5); R1 = lshift(R2, -2); JUMP IF NOT SZ`) would then treat
    every real command as out-of-range and always take the "clear" path,
    never "render". Swapping each 16-bit unit's own two bytes before
    writing (this function's own default) fixes this exactly (confirmed:
    the same header now reads back as `3`) -- consistent with the SPI
    peripheral's own documented 16-bit transfer granularity (`SIZE=1`,
    docs/findings/04's Lane D2) landing each 16-bit unit in DM such that a
    NATIVE (little-endian) narrow read recovers the ColdFire's own
    big-endian-sent value, the same way `frame_command()`'s existing
    `_u16be()` convention already assumes for the wire bytes themselves.
    This is applied uniformly to the whole transfer (every per-track field
    docs/findings/04 documents is itself 16-bit), not only the command
    header -- **[O]**: the exact receive-side hardware/DMA mechanism that
    performs this on real silicon was not found in the public HWR at the
    bit level (`DMA_CFG`'s own fields, out/refs/adsp-2156x-hwr, have no
    documented byte-swap control); this function reproduces its observable
    effect, not a cited register setting. Pass `swap16=False` to write the
    raw wire order instead (e.g. to reproduce the old, pre-lane-G1 bug for
    an A/B comparison).

    Truncated to `RING_SIZE_BYTES` (one ring's own span) if longer; a real
    transfer (`dspiframe.FRAME_BYTES`, 2,748 bytes) already fits inside
    that with room to spare. Returns the address written."""
    base = dma_landing_address(state, image)
    truncated = payload[:RING_SIZE_BYTES]
    data = _swap16(truncated) if swap16 else truncated
    for i, byte in enumerate(data):
        _poke(state, base + i, byte, width=1)
    return base


def drive_dma_completion(
    runner: sr.Runner, image: str, *, rearm_copy_gate: bool = False
) -> sr.Runner:
    """Call `FUN_1c77b4` (`DMA_SHIFT_CALLBACK`) the way the SPI service's
    own ISR dispatch would on a receive-complete event (`R8` with bit 5
    set -- see this module's own section note for why), toggling
    `profile(image).command_word_shift_src` onto the buffer
    `write_dma_transfer()` just filled.

    REARM_COPY_GATE (default False) also writes 1 to `COPY_GATE_ADDRESS`,
    the old per-frame stand-in (see that note's own **[O]** paragraph).
    **[C] 2026-09-28: off by default.** With the gate set every frame,
    render_frame's start-of-frame copy makes the per-voice flag table
    0x2522ac (FUN_1c24e9, from RX_BASE) and the trig test at sw 0x1c2d1a
    (the landing ring's TX halfword 0x22) read the same frame, so the
    sample-load gate at sw 0x1c2d22 can only be true when 0x1c2d1a has
    already branched to the trigger block 0x1c33bc: the tail block
    0x1c3329 (FUN_1c3f78 -> FUN_1c7442 -> FUN_1c4e70, the only per-track
    sample load in the frame) is unreachable after a trig. Without the
    re-arm, the unconditional end-of-frame copy (sw 0x1c3064-0x1c3078)
    feeds the next frame, and a trig in frame N loads the track's slot and
    arms its voices in frame N+1 (scratchpad report sharc-trig-arm.md). The
    only SHARC writer of the gate is the clear at sw 0x1c2c8b; run_init()
    leaves it 1, so the first frame still copies.

    Returns a NEW Runner (`Runner.fresh_call()`, the same convention every
    other entry point in this module uses) positioned right after the
    callback's own return; DM memory -- including both writes above -- is
    shared with RUNNER's own State (`fresh_call_state()`'s `dataclasses.
    replace()` only gives fresh copies to control-flow/register containers,
    never to the memory backing -- the same sharing this whole module's
    multi-frame continuity already relies on), so a following `call_frame()`
    on either Runner sees them.
    """
    new_runner = runner.fresh_call(
        DMA_SHIFT_CALLBACK,
        regs={"R8": DMA_SHIFT_CALLBACK_COMPLETE_EVENT},
        diagnose_unknown=True,
    )
    result = new_runner.run(max_steps=64)
    if result.halt.reason != "return without followed call":
        raise RuntimeError(
            "drive_dma_completion: FUN_1c77b4 (%#x) did not return cleanly: %s"
            % (DMA_SHIFT_CALLBACK, result.halt)
        )
    if rearm_copy_gate:
        _poke(new_runner.state, COPY_GATE_ADDRESS, 1, width=1)
    return new_runner


def setup_frame_dma(state, image: str, *, ring_flag: int = 0) -> int:
    """Prepare STATE for the real-DMA replay path (`write_dma_transfer()`/
    `drive_dma_completion()` above) instead of `setup_frame()`'s own direct
    `command_word` poke: only `command_word_shift_src` (the ping-pong
    toggle's own starting position -- a cold-reset value, not a hack) and
    `ring_flag` (the DAC ring half, unrelated to this section) are touched.
    Returns `profile(image).block_handler`, the address `call_frame()`
    should `fresh_call()` at -- the same convention `setup_frame()` uses.
    """
    p = profile(image)
    _poke(state, p.command_word_shift_src, 0)
    _poke(state, p.ring_flag, ring_flag)
    return p.block_handler
