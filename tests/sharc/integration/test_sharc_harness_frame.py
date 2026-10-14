"""sharc_harness frame, ring and capture rendering."""

import hashlib
import pathlib
import struct
import unittest

import pytest

from tools.sharc_dev import sharc_harness as h  # noqa: E402
from tools.sharc_dev import sharc_run as sr  # noqa: E402

DT2_116_BLOB = pathlib.Path("out/sections/dt2-1.16/section_7_BLOB.bin")
DEFAULT_CAPTURE = pathlib.Path(h.DEFAULT_CAPTURE_PATH)


@unittest.skipUnless(DT2_116_BLOB.exists(), "DT2 1.16 firmware bytes are not available")
class FrameRenderTest(unittest.TestCase):
    """Lane L13's frame render entry point: setup_frame()/call_frame()/
    render_frames(), calling FUN_1c2b24 (render_frame) via block_handler,
    the real firmware call chain, instead of bare with hand-set registers
    (see this module's own "Frame render entry point" section)."""

    @classmethod
    def setUpClass(cls):
        cls.memory = h.load_image_memory("dt2-1.16")

    def test_setup_frame_pokes_command_dispatch_and_returns_block_handler(self):
        from tools.sharc_dev import sharc_symbols as ss

        runner = h.new_runner(self.memory, "dt2-1.16")
        entry = h.setup_frame(runner.state, "dt2-1.16", command=3, ring_flag=1)
        p = ss.resolve(h.sharcmod.load("dt2-1.16"), device="dt2")
        self.assertEqual(entry, p.block_handler)

        def read32(addr):
            return sr.st._dm_read(runner.state, addr, 4).value

        self.assertEqual(read32(p.command_word_shift_src), 0)
        self.assertEqual(read32(p.command_word), 3)
        self.assertEqual(read32(p.ring_flag), 1)

    def test_read_master_mix_reads_back_poked_floats(self):
        runner = h.new_runner(self.memory, "dt2-1.16")
        for i in range(64):
            h._poke(
                runner.state,
                0x25F180 + i * 4,
                struct.unpack("<I", struct.pack("<f", float(i)))[0],
            )
        mix = h.read_master_mix(self.memory, "dt2-1.16", runner)
        self.assertEqual(len(mix), 64)
        self.assertEqual(mix, [float(i) for i in range(64)])

    def test_read_master_mix_defaults_to_zero_when_unwritten(self):
        runner = h.new_runner(self.memory, "dt2-1.16")
        self.assertEqual(h.read_master_mix(self.memory, "dt2-1.16", runner), [0.0] * 64)


@pytest.mark.slow
@unittest.skipUnless(DT2_116_BLOB.exists(), "DT2 1.16 firmware bytes are not available")
class FrameRenderFromInitTest(unittest.TestCase):
    """render_frames()'s real call chain from a run_init() state (~1.24M
    instructions, several seconds -- see InitStateRenderTest above): this
    lane's own cross-check that block_handler's real dispatch reaches the
    same known stops a bare `--root 0x1c2b24` run already found (this
    module's FRAME_PATCH_TABLE docstring), and does not regress. One
    run_init() shared across both methods, the same convention
    InitStateRenderTest uses."""

    @classmethod
    def setUpClass(cls):
        cls.memory = h.load_image_memory("dt2-1.16")

    def test_render_frames_without_patches_returns(self):
        runner, results = h.render_frames(
            self.memory, "dt2-1.16", n_frames=1, patch_table=None
        )
        self.assertEqual(len(results), 1)
        halt = results[0].halt
        # Originally the first of the known stops for an (almost) empty
        # synthetic frame was 0xB88E4B (FIX-of-NaN/infinity, unsaturated),
        # fixed by lane G2 (tools/sharc_dev/sharc_core/floats.py's _float_to_fixed);
        # then 0x1C4969, the supposed BITEXT(BITLEN12>32) gap. 2026-09-28:
        # that "BITEXT" was ShiftImm 0x19, Rn = Rn or fdep (PGR Table
        # 12-11; tools/sharc_dev/sharc_core/compute_shift.py), so the frame now runs
        # to block_handler's own return with no patch at all, and
        # FRAME_PATCH_TABLE is empty.
        self.assertEqual(h.FRAME_PATCH_TABLE, {})
        self.assertEqual(halt.reason, h.FRAME_MILESTONE["reason"])
        self.assertEqual(halt.pc_sw, h.FRAME_MILESTONE["pc_sw"])
        self.assertEqual(results[0].instructions, h.FRAME_MILESTONE["instructions"])

    def test_render_frames_with_frame_patch_table_returns(self):
        runner, results = h.render_frames(self.memory, "dt2-1.16", n_frames=1)
        self.assertEqual(len(results), 1)
        halt = results[0].halt
        # FRAME_PATCH_TABLE's two hypotheses get past the first two known
        # stops (0xb88e4b, 0x1c4969); the Type18a bit test on a partly known
        # ASTATX now resolves the third (0x1c088e). The frame then used to
        # stop on a computed return in the 0xb82xxx routines (0xb82bf1,
        # 79,625 instructions: "return target 0xb82d1a differs from
        # recorded return 0xb82b31").
        #
        # That stop was this lane's own sequencer gap, not a firmware
        # anomaly: FUN_b82b09 (0xb82b09) calls FUN_1c0c3f (0x1c0c40) with a
        # real hardware CALL (pushing 0xb82b31 onto call_stack), but
        # FUN_1c0c3f returns through a Type9b_abs JUMP over I13/M14
        # (0x1c0cb5, "JUMP delayed target=indirect PM(I5, M6)" in this
        # image's own disassembly -- I5/M6 there are DAG2's local index 5
        # and modifier 6, i.e. I13/M14), not the I12/M14 pattern
        # sequencer._check_return_target used to hardcode. Traced back
        # (concrete run with State.record_events on), I13 is loaded at
        # FUN_1c0c3f's own entry (0x1c0c57) from the manual return-address
        # slot the call's own delay slots pushed via DM(I7++, M7) -- the
        # exact same software return-address convention as the verified
        # I12/M14 idiom, just through a different DAG2 index register.
        # SHARC+ Core Programming Reference p.111 ("PC Stack Access",
        # Table 4-3) confirms only CALL/IVT-branch/DO-UNTIL push the
        # hardware PC stack and only RTS/RTI pop it: an ordinary JUMP
        # (I12/M14's idiom included) never touches it, so this was always
        # a software convention, not a literal hardware return -- and nothing
        # rules out a different free DAG2 index register per callee. A
        # tools/sharc_dev/sharcdb census of dt2-1.16's Type9a_abs/9b_abs population
        # confirms this reading: 1051 of 1052 cond=0x1F/b=0/j=1/pmm=6
        # (M14) instructions use pmi=4 (I12); exactly one (0x1c0cb5) uses
        # pmi=5 (I13). sequencer._check_return_target now takes pmi and
        # checks I(8+pmi)/M14 generally instead of hardcoding I12, so this
        # one instruction is recognized as a return (popping call_stack)
        # like the other 1051.
        #
        # With that gap closed, the frame ran 7,115 instructions further
        # (86,740 vs 79,625) before stopping on a then-unrelated sharc_core
        # gap: a Type3a long-word DM access at 0x1c2920 that forms_move.py's
        # Type3a handler refused outright ("unsupported Type3a long-word
        # access"). This lane's Y1 pass implemented it (the same
        # Type14a-style neighbor-register-pair access the PRM documents for
        # the (LW) modifier, PRM p.2-4), and the frame now runs on past that
        # pc instead of stopping there. It reaches a genuine RETURN, not
        # another gap: "return without followed call" is how
        # tools/sharc_dev/sharc_run.py's fresh_call()/fresh_call_state() (see their
        # own docstrings) signal a call that started with an empty
        # call_stack unwinding through its own entry point's RTS -- since
        # call_frame() enters at block_handler (0x1c74cd) with no real
        # caller pushed, and the halt pc (0x1c75d3) is inside block_handler
        # itself (tools/sharc_dev/sharc.py's img.func: entry 0x1c74cd, end 0x1c75d8),
        # this is block_handler's own return firing, i.e. the whole frame
        # render call chain (block_handler -> command_dispatch_fn ->
        # cmd_handler_3 -> render_frame -> ...) completed.
        # tools/sharc_dev/sharc_widthaudit.py --root frame independently confirms 0
        # access-width mismatches across this run's 17,468 load/store
        # events. See tools/sharc_dev/sharc_harness.py's FRAME_MILESTONE docstring.
        self.assertEqual(halt.reason, h.FRAME_MILESTONE["reason"])
        self.assertEqual(halt.pc_sw, h.FRAME_MILESTONE["pc_sw"])
        self.assertEqual(results[0].instructions, h.FRAME_MILESTONE["instructions"])


@pytest.mark.slow
@unittest.skipUnless(DT2_116_BLOB.exists(), "DT2 1.16 firmware bytes are not available")
class RingAMilestoneTest(unittest.TestCase):
    """render_frames_to_ring_a()'s own milestone: the first real, non-silent
    ring A output this project has produced from a synthetic voice, pinned
    the same way FRAME_MILESTONE/ReplayIdleCaptureTest pin theirs. This is
    signal reached by INJECTING a voice's own already-firmware-rendered
    decimated buffer into one track's master-mix input (see
    inject_track_buffer()'s module note for why: the real per-track
    accumulate gate, DM(0x252d3c), has no known runtime writer that both
    avoids a new stop and produces this write the ordinary way), not
    evidence that the whole per-track accumulate path is understood.

    **Updated (lane B2, 2026-09-25): now a genuinely continuous Runner, not
    N independent post-init renders.** render_frames_to_ring_a() runs all
    frames on one Runner and applies CONTINUOUS_MIX_SCALAR_PATCH (see that
    constant's own docstring in tools/sharc_dev/sharc_harness.py for the full
    evidence): without it, ring A goes silent from frame 1 onward given
    this lane's blank synthetic mix configuration -- real firmware
    behaviour, not an emulator bug, per that investigation.

    **Updated again (lane D1, 2026-09-25): injection moved past the
    per-track mixer, onto the master mix itself.** `FUN_1c207b`'s own
    per-track dispatch never carries a track's sample values into the
    master mix under any state this lane found (see the "why the track
    buffer never reaches the master mix as audio" module note above
    `MASTER_MIX_INJECT_PC` in tools/sharc_dev/sharc_harness.py) -- what
    `inject_track_buffer()` alone produced there was a sparse,
    compressor-envelope-shaped signal, not the injected tone. This test
    now checks `ring_a_left`/`ring_a_right` (not `ring_a_mono`): ring A's
    own L channel is deliberately negated in-place by real firmware code
    right after conversion (see that same module note), so a coherent
    signal written identically to both master-mix channels arrives at ring
    A as `(-L, +R)` -- `ring_a_mono`'s own `0.5*(left+right)` downmix
    cancels such a signal to (near) silence, which is real, not a
    regression to chase. This changed every frame's own output again, so
    the digest below is a new pin, not the same value. A future resolution
    of the per-track mixer/mix-gate maze (see docs/findings/06 and the
    report) changes this pin again, and the commit that does so should say
    why, exactly like FRAME_MILESTONE's own docstring asks.
    """

    @classmethod
    def setUpClass(cls):
        cls.memory = h.load_image_memory("dt2-1.16")

    def test_four_frame_ring_a_milestone(self):
        result = h.render_frames_to_ring_a(
            self.memory, "dt2-1.16", n_frames=4, freq=1000.0
        )
        self.assertFalse(result["any_new_stop"])
        for frame in result["per_frame"]:
            self.assertEqual(frame["halt"], "return without followed call")
            self.assertGreater(frame["injected_max_abs"], 0.0)

        left = result["ring_a_left"]
        right = result["ring_a_right"]
        self.assertEqual(len(left), 4 * 32)
        self.assertEqual(len(right), 4 * 32)
        # Real, non-silent signal reached ring A (the DAC-facing buffer) --
        # this is the core claim this milestone pins -- on EACH channel,
        # not just a mono downmix that can cancel a coherent signal (see
        # the class docstring's "ring A's own L channel" note).
        self.assertGreater(max(abs(v) for v in left), 0.0)
        self.assertGreater(max(abs(v) for v in right), 0.0)
        # 2026-09-28 re-pin. L == R: the block handler's ring A pass (sw
        # 0x1c7593-0x1c759f, 16 SIMD passes over 64 words) negates every
        # word once its PEy half runs (Type3a SIMD companion and the PEy
        # compute of Type4a); the old "L is the negation of R" came from
        # PEy storing a stale S1. And every frame repeats the same 32
        # samples: on this synthetic frame the now-real parameter setters
        # set voice 0's reverse flag (sw 0x1c4ba0) and a 0x46 end (sw
        # 0x1c49c6), render_body clears ACTIVE in frame 0 (sw 0x1c5048),
        # and the later frames re-inject its unchanged work buffer.
        for l_sample, r_sample in zip(left, right, strict=True):
            self.assertAlmostEqual(l_sample, r_sample, places=5)
        for frame in range(1, 4):
            self.assertEqual(left[32 * frame : 32 * frame + 32], left[:32])

        # Deterministic output: pin it, rounded to 6 decimal places (float
        # repr noise only) by hash, the same convention
        # tests/sharc/integration/test_sharc_golden.py uses for its own outputs.
        rounded = [round(v, 6) for v in left] + [round(v, 6) for v in right]
        digest = hashlib.sha256(repr(rounded).encode()).hexdigest()
        self.assertEqual(
            digest,
            "1a01905595d06870fac4fb73e718dcc3902ea754ec2ad2503d0c807476bce03a",
        )


@unittest.skipUnless(
    DEFAULT_CAPTURE.exists(), "the fulltx play capture is not available"
)
class CaptureFrameBytesTest(unittest.TestCase):
    """Lane E1's capture-loading helpers: pure parsing/slicing over a real
    tools/sharc_capture_run.py capture, no firmware image needed."""

    @classmethod
    def setUpClass(cls):
        cls.cap = h.load_capture()

    def test_frame_0_is_not_settled(self):
        frame0 = h.capture_frame_bytes(self.cap, 0)
        self.assertEqual(len(frame0), h.CAPTURE_FRAME_LEN)
        for lo, hi in h.SETTLED_FRAME_RANGES:
            self.assertFalse(any(frame0[lo:hi]))

    def test_default_capture_frame_index_is_settled(self):
        idx = h.find_settled_capture_frame(
            self.cap, min_frame=h.DEFAULT_CAPTURE_FRAME_INDEX
        )
        self.assertEqual(idx, h.DEFAULT_CAPTURE_FRAME_INDEX)

    def test_no_settled_frame_past_the_end_returns_none(self):
        self.assertIsNone(
            h.find_settled_capture_frame(self.cap, min_frame=len(self.cap.dspi2_frames))
        )

    def test_machine_type_field_for_default_track(self):
        # docs/findings/04's "The machine type reaches the SHARC, at TX
        # frame offset 0x94 + 2i" -- a big-endian word per track.
        frame = h.capture_frame_bytes(self.cap, h.DEFAULT_CAPTURE_FRAME_INDEX)
        offset = 0x94 + 2 * h.DEFAULT_CAPTURE_TRACK
        value = struct.unpack_from(">H", frame, offset)[0]
        self.assertEqual(value, 2)
        # No other track has one assigned in this capture (this lane's own
        # finding -- see DEFAULT_CAPTURE_TRACK's own docstring).
        for track in range(16):
            if track == h.DEFAULT_CAPTURE_TRACK:
                continue
            other = struct.unpack_from(">H", frame, 0x94 + 2 * track)[0]
            self.assertEqual(
                other, 0, "track %d unexpectedly has a machine type" % track
            )

    def test_out_of_range_index_raises(self):
        with self.assertRaises(IndexError):
            h.capture_frame_bytes(self.cap, len(self.cap.dspi2_frames))

    def test_capture_frame_bytes_for_render_masks_only_the_master_bus_range(self):
        raw = h.capture_frame_bytes(self.cap, h.DEFAULT_CAPTURE_FRAME_INDEX)
        masked = h.capture_frame_bytes_for_render(
            self.cap, h.DEFAULT_CAPTURE_FRAME_INDEX
        )
        lo, hi = h.MASTER_BUS_TABLE_RANGE
        self.assertTrue(any(raw[lo:hi]), "fixture assumption: real bytes are nonzero")
        self.assertFalse(any(masked[lo:hi]))
        self.assertEqual(raw[:lo], masked[:lo])
        self.assertEqual(raw[hi:], masked[hi:])

    def test_capture_frame_bytes_for_render_can_leave_it_unmasked(self):
        raw = h.capture_frame_bytes(self.cap, h.DEFAULT_CAPTURE_FRAME_INDEX)
        unmasked = h.capture_frame_bytes_for_render(
            self.cap, h.DEFAULT_CAPTURE_FRAME_INDEX, mask_master_bus_table=False
        )
        self.assertEqual(raw, unmasked)


@unittest.skipUnless(DT2_116_BLOB.exists(), "DT2 1.16 firmware bytes are not available")
@unittest.skipUnless(
    DEFAULT_CAPTURE.exists(), "the fulltx play capture is not available"
)
class WriteCaptureFrameTest(unittest.TestCase):
    """write_capture_frame() actually lands the bytes at CAPTURE_FRAME_BASE."""

    @classmethod
    def setUpClass(cls):
        cls.memory = h.load_image_memory("dt2-1.16")
        cls.cap = h.load_capture()

    def test_written_bytes_read_back(self):
        runner = h.new_runner(self.memory, "dt2-1.16")
        frame = h.capture_frame_bytes(self.cap, h.DEFAULT_CAPTURE_FRAME_INDEX)
        h.write_capture_frame(runner.state, frame)
        # Sample every 251st byte (coprime with typical alignment, so this
        # covers a spread of offsets) rather than reading all 2,050.
        for i in range(0, len(frame), 251):
            raw = sr.st._dm_read(runner.state, h.CAPTURE_FRAME_BASE + i, 1)
            self.assertEqual(raw.value & 0xFF, frame[i])

    def test_wrong_length_raises(self):
        runner = h.new_runner(self.memory, "dt2-1.16")
        with self.assertRaises(ValueError):
            h.write_capture_frame(runner.state, b"\x00" * 10)


@pytest.mark.slow
@unittest.skipUnless(DT2_116_BLOB.exists(), "DT2 1.16 firmware bytes are not available")
@unittest.skipUnless(
    DEFAULT_CAPTURE.exists(), "the fulltx play capture is not available"
)
class RealFrameRingAMilestoneTest(unittest.TestCase):
    """render_frames_to_ring_a()'s own milestone with REAL capture frame
    bytes (lane E1, 2026-09-26), replacing RingAMilestoneTest's synthetic,
    permanently-empty RX frame as the reference configuration this project
    now targets.

    **Two of RingAMilestoneTest's three hacks are gone.** With the real
    per-track gate source and machine type (track `DEFAULT_CAPTURE_TRACK`)
    driving `FUN_1c642a`'s dispatch -- master-bus table masked out (see
    `MASTER_BUS_TABLE_RANGE`'s own docstring for why) -- this lane's own
    16/20/384-frame experiments (see its report) found `inject_track_buffer`
    (`inject_track=False` here) and `CONTINUOUS_MIX_SCALAR_PATCH`
    (`mix_scalar_patch=None` here) make NO difference to ring A's own output
    once `inject_master_mix` (`write_master_mix=True`, still the one
    surviving hack -- the real per-track accumulate path still does not
    carry a track's samples to the master mix even with this real gate data,
    see docs/findings/06's own "[C]" update) is active. `FRAME_PATCH_TABLE`
    stays: its remaining fork (`0x1c4965`, unrelated to per-track mixing)
    is still open (lane G2, docs/findings/06's own "Lane G2" section --
    the BITEXT(BITLEN12>32) result it forces is architecturally
    undocumented, not a `tools/sharc_dev/sharc_core` gap this project may guess at).
    `0x1c2fec`'s own entry is gone (lane G2 fixed the underlying
    `tools/sharc_dev/sharc_core` FIX-of-NaN/infinity saturation gap instead: a real,
    documented `0xFFFFFFFF` FIX result now reaches this render instead of
    the entry's own arbitrary `0x4000` override). This test's own digest
    below is UNCHANGED even so -- checked by running this exact
    configuration both with and without that entry: the fork it used to
    patch belongs to a genuinely silent OTHER track's own per-track divide
    (see docs/findings/06's own "Lane G2" section), not to `voice`'s own
    decimated output this test measures (`write_master_mix=True` injects
    that output directly, bypassing the per-track accumulate path
    entirely), so the two runs' ring A content is bit-for-bit identical
    even though their own total instruction counts differ slightly.

    Real frame data changes the render's own instruction count per frame
    (fewer instructions once the gate data lets more of the per-track
    dispatch actually run its "has content" branches instead of the
    "everything is zero" default path) and, unlike RingAMilestoneTest's
    permanently-empty frame, is genuine, real-device data -- so this is a
    NEW pin, not a refinement of the old one; both are kept (RingAMilestoneTest
    still exercises the capture=None/backward-compatible path with an
    unchanged hash)."""

    @classmethod
    def setUpClass(cls):
        cls.memory = h.load_image_memory("dt2-1.16")
        cls.cap = h.load_capture()

    def test_eight_frame_real_frame_ring_a_milestone(self):
        result = h.render_frames_to_ring_a(
            self.memory,
            "dt2-1.16",
            n_frames=8,
            voice=0,
            freq=1000.0,
            track=h.DEFAULT_CAPTURE_TRACK,
            capture=self.cap,
            capture_frame_start=h.DEFAULT_CAPTURE_FRAME_INDEX,
            inject_track=False,
            write_master_mix=True,
            mix_scalar_patch=None,
        )
        self.assertFalse(result["any_new_stop"])
        self.assertEqual(result["capture_frame_start"], h.DEFAULT_CAPTURE_FRAME_INDEX)
        for frame in result["per_frame"]:
            self.assertEqual(frame["halt"], "return without followed call")

        left = result["ring_a_left"]
        right = result["ring_a_right"]
        self.assertEqual(len(left), 8 * 32)
        self.assertEqual(len(right), 8 * 32)
        self.assertGreater(max(abs(v) for v in left), 0.0)
        self.assertGreater(max(abs(v) for v in right), 0.0)
        # 2026-09-28 re-pin: the same stale-block result as
        # RingAMilestoneTest (see its comment) -- L == R and every frame
        # repeats frame 0's 32 samples.
        self.assertEqual(left, right)
        for frame in range(1, 8):
            self.assertEqual(left[32 * frame : 32 * frame + 32], left[:32])

        rounded = [round(v, 6) for v in left] + [round(v, 6) for v in right]
        digest = hashlib.sha256(repr(rounded).encode()).hexdigest()
        self.assertEqual(
            digest,
            "04484388a91f781eea0a470ccaf4cc9ccabeb3ab6f7b98ee207aad1cddceb5ea",
        )


if __name__ == "__main__":
    unittest.main()
