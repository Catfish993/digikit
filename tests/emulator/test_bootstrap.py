"""The real Digitakt II bootstrap on the native ColdFire core (tools/emulator/bootstrap_run.py, native/bootstrap).

Each test runs unmodified bootstrap code with a modelled flash, panel and MIDI UART. They need the Digitakt II 1.16
firmware in the repo root and a cargo toolchain (the first call builds native/bootstrap, no generated core).
"""

import struct

import pytest
from support.paths import REPO_ROOT

ROOT = REPO_ROOT
SYX = ROOT / "Digitakt_II_OS1.16.syx"
pytestmark = pytest.mark.skipif(
    not SYX.exists(), reason="needs Digitakt_II_OS1.16.syx in the repo root"
)


from tools.emulator import bootstrap_run as br  # noqa: E402


@pytest.fixture(scope="module")
def boot():
    return br.extract_boot(SYX)


@pytest.fixture(scope="module")
def faulting_flash(tmp_path_factory, boot):
    bad = br.faulting_main_syx(SYX, tmp_path_factory.mktemp("bad") / "faulting.syx")
    return bad, bytes(br.flash_with(br.container_of(bad)))


def test_stock_boot_loads_main_os_and_leaves_flash_alone(boot):
    flash = bytes(br.flash_with(br.container_of(SYX)))
    main = br.section3(SYX)
    r = br.run(
        boot,
        flash,
        watches=(
            br.PC_UPGRADE_ROUTINE,
            br.PC_UPGRADE_GATE_EXIT,
            br.PC_MENU,
            br.PC_LOAD_MAIN,
        ),
        dumps=((br.MAIN_LOAD, len(main)),),
    )
    assert r.report["end"] == "main_os:0x400004e8"
    assert r.dumps[f"{br.MAIN_LOAD:#x}"] == main
    # The bootstrap-version gate runs and exits (running version >= installed one); no menu; nothing written.
    assert r.hits(br.PC_UPGRADE_ROUTINE) == 1 and r.hits(br.PC_UPGRADE_GATE_EXIT) == 1
    assert r.hits(br.PC_MENU) == 0
    assert r.report["flash"]["programmed_bytes"] == 0 and r.flash == flash


def test_main_os_is_not_validated_before_the_jump(boot):
    flash = bytearray(br.flash_with(br.container_of(SYX)))
    for k in (0x40000, 0x80000):  # two bytes inside section 3's packed stream
        flash[br.CONTAINER_AT + 0x3FB0 + k] ^= 0xFF
    r = br.run(
        boot,
        bytes(flash),
        watches=(br.PC_LOAD_MAIN,),
        dumps=((br.MAIN_LOAD, len(br.section3(SYX))),),
    )
    assert r.report["end"].startswith("main_os")
    assert r.dumps[f"{br.MAIN_LOAD:#x}"] != br.section3(SYX)


def test_func_reaches_the_menu_before_a_faulting_main_os(boot, faulting_flash):
    _bad, flash = faulting_flash
    # Without the key, MAIN OS runs and faults at its first instruction.
    r = br.run(
        boot, flash, run_main=True, watches=(br.PC_MENU, br.PC_LOAD_MAIN), seconds=30
    )
    assert r.report["end"] == "fault:vector4@0x40000404"
    assert r.hits(br.PC_MENU) == 0 and r.hits(br.PC_LOAD_MAIN) == 1
    # With [FUNC] held, the STARTUP MENU comes up and MAIN OS is never loaded.
    r = br.run(
        boot,
        flash,
        hold=("func",),
        run_main=True,
        stops=(br.PC_KEY_WAIT,),
        watches=(br.PC_MENU, br.PC_LOAD_MAIN),
    )
    assert r.report["end"] == f"stop_pc:{br.PC_KEY_WAIT:#x}"
    assert r.hits(br.PC_MENU) == 1 and r.hits(br.PC_LOAD_MAIN) == 0
    assert r.report["main_os_entry"] is None
    assert br.render_panel(r.panel).getbbox() is not None


def test_a_higher_table_version_without_a_matching_image_is_refused_and_forces_the_menu(
    boot,
):
    c = bytearray(br.container_of(SYX))
    for k in range(struct.unpack_from(">I", c, 0x1C)[0]):
        if struct.unpack_from(">I", c, 0x20 + 16 * k)[0] == 2:
            struct.pack_into(">I", c, 0x20 + 16 * k + 12, 0x02020000)
    r = br.run(
        boot,
        bytes(br.flash_with(bytes(c))),
        stops=(br.PC_KEY_WAIT,),
        watches=(br.PC_LOAD_MAIN,),
    )
    assert r.report["end"] == f"stop_pc:{br.PC_KEY_WAIT:#x}"
    assert r.hits(br.PC_LOAD_MAIN) == 0
    assert (
        r.report["flash"]["programmed_bytes"] == 0
        and not r.report["flash"]["erased_sectors"]
    )


def test_receive_accepts_the_stock_image_and_boots_it():
    v = br.receive(SYX)
    assert v.accepted, v.detail
    flash = v.run.report["flash"]
    # Only the OS container area is written: sectors from 0x80000, nothing below it (the bootstrap is not rewritten).
    assert flash["min"] == "0x80000" and flash["not_erased_programs"] == 0
    assert v.run.hits(br.PC_CONTENT_CHECK) == 1 and v.run.hits(br.PC_HMAC) == 1
    c = br.container_of(SYX)
    assert v.run.flash[br.CONTAINER_AT : br.CONTAINER_AT + len(c)] == c


def test_receive_refuses_a_content_checksum_or_hmac_failure():
    for kind, hmac_runs in (("content", 0), ("hmac", 1)):
        v = br.receive(SYX, midi_bytes=br.corrupt_syx(SYX, kind))
        assert not v.accepted and v.detail == "UPGRADE ABORTED", kind
        assert v.run.hits(br.PC_HMAC) == hmac_runs, kind
        assert (
            v.run.report["flash"]["programmed_bytes"] == 0
            and not v.run.report["flash"]["erased_sectors"]
        )


@pytest.mark.slow
def test_receive_ignores_a_message_with_a_bad_packet_checksum():
    v = br.receive(SYX, midi_bytes=br.corrupt_syx(SYX, "packet"))
    assert not v.accepted and v.run.hits(br.PC_CONTENT_CHECK) == 0
    assert v.run.report["flash"]["programmed_bytes"] == 0


def test_recovery_from_a_faulting_main_os(faulting_flash):
    bad, _flash = faulting_flash
    v = br.receive(SYX, base=bad)
    assert v.accepted, v.detail
