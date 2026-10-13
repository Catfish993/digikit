#!/usr/bin/env python3
"""patchkit slice proof: the master-gain module changes the emulated audio exactly as designed.

    uv run python tools/pk_proof_gain.py [dt2|dn2] [--stock STOCK.syx] [--patched PATCHED.syx] [--ready DIR]
        [--run-dir DIR] [--out DIR] [--quick] [--jobs N]

With no arguments: DT2 1.17 (DN2: 1.12), the stock firmware and ready state of tools/pk_harness.py and the patch of
fx/master-gain built on the fly (out/patch/).

Starts a coupled daemon (tools/digi_client.py `Emu.spawn_coupled`) on the stock firmware, then in the same
session and from the same ready state (`restart` each time):

  1. stock                         -> reference PCM
  2. inject PATCHED (hot DSP load), gain word 1.0 as built      -> must be bit-identical to the reference
  3. same, gain word set to 0.5 with `dsp.patch` (DM <addr> 3f000000) -> RMS -6.0206 dB, error within 2 ulps

The gain word's address comes from PATCHED's plan (`<name>.plan.json` next to it, symbol `_mg_gain`).
The note: DN2 TRIG 1 then PLAY (3 s); DT2 a one-sample card (220 Hz sine) and TRIG 1 (1.5 s). Prints JSON and
exits 1 unless 2 and 3 hold. Requires DIGI_EMUD (the digi-emud binary of `mise run emu:serve -- --build-only`).
"""

from __future__ import annotations

import argparse
import json
import pathlib
import struct
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
try:
    import card_check
    import hook_ab
    import pk_harness as H
    import pk_proofkit as K
    from digi_client import Emu
    from patched_audio_check import DN2_NOTE, TRIG1
finally:
    sys.path.pop(0)


FW = {"dt2": "1.17", "dn2": "1.12"}


def f32_hex(x: float) -> str:
    return f"{struct.unpack('<I', struct.pack('<f', x))[0]:08x}"


def play(
    emu: Emu,
    device: str,
    wav: pathlib.Path,
    gain_addr: int | None,
    gain: float | None,
    card: str | None,
):
    """Restart to the ready state, optionally set the gain word, play the note and capture the PCM."""
    emu.restart(auto_no=True)
    if gain is not None:
        emu.patch(text=f"DM {gain_addr:x} {f32_hex(gain)}")
    if device == "dn2":
        emu.call("pcm.tap", action="capture", path=str(wav))
        emu.schedule(DN2_NOTE)
        emu.run_until(audio_s=3, timeout_wall_s=900)
    else:
        emu.step(20_000_000)
        emu.call("pcm.tap", action="capture", path=str(wav))
        emu.call("input.button", code=TRIG1, down=True)
        emu.step(40_000_000)
        emu.call("input.button", code=TRIG1, down=False)
        frames = emu.call("status")["dsp_frames"]
        emu.call("run_until", frames=frames + int(1.5 * 1500))
    emu.call("pcm.tap", action="capture_stop")
    return hook_ab.read_wav(wav)


class GainProof(K.Proof):
    tool = "gain"
    args: argparse.Namespace

    def part_run(self) -> None:
        a = self.args
        device = a.device
        cell = H.Cell(device, FW[device], ["fx/master-gain"])
        base_syx, base_ready = H.firmware_inputs(cell)
        stock = pathlib.Path(a.stock or base_syx).resolve()
        patched = pathlib.Path(a.patched or H.patched_image(cell, stock)).resolve()
        plan = json.loads(patched.with_suffix(".plan.json").read_text())
        gain_addr = next(
            v for k, v in plan["symbols"].items() if k.endswith(":_mg_gain")
        )
        ready = pathlib.Path(a.ready or base_ready)
        work = self.out
        out: dict = {
            "device": device,
            "stock": stock.name,
            "patched": patched.name,
            "gain_word": hex(gain_addr),
        }
        run_dir = a.run_dir or ROOT / "out" / "runs" / "patchkit" / f"run-gain-{device}"
        t0 = time.monotonic()
        with Emu.spawn_coupled(
            stock,
            ready,
            timeout=300,
            run_dir=run_dir,
            binary=H.daemon_binary(device, stock),
        ) as emu:
            card = None
            if device == "dt2":
                samples = work / "samples"
                card_check.make_samples(samples)
                card = emu.call("card.load", dir=str(samples))["card"]
            base = play(emu, device, work / "stock.wav", None, None, card)
            rep = emu.call("firmware.load", syx=str(patched), auto_no=True)
            out["inject"] = {
                k: rep.get(k)
                for k in ("start_used", "main_changed", "sections_changed", "dsp")
            }
            unity = play(emu, device, work / "unity.wav", None, None, card)
            half = play(emu, device, work / "half.wav", gain_addr, 0.5, card)
        self.timings["run_s"] = time.monotonic() - t0
        self.timings["emu_ms"] = 3000.0 * 3
        r_unity = hook_ab.compare(base, unity, 1.0)
        r_half = hook_ab.compare(base, half, 0.5)
        out["unity"] = r_unity
        out["half"] = r_half
        out["active_samples"] = r_unity["active_samples"]
        ok_unity = r_unity["identical"] and r_unity["active_samples"] > 1000
        ok_half = (
            r_half["length_equal"]
            and r_half["max_err_ulps"] <= 2.0
            and abs(r_half["rms_db"] + 6.0206) < 0.001
        )
        out["pass"] = bool(ok_unity and ok_half)
        (work / "report.json").write_text(json.dumps(out, indent=1, default=float))
        self.check(
            "unity gain is bit-identical to stock",
            bool(ok_unity),
            f"{r_unity['active_samples']} active samples",
        )
        self.check(
            "gain word 0.5 gives -6.0206 dB within 2 ulps",
            bool(ok_half),
            f"rms {r_half['rms_db']:.4f} dB, max err {r_half['max_err_ulps']} ulps",
        )


PARTS = [K.Part("run", "stock, unity, half", GainProof.part_run, True, 60)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("device", nargs="?", default="dt2", choices=("dn2", "dt2"))
    ap.add_argument("--stock")
    ap.add_argument(
        "--patched", help="default: the patch of fx/master-gain, built here"
    )
    ap.add_argument("--ready")
    ap.add_argument(
        "--run-dir",
        type=pathlib.Path,
        help="daemon run dir (default out/runs/patchkit/run-gain-DEV: keeps the patched boot cache)",
    )
    K.add_args(ap, PARTS)
    a = ap.parse_args()
    a.out = a.out or ROOT / "out" / "runs" / "gain-proof" / a.device
    GainProof.args = a
    return K.run_tool(__file__, GainProof, PARTS, a)


if __name__ == "__main__":
    sys.exit(main())
