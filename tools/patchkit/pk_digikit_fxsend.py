#!/usr/bin/env python3
"""The `fxsend` kind of the digikit scenarios: a run-time effect on the send route of `core/fx` (Digitakt II; docs/findings/62-fx-send-route.md).

Run by `tools/patchkit/pk_digikit_scen.py` (scenario `fx-send-dt2`). The image is the runtime with `core/dynfx` and `core/fxpages`; the sound is the run-time machine `test/sine` on track 1, the effect
`test/dyn-fx-gain` (p0 = 0.5, a call counter in its state) is loaded from the card at boot into row 0 of the chain. The runtime does not write the route and the amounts yet (a later
step): the scenario writes the row's words 18 to 22 with `dsp.patch` after the boot, as the runtime will over window 2 of `core/dsp-link`. Five boots, each in its own daemon:

  dry    the sine alone: the reference PCM.
  zero   the effect on route 1 (send), all amounts 0: the PCM is the dry PCM bit for bit (the send bus is silent, the return gets nothing); the effect ran once a block (the counter) and the
         master chain did not run it as well (no second call, no 0.5 gain on the master).
  other  an amount of 127 on track 2 (silent): the same, bit for bit.
  full   an amount of 127 on track 1 (the sine): the return carries 0.5 x the track, the master mixes it in: the PCM is the dry PCM times 1.5: the master mixes the FX return at unity, so the send adds 0.5 x the track.
  half   an amount of 64 on track 1: the added part is (64/127)^2 of the full amount's, within the tolerance of the master's own smoothing (the gain law is the stock sends' square)."""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

import numpy as np

from tools.patchkit import pk_device
from tools.patchkit import pk_dkrt_machine as M
from tools.patchkit.pk_digikit_fx import (
    FX,
    SINE,
    W_DIS,
    W_NFAULT,
    W_WAS,
    chain_rows,
    scaled,
)

W_ROUTE, W_AMT0 = 18, 19  # row words of the send route (core/fx module.toml)
GAIN = 0.5  # test/dyn-fx-gain


def aligned(ref: np.ndarray, run: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """REF and RUN from the note's onset in each, over the common length (the boots reach the note up to a few blocks apart)."""

    def onset(x: np.ndarray) -> int:
        nz = np.flatnonzero(np.abs(x) > 1e-6)
        return int(nz[0]) if len(nz) else 0

    a, b = onset(ref), onset(run)
    n = min(len(ref) - a, len(run) - b)
    return ref[a : a + n], run[b : b + n]


def added_factor(dry: np.ndarray, out: np.ndarray) -> float:
    """The median of (out - dry) / dry where the dry sound is above a fifth of its peak: the factor of the part that the send adds."""
    d, o = aligned(dry, out)
    sel = np.abs(d) > 0.2 * float(np.abs(d).max())
    return float(np.median((o[sel] - d[sel]) / d[sel]))


def run_fxsend(c: Any) -> None:
    from tools.patchkit.pk_digikit_scen import RT

    t = c.table()
    hint = t.get("hint", 7)
    trig = t.get("trig_frame", M.TRIG_FRAME)
    slots = pk_device.limits(c.device).fx_slots
    img = c.image_for(
        RT + ",core/dynfx,core/fxpages", "fxsend"
    )  # the test effect has a page: core/fxpages
    sine = c.pack(SINE, "m0.dkm")
    fx_file = c.pack(FX, "m1.dkm")
    pcm: dict[str, np.ndarray] = {}
    counts: dict[str, tuple[int, int]] = {}

    def sound(rig: Any, wav: Path) -> np.ndarray:
        rig.pick(rig.dyn_type(SINE, "sine"), hint)
        return rig.note(wav, trig)

    def set_row(rig: Any, words: dict[int, int]) -> None:
        base = M.sym(rig.plan, "core/fx", "fx.rows")
        text = "".join(f"DM {base + 4 * w:x} {v:08x}\n" for w, v in words.items())
        rig.emu.patch(text=text)

    def boot(tag: str, amounts: int | None) -> None:
        """Sine and effect; AMOUNTS None: the effect stays on the master route (not used), else route 1 and the word 19 = AMOUNTS (the others 0)."""
        card = c.card([sine, fx_file] if amounts is not None else [sine])
        with c.rig(img, card, tag) as rig:
            info = rig.boot_info()
            want = [0, 0] if amounts is not None else [0]
            c.check(
                f"{tag}: the sine{' and the effect' if amounts is not None else ''} loaded",
                info["results"] == want and info["safe"] == 0,
                f"{info['results']}",
            )
            if amounts is not None:
                row = chain_rows(rig, slots)[0]
                c.check(
                    f"{tag}: row 0 of the chain holds the effect, on the master route with no amount (words 18 to 22 are 0)",
                    row[0] != 0xFFFF_FFFF
                    and row[3] == 1
                    and row[W_ROUTE : W_AMT0 + 4] == (0,) * 5,
                    f"{[hex(x) for x in row]}",
                )
                set_row(rig, {W_ROUTE: 1, W_AMT0: amounts})
                row = chain_rows(rig, slots)[0]
                c.check(
                    f"{tag}: the route word is 1 and the amount word {amounts:#x}",
                    row[W_ROUTE] == 1 and row[W_AMT0] == amounts,
                    f"{row[W_ROUTE]} {row[W_AMT0]:#x}",
                )
            pcm[tag] = sound(rig, c.work / f"{tag}.wav")
            if amounts is not None:
                rows = chain_rows(rig, slots)
                after = rows[0]
                c.check(
                    f"{tag}: the effect ran (was 1), not switched off, no fault",
                    after[W_WAS] == 1 and after[W_DIS] == 0 and after[W_NFAULT] == 0,
                    f"was {after[W_WAS]} dis {after[W_DIS]} faults {after[W_NFAULT]}",
                )
                state_addr = after[2]
                n0 = struct.unpack("<I", rig.dsp_bytes(state_addr, 4))[0]
                rig.emu.call("run_until", frames=rig.frames() + 100)
                n1 = struct.unpack("<I", rig.dsp_bytes(state_addr, 4))[0]
                counts[tag] = (n0, n1)
                c.check(
                    f"{tag}: the effect's call counter goes up by one a block: the send hook runs it and the master chain does not",
                    n0 > 1000 and 90 <= n1 - n0 <= 110,
                    f"{n0} then {n1}",
                )
            c.done(rig)

    c.parallel(
        lambda: boot("dry", None),
        lambda: boot("zero", 0),
        lambda: boot("other", 0x7F00),
        lambda: boot("full", 0x7F),
        lambda: boot("half", 0x40),
    )
    dry = pcm["dry"]
    c.check(
        "dry: the sine is audible",
        float(abs(dry).max()) > 0.01,
        f"peak {float(abs(dry).max()):.3f}",
    )
    for tag, what in (
        ("zero", "route 1, all amounts 0"),
        ("other", "route 1, amount 127 on track 2 (silent)"),
    ):
        r = scaled(dry, pcm[tag], 1.0)
        c.check(
            f"{tag}: with {what} the PCM is the dry PCM, bit for bit from the onset on",
            bool(r["identical"]) and abs(r["shift"]) <= 128,
            f"onset shift {r['shift']}, max err {r.get('max_abs_err')}",
        )
    e_full = added_factor(dry, pcm["full"])
    e_half = added_factor(dry, pcm["half"])
    ratio = e_half / e_full if e_full else float("nan")
    want = (64 / 127) ** 2
    c.check(
        f"full: with an amount of 127 on track 1 the master mixes the effect's return: the PCM is the dry PCM times {1 + e_full:.5f} (the send adds {GAIN} x the track x the return's level {e_full / GAIN:.4f})",
        abs(e_full / GAIN - 1.0) < 1e-3,
        f"added factor {e_full:.5f}",
    )
    c.check(
        f"half: with an amount of 64 the added part is (64/127)^2 = {want:.5f} of the full amount's (the stock sends square their amount): {ratio:.5f}",
        abs(ratio / want - 1.0) < 0.01,
        f"added factor {e_half:.5f} against {e_full:.5f}",
    )
    c.check(
        "full: the PCM differs from the dry PCM (the effect is audible through the return)",
        not bool(scaled(dry, pcm["full"], 1.0)["identical"]),
        "",
    )
    c.common_checks()
