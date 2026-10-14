"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import math
import time
from collections.abc import Sequence

from tools.sharc_dev import sharc_dac

from .audio import _thdn_dbc, decimate, flatten, measure_tone, write_wav
from .check import _default_sample_len, check_correctness
from .frame import FRAME_PATCH_TABLE, render_frames
from .layout import SOURCE_SAMPLE_RATE
from .mix import read_master_mix, render_frames_to_ring_a
from .runner import InitResult, load_image_memory, run_init
from .voice import render_blocks


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("image", help='sharc.py image name, e.g. "dt2-1.16"')
    p.add_argument("--voice", type=int, default=0)
    p.add_argument("--blocks", type=int, default=8)
    p.add_argument(
        "--pitch-step",
        type=float,
        default=1.0,
        help="input samples per output (pre-interpolation) step; 1.0 plays "
        "the source at its own rate (SOURCE_SAMPLE_RATE), 2.0 at double "
        "speed/pitch, 0.5 at half",
    )
    p.add_argument(
        "--sample-len",
        type=int,
        default=None,
        help="source buffer length, in samples; default: auto-sized from "
        "--blocks/--pitch-step (--frame: 4096) so a long render does not "
        "run off the end of its own synthetic sample (see "
        "_default_sample_len())",
    )
    p.add_argument(
        "--freq",
        type=float,
        default=1000.0,
        help="the source sample's own frequency, in Hz, at "
        "SOURCE_SAMPLE_RATE (the firmware's sample-playback rate, "
        "96 kHz -- see that constant's docstring); playing it at "
        "--pitch-step produces an audible output frequency of "
        "freq * pitch_step Hz (Hz is invariant under the render's own 2:1 "
        "decimation -- see main()'s own comment below for the derivation)",
    )
    p.add_argument("--out", default=None, help="WAV output path")
    p.add_argument(
        "--sample-rate",
        type=int,
        default=48000,
        help="the WAV file's declared playback rate; should match the "
        "render's actual decimated output rate (SOURCE_SAMPLE_RATE / 2 = "
        "48000) for --freq * --pitch-step to sound right on playback",
    )
    p.add_argument("--run-init", action="store_true", default=False)
    p.add_argument("--check-correctness", action="store_true", default=False)
    p.add_argument(
        "--frame",
        action="store_true",
        default=False,
        help="call render_frame (FUN_1c2b24) via block_handler instead of "
        "one voice's render (see render_frames()/call_frame())",
    )
    p.add_argument(
        "--frames", type=int, default=1, help="--frame only: number of frame calls"
    )
    p.add_argument(
        "--no-frame-patches",
        dest="frame_patches",
        action="store_false",
        default=True,
        help="--frame only: run without FRAME_PATCH_TABLE (stop at the first fork)",
    )
    p.add_argument(
        "--ring-a",
        action="store_true",
        default=False,
        help="--frame only: render --frames frames of one voice, injecting "
        "each frame's real decimated output into --track's master-mix "
        "input (see inject_track_buffer()'s own module note for why this "
        "is needed, and render_frames_to_ring_a() for the technique), and "
        "write ring A (the DAC-facing buffer) to --out as a WAV instead of "
        "reporting the master mix",
    )
    p.add_argument(
        "--track",
        type=int,
        default=0,
        help="--ring-a only: which of the 16 master-mix track inputs to "
        "inject the voice's rendered output into",
    )
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)

    memory = load_image_memory(a.image)

    if a.frame and a.ring_a:
        result = render_frames_to_ring_a(
            memory,
            a.image,
            n_frames=a.frames,
            voice=a.voice,
            freq=a.freq,
            pitch_step=a.pitch_step,
            track=a.track,
            patch_table=FRAME_PATCH_TABLE if a.frame_patches else None,
        )
        sample_rate = int(SOURCE_SAMPLE_RATE // 2)
        output_frequency_hz = a.freq * a.pitch_step
        # Discard the first frame's worth of decimated samples: this
        # lane's own experiments (see the report) found the master stage's
        # own gain/declick ramp attenuates the very first frame or two
        # before a steady level settles, so measuring frequency/level from
        # frame 0 alone would be measuring the ramp, not the tone.
        #
        # Per-channel, not a mono downmix: see the "ring A's own L channel
        # is deliberately negated" module note above MASTER_MIX_INJECT_PC --
        # a coherent tone written identically to both channels arrives at
        # ring A as (-L, +R), so a naive (L+R)/2 downmix cancels to silence.
        measurement_left = measure_tone(
            result["ring_a_left"], output_frequency_hz, sample_rate, discard=32
        )
        measurement_right = measure_tone(
            result["ring_a_right"], output_frequency_hz, sample_rate, discard=32
        )
        if a.out:
            sharc_dac.write_wav_stereo(
                a.out,
                result["ring_a_left"],
                result["ring_a_right"],
                sample_rate=sample_rate,
            )
        report = {
            "image": a.image,
            "voice": a.voice,
            "track": a.track,
            "frames": a.frames,
            "freq_hz": a.freq,
            "output_frequency_hz": output_frequency_hz,
            "any_new_stop": result["any_new_stop"],
            "per_frame": result["per_frame"],
            "measurement_left": measurement_left,
            "measurement_right": measurement_right,
            "thdn_dbc_left": _thdn_dbc(measurement_left["power_ratio"]),
            "thdn_dbc_right": _thdn_dbc(measurement_right["power_ratio"]),
            "out": a.out,
        }
        if a.json:
            print(json.dumps(report, indent=2, default=str))
        else:
            print(
                "image: %s  voice: %d  track: %d  frames: %d  output: %.2f Hz"
                % (a.image, a.voice, a.track, a.frames, output_frequency_hz)
            )
            print("any_new_stop:", result["any_new_stop"])
            for label, measurement, thdn in (
                ("L", measurement_left, report["thdn_dbc_left"]),
                ("R", measurement_right, report["thdn_dbc_right"]),
            ):
                print(
                    "measured %s: freq=%.2f Hz power_ratio=%.4f rms_dbfs=%s "
                    "peak_dbfs=%s thdn_dbc=%s"
                    % (
                        label,
                        measurement["freq_hz"],
                        measurement["power_ratio"],
                        "%.2f" % measurement["rms_dbfs"]
                        if measurement["rms_dbfs"] is not None
                        else "-inf",
                        "%.2f" % measurement["peak_dbfs"]
                        if measurement["peak_dbfs"] is not None
                        else "-inf",
                        "%.2f" % thdn if thdn is not None else "n/a",
                    )
                )
            if a.out:
                print("wrote", a.out)
        return 0

    if a.frame:
        frame_sample_len = a.sample_len if a.sample_len is not None else 4096
        runner, results = render_frames(
            memory,
            a.image,
            n_frames=a.frames,
            voice=a.voice,
            pitch_step=a.pitch_step,
            sample_len=frame_sample_len,
            # The source is a --freq Hz tone at SOURCE_SAMPLE_RATE (task 1;
            # see that constant's docstring and this function's own comment
            # below, before the non-frame render path) -- not pre-scaled by
            # pitch_step, which the DO-64 interpolation loop itself already
            # applies by advancing pitch_step raw samples per output step.
            samples=[
                math.sin(2 * math.pi * (a.freq / SOURCE_SAMPLE_RATE) * i)
                for i in range(frame_sample_len)
            ],
            patch_table=FRAME_PATCH_TABLE if a.frame_patches else None,
        )
        report = {
            "image": a.image,
            "voice": a.voice,
            "frames": a.frames,
            "halts": [r.halt.reason for r in results],
            "instructions": [r.instructions for r in results],
            "master_mix": read_master_mix(memory, a.image, runner),
        }
        if a.json:
            print(json.dumps(report, indent=2, default=str))
        else:
            print("image: %s  voice: %d  frames: %d" % (a.image, a.voice, a.frames))
            for i, r in enumerate(results):
                print(
                    "frame %d: halt=%s pc=%#x instructions=%d"
                    % (i, r.halt.reason, r.halt.pc_sw, r.instructions)
                )
            mix = report["master_mix"]
            print(
                "master_mix[0:8]=%s max_abs=%.4g"
                % (
                    ["%.4g" % v for v in mix[:8]],
                    max((abs(v) for v in mix), default=0.0),
                )
            )
        return 0

    init: InitResult | None = None
    if a.run_init:
        init = run_init(memory, a.image)
    # Only feed a *successful* init into the render/correctness paths --
    # new_runner(..., init=...) raises on an init that did not complete, and
    # a failed --run-init should still fall back to reporting rather than
    # crashing the whole invocation. init.ran/init.error are still reported
    # below either way.
    active_init = init if (init is not None and init.ran) else None

    # Task 1's CLI semantics fix (2026-09-25), correcting this harness's own
    # earlier bug: the source PCM this harness writes to sample_base is a
    # --freq Hz tone at SOURCE_SAMPLE_RATE (the firmware's own
    # sample-playback rate -- see that constant's docstring), not
    # pre-scaled by pitch_step. The DO-64 interpolation loop itself already
    # applies pitch_step (it advances that many raw samples per output
    # step; see setup_voice()'s FIELD_STEP), so folding pitch_step into the
    # source too (the previous version's ``(freq/sample_rate)*pitch_step``)
    # double-counted it. Measured before this fix, on the WAV output: with
    # --sample-rate left at its 48000 default, audible frequency was
    # ``2 * freq * pitch_step**2`` (the extra factor of 2 from the WAV
    # being written at 48000 while the pre-scaled source's own
    # cycles-per-sample was computed against that same 48000, one octave
    # below the 96 kHz the DO-64 domain and its 2:1 decimator actually
    # assume -- see decimate()'s docstring). Corrected: the output's
    # audible frequency is ``freq * pitch_step`` Hz, full stop -- Hz is
    # invariant under the render's own 2:1 decimation to
    # SOURCE_SAMPLE_RATE/2 (48 kHz, matching write_wav()'s own 48000
    # default), so it does not depend on --sample-rate at all; --sample-rate
    # only needs to match that 48 kHz decimated rate for the WAV to *play
    # back* at the frequency its own header claims (this lane verified the
    # corrected formula empirically: tests/sharc/integration/test_sharc_harness_cli.py's
    # CliFrequencyTest zero-crossing-counts a rendered WAV against
    # freq * pitch_step for several (freq, pitch_step) pairs).
    output_frequency_hz = a.freq * a.pitch_step
    samples = [
        math.sin(2 * math.pi * (a.freq / SOURCE_SAMPLE_RATE) * i)
        for i in range(
            a.sample_len
            if a.sample_len is not None
            else _default_sample_len(a.blocks, a.pitch_step)
        )
    ]
    sample_len = len(samples)

    start = time.perf_counter()
    blocks, results = render_blocks(
        memory,
        a.image,
        a.voice,
        a.blocks,
        pitch_step=a.pitch_step,
        sample_len=sample_len,
        samples=samples,
        init=active_init,
    )
    elapsed = time.perf_counter() - start
    total_instructions = sum(r.instructions for r in results)

    if a.out:
        write_wav(
            a.out,
            flatten([decimate(block) for block in blocks]),
            sample_rate=a.sample_rate,
        )

    correctness = (
        check_correctness(memory, a.image, init=active_init)
        if a.check_correctness
        else None
    )

    report = {
        "image": a.image,
        "voice": a.voice,
        "blocks": a.blocks,
        "pitch_step": a.pitch_step,
        "freq_hz": a.freq,
        "output_frequency_hz": output_frequency_hz,
        "sample_len": sample_len,
        "instructions_total": total_instructions,
        "instructions_per_block": total_instructions / a.blocks if a.blocks else None,
        "elapsed_s": elapsed,
        "instructions_per_second": total_instructions / elapsed
        if elapsed > 0
        else None,
        "halts": [r.halt.reason for r in results],
        "out": a.out,
        "init": None if init is None else {"ran": init.ran, "error": init.error},
        "correctness": correctness,
    }
    if a.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print("image: %s  voice: %d  blocks: %d" % (a.image, a.voice, a.blocks))
        print(
            "source: %.2f Hz @ %.0f Hz (sample_len=%d)  pitch_step=%g  "
            "output: %.2f Hz"
            % (
                a.freq,
                SOURCE_SAMPLE_RATE,
                sample_len,
                a.pitch_step,
                output_frequency_hz,
            )
        )
        if init is not None:
            print("init:", "ok" if init.ran else "failed: %s" % init.error)
        print("instructions/block: %.1f" % report["instructions_per_block"])
        print(
            "elapsed: %.3fs (%.0f instr/s)"
            % (elapsed, report["instructions_per_second"] or 0.0)
        )
        print("halts:", report["halts"])
        if a.out:
            print("wrote", a.out)
        if correctness is not None:
            for name, case in correctness.items():
                print(
                    "correctness[%s]: max_abs_error=%.4g mean_abs_error=%.4g "
                    "phase_deltas=%s (expected %.1f) active=%s"
                    % (
                        name,
                        case["max_abs_error"],
                        case["mean_abs_error"],
                        ["%.2f" % d for d in case["phase_deltas"]],
                        case["phase_delta_expected"],
                        case["active"],
                    )
                )
                print(
                    "  record diffs (changed word offsets per block): %s"
                    % [
                        [hex(d["offset"]) for d in diffs]
                        for diffs in case["record_diffs"]
                    ]
                )
    return 0
