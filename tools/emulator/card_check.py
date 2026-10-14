#!/usr/bin/env python3
"""Headless check of the in-process DT2 card: three WAV files, track N plays file N.

    uv run python -m tools.emulator.card_check [--run-dir DIR] [--mode direct|select]

Writes three 48 kHz mono 16-bit files (220 Hz sine, 880 Hz sine, white noise;
out/card-check/samples), starts the windowless daemon `digi-emud` on the DT2
1.16 ready inputs, and sends it `card.load` (the controller builds the card in
process, cold-boots the DT2 with it to ready or restores the cached ready state,
and swaps its runtime). Then for track N = 1..3: the emulator's own PCM is captured
while panel TRIG N is held (a TRIG n press plays track n) and analysed: track 1 must
peak near 220 Hz, track 2 near 880 Hz, track 3 must be broadband (no dominant partial).
`--mode select` presses TRK+TRIG N and then TRIG 1 instead (it still sounds track 1).

The run directory (default out/card-check/run) keeps the card cache between
runs, so the second run shows the cached boot. Exit status 0 only if all
three tracks sound as expected. The audio is not played (--no-audio).
"""

from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import math
import os
import pathlib
import random
import struct
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
from tools.emulator import digi_env
from tools.control.digi_client import Emu

TRK, TRIG1 = 16, 25


def native_emu():
    loader = importlib.machinery.SourceFileLoader(
        "native_emu", str(ROOT / "tools" / "emulator" / "native_emu.py")
    )
    spec = importlib.util.spec_from_loader("native_emu", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def write_wav(path: pathlib.Path, samples: list[int]) -> None:
    data = struct.pack(f"<{len(samples)}h", *samples)
    header = (
        b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, 48000, 96000, 2, 16)
        + b"data" + struct.pack("<I", len(data))
    )  # fmt: skip
    path.write_bytes(header + data)


def make_samples(directory: pathlib.Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    n = 48000
    for name, hz in (("1-sine220.wav", 220.0), ("2-sine880.wav", 880.0)):
        write_wav(
            directory / name,
            [
                int(0.5 * 32767 * math.sin(2 * math.pi * hz * i / 48000))
                for i in range(n)
            ],
        )
    rng = random.Random(7)
    write_wav(
        directory / "3-noise.wav",
        [int(rng.uniform(-0.5, 0.5) * 32767) for _ in range(n)],
    )


def read_left(path: pathlib.Path) -> list[float]:
    data = path.read_bytes()
    at = data.find(b"data")
    size = struct.unpack_from("<I", data, at + 4)[0]
    size = min(size, len(data) - at - 8)
    floats = struct.unpack_from("<%df" % (size // 4), data, at + 8)
    return list(floats[0::2])


def analyse(left: list[float], rate: float = 48000.0) -> dict:
    import numpy as np

    x = np.asarray(left, dtype=np.float64)
    loud = np.flatnonzero(np.abs(x) > 1e-3)
    if loud.size == 0:
        return {"silent": True}
    start = int(loud[0]) + 2400
    n = 8192
    seg = x[start : start + n]
    if seg.size < n:
        return {"silent": False, "short": True}
    power = np.abs(np.fft.rfft(seg * np.hanning(n))) ** 2
    power[0] = 0
    peak = int(np.argmax(power))
    share = float(power[max(0, peak - 3) : peak + 4].sum() / power.sum())
    return {
        "silent": False,
        "onset_s": float(loud[0]) / rate,
        "peak_hz": peak * rate / n,
        "peak_share": share,
        "rms": float(np.sqrt(np.mean(seg**2))),
    }


def judge(track: int, result: dict) -> tuple[bool, str]:
    if result.get("silent") or result.get("short"):
        return False, "no sound" if result.get("silent") else "capture too short"
    hz, share = result["peak_hz"], result["peak_share"]
    if track in (1, 2):
        want = 220.0 if track == 1 else 880.0
        ok = abs(hz - want) / want < 0.04 and share > 0.8
        return ok, f"peak {hz:.1f} Hz (want ~{want:.0f}), share {share:.3f}"
    return share < 0.2, f"broadband: peak share {share:.3f} (want < 0.2)"


def build_daemon(syx: pathlib.Path) -> pathlib.Path:
    done = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "emulator" / "native_emu.py"), "--daemon",
         "--build-only", str(syx)],
        cwd=ROOT, capture_output=True, text=True,
    )  # fmt: skip
    if done.returncode:
        raise SystemExit(f"building digi-emud failed:\n{done.stderr[-2000:]}")
    return pathlib.Path(done.stdout.strip().splitlines()[-1])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--run-dir", type=pathlib.Path, default=ROOT / "out/card-check/run")
    p.add_argument(
        "--hold",
        type=int,
        default=40_000_000,
        help="TRIG 1 hold (ColdFire instructions)",
    )
    p.add_argument(
        "--audio-s", type=float, default=1.5, help="seconds captured per track"
    )
    p.add_argument(
        "--mode",
        choices=("direct", "select"),
        default="direct",
        help="direct: TRIG n sounds track n; select: TRK+TRIG n, then TRIG 1",
    )
    p.add_argument(
        "--settle",
        type=int,
        default=20_000_000,
        help="guest instructions to wait after the card is up",
    )
    a = p.parse_args(argv)
    work = ROOT / "out" / "card-check"
    samples = work / "samples"
    make_samples(samples)
    syx = digi_env.firmware_for("dt2")
    ne = native_emu()
    args = ne.default_audio_args([str(syx)])
    if "--auto-coupled" not in args:
        raise SystemExit("no DT2 ready inputs: run `mise run ready -- dt2` once")
    ne.set_fast_regions(args, os.environ)
    binary = build_daemon(syx)
    t0 = time.monotonic()
    ok_all = True
    with Emu.spawn(*args, "--no-audio", binary=binary, run_dir=a.run_dir) as emu:
        print(f"daemon up in {time.monotonic() - t0:.1f} s")
        t1 = time.monotonic()
        loaded = emu.call("card.load", dir=str(samples))
        print(
            f"card.load: {time.monotonic() - t1:.1f} s wall "
            f"(build {loaded['build_seconds']:.1f} s, boot {loaded['boot_seconds']:.1f} s, "
            f"card cached {loaded['card_cached']}, ready cached {loaded['ready_cached']}, "
            f"audio {loaded['audio']}, samples {loaded['samples']})"
        )
        emu.step(
            a.settle
        )  # NO closes FILE SYSTEM OK; the firmware finishes "Loading..."
        for track in (1, 2, 3):
            trig = TRIG1 + track - 1
            if a.mode == "select":
                # TRK held + TRIG n selects track n; TRIG 1 then sounds the selected track.
                emu.call("input.button", code=TRK, down=True)
                emu.step(2_000_000)
                emu.call("input.button", code=trig, down=True)
                emu.step(3_000_000)
                emu.call("input.button", code=trig, down=False)
                emu.step(2_000_000)
                emu.call("input.button", code=TRK, down=False)
                emu.step(5_000_000)
                trig = TRIG1
            wav = work / f"track{track}.wav"
            emu.call("pcm.tap", action="capture", path=str(wav))
            emu.call("input.button", code=trig, down=True)
            emu.step(a.hold)
            emu.call("input.button", code=trig, down=False)
            frames = emu.call("status")["dsp_frames"]
            emu.call("run_until", frames=frames + int(a.audio_s * 1500))
            emu.call("pcm.tap", action="capture_stop")
            ok, text = judge(track, analyse(read_left(wav)))
            ok_all &= ok
            print(f"track {track}: {'ok' if ok else 'FAIL'}  {text}")
        audio = emu.call("audio.report")["audio"]
        print("underruns/missing blocks:", audio.get("missing_blocks"))
    print("PASS" if ok_all else "FAIL")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
