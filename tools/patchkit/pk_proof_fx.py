#!/usr/bin/env python3
"""patchkit slice proof: core/fx is invisible with an empty chain and test/fx-gain halves the master in the DAC and the USB ring.

    uv run python -m tools.patchkit.pk_proof_fx [dt2|dn2] [--run-dir DIR] [--out DIR] [--only empty,gain] [--quick] [--jobs N]

DT2 1.17 or DN2 1.12, the stock firmware and ready state of tools/patchkit/pk_harness.py. Each part starts a coupled daemon on the stock firmware,
plays the same note (DT2 a one-sample card and TRIG 1; DN2 the scheduled note) in a session and, after the same number of DSP frames, reads the master
block, the DAC ring (A) and the USB ring (C) with `dsp.peek` (tools/patchkit/fx_rings.py has the formats and the addresses). The PCM tap records the
DAC side of the whole run.

  empty   stock, then the image with `core/fx` alone (no effect in the chain):
            PCM bit-identical to stock; master, ring A, ring C and the flag bit-identical at the snapshot frame.
  gain    the image with `core/fx` and `test/fx-gain` (0.5):
            master = 0.5 x stock (exact); ring A = the DAC conversion of that master; ring C master pair = the USB conversion of it; ring C
            channels 2 to 15 bit-identical to stock (the effect is in USB MAIN and does not touch the track outputs); PCM -6.0206 dB within 2 ulps.
          the same image with the gain word set to 1.0 with `dsp.patch`: master, rings equal to stock again.
          the same image with the chain row's `enabled` word cleared in mid-run: master, rings equal to stock after the fade-out block.

The note's frame count is the same in every run (the steps are instruction counts), so the snapshots of two runs are the same moment.
Requires DIGI_EMUD (the digi-emud binary of `mise run emu:serve -- --build-only`) or builds it.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import struct
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
from tools.control.digi_client import Emu  # noqa: E402
from tools.emulator import card_check  # noqa: E402
from tools.patchkit import fx_rings as R  # noqa: E402
from tools.patchkit import hook_ab  # noqa: E402
from tools.patchkit import pk_harness as H  # noqa: E402
from tools.patchkit import pk_proofkit as K  # noqa: E402
from tools.patchkit.patched_audio_check import DN2_NOTE, TRIG1  # noqa: E402

FW = {"dt2": "1.17", "dn2": "1.12"}
EXTRA = 150  # frames after the note has started, to the snapshot
CORE = "core/fx@0.1.0"
GAIN = "test/fx-gain@0.1.0"


def f32_hex(x: float) -> str:
    return f"{struct.unpack('<I', struct.pack('<f', x))[0]:08x}"


class Image:
    """A patched image and the addresses of its plan (symbols of the fixture)."""

    def __init__(self, device: str, mods: list[str]):
        self.cell = H.Cell(device, FW[device], mods)
        self.stock, self.ready = H.firmware_inputs(self.cell)
        if mods:
            self.syx = pathlib.Path(H.patched_image(self.cell, self.stock)).resolve()
            plan = json.loads(self.syx.with_suffix(".plan.json").read_text())
            self.sym = plan["symbols"]
        else:
            self.syx, self.sym = self.stock, {}


def play(
    emu: Emu,
    device: str,
    wav: pathlib.Path,
    samples_card: bool,
    patches: list[str],
    mid_patch: list[str] | None,
    frame_hint: int | None,
):
    """Restart, apply the DSP patches, play the note with the PCM tap on, stop EXTRA frames after the note started
    (MID_PATCH is applied half way) and return (pcm, snapshot)."""
    emu.restart(auto_no=True)
    for p in patches:
        emu.patch(text=p)
    if device == "dn2":
        emu.call("pcm.tap", action="capture", path=str(wav))
        emu.schedule(DN2_NOTE)
        emu.run_until(audio_s=1.0, timeout_wall_s=900)
    else:
        emu.step(20_000_000)
        emu.call("pcm.tap", action="capture", path=str(wav))
        emu.call("input.button", code=TRIG1, down=True)
        emu.step(40_000_000)
        emu.call("input.button", code=TRIG1, down=False)
    fr = emu.call("status")["dsp_frames"]
    if mid_patch:
        emu.call("run_until", frames=fr + EXTRA // 2)
        for p in mid_patch:
            emu.patch(text=p)
    emu.call("run_until", frames=fr + EXTRA)
    frame = emu.call("status")["dsp_frames"]
    snap = R.snap(emu, device, frame)
    emu.call("pcm.tap", action="capture_stop")
    return hook_ab.read_wav(wav), snap


def same(a: R.Snap, b: R.Snap) -> dict[str, bool]:
    return {
        "frame": a.frame == b.frame,
        "flag": a.flag == b.flag,
        "master": a.master == b.master,
        "ring_a": a.a_words() == b.a_words(),
        "ring_c": a.c_words() == b.c_words(),
    }


class FxProof(K.Proof):
    tool = "fx"
    args: argparse.Namespace

    def _session(self, image: Image, name: str):
        a = self.args
        run_dir = (
            pathlib.Path(a.run_dir)
            if a.run_dir
            else ROOT / "out" / "runs" / "patchkit" / f"run-fx-{a.device}-{name}"
        )
        return Emu.spawn_coupled(
            image.stock,
            image.ready,
            timeout=300,
            run_dir=run_dir,
            binary=H.daemon_binary(a.device, image.stock),
        )

    def _card(self, emu: Emu):
        if self.args.device == "dt2":
            samples = self.out / "samples"
            card_check.make_samples(samples)
            emu.call("card.load", dir=str(samples))

    def _run_image(
        self,
        name: str,
        mods: list[str],
        runs: dict[str, tuple[list[str], list[str] | None]],
    ):
        """Start a daemon, load the image (stock when MODS is empty) and play each run of RUNS (name -> (patches, mid patch))."""
        a = self.args
        image = Image(a.device, mods)
        out = {}
        with self._session(image, name) as emu:
            self._card(emu)
            if mods:
                emu.call("firmware.load", syx=str(image.syx), auto_no=True)
            for rn, (patches, mid) in runs.items():
                out[rn] = play(
                    emu,
                    a.device,
                    self.out / f"{name}-{rn}.wav",
                    a.device == "dt2",
                    patches,
                    mid,
                    None,
                )
        return image, out

    def part_empty(self) -> None:
        t0 = time.monotonic()
        _, st = self._run_image("stock", [], {"run": ([], None)})
        _, em = self._run_image("empty", ["core/fx"], {"run": ([], None)})
        self.timings["run_s"] = time.monotonic() - t0
        (pcm0, s0), (pcm1, s1) = st["run"], em["run"]
        r = hook_ab.compare(pcm0, pcm1, 1.0)
        self.check(
            "PCM of the image with an empty chain is bit-identical to stock",
            bool(r["identical"] and r["active_samples"] > 1000),
            f"{r['active_samples']} active samples",
        )
        for what, ok in same(s0, s1).items():
            self.check(f"{what} at frame {s0.frame} is bit-identical to stock", ok)
        self.check(
            "the snapshot holds a sound (the comparison means something)",
            max(abs(x) for x in s0.master_floats()) * 2**31 > 1e5,
            f"peak {max(abs(x) for x in s0.master_floats()):.2e}",
        )

    def part_gain(self) -> None:
        a = self.args
        t0 = time.monotonic()
        _, st = self._run_image("stock", [], {"run": ([], None)})
        image = Image(a.device, ["core/fx", "test/fx-gain"])
        gain_addr = image.sym[f"{GAIN}:_fg_gain"]
        enabled_addr = image.sym[f"{CORE}:fx.rows"] + 4 * 3  # row 0, word 3
        _, runs = self._run_image(
            "gain",
            ["core/fx", "test/fx-gain"],
            {
                "half": ([], None),
                "unity": ([f"DM {gain_addr:x} {f32_hex(1.0)}"], None),
                "off": ([], [f"DM {enabled_addr:x} 0"]),
            },
        )
        self.timings["run_s"] = time.monotonic() - t0
        pcm0, s0 = st["run"]
        pcm_h, sh = runs["half"]
        pcm_u, su = runs["unity"]
        pcm_o, so = runs["off"]
        m0, mh = s0.master_floats(), sh.master_floats()
        self.check(
            "same moment in every run",
            s0.frame == sh.frame == su.frame == so.frame,
            f"frame {s0.frame}",
        )
        self.check(
            "master = 0.5 x stock, exactly",
            all(h == R.f32(m * 0.5) for h, m in zip(mh, m0, strict=True)),
        )
        ea = R.ring_a_error(sh)
        ec = R.ring_c_pair_error(sh)
        self.check(
            "ring A (DAC) is the conversion of the halved master",
            ea <= 1,
            f"largest error {ea:.2f} LSB",
        )
        self.check(
            "ring C master pair (USB MAIN) is the conversion of the halved master",
            ec <= 1,
            f"largest error {ec:.2f} LSB",
        )
        a0, ah = s0.a_words(), sh.a_words()
        c0, ch = s0.c_words(), sh.c_words()
        self.check(
            "ring A is half of stock (the DAC carries the effect)",
            max(
                min(abs(2 * h - x), abs(2 * h + x)) for h, x in zip(ah, a0, strict=True)
            )
            <= 2,
        )
        pair = [16 * k + j for k in range(32) for j in (0, 1)]
        self.check(
            "ring C master pair is half of stock (the USB carries the effect)",
            max(abs(2 * ch[i] - c0[i]) for i in pair) <= 2,
        )
        self.check(
            "ring C channels 2 to 15 are bit-identical to stock (the tracks are untouched)",
            R.c_others(sh) == R.c_others(s0),
        )
        r = hook_ab.compare(pcm0, pcm_h, 0.5)
        self.check(
            "PCM is 0.5 x stock: -6.0206 dB within 2 ulps",
            bool(
                r["length_equal"]
                and r["max_err_ulps"] <= 2.0
                and abs(r["rms_db"] + 6.0206) < 0.001
            ),
            f"rms {r['rms_db']:.4f} dB, max err {r['max_err_ulps']} ulps",
        )
        for what, ok in same(s0, su).items():
            self.check(f"gain word 1.0: {what} equals stock", ok)
        for what, ok in same(s0, so).items():
            self.check(f"effect switched off in mid-run: {what} equals stock", ok)
        out = {
            "device": a.device,
            "frame": s0.frame,
            "ring_a_error": ea,
            "ring_c_pair_error": ec,
            "gain_word": hex(gain_addr),
            "enabled_word": hex(enabled_addr),
        }
        (self.out / "report.json").write_text(json.dumps(out, indent=1))


PARTS = [
    K.Part("empty", "stock vs empty chain", FxProof.part_empty, True, 90),
    K.Part(
        "gain",
        "0.5 effect: master, DAC ring, USB ring; unity; off",
        FxProof.part_gain,
        True,
        240,
    ),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("device", nargs="?", default="dt2", choices=("dn2", "dt2"))
    ap.add_argument(
        "--run-dir",
        type=pathlib.Path,
        help="daemon run dir (default out/runs/patchkit/run-fx-DEV-NAME)",
    )
    K.add_args(ap, PARTS)
    a = ap.parse_args()
    a.out = a.out or ROOT / "out" / "runs" / "fx-proof" / a.device
    FxProof.args = a
    FxProof.device = a.device
    FxProof.fw = FW[a.device]
    return K.run_tool(__file__, FxProof, PARTS, a)


if __name__ == "__main__":
    sys.exit(main())
