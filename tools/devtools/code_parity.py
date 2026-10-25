#!/usr/bin/env python3
"""Parity of the engine's run-time decoder with the generated instruction table, on a stock image.

    uv run python -m tools.devtools.code_parity [--device dt2|dn2] [--firmware 1.17] [--ms 800]

Boots the stock image of the device in one `digi-emud` with `SHARC_CODE_PARITY=1`. When the loaded DSP code
hashes as the generator's (`CODE_RANGES`), the engine decodes the loaded words of every range and compares them
with the generated table (`Engine::code_parity`) and prints one line to the daemon's stderr:
`code parity: N PCs compared, M differ [...]`. This tool prints that line. It must say `0 differ`: the barring
of only the changed blocks after a patch (`sharc/src/code_diff.rs`) relies on it. The daemon of the device's
generated core is the one `pk_harness.daemon_binary` picks (`DIGI_EMUD_<DEV>` names another).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

from tools.patchkit import pk_harness as H  # noqa: E402

DEFAULT_FIRMWARE = {"dt2": "1.17", "dn2": "1.12"}


def parity_line(device: str, firmware: str, ms: float) -> str:
    os.environ["SHARC_CODE_PARITY"] = "1"
    name = f"code-parity-{device}"
    cell = H.Cell(device, firmware)
    with H.Session(cell, H.Goldens(), log=None, run_name=name) as s:
        s.open()
        s.reset()
        s.run_ms(ms)
    err = (H.OUT / f"run-{name}" / "daemon.stderr").read_text(errors="replace")
    lines = [ln for ln in err.splitlines() if ln.startswith("code parity:")]
    return (
        "\n".join(lines)
        or "no `code parity:` line (the code did not hash as the generated one)"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--device", default="dt2", choices=("dt2", "dn2"))
    ap.add_argument("--firmware")
    ap.add_argument("--ms", type=float, default=800.0)
    a = ap.parse_args()
    os.chdir(ROOT)
    line = parity_line(a.device, a.firmware or DEFAULT_FIRMWARE[a.device], a.ms)
    print(f"{a.device}: {line}")
    return 0 if " 0 differ" in line else 1


if __name__ == "__main__":
    sys.exit(main())
