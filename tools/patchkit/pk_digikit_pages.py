#!/usr/bin/env python3
"""Parameter pages of a machine chosen on track 1: the OLED frames of the patched-in machine against the run-time one (finding 50).

Run by `tools/patchkit/pk_digikit_scen.py` as the `pages` kind. Both boots choose the machine in the SRC list, then press SRC again
for page 2 and turn encoders; the frames go to out/runs/lanes/r1-pages/ (never committed). The kept check is that the run-time
machine's frames equal the patched-in twin's on every page (the same machine, the same data; only the type number differs and it is
not drawn), and that page 2 is not blank.
"""

from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from tools.patchkit import pk_dkrt_machine as M

ROOT = Path(__file__).resolve().parents[2]


def out_dir() -> Path:
    d = Path(
        os.environ.get(
            "DIGIKIT_PAGES_OUT", ROOT / "out" / "runs" / "lanes" / "r1-pages"
        )
    )
    d.mkdir(parents=True, exist_ok=True)
    return d


WAVE_ROW = 157  # the waveform's baseline in the sample view (the knob page has no long line there)


def sample_view(png: bytes) -> bool:
    """True when the frame is the sample view (SAMPLE, WERP, STRETCH, REPITCH: a line across the waveform area)."""
    im = np.array(Image.open(io.BytesIO(png)).convert("L"))
    return int((im[WAVE_ROW] > 128).sum()) > 300


def run_pages(c: Any) -> None:
    from tools.patchkit.pk_digikit_scen import RT

    t = c.table()
    shots = out_dir()
    mods = [c.pack(m, f"m{i}.dkm") for i, m in enumerate(t["modules"])]
    run_img = c.image_for(RT, "run")
    ref_img = c.image_for(RT + "," + t["twin"], "ref")
    empty = c.card(None)
    card = c.card(mods)
    frames: dict[str, dict[str, bytes]] = {"ref": {}, "run": {}}

    def sequence(rig: Any, who: str, want: int) -> None:
        def snap(name: str) -> None:
            png = rig.emu.frame_png()
            frames[who][name] = png
            (shots / f"{who}-{name}.png").write_bytes(png)

        rig.select_type(want, t["hint"])
        rig.s.run_ms(500)
        snap("p0-after-select")
        for page in (1, 2):
            rig.s.do_tap("SRC")
            rig.s.run_ms(500)
            snap(f"p{page}-src")
            for enc in (1, 2, 5):  # a touched knob shows its parameter's name and value
                rig.s.turn_burst(enc, 4, 12)
                rig.s.run_ms(100)
                snap(f"p{page}-turn{enc}")
        rig.s.run_ms(300)

    def ref_boot() -> None:
        with c.rig(ref_img, empty, "ref") as rig:
            sequence(rig, "ref", t["static_type"])
            c.done(rig)

    def run_boot() -> None:
        with c.rig(run_img, card, "run") as rig:
            sequence(rig, "run", M.PLUCK_DYN_TYPE)
            c.done(rig)

    stock: dict[int, bytes] = {}

    def stock_boot() -> None:
        with c.rig(run_img, empty, "stock") as rig:
            for downs in t.get("stock_downs", []):
                rig.select(downs)
                typ = rig.type_now()
                rig.s.run_ms(500)
                for page in (1, 2):
                    rig.s.do_tap("SRC")
                    rig.s.run_ms(500)
                    png = rig.emu.frame_png()
                    (shots / f"stock{typ}-p{page}.png").write_bytes(png)
                    if page == 1:
                        stock[typ] = png
            c.done(rig)

    c.parallel(ref_boot, run_boot, stock_boot)
    for name, png in frames["run"].items():
        c.check(
            f"frame {name}: the run-time machine draws what the patched-in twin draws",
            png == frames["ref"].get(name),
            f"{len(png)} / {len(frames['ref'].get(name, b''))} bytes",
        )
    shown = {n: sample_view(png) for n, png in frames["run"].items()}
    c.check(
        "an added machine draws the knob page on both pages, never the sample view (finding 50)",
        not any(shown.values()),
        str([n for n, v in shown.items() if v]),
    )
    sv = {typ: sample_view(png) for typ, png in stock.items()}
    c.check(
        "the stock sample machines still draw the sample view on their second page (the hook leaves types below machines.first alone)",
        bool(stock)
        and all(v for typ, v in sv.items() if typ in (0, 1, 3))
        and {0, 1, 3} <= set(sv),
        str(sv),
    )
    c.common_checks()
