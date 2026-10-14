"""fx/master-drive end to end in the emulator (slow: a coupled cold boot and six renders per device).

Skips without the inputs: DIGI_EMUD (the digi-emud binary of the device's core), the ready inputs
`out/ready-md-dt2` / `out/ready-md-dn2` (`tools/ready.py dt2|dn2 --syx ... --out ... --force`), the stock firmware
and Selache (the patched image is built here). The checks are the ones of tools/pk_proof_drive.py."""

import os
import subprocess
import sys

import pytest
from support.paths import REPO_ROOT

ROOT = REPO_ROOT
sys.path.insert(0, str(ROOT / "tools"))

import pk_toolchains  # noqa: E402

CASES = {
    "dt2": ("Digitakt_II_OS1.17.syx", "ready-md-dt2"),
    "dn2": ("Digitone_II_OS1.12.syx", "ready-md-dn2"),
}


@pytest.mark.parametrize("device", sorted(CASES))
def test_proof(device, tmp_path):
    syx, ready = CASES[device]
    if not os.environ.get("DIGI_EMUD"):
        pytest.skip("DIGI_EMUD not set")
    if not (ROOT / syx).is_file() or not (ROOT / "out" / ready).is_dir():
        pytest.skip(f"{syx} or out/{ready} missing")
    if pk_toolchains.selache_dir() is None:
        pytest.skip("selache not built")
    patched = tmp_path / f"md-{device}.syx"
    run = [sys.executable, str(ROOT / "tools" / "patchkit.py"), "patch"]
    done = subprocess.run(
        [
            *run,
            "--firmware",
            str(ROOT / syx),
            "--modules",
            "fx/master-drive",
            "-o",
            str(patched),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    proof = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "pk_proof_drive.py"),
            device,
            "--patched",
            str(patched),
            "--out",
            str(tmp_path / "proof"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert proof.returncode == 0, proof.stdout[-3000:] + proof.stderr[-1000:]
