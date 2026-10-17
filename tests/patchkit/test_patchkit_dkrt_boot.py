"""Boot-time loading of run-time modules (finding 37): the selection record, the anchors of the boot hook on the four ports, the
packed test modules; and, slow, the emulator proof (tools/patchkit/pk_dkrt_boot.py)."""

import hashlib
import shutil
import subprocess
import tomllib

import pytest
from support.paths import REPO_ROOT

ROOT = REPO_ROOT

from tools.firmware import config  # noqa: E402
from tools.patchkit import pk_build, pk_dkrt, pk_dkrt_boot, pk_toolchains  # noqa: E402

RUNTIME = ROOT / "modules/core/runtime"
IMAGES = {
    "dt2-1.16": "Digitakt_II_OS1.16.syx",
    "dt2-1.17": "Digitakt_II_OS1.17.syx",
    "dn2-1.11": "Digitone_II_OS1.11.syx",
    "dn2-1.12": "Digitone_II_OS1.12.syx",
}


def test_the_record_layout_is_the_documented_one():
    rec = pk_dkrt_boot.make_record(["modules/a.dkm", "modules/b.dkm"], seq=7, counter=1)
    assert len(rec) == 512 and rec[:4] == b"DKSL"
    # the same bytes as the Rust test of dkrt_sdk::sel (modules/core/runtime/sdk/src/lib.rs)
    assert rec[508:] == bytes.fromhex("ce5a5f11")
    assert hashlib.sha256(rec).hexdigest().startswith("f274068ed39da26c")
    back = pk_dkrt_boot.parse_record(rec)
    assert back == {
        "seq": 7,
        "counter": 1,
        "flags": 0,
        "paths": ["modules/a.dkm", "modules/b.dkm"],
    }
    torn = bytearray(rec)
    torn[100] ^= 1
    assert pk_dkrt_boot.parse_record(bytes(torn)) is None
    assert pk_dkrt_boot.parse_record(bytes(512)) is None
    with pytest.raises(pk_dkrt_boot.Fail):
        pk_dkrt_boot.make_record(["x" * 56])


def test_the_sdk_record_code_passes_its_own_tests(tmp_path):
    rustc = shutil.which("rustc")
    if rustc is None:
        pytest.skip("rustc not found")
    exe = tmp_path / "sdk_test"
    sdk = RUNTIME / "sdk/src/lib.rs"
    r = subprocess.run(
        [rustc, "--edition", "2021", "--test", str(sdk), "-o", str(exe)],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    r = subprocess.run([str(exe)], capture_output=True, text=True)
    assert r.returncode == 0 and "7 passed" in r.stdout, r.stdout


def test_the_store_profile_reserves_the_two_record_sectors():
    for dev in ("digitakt-ii", "digitone-ii"):
        store = tomllib.loads((ROOT / "devices" / f"{dev}.toml").read_text())["card"][
            "store"
        ]
        assert (
            store["end_sector"] == pk_dkrt_boot.STORE_FIRST + pk_dkrt_boot.STORE_SECTORS
        )
        assert store["first_sector"] == pk_dkrt_boot.STORE_FIRST
        assert store["selection_sectors"] == 2
        assert store["header_b_sector"] == store["end_sector"] - 2 - 1
        assert store["header_b_sector"] == pk_dkrt_boot.STORE_FIRST + 0x5FFD
        assert (
            store["end_sector"] - 2 == pk_dkrt_boot.SECTOR_A
            and store["end_sector"] - 1 == pk_dkrt_boot.SECTOR_B
        )


@pytest.mark.parametrize("image", sorted(IMAGES))
def test_the_boot_anchors_sit_on_the_prologue_of_the_main_os_task_and_the_block_write(
    image,
):
    from tools.patchkit import port_resolve

    syx = config.stock(IMAGES[image])
    if not syx.exists():
        pytest.skip(f"{IMAGES[image]} missing")
    anchors = {
        a["name"]: a
        for a in tomllib.loads((ROOT / "devices/ports" / f"{image}.toml").read_text())[
            "anchor"
        ]
    }
    main = port_resolve.sections_of(syx)[3]
    base = 0x40000400
    boot = anchors["dkrt.hook.boot"]
    # `link a6,#-0xa8; movem.l d2-d7/a2-a5,(sp)`: the 8 bytes the hook displaces
    assert main[boot["addr"] - base : boot["addr"] - base + 8] == bytes.fromhex(
        "4e56ff5848d73cfc"
    )
    assert boot["len"] == 8
    # the block write is a function (it starts with `lea -N(sp),sp`, like the block read), the queue literal sits in a `pea <queue>`
    w = anchors["dkrt.fw.block_write"]["addr"]
    assert main[w - base : w - base + 2] == bytes.fromhex("4fef")
    q = anchors["dkrt.ui.queue_lit"]["addr"]
    assert main[q - base - 2 : q - base] == bytes.fromhex("4879") and main[
        q - base + 4 : q - base + 6
    ] == bytes.fromhex("4eb9")


@pytest.mark.parametrize("image", sorted(IMAGES))
def test_the_upload_hook_sits_on_the_router_call_of_the_midi_task_and_the_send_function_is_found(
    image,
):
    """docs/design/digikit-upload.md: the hook replaces `movea.l (0,A2,D1*4),A0; jsr (A0)` (the router call with its arguments pushed)
    and is followed by the task's `lea 12,sp`; the MIDI send function starts like the one the rule hashes."""
    from tools.patchkit import port_resolve

    syx = config.stock(IMAGES[image])
    if not syx.exists():
        pytest.skip(f"{IMAGES[image]} missing")
    anchors = {
        a["name"]: a
        for a in tomllib.loads((ROOT / "devices/ports" / f"{image}.toml").read_text())[
            "anchor"
        ]
    }
    main = port_resolve.sections_of(syx)[3]
    base = 0x40000400
    hook = anchors["dkrt.hook.midi_rx"]
    at = hook["addr"] - base
    assert hook["len"] == 6 and main[at : at + 6] == bytes.fromhex("20721c004e90")
    assert main[at + 6 : at + 10] == bytes.fromhex(
        "4fef000c"
    )  # lea 12(sp),sp: pops msg, len, source
    send = anchors["dkrt.fw.midi_send"]["addr"]
    assert main[send - base : send - base + 4] == bytes.fromhex("4feffff4")


def test_committed_runtime_objects_are_current():
    if not pk_toolchains.codegen_gcc_ready():
        pytest.skip("Rust-for-ColdFire toolchain not installed")
    pk_build.build_module(RUNTIME, check=True)
    pk_build.build_module(ROOT / "modules/test/dyn-fault", check=True)


def test_the_fault_module_packs_for_both_devices(tmp_path):
    tools = pk_dkrt.Tools()
    for dev in ("dt2", "dn2"):
        out = tmp_path / f"{dev}.dkm"
        tools.patchkit_run("pack", "test/dyn-fault", "--device", dev, "-o", out)
        assert "test/dyn-fault" in tools.patchkit_run("dkm-info", out)


@pytest.mark.slow
@pytest.mark.parametrize("image", ["dt2-1.17", "dn2-1.12"])
def test_boot_load_restart_cut_and_safe_mode(image, tmp_path):
    if not config.stock(IMAGES[image]).exists():
        pytest.skip(f"{IMAGES[image]} missing")
    proof = pk_dkrt_boot.Proof(image, True, tmp_path, set())
    report = proof.run()
    failed = [c for c in report["checks"] if not c["ok"]]
    assert not failed, failed
