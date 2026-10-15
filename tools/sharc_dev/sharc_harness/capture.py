"""Capture frame loading and lookup."""

from __future__ import annotations

from collections.abc import Sequence

from tools.sharc_dev import sharc_capture

from .runner import _poke

# --- Real capture frame bytes (lane E1, 2026-09-25) -----------------------
#
# Every render_frames()/render_frames_to_ring_a() run above (and
# FRAME_PATCH_TABLE/CONTINUOUS_MIX_SCALAR_PATCH) was built against a
# completely EMPTY RX frame: setup_frame() only ever poked the three
# command-dispatch cells, never anything in [0x2558dc, 0x2560de) itself. Per
# this repo's scratchpad STATE.md ("KEY" item, 2026-09-25): the master-bus
# parameter table FUN_1c207b decodes every frame (0x255fb6-0x2560d0) and the
# per-track mix-gate source FUN_1c207b's own dispatch masks come from
# (0x255908-0x25600a) are BOTH inside that 2,050-byte RX frame at 0x2558dc
# (offsets 0x6da-0x7f4 and 0x2c-0x72e respectively) -- so every prior
# "reads all zero" finding about those tables was reading an RX frame this
# project never populated, not a property of the firmware. The functions
# below load real RX frame bytes from a unicorn-reference-final:tools/sharc_capture_run.py capture
# (a real device's own DSPI2 traffic) and write them at the same address a
# real transfer would, so a frame render sees whatever a real device
# actually put in its own mixer/gate tables.
#
# CAPTURE_FRAME_BASE/CAPTURE_FRAME_LEN repeat tools/sharc_dev/sharc_replay.py's own
# RX_BASE/TX_PAYLOAD_BYTES and tools/sharc_dev/sharc_framemap.py's FRAME_BASE/
# FRAME_LEN (docs/findings/06's "The ColdFire frame is mapped into SHARC DM
# at 0x2558dc", confirmed by execution in tools/sharc_dev/sharc_framemap.py) rather
# than importing either -- tools/sharc_dev/sharc_replay.py is lane E2's file for this
# task, and tools/sharc_dev/sharc_framemap.py is a shared analysis tool neither lane
# owns; the literal itself is already established in three places, so a
# fourth citing the same evidence is not a new claim.
CAPTURE_FRAME_BASE = 0x2558DC
CAPTURE_FRAME_LEN = 0x802  # 2,050 bytes


def capture_frame_bytes(cap: sharc_capture.Capture, index: int) -> bytes:
    """The raw bytes a real device's DSPI2 TX frame ``cap.dspi2_frames[index]``
    puts at CAPTURE_FRAME_BASE: the first CAPTURE_FRAME_LEN bytes of that
    frame's own ``.tx`` payload (the ColdFire's TX = the SHARC's RX; see
    tools/sharc_dev/sharc_replay.py's own module docstring point 1 for the same
    convention, and tools/sharc_dev/sharc_framemap.py's ``_frame_tx`` for the same
    slice). Raises IndexError/ValueError for an out-of-range index or a
    payload shorter than CAPTURE_FRAME_LEN (should not happen for a
    ``--full-tx`` capture -- this lane's own capture,
    out/captures/dt2-1.16-play-pretracks-fulltx.dt2cap, has 2,748-byte
    payloads throughout)."""
    if not 0 <= index < len(cap.dspi2_frames):
        raise IndexError(
            "capture_frame_bytes: frame %d out of range (0..%d)"
            % (index, len(cap.dspi2_frames) - 1)
        )
    tx = cap.dspi2_frames[index].tx
    if len(tx) < CAPTURE_FRAME_LEN:
        raise ValueError(
            "capture_frame_bytes: frame %d payload is only %d bytes, need %d"
            % (index, len(tx), CAPTURE_FRAME_LEN)
        )
    return bytes(tx[:CAPTURE_FRAME_LEN])


def write_capture_frame(state, frame_bytes: bytes) -> None:
    """Poke FRAME_BYTES (CAPTURE_FRAME_LEN bytes) into DM at
    CAPTURE_FRAME_BASE, byte for byte -- the same convention
    tools/sharc_dev/sharc_framemap.py's own run uses (``h._poke(state, FRAME_BASE + i,
    byte, width=1)``), so a real capture's per-track mixer/gate tables read
    exactly as a real device would have loaded them."""
    if len(frame_bytes) != CAPTURE_FRAME_LEN:
        raise ValueError(
            "write_capture_frame: expected %d bytes, got %d"
            % (CAPTURE_FRAME_LEN, len(frame_bytes))
        )
    for i, byte in enumerate(frame_bytes):
        _poke(state, CAPTURE_FRAME_BASE + i, byte, width=1)


# Frame offsets (relative to CAPTURE_FRAME_BASE) STATE.md's own "KEY" item
# names as the two ranges to check for the kit-load settle transient: the
# master-bus parameter table (0x255fb6-0x2560d0 = offset 0x6da-0x7f4) and
# the per-track mix-gate source (0x255908-0x25600a = offset 0x2c-0x72e).
SETTLED_FRAME_RANGES: tuple[tuple[int, int], ...] = ((0x6DA, 0x7F4), (0x2C, 0x72E))


def find_settled_capture_frame(
    cap: sharc_capture.Capture,
    *,
    ranges: Sequence[tuple[int, int]] = SETTLED_FRAME_RANGES,
    min_frame: int = 0,
) -> int | None:
    """The first frame index >= MIN_FRAME whose own frame bytes
    (``capture_frame_bytes``) have at least one nonzero byte in every one of
    RANGES -- past the kit-load settle transient (frame 0 of this lane's own
    capture reads all-zero in both ranges: the ColdFire has not yet
    populated the mixer/gate tables). Returns None if no such frame exists.

    This is a coarse gate (some byte nonzero in range), not a stability
    check. This lane's own scan of
    out/captures/dt2-1.16-play-pretracks-fulltx.dt2cap (291 frames) found
    both ranges already nonzero from frame 1 onward (62/561 nonzero bytes),
    but still drifting (68/659 at frame 2) before settling to a near-constant
    63/~593-596 from frame 20 on -- this module's own default pick
    (``DEFAULT_CAPTURE_FRAME_INDEX``) uses ``min_frame=20`` for that reason,
    not the first frame this function would accept unconstrained."""
    for index in range(min_frame, len(cap.dspi2_frames)):
        frame = capture_frame_bytes(cap, index)
        if all(any(frame[lo:hi]) for lo, hi in ranges):
            return index
    return None


# This lane's own capture and chosen start frame (see
# find_settled_capture_frame()'s docstring for why 20, not the first frame
# that satisfies SETTLED_FRAME_RANGES at all): a real 4-track kit, PLAY
# running, DSPI2 traffic captured with the full 2,748-byte TX/RX payload
# (out/captures/dt2-1.16-play-pretracks-fulltx.dt2cap's own header: kind
# "play", 291 frames). At this frame, the big-endian machine-type word at
# CAPTURE_FRAME_BASE offset 0x94+2i (docs/findings/04's "The machine type
# reaches the SHARC, at TX frame offset 0x94 + 2i") is nonzero (value 2)
# only for track index 2 of the 16 -- the one kit track among the four this
# capture's own kit populates that has a real machine assigned, per this
# lane's own report -- every other track offset reads 0.
DEFAULT_CAPTURE_PATH = "out/captures/dt2-1.16-play-pretracks-fulltx.dt2cap"
DEFAULT_CAPTURE_FRAME_INDEX = 20
DEFAULT_CAPTURE_TRACK = 2


def load_capture(path: str = DEFAULT_CAPTURE_PATH) -> sharc_capture.Capture:
    """``tools.sharc_dev.sharc_capture.load(path)`` -- re-exported so a caller (this
    module's own CLI, a test, a caller script) does not need its own
    ``sys.path``/import dance for ``tools.sharc_dev.sharc_capture`` just to load one
    capture file."""
    return sharc_capture.load(path)


# The master-bus parameter table range (STATE.md's own "KEY" item; see
# SETTLED_FRAME_RANGES), masked out by default when writing a capture's real
# frame bytes for more than one consecutive block_handler call.
#
# **Root cause of a brand-new gap, found by this lane's own bisection (see
# the report's full matrix): real, nonzero content in JUST this range --
# not the per-track gate source (0x2c-0x72e) and not the machine-type table
# (0x94+2i) -- deterministically trips a sharc_core gap this lane has never
# seen before ("unsupported full compute cu=0x3 opcode=0xe0") on the SECOND
# real-frame-driven block_handler call, at the same instruction count every
# time, even when every subsequent frame's own bytes are held IDENTICAL to
# the first (so this is not about frame-to-frame content changing; it is
# about running the per-track dispatch a second time after whatever it did
# on the first). ``tools/sharc_dev/sharc_core/compute.py``'s own CU3_OPS comment
# already documents this exact caution for cu=3 in general ("PRM: cu=11 not
# used by SINGLEFN ... there is no evidence it is real or what it would
# mean") -- this lane extends that caution to opcode 0xe0 specifically, not
# a new finding about cu=3 as a whole. The likeliest explanation (not
# confirmed further, out of this lane's own scope) is that a real machine
# type reaching FUN_1c642a's per-track dispatch for the first time (this
# repo's scratchpad STATE.md: "FUN_1c60a2 (machine-type change detector)
# ... a replay with a real 0->2 change saw zero writes there" -- this lane's
# own real-frame render is, as far as this project's history shows, the
# first time that detector has actually fired) reaches genuine per-machine
# synthesis code this project has never executed before, using a compute
# form with no public semantics -- exciting as a "first contact" with real
# machine code, but not something this lane may guess an implementation
# for (this repo's CLAUDE.md: sharc_core fixes need a PRM page, a test, or
# a widthaudit 0-mismatch proof; none exists here). Masking this one range
# lets a capture's real machine-type and per-track gate-source data (this
# lane's own actual target, per STATE.md's KEY item) run for any number of
# continuous frames instead of stopping after one.
MASTER_BUS_TABLE_RANGE: tuple[int, int] = SETTLED_FRAME_RANGES[0]


def capture_frame_bytes_for_render(
    cap: sharc_capture.Capture,
    index: int,
    *,
    mask_master_bus_table: bool = True,
) -> bytes:
    """``capture_frame_bytes()``, with MASTER_BUS_TABLE_RANGE zeroed by
    default (see that constant's own docstring for why) -- what
    ``render_frames_to_ring_a()`` actually writes per frame when given a
    ``capture``. Pass ``mask_master_bus_table=False`` to get the completely
    unmodified real bytes (reproduces the new gap from the second real
    frame on -- useful for confirming the finding, not for a working
    multi-frame render)."""
    frame = bytearray(capture_frame_bytes(cap, index))
    if mask_master_bus_table:
        lo, hi = MASTER_BUS_TABLE_RANGE
        frame[lo:hi] = bytes(hi - lo)
    return bytes(frame)
