"""sharc_harness command line."""

import math
import os
import pathlib
import struct
import sys
import unittest
import wave

from support.paths import REPO_ROOT

sys.path.insert(0, os.path.join(str(REPO_ROOT), "tools"))
import sharc_harness as h  # noqa: E402

DT2_116_BLOB = pathlib.Path("out/sections/dt2-1.16/section_7_BLOB.bin")
SCRATCH = (
    pathlib.Path(os.environ.get("CLAUDE_SCRATCHPAD", "/tmp")) / "test_sharc_harness"
)


@unittest.skipUnless(DT2_116_BLOB.exists(), "DT2 1.16 firmware bytes are not available")
class CliFrequencyTest(unittest.TestCase):
    """Task 1's CLI semantics fix, checked end-to-end through main() itself
    (not just render_blocks()/setup_voice() internals): the WAV main()
    writes for a given --freq/--pitch-step must actually sound at
    freq * pitch_step Hz (see main()'s own comment for the corrected
    derivation, and SOURCE_SAMPLE_RATE's docstring for why).

    Verified by direct correlation against the *exact* expected frequency
    (continuous, not restricted to an FFT bin) rather than an FFT: for a
    single embedded sinusoid, correlating a window of N samples against
    cos/sin at its own exact frequency reconstructs that sinusoid's power
    with no leakage regardless of how many whole cycles fit the window (only
    an FFT bin readout has a coherent-sampling requirement) -- so this does
    not need this module's own THD+N measurement's coherent-bin machinery
    (see the module docstring's "CLI semantics corrected" section) to tell
    "the output is at the frequency main() claims" from "it is not".
    """

    @classmethod
    def setUpClass(cls):
        cls.memory = h.load_image_memory("dt2-1.16")
        SCRATCH.mkdir(parents=True, exist_ok=True)

    def _dominant_frequency_power_ratio(
        self, path: str, freq_hz: float, discard_samples: int = 32
    ) -> float:
        with wave.open(path, "rb") as wav:
            sample_rate = wav.getframerate()
            frames = wav.readframes(wav.getnframes())
        values = struct.unpack("<%dh" % (len(frames) // 2), frames)
        samples = [v / 32768.0 for v in values[discard_samples:]]
        n = len(samples)
        mean = sum(samples) / n
        total_power = sum((v - mean) ** 2 for v in samples) / n
        a_cos = (
            sum(
                samples[i] * math.cos(2 * math.pi * freq_hz * i / sample_rate)
                for i in range(n)
            )
            * 2.0
            / n
        )
        a_sin = (
            sum(
                samples[i] * math.sin(2 * math.pi * freq_hz * i / sample_rate)
                for i in range(n)
            )
            * 2.0
            / n
        )
        amplitude = math.hypot(a_cos, a_sin)
        fund_power = amplitude * amplitude / 2.0
        return fund_power / total_power if total_power else 0.0

    def _render_and_check(self, freq: float, pitch_step: float, blocks: int = 96):
        path = str(SCRATCH / ("cli_freq_%g_%g.wav" % (freq, pitch_step)))
        rc = h.main(
            [
                "dt2-1.16",
                "--blocks",
                str(blocks),
                "--pitch-step",
                str(pitch_step),
                "--freq",
                str(freq),
                "--out",
                path,
            ]
        )
        self.assertEqual(rc, 0)
        return self._dominant_frequency_power_ratio(path, freq * pitch_step)

    def test_unity_pitch_step_output_is_at_freq(self):
        ratio = self._render_and_check(500.0, 1.0)
        self.assertGreater(ratio, 0.9, "expected 500 Hz to dominate at pitch_step=1.0")

    def test_double_pitch_step_output_is_at_double_freq(self):
        # The old, wrong CLI would have put this at 2 * 500 * 2**2 = 4000 Hz
        # instead of 500 * 2 = 1000 Hz; checking the ratio at the *corrected*
        # target frequency (not the old formula's) is itself the regression
        # guard for task 1's fix.
        ratio = self._render_and_check(500.0, 2.0)
        self.assertGreater(
            ratio, 0.9, "expected 1000 Hz (500 * 2) to dominate at pitch_step=2.0"
        )

    def test_half_pitch_step_output_is_at_half_freq(self):
        ratio = self._render_and_check(250.0, 0.5)
        self.assertGreater(
            ratio, 0.9, "expected 125 Hz (250 * 0.5) to dominate at pitch_step=0.5"
        )

    def test_auto_sample_len_covers_the_requested_blocks_without_deactivating(self):
        # Task 1's other half: --sample-len omitted must auto-size large
        # enough that a long render does not run off the buffer (see
        # _default_sample_len()). 96 blocks * 64 samples/block is well past
        # the old fixed default of 4096.
        path = str(SCRATCH / "cli_auto_len.wav")
        rc = h.main(
            [
                "dt2-1.16",
                "--blocks",
                "96",
                "--pitch-step",
                "1.0",
                "--freq",
                "500",
                "--out",
                path,
            ]
        )
        self.assertEqual(rc, 0)
        with wave.open(path, "rb") as wav:
            # 96 blocks * 32 decimated samples/block.
            self.assertEqual(wav.getnframes(), 96 * 32)


if __name__ == "__main__":
    unittest.main()
