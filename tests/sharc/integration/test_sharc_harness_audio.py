"""sharc_harness audio helpers (no firmware)."""

import os
import pathlib
import struct
import unittest
import wave

from support.paths import REPO_ROOT

from tools.sharc_dev import sharc_harness as h  # noqa: E402

SCRATCH = (
    pathlib.Path(os.environ.get("CLAUDE_SCRATCHPAD", "/tmp")) / "test_sharc_harness"
)


class DecimateTest(unittest.TestCase):
    def test_pairwise_average(self):
        self.assertEqual(h.decimate([1.0, 3.0, 2.0, 4.0]), [2.0, 3.0])

    def test_odd_trailing_sample_dropped(self):
        # docs/findings/06: "0xb80000 reads 64, writes 32, x0.5" -- an
        # always-even work buffer in practice, but decimate() should not
        # crash on a short/odd input either.
        self.assertEqual(h.decimate([1.0, 1.0, 1.0]), [1.0])

    def test_empty(self):
        self.assertEqual(h.decimate([]), [])


class Swap16Test(unittest.TestCase):
    """h._swap16(): swap the two bytes of every 16-bit unit -- see
    write_dma_transfer()'s own docstring for why lane G1 needs this."""

    def test_swaps_each_16_bit_unit(self):
        self.assertEqual(
            h._swap16(bytes([0x00, 0x03, 0x01, 0x02])), b"\x03\x00\x02\x01"
        )

    def test_odd_trailing_byte_untouched(self):
        self.assertEqual(h._swap16(bytes([0x00, 0x03, 0xFF])), b"\x03\x00\xff")

    def test_empty(self):
        self.assertEqual(h._swap16(b""), b"")


class WriteWavTest(unittest.TestCase):
    def test_round_trips_pcm16(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        path = str(SCRATCH / "roundtrip.wav")
        h.write_wav(path, [0.0, 0.5, -0.5, 1.0, -1.0], sample_rate=48000)
        with wave.open(path, "rb") as wav:
            self.assertEqual(wav.getnchannels(), 1)
            self.assertEqual(wav.getsampwidth(), 2)
            self.assertEqual(wav.getframerate(), 48000)
            frames = wav.readframes(wav.getnframes())
        values = struct.unpack("<%dh" % (len(frames) // 2), frames)
        self.assertEqual(values[0], 0)
        self.assertGreater(values[1], 0)
        self.assertLess(values[2], 0)
        self.assertEqual(values[3], 32767)

    def test_clips_out_of_range(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        path = str(SCRATCH / "clip.wav")
        h.write_wav(path, [10.0, -10.0], sample_rate=8000)
        with wave.open(path, "rb") as wav:
            frames = wav.readframes(wav.getnframes())
        values = struct.unpack("<2h", frames)
        self.assertEqual(values, (32767, -32767))


class WriteRingAWavTest(unittest.TestCase):
    """`read_ring_a()`'s own dict shape (module docstring: "left"/"right",
    32 floats each) fed straight into `write_ring_a_wav()` -- no Runner
    needed, since both ends of this are plain Python."""

    def _ring(self, left, right):
        return {"left": list(left), "right": list(right)}

    def test_stereo_concatenates_frames_in_order(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        path = str(SCRATCH / "ring_a_stereo.wav")
        rings = [
            self._ring([0.5, 0.25], [-0.5, -0.25]),
            self._ring([1.0, -1.0], [0.1, -0.1]),
        ]
        h.write_ring_a_wav(path, rings, sample_rate=48000, stereo=True)
        with wave.open(path, "rb") as wav:
            self.assertEqual(wav.getnchannels(), 2)
            self.assertEqual(wav.getframerate(), 48000)
            self.assertEqual(wav.getnframes(), 4)
            frames = wav.readframes(wav.getnframes())
        samples = struct.unpack("<%dh" % (len(frames) // 2), frames)
        left = samples[0::2]
        right = samples[1::2]
        self.assertEqual(left[2], 32767)  # 1.0 clipped to full scale
        self.assertEqual(left[3], -32767)
        self.assertLess(right[0], right[1])  # -0.5 vs -0.25 -> more negative first
        self.assertLess(right[0], 0)

    def test_mono_is_the_l_r_average(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        path = str(SCRATCH / "ring_a_mono.wav")
        rings = [self._ring([1.0], [-1.0])]
        h.write_ring_a_wav(path, rings, sample_rate=48000, stereo=False)
        with wave.open(path, "rb") as wav:
            self.assertEqual(wav.getnchannels(), 1)
            frames = wav.readframes(wav.getnframes())
        (value,) = struct.unpack("<1h", frames)
        self.assertEqual(value, 0)  # 0.5*(1.0 + -1.0) == 0.0


class ReferenceRenderTest(unittest.TestCase):
    def test_unity_step_identity_coefficients_pass_input_through(self):
        # A one-phase, one-tap "coefficient table" of [1.0] at unity step
        # degenerates the polyphase interpolator to a single-sample lookup
        # at ``base + t`` = ``base`` for a 1-tap table -- forward taps, not
        # centered (this lane's own finding, 2026-09-25: an impulse-
        # response sweep against a concrete FUN_1c4f81 run showed the real
        # DO 64 loop reads ``sample[base + t]`` for t=0..5, never behind
        # ``base`` -- see reference_render()'s own docstring); the
        # following 2:1 decimation then averages consecutive pairs.
        samples = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
        table = [[1.0]]
        out = h.reference_render(samples, table, step=1.0, n_output=3)
        self.assertEqual(len(out), 3)
        # interpolated[k] = samples[k] for k=0..5: [1,2,3,4,5,6], decimated
        # pairwise.
        self.assertEqual(out, [1.5, 3.5, 5.5])

    def test_reads_past_the_end_as_zero(self):
        table = [[1.0]]
        # The lone real sample sits at index 1 so the tap offset above
        # (base + 0) reaches it one step later, at output position 0's
        # second (odd) interpolated point -- still decimated into output
        # index 0.
        out = h.reference_render([0.0, 1.0], table, step=1.0, n_output=4)
        self.assertEqual(len(out), 4)
        self.assertEqual(out[0], 0.5)
        self.assertEqual(out[1:], [0.0, 0.0, 0.0])


class DefaultSampleLenTest(unittest.TestCase):
    """Task 1's --sample-len auto-sizing (main()'s own comment, and
    _default_sample_len()'s docstring): the same "+1 block, +32 tap-window
    pad" margin check_correctness() computes inline for each of its own
    cases."""

    def test_matches_check_correctness_own_formula_at_unity_step(self):
        n_blocks, pitch_step = 8, 1.0
        expected = int((n_blocks + 1) * 64 * pitch_step) + 32
        self.assertEqual(h._default_sample_len(n_blocks, pitch_step), expected)

    def test_scales_with_pitch_step(self):
        # A higher pitch_step consumes source samples faster per block, so
        # the auto-sized buffer must grow with it, not stay fixed.
        low = h._default_sample_len(8, 0.5)
        unity = h._default_sample_len(8, 1.0)
        high = h._default_sample_len(8, 2.0)
        self.assertLess(low, unity)
        self.assertLess(unity, high)

    def test_scales_with_blocks(self):
        self.assertLess(
            h._default_sample_len(4, 1.0),
            h._default_sample_len(16, 1.0),
        )


if __name__ == "__main__":
    unittest.main()
