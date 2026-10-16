"""core/runtime and the .dkm packer (digikit runtime v0, finding 29).

The committed objects are current and plain ColdFire; the packer is deterministic and the file checks itself; the emulator
proof (tools/patchkit/pk_dkrt.py) patches the runtime into a firmware, loads a packed module from a card store through the
mailbox, watches its counter rise while the UI task runs, unloads it, and runs the negative cases, with the write-watch and
the flush check on. One boot of about 25 s per image."""

import json
import subprocess

import pytest
from support.paths import REPO_ROOT

ROOT = REPO_ROOT

from tools.firmware import config  # noqa: E402
from tools.patchkit import pk_build, pk_dkrt, pk_toolchains  # noqa: E402

RUNTIME = ROOT / "modules/core/runtime"
DEMO = ROOT / "modules/test/dyn-demo"


def test_committed_objects_are_current_and_plain_coldfire():
    for path in (
        RUNTIME / "build/runtime.dko.json",
        RUNTIME / "build/tramp.dko.json",
        DEMO / "build/dyn-demo.dko.json",
    ):
        obj = json.loads(path.read_text())
        assert {r["type"] for r in obj["relocs"]} == {"cf.32"}, path
        assert int(obj["toolchain"]["instructions_redecoded"]) > 10, path
    if not pk_toolchains.codegen_gcc_ready():
        pytest.skip("Rust-for-ColdFire toolchain not installed")
    pk_build.build_module(RUNTIME, check=True)
    pk_build.build_module(DEMO, check=True)


def test_pack_is_deterministic_and_the_file_checks_itself(tmp_path):
    tools = pk_dkrt.Tools()
    a, b = tmp_path / "a.dkm", tmp_path / "b.dkm"
    pk_dkrt.pack(tools, "test/dyn-demo", a)
    pk_dkrt.pack(tools, "test/dyn-demo", b)
    assert a.read_bytes() == b.read_bytes()
    info = tools.patchkit_run("dkm-info", a)
    assert "test/dyn-demo" in info and "slots     [(0," in info
    bad = bytearray(a.read_bytes())
    bad[-1] ^= 1
    (tmp_path / "bad.dkm").write_bytes(bad)
    r = tools.run(tools.patchkit, "--root", ROOT, "dkm-info", tmp_path / "bad.dkm")
    assert r.returncode != 0 and "sha256" in r.stderr
    # a name the runtime does not know is packed (the runtime refuses it at load time); a PC-relative one is not
    assert subprocess.run([str(tools.patchkit), "--root", str(ROOT), "pack", "nope/none", "-o", str(tmp_path / "x")],
                          capture_output=True).returncode != 0  # fmt: skip


@pytest.mark.parametrize("image", ["dt2-1.16", "dn2-1.11"])
def test_runtime_loads_runs_and_unloads_a_module(image, tmp_path):
    if not config.stock(pk_dkrt.DEVICE_SYX[image]).exists():
        pytest.skip(f"{pk_dkrt.DEVICE_SYX[image]} missing")
    report = pk_dkrt.proof(image, tmp_path)
    failed = [c for c in report["checks"] if not c["ok"]]
    assert not failed, failed
    n = report["numbers"]
    assert n["counter"][0] < n["counter"][1] < n["counter"][2]
    assert n["write_watch"]["events"] == 0


def test_the_write_watch_sees_the_runtime_when_its_code_is_not_allowed(tmp_path):
    """Control: with the allowed PC range moved elsewhere the same run reports the runtime's own writes."""
    if not config.stock("Digitakt_II_OS1.16.syx").exists():
        pytest.skip("Digitakt_II_OS1.16.syx missing")
    report = pk_dkrt.proof("dt2-1.16", tmp_path, control=True)
    watch = next(c for c in report["checks"] if c["name"].startswith("control"))
    assert watch["ok"], watch
