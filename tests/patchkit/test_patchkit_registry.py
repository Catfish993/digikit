"""The run-time registry (core/dynreg, the runtime's registry.rs, `patchkit pack` of a dynamic module): machines and
parameters that a `.dkm` adds at run time (docs/design/digikit-registry.md, finding 30).

The committed objects are current; the packer lowers a dynamic module's typed sections into `register` records
deterministically; the emulator proofs (tools/patchkit/pk_registry.py) patch the runtime and core/dynreg, load, refuse and
unload modules through the mailbox, and check the firmware's own list getter and the rows (about 12 s per image), and, on
a coupled daemon, the machine list on the panel, a track on the machine, the unload and the reload (frames pinned in
tests/patchkit/registry_frames.json)."""

import json

import pytest
from support.paths import REPO_ROOT

ROOT = REPO_ROOT

from tools.firmware import config  # noqa: E402
from tools.patchkit import pk_build, pk_dkrt, pk_registry, pk_toolchains  # noqa: E402

DYNREG = ROOT / "modules/core/dynreg"
RUNTIME = ROOT / "modules/core/runtime"


def test_committed_objects_are_current_and_plain_coldfire():
    obj = json.loads((DYNREG / "build/dynreg-dt2.dko.json").read_text())
    assert {r["type"] for r in obj["relocs"]} <= {"cf.32", "cf.pc32", "cf.pc16"}
    pk_build.build_module(DYNREG, check=True)
    if pk_toolchains.codegen_gcc_ready():
        pk_build.build_module(RUNTIME, check=True)


def test_pack_lowers_the_typed_sections_deterministically(tmp_path):
    tools = pk_dkrt.Tools()
    a, b = tmp_path / "a.dkm", tmp_path / "b.dkm"
    for out in (a, b):
        tools.patchkit_run("pack", "test/dyn-xslice", "--device", "dt2", "-o", out)
    assert a.read_bytes() == b.read_bytes()
    info = tools.patchkit_run("dkm-info", a)
    assert "device 1 (1 params, 1 machines)" in info
    assert 'machine type 16 "XSlice-D" "XSD" clone_of 6' in info
    # a run-time module needs its device, and a machine module that is not dynamic cannot be packed without code
    r = tools.run(
        tools.patchkit,
        "--root",
        ROOT,
        "pack",
        "test/dyn-xslice",
        "-o",
        tmp_path / "x.dkm",
    )
    assert r.returncode != 0 and "--device" in r.stderr
    r = tools.run(
        tools.patchkit,
        "--root",
        ROOT,
        "pack",
        "machines/xslice",
        "--device",
        "dt2",
        "-o",
        tmp_path / "y.dkm",
    )
    assert r.returncode != 0


@pytest.mark.parametrize("image", pk_registry.IMAGES)
def test_runtime_registers_machines_and_parameters(image, tmp_path):
    if not config.stock(pk_dkrt.DEVICE_SYX[image]).exists():
        pytest.skip(f"{pk_dkrt.DEVICE_SYX[image]} missing")
    report = pk_registry.proof(image, tmp_path)
    failed = [c for c in report["checks"] if not c["ok"]]
    assert not failed, failed
    assert report["write_watch"]["flush_hits"] == []
    assert set(report["write_watch"]["by_owner"]) <= {"core/machines"}


def test_the_write_watch_sees_the_runtime_when_its_code_is_not_allowed(tmp_path):
    """Control: with the allowed PC range moved elsewhere the same run reports the runtime's own writes."""
    if not config.stock("Digitakt_II_OS1.16.syx").exists():
        pytest.skip("Digitakt_II_OS1.16.syx missing")
    report = pk_registry.proof("dt2-1.16", tmp_path, control=True)
    watch = next(c for c in report["checks"] if c["name"].startswith("control"))
    assert watch["ok"], watch


@pytest.mark.slow
@pytest.mark.parametrize("image", pk_registry.IMAGES)
def test_the_machine_appears_on_the_panel_and_goes_away(image, tmp_path):
    from tools.patchkit import pk_harness as H

    try:
        H.firmware_inputs(H.Cell(image[:3], image[4:]))
    except H.HarnessError as e:
        pytest.skip(str(e))
    report = pk_registry.ui_proof(image, tmp_path)
    failed = [c for c in report["checks"] if not c["ok"]]
    assert not failed, failed
